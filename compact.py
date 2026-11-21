"""Keep the corpus small: normalize heavy originals to compact text, then prune the originals.
Pruning is dry-run unless --apply, refuses anything without a verified compact form, and leaves a
tombstone (hash, bytes, source locator, re-acquire command) so an original can be fetched again.

  python compact.py normalize <app_dir> <ocr_run_id>          -> corpus/normalized/<app>/<doc>.md + index.json
  python compact.py normalize-xml <app_dir>                   -> USPTO xmlarchive <doc>.tar -> Markdown (supersedes OCR)
  python compact.py prune-originals <app_dir> [--apply]       -> deletes PDFs/TARs that have verified Markdown
  python compact.py compact-frame <frame.json> [--apply]      -> corpus/frame-events/<frame_id>.jsonl, deletes raw pages

Re-acquired bytes are checked against the tombstone hash; ODP search pages are NOT byte-reproducible
(provider data changes), so a re-fetched frame page is new evidence, not a restored one."""
import gzip, hashlib, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
from pg_tool import fsutil, paths

ROOT = paths.resolve_data_dir()  # the data folder; every path below is inside it
sha = lambda b: hashlib.sha256(b).hexdigest()
now = lambda: datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _write_new(path, text):
    """Write once; '.gz' paths are gzip-compressed with a fixed mtime. Identical content may be rewritten, different content never."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode('utf-8')
    if path.exists():
        old = path.read_bytes()
        if (gzip.decompress(old) if path.suffix == '.gz' else old) != data: raise SystemExit(f'refusing to overwrite different content at {path}')
    path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == '.gz' else data)


def _manifest_docs(app_dir=None):
    """Document type + source locator by document id: the reviewed manifest first, else the app's inventory.json."""
    mp = ROOT / 'corpus' / 'acquired-public-manifest.json'  # optional: a reviewed manifest you may have written; most users have none
    m = json.loads(mp.read_text(encoding='utf-8')) if mp.exists() else {'documents': []}
    docs = {d['id'].split(':', 1)[1]: d for d in m['documents'] if d['id'].startswith('public:')}
    inv = Path(app_dir or '') / 'inventory.json'
    if app_dir and inv.exists():
        for d in json.loads(inv.read_text(encoding='utf-8')).get('documents', []):
            docs.setdefault(d['document_id'], {'document_type': d.get('document_code'), 'source_locator': d.get('url'), 'official_date': d.get('official_date'), 'file_id': d.get('file_id'), 'docx_file_id': d.get('docx_file_id')})
    return docs


def normalize(app_dir, run_id, root=ROOT):
    app_dir = Path(app_dir); app = app_dir.name
    run = app_dir / 'ocr' / run_id
    out = root / 'corpus' / 'normalized' / app
    docs = _manifest_docs(app_dir) if root == ROOT else {}
    idx_path = out / 'index.json'
    index = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {'application': app, 'documents': {}}
    failed, ok = {}, []
    for pj in sorted(run.glob('*.pages.json')):
        try:
            meta = json.loads(pj.read_text(encoding='utf-8'))
            doc = meta['document_id']
            if index['documents'].get(doc, {}).get('source') in ('uspto-xmlarchive', 'uspto-docx'): ok.append(doc); continue  # XML/DOCX rendering supersedes OCR
            raw = (run / (doc + '.txt')).read_bytes()
            if sha(raw) != meta['text_sha256']: raise ValueError('OCR text hash mismatch')
            pages = raw.decode('utf-8').split('\f')
            if len(pages) != len(meta['pages']): raise ValueError('page count mismatch')
        except Exception as e: failed[pj.name.split('.')[0]] = f'{type(e).__name__}: {str(e)[:160]}'; continue
        d = docs.get(doc, {})
        from xml_to_md import _front   # JSON-escaped values: a quote or line break in a value cannot break out of its line
        head = _front({'application': app, 'document_id': doc, 'document_type': d.get('document_type', 'unknown'), 'original_sha256': meta['original_sha256'],
                       'source_locator': d.get('source_locator', 'unknown'), 'ocr_run': run_id, 'ocr_text_sha256': meta['text_sha256'], 'pages': len(pages),
                       'derivative': 'OCR text of a scanned original; not the official record'})
        body = []
        for p, text in zip(meta['pages'], pages):
            body += [f'## Page {p["page"]}', f'<!-- status={p["status"]} mean_confidence={p.get("mean_confidence")} low_confidence_words={p.get("low_confidence_words")} -->', '', text.rstrip(), '']
        md = '\n'.join(head + body)
        _write_new(out / (doc + '.md'), md)
        from xml_to_md import COMPONENT
        index['documents'][doc] = {'markdown': doc + '.md', 'markdown_sha256': sha(md.encode('utf-8')), 'original_sha256': meta['original_sha256'],
                                   'code': d.get('document_type'), 'component': COMPONENT.get(d.get('document_type'), 'other'), 'component_source': 'inventory code (OCR text)',
                                   'ocr_run': run_id, 'ocr_text_sha256': meta['text_sha256'], 'pages': len(pages)}
        ok.append(doc)
    out.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_text(idx_path, json.dumps(index, indent=1) + '\n')
    _record_failures(out, failed, ok, 'ocr', {pj.name.split('.')[0] for pj in run.glob('*.pages.json')})
    return index


LAST_FAILED = 0   # documents that failed in the most recent normalisation of this process (the command line reports it and exits non-zero)


def _record_failures(out, failed, ok=(), kind='ocr', listed=None):
    """One bad document is recorded in <app>/compact-failures.json and skipped; it never stops the others. The file is ONE merged record for the
    application (entries carry the kind of normalisation that failed): a document that is normalised cleanly (`ok`) is removed from it, an entry of
    this kind whose source file is no longer there (not in `listed`) is removed, other entries stay, and a run with nothing left to record removes the file."""
    global LAST_FAILED
    path = out / 'compact-failures.json'
    old, kinds = {}, {}
    if path.is_file():
        try:
            j = json.loads(path.read_text(encoding='utf-8')); old = dict(j.get('failed') or {}); kinds = dict(j.get('kinds') or {})
        except (OSError, ValueError, AttributeError): old, kinds = {}, {}
    for d in ok: old.pop(d, None); kinds.pop(d, None)
    if listed is not None:
        for d in [d for d in old if kinds.get(d, kind) == kind and d not in listed]: old.pop(d); kinds.pop(d, None)
    for doc, why in failed.items(): print(f'compact: {doc} skipped: {why}', file=sys.stderr)
    old.update(failed); kinds.update({d: kind for d in failed}); LAST_FAILED += len(failed)
    if old:
        out.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_text(path, json.dumps({'failed': dict(sorted(old.items())), 'kinds': {d: kinds.get(d, kind) for d in sorted(old)}}, indent=1) + '\n')
    elif path.is_file():
        try: path.unlink()
        except OSError: pass


RENDERER = 3  # 2: one Markdown per XML part, component labels, <ins>/~~ amendment markup, per-claim rendering
             # 3: allowance aliases (CNTA/CNOA), abstract root compatibility, member-name code fix


def _write_parts(out, doc, parts, prev):
    """Write <doc>.md (primary) and <doc>.<CODE>[-n].md (other parts); remove part files of an earlier rendering."""
    out.mkdir(parents=True, exist_ok=True)
    used = set()
    for i, p in enumerate(parts):
        name = doc + '.md' if i == 0 else f'{doc}.{re.sub(r"[^A-Za-z0-9]+", "_", p["code"])}.md'
        k = 2
        while name in used: name = name[:-3] + f'-{k}.md'; k += 1
        used.add(name); p['file'] = name
        data = p['markdown'].encode('utf-8'); p['markdown_sha256'] = sha(data)
        (out / name).write_bytes(data)
    for old in (prev or {}).get('parts', []):
        if old.get('file') and old['file'] not in used and (out / old['file']).exists(): (out / old['file']).unlink()


def _carry_superseded(entry, prev):
    if not prev: return
    if prev.get('superseded'): entry['superseded'] = prev['superseded']
    elif prev.get('source') not in ('uspto-xmlarchive', 'uspto-docx'):
        entry['superseded'] = {k: prev[k] for k in ('markdown_sha256', 'original_sha256', 'ocr_run') if k in prev}


def normalize_docx(app_dir, root=ROOT):
    """As-filed application text (<doc>.docx, APP.TEXT) -> specification / claims / abstract Markdown, split at headings."""
    from xml_to_md import docx_parts
    app_dir = Path(app_dir); app = app_dir.name
    out = root / 'corpus' / 'normalized' / app
    docs = _manifest_docs(app_dir) if root == ROOT else {}
    idx_path = out / 'index.json'
    index = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {'application': app, 'documents': {}}
    done, failed, ok = [], {}, []
    for f in sorted(app_dir.glob('*.docx')):
        doc = f.stem
        try: b = f.read_bytes(); h = sha(b)                      # an unreadable file is one failed document, not a crash
        except OSError as e: failed[doc] = f'{type(e).__name__}: {str(e)[:160]}'; continue
        prev = index['documents'].get(doc)
        if prev and prev.get('source') == 'uspto-docx' and prev.get('original_sha256') == h and prev.get('renderer') == RENDERER: ok.append(doc); continue
        d = docs.get(doc, {})
        try:
            parts = docx_parts(b, app=app, doc=doc, inventory_code=d.get('document_type'), original_sha256=h, source_locator=d.get('source_locator'))
            _write_parts(out, doc, parts, prev)
        except Exception as e: failed[doc] = f'{type(e).__name__}: {str(e)[:160]}'; continue
        entry = {'markdown': doc + '.md', 'markdown_sha256': parts[0]['markdown_sha256'], 'source': 'uspto-docx', 'renderer': RENDERER,
                 'original_sha256': h, 'component': parts[0]['component'], 'code': parts[0]['code'],
                 'parts': [{k: p[k] for k in ('file', 'code', 'component', 'role', 'paragraphs', 'markdown_sha256')} for p in parts]}
        _carry_superseded(entry, prev)
        index['documents'][doc] = entry; done.append(doc); ok.append(doc)
    fsutil.atomic_write_text(idx_path, json.dumps(index, indent=1) + '\n')   # replaced whole: a crash never leaves half an index
    _record_failures(out, failed, ok, 'docx', {f.stem for f in app_dir.glob('*.docx')})
    return done


def normalize_xml(app_dir, root=ROOT):
    """USPTO xmlarchive TARs (<doc>.tar) -> Markdown via xml_to_md (bounded member reads, no extraction).
    Replaces an earlier OCR rendering of the same document; the OCR run itself stays on disk."""
    from xml_to_md import xml_members, archive_parts
    app_dir = Path(app_dir); app = app_dir.name
    out = root / 'corpus' / 'normalized' / app
    docs = _manifest_docs(app_dir) if root == ROOT else {}
    idx_path = out / 'index.json'
    index = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {'application': app, 'documents': {}}
    done, failed, ok = [], {}, []
    for tar in sorted(app_dir.glob('*.tar')):
        doc = tar.stem
        try: b = tar.read_bytes(); h = sha(b)                    # an unreadable file is one failed document, not a crash
        except OSError as e: failed[doc] = f'{type(e).__name__}: {str(e)[:160]}'; continue
        prev = index['documents'].get(doc)
        if prev and prev.get('source') == 'uspto-xmlarchive' and prev.get('archive_sha256') == h and prev.get('renderer') == RENDERER: ok.append(doc); continue
        d = docs.get(doc, {})
        # Every XML part gets its own Markdown and component label (claims / specification / remarks / ...):
        # the part carrying the document's own code is <doc>.md, bundled parts are <doc>.<CODE>.md.
        try:
            parts = archive_parts(xml_members(b, app, doc), app=app, doc=doc, inventory_code=d.get('document_type'), archive_sha256=h, source_locator=d.get('source_locator'))
            _write_parts(out, doc, parts, prev)
        except Exception as e: failed[doc] = f'{type(e).__name__}: {str(e)[:160]}'; continue
        entry = {'markdown': doc + '.md', 'markdown_sha256': parts[0]['markdown_sha256'], 'source': 'uspto-xmlarchive', 'renderer': RENDERER,
                 'archive_sha256': h, 'xml_member': parts[0]['member'], 'xml_sha256': parts[0]['xml_sha256'],
                 'component': parts[0]['component'], 'code': parts[0]['code'],
                 'parts': [{k: p[k] for k in ('file', 'member', 'code', 'component', 'role', 'code_source', 'warnings', 'xml_sha256', 'markdown_sha256')} for p in parts]}
        _carry_superseded(entry, prev)
        index['documents'][doc] = entry; done.append(doc); ok.append(doc)
    fsutil.atomic_write_text(idx_path, json.dumps(index, indent=1) + '\n')   # replaced whole: a crash never leaves half an index
    _record_failures(out, failed, ok, 'xml', {t.stem for t in app_dir.glob('*.tar')})
    return done


def prune_originals(app_dir, apply=False, root=ROOT):
    app_dir = Path(app_dir); app = app_dir.name
    norm = root / 'corpus' / 'normalized' / app
    index = json.loads((norm / 'index.json').read_text(encoding='utf-8'))['documents']
    docs = _manifest_docs(app_dir) if root == ROOT else {}
    tomb_path = app_dir / 'pruned.json'
    tomb = json.loads(tomb_path.read_text(encoding='utf-8')) if tomb_path.exists() else {'application': app, 'pruned': []}
    plan, refused = [], []
    for pdf in sorted(app_dir.glob('*.pdf')):
        doc = pdf.stem; b = pdf.read_bytes(); h = sha(b); e = index.get(doc)
        why = None
        if e and e.get('source') in ('uspto-xmlarchive', 'uspto-docx'):
            # an XML/DOCX rendering superseded this PDF's OCR text: prune only the exact PDF that OCR was made from
            if (e.get('superseded') or {}).get('original_sha256') != h: why = 'XML rendering exists but this PDF is not the superseded original'
        elif not e: why = 'no normalized Markdown'
        elif e['original_sha256'] != h: why = 'normalized Markdown was made from different bytes'
        if not why and sha((norm / e['markdown']).read_bytes()) != e['markdown_sha256']: why = 'Markdown hash mismatch'
        if why: refused.append({'document_id': doc, 'reason': why}); continue
        fid = docs.get(doc, {}).get('file_id')
        plan.append({'document_id': doc, 'original_sha256': h, 'bytes': len(b), 'source_locator': docs.get(doc, {}).get('source_locator'),
                     'markdown': f'corpus/normalized/{app}/{e["markdown"]}', 'reacquire': f'node acquire_round.mjs {app} 1 {doc}' + (f'@file:{fid}' if fid else ''),
                     'file': pdf.name})
    for tar in sorted(app_dir.glob('*.tar')):
        doc = tar.stem; b = tar.read_bytes(); h = sha(b); e = index.get(doc)
        why = None
        if not e or e.get('source') != 'uspto-xmlarchive': why = 'no XML-derived Markdown'
        elif e.get('archive_sha256') != h: why = 'Markdown was made from a different archive'
        elif sha((norm / e['markdown']).read_bytes()) != e['markdown_sha256']: why = 'Markdown hash mismatch'
        if why: refused.append({'document_id': doc, 'reason': why}); continue
        plan.append({'document_id': doc, 'archive_sha256': h, 'xml_sha256': e.get('xml_sha256'), 'bytes': len(b),
                     'markdown': f'corpus/normalized/{app}/{e["markdown"]}', 'reacquire': f'node acquire_round.mjs {app} 1 {doc}@xml',
                     'note': 'USPTO regenerates xmlarchive per request: compare xml_sha256 of the extracted member, not the archive hash', 'file': tar.name})
    for f in sorted(app_dir.glob('*.docx')):
        doc = f.stem; b = f.read_bytes(); h = sha(b); e = index.get(doc)
        why = None
        if not e or e.get('source') != 'uspto-docx': why = 'no DOCX-derived Markdown'
        elif e.get('original_sha256') != h: why = 'Markdown was made from a different DOCX'
        elif sha((norm / e['markdown']).read_bytes()) != e['markdown_sha256']: why = 'Markdown hash mismatch'
        if why: refused.append({'document_id': doc, 'reason': why}); continue
        fid = docs.get(doc, {}).get('docx_file_id')
        plan.append({'document_id': doc, 'original_sha256': h, 'bytes': len(b), 'markdown': f'corpus/normalized/{app}/{e["markdown"]}',
                     'reacquire': f'node acquire_round.mjs {app} 1 {doc}@docx:{fid}' if fid else None, 'file': f.name})
    if apply and plan:
        stamp = now()
        for p in plan: p['pruned_at'] = stamp
        tomb['pruned'] += plan
        tomb_path.write_text(json.dumps(tomb, indent=1) + '\n', encoding='utf-8', newline='\n')
        for p in plan: (app_dir / p.pop('file')).unlink()
    return {'applied': bool(apply and plan), 'prune': plan, 'refused': refused, 'bytes_freed': sum(p['bytes'] for p in plan)}


def compact_frame(frame_path, apply=False, root=ROOT):
    from frame_contracts import load_frame
    f = load_frame(frame_path)
    try: paths.check_frame_id(f.get('frame_id'))
    except paths.PathError as e: raise SystemExit(f'frame file: {e}')
    base = (root / 'public-originals' / 'frames' / f['frame_id']).resolve()
    inside = lambda path: Path(path).resolve().is_relative_to(base)
    # Every path the frame file names must lie inside public-originals/frames/<frame_id>/: a frame file can never point a delete elsewhere.
    for q in f['queries']:
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,80}', str(q.get('query_id'))) or not inside(base / q['query_id']): raise SystemExit(f'query id {str(q.get("query_id"))[:40]!r} is not a valid folder name under {base}')
        for p in q['pages']:
            if p.get('raw_path') and not inside(root / p['raw_path']): raise SystemExit(f'{q["query_id"]}: raw_path {str(p["raw_path"])[:80]!r} is outside {base}; nothing was read or deleted')
    codes, out_rows, files = {}, {}, []
    for q in f['queries']:
        for p in q['pages']:
            fp = root / p['raw_path'] if p.get('raw_path') else None
            if not fp or not fp.exists(): raise SystemExit(f'{q["query_id"]} offset {p["offset"]}: raw page missing (already pruned?)')
            raw = fp.read_bytes()
            if sha(raw) != p['response_sha256']: raise SystemExit(f'{fp}: hash mismatch')
            files.append((fp, len(raw)))
            for rec in json.loads(raw).get('patentFileWrapperDataBag') or []:
                app = rec.get('applicationNumberText'); m = rec.get('applicationMetaData') or {}
                ev = []
                for e in rec.get('eventDataBag') or []:
                    c = e.get('eventCode'); codes.setdefault(c, e.get('eventDescriptionText')); ev.append([e.get('eventDate'), c])
                out_rows[app] = {'application_number': app, 'examiner': m.get('examinerNameText'), 'art_unit': m.get('groupArtUnitNumber'),
                                 'parents': [[x.get('parentApplicationNumberText'), x.get('claimParentageTypeCode'), x.get('parentApplicationFilingDate')] for x in rec.get('parentContinuityBag') or []],
                                 'events': sorted(ev, key=lambda x: (x[0] or '', x[1] or ''))}   # by date, then code: the provider's array order is not chronological and is not kept
    body = '\n'.join(json.dumps(out_rows[a], separators=(',', ':')) for a in sorted(out_rows)) + '\n'
    out = root / 'corpus' / 'frame-events' / (f['frame_id'] + '.jsonl.gz')  # events_sha256 below is over the uncompressed body
    _write_new(out, body)
    codes_out = out.parent / (f['frame_id'] + '.event-codes.json')
    _write_new(codes_out, json.dumps(dict(sorted((k or '', v) for k, v in codes.items())), indent=1) + '\n')
    result = {'frame_id': f['frame_id'], 'events_file': str(out.relative_to(root)).replace('\\', '/'), 'events_sha256': sha(body.encode('utf-8')),
              'applications': len(out_rows), 'raw_pages': len(files), 'raw_bytes': sum(n for _, n in files), 'compact_bytes': len(body.encode('utf-8')), 'applied': False}
    if apply:
        for q in f['queries']:
            qdir = root / 'public-originals' / 'frames' / f['frame_id'] / q['query_id']
            (qdir / 'pruned.json').write_text(json.dumps({'pruned_at': now(), 'compact': result, 'pages': q['pages'],
                'note': 'raw ODP pages deleted after compaction; hashes above and in the frame file. Re-running frame_search.mjs gives NEW evidence, not byte-identical pages.'}, indent=1) + '\n', encoding='utf-8', newline='\n')
        for fp, _ in files: fp.unlink()
        result['applied'] = True
    return result


if __name__ == '__main__':
    a = sys.argv[1:]; apply = '--apply' in a; a = [x for x in a if x != '--apply']
    if '--data-dir' in a:
        i = a.index('--data-dir'); ROOT = Path(a[i + 1]).expanduser().resolve(); del a[i:i + 2]
    try: paths.refuse_install_write(ROOT)
    except paths.PathError as e: sys.exit(str(e))
    if not a: sys.exit(__doc__)
    for x in a[1:2]: paths.checked_or_exit(x, 'the folder or file argument')
    if a[0] == 'normalize': r = normalize(a[1], a[2], root=ROOT); r = {'normalized': len(r['documents'])}
    elif a[0] == 'normalize-xml': r = {'normalized_xml': normalize_xml(a[1], root=ROOT)}
    elif a[0] == 'normalize-docx': r = {'normalized_docx': normalize_docx(a[1], root=ROOT)}
    elif a[0] == 'prune-originals': r = prune_originals(a[1], apply, root=ROOT)
    elif a[0] == 'compact-frame': r = compact_frame(a[1], apply, root=ROOT)
    else: sys.exit(__doc__)
    if a[0].startswith('normalize'): r['failed'] = LAST_FAILED
    print(json.dumps(r, indent=1))
    if LAST_FAILED: sys.exit(1)   # some documents were skipped (see compact-failures.json): not a clean run
