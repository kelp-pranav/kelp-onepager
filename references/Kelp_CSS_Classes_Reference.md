# Kelp CSS Classes Reference

This is a complete mapping of all CSS classes used in the Embio one-pager.
Copy this into style.py as documentation for Claude Code.

## Layout & Grid
- `.page` — max-width container
- `.hdr` — header row with border-bottom, flex space-between
- `.body` — two-column grid: 1.85fr (left) + 1fr (right)
- `.col-l` — left column with right border
- `.col-r` — right column with light gray background

## Sections & Typography
- `.sec` — section block with margin-bottom
- `.sec-h` — section header: bold 11px, gray bottom border 0.5px, flex with gap (for tags)
- `.tag` — pill badge: .tg (gray) or .tp (blue)
- `body-text` — standard paragraph: 11px, line-height 1.6

## Tables
- `table` — border-collapse, 100% width, 10px font
- `th` — #F7F8FA bg, #5A6878 text, 0.5px border
- `th:first-child` — text-align: left
- `td` — 0.5px border, right-aligned by default
- `td:first-child` — left-aligned, font-weight: 500
- `tr:nth-child(even) td` — #FCFCFC alternating rows
- `tr.tot` — bold, #F7F8FA bg, top border
- `tr.hl` — #F0F6F0 highlight (current company)
- `td.up` — green #2A7A30 (positive)
- `td.dn` — red #C0392B (negative)
- `td.nu` — muted #8A9AB0 (neutral)
- `td.la` — text-align: left
- `td.sm` — font-size: 9.5px

## Lists & Bullets
- `ul` — no bullets, padding: 0
- `li` — 10.5px, position: relative, ::before bullet at left

## Pills & Badges
- `.pill` — inline-block, 9px font, 1px 6px padding, border-radius: 100px, margin: 1px 2px
- `.pg` — green background (#EAF3DE) + text (#27500A) — used for ✓ positive items
- `.pr` — red background (#FCEBEB) + text (#791F1F) — used for negative/warning
- `.pa` — amber background (#FAEEDA) + text (#633806) — used for caution/pending
- `.pb` — blue background (#EAF3FF) + text (#185FA5) — used for institutional/sector tags
- `.pn` — gray background (#F1EFE8) + text (#5F5E5A) — used for neutral/N/A

## Detail Rows (right column info)
- `.dr` — flex row, gap: 5px, padding: 2px 0, border-bottom: 0.5px
- `.dl` — label column: width: 82px, flex-shrink: 0, color: #5A6878, font-weight: 500
- `.dv` — value column: flex: 1, color: #1A2332

## Flag Rows (risk/compliance checkmarks)
- `.fl` — flex row, gap: 6px, padding: 3px 0, border-bottom: 0.5px, align-items: flex-start
- `.fi` — icon 14×14px, border-radius: 2px, flex-shrink: 0, centered
- `.fig` — green icon (#EAF3DE bg + #27500A text) — used for ✓ clean
- `.fia` — amber icon (#FAEEDA bg + #854F0B text) — used for ! watch
- `.fir` — red icon (#FCEBEB bg + #A32D2D text) — used for ! risk

## Stats & Numbers
- `.stats` — flex row with gap: 5px
- `.st` — each stat card: flex: 1, padding: 5px, border: 0.5px, border-radius: 3px
- `.sv` — stat value: font-size: 13px, font-weight: 600, color: #1A2332
- `.sl` — stat label: font-size: 8.5px, color: #5A6878, margin-top: 1px
- `.ss` — stat sub (YoY change): font-size: 8.5px, .green or .red class

## Colors (semantic)
- `.green` — #2A7A30 (used for up arrows ↑ and positive metrics)
- `.red` — #C0392B (used for down arrows ↓ and negative metrics)
- `.blue` — #1E5FA8 (used for blue accents)
- `.amber` — #854F0B (used for amber/caution)

## Grid Layouts
- `.g2` — grid-template-columns: 1fr 1fr, gap: 10px (two-column inside section)
- `.g3` — grid-template-columns: 1fr 1fr 1fr, gap: 6px (three-column inside section)

## SWOT Grid
- `.swot` — grid: 1fr 1fr, gap: 5px (creates 2×2)
- `.sw` — each quadrant box: padding: 6px 8px, border-radius: 3px
- `.sw.s` — Strengths: #EAF3DE background
- `.sw.w` — Weaknesses: #FCEBEB background
- `.sw.o` — Opportunities: #EAF3FF background
- `.sw.t` — Threats: #FAEEDA background
- `.sw-h` — SWOT heading inside box: font-size: 9px, uppercase, color varies by quadrant
- `.sw li` — bullet items inside: font-size: 9.5px, color varies (#3B6D11, #A32D2D, #185FA5, #854F0B)

## Charts & Bars (CSS bar visualization)
- `.br` — flex row with gap: 5px, align-items: center, margin-bottom: 3px
- `.bn` — bar name label: font-size: 9.5px, color: #5A6878, width: 115px, text-align: right
- `.bt` — bar track: flex: 1, height: 8px, background: #F0F2F5, border-radius: 1px, overflow: hidden
- `.bf` — bar fill (inside .bt): height: 100%, border-radius: 1px, background: varies (dark, green, etc)
- `.bv` — bar percentage value: font-size: 9.5px, font-weight: 500, width: 30px, text-align: right

## Rating Boxes
- `.rbox` — flex row with gap: 6px
- `.rb` — each rating box: flex: 1, border: 0.5px, border-radius: 3px, padding: 5px 8px, text-align: center
- `.rv` — rating value: font-size: 14px, font-weight: 600

## News Items
- `.ni` — padding: 3.5px 0, border-bottom: 0.5px
- `.nd` — date: font-size: 8.5px, color: #8A9AB0, margin-bottom: 1px
- `.nt` — title: color: #1A2332, font-weight: 500, line-height: 1.35
- `.ns` — source: font-size: 8.5px, color: #8A9AB0, margin-top: 1px

## Milestones & Timelines
- `.ms` — milestone row: flex row, gap: 7px, padding: 3px 0, border-bottom: 0.5px
- `.md` — milestone date label: color: #5A6878, width: 48px, flex-shrink: 0, font-weight: 500

## Deals Timeline
- `.deal` — deal row: flex row, gap: 7px, padding: 3px 0, border-bottom: 0.5px
- `.dd` — deal date label: color: #5A6878, width: 52px, flex-shrink: 0, font-weight: 500

## Catalysts Timeline
- `.cat` — catalyst row: flex row, gap: 7px, padding: 3px 0, border-bottom: 0.5px
- `.cd` — catalyst date label: color: #3C9E41, font-weight: 500, width: 48px, flex-shrink: 0

## Utility
- `.note` — font-size: 9px, color: #8A9AB0, margin-top: 3px, font-style: italic
- `.nalert` — font-size: 9.5px, padding: 4px 7px, border-radius: 3px, margin-top: 4px
- `.na` — amber alert: #FAEEDA bg + #633806 text
- `.nb` — blue alert: #EAF3FF bg + #185FA5 text
- `.ng` — green alert: #EAF3DE bg + #27500A text

## Management
- `.mgmt` — padding: 4px 0, border-bottom: 0.5px
- `.mn` — name: font-size: 10.5px, font-weight: 500, color: #1A2332
- `.mb` — bio: font-size: 9.5px, color: #8A9AB0, line-height: 1.45, margin-top: 1px

## Investment Thesis Box
- `.thesis` — padding: 7px 10px, border-left: 2.5px solid #3C9E41, background: #F5FBF5, border-radius: 0 3px 3px 0, margin-top: 6px
- `.th` — thesis heading: font-size: 9px, font-weight: 600, color: #27500A, margin-bottom: 4px

## Footer
- `.ftr` — border-top: 0.5px, padding: 6px 26px, display: flex, justify-content: space-between, background: #FAFAFA
- `.fl-t` — footer left text: font-size: 8.5px, color: #8A9AB0
- `.fr-t` — footer right text: font-size: 8.5px, color: #8A9AB0, font-weight: 600

## Colors Used (hex values)
Primary text: #1A2332
Secondary text: #5A6878
Muted text: #8A9AB0
Border: #E5E8EE
Table header bg: #F7F8FA
Right column bg: #FAFAFA
Kelp green: #3C9E41
Green pill/success: #EAF3DE (#27500A text)
Red pill/danger: #FCEBEB (#791F1F text)
Amber pill/warning: #FAEEDA (#633806 text)
Blue pill/info: #EAF3FF (#185FA5 text)
Gray pill/neutral: #F1EFE8 (#5F5E5A text)
