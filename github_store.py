"""Durable run/comment storage on a GitHub branch (stdlib-only, no extra deps).

Streamlit Community Cloud's disk is EPHEMERAL — runs and comments written there vanish
on every restart/redeploy. This module persists them to a dedicated `data` branch in the
same repo via the GitHub REST API, and pulls them back on app startup, so saved runs
accumulate permanently and are visible to everyone.

Why a SEPARATE branch (not `main`): Streamlit auto-redeploys on any commit to the deployed
branch. Writing data to `main` would trigger a rebuild on every run. The `data` branch is
never watched, so writes are silent.

Config (env / Streamlit secrets):
    GITHUB_TOKEN  — a PAT (fine-grained: Contents read/write on the repo) — REQUIRED to write.
    GITHUB_REPO   — "owner/name" (e.g. kelp-pranav/kelp-onepager).
    GITHUB_DATA_BRANCH — storage branch name, default "data".

All functions degrade gracefully: with no token/repo configured they no-op and the app
falls back to local (ephemeral) behaviour. Network/API errors never raise into the UI.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import List, Tuple

_API = "https://api.github.com"


def _conf() -> Tuple[str, str, str]:
    return (os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPO", ""),
            os.getenv("GITHUB_DATA_BRANCH", "data"))


def configured() -> bool:
    """True when a token + repo are set (so writes are possible)."""
    token, repo, _ = _conf()
    return bool(token and repo)


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": accept,
            "User-Agent": "kelp-onepager-app", "X-GitHub-Api-Version": "2022-11-28"}


def _request(method: str, url: str, token: str, body: dict = None,
             accept: str = "application/vnd.github+json") -> Tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = _headers(token, accept)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # network/DNS/etc — treat as soft failure
        return 0, str(e).encode()


def _ensure_branch(token: str, repo: str, branch: str) -> bool:
    """Make sure the data branch exists; create it from the default branch if missing."""
    status, _ = _request("GET", f"{_API}/repos/{repo}/branches/{branch}", token)
    if status == 200:
        return True
    # Find the default branch's head sha, then create refs/heads/<branch> from it.
    st_repo, body = _request("GET", f"{_API}/repos/{repo}", token)
    if st_repo != 200:
        return False
    default = json.loads(body).get("default_branch", "main")
    st_ref, body = _request("GET", f"{_API}/repos/{repo}/git/ref/heads/{default}", token)
    if st_ref != 200:
        return False
    sha = json.loads(body).get("object", {}).get("sha")
    if not sha:
        return False
    st_new, _ = _request("POST", f"{_API}/repos/{repo}/git/refs", token,
                         {"ref": f"refs/heads/{branch}", "sha": sha})
    return st_new in (200, 201)


def put_file(repo_path: str, content: bytes, message: str) -> Tuple[bool, str]:
    """Create/update one file on the data branch. Returns (ok, detail)."""
    token, repo, branch = _conf()
    if not (token and repo):
        return False, "GitHub storage not configured"
    if not _ensure_branch(token, repo, branch):
        return False, "could not access/create data branch"
    url = f"{_API}/repos/{repo}/contents/{repo_path}"
    # Need the current sha to update an existing file.
    sha = None
    status, body = _request("GET", f"{url}?ref={branch}", token)
    if status == 200:
        try:
            sha = json.loads(body).get("sha")
        except json.JSONDecodeError:
            pass
    payload = {"message": message, "branch": branch,
               "content": base64.b64encode(content).decode("ascii")}
    if sha:
        payload["sha"] = sha
    status, body = _request("PUT", url, token, payload)
    if status in (200, 201):
        return True, "ok"
    return False, f"{status}: {body.decode('utf-8', 'replace')[:160]}"


def pull_dir(remote_dir: str, local_dir: str) -> int:
    """Download every file under `remote_dir` on the data branch into `local_dir`.
    Returns the number of files written. No-op (0) when unconfigured or the dir is absent."""
    token, repo, branch = _conf()
    if not (token and repo):
        return 0
    status, body = _request(
        "GET", f"{_API}/repos/{repo}/contents/{remote_dir}?ref={branch}", token)
    if status != 200:
        return 0  # 404 = nothing stored yet
    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return 0
    if not isinstance(items, list):
        return 0
    os.makedirs(local_dir, exist_ok=True)
    written = 0
    for it in items:
        if it.get("type") != "file":
            continue
        name = it.get("name")
        path = it.get("path")
        if not name or not path:
            continue
        st_f, raw = _request("GET", f"{_API}/repos/{repo}/contents/{path}?ref={branch}",
                             token, accept="application/vnd.github.raw")
        if st_f != 200:
            continue
        try:
            with open(os.path.join(local_dir, name), "wb") as fh:
                fh.write(raw)
            written += 1
        except OSError:
            continue
    return written
