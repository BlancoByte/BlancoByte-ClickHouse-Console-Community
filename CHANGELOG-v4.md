# Changelog — v4 Phase 4z-da (Compliance Pack: custom date range)

## New
The window selector now offers 'Custom range…' alongside the
existing presets. Picking it reveals two datetime-local fields
(From / To) below the controls row, seeded with 'last 7 days'
on first selection so the user starts somewhere sensible.

## Backend
POST /api/compliance/export-pack now accepts:
  - from (ISO datetime) + to (ISO datetime) → arbitrary window
  - hours (preset)                          → fallback / default

Both paths converge on a (from_ts, to_ts) tuple used by every
section: audit_events, user_activity_summary, schema_drift_ddl
all switched to BETWEEN-style filters with explicit bounds.

Validation:
  - 'to' must be after 'from'  (400 Bad Request)
  - range capped at 5 years    (400 Bad Request)
  - naive timestamps treated as UTC

## Manifest + README
manifest.json now carries window_from_utc, window_to_utc,
window_label, and window_is_custom so reviewers can read the
exact bounds machine-readably regardless of how the window was
expressed at request time. README.md prints the same in human
form.

## Filename
Preset window: 'compliance-pack-2026-05-31-720h.zip'
Custom range: 'compliance-pack-from-20260101-to-20260531.zip'

## Audit
'Export Compliance Pack' audit event now carries the full
window_label rather than just hours — so the trail records the
explicit dates a custom-range pack covered.
# Changelog — v4 Phase 4z-cz (Sidebar username: light text on dark)

## Problem
The username in the sidebar's user card used color: var(--tx),
which flips to a dark grey in light theme. Since the sidebar
itself is dark in both themes, the username became unreadable
on light theme.

## Fix
The username text now uses a fixed light colour
(rgba(255,255,255,.92)) matching the rest of the sidebar's
text tone. The role pill below it was already a coloured badge
with white text and was unaffected.
# Changelog — v4 Phase 4z-cy (Shortcuts ? button moved to header)

## Problem
The floating circular '?' button at the bottom-right was
overlapping the query_id chip in the result table footer on
wide screens, making the chip hard to read and harder to click.

## Fix
Replaced the floating button with a small '?' button inside the
header, immediately to the left of the theme toggle:

  [...connection chip...]   [?]  [☀️/🌙]   [user menu]

  - Visual weight matches the other header buttons (transparent
    bg, white-tinted text, 15px monospace bold).
  - Tooltip remains 'Keyboard shortcuts (?)'.
  - Click opens the overlay; the global '?' keyboard shortcut
    keeps working from anywhere.
  - On upgrade, any stale floating button left in the DOM from
    the previous build is removed on first render — no orphans.
# Changelog — v4 Phase 4z-cx (Editor resize: rescue + scroll fallback)

## Problem
A user dragged the editor handle so far down that the result
area collapsed entirely and the handle itself ended up
off-screen — no way to drag it back up.

## Fixes

### 1. Tighter clamp
The max editor height now caps at min(viewport*0.65, 700px)
instead of 80vh. Both the load-time bootstrap and the runtime
drag-clamp share this rule, so a previously-saved 80vh value is
automatically rescued on next load.

### 2. Double-click → reset
Double-clicking the resize handle snaps the editor back to its
default 220px and persists that. One-gesture escape hatch when
the editor is too tall to be useful. Tooltip updated to
'Drag to resize editor · Double-click to reset' so the gesture
is discoverable.

### 3. Vertical scroll on the editor column
The flex column containing the editor and the result switched
from overflow:hidden to overflow-y:auto (and gained minHeight:0
for correct flex shrink). If the editor is somehow taller than
the column anyway, the column itself can scroll — the resize
handle stays reachable.

A toast confirms when the reset gesture fires so the user knows
why the editor suddenly snapped.
# Changelog — v4 Phase 4z-cw (Editor / result splitter)

## New: Resizable query editor
The SQL editor in the Query panel now has a drag handle directly
below it. Grab the handle and drag up / down to make the editor
taller or shorter; the result grid below picks up the freed (or
freed-back) vertical space automatically.

## Behaviour
  - Handle is a thin 6px bar between editor and result. Hover and
    drag both turn it the accent colour so the affordance is
    obvious; the cursor flips to row-resize while you're over it.
  - Drag uses pointer events (mouse / touch / pen — same code).
  - Height changes flow through a CSS variable (--editor-h) shared
    by .sql-editor and .CodeMirror, so the resize is live; we
    call cm.refresh() each frame (rAF-batched) to keep CodeMirror
    aligned.
  - Clamped to a minimum of 80px and a maximum of 80% of the
    viewport so a runaway drag can't push the result grid
    off-screen.
  - On pointer release the final height is persisted to
    localStorage under 'ch_editor_height'. A bootstrap script
    applies the saved value as the CSS variable before the first
    render, so there's no flash-of-default-220px-then-jump on
    reload.
  - The handle is re-installed each time CodeMirror is mounted
    (tab switch, theme switch, etc.), with a defensive cleanup of
    any previous handle so re-mounts can't stack duplicates.
# Changelog — v4 Phase 4z-cv (Notes: drop the audit-log badge)

## Change
Removed the '● recorded in audit log' badge from the notes
compose form. The audit behaviour is unchanged — full note text
is still captured in Add Note / Delete Note audit events — only
the inline hint is gone. Cmd+Enter shortcut hint stays.
# Changelog — v4 Phase 4z-cu (Notes: capture content in audit detail)

## Change
Note content now travels with the audit event:

  - Add Note     → detail = full note text
  - Delete Note  → detail = original text of the note being removed
                   (looked up BEFORE the in-memory list removal so the
                   trail captures what was deleted)

uiAudit's backend caps detail at 1 MB; nothing extra is needed
on the server side.

## UI
The compose form gained an inline hint left of the buttons:

  ● recorded in audit log · Cmd+Enter to save

The amber dot + tooltip make it explicit before the operator types
that note content will be captured.

## Storage layers
Unchanged:
  - Rendering / display: per-user localStorage (ch_notes_<username>)
Changed:
  - Audit: full note text now in detail (previously action-only)

## Whitepaper §11.7
  - Add Note and Delete Note rows updated — content is now part of
    the audit detail.
  - Explanatory paragraph rewritten: two storage layers
    (browser-local for rendering, audit trail for content). The
    notes pad is now a properly auditable annotation surface, not
    an opaque action.
# Changelog — v4 Phase 4z-ct (Notes: per-user scoping + audit + whitepaper)

## Change A — Per-user localStorage key
Notes now live under 'ch_notes_<username>' rather than a single
shared 'ch_notes'. Two operators sharing a browser no longer see
each other's notes. A user who signs out and back in gets their
own list back intact.

  - State init starts with an empty list (used for the login screen
    where no user is signed in).
  - After every successful sign-in / session restore,
    loadNotesForCurrentUser() pulls the list for that user.
  - On every logged-out reset, S.notes.list is cleared so the
    in-memory state doesn't leak to the next user.
  - One-time migration: when a per-user key is missing but the
    legacy unscoped 'ch_notes' key exists for a signed-in user,
    the legacy notes are adopted into that user's space.

## Change B — Audit story documented
Added two rows to the §11.7 event catalogue in the whitepaper:
'Add Note' and 'Delete Note'. Both are action-only events; the
note text never leaves the browser, so it's not part of any
audit event detail.

A short paragraph between §11.7 and §11.8 explains the split
explicitly: notes are stored only in browser-local storage
under a per-user key, no database row is ever written, and the
audit trail records the action (Add / Delete) without the
content. This satisfies SOC 2 CC7.2 and ISO 27001 A.12.4.1
visibility expectations while preserving privacy for personal
annotations.
# Changelog — v4 Phase 4z-cs (Notes / scratch pad replaces sidebar history)

## Change
The History list under the Query panel's schema tree (the sidebar
column on the left) is replaced by a Notes / scratch pad. Tab
history remains accessible through the existing pop-up over the
editor — only the sidebar real-estate is repurposed.

## Why
History duplicated the tab pop-up and the Command Palette
(which now also indexes recent tabs). The sidebar's bottom
panel had no unique role. Notes is the smallest useful widget
that earns its place: per-browser scratch space that stays
visible while the operator works.

## Behaviour
  - Per-browser, persisted in localStorage under 'ch_notes'.
    No network roundtrip; notes never leave the client.
  - Max 50 entries (FIFO trim).
  - Header shows count and a small + button.
  - Composing a note opens an inline textarea with Cancel /
    Add buttons. Cmd+Enter saves, Esc cancels. The textarea
    autofocuses.
  - Each note is plain text, multi-line preserved. Hovering a
    note reveals a small × to delete it.
  - Empty state shows '// no notes yet — click + to add'.

## Audit
Add Note and Delete Note are recorded via uiAudit, action-only —
the note text never leaves the browser, so the audit trail
captures intent (a note was added / removed) without exposing
content.
# Changelog — v4 Phase 4z-cr (Command Palette: audit trail)

## Change
Command Palette activity now leaves a trail. Two events:

  - Open Command Palette
      Fires when the palette is invoked (Cmd/Ctrl+K, the header
      trigger, or the tab-toolbar trigger). The audit row carries
      the panel the operator was on when they opened it.

  - Navigate via Command Palette
      Fires when the operator picks an item. The detail carries
      the kind (Panel / Favorite / Query Tab) and the title of the
      selected item. The action name is distinct from the sidebar's
      'Navigate' event so audit filters can separate keyboard /
      palette-driven navigation from sidebar clicks.

The Navigate-via-Palette event is recorded BEFORE the run handler
fires, so the audit trail captures the intent even if the
downstream navigation throws.

Whitepaper §11.7 (event catalogue) updated with the two new rows.
# Changelog — v4 Phase 4z-cq (Command Palette listed in shortcuts overlay)

## Change
The Cmd / Ctrl + K shortcut is now documented in the in-app
keyboard-shortcuts overlay (press '?' to open it). Added as the
first row in the 'Anywhere' group:

  CMD K  →  Command palette — search panels, favorites & tabs

CMD resolves to ⌘ on macOS and 'Ctrl' on other platforms via
the overlay's existing OS-aware modifier helper, so the same
row renders correctly on both.
# Changelog — v4 Phase 4z-cp (Command Palette: panel clicks silently dropping)

## Bug
Clicking a panel result in the Command Palette didn't navigate.
Keyboard Enter worked.

## Root cause
The hover handler called rerender(), which threw away the row
nodes and built fresh ones. Browser click dispatch requires
mousedown and mouseup to land on the SAME DOM element — if a
mouseenter between them rebuilt the row, the click event never
fired. Keyboard Enter routed through input.onkeydown and didn't
touch the rows, so it kept working — making this an intermittent
'mouse-only' failure that was easy to misdiagnose.

## Fix
Hover and keyboard navigation now mutate the existing row
styles in place (background + border) via _cpApplySelectionStyle.
The full rerender only runs when the candidate list actually
changes (i.e. typing in the input). Mousedown / mouseup stay on
the same node; the click fires; navigation happens.
# Changelog — v4 Phase 4z-co (Command Palette: move trigger to Query tab toolbar)

## Change
The command-palette trigger moved out of the header and into
the Query panel's tab toolbar, between the 'search tabs' input
and the Diff button. Two reasons:
  1. The Query toolbar is where users already 'look for
     something' — search tabs lives right there.
  2. The header was getting crowded; the global running-query
     indicator and connection chip are higher value at the top.

## Visual
Matches the weight of the Diff and Settings buttons:
btn-ghost-sm, 24px tall, with a small kbd badge showing
⌘K on Mac and CtrlK on Windows / Linux (auto-detected).

Cmd/Ctrl+K shortcut still works globally.
# Changelog — v4 Phase 4z-cn (Command Palette: persistent header trigger)

## Change
The Command Palette is no longer keyboard-only — a persistent
search box now lives in the header, immediately before the
running-query indicator and the rest of the action row.

Design (VSCode / Linear / Mac Spotlight style):
  - Search icon, faded 'Search panels, favorites…' placeholder
    text, and a ⌘K (or Ctrl K) kbd badge on the right.
  - Min-width 260px, subtle dark-translucent background; hover
    lifts the background a notch.
  - Click opens the palette (same behaviour as the keyboard
    shortcut, which still works).
  - Auto-detects platform: shows ⌘K on Mac, Ctrl K elsewhere.
# Changelog — v4 Phase 4z-cm (Command Palette / Cmd+K)

## New: Quick switcher
VSCode-style centred quick switcher: press Cmd+K (macOS) or
Ctrl+K (Windows / Linux) anywhere in the console to open it.
Search across:
  - Every navigable panel (role-filtered via hidden_panels — so
    a readonly user can't even see admin-only panels in the
    search results)
  - Saved favorites (S.q.favorites)
  - Open query tabs with non-empty SQL

## Behaviour
  - Cmd/Ctrl+K toggles the palette (open / close).
  - Live filter as you type — fuzzy substring with title-prefix
    priority. Results capped at 60.
  - Each row carries a coloured 'kind' pill (Panel / Favorite /
    Query Tab) and a subtitle (group + sub for panels, SQL
    snippet for favorites and tabs).
  - ↑ / ↓ navigates, Enter runs, mouse-hover moves the
    selection, Esc and backdrop click close.
  - Selecting a panel just sets S.nav and renders. Selecting a
    favorite opens it in a new query tab and switches to the
    Query panel. Selecting a tab switches the active tab.

## Implementation
Imperative DOM (mounted under <body>, removed on close) so the
input never loses focus across re-filters and the rest of the
app doesn't re-render on every keystroke.

A new PANEL_REGISTRY constant lists every navigable panel; the
palette searches against this rather than walking the in-render
navGroups tree, so it works regardless of which panel is open.
Adding a future panel requires a row in this registry too.
# Changelog — v4 Phase 4z-cl (favorites panel: remember closed state across reconnect)

## Bug
Closing the favorites side panel, refreshing the page, and
signing back in re-opened the panel — even though the
preference was saved.

## Root cause
restoreClusterState() resets S.q from scratch on a fresh-state
branch (no snapshot for that cluster). The reset literal didn't
carry showFavoritesPanel forward, so it became undefined after
reconnect. The panel's visibility check (S.q.showFavoritesPanel
!== false) then evaluated to true (undefined !== false), forcing
the panel open regardless of the user's saved preference in
localStorage.

## Fix
The S.q reset now reads the preference back from localStorage so
it survives reconnect. Closed stays closed; open stays open.
# Changelog — v4 Phase 4z-ck (Compliance Pack: audit report framing)

## Change
The Compliance Pack banner now reads as an 'Audit report
generator', making it explicit that this is the report the
operator hands to a SOC 2 / ISO 27001 / GDPR assessor.

Added per-standard intent lines (each with a coloured badge):

  - SOC 2:    Evidence for Trust Services Criteria —
              access control (CC6), monitoring (CC7), change
              management (CC8). For SOC 2 Type II assessors.
  - ISO 27001: Annex A evidence — A.9 access control, A.12.1.2
              change control, A.12.4 logging. For certification
              and surveillance audits.
  - GDPR:     Article 30 records of processing, Article 32
              security of processing, DPIA evidence. For DPO
              reviews and supervisory-authority requests.

Plus the sensitive-material warning is now a bordered amber
callout (encrypt at rest, transmit securely, retention policy)
rather than a low-contrast footnote.
# Changelog — v4 Phase 4z-cj (Compliance Pack Export)

## New panel: Compliance Pack (Security)
Admin-only one-click ZIP of audit evidence, sitting under LDAP
in the Security group. Each file maps to specific SOC 2 /
ISO 27001 / GDPR controls (listed in the in-app banner and the
in-pack README + manifest).

## Backend
POST /api/compliance/export-pack — streams a ZIP back as
a download. Admin-only at the ACL plus a second role check in
the handler. Defensive: every section is wrapped — a single
failure (e.g. CH connection down) is recorded in manifest.errors
but the pack still builds with whatever data is reachable.

ZIP contents:
  - manifest.json   — generated_at, generated_by, window, row
                      counts per file, errors, and a file-by-file
                      control map.
  - README.md       — human-readable table of files and controls.
  - audit_events.csv — every console action in the window.
  - users.csv       — console user roster.
  - user_activity_summary.csv — per-user action rollups.
  - grants.csv      — current system.grants matrix.
  - schema_drift_ddl.csv — every DDL/DCL from system.query_log.

Audited as 'Export Compliance Pack' with window, byte size, and
row counts (audit / DDL / grants).

## Frontend
New panel buildCompliancePackPanel:
  - 'What's in this pack' banner with a 3-column table (File /
    Covers / Maps to) listing all five evidence files and the
    controls each one satisfies.
  - Window selector: 24h / 7d / 30d / 90d / 365d.
  - Big 'Generate & Download' button. The browser triggers the
    download via Blob + anchor click; server-supplied filename is
    used when present.
  - 'last: filename · size' status line after a successful
    generation.

## RBAC
  - ACL: admin only.
  - hidden_panels: 'compliancepack' added to developer /
    monitoring / readonly so they don't even see the nav entry.

## Nav
Added under Security group, immediately after LDAP.
# Changelog — v4 Phase 4z-ci (Schema Drift + Table Activity: clearer UI)

## Goal
Make the two newest panels self-explanatory at first glance — what
they show, what the colours mean, how to use them.

## Schema Drift Tracker
  - 'How to read this' banner at the top of the control card —
    one paragraph summary plus a colour key for all six
    categories (CREATE / ALTER / DROP / RENAME / TRUNCATE /
    GRANT-REVOKE) and a note explaining the Analyze → button and
    FAILED badge.
  - Counter cards now carry a short description under the number
    (e.g. 'Column adds/drops, settings changes'), a tooltip
    spelling out the click behaviour, and an active-state outline
    when their filter is the one applied.

## Table Activity
  - 'How to read this' banner explaining hot vs cold tables, the
    intent (tune indexes vs archive/drop), and a visual legend:
    blue square = reads, red square = writes, gradient stripe =
    row intensity, plus a note on the 'Cold only' toggle.
  - Column header row above the list with explicit labels —
    'Table', 'Reads (blue) vs Writes (red) · bar width = relative
    activity', 'Reads / Writes', 'Bytes read', 'Last access'. No
    more guessing what the bar segments mean.
  - Each row's hover tooltip now spells out reads, writes, total,
    bytes read, distinct users, and the last-access timestamp on
    separate lines.
  - Bar segment titles say 'reads (SELECT)' / 'writes (INSERT)'
    so the source signal is explicit.
# Changelog — v4 Phase 4z-ch (Schema Drift Tracker + Table Activity Heatmap)

## 1. Schema Drift Tracker (Security)
New panel: every DDL/DCL change against the cluster in a chosen
window, with who-did-what-when audit trail.

### Backend
POST /api/security/schema-drift — reads system.query_log over
the chosen window (24h / 7d / 30d). Captures CREATE / ALTER /
DROP / RENAME / TRUNCATE plus GRANT / REVOKE. Doesn't trust
query_kind alone (varies by CH version); unions it with a
query-prefix regex match for safety. Cluster-aware via
clusterAllReplicas when a cluster is detectable. Audited as
'View Schema Drift'.

### Frontend
  - 6 mini-counter cards (Creates / Alters / Drops / Renames /
    Truncates / Grants-Revokes) — clickable, filter by category.
  - Category filter pills (All + 6 specific).
  - User filter input (substring match).
  - Search input (matches query body, tables, databases).
  - Change list: each row shows category pill, user, timestamp,
    affected table chips, failed badge (with exception_code) if
    the DDL errored. Click 'Analyze →' to open that query_id in
    the Query Analyzer.

## 2. Table Activity Heatmap (Storage & Schema)
New panel: per-table read/write hotness from system.query_log,
with cold-table detection for archival decisions.

### Backend
POST /api/storage/table-activity — aggregates Select (reads) +
Insert (writes) against each (db, table) over a chosen window.
Excludes system tables. Captures bytes_read, last_access,
distinct_users per table. Audited as 'View Table Activity'.

### Frontend
  - Window selector (24h / 7d / 30d), Load Activity button.
  - Sort by Total / Reads / Writes / Last access.
  - 'Cold only' toggle — surfaces tables with <10 accesses in
    the window (archive candidates).
  - Search input filters db.table client-side.
  - Heatmap rows: per-table coloured intensity stripe (proportional
    to total activity), split bar showing reads (blue) vs writes
    (red), totals, bytes_read, last_access timestamp.

## RBAC
Both endpoints + panels are open to admin / developer /
monitoring / readonly (read-only by nature; no mutating action).
# Changelog — v4 Phase 4z-cg (Health Score History + Cost Trend Chart)

## 1. Health Score History
The Health Dashboard now writes a history row on every fetch and
shows a trend sparkline in the hero card.

### Database
New table health_score_history (id, ts, overall, band, plus
each sub-score, recorded_by_id, cluster_label) with an index on
ts. Append-only.

### Backend
  - /api/health/dashboard now INSERTs a row after computing the
    composite (failure silent — doesn't block the response).
  - New /api/health/history — returns a bucketed (min/avg/max)
    time series over a 24h / 7d / 30d window. 60 buckets max,
    server-side aggregation so the payload stays small even after
    thousands of fetches.

### Frontend
Hero card gets a trend row:
  - SVG sparkline (320×40): min/max band fill + avg line + last-
    point marker. Colour follows the current overall band.
  - Range pills: 24h / 7d / 30d.
  - Delta indicator: '↑ +N pts' or '↓ -N pts' first-vs-last,
    with sample count.
History is fetched in parallel with the dashboard load — silent
on failure.

## 2. Cost Trend Chart
User Cost now shows a daily time series of scanned bytes /
total duration / query count, one line per user.

### Backend
New /api/cost/user-trend — same window / group_by / filter
shape as the breakdown endpoint. Returns one series per user
(top 10 by total scanned, rest rolled into 'others'). Audited
as 'View User Cost Trend'.

### Frontend
New card between the error block and the result table:
  - SVG multi-line chart, one polyline per user.
  - Metric toggle (Scanned / Time / Queries).
  - Range toggle (7d / 30d).
  - Legend with user labels and colour swatches.
Trend fetches automatically after a successful Load Cost.
Changing range refetches; changing metric just re-renders the
chart from the existing data.
# Changelog — v4 Phase 4z-cf (Health Dashboard: auto-refresh fix)

## Bug
Clicking an Auto: pill (10s / 30s / 1m) lit up the pill but the
'last: …' timestamp on the right didn't update. Two issues:
  1. The first refresh only fired N seconds later (no immediate
     load), so it felt like nothing was happening.
  2. Navigating away from the dashboard cleared the interval,
     but navigating back didn't re-establish it — the pill still
     showed selected but the timer was gone.

## Fix
  1. setHealthDashAutoRefresh now triggers an immediate
     loadHealthDashboard() the moment you pick an interval, so
     the timestamp updates right away.
  2. render() now re-establishes the interval whenever the user
     is on the dashboard with autoRefresh > 0 but no live timer
     — handles the navigate-away-and-back case automatically.
# Changelog — v4 Phase 4z-ce (Health Score Dashboard)

## New panel: Health Dashboard
Single-pane-of-glass cluster health overview, first item under
Monitoring. Visible to all four roles (read-only).

Hero card:
  - Big overall score (0-100), weighted average of 5 sub-scores
  - Band pill: Healthy (≥90) / Degraded (70-89) / Critical (<70)
  - Refresh button + Auto-refresh pills (Off / 10s / 30s / 1m)
  - Last-loaded timestamp

5-card grid (responsive, auto-fill 240px min):
  - ⛓ Replication — broken / lagging / missing replicas; max
    absolute_delay. Score: 100 - 30·broken - 15·lagging
    - 10·missing.
  - ⚙ Mutations — running / failing / 7-day total. Score:
    100 - 25·failing - 2·(running over 5).
  - 💾 Disk — disk count, max used %, hottest disk. Score:
    100 above 70%, drops 3 points per % thereafter.
  - ⚠ Errors (1h) — distinct codes, total events. Score:
    100 - 10 per distinct code.
  - ▶ Queries — currently running, over 60s, longest. Score:
    100 - 10·long_running - (running over 50).
Each card border-left is coloured by its own band, plus a
'Details →' link that jumps to the matching panel
(clusterhealth / mutations / diskusage / cluster / monitor).

## Backend
POST /api/health/dashboard — one round trip, five defensive
SELECTs. Any section failure (e.g. cluster without replicated
tables) is isolated; the rest still return. Weights for the
composite: replication 30%, disk 25%, errors 20%, mutations 15%,
queries 10%. Audited as 'View Health Dashboard' with the score
and band.

## Cleanup
The auto-refresh interval is cleared whenever the user
navigates away from the panel (in render()).

## Also in this drop
Query Analyzer — Analyze button: disabled state now only
reflects 'loading'. The id is validated inside the click handler
against the live S.qanalyzer.queryIdInput, so pasting + clicking
just works (the previous disabled flag relied on a re-render
that setQ doesn't trigger). Same fix on Enter. Empty pastes show
a toast.
# Changelog — v4 Phase 4z-cd (Query Annotations / Notes)

## Feature
A console user can attach text notes to a ClickHouse query_id.
Notes are persistent (Postgres), audited, and surfaced both in
the Query Analyzer panel and in the PDF Analyzer Report.

## Database
New table query_annotations (id, query_id, user_id, username,
note, ts) with indexes on query_id and ts. Multi-note per
query_id (thread-like). Deleting a console user cascades.

## Backend (3 endpoints)
  - POST /api/query/annotations/list — list notes for a
    query_id, oldest first; returns can_delete per note.
  - POST /api/query/annotations/add — add a note (≤2000 chars).
    Audit 'Add Annotation'.
  - POST /api/query/annotations/delete — remove a note;
    author OR admin only. Audit 'Delete Annotation'.

All three are session-authenticated. List/add are open to any
signed-in user (collaborative). Delete is author-or-admin.

## Frontend
Query Analyzer Overview gains an 'Annotations' block below
Historical Comparison:
  - Existing notes are listed (timestamp + username + body, with
    a delete X for the author or an admin).
  - A textarea + 'Add Note' button. Ctrl/Cmd+Enter submits.
  - Annotations load in parallel with the main analyzer and
    history fetches; failure is silent.

## PDF report
Export Analyzer Report now includes an 'Annotations' section
between Historical Comparison and Overview. Each note is rendered
with author and timestamp.

## Whitepaper
Whitepaper update will follow next when the user requests it.
# Changelog — v4 Phase 4z-cc (User Cost: RBAC, user filter, PDF; topology: RBAC; whitepaper)

## RBAC
User Cost Breakdown and Cluster Topology (the latter embedded in
Cluster Health) are now visible to ALL authenticated roles —
admin, developer, monitoring, readonly — since both panels are
strictly read-only and offer no mutating actions.
  - /api/cost/user-breakdown ACL widened to all four roles.
  - /api/cluster/health ACL widened to include developer (it
    already covered admin / monitoring / readonly).
  - usercost removed from developer / monitoring / readonly
    hidden_panels lists.

## User Cost — username filter
A new filter textbox accepts a comma- or semicolon-separated
list of usernames (or 'all' or empty for every user). The list
is parsed and applied as an IN clause on the query_log
aggregate. The applied filter is snapshot when Load Cost runs
and shown in the result card header as 'Filter: …'.

Backend escapes each name (single-quote doubling) and bounds the
length, so the IN clause is safe even though it's built by
string concatenation.

## User Cost — PDF report
New '📄 Export PDF' button appears once data is loaded. Opens a
print-friendly HTML report with the same shape as the analyzer
and user-activity reports: header (window, group-by, filter,
sort), summary band (users / queries / total scanned), full row
table in the chosen sort order. Audited as 'Export User Cost
Report' with the window, group-by, filter, and row count.

## Whitepaper
Security whitepaper updated:
  - §14.8.4 User Cost Breakdown — new subsection covering the
    panel, its window + filter, the audit events, and the cost
    trio (estimator → analyzer → breakdown).
  - §14.8.5 Cluster Topology — new subsection covering the card
    embedded in Cluster Health, its data source, colour mapping,
    and visibility profile.
  - §11.7 event catalog — two new rows: 'View User Cost' and
    'Export User Cost Report'.
# Changelog — v4 Phase 4z-cb (User Cost Breakdown + Cluster Topology)

## 1. User Cost Breakdown (Security group)
New panel 'User Cost'. Aggregates system.query_log over a chosen
window (preset 24h/7d/30d or custom from/to range) by user (or
initial_user) and shows scanned bytes, rows scanned, total/avg
duration, peak memory, ok/fail counts, plus an inline bar by
scanned bytes. Sort by scanned / total time / queries / peak
memory. Top 200 users.
  - Backend: POST /api/cost/user-breakdown (admin-only). Cluster-
    aware via clusterAllReplicas when a cluster name is
    detectable.
  - Nav: Security group, after Grant Explorer.
  - RBAC: admin-only at both the API ACL and the navigation
    layer (added to all three non-admin roles' hidden_panels).
  - Audited as 'View User Cost'.
Completes the cost trio: estimator (pre-run) → analyzer (post-
mortem) → breakdown (aggregate over time).

## 2. Cluster Topology (Cluster Health panel)
Cluster Health now includes a 'Cluster Topology' card below the
existing summary/details. Reads system.clusters once per refresh
and renders one block per cluster — shards laid out horizontally,
replicas stacked vertically inside each shard. Each replica is a
small card coloured by health: green (healthy), red
(errors_count > 0), orange (estimated_recovery_time > 0).
is_local replicas are highlighted with a dashed accent border.
Tooltip on each replica shows host:port and the status.
  - Backend: cluster_health endpoint now returns an extra
    'topology' section. estimated_recovery_time is probed
    defensively in case the CH version lacks that column.
  - No new endpoint; piggybacks on the existing
    /api/cluster/health call.
# Changelog — v4 Phase 4z-ca (dashboard auto-refresh: add 5s)

## Change
The dashboard auto-refresh options were Off / 10s / 30s / 1m /
5m. Added a 5s option (between Off and 10s). The existing label
logic renders it as '5s' automatically; selecting it sets a
5-second global refresh interval for all widgets on the board.
# Changelog — v4 Phase 4z-bz (custom action filter: accept comma OR semicolon)

## Bug
Typing 'navigate, query' in the custom action filter returned
only queries, not Navigate events. The split was semicolon-only,
so 'navigate, query' was treated as ONE term; it happened to
contain 'query' (matching query events) but no audit action
contains the literal string 'navigate, query', so Navigate
events never matched.

## Fix
Custom terms now split on EITHER comma or semicolon
(/[;,]/). 'navigate, query' → ['navigate', 'query'], so Navigate
audit events and query events both match as expected. Placeholder
updated to 'Navigate, Login, Export … (comma or semicolon)'.
# Changelog — v4 Phase 4z-by (User Activity: two-mode filter, applied on View Activity)

## Changes
1. Filter is now two modes only: 'All actions' and 'Custom
   actions…'. Removed the example 'Queries only' / 'Queries +
   logins' presets.
2. Custom actions use SEMICOLON separators: 'Action1; Action2;
   …'. Each term is a case-insensitive substring match on the
   audit action; query events match if a term mentions 'query'
   or appears in the SQL.
3. The filter moved from the results card to the TOP control row
   (next to username/window), and is now APPLIED ON View
   Activity rather than live. A snapshot (appliedKindFilter /
   appliedCustomActions) is taken when View Activity runs; the
   timeline and PDF render off that snapshot, so editing the
   dropdown/textbox doesn't change the result until you click
   View Activity again (or press Enter in the custom box).
4. The custom textbox only updates state on input (no render),
   so focus is never lost while typing.
5. PDF export uses the applied snapshot, so it always matches
   what's on screen; header prints 'Filter: …' and the summary
   band shows filtered counts.
# Changelog — v4 Phase 4z-bx (User Activity: richer filter + filtered report)

## Change
The All/Queries/Actions buttons became a filter dropdown with
four modes, and the PDF report now exports exactly what the
filter shows.

Filter modes:
  - All activity        — every event
  - Queries only        — executed queries
  - Queries + logins    — queries plus login/logout audit events
  - Custom actions…     — a textbox for comma-separated action
                          terms (e.g. 'Login, Drop, Export').
                          Audit events match if their action
                          contains any term; query events match if
                          a term mentions 'query' or appears in the
                          SQL. Live-filters as you type.

Shared _uaFilter() drives both the on-screen timeline and the PDF
export, so they never diverge. _uaFilterDesc() produces the
human label printed in the PDF header.

## PDF report
  - Exports the FILTERED event set, not the full one.
  - Header line now includes 'Filter: <description>'.
  - Summary band shows filtered event/query/action counts.
  - Refuses to export if the filter matches nothing.
  - Audit detail records the filter used and the exported count.
# Changelog — v4 Phase 4z-bw (User Activity: compact controls + stats on the right)

## Change
The User Activity controls (username box, two selects) were
full-width and stacked vertically, taking too much space.
  - Username input: fixed 200px (was min-width 240px, stretchy).
  - Both selects: fixed 150px, flex:0 0 auto — no stretch.
  - All controls now sit on one row.
  - The event-count stats (N events · N audit · N queries) moved
    from the results card to the right end of the control row
    (marginLeft:auto), shown once a timeline is loaded.
  - The results card header now shows a short 'Showing all / N
    <kind> events' label next to the All/Queries/Actions filter.
# Changelog — v4 Phase 4z-bv (Diff: per-query stats + query_id)

## Change
The Query Diff screen only showed a compact 'A: N rows · B: M
rows' line. Now each side gets its own stats footer — rows,
elapsed, scanned bytes, rows-scanned — plus a clickable query_id
chip that routes to the Query Analyzer, exactly like the editor
result footer.

The diff already ran both queries through the same async job
path that returns query_id / read_rows / read_bytes / elapsed;
runDiff now carries those into computed.statsA / statsB, and the
render shows an A row and a B row above the diff summary.
# Changelog — v4 Phase 4z-bu (hide vendor license-generation command)

## Change
The Settings → License section showed a self-service hint:
'No license? Generate one with python3 issue_license.py issue
--customer ... --out customer.lic'. That command is the
VENDOR-side (BlancoByte) license-minting tool — exposing it to
customers invites them to forge their own licenses.

Replaced with a neutral message:
'No license? Contact BlancoByte to obtain a license file for
your organisation, then upload it above.'

The issue_license.py issue command no longer appears anywhere in
the UI.
# Changelog — v4 Phase 4z-bt (analyzer PDF: include 'slower than median' headline)

## Bug
The on-screen Historical Comparison card shows a coloured headline
('this run was 2.3× slower than median'), but the Export PDF
report only printed the p50/p95/p99/avg/max table — the headline
was missing.

## Fix
exportAnalyzerPDF now computes the same this-run-vs-median ratio
and prepends it as a coloured line above the history table in the
PDF: red when ≥2× slower, green when ≤0.5× faster, neutral
otherwise — matching the panel. Also uses the resolved thisDur
(falls back to hist.this_duration_ms) for the 'This run' cell.
# Changelog — v4 Phase 4z-bs (User Activity: custom range + PDF report)

## 1. Custom date range
The User Activity window selector now offers 'Quick window'
(Last 24h / 7d / 30d) OR 'Custom range…'. Picking custom swaps
the preset dropdown for two datetime-local inputs (from / to).
  - Backend POST /api/security/user-activity now accepts from_ts
    + to_ts; when both are present it filters
    ts BETWEEN from AND to on both audit_events and
    query_history, otherwise it uses the relative 'last N hours'
    window.
  - Audit detail records the window used (absolute range or
    'last Nh').
  - Date inputs are raw DOM so typing/picking never loses focus.

## 2. PDF report (same screen)
New '📄 Export PDF' button appears once a timeline is loaded.
Opens a print-friendly HTML report in a new tab and triggers the
browser's print dialog for save-as-PDF. The report contains:
  - Header: user, window description, generation timestamp
  - Summary band: total events / queries / actions
  - Full event table: time · kind (colour-tagged QUERY vs action)
    · detail (full SQL or panel+detail) · meta (duration, rows,
    connection, IP, errors)
No server-side PDF dependency — pure HTML + print CSS, same
approach as the Query Analyzer report. Audited as
'Export User Activity Report'.
# Changelog — v4 Phase 4z-br (User Activity textbox + Grant Explorer focus fix)

## 1. User Activity — username textbox instead of dropdown
The user-picker <select> wasn't usable. Replaced with a raw-DOM
username textbox: type a console username, press Enter or click
View Activity. The value lives on the DOM element and state, read
on submit — no render() per keystroke, so focus is never lost.
Backend POST /api/security/user-activity now accepts either
user_id or username (resolved case-insensitively to user_id from
the users table; 404 if no such user).

## 2. Grant Explorer — filter inputs no longer lose focus
The two filter inputs called render() on every keystroke, which
rebuilt the whole panel and dropped focus after the first char.
Rewrote the panel so:
  - The result tables live in a single container div.
  - Filter inputs are raw DOM, mounted once.
  - On input we update state and re-render ONLY that container
    (renderTables()), never the whole panel.
  - Inputs keep focus + cursor position across keystrokes.
  - Header counts now show 'matched / total' (e.g. '12 / 47
    grants') so the filter effect is visible.
# Changelog — v4 Phase 4z-bq (RBAC for the new security panels)

## Issue
4z-bp added User Activity + Grant Explorer with admin-only
backend ACLs, but their nav items weren't in any non-admin role's
hidden_panels. So a developer/monitoring/readonly user saw the
nav items, clicked them, and got a 403 — correct security, poor
UX.

## Fix
Added 'useractivity' and 'grants' to the hidden_panels list of
all three non-admin roles (developer, monitoring, readonly). Now:
  - Backend ACL: admin-only (unchanged) — defence in depth.
  - Frontend nav: the two panels are hidden for non-admins, so
    they never see a nav item they can't use.

Disk Usage stays visible to all authenticated roles (like the
existing Storage panel) — capacity data isn't sensitive and is
useful to monitoring/readonly users.

## Access matrix (final)
  User Activity   → admin only (ACL + nav)
  Grant Explorer  → admin only (ACL + nav)
  Disk Usage      → all authenticated roles
# Changelog — v4 Phase 4z-bp (three new panels: User Activity, Grant Explorer, Disk Usage)

## 1. User Activity Timeline  (Security group)
New panel 'User Activity' under Security. Pick a console user +
a time window (24h / 7d / 30d) → unified chronological timeline
merging audit_events (every audited action) with query_history
(the SQL they ran). Colour-coded left border per event kind
(query = accent, action = grey, error = red). Filter by
all / queries / actions. Answers 'what did this user do?' on one
screen for compliance and incident review.
  - Backend: POST /api/security/user-activity (admin-only).
    Merges both Postgres tables for one user_id, sorts by ts desc.
  - Audited as 'View User Activity'.

## 2. Grant Explorer  (Security group)
New panel 'Grant Explorer' under Security. One click loads
effective ClickHouse permissions from system.grants (direct
grants), system.role_grants (role assignments), and system.users.
Two tables — Direct Grants (grantee, access type, db, table,
column, grant option, revokes flagged red) and Role Assignments.
Filter by grantee and by access type. Answers 'who can access
what?' for a security review.
  - Backend: POST /api/security/grants (admin-only).
  - Audited as 'View Grants'.

## 3. Disk Usage  (Storage & Schema group)
New panel 'Disk Usage' under Storage & Schema. Scans
system.parts (active) aggregated by database and table. Shows a
proportional by-database bar (click a database to filter), a
colour legend, and a top-tables table with disk/compressed/
ratio/rows/parts and an inline bar. Capacity planning at a
glance.
  - Backend: POST /api/storage/disk-usage (all auth roles).
  - Audited as 'View Disk Usage'.

## Wiring
  - 3 nav items: diskusage (Storage & Schema), useractivity +
    grants (Security).
  - 3 state blocks, 3 panel builders + loaders, 3 router cases.
  - 2 admin-only ACL entries for the security endpoints; disk
    usage open to all authenticated roles like other storage
    reads.
# Changelog — v4 Phase 4z-bo (no panel flicker on table-name insert)

## Bug
Clicking a table in the Query panel tree to insert its name caused
the whole panel to flicker — the editor, the tree, everything
rebuilt on every click.

## Cause
The click handler called render() after the insert. render()
rebuilds the entire query panel, CodeMirror included, which reads
as a full-panel refresh.

## Fix
Dropped the render() call. _insertTokenAtCursor writes directly
into CodeMirror via doc.replaceRange, so the editor is already up
to date — no render needed. The active-db state is still followed
(S.q.selectedDb / S.q.tables mutated directly), so the active-db
pill catches up on the next natural render without forcing one
now. Insert and toast are instant; no flicker.
# Changelog — v4 Phase 4z-bn (table-name insert by clicking the row)

## Change
The → insert button (4z-bk through bm) was shrinking the table
names to make room for itself. Removed from both trees. Now the
table row's own click does the insert:

  - Query panel DB tree: clicking a table row inserts its
    db-qualified name (db.table) at the cursor, follows the active
    db, and toasts. Non-destructive — appends at the cursor,
    never overwrites the open query.
  - Schema panel DB tree: row click is unchanged (opens the table
    detail) — that panel is an explorer; insert-into-editor is the
    Query panel's job.

Table names render at full width again — no flex/ellipsis
shrinking to accommodate a button.
# Changelog — v4 Phase 4z-bm (table-insert button in Query panel tree)

## Issue
4z-bk/bl added the → insert button to the Schema panel's DB tree,
but the Query panel has its OWN DB tree on the left — the one the
user actually looks at while composing a query. That tree's table
rows only set the active database; clicking a table did nothing
to the editor (by earlier design). So 'click a table in the query
panel' felt broken.

## Fix
The Query panel's DB tree now carries the same → insert button as
the Schema panel. Clicking it inserts the db-qualified table name
(db.table) at the cursor (or end of document), audits as
'Insert Table Name' under the 'query' panel, and toasts. The row
click still only sets the active db — unchanged, as before.

Table-name span flex-grows + ellipsis-truncates so the button and
engine badge stay aligned on the right for long names.
# Changelog — v4 Phase 4z-bl (table-name insert: bare name, then db-qualified)

## Tweak
The table-row → insert button (added in 4z-bk) is finalised to
insert the db-qualified name (db.table_name) at the cursor — no
SELECT/FROM scaffolding, just the identifier. db-qualified is
unambiguous regardless of which database is active, which the
user chose as the better default after trying the bare form.

Insertion uses _insertTokenAtCursor, so:
  - cursor position when the editor is focused, end of document
    otherwise;
  - a single separating space only when the preceding character
    needs one (so 'FROM ' + 'db.table' stays clean);
  - no blank-line padding.
# Changelog — v4 Phase 4z-bk (insert table name into editor)

## Feature
Each table row in the Schema panel's tree now has a '→' insert
button. Clicking it writes the qualified db.table name into the
SQL editor at the cursor (or end of document if the editor isn't
focused) and jumps to the Query panel. Especially useful for long
table names the user would otherwise type by hand.

## New helper
_insertTokenAtCursor(text) — sibling to _insertSqlAtCursor but
for inline identifiers. No blank-line padding (a table name
belongs inline, e.g. 'FROM db.table', not on its own paragraph).
Adds a single leading space only when the char before the cursor
is non-space, non-open-paren, non-dot, non-comma, non-backtick,
so 'FROM' + token becomes 'FROM token' but 'FROM ' + token
doesn't double the space and 'db.' + column doesn't break the
qualified name.

## Behaviour
  - Button stops event propagation so it doesn't trigger the
    row's detail-open.
  - Hover highlights the arrow in the accent colour.
  - Inserts qualified name (db.table), audits as
    'Insert Table Name', toasts confirmation.
  - Table name span now flex-grows and ellipsis-truncates so the
    button and engine-size stay aligned on the right even with
    very long names.
# Changelog — v4 Phase 4z-bj (analyzer insights + history + PDF export)

## Three additions to the Query Analyzer panel

### 1. Auto-insight block (rule-based)
New 'Insights' section at the top of the Overview tab.
Deterministic rules over Profile Events + Settings + scan stats,
flagging issues in red/orange and reassurance in green:
  - Exception present (red)
  - Mark cache cold / excellent
  - Low selectivity (rows scanned vs returned)
  - Heavy per-row payload (bytes/row)
  - Many parts scanned (partition pruning)
  - Distributed network overhead
  - IO-bound or CPU-contended
  - Memory spilled to disk (external sort/agg)
  - High peak memory
  - Parallelism disabled (max_threads = 1)
  - Low compression ratio
  - 'Fast and clean' green stamp when sub-200ms and no flags
No AI; pure rules. Triggers only on patterns an experienced DBA
would call out.

### 2. Historical comparison (30 days)
New backend endpoint POST /api/query/analyze/history. Resolves
the query's normalized_query_hash, then aggregates the last 30
days of runs for that hash:
  - count, p50, p95, p99, avg, min, max
  - unique users
  - last 100 runs for the sparkline
Frontend: card in Overview showing 'this run vs median' with a
colour-coded ratio (red >2× slower, green <0.5× faster, neutral
in between) and an inline SVG sparkline of recent durations.
Audit-emit unchanged — the history fetch is silent on failure
since it's a nice-to-have, not a hard requirement.

### 3. PDF export
New '📄 Export PDF' button in the analyzer header (visible once
data is loaded). Opens a print-friendly HTML report in a new
window and triggers window.print() so the user saves it via the
browser's native PDF dialog. The report contains:
  - Header with query_id, node, generation timestamp
  - Exception block (if present)
  - Insights (same rule output as the panel)
  - Historical comparison stats
  - Overview key/value table
  - SQL
  - Profile Events (non-zero only)
  - Settings (all)
  - Thread breakdown
  - Tables / Columns / Used resources as chip groups
No new server dependency (no PDF library); pure HTML+CSS+print.
Audit-logged as 'Export Analyzer Report'.

## Why this matters
4z-bd-bg gave the analyzer the data; this phase makes it
interpret the data. An operator opening the panel after an
incident now sees, in order: what's wrong (insights), how this
run compares to history (median, sparkline), and the raw
evidence (existing tabs). One Export PDF away from an incident
postmortem attachment.
# Changelog — v4 Phase 4z-bi (Run All — stats + query_id per statement)

## Issue
Single-query result had a stats footer with rows, elapsed,
scanned bytes, plus a clickable query_id chip routing to the
Query Analyzer. Run All (multi-statement) showed only
'rows · elapsed' in each statement's header strip — no scan
bytes, no query_id, no analyzer drill-down.

## Cause
Backend already returns the rich payload (read_rows, read_bytes,
query_id) for every statement in the batch — Run All calls the
same /api/query/run endpoint per statement. The multi-result
render simply wasn't surfacing it.

## Fix
renderMultiResults() now mirrors the single-query path:
  - Header strip's compact stats include scanned bytes when
    available: 'N rows · Ys · K MB scanned'.
  - Each statement card gets a footer bar below its result
    table with the same shape as the single-query footer:
      STATS · N rows · Ys · K MB scanned · M rows scanned · [QUERY_ID chip]
  - Footer is skipped on errored statements and on collapsed
    statements.
  - query_id chip stops event propagation so clicking it doesn't
    collapse the card; routes to the Query Analyzer panel with
    that statement's id pre-loaded.
# Changelog — v4 Phase 4z-bh (Query Analyzer audit + whitepaper section)

## Backend audit panel name
The Analyze Query audit event was being written with
panel='slowlog' (a leftover from when the analyzer was a modal
inside Slow Queries). After the 4z-bd refactor the frontend's
audit emits panel='qanalyzer'; backend now matches. Admin-UI's
per-panel filter groups all analyzer events under 'qanalyzer'.

## Security whitepaper §14.7 — Query Analyzer
New subsection added directly after §14.6 Cost Estimator. Cost
estimator answers 'what will this cost?' before a query runs;
Query Analyzer answers 'what did it actually do?' after the
fact. Neighbour placement is natural.

The new subsection covers:
  - 14.7.1 Surfaces and entry points: result-footer chip, slow-
    query Analyze button, direct paste of an external query_id.
  - 14.7.2 Data sources and auth: /api/query/analyze endpoint
    reads from system.query_log + system.query_thread_log,
    clusterAllReplicas-aware, version-defensive column probing,
    parameterised binding for query_id, never escalates beyond
    the operator's own ClickHouse credentials.
  - 14.7.3 query_id provenance: every editor query gets an
    explicit UUID at submit time so the result-footer chip can
    deep-link to the analyzer.
  - 14.7.4 What it does NOT do: no execution, no per-analyzer
    storage, no bypass of cluster-side authorisation.
  - 14.7.5 Audit and accountability: every analyzer open emits
    an Analyze Query event with the calling user's id, the
    inspected query_id, the inspected query's duration_ms,
    user, and event type. Triple-written and SIEM-forwarded
    like every other audit event.

## §11.7 event catalog
Added 'Analyze Query' to the per-event table in §11.7
(Operations and cluster administration) right after 'Run Slow
Queries' so auditors find the analyzer entry next to its
natural neighbour.

## Whitepaper rebuilt
ClickHouse-Console-Security-Whitepaper-v4.docx regenerated from
gen-security.js; bundled with the app source.
# Changelog — v4 Phase 4z-bg (clickhouse-connect compatibility fix)

## Bug
4z-bf passed query_id= as a kwarg to clickhouse-connect's
Client.query(). That parameter only exists in 0.5+; on older
versions it raises TypeError, which propagated up as 'unexpected
keyword argument'. Every query in the editor was failing.

## Fix
Two-tier strategy:
  1. Try the modern API: cl.query(sql, query_id=X, settings=...)
  2. On TypeError, fall back to cl.query(sql, settings=...) and
     read whatever id the server assigned from
     QueryResult.query_id (which has been there for a long time).

Either way the query runs, ch_query_id is populated as long as
the QueryResult exposes one. Footer chip and analyzer drill-down
keep working on both old and new clients.

## Verified
  - Modern client: explicit UUID is what ends up in
    system.query_log (we control the id).
  - Older client: server-assigned id is read back; same effect
    from the user's perspective, just without us pre-generating.
# Changelog — v4 Phase 4z-bf (analyzer lookback + query_id chip)

## Issue 1 — analyzer 'not found' on older queries
Default 30-day lookback on the analyzer was too tight for some
operators. With a query_id (UUID) filter we're already selecting
at most a few rows out of the whole log, so the time bound was
imposing a hard cap for no real performance gain.

## Fix
Lookback bound is now OPTIONAL. When the caller doesn't pass
hours_back, the WHERE clause has no time constraint and the
filter runs against all of system.query_log. Cheap because the
UUID match is very selective. Caller can still pass hours_back
explicitly for huge logs.

## Hint message updated
'Older rows may be purged' replaced with the more accurate
'The row may have been purged by query_log_retention, or the
query never reached the log (e.g. cancelled before logging).'

## Feature — clickable query_id in result footer

### Backend
When a query runs, we now assign an explicit UUID query_id via
clickhouse-connect's query_id parameter. This same id ends up in
system.query_log, which lets us pivot. Result payload carries
the id back to the client.

### Frontend
Stats footer below the result table gains a query_id chip on the
far right. Clicking it routes to the Query Analyzer panel with
that id pre-loaded — natural drill-down: 'query ran, here are
the stats — what was actually going on inside it?'

Chip styling: surface background, 1 px border, small uppercase
'QUERY_ID' label, the id itself in accent colour. Tooltip
explains the destination.

## Why this matters
Before: to analyse a query you'd just run, you had to either
remember its id from the logs or go to Slow Queries and find it
again. Now: the id is one click away from the result you're
already looking at.
# Changelog — v4 Phase 4z-be (Query Analyzer version compatibility)

## Bug
On older ClickHouse versions, /api/query/analyze failed with
'Unknown expression identifier peak_memory_usage'. peak_memory_usage
was added in CH 22.8; used_storages around 22.6. The hard-coded
SELECT assumed both were present.

## Fix
Defensive column probing. Before building the SELECT, one quick
lookup against system.columns enumerates which of the optional
columns are present in this CH version. Missing ones get
substituted with typed defaults:
  - peak_memory_usage → memory_usage as peak_memory_usage
    (in older versions memory_usage already tracks the peak)
  - used_functions     → [] as used_functions
  - used_aggregate_functions → [] as used_aggregate_functions
  - used_dictionaries  → [] as used_dictionaries
  - used_storages      → [] as used_storages

The downstream Python code is unchanged — it sees the same column
positions regardless of CH version.

## Coverage
Works on ClickHouse 19.x onwards. The required columns
(query_id, event_time, query_duration_ms, memory_usage, query,
ProfileEvents, Settings, etc.) have been stable for years.
# Changelog — v4 Phase 4z-bd (Query Analyzer panel)

## What changed
The Query Analyzer (introduced in 4z-bc as a modal) is now a
dedicated panel. The modal had two problems:
  1. Bug: depending on slow-log render state the modal didn't
     always mount, so the 🔍 Analyze button appeared to do
     nothing.
  2. UX: modal is the wrong shape for this content. A query
     analysis is a workspace, not a confirm — you want to scroll,
     swap tabs, search, and come back later. A first-class panel
     is the right shape.

## Now
  - New left-nav item 'Query Analyzer', sub: 'deep-dive by id',
    placed right after 'Slow Queries' so the flow Slow → Analyze
    is neighbours in the navigation.
  - Panel has its own query_id input field, so the user can
    paste a query_id from anywhere (an email, a log line, an
    incident ticket) and analyze it without going through Slow
    Queries first.
  - 🔍 Analyze button on a slow-query row now: sets the analyzer
    state, navigates to S.nav='qanalyzer', fires the fetch. The
    user sees the panel populate in front of them.
  - Same six tabs as before: Overview, SQL, Profile Events,
    Settings, Threads, Tables / Columns.
  - Same search filter on Profile Events and Settings.
  - State moved from S.slowlog.analyze to S.qanalyzer.

## Backend
Unchanged. /api/query/analyze keeps the same contract.

## Discoverable now
A user who never went to Slow Queries can still find the
analyzer in the left nav. They paste a query_id, click Analyze,
get the full breakdown. That's a different surface than a modal
buried inside Slow Queries.
# Changelog — v4 Phase 4z-bc (Query Analyzer)

## Feature
A deep-dive analyser for any query you find in the Slow Query log.
The 🔍 Analyze button on each row opens a tabbed modal pulling
data from system.query_log and system.query_thread_log for that
exact query_id.

## Backend
New endpoint POST /api/query/analyze takes a query_id, looks up
the row in system.query_log (clusterAllReplicas when a cluster
name is detectable so cross-node queries are found), plus
aggregates system.query_thread_log for the top 20 threads by
duration. Returns a structured payload — overview, full SQL,
profile_events, settings, threads, used resources.
  - Audit-logged as 'Analyze Query'.
  - Uses parameterised binding for query_id (no injection).
  - 30-day lookback by default to handle older query_log_retention.

## Frontend
  - 🔍 Analyze button on every slow-query row (between → Edit and
    ▼ More).
  - Modal: 1100 px wide, 90 vh tall, backdrop close on click-outside.
  - Six tabs: Overview, SQL, Profile Events (N), Settings (N),
    Threads (N), Tables / Columns.

### Overview tab
Two-column grid with: status (OK/Exception with code), kind, event
time, duration, user, database, threads, read/written/result rows
+ bytes, memory + peak memory, client host/name, cluster node.
Exception text + code prominently boxed in red when present.

### SQL tab
Full query and its normalized form (literals replaced with ?).
Copy and 'Open in Editor' buttons. Read-only display.

### Profile Events tab
ClickHouse's per-query counters — 100+ rows typically. Search box
filters by name or value. Right-aligned numeric values.

### Settings tab
All query-specific settings the row carries. Search filter same
pattern as Profile Events.

### Threads tab
Top 20 threads by duration with thread_id, name, duration,
memory, peak memory. Empty-state message when
system.query_thread_log is disabled.

### Tables tab
Chips for databases, tables, columns the engine touched, plus
used functions, aggregate functions, dictionaries, storages.

## Why it matters
The slow log row tells you 'this query was slow'. The analyser
tells you WHY: what scan settings were active, which profile
events spiked (e.g. MarkCacheHits vs MarkCacheMisses,
ReadCompressedBytes), which threads ran, what memory peaked.
# Changelog — v4 Phase 4z-bb (footer visibility fix)

## Bug
4z-ba added a footer status bar but it was invisible — the
wrapping div around toolbar + table + footer didn't claim the
parent .qresult's available height, so the table overflowed past
the container and pushed the footer below the visible area.

## Cause
The wrapper had display:flex + flexDirection:column + height:100%
but no flex value. Inside a parent flex column, that means
flex-grow defaults to 0, so the wrapper takes only its content's
intrinsic size. height:100% does NOT override that — it asks for
the parent's height but the parent has already allocated zero to
this child. End result: the wrapper is its content's height
(toolbar + giant table + footer), and the bottom is clipped.

## Fix
Added flex:'1', minHeight:0 to the wrapper. Now the wrapper
claims all the leftover vertical space inside .qresult, its
internal flex column resolves correctly, the table body's
flex:1+overflow:auto absorbs the middle, and the footer's
flex-shrink:0 keeps it pinned to the bottom.
# Changelog — v4 Phase 4z-ba (result stats footer)

## Tweak
4z-az moved the stats chip into the Results toolbar, which was
better than the editor toolbar but still bad: on narrow viewports
the chip squeezed the filter input until it was barely usable. A
user couldn't read 'Filter results...' once the chip showed
something like '1,234 rows · 0.42 s · 18.4 MB scanned · 200 rows
scanned'.

## Change
Stats chip relocated to a footer bar below the result table.
  - flex-shrink:0 keeps it pinned while the table body absorbs
    the remaining vertical space.
  - 1 px top border + surface2 background separate it visually
    from the data above.
  - overflowX:auto on the bar means very long stat strings
    scroll horizontally inside the bar rather than wrapping or
    pushing the table out of the way.
  - Small 'STATS' label on the left as a wayfinder so the
    purpose is obvious at a glance.

The toolbar at the top now keeps just: filter input, clear-sort,
clear-filter, export buttons. Plenty of room.

## Why it's better here
  - Operators scan the data top-to-bottom, then want totals.
    Stats at the bottom matches the reading flow.
  - The bar is always visible (it doesn't scroll with the table
    body), so the user can be deep in row 800 of 1000 and still
    see what their query cost.
# Changelog — v4 Phase 4z-az (execution stats relocated to Results toolbar + scan bytes)

## Tweak
The exec-stats chip was tucked into the editor toolbar at the top
of the panel — far from the data it described, and easy to miss
once a user scrolled down to read the result. It also only showed
'X rows · Ys', omitting the actual cost driver: how much the
engine scanned to produce that result.

## Change
  - Backend (app.py): query result now carries read_rows and
    read_bytes pulled from clickhouse-connect's QueryResult.summary
    (which mirrors the X-ClickHouse-Summary header). Operator sees
    the *true* engine cost, not just the rows-out figure.
  - Frontend: chip moved from editor toolbar into the Results
    toolbar, immediately right of the Filter input. Now reads
    something like '1,234 rows · 0.42 s · 18.4 MB scanned'.
  - When a filter is active, the chip leads with 'X / Y matched'
    so the user can see both the filtered count and the original.
  - Visual treatment: chip background + 1px border, monospace,
    rounded corners. Reads as a discrete pill rather than a
    floating sentence fragment.

## Why scan bytes matter
A query can return one row but scan a gigabyte (SELECT count()
without index, LIKE '%foo%' on a wide column, etc.). The rows
figure under-sells the real impact on the cluster. Scan bytes
tell the operator what their query actually cost.

## Defensive
read_bytes is only shown when non-zero. Some result types
(DDL, INSERT) don't return a summary; the chip degrades to just
'rows · elapsed' in those cases.
# Changelog — v4 Phase 4z-ay (container-width-aware chart rendering)

## Tweak
4z-ax bumped axis font sizes to 12 px / 11.5 px for readability,
which fixed large widgets but didn't help small/medium ones. The
root cause: SVG viewBox stayed fixed at 0 0 800 210 while widget
containers are as narrow as ~220 px on the small size class
(grid minmax(220px, 1fr)). Browser scales the entire SVG to fit,
so the 12 px label rendered at ~3 px on screen. Unreadable.

## Change
drawTSChart() now renders relative to the container's actual
pixel width rather than a hard-coded 800. The SVG construction
is wrapped in an inner build(W) closure:

  1. Initial build(800) so the chart paints immediately on the
     first frame.
  2. requestAnimationFrame measures container.clientWidth after
     mount; if it differs by more than 30 px from the guess,
     rebuild at the actual width.
  3. ResizeObserver watches for later layout changes (widget
     size cycling sm→md→lg→xl, window resize, sidebar collapse)
     and rebuilds the same way.

Now a 220 px small widget gets viewBox 0 0 220 210, so the 12 px
font renders at exactly 12 px on screen — readable at every size.

## What didn't change
  - Padding, font sizes, colours, tooltip behaviour, data path,
    polling, refresh timers. Pure rendering plumbing.
  - Wider widgets render with the same proportions they had at
    800 (the formula is W-padL-padR for inner width).

## Defence against re-render churn
The 30 px threshold avoids redrawing on sub-pixel jitter from
flexbox reflow. ResizeObserver fires once per actual size class
change; in practice that's <1 redraw per chart per minute.
# Changelog — v4 Phase 4z-ax (dashboard timeseries axis legibility)

## Tweak
Time-series chart axis labels were too small and too low-contrast
to read at the typical viewing distance, especially the Y-axis
numbers and dense X-axis time stamps. Operator feedback was that
'I can see the line but not what it's drawing on'.

## Change
  - Y-axis labels: 10 → 12 px, weight 400 → 500,
    opacity 0.75 → 0.92.
  - X-axis labels: 9.5 → 11.5 px, weight 400 → 500,
    opacity 0.7 → 0.92.
  - Left padding: 52 → 64 px to fit larger labels like '967.7k'.
  - Bottom padding: 44 → 52 px to fit larger time stamps,
    including two-line 'MM-DD HH:MM' mode on >12h ranges.
  - Two-line label offsets: 18/30 → 20/36 px so the second
    line clears the baseline.
  - Tick marks and baseline: opacity 0.25 → 0.32 / 0.20 → 0.28
    so they're visible but still subordinate to the data line.
  - Grid lines: 0.08 → 0.10 minor, 0.25 → 0.30 zero-line.

Inner plot area shrinks 8 px in height (146 → 138) to make room
for the bigger bottom padding — visually negligible on the
default 210 px tile but the chart line keeps its readability.

## Nothing semantic changed
Same data, same axes, same number format, same colour. Pure
typography and spacing.
# Changelog — v4 Phase 4z-aw (in-app prompt for New Dashboard + template→new board)

## Two small but high-visibility tweaks

### 1. 'New dashboard' uses appPrompt
Browser-native prompt() replaced with the in-app appPrompt
helper, matching every other input prompt in the app. Same
styling, Esc-cancels, Enter-submits, no 'Don't allow this site
to prompt you again' surprise.

### 2. Templates create a new dashboard instead of polluting the active one
Previously, clicking a template added its widgets to whichever
dashboard happened to be active — with a confirm() if it
already had widgets. This made two problems easy:
  - The user's existing dashboard could grow 19+ unwanted tiles.
  - Re-applying the same template duplicated tiles.

Now: applyDashTemplate(name)
  - Always creates a new dashboard named after the template.
  - If the name collides with an existing dashboard, suffixes
    with ' (2)', ' (3)', ... so the user never overwrites.
  - Switches active to the new dashboard so the user sees the
    result immediately.
  - Toasts: 'New dashboard ... with N widgets'.
  - The native confirm() is gone — nothing destructive can
    happen, so it no longer needs to ask.

This pairs naturally with the first-run bootstrap, which also
creates a fresh 'ClickHouse Play' dashboard. The same template,
applied again, yields 'ClickHouse Play (2)' etc.
# Changelog — v4 Phase 4z-av (Play dashboard — complete metric list)

## Gap
The 'ClickHouse Play (server metrics)' default dashboard template
was missing 4 of the 19 tiles that ship with the official Play UI
dashboard:
  - In-memory caches (bytes)
  - Load Average (15 minutes)
  - Selected rows / second
  - Concurrent network connections

## Added (in their Play-UI order)
  - In-memory caches (bytes)   → async:MarkCacheBytes
  - Load Average (15 minutes)  → async:LoadAverage15
  - Selected rows / second     → ProfileEvent_SelectedRows
  - Concurrent network conn.   → CurrentMetric_TCPConnection

For 'in-memory caches' we surface the mark cache, which is the
largest and most operationally interesting cache in ClickHouse.
For 'concurrent network connections' we surface TCP connections
(clickhouse-client and most drivers); HTTP/MySQL/PostgreSQL have
their own counters but TCP is the closest single-column analog to
Play UI's combined tile without resorting to a multi-column SUM.

## Migration for existing users
The bootstrap only runs once per browser (gated by
ch_dashboards_initialized). Without migration, only NEW users would
get the additions — anyone who'd already used the console would
keep an incomplete board.

Solution: PLAY_TEMPLATE_VERSION constant (bumped to 2) plus
ensureDefaultPlayMetrics() that runs every load. It walks the
template, finds any tile name not present on the user's current
ClickHouse Play board, and appends the missing one. Persists.
Stores the satisfied version in localStorage so it's a no-op once
the user is up to date.

User-edit safety:
  - Match is by case-insensitive name. Renaming a tile keeps it
    from being re-added (the renamed version stays).
  - Deleted tiles WILL come back on a version bump — that's the
    contract of a default board, but the user can delete again
    if they really don't want it.
  - User-added (non-template) tiles are untouched.
  - If the user removed the entire Play board, nothing is
    recreated; the version is just marked satisfied.
# Changelog — v4 Phase 4z-au (folder form layout tweak)

## Tweak
Phase 4z-at landed folder colours but put the colour swatches on a
second row underneath the folder input, which made the input look
artificially wide and wasted the right-hand half of the form. The
folder name is almost always one short word; the input doesn't need
to stretch across the full form width.

## Change
  - Folder input pinned to 240 px wide instead of flex:1.
  - Colour swatches now sit on the same row, immediately to the
    right of the input (margin-top removed).
  - The row wraps on narrow viewports via flex-wrap so the layout
    still works on tablet-width screens.

Everything else (palette, auto-select, clear button, validation,
persistence) stays identical to 4z-at.
# Changelog — v4 Phase 4z-at (folder colours)

## Feature
Saved-connection folders (Production, Test, Staging, ...) can now be
tagged with a colour. The colour is rendered on the folder header in
the Connections sidebar and the header Saved dropdown, so operators
who manage many environments can tell them apart at a glance. The
choice is per-user, persisted to PostgreSQL, and survives logout /
re-login.

## Schema (idempotent)
New table user_folder_settings:
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  folder TEXT,
  color TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, folder)
Plus an idx on user_id for fast loads. CREATE TABLE IF NOT EXISTS,
auto-applied by init_db() — no manual migration.

## Backend
  - GET /api/user/saved-connections/folders now returns
    [{folder, color}, ...] instead of [folder, ...]. Backwards-
    compatible for any consumers that only look at the .folder key.
  - POST /api/user/saved-connections accepts an optional
    folder_color parameter. Validates as hex (#rgb or #rrggbb),
    rejects anything else. UPSERTs user_folder_settings atomically
    with the connection save so a single click handles both.
  - POST /api/user/saved-connections/folder-settings — dedicated
    endpoint for updating a folder's colour without re-saving a
    connection. Same hex validation. Audit-logged as
    'Update Folder Settings'.
  - Save audit detail now includes the colour for traceability.

## Frontend
  - State S.folderColors: {folderName: hex} — populated by
    loadSavedConnections from the /folders endpoint.
  - Form colour picker: 8 preset swatches (Red, Orange, Yellow,
    Green, Blue, Purple, Pink, Gray) with a clear (×) chip.
    Disabled (grey-out) while the folder field is empty.
  - Typing a folder name that already has a stored colour
    auto-fills the swatch so the user doesn't accidentally
    overwrite it with empty.
  - Sidebar folder header: colour comes from S.folderColors first,
    falls back to the existing blue tint when no colour is set.
  - Header Saved dropdown: same colouring rule.
  - Switching clusters resets only the form's local override
    (S.conn.folder_color = ''); the stored colour stays intact.

## Privacy
Same trust boundary as user_saved_connections: per-user, scoped by
user_id, no cross-user visibility. Two operators can pick different
colours for the same folder name without seeing each other's
choices.

## Validation
Backend rejects any color value that isn't a strict /#[0-9a-f]{3,6}/
hex. The form's swatch picker only ever produces those, but the
validation defends against direct API misuse.
# Changelog — v4 Phase 4z-as (nav sidebar header + matching collapse UX)

## Tweak
Phase 4z-ar added the collapsible behaviour but left an awkward empty
strip at the top of the sidebar with the lone collapse chevron
floating against an empty band. The Connections sidebar has had a
proper header (title + chevron on the same row) since 4z-h, and the
nav sidebar should match.

## Change
Header now reads 'PANELS' (the same visual style as 'CONNECTIONS'
across the layout) with the collapse chevron pinned to its right.
Collapsed mode mirrors the Connections sidebar exactly: an expand
button at the top, then a vertical 'Panels' label running down the
otherwise-empty column.

## Implementation
  - Sidebar split into header (sb) + scrollable body (sbInner).
  - Header is one row with the title on the left and the chevron on
    the right, padding 12/8/8/14 (same as csb).
  - Items, separators, and the bottom user-info block now go into
    sbInner so the header stays put.
  - Vertical label in collapsed mode uses writing-mode + rotate,
    matches the csb implementation exactly.

## What didn't change
  - Toggle behaviour, persistence, transitions.
  - Item rendering, badges, tooltips.
  - The Connections sidebar — it already had this layout.

# Changelog — v4 Phase 4z-ar (collapsible main nav sidebar)

## What
The main left nav sidebar (Query / Schema / Monitor / …) is now
collapsible the same way the Connections sidebar already was. A small
chevron at the top toggles between two states:
  - Expanded (190 px): icons + label + subtitle + section headers.
  - Collapsed (56 px): icons only, with the label as tooltip on hover.

State persists per browser via localStorage ('ch_nav_collapsed'),
just like the Connections sidebar (4z-h era).

## Why
On laptop-sized screens the two sidebars combined eat almost 400 px
of horizontal real estate. Operators who know the icon set well don't
need both columns of labels visible. Collapsing the nav recovers
about 130 px for the query result table without losing access to any
panel.

## How
  - New S.navCollapsed boolean, persisted to localStorage.
  - toggleNav() helper mirrors toggleCsb().
  - Sidebar width / padding / item layout switch on _navCollapsed.
  - Section headers ('QUERY', 'MONITORING', ...) become thin
    horizontal separators in collapsed mode.
  - Each nav item: collapsed → icon centered, label/subtitle hidden,
    label set as title attribute (native tooltip).
  - Alert badge stays — repositioned as a small dot in collapsed mode
    so it doesn't disappear when the user is in icons-only view.
  - Bottom user-info / Sign Out block collapses to just an avatar
    circle (initial, role on tooltip) plus a ⏻ icon-only Sign Out
    button.
  - Width transitions get a 150 ms ease so the collapse is visually
    smooth, not a jarring snap.

## What didn't change
  - Nav item routing logic.
  - Connections (csb) sidebar collapse — both work independently.
  - Hidden / RBAC-filtered nav items still don't show in either mode.
# Changelog — v4 Phase 4z-aq (restore running-query guards on insert sites)

## Why
Phase 4z-ap changed history/favorite/slow-log clicks from 'replace
editor contents' to 'insert at cursor', and dropped the running-query
appConfirm guards because insertion was non-destructive. The user
preferred to keep the guards anyway — even non-destructive insertion
into a tab with an in-flight query can confuse the operator (was
this the running query or something new?).

## Fix
Re-added the appConfirm guard on all 5 sites with wording updated
for the insert semantics:

  'A query is still running in this tab. Insert another statement
   into the editor anyway?'

Variants for favorite ('Insert this favorite into the editor anyway?')
and slow-log ('Insert this slow-log query into the editor anyway?').

Cancel: nothing happens, the in-flight query stays visible.
OK:     normal insert flow runs, with the 'Inserted at cursor' toast.

## Behaviour summary
| Scenario                              | Result          |
| ------------------------------------- | --------------- |
| No query running, click insert source | Inserts silently |
| Query running, click insert source    | Prompt → Cancel = abort |
| Query running, click insert source    | Prompt → OK     = insert |

The Tab close / Logout / Disconnect / beforeunload guards from earlier
phases remain untouched — they protect against truly destructive
actions (loss of UI handle on the running query), not just text
clutter.
# Changelog — v4 Phase 4z-ap (Insert at cursor for history/favorites/slow-log)

## What changed
Clicking a history entry, a favorite, or a slow-log query's
'→ Edit' / '→ Open in Editor' button used to REPLACE the entire
editor contents with the clicked statement. If the operator was in
the middle of writing something, that work was wiped (recoverable
via Ctrl-Z, but a surprise).

Now those clicks INSERT the clicked SQL at the cursor's position
(or append to the end of the document if the editor doesn't have
focus), preserving whatever the operator was already writing.
Blank-line padding is added automatically so the inserted block
doesn't collide with existing text.

## How
New helper _insertSqlAtCursor(text):
  - If CodeMirror has focus, insert at the cursor.
  - Otherwise, append to the end of the document.
  - Pad with newlines: a blank line before if there's preceding
    content, a blank line after if there's trailing content.
  - Drop the cursor immediately after the inserted SQL so the
    operator can keep editing it.
  - Sync to tab.sql, focus the editor.
  - Plain-textarea fallback when CodeMirror isn't available.

## Sites updated
  - History dropdown 'Load to editor' button → 'Insert at cursor'
  - History dropdown inline item click
  - Favorite item click
  - Slow-log '→ Edit'
  - Slow-log '→ Open in Editor'

## Side benefit
The running-query confirmation guards (4z-ag/4z-ah/4z-ai) on these
five sites are no longer needed — insertion doesn't destroy the
editor contents, so there's nothing to ask permission about. The
guards are removed for these specific sites. The other guards
(Tab close, Logout, Disconnect, beforeunload) stay in place
because those paths still kill the UI handle on a running query.

## Audit
Renamed events to match the new semantics:
  - 'History Load'    → 'History Insert'
  - 'Favorite Load'   → 'Favorite Insert'
(The slow-log buttons did not have a dedicated audit event before
and don't get one now — they go through the same uiAudit cycle as
any other navigation.)

## What didn't change
- The favorite save flow (uses appPrompt, phase 4z-ao).
- The History entry preview / search box.
- Slow-log's 'Copy' button (just copies to clipboard).
- The Format button — it formats the existing SQL in place.
# Changelog — v4 Phase 4z-ao (in-app prompt for Save Favorite)

## Problem
Save Favorite called the browser's native prompt() for the name —
the same pattern that caused trouble for our confirmation guards in
phase 4z-ah. Native prompt() is:
  - ugly and uncustomisable
  - inconsistent across browsers
  - silently allows empty submissions (we trim/return only after
    the user has already clicked OK)
  - in some browsers shows a 'Don't allow this site to prompt you
    again' checkbox after a couple of uses

## Fix
New appPrompt(message, options) helper, modelled after appConfirm:
  - in-app modal rendered into document.body, no browser chrome
  - same CSS variables as the rest of the app (dark/light themes
    work automatically)
  - Esc cancels, Enter submits, backdrop click cancels
  - input auto-focuses with select-all on open
  - OK button disabled while the input is blank (visual + functional)
  - Enter on an empty field is ignored — caller never sees an empty
    submit
  - returns Promise<string|null>: trimmed string on OK, null on cancel
  - options: initial, placeholder, okLabel, maxLength

Save Favorite now uses appPrompt with placeholder 'e.g. Daily revenue
check', okLabel 'Save', maxLength 100. Empty submissions are
impossible.

## Other native prompts in the codebase
Dashboard creation (line 7219) and user password reset (line 10276)
still use prompt(). Those weren't part of this ticket and are
admin-tier flows; addressing them is a future cleanup.
# Changelog — v4 Phase 4z-an (estimate state moved to per-tab)

## Problem
Cost estimate state lived in S.q.estimate / estimateError /
estimateLoading — global to the query panel, not scoped to a tab.
Effect: estimate one statement in Tab 1, switch to Tab 2, and the
SAME estimate card was still visible above Tab 2's editor —
suggesting Tab 2's query has those costs (it doesn't).

## Fix
Moved all three fields to the per-tab object (tab.estimate,
tab.estimateError, tab.estimateLoading). Now:
  - Each tab keeps its own last estimate independently.
  - Switching tabs shows the destination tab's estimate (if any) or
    nothing at all (clean panel).
  - Closing a tab disposes its estimate with it.
  - Run-Estimate during a network slowdown re-checks the tab id
    before writing, so a tab the user closed mid-flight doesn't
    receive a stale estimate.

## What didn't change
- Backend endpoint /api/query/estimate
- Toolbar button placement
- Result card layout / heavy-query orange band / per-table table
- No schema changes; estimate is in-memory only.
# Changelog — v4 Phase 4z-am (Query Cost Estimator — on-demand)

## What
A new toolbar button '💰 Estimate' next to '🌳 Tree' that gives the
operator a single-glance cost summary BEFORE running a query, without
executing it. Pure on-demand — the button is the only trigger. No
threshold check during Run, no autosuggest while typing, no warn-
before-run popup. Click to ask, get an answer.

## Why distinct from EXPLAIN
EXPLAIN tells you HOW the query will run (operator tree). Cost Estimate
tells you HOW MUCH it will read (row count, byte count, partitions
touched, table totals for context). Same underlying signal source —
ClickHouse's EXPLAIN ESTIMATE + system.parts — but synthesised into
a human-readable card the user can act on without parsing operator
tree output.

## Backend
- POST /api/query/estimate
  - body: { sql, ...connection params }
  - runs EXPLAIN ESTIMATE <sql>
  - for each referenced (database, table), fetches sum(rows),
    sum(data_compressed_bytes), sum(data_uncompressed_bytes) from
    system.parts WHERE active
  - pro-rates byte counts by row ratio (rows_to_read / table_total)
  - returns { tables: [...per-table breakdown...], summary: {...totals...} }
- Audit event: 'Run Cost Estimate' with row+parts+bytes in detail.
- DDL / DML statements: EXPLAIN ESTIMATE only supports SELECT;
  backend returns a friendly error with a hint for those.

## System load
EXPLAIN ESTIMATE does NOT execute the query — it asks the optimizer
the same questions it asks during planning. Combined with one
metadata-only SELECT per table from system.parts (cache-friendly),
cost: ~30-150ms per click on production clusters. Equivalent to
clicking the existing EXPLAIN button. Zero impact on cluster
throughput.

## Frontend
- State: S.q.estimate (response object), estimateError (string),
  estimateLoading (bool). All on the active tab's state by
  convention.
- runEstimate() helper modelled after runExplain().
- New toolbar button between explainTreeBtn and favBtn.
- Result card renders between sqlTA and the existing EXPLAIN panel
  (dismissable with × button, just like EXPLAIN). Shows:
    - top summary band: rows to scan, compressed bytes, uncompressed
      bytes, parts to read
    - soft visual cue: band turns warning-orange if >1GB or >100M rows
      (purely cosmetic — never blocks anything)
    - per-table table: db, table, rows-to-read, compressed-read,
      parts, table total rows, table total size
    - hint paragraph for heavy queries ("add WHERE filters / use
      materialized view") — informational, dismissable

## Permissions
Uses the same auth as the rest of /api/query/* endpoints. The user
must be signed in and have a connection. No additional role required.
# Changelog — v4 Phase 4z-aj (folder grouping for saved connections)

## Feature
Saved connections can now be organised into user-defined folders
(e.g. "Production", "Test", "Staging"). The Connections panel sidebar
and the header Saved dropdown both show entries grouped under their
folder labels.

## Schema migration (idempotent)
schema.sql gains a DO $$ ... $$ block that adds the `folder` column
to user_saved_connections only if it doesn't already exist. Existing
deployments pick it up on next app start; new deployments get it on
first start. No manual SQL required.

Also adds idx_user_saved_conns_user_folder for fast group rendering.

## Backend
- GET /api/user/saved-connections — now returns the folder field
  and sorts entries by folder first, then by sort_order, then
  last_used_at desc.
- POST /api/user/saved-connections — accepts a `folder` parameter,
  trimmed to 64 chars, NULL on empty. ON CONFLICT update path
  changes folder along with name and db.
- GET /api/user/saved-connections/folders (NEW) — returns the
  distinct list of folder labels the user has used. Drives the
  form dropdown's autocomplete so people don't accidentally create
  "Prod" vs "production" vs "PROD" duplicates.
- Audit detail string now includes the folder label.

## Frontend — form
- New "Folder (optional)" field on the connection form.
- Implemented as a free-text input + HTML datalist of existing
  labels. Type a new label to create on save; pick an existing one
  from the dropdown to reuse.
- 64-char maxlength enforced client-side too.

## Frontend — sidebar (Connections panel)
- Entries grouped by folder. Each group has a collapsible header
  (▼/▶) with the folder label and entry count.
- Sort: real folders alphabetically, "Ungrouped" at the bottom.
- Collapsed state persisted in S.collapsedFolders (resets per
  session — kept intentionally simple).

## Frontend — header Saved dropdown
- Same grouping with section bands. No collapse here (the dropdown
  is short-lived and compact).
- Single-group case with empty folder: header is hidden so the
  dropdown looks identical to pre-feature when no folders are used.

## Migration of existing entries
Legacy entries (from before this phase) keep folder = NULL until
the user opens them in the form and saves with a folder selected.
They appear under "Ungrouped" in the meantime.
# Changelog — v4 Phase 4z-ai (broader running-query protection)

## Audit done
Walked every code path that can make an in-flight query untrackable
through the UI (server keeps running the query, but you lose handle).

## New guards (all use appConfirm — no browser "Don't show again")
4. closeTab — closing the tab that owns a running query.
   "This tab has a query still running. Closing it will keep the
   query running on the server but you will no longer be able to
   see its progress, cancel it, or view its result. Continue?"
   Also catches Ctrl+W since it routes through closeTab.

5. doLogout — logout while ANY query is running (live + snapshots).
   Counts running tabs across the active cluster AND every snapshot
   so cross-cluster running queries are also caught. Singular vs
   plural message variant.

6. disconnectConn — disconnect while a query is running on the
   active cluster's tabs. Server keeps it alive briefly but you
   can no longer fetch the result through this session.

## New native warning
7. beforeunload — added a second listener that sets event.returnValue
   when any tab is running (across live + snapshots). Triggers the
   browser-native "Leave page?" prompt on close / reload / nav-away.
   Modern browsers ignore custom message text but the prompt itself
   is enough to give the user a chance to cancel.

## Skipped on purpose
- runExplain — separate endpoint, doesn't touch tab.running or tab.sql.
- runAllQueries — Run button is already disabled while tab.running.
- cancelQuery — explicit user intent to stop, no guard needed.
- Format button — formats existing SQL in place, doesn't load new.
- New tab (+) — creates a fresh tab, running tab stays untouched.
- Connection switch — preserves running state via snapshot, no loss.

## What stays unprotected by design
- Hard browser kill (Cmd-Q, task manager) — beforeunload doesn't fire
  in those paths.
- Network drop — no client-side fix; query continues on server,
  recoverable via /api/query/poll/<jobId> if jobId is known.
- Server restart — query is killed; out of scope for the client.
# Changelog — v4 Phase 4z-ah (in-app confirm dialog — no browser "Don't show again")

## Problem
Native browser confirm() in Chrome/Safari/Edge automatically adds a
"Don't show this again" checkbox after the second prompt in a session.
For the running-query guard introduced in phase 4z-ag, that defeats
the entire safety purpose — the user could dismiss the checkbox once
and then silently lose in-flight queries forever in that tab.

## Fix
Introduced appConfirm(message) — a promise-based in-app dialog
rendered directly into document.body. Features:
  - No "Don't show again" checkbox, ever — guaranteed by being our
    own DOM, not browser-controlled.
  - Visually consistent with the rest of the app (uses --surface,
    --tx, --bd CSS variables; same btn-ghost/btn-primary classes).
  - Esc = Cancel, Enter = OK, backdrop click = Cancel.
  - Returns a Promise<boolean>.

All 5 running-query guard sites swapped from native confirm() to
await appConfirm(); their onClick handlers were made async to host
the await.

## Sites
  - History dropdown "Load to editor" button
  - History dropdown item inline click
  - Favorite item click
  - Slow-log "→ Edit"
  - Slow-log "→ Open in Editor"

The browser-native confirm() is still used elsewhere in the app for
non-critical "are you sure?" prompts where opt-out is acceptable.
Only the running-query guards are upgraded — those are the ones the
user can't afford to dismiss permanently.
# Changelog — v4 Phase 4z-ag (guard against overwriting in-flight query SQL)

## Audit done
Phase 4z-af removed the auto-insert SELECT * from table clicks. Full
codebase sweep confirms no other call site auto-inserts SQL on a
non-action click. The remaining tab.sql writes are all user-explicit:
  - History dropdown items (load button + inline click)
  - Format button (formats existing SQL, doesn't replace)
  - Favorites items
  - Slow-log "→ Edit" / "→ Open in Editor" buttons
  - Editor input event (typing)

## Additional protection
Even those explicit loads can damage UX if a query is still running
in the active tab: the in-flight SQL would silently vanish from the
editor (the query keeps running in the background, but the user
loses visual context of what it was).

Added a confirm() guard at the five user-action SQL-replace sites:
  - History dropdown "Load to editor"
  - History dropdown item inline click
  - Favorite item click
  - Slow-log "→ Edit"
  - Slow-log "→ Open in Editor"

If tab.running is true, the click prompts:
  "A query is still running in this tab. Loading another statement
   will replace what you see in the editor (the running query
   continues in the background but its SQL will no longer be
   visible). Continue?"

Format button is intentionally NOT guarded — it formats the existing
SQL in place, doesn't load new SQL, so it's safe even while running.
# Changelog — v4 Phase 4z-af (undo granularity + remove table-click SELECT auto-insert)

## Problem 1
CodeMirror's historyEventDelay defaults to 1250ms, which groups every
keystroke typed within that window into a single undo transaction.
Fast typing (anything quicker than a long pause) ended up as one
giant block — Ctrl+Z reverted everything at once.

## Fix 1
Set historyEventDelay: 300 in the editor config. New undo entry every
300ms of pause, matching most modern editors. Ctrl+Z now walks back
in normal-sized chunks.

## Problem 2
Clicking a table name in the schema sidebar tree (or in the schema
search dropdown) overwrote whatever was in the SQL editor with
"SELECT * FROM <table> LIMIT 100". Destructive — wiped in-progress
queries on a stray click.

## Fix 2
Removed the auto-insert at both call sites:
  - sidebar tree table click: now only sets the active database
    (active-db pill follows). Editor untouched.
  - schema-search dropdown click: now only opens the table in the
    schema tree. Editor untouched.

The "show me this table's contents" behavior is still one click away
via Schema Explorer; the editor just no longer auto-fills on
incidental clicks.
# Changelog — v4 Phase 4z-ae (live counter no longer wipes CodeMirror undo history)

## Problem
Phase 4z-ad's per-second tick called render() to refresh the elapsed
counter. Every render() rebuilds the SQL editor's <textarea>, which
forces CodeMirror to tear down and re-mount, which wipes its undo
buffer. Net effect: while ANY query is running anywhere, the user
cannot Ctrl+Z in any editor — the buffer was just nuked.

## Fix
The tick now patches just two DOM nodes by id, without calling render():
  - #hdr-running-text  → "⏵ N running · 14s"
  - #qpanel-run-btn    → "Running... 14s"

A single render() fires exactly once at the boundary when the last
running tab finishes, so the pill disappears cleanly. Between then
and now, the editor instance is left strictly alone.

The Run button also carries data-started-at / data-runall-cur /
data-runall-total attributes so the tick can recompute its text
without consulting React state at all.
# Changelog — v4 Phase 4z-ad (running indicator — duplicate count + live elapsed)

## Problems
1. "1 running" became "2 running" after a quick cluster hop and back.
   The indicator was counting BOTH the live S.q.tabs AND every
   snapshot in S.clusters, including the snapshot for the currently-
   active cluster. So one query was tallied once from live state and
   once from its own snapshot.
2. No live counter while the query is in flight. The user could see
   "1 running" but had no idea whether it had been running for 5
   seconds or 5 minutes.

## Fix 1 — skip active cluster's snapshot
Header indicator now resolves clusterKey(S.conn) once and skips that
key when walking S.clusters. Live tabs are the source of truth for
the active cluster; snapshots are only the source for OTHER clusters.

## Fix 2 — live elapsed counter
- tab.startedAt = Date.now() stamped at the moment runQuery (and
  runAllQueries) flips tab.running = true.
- Header indicator computes the OLDEST startedAt across all running
  tabs and displays "1 running · 14s" (the longest in-flight elapsed
  in the system).
- Run button text in the query panel becomes "Running... 14s" /
  "Running 3/10 · 14s…", visible while you're on the query panel.
- _ensureRunningTick — a single shared 1-second setInterval that
  triggers render() while anything is running, self-extinguishing
  when nothing is. No idle CPU cost when no query is in flight.
- Resume path also kicks the tick so a poll resumed after cluster
  switch still updates the counter visibly.
# Changelog — v4 Phase 4z-ac (long-running query visibility)

## Problem
A user running a long INSERT (sourcing from a remote URL) lost visual
contact with the query when they navigated:
  - Switching between panels: the "Running…" button text was only
    visible while the query panel was the active panel. Other panels
    had no indication that a query was in flight.
  - Switching between clusters: the snapshot/restore path forcibly
    reset every tab's `running` and `jobId` fields, so coming back to
    the originating cluster showed an idle UI even though the server
    was still chewing on the query.

## Fix 1 — global running-queries header indicator
Added a pulsing badge next to the existing connection / topology pills
in the header. Counts running tabs across the active cluster AND every
snapshot in S.clusters, so it appears from any panel and any cluster.
Click navigates to the query panel.

## Fix 2 — snapshot restore preserves running + jobId
testConn's cluster-state restore path previously did:
    S.q.tabs = snap.q.tabs.map(t => ({...t, running:false, jobId:null}))
which threw away the only handle to the running query. Now:
    S.q.tabs = snap.q.tabs.map(t => ({...t, result:null, error:''}))
preserving the running flag and jobId.

## Fix 3 — _resumePollForTab helper
After restoring tabs with a stale jobId, the original setInterval poll
was closure-bound to the old tab object that no longer exists in
S.q.tabs. New helper looks up the live tab by id every tick and
gracefully handles the server having already evicted the job from its
poll cache (404 → mark idle, no error).

## Note on tab.sql vanishing
The user also reported the SQL text disappearing. That symptom usually
points at a cluster switch resetting S.q to a fresh-state default (the
restoreClusterState else-branch). The fixes above don't directly
address that, but the global running badge makes the underlying state
visible enough that the user can navigate back to the right cluster
to find their query. A more invasive fix to sql persistence is on the
backlog if it keeps reproducing.
# Changelog — v4 Phase 4z-ab (hotfix: clear snapshot's _password, not "password")

## What broke
4z-aa cleared S.clusters[k].password on disconnect. The snapshot's
actual field is _password (underscore prefix) — saveCurrentClusterState
writes snap._password and restoreClusterState reads snap._password.
Clearing "password" was clearing a field nobody reads. switchCluster's
step 4 (restoreClusterState) then put _password right back into
S.conn.password and step 6 silently reconnected.

## Fix
Clear S.clusters[_ck]._password (correct field). disconnect now
actually severs all four password caches:
  - S.conn.password
  - S.clusterList[i].password
  - S.clusters[k]._password         ← was wrong field in 4z-aa
# Changelog — v4 Phase 4z-aa (disconnect actually clears ALL cached passwords)

## What was still wrong after 4z-z
The user's actual sequence is:
   1. Connect to "single"
   2. Click ⏻ Disconnect on single
   3. Click "sharded" → auto-connects to sharded (intended)
   4. Click "single" again → SILENTLY RECONNECTS without password prompt

4z-z's same-cluster early-return only fires when the clicked cluster
is the *currently-active* one. In the sequence above, sharded is
active when the user clicks single — so the early-return is skipped
and switchCluster runs the normal path, which reads profile.password
back into S.conn.password and calls testConn. The password was still
sitting in S.clusterList[single].password (and possibly in
S.clusters[singleKey].password) because disconnectConn only cleared
S.conn.password.

## Fix
disconnectConn now wipes the password from all THREE places it can
hide for the cluster being disconnected:
  (a) S.conn.password — the active connection form
  (b) S.clusterList[i].password — the sidebar entry
  (c) S.clusters[k].password — the per-cluster snapshot

switchCluster gained a second guard at step 6: if profile.password is
empty after the form-fill, open the connection form and bail before
testConn — otherwise the user sees a confusing "Connection refused"
toast when their real problem is "I haven't typed the password yet".
# Changelog — v4 Phase 4z-z (real disconnect: clear password + handle same-cluster re-click)

## What the user reported (round 2, more precise)
"On the single-named cluster I press 'disconnect but keep in list',
then I switch to sharded. Single stays GREEN. And when I click it
again it reconnects without asking for a password — even though I
disconnected."

## Two root causes
1. disconnectConn updated only the live S.connStatus, never the cluster's
   snapshot in S.clusters[k]. The sidebar dot for inactive clusters
   reads from snap.connStatus → stale 'ok' → green dot persists after
   disconnect.

2. switchCluster's same-cluster early-return only matched the
   "already connected" case. If you disconnected then re-clicked the
   same cluster, it fell through to step 6 (testConn) — and since
   S.conn.password was still cached in memory, the reconnect went
   through silently, making the Disconnect button feel cosmetic.

## Fixes (narrowly scoped — does not regress 4z-x's broken switch flow)
- disconnectConn now mirrors 'idle' into the snapshot of the cluster
  being disconnected, and ALSO clears S.conn.password so a future
  auto-flow can't reuse it.
- switchCluster now has a second early-return: same key + not currently
  connected → refill the form (with empty password), open the connection
  card, render, bail. The user must explicitly click Connect.

The disconnect/click-different-cluster path is unchanged from 4z-w — no
conditional auto-connect was reintroduced (that's what broke 4z-x).
Auto-connect when clicking a *different* cluster after disconnect is
still convenient and still works.
# Changelog — v4 Phase 4z-y (revert 4z-x — disconnect/switch UX changes)

## Reverted
Phase 4z-x made two changes that interacted in an unexpected way with
the cluster-switching flow, causing the user to see "all connections
being closed" when clicking around. Rolled back to phase 4z-w
behavior:
  - disconnectConn no longer updates S.clusters[k].connStatus
  - switchCluster auto-connects unconditionally (as before)

The original UX gripes (stale dot color after disconnect, auto-connect
after explicit disconnect) remain. Need to investigate further before
re-attempting.
# Changelog — v4 Phase 4z-x (disconnect UX: stale dot + auto-connect after disconnect)

## Problems
1. Cluster sidebar dot stayed green after clicking the disconnect ⏻
   button on the active cluster. switchCluster reads the dot color
   for inactive clusters from S.clusters[k].connStatus (the snapshot),
   and disconnectConn was only resetting S.connStatus — never the
   snapshot — so the stale "ok" persisted.

2. After explicitly disconnecting, clicking another cluster in the
   sidebar would auto-connect to it. The user's intent ("I disconnected
   on purpose") was overruled by the convenience auto-connect path,
   which assumed any cluster click implies a connection attempt.

## Fixes
1. disconnectConn now also writes S.clusters[clusterKey(S.conn)] = idle
   so the sidebar dot for the just-disconnected cluster turns gray
   immediately.

2. switchCluster captures S.connStatus once at the very top of the
   function. If the user was connected/fail when they clicked, behavior
   is unchanged — auto-connect to the new target (fast cluster
   switching). If they were idle (explicit disconnect), the form is
   populated with the target cluster details but no testConn is fired —
   the user has to click Connect to actually connect.
# Changelog — v4 Phase 4z-w (fix _user_id NameError in saved-connections endpoints)

## Problem
Phase 4z-v's new endpoints called _user_id() — a function that doesn't
exist in this codebase. Every call to /api/user/saved-connections
returned 500 with NameError, surfacing as "Save to my list" failing
with: "name '_user_id' is not defined".

## Fix
Use g.user["id"] — the actual auth context that auth_gate populates
on every request. Same pattern as every other authenticated endpoint
in the file.
# Changelog — v4 Phase 4z-v (per-user saved connections in Postgres)

## Problem
The header dropdown ("⚡ Saved (N)") and the Connections panel sidebar
both lived in browser localStorage. Effects:
  - Logout wiped the lists (intended), but signing back in didn't bring
    them back — the user had to recreate every connection entry.
  - Cross-browser / cross-device: each browser had its own list, so an
    operator signing in from a laptop saw nothing of what was saved
    from their desktop.

## Solution
New table user_saved_connections in Postgres. Per-user list, scoped by
user_id, with (user_id, host, port, username) uniqueness so an upsert
is idempotent.

## Schema
CREATE TABLE user_saved_connections (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 8123,
    username TEXT NOT NULL DEFAULT 'default',
    db TEXT,
    sort_order INTEGER DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, host, port, username)
);

Password is NEVER stored here. Locator + display fields only. For
password persistence the user can either re-type on connect (most
private) or use the admin-managed Connection Registry (vault-backed).

## Backend
- GET    /api/user/saved-connections        list
- POST   /api/user/saved-connections        upsert (idempotent)
- DELETE /api/user/saved-connections/<id>   remove
All require an authenticated session. Save/delete audit-logged
("Save Connection To My List" / "Remove Connection From My List").

## Frontend
- loadSavedConnections()  — fetch + populate S.savedConns and
                            S.clusterList (header dropdown + sidebar)
- saveConnectionToList()  — POST + splice into in-memory state
- deleteSavedConnection() — DELETE + remove from in-memory state
- _migrateLegacyConnections() — one-time migration: push any legacy
  localStorage entries (savedConns / ch_cluster_list) to the server,
  then clear the localStorage keys so it doesn't re-fire.

## UX changes
- New "☆ Save to my list" button next to Connect in the connection
  form. Once saved, it toggles to "★ Saved — Remove" so the user can
  un-save without leaving the form.
- Connect no longer auto-persists. A connection becomes session-only
  on the sidebar; the user opts in to persistence via the Save button.
- Header dropdown ✕ and sidebar ✕ both route through the server.
- Clear all dropdown action walks deletes server-side too.

## Audit
- Save Connection To My List   (server side)
- Remove Connection From My List (server side)
- Plus the existing Save/Remove uiAudit on the client.

## Migration
First login after this deploy runs _migrateLegacyConnections() once,
which silently uploads any pre-existing localStorage entries to the
server then removes the legacy keys. Re-login is safe (idempotent
upsert).
# Changelog — v4 Phase 4z-u (cluster topology badge fix)

## Problem
Header's cluster-kind badge ("◆ 2 Shards", "🔁 Replicated …") was hidden
on a freshly-deployed replicated cluster — even when system.clusters
clearly defined two replicas. The backend classified cluster type from
whether ReplicatedMergeTree tables happened to exist (system.replicas),
not from the cluster topology itself. An empty replicated cluster has
zero rows in system.replicas → classified as "single" → badge hidden.

Additionally, ClickHouse's built-in test clusters (test_shard_localhost
et al.) were counted in num_shards / num_replicas, inflating topology
for single-cluster deployments.

## Fix
- Topology classification now reads num_shards / num_replicas from the
  user's actual cluster definition (filtered against a built-in
  cluster blocklist). Per-table existence is now a fallback for the
  edge case of a single-node deployment hosting ReplicatedMergeTree
  tables.
- New blocklist filters out test_shard_localhost,
  test_cluster_two_shards, etc. so they don't show up in num_shards
  or in the cluster picker.

## Result
- 1 shard × 2 replica (empty) → "replicated" badge "Replicated (2 Replicas)"
- 2 shard × 2 replica         → "sharded" badge "2 Shards"
- 1 shard × 1 replica         → "single" (badge hidden)
# Changelog — v4 Phase 4z-t (DB Users white-screen + query editor placeholder flash)

## Problem 1: DB Users panel blank screen
buildUsersPanel crashed mid-render whenever any S.users sub-field was
null or missing (which evidently happens after some connection-switch /
state-load paths). The crash blanked the whole main panel because
panel dispatches had no try/catch.

## Fix 1
- Added a defensive state-shape guard at the top of buildUsersPanel:
  every expected array, object, and string sub-field is normalised to
  its expected shape if missing or wrong type. The panel can no longer
  blank from this class of bug.
- Wrapped ALL 30 panel dispatches in render() in try/catch. If any
  panel throws, the main pane now shows "Panel error: <message>"
  inline with the stack trace, instead of going white.

## Problem 2: "Multi-statements / Ctrl+Enter" hint flicker
Every connection switch re-renders the query panel, which recreates
the underlying <textarea>. For ~16-32ms between DOM insertion and
CodeMirror attach, the browser paints the native textarea — including
its placeholder text. Then CodeMirror replaces it with its own DOM.
Result: a brief flicker of the placeholder text on every switch.

## Fix 2
- Added `style.display='none'` to the textarea — CodeMirror keeps it
  as its source-of-truth in the background, so hiding it is safe and
  prevents the placeholder from ever being painted.
- Changed ensureCM to always call setValue (even on empty content),
  so a freshly-mounted CM cannot show stale text from the previous
  mount for a frame.
# Changelog — v4 Phase 4z-s (Persist per-tab history across logins)

## Problem
The per-tab in-memory history (driving the "History (N)" dropdown in
the editor toolbar) was lost on every logout / login. The query_tabs
save endpoint persisted only id, name, sql — history was a transient
field. Users had to re-run queries to repopulate it.

## Fix
- Save endpoint now accepts and persists per-tab history, bounded to
  200 entries per tab, each entry's sql capped at 10_000 chars.
  Accepts both string-list and object-list shapes; normalises to
  the latter ({sql, ts?, duration?, rows?, error?}).
- All three client save sites (debounced typing, structural change,
  sendBeacon on unload) now ship history along with id/name/sql,
  trimmed to the last 200 entries client-side.
- Restore path uses srv.tabs[i].history when available instead of
  initialising to [].

## Bounds (so a long session can't bloat tabs_json)
- 200 entries per tab
- 10_000 chars per entry sql
- typical realistic upper bound per tab: ~500 KB; per user with 50
  tabs ~25 MB JSONB — within reason for a per-user-per-cluster row.
# Changelog — v4 Phase 4z-r (Audit coverage hardening for backup)

## Background
A targeted sweep of backup-related code paths revealed several
non-state-changing or rarely-used endpoints with no audit entries.
Audit coverage is now exhaustive — every backup-touching path emits
an event, which is then forwarded to SIEM destinations by the
standard pipeline.

## Backend audit additions
- /api/backup/schedules/<id>/run-now:
    NEW "Manual Trigger Backup Schedule" (captures user identity).
    Joins with the subsequent system-level "Backup Schedule Fired"
    event by schedule name for full who+what accountability.
- /api/backup/run (legacy script):
    NEW "Start Legacy Backup"
- /api/restore/run (legacy script):
    NEW "Start Legacy Restore"
- /api/backup/verify:    NEW "Legacy Backup Verify"
- /api/backup/prune:     NEW "Legacy Backup Prune"
- /api/backup/chain:     NEW "Legacy Backup Chain Inspect"
- /api/backup/schedule:  NEW "Legacy Backup Cron Schedule"

These legacy endpoints aren't called by the UI anymore but remain
wired for back-compat; any direct API invocation (curl, admin
scripts) is now visible in the audit trail and SIEM forward.

## Frontend uiAudit additions
- restoreFromHistory(): NEW "Stage Restore From History" — captured
  the moment the user clicks "Restore from this backup" in the
  history detail modal (before the Restore tab opens and the actual
  RESTORE is queued).
- scheduleToggleEnabled(): NEW "Pause Backup Schedule" / "Resume
  Backup Schedule" — separates routine schedule firing from a
  deliberate disable of automated protection.

## Security Whitepaper
§13B.3 (audit trail integration) expanded:
- Cancel Backup/Restore
- Manual Trigger Backup Schedule
- Pause / Resume Backup Schedule
- Stage Restore From History
- Legacy script-based backup endpoints
Plus a final paragraph confirming all backup events are picked up by
the SIEM forwarder within seconds of being written.

## SIEM pipeline
No code change needed. The standard SIEM forwarder
(_siem_loop, /api/siem/destinations) pulls every audit_events row
with id > last_forwarded_id and forwards in real time, so the new
event types reach all configured destinations (Splunk, Datadog,
Elastic, Slack) automatically.
# Changelog — v4 Phase 4z-q (Cancel running backup / restore)

## Problem
Backups and restores run ASYNC on the ClickHouse server and can take
many minutes for large databases. Users who fire one off (or who see
an unwanted scheduled one in progress) had no way to stop it short of
SSHing to the ClickHouse host and issuing KILL QUERY manually.

## Solution
A native ClickHouse KILL QUERY behind /api/backup/native/kill, surfaced
in three places in the UI.

## Backend
New endpoint: POST /api/backup/native/kill
  - input: id (UUID from system.backups)
  - reads system.backups to check if the operation is still in-flight
  - if terminal (BACKUP_CREATED / BACKUP_FAILED / RESTORED / RESTORE_FAILED):
    returns 200 with already_done=true — no error, idempotent
  - if in-flight: issues KILL QUERY WHERE query_id = '<id>' SYNC, returns
    the resulting kill_status
  - races (operation finished between check and kill) treated as normal,
    not errors
  - Cancel Backup/Restore audit event emitted in all paths

Defence: id is validated against a strict UUID regex before being
interpolated into SQL.

## Frontend
1. Backup tab result box gains:
   - "(running on the ClickHouse server; see History tab for progress)"
   - ✕ Cancel Backup button while runningId is set
   - Dismiss button to clear the box

2. Restore tab result box: same treatment.

3. History tab table:
   - new trailing Actions column
   - inline ✕ Cancel button on every row in CREATING_BACKUP / RESTORING
   - stopPropagation on the button so it doesn't open the detail modal

4. History detail modal:
   - hint line "// Backup is running. Cancelling now will leave a
     partial ZIP..." / "// Restore is running. Cancelling now will
     leave target tables in a partial state."
   - large ✕ Cancel button for in-flight operations

Shared cancelNativeOperation(id, kind, opts) handles all four entry
points with a single confirm() dialog, calls KILL, refreshes history,
clears UI state on success.

## ASYNC nuance
ClickHouse BACKUP TO File(...) ASYNC returns immediately with an id;
the file write continues on the server. The previous UI flipped
running:false the moment fetch returned, hiding the fact that the
operation was still running. Now we keep runningId set so the Cancel
button stays available, and only clear it on dismiss / cancel /
auto-refresh observing terminal status.
# Changelog — v4 Phase 4z-p (Multi-DB tree expansion)

## Problem
The schema sidebar, the standalone Schema panel, and the Part Inspector
all enforced accordion-style single-DB expansion. Opening a second DB
collapsed the first. Users wanted to see tables across multiple
databases at once, and to keep the unqualified-name resolution from
4z-o working with whichever DB is "active".

## Model
Each tree state gains:
  expandedDbs   — list of DBs currently open
  tablesByDb    — per-DB cache of the table list (parallel fetches)
  selectedDb    — preserved meaning: the "active" DB, what qP() returns
                  for query context, where bare table names resolve

Behaviour:
  Click closed DB        → expand it + make active + fetch tables
  Click open active DB   → collapse it; pick another expanded as active
  Click open !active DB  → just switch active context
  Click table            → make table's DB active, then open detail/SQL

Visual: ACTIVE pill on the active row (bold + accent), subtle left
border + muted background on expanded-but-not-active rows.

## Sites updated
1. Query panel schema sidebar (S.q)
2. Schema panel (S.schema)
3. Part Inspector (S.parts)

Each got: state init, load function rewrite, refresh function (now
refreshes every expanded DB in parallel), tree render rewrite.

## Compatibility
selectedDb / tables fields preserved (kept as a mirror of
tablesByDb[selectedDb]) so qP(), legacy search-tables links, table-
detail navigation etc. continue to work without changes.
# Changelog — v4 Phase 4z-o (Active database context for query editor)

## Problem
Users selecting a database from the schema panel still had to qualify
every table reference as 'db.table'. This is friction every long
session pays, and is unlike DBeaver / DataGrip / pgAdmin / Tabix /
clickhouse-client itself — all of which let the selected db drive
unqualified table resolution.

## Solution
A new payload field `database` on every /api/query/* call, propagated
to clickhouse_connect's get_client(database=...) so unqualified table
names resolve server-side without rewriting the user's SQL. Cross-db
references ('other.t') still work — the default is only consulted for
bare names.

## Backend
- _get_client(d) now honors d['database'] when present and non-empty
- Connection-level operations (cP() callers — backup, admin, etc.)
  unchanged; they don't pick up a database default

## Frontend
- New helper qP() = cP() + {database: S.q.selectedDb}
- Seven /api/query/* sites switched from cP() to qP():
  - /api/query/run         (5 sites: single, multi, batch, compare A/B,
    schema-changes apply)
  - /api/query/explain     (1 site)
  - /api/query/explain-tree (1 site)
- Visual indicator pill at the start of the primary editor toolbar:
  - Active db: blue pill 'db: <name> ✕' — click to clear
  - No active db: dashed gray 'db: (none)' with help tooltip
- Cross-tab clarity: pill shows the current selection even when the
  schema panel is collapsed.
# Changelog — v4 Phase 4z-n (Backup: differential + scheduling)

## Summary
Three new capabilities on top of phase 4z-m's native ClickHouse BACKUP
foundation:

1. **Differential backup type.** A backup whose base is always the most
   recent full. Auto-discovered server-side via system.backups using a
   filename glob — operator doesn't manually pick a base.

2. **Scheduled backups.** Cron-driven recurring backups via a new
   backup_schedules table and a 30-second-tick scheduler thread. Each
   schedule has a cron expression, target, type (full/diff/incr),
   storage path, and filename template with {db}/{type}/{date}/{time}
   /{datetime}/{ts} placeholders. Differential schedules automatically
   chain off the last full from the same schedule.

3. **Pattern support: weekly-full + daily-differential.** The canonical
   production pattern is two paired schedules in the same storage path;
   the diff schedule keys off the latest full automatically.

## Schema
New table backup_schedules with:
  - id, name, enabled, cron
  - target (database|tables|all), db_name, tables
  - backup_type (full|differential|incremental)
  - storage_path, name_template
  - last_full_name, last_run_name, last_run_at, last_status, last_error
  - next_run_at, consecutive_failures
Schedule state lives in PostgreSQL; the scheduler thread maintains it
in place. Idempotent migration (CREATE TABLE IF NOT EXISTS).

## Dependency
requirements.txt: croniter>=1.3 (pure Python, no native compile). The
scheduler thread lazy-imports it and logs a clear warning if missing
rather than crashing the application.

## Backend
- `_backup_render_name(template, db, type, dt)` — placeholder substitution
- `_backup_compute_next_run(cron, base_time)` — uses croniter
- `_backup_fire_schedule(row)` — executes one schedule, updates state
- `_backup_record_failure(id, err)` — bookkeeping for failed runs
- `_backup_scheduler_loop()` — daemon thread, app_context-wrapped,
  polls due rows every 30 seconds
- `_find_latest_full_backup(client, path, glob)` — discovers the most
  recent full backup matching a filename glob via system.backups
- New endpoints (admin-only, under existing /api/backup RBAC prefix):
    GET    /api/backup/schedules                    — list + cron presets
    POST   /api/backup/schedules                    — create
    PATCH  /api/backup/schedules/<id>               — update
    DELETE /api/backup/schedules/<id>               — delete
    POST   /api/backup/schedules/<id>/run-now       — manual trigger
- Differential support in backup_native_run: backup_type='differential'
  + base_search_glob -> auto-finds last full server-side, refuses if
  no full found rather than silently degrading

## Frontend
- New "Schedule" tab in the Backup panel (4 tabs total: Backup,
  Schedule, Restore, History)
- Backup type dropdown gains "Differential" option
- Differential mode shows a base-search-glob input with smart default
  ({db}_full_*.zip)
- Schedule list with status pills (OK/PAUSED/PENDING/FAILING-Nx),
  type pills (full/diff/incr), inline actions (Run now, Pause/Resume,
  Edit, Drop)
- Add Schedule modal with:
  - Name, enabled toggle
  - Cron expression + preset dropdown (hourly, 6h, daily 2am/4am,
    weekly Sun 2am, monthly 1st 2am)
  - Backup type + target picker
  - Database dropdown (from system.databases)
  - Storage path
  - Filename template with live preview that shows current substitution
- Empty state explicitly suggests the weekly-full + daily-diff pattern

## Installation Guide
New subsections under §7.4:
  7.4.3 Scheduled backups            (croniter dep, table, templates)
  7.4.4 Backup types                 (full/diff/incr semantics)
  7.4.5 Worked example               (weekly + daily, sample file list)
  7.4.6 Retention                    (filesystem-level cron, S3 lifecycle)
Section 5.3 table inventory now includes backup_schedules.

## RBAC
No changes — the existing /api/backup prefix entry covers all new
endpoints. Schedule administration remains admin-only.
# Changelog — v4 Phase 4z-m (Native ClickHouse Backup)

## Background
Previous backup feature relied on the bundled `clickhouse_pitr.py` script
which wrote to a local path on the app host. In real deployments where
ClickHouse lives on a different VM than the application, this failed
with `[Errno 30] Read-only file system: '/var/lib/clickhouse'` because
the app host has no clickhouse data directory.

## Approach
Replaced with native ClickHouse `BACKUP TABLE/DATABASE TO File(path)`
issued directly to the ClickHouse server. The script is bypassed
entirely. The path is supplied by the operator in the UI and must be:
  1. A directory accessible to the clickhouse-server process
  2. Covered by <backups><allowed_path> in clickhouse-server config
  3. Typically a shared mount (NFS / SMB / iSCSI) so the app host can
     also browse it for backup file inventory

## Backend
New endpoints (legacy /api/backup/run preserved for back-compat):
  POST /api/backup/native/run        — BACKUP TO File(...) ASYNC
  POST /api/backup/native/restore    — RESTORE FROM File(...) ASYNC
  POST /api/backup/native/list       — read system.backups
  POST /api/backup/native/databases  — populate UI dropdown

Helpers _backup_file_clause and _backup_target_clause build the
SQL clauses with strict validation: no path traversal, no quote
injection, every table qualified as db.table. Full + incremental
supported (incremental sets base_backup = File(...)).

## Frontend
- Nav label "PITR Backup" → "Backup"
- New `buildPITR()` panel with three tabs:
    Backup    — full or incremental, target = db / tables / all,
                Storage path + filename, native BACKUP SQL fired
    Restore   — same target picker, RESTORE FROM File(path)
                Optional: allow_non_empty_tables, structure_only
    History   — auto-refreshing table of system.backups (status
                pills, file count, size, duration, error)
- Database dropdown auto-populated from system.databases
- Result toast + colored result box after each backup/restore
- Confirmation dialog on Restore (destructive)

## Migration notes
- Old PITR sayfasındaki "List & Manage" ve "Schedule" sekmeleri kaldırıldı.
  ClickHouse'un system.backups (in-memory ring) yeterli görünürlük veriyor.
  Catalog-file-based listing eski script'in artifact'iydi.
- clickhouse_pitr.py hâlâ release'de var ama UI artık çağırmıyor.
  Yeni install'larda dosyayı silebiliriz; mevcut deployment'lar için
  back-compat olarak duruyor.

## Server-side prerequisite
ClickHouse config'inde (örnek):
  <clickhouse>
    <backups>
      <allowed_path>/mnt/shared/backups/</allowed_path>
    </backups>
  </clickhouse>

Sonra clickhouse-server restart. UI'dan path olarak
/mnt/shared/backups girilebilir.
# Changelog — v4 Phase 4z-k (LDAP / Active Directory authentication)

Hybrid auth: every login picks either "Console User" (local) or "LDAP /
AD" at the login screen. Local accounts and directory accounts are
distinct identities — they don't shadow each other and the local
administrator account always works as a recovery path even if LDAP is
misconfigured or unreachable.

## Schema (idempotent)
- users.auth_source (default 'local'), users.ldap_dn columns
- users.password_hash relaxed to NULLable for LDAP users
- ldap_config singleton table holding server URL, service-account bind
  credentials, search bases, filters, default role, nested-groups toggle
- ldap_group_mappings table: LDAP group CN -> console role

## Backend
- requirements.txt: ldap3 >= 2.9 (pure Python, no native compile step)
- Login endpoint accepts auth_method=local|ldap and branches accordingly.
  Existing API clients default to local (no breaking change).
- _ldap_authenticate: service-bind → user search → user-bind → group
  discovery → role resolution → upsert into local users table.
  Auto-provisions on first login; refreshes role on every subsequent
  login so directory group changes propagate.
- Filter injection: every interpolated value (username, user DN) is
  escaped per RFC 4515 before substitution. No filter-metacharacter
  in a malicious username can break out of its filter position.
- Active Directory specifics: nested-groups toggle swaps the inner
  member= for the AD-specific matching rule OID 1.2.840.113556.1.4.1941
  so the directory follows transitive group memberships server-side.
- New REST endpoints (admin-only, gated by /api/ldap prefix in RBAC):
    GET  /api/auth/methods       — public, tells login screen which methods exist
    GET  /api/ldap/config        — masked
    POST /api/ldap/config        — upsert; empty password preserves saved one
    POST /api/ldap/test          — service-bind + sample search, structured result
    GET  /api/ldap/mappings      — list
    POST /api/ldap/mappings      — upsert (unique on group_name)
    DELETE /api/ldap/mappings/<id>

## Frontend
- Login screen now offers a "Console User / LDAP / AD" pill switcher
  when LDAP is enabled. Choice persisted in localStorage so a returning
  user lands on the same method by default. Hidden entirely when LDAP
  is disabled, so a default install shows the simple two-field form.
- New admin nav slot "LDAP" with two cards: server config (all fields,
  with masked service-account password and a Test Connection button)
  and group-role mappings (add/remove with role dropdown).
- Test Connection button does a live service-bind + sample search and
  returns a structured per-step result (bind OK / search OK / N entries
  found, or step-name + error string on failure).

## Recovery path
The local admin account always works. If LDAP is misconfigured or the
directory is unreachable, the operator signs in via "Console User",
fixes the LDAP configuration in the admin panel, and re-tests.

## Per-user data is unaffected by auth source
Query tabs, query history, favorites, dashboards are all tied to
users.id. An LDAP user gets a real users row on first login (with
auth_source='ldap' as the marker, password_hash NULL). All
per-user features work identically — logout/login restores tabs,
history, favorites, dashboards exactly as for local users.

## Security whitepaper
New chapter 7B "LDAP / Active Directory Authentication" with eight
sections covering hybrid model, authentication flow, group→role
mapping, AD specifics (sAMAccountName, nested groups, memberOf, TLS),
filter injection safety, credential storage, audit, and
disabled-by-default behaviour. Chapter 8 privileged-operations table
extended with LDAP-management rows.
# Changelog — v4 Phase 4z-i (SIEM forwarder context fix + status labels)

## 1. Forward log was empty because the background thread had no Flask context

Symptom: panel showed status PENDING forever, "18 events behind", last
attempt blank, and the Log modal said "no forward attempts yet" — even
though the URL was reachable and synthetic Test events worked fine.

Root cause: db_global() resolves the DB connection via Flask's `g`
object (set up by request middleware). The SIEM forwarder ran in a
plain background thread with no active request and no application
context, so every `g.db_global` access raised RuntimeError. The
try/excepts inside the forwarder swallowed those exceptions, so
nothing logged the failure and the forwarder appeared to run while
actually doing nothing.

The Test button worked because it executes inside a Flask request
handler — which has app context — so the same db_global() succeeds.

Fix: wrap `_siem_loop`'s body and the startup log-trim in
`with app.app_context():`. This is the same pattern used by other
background workers in the codebase (the query-history writer at line
1842 and the alerts loop). _siem_forward_one inherits the context
from its caller, so no further changes were needed there. Forwarder
exceptions now also log at WARNING level — silent failures are no
longer possible.

## 2. Status pill rewording

Per request, status text now reads in operator-friendly terms:
  - STOPPED — destination is disabled (was: OFF)
  - ACTIVE  — enabled and most recent attempt succeeded (was: OK)
  - FAILING — enabled but recent attempts errored, backoff active
  - PENDING — enabled but no cycle has run yet (transient state,
              clears within ~8 s; after the context-fix above this
              should be visible only momentarily after creating a
              destination)
# Changelog — v4 Phase 4z-h (SIEM forwarding)

Forward every audit_events row to external SIEMs (Splunk, Elastic,
Datadog, Slack, or any HTTP webhook) in near-real-time. Compliance
requirement for SOC 2 / ISO 27001 / PCI-DSS — audit trail must reside
on a tamper-resistant external store, not just on the host that
generated it.

## Schema (idempotent)
Two new tables:
  - siem_destinations: configured external sinks with delivery state
    (last_forwarded_id, last_status, consecutive_failures)
  - siem_forward_log:  rolling record of recent forward attempts per
    destination (status, http_status, batch_size, event range, error)

## Backend
A daemon thread (siem-forwarder) wakes every 8 s, fans out across
every enabled destination, and for each one pulls up to 100 events
from audit_events WHERE id > last_forwarded_id. Events are formatted
(generic JSON, Elastic ECS, Splunk HEC, or Slack), POSTed via stdlib
urllib, and the watermark advances only on HTTP 2xx — at-least-once
delivery, no event drops on transient failure. Exponential backoff
caps at 5 minutes for destinations that keep failing.

REST endpoints (all admin-only):
  GET    /api/siem/destinations              — list + lag
  POST   /api/siem/destinations              — create
  PATCH  /api/siem/destinations/<id>         — update
  DELETE /api/siem/destinations/<id>         — delete
  POST   /api/siem/destinations/<id>/test    — send synthetic event
  GET    /api/siem/destinations/<id>/log     — recent forwards

Permission table denies non-admin roles. Auth-header values are
masked when serialised back to clients, and a round-tripped masked
value preserves the underlying secret on PATCH.

## Frontend
New admin nav slot "SIEM" with shield-icon. Panel shows a destination
table with name, status pill (OK / FAILING / OFF / PENDING), format,
url, lag (events behind), and per-row actions:
  - ⚡ Test — synthetic event, immediate result toast
  - Log    — recent forward attempts modal
  - Edit   — opens form pre-filled with masked auth
  - Drop   — confirm + delete

Empty state explicitly suggests https://webhook.site/ for first-time
end-to-end verification under a minute.

## What it solves
1. Compliance: audit trail leaves the host immediately
2. Real-time alerting: SIEM detects anomalies (admin actions at 03:00,
   foreign-IP logins, mass deletions) and pages on-call
3. Long-term retention: console DB is short-lived; SIEM is archive
4. Multi-instance: one SIEM aggregates many consoles

## Test
1. Get a webhook.site URL.
2. Console → Admin → SIEM → + Add Destination, paste URL, format=JSON.
3. Click Test — synthetic event lands on webhook.site immediately.
4. Run a query or create a user — real audit event lands within ~10 s.
5. Disable destination, generate events, re-enable — backlog flushes.
# Changelog — v4 Phase 4z-g (history reset on reconnect + Run All as a single history entry)

Two related history-system fixes.

## 1. History reset on reconnect — race fix

When the user reconnected to a cluster, the History dropdown came back
empty. Root cause was a race inside doConnect:

  1. Default tab [t1] is created synchronously with history:[].
  2. loadQueryHistory() fires asynchronously — it fetches history and
     fills t1.history.
  3. loadQueryTabs() also fires asynchronously. When its .then() runs,
     it REPLACES S.q.tabs with the server-restored tabs, each
     freshly initialised with history:[]. That replacement wipes the
     work loadQueryHistory just did.

Fix: at the end of the loadQueryTabs().then() callback — right after
S.q.tabs has been swapped — we now call loadQueryHistory() again. It
re-fetches and populates each restored tab's history, so the dropdown
is correct regardless of which of the two requests landed first.

(The in-memory snapshot path — when reconnecting within the same
session — was already correct because snapshot tabs preserve their
history field.)

## 2. Run All as a single history entry

A Run All used to enter N rows into history (one per statement). The
user wanted to see it as one logical entry: "Run All — 3 statements".
Implemented end to end:

### Schema (idempotent migration)
ALTER TABLE query_history ADD COLUMN IF NOT EXISTS batch_id TEXT;
Runs at startup via init_db(), zero-effort upgrade.

### Backend
/api/query/run now accepts an optional batch_id (≤64 chars). Both the
success and the failure INSERT into query_history record it.

/api/query-history GET was rewritten: it pulls 5× the asked limit of
raw rows (so a 5-statement batch still fits within one folded page),
then folds rows sharing a batch_id into a single output entry. Each
batch entry carries:
  - is_batch: true
  - batch_id, statement_count
  - duration_ms = sum across the batch's statements
  - rows_returned = sum across the batch's statements
  - sql = each statement's text joined by ';\n\n' (original execution
    order — the iteration reverses to undo the newest-first scan)
  - error = first non-null error within the batch (or null)
Non-batch rows still dedupe by SQL the way the old endpoint did, so
the dropdown stays clean when the user re-runs the same query.

### Frontend — Run All
Generates one batchId per Run All session ('b' + base36 of now() +
random suffix) and passes it on every /api/query/run call in the
loop. After the loop completes, instead of the previous N per-
statement history.unshift() calls, ONE batch entry is unshifted with:
  - joined SQL,
  - sum of per-statement durations + row counts,
  - is_batch:true, batch_id, statement_count.
Capped at 10 entries (unchanged).

### Frontend — display
Each history row checks item.is_batch. When true, the row is prefixed
with an accent-coloured pill:
  ▶▶ Run All · 3 stmts
The combined SQL still appears next to it (single line, truncated as
before). Clicking the row still expands to show the full joined SQL
and offers the same Load / Re-run actions, so re-running a "Run All"
loads the joined statements back into the editor.
# Changelog — v4 Phase 4z-f (keyboard shortcuts that actually fire + visible ? hint)

static/index.html only.

## 1. Shortcuts now work when the editor has focus

The previous keydown handler had a single early-return:
    if(_isTyping(e.target)) return;
right before the Alt-key tab shortcut block. This blocked every Alt
combo while the user was typing in CodeMirror — which is the most
common state on the Query panel — so Alt+T / Alt+W / Alt+1..9 looked
broken from the user's seat.

Fix: the typing gate now applies only to '?' (we still need the user
to be able to type a question mark inside SQL). Alt-key shortcuts
bypass it deliberately, since Alt+letter is never a normal-typing
combination and we want the shortcuts to fire from inside the editor.

## 2. Tab navigation: Alt+[ / Alt+] instead of Alt+Arrow

Alt+Left and Alt+Right are bound by every major browser to history
back / forward. The old binding either did nothing or actually
triggered a navigation away from the page. Swapped to Alt+[ for
previous and Alt+] for next — a common convention.

The overlay's text was updated to match.

## 3. The ? hint button is now visibly colorful

The bottom-right discoverability button was a 30px gray circle at 55%
opacity, which several users missed entirely. Now:

  - 38px circle (slightly bigger)
  - Solid accent (var(--ac)) background, white '?'
  - Drop shadow with the accent colour for a subtle glow
  - Hover: scales to 108% and brighter shadow
  - Full opacity at rest — no longer pretending to be subtle

Builds once per session, lives outside #app so render() doesn't
recreate or move it.
# Changelog — v4 Phase 4z-e (keyboard shortcut overlay + power-user tab shortcuts)

static/index.html only. ~200 lines, in three pieces:

## 1. Global keydown listener

A single window-level keydown handler. Skips entirely when the user is
typing in an input, textarea, or CodeMirror — so '?' inside SQL still
types a question mark, and Alt-letter shortcuts inside form fields
don't hijack anything. Outside typing fields:

  ?            → toggle the shortcuts overlay
  Esc          → close overlay if open
  Alt + T      → new query tab        (only when nav === 'query')
  Alt + W      → close active tab     (refuses to close the last one)
  Alt + ← / →  → previous / next tab
  Alt + 1..9   → jump to tab N by position

Browser-reserved combos (Ctrl/Cmd + T, W, Tab) are intentionally not
used.

## 2. Shortcut reference overlay

A new state slot S.showShortcuts (boolean) controls the overlay.
buildShortcutsOverlay() returns the modal element when true, null
when false. Mounted to document.body at the end of render() so it
floats above every panel regardless of which one is current.

The overlay shows three grouped sections — Query editor, Query tabs,
Anywhere — each with key caps + descriptions. The modifier labels
adapt to OS: ⌘/⌥/⇧ on Mac, Ctrl/Alt/Shift on Linux/Windows.

Clicking the dimmed backdrop or the ✕ button closes the overlay; Esc
also closes it (handled in the global keydown listener regardless of
focus state, since it's a quick exit affordance).

## 3. Discoverability hint

A small fixed-position '?' button anchored to the bottom-right of the
viewport so users see that shortcuts exist. Visible only when logged
in, opens the same overlay. Subtle (55% opacity) until hovered, then
fully opaque. Built once per session and never re-built — it lives
outside #app so render()s don't touch it.
# Changelog — v4 Phase 4z-d (tab switch bug: previous tab's SQL bleeding into new tab)

static/index.html only.

## The bug

After switching from tab A to tab B (or via closeTab landing on a
different tab), the editor sometimes kept showing A's SQL even though
S.q.activeTabId had moved to B. The user described it as "ayni
queryleri goruyorum, başka tab'deyim ama önceki tab'ı gösteriyor."

## Root cause

switchTab tore down and rebuilt CodeMirror lazily, by leaving it to
ensureCM to call _cm.toTextArea() the next time the DOM didn't match.
That handoff worked most of the time, but render() destroys the
previous textarea via `app.innerHTML = ''` while the JS variable _cm
keeps a strong reference to the now-orphaned old textarea. ensureCM's
"is _cm still attached to this textarea?" check (_cm.getTextArea() ===
ta) returned false (different DOM nodes), so it called
_cm.toTextArea() on the orphan — which silently failed or left _cm in
a half-detached state. The new CM was created, but the value
displayed could lag behind the underlying tab.sql.

## Fix — proactive teardown

switchTab, closeTab, and newTab now all follow the same sequence
whenever the active tab changes:

  1. Snapshot the current tab's SQL out of CM (CM is source of truth).
  2. **Force-destroy CM immediately**:
       try { _cm.toTextArea(); } catch(e) {} _cm = null;
     before any state change or render call.
  3. Mutate S.q.activeTabId (and / or the tabs array).
  4. render() — builds a fresh textarea with the new active tab's SQL
     as its initial value.
  5. setTimeout(0) → ensureCM creates a fresh CM on the fresh
     textarea. An explicit _cm.setValue(newTab.sql) follows as
     belt-and-suspenders against any divergence.

ensureCM is unchanged — but now it never has a stale _cm to deal with
when called from these three paths, so its early-return optimization
can't accidentally short-circuit a real tab change.
# Changelog — v4 Phase 4z-c (Cluster Health: developer-hidden + self-referential error fix + auth/cluster error separation)

## 1. Permission

Permission table now lists /api/cluster/health → (admin, monitoring,
readonly). Developer role is explicitly excluded — Cluster Health is
an operational/SRE feature, not a query-writer's tool.

Frontend nav handler also hides the "Cluster Health" entry from
developer users so a developer doesn't even see the slot to click.

## 2. Self-referential error fix (the real bug)

The Cluster Health refresh used to issue
    SELECT count() FROM system.zookeeper WHERE path='/'
unconditionally. On standalone (non-replicated) clusters the
system.zookeeper table doesn't exist, so every refresh landed an
UNKNOWN_TABLE entry in system.errors. The very next query in the
refresh — reading system.errors over the last hour — counted those
UNKNOWN_TABLE rows, so the user saw the error count tick up by
exactly the number of refreshes they'd just done. The page reported
its own activity as a problem.

Fix: the ZK probe is now gated on a cheap pre-check against
system.tables. If system.zookeeper isn't present, we don't query it
at all and the ZK card shows "Not configured" (neutral gray) instead
of "Unreachable" (red). The "list index out of range" Python
traceback in the previous ZK error message was a symptom of the same
underlying issue (empty _qsafe result, indexed access blew up); the
guarded code path no longer reaches it.

## 3. Cluster errors vs authentication failures, split

system.errors mixes operational issues (UNKNOWN_TABLE,
TOO_MANY_PARTS, ZOOKEEPER_LOST, ...) with per-user authentication
failures (WRONG_PASSWORD, REQUIRED_PASSWORD, UNKNOWN_USER,
AUTHENTICATION_FAILED). The former are cluster health signal; the
latter are application/user signal that happen to share the same
table. Mixing them made the "ERRORS (LAST HOUR)" number on the
summary card jump up whenever a single user mistyped their password.

Backend now returns two separate lists — items (cluster errors) and
auth_items (auth failures) — plus separate totals. The UI:

  • The summary card's headline number is the cluster total only.
  • A secondary line "+ N authentication failure(s)" appears beneath
    when there are any auth failures (no headline alarm for them).
  • Two detail tables: "Cluster errors by kind" (default styling)
    and "Authentication failures" (muted styling, explicit subtitle
    "login attempts, not cluster issues") so the operator instantly
    knows which is which.

Also: any error whose message contains 'system.zookeeper' or
'postponed_till' (the column-drift error from the rep-queue feature
on older CH versions) is dropped from both lists as a self-noise
guard, in case any stale ones from before this fix are still inside
the one-hour window when the page first loads.
# Changelog — v4 Phase 4z-b (search box pinned right, doesn't scroll away with tabs)

static/index.html only.

The tab bar used to be a single horizontally-scrolling row containing
the tab list, the + button, the search input, the row counter and
the right-side Diff / Settings group. When many tabs were open, the
list scrolled horizontally and dragged the search input AND the
Diff/Settings buttons off-screen with it.

Fix: the tab bar is now an outer flex container with two children:

  • Left child (tabListBox) — flex:1 1 auto, overflowX:auto. Holds
    the tab list and + button. Scrolls horizontally on its own.

  • Right child (tabRightGroup) — flexShrink:0. Holds the 🔍 search
    box, the X/Y match counter, and the Diff / Settings / Editor
    buttons. Never participates in the tab-list scroll.

A subtle visual separator (border-left on the right group) makes
the split obvious. The search box is pinned just before Diff so the
right-side controls are now: search → Diff → Settings → Editor.

No other behavior changed: keyboard focus preservation, tab rename,
tab close, tab filter — all unchanged. State (S.q.tabFilter,
S.q.renamingTabId) untouched.
# Changelog — v4 Phase 4z-a (MV/cluster-health crash fix + tab search threshold + tab rename)

Three targeted fixes on top of phase 4z.

## 1. Bug fix — MV and Cluster Health crashed with 'list' object has no attribute 'result_rows'

`_qsafe(cl, sql)` already returns the row list (it ends with
`return cl.query(sql).result_rows`). The phase 4z endpoints
(mv_list, cluster_health) wrote `_qsafe(...).result_rows`,
re-accessing .result_rows on a plain list → AttributeError, which
the panels surfaced as a red toast.

Fixed in five places:
  - app.py mv_list (one site)
  - app.py cluster_health (four sites: replication, distributed,
    zookeeper, recent errors)

After the fix, an empty cluster (no MVs, no problematic replicas)
shows the friendly empty-state message in each panel, never a
red toast.

## 2. Tab search visibility — threshold dropped from 5 to 2 tabs

The 🔍 search box was hidden until the user already had 5 tabs, by
which point they were drowning. Now it appears as soon as there's a
second tab. Placeholder simplified to "🔍 search tabs" so it stands
out at a glance.

## 3. Tab rename — double-click or right-click, persists across logins

New state slot S.q.renamingTabId. Two ways to start renaming:
  - Double-click the tab name
  - Right-click the tab name (context menu suppressed)

Renaming replaces the tab name with an inline <input>:
  - Enter   → commit (saves new name)
  - Blur    → commit
  - Escape  → cancel without changing the name

The render loop auto-focuses and selects-all on the rename input
after each render pass, so typing begins immediately and replaces
the existing name as one would expect.

Persistence is free: the rename mutates tab.name, then calls
saveQueryTabs(true) which already mirrors tab.name to Postgres
(phase 4s). Login restore (phase 4t) reads tab.name back from the
server. So a renamed tab survives logout, browser close, and
re-login automatically — no schema or restore changes needed.

Tab close (✕) is suppressed while renaming, so a stray click in
the input area can't accidentally close the tab.
# Changelog — v4 Phase 4z (HA addendum + deep health + cluster health panel + MV manager + dictionaries reload-all + tab search + dark theme audit)

A seven-item omnibus delivered as one phase because they share no
dependencies and the user requested a single ZIP.

## 1. Postgres + Redis HA addendum (NEW DOC)

ClickHouse-Console-HA-Addendum-v4.docx — a sibling to the Multi-Node
Deployment Guide that documents how to plug the console into an HA
state tier. Covers Patroni + HAProxy + etcd for self-hosted Postgres,
Redis Sentinel for self-hosted Redis, and the managed-service path
(RDS Multi-AZ, ElastiCache, etc.) for each. No code change — the
.env's DATABASE_URL / REDIS_URL are the only contact points. Includes
failover drill procedures, observability checklist, backup/HA
separation, and a decision matrix.

## 2. /health/deep endpoint (backend)

Adds /health/deep to app.py. Structurally checks Postgres (SELECT 1
through the connection pool) and Redis (session_store.ping). Returns
JSON with per-check ok/ms/error fields plus an overall ok flag. Always
returns HTTP 200 — operators read the body to decide if degraded. Use
as an LB-side fail-closed liveness probe alongside the shallow /health.

## 3. Cluster Health panel (NEW NAV)

New nav slot "Cluster Health" between Rep. Queue and Table Health.
One panel showing four sections in a single round trip via
/api/cluster/health:

  - Replication state of every replicated table — anything with
    absolute_delay > 60s, future_parts > 50, is_readonly = 1, ZK
    session expired, or active_replicas < total_replicas surfaces in
    the "Problematic replicas" table.
  - Distributed queue backlog per (database, table) — long backlogs
    mean inserts aren't flushing to remote shards.
  - ZooKeeper reachability — SELECT count() FROM system.zookeeper
    WHERE path='/', round-trip latency reported.
  - Recent errors from system.errors over the last hour, grouped by
    error name with last_message.

Each section may fail independently; one bad section doesn't poison
the others. Manual refresh, last-loaded-at timestamp, status pill
cards summarizing each section.

## 4. Materialized Views manager (NEW NAV)

New nav slot "Mat. Views" in Storage & Schema. Lists every MV on the
cluster via /api/mv/list (queries system.tables WHERE engine LIKE
'%MaterializedView%'). Each row shows database/name, engine, row count,
size, last modified time. Click to expand and view the full CREATE
DDL inline.

Per-row actions:
  - Refresh (POST /api/mv/refresh) — issues SYSTEM REFRESH VIEW for
    ClickHouse 24.x+ refreshable MVs. Classic continuous MVs return
    a clear toast explaining they aren't refreshable.
  - Drop (POST /api/mv/drop) — DROP TABLE IF EXISTS with SYNC.
    Identifier regex validation server-side keeps SQL injection out.

Top-level action: + Create MV opens a modal with database / view name
/ optional destination table / SELECT statement fields. Goes through
the existing /api/query/run path so the DDL gets the same auditing
and permission gates as any other DDL.

Permission table additions:
  /api/mv/drop     → admin, developer
  /api/mv/refresh  → admin, developer

## 5. Dictionaries Reload-All (cleaner endpoint)

The dictionaries panel had a "Reload All" button but it was POSTing
SYSTEM RELOAD DICTIONARIES through /api/query/run, which bypasses the
dedicated permission/audit path. New endpoint
/api/dictionaries/reload-all wraps the same command, gated by the
permission table (admin, developer), and the panel button now uses it.

## 6. Tab search (frontend, SQL editor)

New state slot S.q.tabFilter. The tab bar gained a small "🔍 filter
tabs (N)" input visible only when there are ≥5 tabs — below that the
clutter outweighs the benefit. Typing filters the displayed tabs by
free-text match across tab name and SQL content (case-insensitive).
Match counter ("3/27") appears next to the input while the filter is
active. Focus is preserved across re-renders the usual way (capture
caret → setQ → render → restore focus + selection).

## 7. Dark theme audit

Two concrete fixes:

  - The dark override block was missing --term and --term2 entirely,
    so the terminal panel inherited the light values. Added both,
    matching the dark surface family.
  - --tx3 (#2a4060) and --tx4 (#192840) collided with --bd (#192840)
    and --bd3 (#2a4060) — dim text on var(--surface) (#0d1520) was
    near-invisible. The new Cluster Health and Materialized Views
    panels relied heavily on var(--tx3) / var(--tx4) for labels and
    would have looked broken in dark mode. Raised both to readable
    values (#5070a0 / #3a5570) above the border range while still
    visibly dimmer than --tx2.

  Also updated --nav-tx to the new --tx3 value for consistency in the
  nav sidebar's section labels.

  The remaining hardcoded #3a5570 / #4a6a90 references are inside
  the left cluster sidebar, which is intentionally hardcoded dark
  (background #0d1520 inline) in both themes — those values are fine
  as-is.
# Changelog — v4 Phase 4y (4-size widgets + dense pack + cross-login dashboard persistence)

Schema, backend, and frontend.

## Widget sizing — 4 options + dense packing

Each widget has a new `size` field with one of: 'sm' (¼ width), 'md'
(½ width), 'lg' (¾ width), 'xl' (full width). Old widgets without a size
are treated as 'md'. Default for newly-created widgets is also 'md'.

The three pre-existing widget grids (small, medium, timeseries) are
replaced by ONE responsive grid using CSS `grid-auto-flow: dense`. When
a row doesn't quite fill, a later widget that fits slips into the gap
— no visual whitespace. Span is determined per widget; the grid
collapses to fewer columns automatically as the window narrows.

The Add Widget modal grew a Size selector (¼/½/¾/1×). Existing widgets
sprout a small chip in their top-left corner while the dashboard is in
edit mode — click it to cycle sm → md → lg → xl. Cycling persists
immediately.

## Cross-browser dashboard persistence

New table `user_dashboards`:
    PRIMARY KEY (user_id)
    boards_json   JSONB
    active_id     TEXT
    updated_at    TIMESTAMPTZ
Idempotent migration via schema.sql.

New endpoints:
    GET  /api/dashboards         — returns {boards, active_id} for the
                                   signed-in user
    POST /api/dashboards         — upsert, with server-side limits:
                                     20 boards / user
                                     50 widgets / board
                                     50KB SQL / widget
    Trims transient widget fields (value, tsData, error, history) before
    persist — only durable widget config goes to disk.

Frontend:
    saveDashboardsToServer()     — debounced 1.5s, called from dashSave
    loadDashboardsFromServer()   — fetched after login or session
                                   restore, replaces local boards if
                                   server has any.

User-visible result: a user who logs in from a different browser sees
the same dashboards they built on their primary machine. localStorage
remains the in-browser fast path; the server is the cross-device backup.
# Changelog — v4 Phase 4x (kill cross-panel auto-refresh leaks)

static/index.html only.

## Root cause

A panel-local auto-refresh timer (mutations, rep queue, monitor,
dashboard widgets) was surviving navigation away from its own panel and
keeping its data-loading function firing every 10s — and every one of
those functions called render(). render() rebuilds #app from scratch,
which destroys CodeMirror, briefly exposes the raw <textarea> with its
"Multiple statements separated by ;" placeholder, and then ensureCM()
re-attaches CodeMirror. The user saw the editor flash through the
placeholder every few seconds.

Two places leaked:
- switchCluster() did only partial cleanup (stopMonitorRefresh +
  _repQTimer + dashboard widgets) and forgot _mutationTimer entirely.
- No defensive guard inside the loader functions, so even a single
  stray timer would keep rendering an off-screen panel.

## Fixes

1. switchCluster() now calls the comprehensive stopAllAutoRefresh()
   that the nav handler already uses. Every timer is cleared.

2. Each loadX() that gets attached to an auto-refresh timer now
   refuses to run when its own panel is not active, and clears the
   timer when it detects the leak — self-healing. Covers:
   - loadMutations  (S.nav==='mutations')
   - loadRepQueue   (S.nav==='repqueue')
   - _monitorRefresh (S.nav==='monitor')
   - dashRunWidget   (S.nav==='dashboard')

Together: the timer cannot leak from switchCluster, and even if some
future code path does leak one, the loader notices and silences itself
on the first tick.
# Changelog — v4 Phase 4w (collapsible cluster sidebar)

static/index.html only.

The left cluster sidebar can now be collapsed to a 32px column or
expanded to its normal 200px. State is persisted to localStorage under
"ch_csb_collapsed" so the user's chosen layout survives both page
refresh and logout/login (localStorage is per-browser, not per-session).

Expanded view: 200px column with the "Connections" header on the left
and a "‹" collapse button on the right. Click to collapse.

Collapsed view: 32px column with a single "›" button at the top and a
vertical "CONNECTIONS" label. Click the button to re-expand.

Width transitions smoothly (.15s). State change is the only thing that
triggers a render — no other state is touched.

State key: S.csbCollapsed (boolean), bootstrapped on page load from
localStorage. Toggle helper: toggleCsb() flips the value, persists to
localStorage, calls render().
# Changelog — v4 Phase 4u (no-stuck-on-Connecting guarantees)

static/index.html only.

Three defensive changes to testConn so the UI cannot get stuck on
"Connecting…", regardless of how the call sequence races:

1. The 8-second watchdog now fires UNCONDITIONALLY when connStatus is
   still 'testing'. The earlier "only if S.conn matches _startKey"
   guard meant any mid-flight cluster switch left the UI frozen forever
   — the watchdog silently exited because S.conn no longer matched.
   Now if 'testing' is still on screen at 8s, it flips to 'fail'.

2. The stale-response early-return now clears 'testing' too. Previously
   when POST returned and the user had switched clusters mid-flight,
   the code returned silently and left connStatus on whatever value it
   was (often still 'testing'). Now it flips to 'idle' so the UI
   recovers immediately rather than waiting for the watchdog.

3. The catch block follows the same rule: on any error, if we are
   still 'testing', force out of it (to 'fail' if keys still match
   and the error is for the current attempt, else to 'idle').

Also: the Connect and Test buttons are now `disabled` while a test is
in flight, and the Connect button reads "⏳ Connecting…" instead of
"✓ Connect" during that window. This blocks the double-click race
where a second click stacks another testConn over the first.

Together these make "Connecting…" a strictly transient state — it
either resolves to 'ok' / 'fail' or, at worst, gets cleared by the
8-second watchdog. It can no longer outlive an actual response.
# Changelog — v4 Phase 4t (refresh-resilient connection list + unstuck "Connecting")

static/index.html only.

## Bug 1 — connections vanish after page refresh

The sidebar cluster list (S.clusterList) lived only in browser memory.
A page refresh wiped it, leaving the user staring at an empty sidebar
even though their saved Postgres-side connection list was untouched.

Fix: S.clusterList is now mirrored to localStorage under the key
"ch_cluster_list", WITHOUT passwords. A new _persistClusterList() helper
writes the safe mirror after every mutation (testConn success, sidebar
Remove button, logout reset). On page load, S init pulls the list back.
Passwords are intentionally not persisted to localStorage; the user still
types the password (or uses stored credentials from the Connections
panel) when they click Connect.

## Bug 2 — second connection stuck on "Connecting…"

Phase 4s added `await loadQueryTabs()` inside the testConn success path,
between the line that sets connStatus='ok' and the final render(). If
the GET took noticeably long or hung, the UI never re-rendered to clear
"Connecting…", and a fresh connection attempt was visually frozen.

Fix: loadQueryTabs is no longer awaited. The connect flow now sets up
the default empty tab synchronously and the server-persisted tabs swap
in via a non-blocking .then() handler once the GET returns. Guarded so
that a late reply for a now-stale connection (user disconnected or
switched clusters mid-fetch) is ignored, and so it never stomps tabs the
user has already started typing into.
# Changelog — v4 Phase 4s (resumable query tabs)

Schema, backend, and frontend changes so that a user picks up exactly
where they left off after logout, browser close, or full page reload.
Saved per-(user, connection); switching clusters restores that cluster's
tabs.

## Schema

New table query_tabs:
    PRIMARY KEY (user_id, conn_host, conn_port)
    tabs_json   JSONB            -- array of {id, name, sql}
    active_id   TEXT             -- the tab that was focused
    updated_at  TIMESTAMPTZ
Added via schema.sql so init_db() applies it idempotently on first boot
after the upgrade. No data migration: an empty table is the correct
initial state for every user.

## Backend

GET  /api/query/tabs?host=…&port=…
    Returns {tabs: [...], active_id} for the signed-in user. Empty if
    nothing saved yet.

POST /api/query/tabs
    Body {host, port, tabs: [{id, name, sql}], active_id}. Upsert keyed by
    (user_id, conn_host, conn_port). Enforces hard limits server-side: at
    most 50 tabs per connection, at most 200k characters of SQL per tab.

Transient fields — result, error, running, jobId, elapsed, in-tab
history — are never persisted. Restoring resets them to defaults.

## Frontend

New helpers:
    loadQueryTabs()         GET wrapper, returns {tabs, active_id} or null
    saveQueryTabs(immediate) POST wrapper, debounced 1.5s by default;
                            pass immediate=true for structural changes

Restore (in doConnect, on successful connect, priority order):
    1. Same-session in-memory snapshot (S.clusters[connKey]) — most recent
    2. Server-persisted tabs (cross-session resume)
    3. A single fresh empty tab

Save trigger points:
    - CodeMirror onChange / textarea input → debounced (1.5s)
    - newTab / closeTab / switchTab        → immediate
    - disconnectConn                       → immediate (await flushes)
    - doLogout                             → immediate (awaited POST runs
                                              BEFORE the logout POST so
                                              the cookie is still valid)
    - window.beforeunload                  → sendBeacon (browser close)

sendBeacon is used on beforeunload because regular fetch() can be
cancelled when the page is unloading; sendBeacon is queued by the
browser even after the page is gone.

User-visible result: open four tabs with different SQL, close the
browser, log back in, reconnect to the same cluster — the four tabs come
back with the same SQL, the same names, and the same active tab.
Switching to a different cluster restores that cluster's saved tabs (or
a fresh empty tab if it's the first visit). History and favorites already
persisted; this completes the picture for tab state.
# Changelog — v4 Phase 4r (/health reports correct version)

app.py only.

The /health endpoint was still hard-coded to "version": "2.0" — a leftover
from the v2 branch. Bumped to "4.0" to match the rest of the product
(title, startup banner, audit version field). The endpoint behaviour and
shape are unchanged.
# Changelog — v4 Phase 4q (history button: read state live to fix close-by-click)

static/index.html only. Follow-up to Phase 4p.

Phase 4p stopped re-rendering #app when toggling the History dropdown — which
killed the flicker, but introduced a subtler bug: the History button's
onClick captured histOpen at the moment the button was built. With no
re-render on toggle, the button DOM (and its closure) stayed stuck with the
old value, so the second click computed `!histOpen` from a stale `false` and
"re-opened" instead of closing. The popup only became closable after some
unrelated event (a finished job-poll, a tab switch, etc.) caused a full
render and rebuilt the button.

Fix: the onClick now reads `S.q.historyDropdownOpen` LIVE from state, not the
closure-captured value, so every click resolves the current open/closed
state correctly. The button's opacity (the only visual cue affected by
open/closed) is also updated directly on click, so the feedback is
immediate without a full render.
# Changelog — v4 Phase 4p (history dropdown stable across re-renders)

static/index.html only. No backend change.

The History dropdown used to render INSIDE the Query panel's DOM tree. Every
full-page render() ran `app.innerHTML=''`, so the popup was destroyed and
rebuilt on every render — and any background activity that fires render()
(job-poll completion, terminal updates, query-poll completion, schema
refreshes, etc.) would flash through the popup, giving the "popup keeps
refreshing itself" impression.

Fix: the popup is now rendered as a direct child of <body>, outside #app,
managed by four new helpers — _buildHistoryPopup, _histReposition,
_historyPopupKey, _histSync. The popup persists across renders. Each
render() simply calls _histSync, which:
  • removes the popup if it should be closed,
  • repositions it (cheap, just style updates) if its content key is
    unchanged — NO DOM recreation, NO flicker,
  • rebuilds it only when content actually changes (entry expanded,
    history list grew, popup just opened).

The History button click no longer triggers a full render() — toggling the
popup is purely a _histSync call, which is essentially free. The same applies
to clicking an entry to expand it. Loading a query to the editor and clearing
history still trigger render() so the button's "(N)" entry-count label updates.

User-visible result: the dropdown opens once, sits still, and accepts scroll /
clicks without visible re-renders.
# Changelog — v4 Phase 4o (first_name / last_name on users)

Schema change + endpoint updates + UI.

## Schema

The users table gains two optional columns: first_name TEXT and last_name TEXT.
A new CREATE TABLE includes them; existing installs are migrated automatically
on the next service boot via:
    ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name  TEXT;
init_db() applies schema.sql on every boot, so the migration is idempotent and
happens transparently — no manual SQL, no downtime, no data loss. Existing
users keep NULL first_name / last_name until an admin sets them.

## Backend

- POST /api/console/users/create now accepts optional first_name and
  last_name strings (trimmed, stored as-is).
- GET  /api/console/users/list now returns first_name and last_name.
- GET  /api/console/profile      now returns first_name and last_name.

## Frontend

- Create User form: two new optional inputs (First name, Last name).
- User List, expanded profile row (clicking a username): shows a new
  "Full name" line — "(not set)" if both are empty, otherwise the two
  names joined and trimmed.
- User List search: now also matches first_name / last_name.
- My Profile panel: shows the same "Full name" line.
# Changelog — v4 Phase 4n (search boxes for Active Sessions and User List)

static/index.html only.

Two search inputs added to the Users panel:
- Active Sessions: filters rows by username / role / IP, case-insensitive.
- User List: filters rows by username / email / role, case-insensitive.

The trick was keeping focus in the textbox while typing. The framework
rebuilds the DOM on every render(), so a naive input would lose focus on
every keystroke. The new _searchOnInput helper:
  1. captures the caret position before re-rendering,
  2. updates state with setQ + calls render() (synchronous innerHTML rebuild),
  3. finds the freshly-rebuilt input by its stable id and restores focus +
     caret to the exact position.

State: S.activeSessions.search and S.consoleUsers.search.
Empty-state messages distinguish "no rows at all" from "no matches for X".
# Changelog — v4 Phase 4m (Users panel cleanup + inline profile view + email on create)

static/index.html only. No schema or backend change — the list endpoint
already returned every field needed; create already accepted email.

## 1. Removed empty "Console Users" card from Users panel

The card was a header + lone Refresh button with no content, sitting above the
actual user table. It is gone. The Refresh button moved into the "User List"
card title where it belongs (right-aligned).

## 2. Click a username to view that user's profile inline

Clicking a username in the User List toggles an expanded detail row below it,
showing the same fields the "My Profile" panel shows: username, role, email
("(not set)" if empty), status (Active / Inactive), created_at, last_login.
A small chevron (▸ / ▾) indicates state. Click again to collapse. State lives
in S.consoleUsers.expanded (a user id, or null).

## 3. Create User form now accepts email

A new Email input sits between Username and Password (optional, the placeholder
says so). The form POSTs email along with the other fields to
/api/console/users/create — the endpoint already supported the email
parameter and the users table already has the column.
# Changelog — v4 Phase 4l (company logo as browser tab favicon)

static/index.html only.

The browser tab showed no real favicon. A tiny snippet now sets the existing
embedded company logo (LOGO_SRC) as the tab icon: it creates/updates a
<link rel="icon"> with LOGO_SRC as the href. It reuses the already-embedded
logo constant, so the large base64 blob is not duplicated into <head>.
# Changelog — v4 Phase 4k (CRITICAL: revert dangerous rsync --delete; fix initial conn state)

Two fixes. No app logic change beyond the connection-state default.

## 1. CRITICAL — removed rsync --delete from update.sh

Phase 4j added `rsync -a --delete` to clean up files dropped from the repo.
This was a mistake: the install directory legitimately contains per-install
files that are NOT in the repo — most importantly the license file
(customer.lic) at the install root. --delete protected the --exclude'd paths
(.env, data/, logs/, .venv/, nginx/certs/) but wiped everything else not in
the release, including customer.lic.

update.sh now uses plain `rsync -a` (NO --delete). Obsolete files are removed
by an EXPLICIT, hand-maintained name list (currently just the two NEXT_STEPS
docs) — never a blanket delete. Per-install files can no longer be destroyed
by an update.

Note on what was at risk: the app reads its license from data/global/license.lic,
which is inside the --exclude'd data/ directory and was NOT deleted. The file
removed was the root-level customer.lic (the original copy you cp into
data/global/). A missing license does not crash the app — it falls back to
COMMUNITY mode (3-user cap + banner).

## 2. Initial connection state no longer shows a phantom "localhost" card

Phase 4h fixed _resetConnectionState() to use host:'' but the INITIAL S state
object still had conn:{host:'localhost',...}. On a fresh page load / refresh,
S is built from that initial object (not via _resetConnectionState), so the
CONNECTIONS sidebar — which renders a card for any S.conn with a truthy host —
showed a "localhost:8123 / Not connected" card again. The initial state now
uses host:'' as well, so a refreshed, freshly-logged-in user sees an empty
CONNECTIONS panel.
# Changelog — v4 Phase 4j (update.sh removes stale files)

deploy/update.sh only. No app change.

The rsync in update.sh ran without --delete, so files that were dropped from
the release in an earlier version still lingered in /opt/clickhouse-console
(e.g. NEXT_STEPS.md, removed from the repo back in Phase 4b but never cleaned
from existing installs). update.sh now runs rsync -a --delete: anything no
longer part of the release is removed from the install on the next update.

The --exclude rules double as deletion guards, so .env, data/, logs/, .venv/
and nginx/certs/ are still never touched. The cp fallback (used only when
rsync is missing) does NOT remove stale files — a warning now says so.
# Changelog — v4 Phase 4i (X button closes the only connection too)

Follow-up to Phase 4h. No schema change.

The CONNECTIONS sidebar's X (remove) button removed a card from
S.clusterList, but for the card representing the CURRENT connection it only
cleared S.conn.name. Since the sidebar re-adds a card for any S.conn with a
non-empty host, that card reappeared immediately — so pressing X on the only
(or current) connection appeared to do nothing.

Fix: when X is pressed on the current connection slot, S.conn is now fully
cleared (host:'', status idle, ver/tbl counters reset) — and the live session,
if any, is torn down via disconnectConn() first. The card now actually
disappears, even when it is the only connection. Removing a non-current card
is unchanged.

# Changelog — v4 Phase 4h (no phantom connection card for new users)

Follow-up to Phase 4e. No schema change.

Phase 4e's _resetConnectionState() reset S.conn.host to 'localhost'. The
CONNECTIONS sidebar renders a card for S.conn whenever S.conn.host is truthy,
so 'localhost' (a non-empty value) left a phantom "localhost:8123 / Not
connected" card sitting in the sidebar for a brand-new user who had never
created a connection — it looked like the previous user's connection was
still there.

Fix: _resetConnectionState() now resets S.conn.host to '' (empty), matching
what the "+ Add connection" flow already does. Empty host => no card => a new
user sees an empty CONNECTIONS panel with just "+ Add connection", as
expected.

# Changelog — v4 Phase 4g (hide soft-deleted users from the UI list)

Follow-up to Phase 4e. No schema change.

Phase 4e made user deletion a soft-delete (is_active=0) — the row stays in the
database, which is intended. But the Users panel still listed deactivated
users, so a "deleted" user kept showing in the UI. The console users table
now filters them out (cu.list.filter(u=>u.is_active!=0)): a soft-deleted user
disappears from the list just like a real delete, while the row — and its
audit history and logs — remain in the database.

Reactivating a soft-deleted user is still possible directly in the database:
  UPDATE users SET is_active=1 WHERE username='<name>';
# Changelog — v4 Phase 4f (password-change: reuse check + audit cluster)

Two fixes around password changes. No schema change.

## 1. Reject reusing the current password (English warning)

- Own password (Profile → Change Password): already rejected new == current.
  The message is now the clearer "New password must be different from the
  current password", and the rejection is now written to the audit log too
  (it previously returned silently, unlike the other rejection paths).
- Admin password reset (Users → Change PW): previously had NO such check — an
  admin could "reset" any account's password (including their own) to the
  value it already had. It now looks the user up, and if the new password
  verifies against their current hash it is rejected with
  "New password must be different from the current password" and an audit
  event with result=failed. Covers admins resetting other users AND
  themselves.

## 2. Password-change audit events now record the cluster

The UserPasswordChange / Change Own Password audit events showed
"Connected To: —" because they are logged server-side and were never given
any connection context. The UI now sends the active connection (host:port +
user, only when actually connected) with both password-change requests, and
the endpoints pass it through to audit() — so "Connected To" is populated
just like every other action.

# Changelog — v4 Phase 4e (soft-delete users + connection isolation)

Three fixes. No schema change.

## 1. User deletion is now a soft-delete

Deleting a user used to run DELETE FROM users — the row was physically
removed. It is now a soft-delete: UPDATE users SET is_active=0. The username,
role, audit history and per-user logs are all kept; the account simply can no
longer log in. An admin can reactivate it later by setting is_active back to 1.
Applies to BOTH the web UI (Users → Delete) and the CLI (app.py delete-user).
The deactivated user's Redis sessions are still revoked immediately, so a
soft-deleted account is signed out everywhere at once.

## 2. Per-user logs are left completely untouched on delete

The old code RENAMED logs/users/<username>/ to <username>.deleted/ on
deletion. Nothing was ever actually deleted, but the rename was confusing.
That block is now gone entirely: on a (soft-)delete the per-user log
directory logs/users/<username>/ and every activity/audit file inside it are
left exactly where they are — not renamed, not deleted.

## 3. ClickHouse connection no longer leaks between console users

Bug: logging in as a different user on the same browser (without a page
reload) inherited the previous user's active ClickHouse connection — it
"activated itself". doLogout() never cleared S.conn / S.connStatus, and
doLogin() actively re-adopted the old connection. Since the connection
payload (cP()) reads user/password from S.conn, the new user could even run
queries with the previous user's ClickHouse credentials — an isolation bug.

Fix: new _resetConnectionState() helper, called on BOTH login and logout. It
resets S.conn to defaults and clears S.connStatus, S.connVer, S.connTbl,
S.clusterList and S.clusters. The line in doLogin that re-adopted an existing
connection was removed. A login boundary now always means: no active
ClickHouse connection — the new user must connect with their own credentials.

# Changelog — v4 Phase 4d (update.sh translated to English)

Cosmetic only. `deploy/update.sh` was the last file with Turkish text in it;
all of its messages, comments and --help output are now in English, matching
the rest of the release (install.sh, INSTALLATION.md, CHANGELOG were already
English). No behaviour change — same flags, same steps, same exit codes.

# Changelog — v4 Phase 4c (query ekranı: otomatik yenileme + klavye)

İki UI düzeltmesi. Şema değişikliği yok, sadece static/index.html.

## 1. "Query ekranı sürekli yenileniyor" düzeltildi

Bug: nav menüsü, bir panelden çıkarken sadece Monitor ve Dashboard-widget
timer'larını durduruyordu. Mutations veya Rep. Queue panelinde "Auto 10s"
açıp Query ekranına geçersen `_mutationTimer` / `_repQTimer` çalışmaya devam
ediyordu — her 10 saniyede `render()` çağrılıyor, tüm uygulama (CodeMirror
editörü dahil) yeniden kuruluyordu. Yazarken imleç kayıyor, ekran "kendini
yeniliyor" gibi görünüyordu.

Düzeltme: yeni `stopAllAutoRefresh()` — bir panelden çıkıldığında HER panelin
otomatik yenileme timer'ını durduruyor (_monitorTimer, _mutationTimer,
_repQTimer, dashboard widget timer'ları, globalRefreshTimer). Nav menüsü artık
bunu çağırıyor. Otomatik yenilemeyi tekrar istiyorsan ilgili panelde "Auto"
düğmesine basman yeterli — ve artık sadece o paneldeyken çalışır.

## 2. Ctrl+A / Ctrl+Enter klavye davranışı

- Ctrl+A (Cmd+A) → editördeki tüm metni seçer.
- Ctrl+Enter (Cmd+Enter):
    - Tüm metin seçiliyse (Ctrl+A sonrası)  → Run All (her ifadeyi sırayla)
    - Bir kısım seçiliyse                   → o seçimi tek sorgu olarak çalıştırır
    - Hiçbir şey seçili değilse             → imlecin olduğu ifadeyi çalıştırır

Yeni `runFromEditor()` sarmalayıcısı bu kararı veriyor; "Run" ve "Run all"
düğmeleri eskisi gibi çalışmaya devam ediyor.

# Changelog — v4 Phase 4b (deploy/install scripts + data/ layout fix)

Phase 4'ün üstüne üç küçük düzenleme. Şema değişikliği yok.

## 1. Ayrı kurulum / güncelleme script'leri

İki net, ayrı script:

- `deploy/install.sh`  — İLK kurulum (eski adı install-phase3.sh; yeniden
  adlandırıldı). chconsole kullanıcısı + virtualenv + systemd unit + nginx +
  self-signed sertifika oluşturur. Servisi başlatmaz — .env'i gözden geçirip
  sen başlatırsın.
- `deploy/update.sh`   — SADECE güncelleme. Tek komut:

      sudo bash /opt/clickhouse-console/deploy/update.sh /root/yeni-surum.zip

  ZIP'i geçici dizine açar → /opt/clickhouse-console'a rsync'ler (.env, data/,
  logs/, .venv/, nginx/certs/ HARİÇ) → sahipliği düzeltir → Python
  bağımlılıklarını tazeler → servisi yeniden başlatır → geçici dizini siler.
  /opt/clickhouse-console yoksa HATA verir ve install.sh'e yönlendirir —
  artık otomatik devretme yok, iki iş net ayrı. Senin /root altındaki açılım
  klasörüne dokunmaz.

## 2. data/ dizin yapısı düzeltildi

Bug: kullanıcı oluşturulduğunda kod yanlışlıkla BOŞ bir `data/<username>/`
klasörü açıyordu (örn. `data/cansayin/`). Audit logları aslında hep doğru
yerdeydi — `logs/users/<username>/` altında — ama boş `data/<username>/`
klasörü "loglarım nerede" karışıklığına yol açıyordu.

Düzeltme: `_user_dir()` artık `logs/users/<username>/` döndürüyor (v4'teki
tek per-user dizin). Sonuç:
  - `data/`  → SADECE `global/` (master.key, instance.id, license.lic)
  - `logs/`  → `global/` (genel loglar) + `users/<username>/` (kişi audit logları)
CLI create-user mesajı ve kullanıcı silme (soft-delete) de buna göre düzeltildi.
Mevcut boş `data/<username>/` klasörlerini elle silebilirsin (rmdir).

## Dosya değişiklikleri
- deploy/update.sh ....... YENİ — tek komutluk kurulum/güncelleme
- app.py ................. _user_dir() → logs/users/ ; CLI mesajı + yorum düzeltildi

# Changelog — v4 Phase 4 (Bug fixes: async jobs + schema refresh)

Three user-reported bugs, all fixed. No schema change, no new operator
steps — drop-in over a Phase 3 install.

## Bug 1 — Run / Run All sometimes returns "not found"

**Cause.** Async job state (the SQL query editor's Run/Run All, and the
generic script-runner behind Backups/PITR/Branching/Profiler) was kept in
two in-process Python dicts (`_jobs`, `_query_jobs`). That was fine while
the app was a single process. Phase 3 switched to gunicorn with multiple
workers — and each worker has its OWN copy of those dicts. A query is
submitted to worker A, but the follow-up poll request is round-robined and
frequently lands on worker B, which has never heard of the job → the result
comes back as `not found`. The more workers, the more often it happens.

**Fix.** New `job_store.py` keeps job state in Redis (the same Redis already
used for sessions — one connection pool, one set of `REDIS_*` vars). Every
gunicorn worker shares it, so the poll resolves correctly no matter which
worker started the job. Keys carry a TTL (query jobs 1h, generic jobs 6h)
so finished jobs do not accumulate.
- `_query_jobs` → Redis `qjob:<jid>` (JSON string).
- `_jobs` → Redis `job:<jid>` HASH + `job:<jid>:lines` LIST. `RPUSH` for
  line output is atomic, so a streaming subprocess and a concurrent cancel
  never corrupt the output.
- `session_store.py` gains a public `get_client()` accessor so `job_store`
  reuses the one Redis client.
- Poll endpoints now return a clear HTTP 503 on a Redis outage instead of
  the misleading `not found`.

## Bug 2 — Run should execute the statement the cursor is in

Already implemented (`getActiveSQL()` → `findStmtAtPos()` splits on `;` or a
blank line and picks the statement under the cursor). It only *looked*
broken because Bug 1's "not found" masked every result. Fixed by Bug 1 —
no code change needed. If text is selected, the selection runs; otherwise
the statement under the cursor runs; Run All runs every statement in order.

## Bug 3 — Refresh (↻) doesn't show newly created tables

**Cause.** The ↻ button in the query panel and the Schema panel only
re-fetched the **database list**. A table added to an already-expanded
database was never re-fetched, so it didn't appear until you collapsed and
re-expanded the database. (Not a caching bug and not "the screen keeps
refreshing" — the server reads `system.tables` live every time; the button
just didn't ask for the open database's tables.)

**Fix.** New `refreshQueryTree()` / `refreshSchemaTree()` handlers reload
the database list AND, if a database is currently expanded, reload its
table list too. An open table-detail view is preserved.

## Files changed
- `job_store.py` ......... NEW — Redis-backed async job storage
- `session_store.py` ..... added public `get_client()`
- `app.py` ............... `_jnew/_ja/_jdone/_run` + job/query poll+cancel
                            endpoints now use `job_store`; in-process dicts
                            removed
- `static/index.html` .... `refreshQueryTree` / `refreshSchemaTree` added
                            and wired to the ↻ buttons

# Changelog — v4 Phase 3 (Production hardening)

This phase makes the bare-metal deployment production-grade. No change to
application behavior or data — it is packaging, process management, and
deployment hygiene.

## What changed

### Process management
- **gunicorn is the default production server**, replacing the Flask dev
  server. New `run-prod.sh` (now a shipped, version-controlled file rather
  than generated by `install.sh`) starts gunicorn with env-configurable
  `GUNICORN_WORKERS` / `GUNICORN_THREADS` / `GUNICORN_BIND` / `GUNICORN_TIMEOUT`
  (defaults: 2 workers, 4 threads, `127.0.0.1:5000`, 120s). The import-time
  `init_db()` / `migrate_legacy_users()` are idempotent, so multiple
  workers are safe.
- **systemd unit** `deploy/clickhouse-console.service` — runs gunicorn as
  the unprivileged `chconsole` user with `Restart=always`,
  `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  and write access limited to `data/` and the log directories. Auto-starts
  on boot, auto-restarts on crash.
- **`deploy/install-phase3.sh`** — idempotent installer: creates the
  `chconsole` user, syncs the release to `/opt/clickhouse-console`, builds
  a virtualenv, installs the systemd unit and the nginx site, and
  generates a self-signed certificate. Never touches an existing `.env` or
  `data/`. Does not auto-start the service — the operator reviews `.env`
  first.

### TLS / reverse proxy
- **`nginx/clickhouse-console.site`** — bare-metal nginx config: TLS
  termination, HTTP→HTTPS redirect, security headers (HSTS, X-Frame-Options,
  X-Content-Type-Options, Referrer-Policy), proxy to local gunicorn.
  Distinct from `nginx/nginx.conf`, which remains the Docker-stack config.

### Audit hardening
- **`harden.sql`** — `REVOKE DELETE, UPDATE, TRUNCATE ON audit_events`
  from the app role. The application only INSERTs/SELECTs `audit_events`,
  so this makes the audit trail append-only at the database level even if
  the app role is compromised. Promoted from an optional note to a
  **required** post-install step in `INSTALLATION.md`.

### Fixes
- `db.py` now closes the psycopg connection pool at interpreter exit
  (`close_pool()` registered via `atexit`). Short-lived CLI commands
  (`list-users`, `reset-password`, …) no longer hang ~10s on exit with a
  `couldn't stop thread` warning. The long-running server path is
  unaffected.
- UI version label corrected from `3.1` to `4.0` (page title and footer);
  `app.py` startup banner, docstring, and `/api/admin/system/info`
  `version` field aligned to `4.0`.

### Documentation
- `INSTALLATION.md` §5.6/§5.7 rewritten around the shipped deploy assets
  and `install-phase3.sh`; §11.8 audit hardening promoted to a required
  step referencing `harden.sql`.

## Operator-visible behavior changes

- Production deployments now run under systemd as `chconsole` instead of a
  manual root-owned `python app.py`. The old manual process is left intact
  by the installer; retire it once the service is confirmed working.
- The console is served via gunicorn behind nginx with TLS, not the Flask
  dev server on plain HTTP.
- Out of scope (deliberately, as in the Phase 3 plan): Redis hardening
  (firewall, password strength, `protected-mode`) remains a separate task
  and is still recommended as a priority after Phase 2.

---

# Changelog — v4 Phase 2 (Redis-backed sessions)

This phase moves **sessions** out of PostgreSQL into Redis. Persistent state
(users, audit, connections, query history, favorites, credentials) stays in
Postgres exactly as in v4 Phase 1 — only session storage changed.

## What changed

### Storage
- New file `session_store.py` — Redis connection + all session primitives.
  Symmetric to `db.py`: `db.py` speaks Postgres, `session_store.py` speaks
  Redis. Audit logging and the `sessions.log` snapshot stay in `app.py`.
- Sessions are no longer a Postgres table. Data model in Redis:
  - `session:<token>` — HASH (`user_id`, `username`, `role`, `email`,
    `created_at`, `expires_at`, `ip`, `user_agent`); key TTL =
    `SESSION_TTL_DAYS`. Expiry is automatic — no cleanup job.
  - `user_sessions:<user_id>` — sorted set (member = token, score = expiry
    epoch), the index for "all of a user's sessions". Stale members are
    pruned lazily on read, so it never grows unbounded.
- `schema.sql` — the `sessions` table and its two indexes are removed.
  Existing installs drop the leftover table by hand once
  (`DROP TABLE IF EXISTS sessions;`).

### Application (`app.py`)
- `create_session` writes to Redis; identity (`username`/`role`/`email`) is
  denormalized into the session hash so the auth hot path needs no JOIN.
- `get_session_user` reads from Redis only. **Fails closed**: any Redis
  error is treated as unauthenticated rather than raising — a Redis outage
  signs everyone out instead of 500-ing the auth gate or letting requests
  through.
- `delete_session` deletes from Redis; `cleanup_expired_sessions` is now a
  no-op (TTL handles expiry).
- Multi-session paths reworked onto Redis: `console_profile` (own session
  list), `console_password_change` (revoke other sessions),
  `console_users_change_password`, `console_users_delete`, and the CLI
  `reset-password`.
- **Security:** because role is denormalized into the session hash,
  `console_users_set_role` and `console_users_delete` now revoke the
  affected user's sessions, so a role change or account deletion takes
  effect immediately instead of lingering until session expiry.
- Admin endpoints (`/api/admin/sessions`, `/api/admin/sessions/revoke`,
  `/api/admin/system/info`) and `_refresh_sessions_log` enumerate from
  Redis. `sessions.log` keeps its exact JSON-lines format.
- **Bug fix:** the Active Sessions panel and the admin revoke endpoint
  checked `cookies.get("session")`, but the session cookie is named
  `ch_session`. The `is_current` flag was always false and the
  self-revoke guard never fired. Both now use `SESSION_COOKIE_NAME`.

### Configuration
- New env vars: `REDIS_HOST` (default `192.168.105.4`), `REDIS_PORT`
  (default `6379`), `REDIS_PASSWORD` (default empty), `REDIS_DB` (default
  `0`).
- `requirements.txt` adds `redis>=5.0`.
- `docker-compose.yml` passes the `REDIS_*` vars through.

### Documentation
- `INSTALLATION.md` updated: Redis prerequisite, `.env` example, upgrade
  procedure (provision Redis, expect a one-time re-login, drop the old
  `sessions` table), and a backup note (Redis sessions are intentionally
  not backed up — they are reconstructible).

## Operator-visible behavior changes

- **The upgrade signs everyone out once.** Sessions are not migrated from
  Postgres to Redis; all users log in again after the update. Expected.
- **Redis is now a hard dependency.** If Redis is unreachable, no one can
  be authenticated (fail-closed). Persistent data is unaffected and the UI
  returns once Redis is back.
- Role changes and account deletions now revoke sessions immediately,
  rather than the previous "takes effect on next request / on expiry".

---

# Changelog — v4 (Postgres backend)

This release replaces the SQLite storage layer with PostgreSQL. The application
API and the on-disk activity log format are unchanged; only the persistence
layer was rebuilt. Customer deployments must point the new build at a
PostgreSQL 14+ database — see `INSTALLATION.md §1` (prerequisites) and `§4.2`
(Postgres setup) for the full procedure.

## What changed

### Storage
- Per-user `data/<username>/<username>.db` SQLite files **removed**. All
  per-user data (query history, favorites, encrypted ClickHouse credentials)
  now lives in shared Postgres tables scoped by `user_id`.
- Master `data/global/global.db` SQLite file **removed**. Users, sessions,
  connections, and the audit master are now in Postgres.
- Audit per-user mirror removed. The single master `audit_events` table is the
  source of truth.
- New file `db.py` — psycopg3 connection pool, `?` → `%s` placeholder
  translation, `datetime('now')` → `now()` translation, datetime → 'YYYY-MM-DD
  HH:MM:SS' string row factory (preserves the legacy comparison behavior of the
  rest of the codebase).
- New file `schema.sql` — idempotent DDL applied at startup via
  `db.apply_schema()`.

### Tables (all in one Postgres database)
- `users`, `sessions`, `connections` — shared, same shape as before.
- `audit_events` — single master, with `ON DELETE SET NULL` from `user_id` so
  deleting a user doesn't lose audit history.
- `query_history`, `query_favorites`, `user_credentials` — per-user tables now
  carry a `user_id` column and `ON DELETE CASCADE` from `users`. Composite
  keys: `query_favorites` is `(user_id, conn_label, name)`; `user_credentials`
  is `(user_id, connection_id)`.

### Application
- `app.py`:
  - All `sqlite3.connect(...)` call sites removed. CLI commands and helpers
    use `_dbmod.get_connection()` from the new pool.
  - All per-user table queries now include `user_id` in WHERE/INSERT.
  - `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`.
  - 3 `IntegrityError` catch sites now also call `rollback()` (Postgres
    aborts the transaction on integrity violations; rollback is required
    before the connection is reusable).
  - Flask `teardown_appcontext` rolls back any uncommitted state before
    returning the per-request connection to the pool.
  - Startup banner now shows `postgres://user@host:port/db` (password
    redacted) instead of the vestigial `data/global/global.db` path.
  - `COALESCE(timestamptz_col, '')` replaced with
    `COALESCE(to_char(col, 'YYYY-MM-DD HH24:MI:SS'), '')` in `list-users` CLI
    and `console_users_list` API (Postgres won't cast `''` to TIMESTAMPTZ).
  - `export-audit --month YYYY-MM` now uses `to_char(ts, 'YYYY-MM') = ?`
    instead of `ts LIKE 'YYYY-MM-%'` (LIKE doesn't apply to TIMESTAMPTZ).
- `requirements.txt` adds `psycopg[binary]>=3.1` and `psycopg_pool>=3.2`.

### Configuration
- New env vars: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`,
  `DB_POOL_MIN` (default 2), `DB_POOL_MAX` (default 20). Required at startup.
- `docker-compose.yml` updated: vestigial `APP_DB` removed, `DB_*` env vars
  added, comment makes clear Postgres is brought separately.

### Documentation
- `INSTALLATION.md` rewritten end-to-end for Postgres: prerequisites,
  architecture, file layout, Docker and bare-metal Postgres setup, `.env`
  example, backup with `pg_dump` (replacing the SQLite `tar` strategy),
  troubleshooting (`psycopg.OperationalError`, `UnicodeEncodeError` on
  `SQL_ASCII` databases), and a hardening note about revoking
  `DELETE`/`UPDATE` on `audit_events`.

## Operator-visible behavior changes

- **Backup workflow** is now two-part: `pg_dump` for the database, plus a
  `tar` of `data/` for the secrets (`master.key`, `instance.id`,
  `license.lic`) and activity log files. Both halves must be backed up
  together — losing `master.key` makes every stored ClickHouse password
  unrecoverable. See `INSTALLATION.md §9`.
- **No automatic 3.x → 4.x data migration.** Old SQLite installs should be
  retired and rebuilt against a fresh Postgres database. User accounts are
  recreated via `python app.py create-user`. The previous SQLite files
  should be archived for the historical audit trail.
- **Audit hardening.** The application never issues `DELETE` or `UPDATE` on
  `audit_events`. Defense in depth: revoke those grants from the
  application role after first startup
  (`REVOKE DELETE, UPDATE, TRUNCATE ON audit_events FROM <role>`).

## Compatibility

- Tested against PostgreSQL 16; the schema and SQL also work on 14 and 15.
- Database encoding must be UTF-8 (`schema.sql` contains UTF-8 characters in
  comments). `SQL_ASCII` databases produce `UnicodeEncodeError` on first
  start — recreate the database with `ENCODING 'UTF8' TEMPLATE template0`.
