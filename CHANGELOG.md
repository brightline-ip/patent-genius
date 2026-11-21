# Changelog

## 0.1.0 - 2026-11-21 (UTC) (first public preview)

Patent Genius is an offline-first toolkit for reading USPTO office actions with fixed rules, building a local database from your own
frames and collected data, finding similar office actions in it, and assembling fact packs. It is a drafting and research aid for a registered
practitioner, not legal advice, and it uses no model.

**What is in it**

- Office-action parser 1.1: rejections, objections, restriction, finality, AIA status, header facts (art unit, examiner, mail date,
  application number) with how each was found, and cited references normalised to document numbers (a bare surname is never matched).
- Your own scope (`scope.json`) and data folder: technology fields, filing dates and document types are the user's, not the program's; frame
  tools (plan, search, build, select, compact), document download and normalisation, and the SQLite database (schema 2.3: every input optional,
  built to a new file and swapped in, read-only readers, skipped applications kept out of denominators, coverage report).
- `pg collect plan|run|status|probe`: office-action rejection rows for the applications in your own frames, through the Patent Research Hub
  (0.2.0 or later) gateway command, resumable, with a dry-run estimate first; behind an adapter (the USPTO API is marked Legacy).
- `oa_outcomes`: what happened after each action, from file-wrapper events, anchored on the action's mailing event at the record date (the action event within 14 days before when there is none), events sorted by date with same-date events
  simultaneous (the provider's array order is not used), a rescinded abandonment skipped, with code lists as data
  checked against the official USPTO PatEx 2022 event-code list (`pg_tool/data/uspto_event_codes.csv`; the role mapping is the tool's own).
- `pg similar` / `pg_similar_office_actions`: a published, versioned, user-overridable weighted similarity (missing features score 0, ranked by
  coverage tier, document numbers only, own family left out), with counts and intervals, one case per family, open cases apart, and applicant
  steps kept apart from outcomes; `min_n` is never below 5.
- `pg pack` / `pg_response_pack` and `pg shell --from-pack`: typed facts, issues, authority pointers (`authorities.json`, checked against the MPEP
  Ninth Edition Revision 01.2024 and the eCFR of 2026-10-27, needs owner review), counts and a fact-filled shell. No tool-authored argument text;
  an optional fenced, capped `untrusted_quote`.
- `pg_db_report` and eleven MCP tools in all (contract 1.1.0); text copied from documents into the tool's own fields is cleaned (control, bidirectional and invisible characters) and every result that can carry
  document text says it is untrusted data; `pg_corpus` returns local document text raw, with the caveat.
- Windows installer (`Setup.cmd`): per user, the Hub command chosen at Setup is kept in `installer.json` for Repair and updates (an explicit `-HubCmd` overrides it), a separate user-only data folder, Python 3.10+ (required), Node.js 24.13+ and an optional hash-pinned
  PyMuPDF `.venv` offered only on a yes, the Hub command recorded as `PG_HUB_CMD`, `PatentGenius.cmd`, MCP setup files without keys, five shortcuts,
  Repair and an Uninstall that keeps your data unless you say otherwise. `tools/build_zip.py` builds the release zip from the committed allowlist.

**Boundaries**

- A path with a `:` beyond the drive letter (an NTFS alternate data stream such as `file.txt:name`) is refused wherever a user-supplied path is read or written
  (MCP tools, command line, Python and Node scripts). Any `.txt` or `.md` file inside an allowed input folder is readable by the MCP client: keep secrets out of those folders.
- Providers are reached only through the Hub's gateway command, started as a short-lived child per call; no Patent Genius process holds a key or
  imports a Hub module. No MCP tool makes a provider call. Live provider search is planned, not part of 0.1.0.
- Nothing read from caller-supplied text is stored, and no provider or network query is made from it (local read-only database queries only).
- Collection and the Hub gateway were tested against a stand-in for the Hub; the Hub's direct transport, a provider 429 and the citations API
  were not exercised live. Windows installation was tested under Windows PowerShell 5.1 into scratch folders; a clean-machine install was not.
