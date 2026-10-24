# drift-cli — agent skill

## The rule
**Never claim "nothing changed" about a page you have not diffed,
and never re-read a whole page when a drift report would do.**

Agents have no memory of the web. A docs page changed, a pricing
page moved, a model list grew — the agent keeps working from
yesterday's version and finds out late, or never. drift-cli gives
the agent a memory: snapshot a page once, then ask what actually
changed and get back only the changes, with the meaning attached
(a price line shown as an old → new pair), not a wall of HTML.

## When to use it
- A task depends on an external page staying true: API model
  lists, docs, pricing, changelogs, status pages, a client's site,
  your own deployed sites.
- Before relying on a page you last read in an earlier session:
  `check` it first. A cheap diff beats a stale assumption.
- On a schedule (e.g. a morning pass): watch the pages you depend
  on once, then `check` all of them in one run.

## Commands
```bash
python3 drift.py watch <url>            # snapshot the page's clean text
python3 drift.py check [<url>]          # diff vs snapshot -> drift-report.md
python3 drift.py check --json           # machine-readable verdict
python3 drift.py list                   # what is being watched
python3 drift.py forget <url>           # stop watching a page
python3 drift.py mcp                    # stdio MCP server
```

Snapshots live in `~/.drift-cli/` (override with `DRIFT_HOME`),
one JSON file per URL, mode 600.

## Reading a check
- Exit **0 = NO CHANGE** — the page is what it was. Say exactly
  that; do not summarize a page you did not re-read.
- Exit **1 = CHANGED** — read `drift-report.md`. It lists, per URL:
  **Changed** pairs (`- old` / `+ new` for lines that clearly
  evolved, e.g. a price), **Added** lines, **Removed** lines.
  Act on the diff, not on a fresh full read, unless the diff
  says the page was restructured wholesale.
- Exit **2 = ERROR** — fetch failed or the URL was never watched.
  Report the error as-is; never invent a diff.

## What is filtered, what is not
- **Noise, filtered before the diff:** ISO dates/times, relative
  times ("3 minutes ago"), UUIDs, long hex tokens (session ids,
  hashes), session/csrf-style query values. The report counts how
  many lines were touched by the filter on each fetch.
- **Never filtered:** ordinary numbers and prices. A price change
  is real drift and shows up as a Changed pair.
- **Secrets:** strings shaped like API keys (`sk-…`, `ghp_…`,
  `AKIA…`, …) are redacted BEFORE the snapshot is stored, so they
  cannot appear in any report. Query strings are stripped from
  URLs shown in reports.

## MCP
`drift mcp` runs a stdio MCP server with two tools:
`drift_watch` (`{url}`) and `drift_check` (`{url?}` — omit the URL
to check everything watched). The check tool returns the full
report text.

```json
{ "command": "python3", "args": ["/path/to/drift.py", "mcp"] }
```

## Honesty rules
- **NO CHANGE is a verdict, not a guess.** It means the cleaned,
  normalized text is byte-identical to the snapshot.
- **A page that needs JavaScript to render may snapshot as nearly
  empty.** drift-cli fetches static HTML only; if a watch returns
  suspiciously few lines, say so instead of trusting the diff.
