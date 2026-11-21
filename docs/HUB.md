# Patent Research Hub integration (optional)

Patent Genius never calls the USPTO, PTAB or GovInfo itself and never holds a provider key. Live access is provided by the
separately published **Patent Research Hub** (Brightline IP), which holds your keys. Without it, Patent Genius still works
offline on files you already have.

## How Patent Genius reaches the Hub: the gateway command

For collection (office-action rejections and the frame harvest scripts) Patent Genius starts the Hub's gateway command as a
short-lived child process, once per provider call:

```
"<Hub install>\Hub.cmd" gateway <route> --json-in      one JSON request on stdin, one JSON document on stdout
"<Hub install>\Hub.cmd" gateway --limits               the Hub's effective limits (reads configuration only; no key, no request)
```

- The Hub's launcher loads `keys.env` for that child only. Patent Genius **never reads `keys.env`**, never imports Hub
  modules for these calls, and the Python tool service never holds a key.
- Needs **Hub 0.2.0 or later** (the build that has `gateway --limits`). An older Hub rejects the unknown option with a
  non-zero exit and no `error` object; Patent Genius reports that as `hub_too_old`.
- Exit codes: 0 ok (a provider status such as 404 or 429 is *returned* in the answer), 1 failed, 2 not configured (the key is
  missing; nothing was sent), 64 bad request, 75 local limit or busy (wait `retry_after_seconds`, then ask again). The code `local_state` (the Hub's limiter state could not be read or saved) is a stop, not a retry. Tests point `PATENT_RESEARCH_LIMITER_DIR` at a throwaway folder.
- A call that times out stops the whole process tree (the `.cmd` and its Node child) and is reported as "the call may have
  happened". Large answers (document PDFs) are read in full.
- The Hub keeps its own limiter (one call at a time per key, spacing, an hourly cap, a pause after a 429). Patent Genius adds
  no pacing of its own. See [COLLECTING.md](COLLECTING.md).

### Configure

```
PG_HUB_CMD=<full path of Hub.cmd>            # the installer will record this; set it yourself until then
PG_HUB_ROOT=<the Hub's install folder>        # alternative: Hub.cmd is looked for inside it (alias PG_TOOL_HUB_ROOT)
```

If neither is set, the Hub installer's default folder (`%LOCALAPPDATA%\Brightline\PatentResearchHub\Hub.cmd`) is tried;
`PG_HUB_DISCOVER=0` switches that look-up off. `python pg_cli.py doctor` shows which command was found and the Hub version (it
runs `gateway --limits`, which makes no network call).

`FRAME_GATEWAY_MODULE` is no longer used. If it is set it is ignored with a warning, so no replacement module can stand in for
the Hub. (The tests use a separate stand-in setting that works only with `PG_ALLOW_TEST_STUB=1` and refuses to run if any
provider key variable is present in the environment.)

## What uses the Hub, and how

| Command / tool | Needs the Hub | How |
|---|---|---|
| `pg_cli.py collect plan\|run\|status\|probe` (office-action rejection rows) | yes (0.2.0+) | gateway command, route `oa-rejections` |
| `frame_search.mjs`, `frame_run.mjs`, `frame_dryrun.mjs` (harvest USPTO search pages) | yes (0.2.0+) | gateway command, route `uspto` |
| `acquire_round.mjs`, `acquire_family.mjs`, `acquire_supplement.mjs` (file-wrapper inventories and originals) | yes (0.2.0+) | gateway command, routes `application-documents` and `application-pdf` (`hub_documents.mjs`); an original can be tens of MB and is read in full, size- and signature-checked, and hash-verified when staged |
| everything else (assess, similar, pack, shells, reports, local data, every MCP tool) | no | |

**No Patent Genius process holds a provider key and nothing imports a Hub module** (`tests/test_documents.py` greps the code for it).
**Live provider search is planned and is not part of 0.1.0**: `pg_search` reads your own frames only, `pg_post_grant` your own data folder.

With no Hub configured every Node script that needs it prints a message and exits with code 78 (`frame_run.mjs` before it counts
or writes anything), and `collect` exits 78.

## Privacy

The gateway command writes nothing to the Hub's store (only limiter state). Collection sends only application numbers from **your own
frames**, and the downloaders only the application and document identifiers of your own selections; nothing read from an office action you gave
an agent is ever sent anywhere.
