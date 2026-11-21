# Similar office actions

`python pg_cli.py similar <office-action.txt>` (MCP tool `pg_similar_office_actions`) ranks the office actions in **your own
database** by a published weighted sum, and summarises where the matches ended up. It is deterministic, offline and read-only: no
model, no network, nothing written. It needs a database (`python pg_cli.py db build`) built from your frames and collected
rows ([COLLECTING.md](COLLECTING.md)); without one it says so.

**Similar by these features only. This is not semantic or legal similarity, and it predicts nothing about your case.**

## The formula

`score = (sum of weight x value over the seven components) / (sum of all weights)`. A component that is missing on either
side scores 0 and does not count towards `coverage`; it is never averaged away. Results are ranked by **coverage tier first**
(full 0.85 and above, high 0.60, partial 0.30, thin below), then by score, then by a fixed tie-break.

| Component | Default weight | Value |
|---|---|---|
| `statute_set` | 0.35 | Jaccard overlap of the statutes rejected (101, 102, 103, 112, dp) |
| `references` | 0.20 | Jaccard overlap of cited **normalised document numbers** (a surname is never matched; the candidate must have a parsed full-text action linked to it) |
| `examiner` | 0.15 | same examiner, compared as written after trimming and upper-casing: the examiner **named on the application record now**, who may not be the one who wrote the action |
| `art_unit` | 0.10 | 1 same art unit, 0.5 same work group (first three digits) |
| `cpc` | 0.10 | 1 same main group, 0.5 same subclass (the first CPC symbol of the application) |
| `ground_101` | 0.05 | 1 when both are 101 rejections and both are flagged Alice or Mayo (the provider's flags for the candidate; the word in the text for a query) |
| `finality` | 0.05 | 1 when both are final or both non-final |

The weights were set by judgement, before any result was seen; they are **not fitted to any data**. They are versioned and
dated in `pg_tool/similarity.json` (version 1.0, 2026-11-20). Override some of them in `<data folder>/similarity.json`
(`{"weights": {"examiner": 0.3}}`) or for one call (`--weights '{"cpc": 0}'`). Overrides are validated (known names, numbers
from 0 to 1, at least one above 0) and the weights used, their source and the file's SHA-256 are echoed in every result.

## The query

An office action as text (`file`), or `--features '{"statutes": ["103"], "references": ["US20190000001"], "art_unit": "1234"}'`,
or an application number (`--application`, which also fills art unit, examiner and CPC from your database). Read from text, the
features are rule-extracted and unverified, and each carries how it was found. The query's own application (named, or read from
the text) and its whole patent family are left out of the results.

## What comes back

- `candidates` (up to `--limit`, at most 50): each with its seven components (available or not, value, weight, a short reason), score,
  coverage, tier and what happened to that application.
- `outcome_summary`, over **all** matches at or above `--min-score` (not only the listed ones), one case per patent family:
  allowed (granted, or allowed and not yet granted) and abandoned, with 95% Wilson intervals; **open cases are reported separately**
  because they have no result yet. A share is not shown when fewer than `--min-n` families are resolved. **`min_n` is never below 5** (the finder, the pack and `pg_trends` refuse a smaller value over MCP and on the command line; code that bypasses the contract gets 5 and a warning). "As of" is the freeze date of your frames, not today: the result gives the range (`as_of_from` to `as_of_to`) of the freeze dates of the matched families' frames. **Abandoned includes abandonment in favour of a continuation**, the unit is one application per patent family, and shares use resolved families only, so they favour applications that resolved quickly: read them as a description of resolved cases, not a rate for new ones.
- `applicant_action_labels` (transaction codes are checked against the USPTO PatEx 2022 list shipped as `pg_tool/data/uspto_event_codes.csv`, whose descriptions are the USPTO's; which codes count as an interview, an abandonment, an amendment and so on is the tool's own mapping in `pg_tool/event_codes.json`. Only actions anchored on their own mailing event (or, failing that, their own action event) are used - see "How an action is anchored" below - and "no later event" is kept apart from "later events, none recognised"; abandonment for a continuation and pre-exam abandonment are labelled apart, ABN9 is not an abandonment, and a rescinded abandonment is skipped, not final): what was done next after the matched actions (interview, amendment, RCE or appeal, the next event). The interview and amendment shares use the actions whose next event is recognised as the denominator, because for an action with none an interview or an amendment may still come; the count of such actions is given.
  These describe steps from the file-wrapper events, **not what any argument said**, are kept apart from the outcomes and never
  cross-tabulated with them, and show no cause.
- `coverage`: the first and last action dates in your data (office actions before the start are missing, so a family's earlier
  history may be incomplete), applications left out as skipped, how many actions have parsed references, and the pool size (capped
  at 20,000; when the cap is hit the result carries a warning and its status is `partial`, because the summaries then describe the capped pool).

## How an action is anchored, and what "next" means

In real collected data the date of a rejection record is the date of the **mailing** event (MCTNF, MCTFR, MCTRS); the action event itself (CTNF,
CTFR, CTRS) is usually 1 to 9 days or more earlier. So each action is anchored on its mailing event at the record date (`mail_exact`, or
`mail_near` within 1 day) and, when there is none, on the action event within 14 days before to 1 day after that date (`ct_near`). With neither
the action is `unanchored` and nothing is claimed about what followed; the coverage report lists those actions. The paired action event and
the mailing event belong to the action and are never "the next event".

The provider's array order is not chronological and is **never used**. Events are sorted by date, and events on the same date are
simultaneous. Events on the anchor's own date are not counted as later. The next turning point (a later Office action, an allowance, an
abandonment, an RCE or an appeal) is the first date that has one; an interview or an amendment on that same date counts as *before* it (the
rule is inclusive). When several turning events share that date a fixed role precedence picks one: RCE, appeal, allowance, abandonment for a
continuation, abandonment, then Office actions, and within a role the code in alphabetical order (the order is this tool's convention, not
a statement about what happened first).

A **rescinded abandonment** is not final: when an abandonment is followed by a rescission code (for example NRAB), the abandonment is skipped
and the search goes on to the next turning point after the rescission (`abandonment_rescinded = 1`); with none the case shows later events
with none recognised.

Candidates are chosen in SQL by sharing at least one statute with the query, then scored in Python. Caller text is kept in memory only.
