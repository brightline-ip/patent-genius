# Frames: sets of USPTO applications

A **frame** is a frozen, hash-verified list of USPTO applications that match a CPC-based query over a filing-date
range. Frames are the population for any statistic Patent Genius reports, so the tool never reports a rate without
saying which frames it came from. Inside frame files a technology field is still called a *stratum*; the name comes from
your `scope.json` (`fields[].name`).

Only the harvesting steps (`frame_search`, `frame_run`, `frame_dryrun`, `acquire_*`) need network access, and they
need the optional Patent Research Hub (see [HUB.md](HUB.md)). Everything else works offline on files.

## Where things live

One data folder: `--data-dir`, else `PG_TOOL_DATA_DIR`, else the per-user default (see the README). Every path below is
inside it. The Python and Node scripts use the same folder, and all of them refuse a data folder inside the install folder.
Frames are built by **filing date**. A frame's technology field and filing window come from its own metadata, and each
row's year from its own `filing_date`, so a frame can have any valid id (lower case letters, digits, `_` and `-`, up to 64
characters, not a Windows device name such as `con` or `nul`) and may span several years.

## Workflow

```
python pg_cli.py scope init                                   # edit scope.json: fields, seeds, filing dates, document types
# Download the CPC Title List archive yourself from the CPC website (it cannot be fetched automatically) and save it in the data folder.
python cpc_expand.py <CPCTitleList.zip> <version> <source_url> scope.json corpus/cpc/<version>/expansions.json
python frame_plan.py <frame_id> <field> [<from> <to-exclusive> [days]]                # dates default to scope.json
node   frame_run.mjs <plan.json> <max_rows_per_query> <page_budget>                   # needs the Hub; exits 78 without it
python frame_build.py <plan.json>                                                     # writes corpus/frames/<frame_id>.json.gz
python frame_select.py corpus/frames/<frame_id>.json.gz <new_frame_id> <seed> <field=target> --out <file>
python compact.py compact-frame corpus/frames/<frame_id>.json.gz --apply
python pg_cli.py db build                                                             # every step optional
python frame_contracts.py examples/frame.json                                         # validate a frame
```

Relative paths in these commands are inside the data folder; pass `--data-dir <folder>` (or set `PG_TOOL_DATA_DIR`).

- **Seeds files.** `cpc_expand.py` reads either your `scope.json` (its `fields`) or a seeds file in one of two forms (see
  [`examples/seeds.example.json`](../examples/seeds.example.json)): `{"field": ["A01B", "A01C1/00"]}` or
  `{"field": {"seeds": ["G01N33/24"], "historical_prefixes": ["G01N33/"]}}`. A seed is a CPC subclass (`A01B`) or a group
  (`A01B1/00`); a historical prefix is a symbol retired from the current scheme that older filings may still carry.
- `frame_plan.py` splits the date range into fixed windows and turns the CPC expansion into provider wildcards. CPC
  values are stored right-aligned in a 4-character field by the USPTO Open Data Portal (`A01B   1/00`), so the wildcards
  are padded (`A01B???1*`); results are post-filtered against the exact expanded symbol set. The expansion file comes from
  `--expansion`, else `corpus/cpc/<version>/expansions.json` for the `cpc_title_list.version` in your scope, else the
  first expansion file that defines the field.
- `frame_run.mjs` runs every query of a plan through `frame_search.mjs` under a page (request) budget, keeps every raw
  page with its SHA-256, resumes with `FRAME_RESUME=1`, and refuses to overwrite an existing query folder.
- `frame_build.py` re-verifies every page hash, de-duplicates by application number, keeps the examiner name, and
  derives a continuity-based family key. `frame_select.py` draws a stratified random sample by family into a **new**
  frame; frames are never modified in place.
- `acquire_family.mjs` and `acquire_supplement.mjs` download documents. Which document types is your choice:
  `--preset office-actions|prosecution|procedural`, or `--codes CTNF,CTFR`, or `documents` in `scope.json`. The default
  preset is `none`: nothing is requested and the script exits 64. `acquire_supplement.mjs` needs documents already
  normalized under `corpus/normalized/` and exits 66 with a message when there are none.
- An optional request-budget ledger (`request_budget.mjs`) is OFF by default. Set `PG_BUDGET_FRAMES` to a regular
  expression to protect matching frame ids: every provider request must then be reserved in the append-only ledger
  `PG_BUDGET_LEDGER` (create it with `node request_budget.mjs init <ledger> <cap> <baseline> <approval note>`), under a
  lock stamp (`PG_BUDGET_LOCK_STAMP`, lock file `PG_BUDGET_LOCK_PATH`, default `<data folder>/locks/harvest.lock`).

## The database

`python pg_cli.py db build` writes `<data folder>/corpus/pg.db` from whatever is in the data folder. Every input is
optional: a missing dataset is recorded and `python pg_cli.py db report coverage` says *not collected*. The build writes a
new file and swaps it into place (a reader is never disturbed and a failed build leaves the old file alone), and readers
open the file read-only. Applications that cannot be used (a malformed row, an office-action row for an application in
none of your frames, an application a collector marked as skipped) are listed in `skipped_applications` and left out of
every denominator. `--years` defaults to every filing year in the database; pass `YYYY` or `YYYY-YYYY` to restrict.

Inputs it reads, all optional:

```
scope.json                                   your scope (kept in the database with its SHA-256)
corpus/frames/<frame_id>.json.gz             frozen frames
corpus/frame-events/<frame_id>.jsonl.gz      file-wrapper events (from compact.py compact-frame)
corpus/oa-rejections/<frame_id>.jsonl.gz     office-action rejection rows, one JSON object per line (see below)
corpus/oa-rejections/<frame_id>.skipped.json {"applications": [{"application": "...", "reason": "..."}]} written by a collector
corpus/normalized/<app>/index.json           normalized full-text documents
corpus/ptab/trials.jsonl.gz, appeals*.jsonl.gz   PTAB records you collected yourself
```

Office-action rejection rows are read with the USPTO field names (`patentApplicationNumber`, `obsoleteDocumentIdentifier`,
`legacyDocumentCodeIdentifier`, `submissionDate`, `legalSectionCode`, `actionTypeCategory`, `claimNumberArrayDocument`,
`groupArtUnitNumber`, `paragraphNumber`, `cite102Gt1`, `cite103Gt3`, `cite103EqOne`, `cite103Max`, the Alice/Mayo/Bilski/Myriad
indicators; the field list is also read through `pg collect probe`). A missing field is stored as empty. The names were seen in a small
read-only probe of the service on 2026-11-20; the Hub's direct transport has not been run live. `pg collect` writes these files (see
COLLECTING.md). There is no collector for the PTAB and Federal Circuit files below: they are read only if you supply them.

## Data layout

```
scope.json                               your scope
corpus/cpc/<version>/expansions.json     CPC expansion files
corpus/frame-plans/<frame_id>.json       plans
corpus/frames/<frame_id>.json.gz         frozen frames
corpus/pg.db                             the database (a derived file: delete it and rebuild)
public-originals/frames/<frame>/<query>/ raw search pages and receipts
corpus/normalized/<app>/                 compact Markdown of file-wrapper documents
```

The USPTO Open Data Portal needs your own API key and has terms of use. Patent Genius itself holds no key; the Hub does.
Check the terms at <https://data.uspto.gov/> before harvesting.

## Script configuration

`PG_TOOL_DATA_DIR` (or `--data-dir` on the Python scripts) sets the data folder. `PG_PYTHON` selects the Python
executable for scripts that start Python (default `python3`, or `python` on Windows).
