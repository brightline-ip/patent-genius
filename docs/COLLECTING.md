# Collecting office-action rejection rows

`python pg_cli.py collect ...` fetches, for the applications in **your own frames**, the USPTO's office-action rejection rows
(one row per paragraph of an action: the statute, whether it was a rejection, the art unit, citation counts). It needs the
[Patent Research Hub](HUB.md) 0.2.0 or later with your own USPTO key. Nothing is collected unless you run `collect run --yes`.

## Before you start (USPTO rules, read 2026-11-20 at data.uspto.gov; they can change)

- An Open Data Portal API key needs a USPTO.gov account with multi-factor sign-in and an **ID.me-verified identity**. People
  outside the US verify on a video call. One key per account; do not share it.
- **A key not used for 90 days is deleted** and must be requested again. The portal also deactivated keys of accounts that did
  not complete extra profile fields by 2026-10-08; they are reactivated in the account settings.
- The USPTO's terms of use and privacy policy apply to the portal APIs.
- The portal's published limits: **one call at a time per key**, 4 to 15 requests per second depending on the call type, weekly
  quotas far above what a research collection needs, and on HTTP 429 *never retry without at least a 5 second delay*. The limits
  are "subject to change". Patent Genius states no other figure.
- The USPTO marks this rejections API (v2) **Legacy**. Its successor, the Office Action Search API, may carry the same
  fields; that was not verifiable without signing in, and it is not implemented. The collector is built behind an adapter
  (`pg_tool/oa_adapter.py`) so a move is a contained change.
- Licence of the data: Creative Commons Public Domain Mark 1.0 (per the data.gov catalogue entry). No money cost.

## What a small read-only probe saw (2026-11-20, five calls, through a Hub gateway)

- The field list has 31 fields, including `hasRej101 hasRej102 hasRej103 hasRej112 hasRejDP`, `legalSectionCode`,
  `actionTypeCategory`, `paragraphNumber`, `groupArtUnitNumber`, `submissionDate`, citation counts. **No examiner name and no
  identity of cited references**: references come from parsing full-text actions you collected, and the examiner from the
  application record in your frames.
- `legalSectionCode` was "102", "103", "112", "Other" or empty. **There are no 112(a)/(b) subsections**: everything is read at
  statute level, and the database says so.
- `actionTypeCategory` was rejected, objected, allowed, cancelled or empty. Only rows labelled `rejected` are rejections. Other
  rows are kept and labelled (`row_kind`).
- An application with no actions returned a normal answer with zero records; whether the Hub's *direct* transport answers the
  same way was **not verified live**. An application filed in 2014 had actions from 2014-11 to 2015-03, so coverage is not
  limited to recent years; **coverage is computed from your data**, never assumed. The newest row seen was about six months old.
- Not probed: the direct transport (needs a key), a 429, the citations API, the bulk file. About 43 and 64 rows were seen for two
  applications; the estimates assume about 30 per application and say so.

## Commands

```
python pg_cli.py collect plan              applications, requests, time and disk: reads the Hub's own limits, sends nothing
python pg_cli.py collect run --yes         collect (without --yes it only shows the plan)
python pg_cli.py collect status            progress per frame and recent runs
python pg_cli.py collect probe [--application 17000001]   the provider field list (and one application of yours)
```

Options: `--frame <id>` (repeatable), `--max-requests N`, `--restart` (discard saved progress), `--max-wait SECONDS` (the longest
single wait for a Hub limit; default 3600). `scope.json` must list `oa_rejections` in `data_types`, and `limits.max_applications`
and `limits.max_requests_per_run` cap a run (the smaller request cap wins).

## What it sends and what it does

- Only application numbers of eligible rows in `corpus/frames/*.json.gz`, de-duplicated, in batches of 50 (pages of 100 rows) to
  the Hub's `oa-rejections` route. Numbers that are not eight digits and applications beyond your cap are listed in
  `corpus/oa-rejections/<frame>.skipped.json` and are **not sent**; the database excludes them from every denominator.
- **Exit 75 from the Hub** (a local limit): wait `retry_after_seconds`, then go on. A wait longer than `--max-wait` stops the
  run (resume later). **A provider 429**: wait at least 5 seconds (longer if told), doubling, at most five times in a row.
  **Any other 4xx** stops the run at once (a 404 is not read as "no records"). **Three failures in a row** (an outage) stop it.
  An answer that is not the documented shape stops it.
- Progress is saved after every batch in files that are replaced whole (`collect/oa-rejections/<frame>.progress.json` plus a
  rows file); stop it any time and start it again: the finished batches are not asked for twice, and anything half-written
  after the last saved batch is cut off. One collector at a time per data folder (`locks/collect-oa.lock`; a crashed run's lock is taken over after about a minute, and the message names the file if it is not).
- Output: `corpus/oa-rejections/<frame>.jsonl.gz` (rows exactly as sent; it appears only when the frame is complete),
  `<frame>.collected.json` (counts, row kinds, date range, which applications had no rows) and the run record in
  `collect/runs.jsonl` (counts and ids only; no application number, no text). `python pg_cli.py db build` loads them.

## Reading the data honestly

An application with no rows may simply have no action in the source yet, or acted before the source's coverage begins: that is
not a finding. The coverage report (`python pg_cli.py db report coverage`) shows the dates the data actually spans, how many
applications have rows, the row kinds, the match rate to full-text documents and, per statute, how often the rule-based parser
and the provider agree.
