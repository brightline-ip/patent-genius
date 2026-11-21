# Keeping the data directory small

Patent Genius is designed so that a research data directory does not grow without bound. Heavy originals are
downloaded, normalized to compact text, and then deleted; anything needed later is downloaded again.

All paths below are relative to your data folder (`--data-dir`, else `PG_TOOL_DATA_DIR`, else the per-user default; see the README).
None of these folders ship with Patent Genius, and none is ever created inside the install folder.

| Original | Compact form kept | Pruned by |
|---|---|---|
| Prosecution PDFs, `public-originals/<app>/*.pdf` | `corpus/normalized/<app>/<doc>.md`: page-anchored OCR text with front matter (original SHA-256, source locator, OCR run, per-page status and confidence); `index.json` binds the Markdown hash to the original hash | `python compact.py prune-originals <app_dir> [--apply]` |
| USPTO XML archives, `public-originals/<app>/*.tar` | One Markdown file per XML part, with an explicit `component` (claims, specification, remarks, office action ...) | `python compact.py normalize-xml <app_dir>`, then `prune-originals` |
| Search pages, `public-originals/frames/<frame>/<query>/page-*.json` | The frame file (bibliographic rows, page hashes) plus `corpus/frame-events/<frame>.jsonl.gz` | `python compact.py compact-frame <frame.json> [--apply]` |

Safeguards:

- Pruning is a dry run unless `--apply` is given.
- It refuses any original whose bytes differ from the hash the Markdown was made from, or whose Markdown was edited.
- It writes a tombstone (hash, byte count, source locator, a command to re-acquire) before deleting.
- OCR text runs are kept, because the evidence manifest binds to them.

Limits:

- Markdown produced by OCR is a derivative, not the official record. Check quotations against the file wrapper.
- A pruned original cannot be re-inspected (layout, images, OCR errors) until it is downloaded again.
- Provider search pages are not byte-reproducible; a re-fetched frame page is new evidence, not a restored one.
