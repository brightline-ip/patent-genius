#!/usr/bin/env python3
"""Patent Genius command line.

  python pg_cli.py scope init|show|validate      your scope: fields, filing dates, document types (scope.json)
  python pg_cli.py db <command> ...              build and query the local database (python pg_cli.py db build --help)
  python pg_cli.py assess <office-action.txt>    rule-based reading of an office action you hold as a text file
  python pg_cli.py collect plan|run|status|probe   collect office-action rejection rows for the applications in your frames (needs the Hub)
  python pg_cli.py similar <office-action.txt>   rank the office actions in your database by a published similarity
  python pg_cli.py pack <office-action.txt>      response material pack: typed facts, pointers, counts; no argument text
  python pg_cli.py shell <key> [--from-pack f]   a structural shell, or the response shell with fact slots filled from a pack
  python pg_cli.py doctor                        check Python, Node, the data folder, the Hub and Tesseract (no network)

Every command accepts --data-dir <folder>; otherwise PG_TOOL_DATA_DIR, otherwise the per-user default folder is used.
Writing inside the install folder is refused. Nothing here calls a provider or a model."""
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_tool import hubgw, paths, scope as scope_mod  # noqa: E402

DATA_DIR_HELP = 'data folder (default: PG_TOOL_DATA_DIR, else the per-user folder)'


# ---------------------------------------------------------------- scope
def cmd_scope(a):
    if a.scope_cmd == 'init':
        try: p = scope_mod.init(a.data_dir, force=a.force)
        except (paths.PathError, FileExistsError, OSError) as e: print(f'error: {e}', file=sys.stderr); return 1
        print(f'wrote {p}\nIt holds an invented example (fields "agri" and "soil_sensing"). Edit every value, then run: python pg_cli.py scope validate')
        return 0
    path = Path(a.path) if getattr(a, 'path', None) else None
    try: sc, p = scope_mod.load(a.data_dir, path)
    except FileNotFoundError as e: print(f'error: {e}', file=sys.stderr); return 2
    except ValueError as e: print(f'invalid: {e}', file=sys.stderr); return 1
    if a.scope_cmd == 'validate': print(f'valid: {p}'); return 0
    out = {'path': str(p), 'sha256': scope_mod.sha256_of(p), 'summary': scope_mod.summary(sc), 'scope': sc}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- db
def cmd_db(a):
    import pg_db
    args = list(a.db_args)
    if a.data_dir: args += ['--data-dir', a.data_dir]
    try: pg_db.main(args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (print(e.code, file=sys.stderr) or 1)
    return 0


# ---------------------------------------------------------------- assess
def cmd_assess(a):
    from pg_tool import service
    src = Path(a.file)
    if src.suffix.lower() == '.pdf': print('error: convert the PDF to a text file first (this tool reads text only)', file=sys.stderr); return 2
    try:
        with open(src, 'rb') as fh: raw = fh.read(service.MAX_INPUT_BYTES + 1)
    except OSError: print('error: the file could not be read', file=sys.stderr); return 2
    if len(raw) > service.MAX_INPUT_BYTES: print(f'error: file exceeds {service.MAX_INPUT_BYTES} bytes', file=sys.stderr); return 2
    text = raw.decode('utf-8', errors='replace')
    ctx = {k: v for k, v in (('stratum', a.field), ('art_unit', a.art_unit), ('cpc', a.cpc)) if v}
    oa = {'text': text, 'include_excerpts': bool(a.include_excerpts)}
    if ctx: oa['context'] = ctx
    if a.markdown: env = service.call('pg_report', {'subject': {'office_action': oa}})
    else: env = service.call('pg_assess_office_action', {**oa, 'include_statistics': not a.no_statistics})
    out = Path(a.out) if a.out else None
    if out is not None:
        try: paths.refuse_install_write(out)
        except paths.PathError as e: print(f'error: {e}', file=sys.stderr); return 1
        if out.exists() and not a.force: print(f'error: {out} exists; pass --force to replace it', file=sys.stderr); return 1
        # The tool service itself writes nothing. This command is about to write a file at the path the user named, so say so truthfully.
        env['boundary']['persisted_by_tool'] = True
        env['boundary']['persisted_to'] = str(out)
        env['boundary']['note'] = 'This command wrote this result to the path you named; it wrote nothing else. ' + env['boundary'].get('note', '')
    body = (env['data'].get('markdown') if a.markdown and env['status'] in ('ok', 'partial') else json.dumps(env, indent=1, ensure_ascii=False))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body if body.endswith('\n') else body + '\n', encoding='utf-8', newline='\n')
        print(f'wrote {out} (status {env["status"]})')
    else:
        if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
        print(body)
    return {'ok': 0, 'partial': 0, 'not_found': 3, 'deferred': 4}.get(env['status'], 1)



# ---------------------------------------------------------------- output of a tool result
def _emit(env, a, body=None, ok_status=('ok', 'partial')):
    """Print or write a result. The tool service writes nothing; this command writes only to the path the user names and says so truthfully."""
    out = Path(a.out) if getattr(a, 'out', None) else None
    if out is not None:
        try: paths.refuse_install_write(out)
        except paths.PathError as e: print(f'error: {e}', file=sys.stderr); return 1
        if out.exists() and not a.force: print(f'error: {out} exists; pass --force to replace it', file=sys.stderr); return 1
        env['boundary']['persisted_by_tool'] = True
        env['boundary']['persisted_to'] = str(out)
        env['boundary']['note'] = 'This command wrote this result to the path you named; it wrote nothing else. ' + env['boundary'].get('note', '')
        if isinstance(env.get('data'), dict) and 'persisted' in env['data']: env['data']['persisted'] = True
    text = body(env) if (body and env['status'] in ok_status) else json.dumps(env, indent=1, ensure_ascii=False)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text if text.endswith('\n') else text + '\n', encoding='utf-8', newline='\n')
        print(f'wrote {out} (status {env["status"]})')
    else:
        if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
        print(text)
    return {'ok': 0, 'partial': 0, 'not_found': 3, 'deferred': 4}.get(env['status'], 1)


def _request_from_file(a):
    src = Path(a.file)
    if src.suffix.lower() == '.pdf': print('error: convert the PDF to a text file first (this tool reads text only)', file=sys.stderr); return None
    from pg_tool import service
    try:
        with open(src, 'rb') as fh: raw = fh.read(service.MAX_INPUT_BYTES + 1)
    except OSError: print('error: the file could not be read', file=sys.stderr); return None
    if len(raw) > service.MAX_INPUT_BYTES: print(f'error: file exceeds {service.MAX_INPUT_BYTES} bytes', file=sys.stderr); return None
    return {'text': raw.decode('utf-8', errors='replace')}


def _similar_options(a):
    req = {}
    ctx = {k: v for k, v in (('cpc', a.cpc), ('art_unit', a.art_unit), ('examiner', a.examiner)) if v}
    if ctx: req['context'] = ctx
    if a.min_score is not None: req['min_score'] = a.min_score
    if a.limit is not None: req['limit'] = a.limit
    if a.min_n is not None: req['min_n'] = a.min_n
    if a.exclude: req['exclude_applications'] = a.exclude
    if a.require: req['require'] = a.require
    if a.field: req['filters'] = {'field': a.field}
    if a.weights: req['weights'] = json.loads(a.weights)
    return req


# ---------------------------------------------------------------- similar, pack, shell
def cmd_similar(a):
    from pg_tool import service
    req = _similar_options(a)
    if a.file:
        base = _request_from_file(a)
        if base is None: return 2
        req.update(base)
    if a.features: req['features'] = json.loads(a.features)
    if a.application: req['application_number'] = a.application
    env = service.call('pg_similar_office_actions', req)
    return _emit(env, a)


def cmd_pack(a):
    from pg_tool import service
    base = _request_from_file(a)
    if base is None: return 2
    req = {**_similar_options(a), **base, 'include_quotes': bool(a.include_quotes), 'include_shell': not a.no_shell}
    if a.application: req['application_number'] = a.application
    env = service.call('pg_response_pack', req)
    return _emit(env, a, body=(lambda e: e['data']['markdown']) if a.markdown else None)


def cmd_shell(a):
    from pg_tool import pack as pack_mod, service, templates as tpl
    if not a.from_pack:
        status, data, warnings = tpl.get(a.key, True)
        if status != 'ok': print('error: ' + '; '.join(warnings), file=sys.stderr); return 2
        text = (data['markdown'] if service.DRAFT_HEADER in data['markdown'] else f'> **{service.DRAFT_HEADER}**\n\n' + data['markdown'])
        persisted = False
    else:
        if a.key != 'office-action-response': print('error: only the office-action-response shell can be filled from a pack', file=sys.stderr); return 2
        try:
            doc = json.loads(Path(a.from_pack).read_text(encoding='utf-8-sig'))
            if isinstance(doc, dict) and isinstance(doc.get('data'), dict) and 'pack' in doc['data']: doc = doc['data']['pack']
            doc = pack_mod.validate_pack_file(doc)
        except (OSError, ValueError) as e: print(f'error: {e}', file=sys.stderr); return 2
        text = pack_mod.render_shell(doc['facts'], doc['issues'], paths.INSTALL_DIR / 'templates' / 'office-action-response.md')
    out = Path(a.out) if a.out else None
    if out is None:
        if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
        print(text); return 0
    try: paths.refuse_install_write(out)
    except paths.PathError as e: print(f'error: {e}', file=sys.stderr); return 1
    if out.exists() and not a.force: print(f'error: {out} exists; pass --force to replace it', file=sys.stderr); return 1
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(text, encoding='utf-8', newline='\n')
    print(f'wrote {out}. persisted_by_tool: true (this command wrote only that file).')
    return 0


# ---------------------------------------------------------------- collect
def cmd_collect(a):
    from pg_tool import collect_oa
    data = paths.resolve_data_dir(a.data_dir)
    if a.collect_cmd == 'status':
        print(json.dumps(collect_oa.status(data), indent=1)); return 0
    try: sc, sp = scope_mod.load(a.data_dir)
    except FileNotFoundError as e: print(f'error: {e}', file=sys.stderr); return 2
    except ValueError as e: print(f'invalid: {e}', file=sys.stderr); return 1
    if 'oa_rejections' not in (sc.get('data_types') or []):
        print('error: scope.json does not list "oa_rejections" in data_types, so nothing will be collected. Add it, then run: python pg_cli.py scope validate', file=sys.stderr); return 64
    frames = set(a.frame) if getattr(a, 'frame', None) else None
    try:
        if a.collect_cmd == 'probe':
            out = collect_oa.probe(data, a.application)
            print(json.dumps(out, indent=1)); return 0
        plan = collect_oa.build_plan(data, sc, frames)
        try: lim = hubgw.limits()
        except hubgw.HubGatewayError as e: lim = None; lim_err = f'{e.code}: {e}'
        else: lim_err = None
        est = collect_oa.estimate(plan, lim, max_requests=getattr(a, 'max_requests', None))
        est['hub_problem'] = lim_err
        est['skipped_not_sent'] = sum(len(f['skipped']) for f in plan['frames'])
        if a.collect_cmd == 'plan':
            print(json.dumps(est, indent=1)); return 0
        print(json.dumps(est, indent=1))
        if not a.yes:
            print('\nNothing was requested. Review the estimate above, then run the same command with --yes to start (it can be stopped and resumed).', file=sys.stderr); return 64
        res = collect_oa.run(data, sc, scope_sha=scope_mod.sha256_of(sp), frames=frames, max_requests=a.max_requests, restart=a.restart, max_wait_s=a.max_wait)
        print(json.dumps({k: v for k, v in res.items() if k != 'frames'} | {'frames': res['frames']}, indent=1))
        return res['exit']
    except ValueError as e: print(f'error: {e}', file=sys.stderr); return 64
    except paths.PathError as e: print(f'error: {e}', file=sys.stderr); return 1
    except hubgw.HubGatewayError as e:
        print(f'error ({e.code}): {e}', file=sys.stderr); return {'hub_too_old': 78, 'hub_unavailable': 78, 'not_configured': 2}.get(e.code, 1)


# ---------------------------------------------------------------- doctor
def _run_version(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8, encoding='utf-8', errors='replace')
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def doctor_report(data_dir=None):
    """Facts about this installation. Reads the environment and local files only; makes no network call."""
    rep = {'network_calls': 0}
    v = sys.version_info
    rep['python'] = {'version': f'{v.major}.{v.minor}.{v.micro}', 'ok': v >= (3, 10), 'need': '3.10 or newer'}
    node = os.environ.get('PG_TOOL_NODE') or shutil.which('node')
    nv = _run_version([node, '--version']) if node else None
    rep['node'] = {'path': node, 'version': nv, 'ok': bool(nv), 'need': 'only for the frame harvest and document download scripts (*.mjs); 24.13 or newer. pg collect does not need it'}
    source = 'argument' if data_dir else 'PG_TOOL_DATA_DIR' if os.environ.get(paths.ENV_DATA) else 'default'
    d = paths.resolve_data_dir(data_dir)
    rep['data_dir'] = {'path': str(d), 'source': source, 'exists': d.is_dir(), 'writable': bool(d.is_dir() and os.access(d, os.W_OK)),
                       'inside_install_folder': paths.is_inside_install(d), 'install_folder': str(paths.INSTALL_DIR)}
    rep['data_dir']['ok'] = not rep['data_dir']['inside_install_folder']
    sp = d / scope_mod.SCOPE_FILE
    sc = {'path': str(sp), 'present': sp.is_file(), 'valid': None, 'problems': []}
    if sc['present']:
        try: scope_mod.load(path=sp); sc['valid'] = True
        except ValueError as e: sc['valid'] = False; sc['problems'] = str(e).splitlines()[:6]
    rep['scope'] = sc
    hub = {'configured': False, 'source': None, 'command': None, 'present': False, 'version': None, 'ok': False, 'problem': None,
           'need': 'only for collecting data from the USPTO; offline functions work without it. Needs Patent Research Hub 0.2.0 or later (its gateway command).'}
    try: found = hubgw.find_hub()
    except hubgw.HubGatewayError as e: found = None; hub['problem'] = str(e)
    if found:
        hub.update(configured=True, source=found['source'], command=found['argv'][0] if not found['stub'] else '(test stand-in)', present=found['stub'] or found.get('exists', True))
        if hub['present']:
            try:
                lim = hubgw.limits(timeout=30); hub.update(version=lim.get('hub_version'), ok=True)   # reads configuration only: no key, no request
            except hubgw.HubGatewayError as e: hub['problem'] = f'{e.code}: {e}'
        else: hub['problem'] = 'the Hub command does not exist at that path'
    rep['hub'] = hub
    tess = os.environ.get('PG_TESSERACT') or shutil.which('tesseract')
    rep['tesseract'] = {'path': tess, 'version': _run_version([tess, '--version']) if tess else None, 'need': 'only for OCR of scanned PDFs'}
    rep['tesseract']['ok'] = bool(rep['tesseract']['version'])
    return rep


def cmd_doctor(a):
    r = doctor_report(a.data_dir)
    if a.json: print(json.dumps(r, indent=1)); return 0 if r['python']['ok'] and r['data_dir']['ok'] else 1
    mark = lambda ok: 'ok     ' if ok else 'MISSING'
    p, n, d, s, h, t = r['python'], r['node'], r['data_dir'], r['scope'], r['hub'], r['tesseract']
    print('Patent Genius check (makes no network call)\n')
    print(f"{mark(p['ok'])} Python {p['version']} (need {p['need']})")
    print(f"{mark(n['ok'])} Node {n['version'] or 'not found'} ({n['need']})")
    print(f"{mark(d['ok'])} Data folder {d['path']} (from {d['source']}; {'exists' if d['exists'] else 'does not exist yet'}{'; writable' if d['writable'] else ''})")
    if d['inside_install_folder']: print(f"        This folder is inside the install folder {d['install_folder']}; writing there is refused. Choose a folder outside it.")
    print(f"{mark(s['valid'])} Scope {s['path']}: " + ('valid' if s['valid'] else 'not valid' if s['present'] else 'not created (python pg_cli.py scope init)'))
    for line in s['problems']: print('        ' + line)
    print(f"{mark(h['ok'])} Patent Research Hub: " + (f"{h['command']} (from {h['source']}) version {h['version'] or 'unknown'}" if h['ok'] else (h['problem'] or 'not configured (set PG_HUB_CMD to its Hub.cmd)')) + f" ({h['need']})")
    print(f"{mark(t['ok'])} Tesseract {t['version'] or 'not found'} ({t['need']})")
    return 0 if p['ok'] and d['ok'] else 1


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog='pg_cli.py', description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sc = sub.add_parser('scope', help='create, show or validate scope.json'); sc.add_argument('--data-dir', help=DATA_DIR_HELP)
    ssub = sc.add_subparsers(dest='scope_cmd', required=True)
    leaf = argparse.ArgumentParser(add_help=False); leaf.add_argument('--data-dir', default=argparse.SUPPRESS, help=DATA_DIR_HELP)   # also accepted after the subcommand
    i = ssub.add_parser('init', parents=[leaf], help='write an editable example scope.json'); i.add_argument('--force', action='store_true', help='replace an existing scope.json')
    ssub.add_parser('show', parents=[leaf], help='print the scope with its SHA-256')
    v = ssub.add_parser('validate', parents=[leaf], help='check the scope; exit 0 when valid'); v.add_argument('path', nargs='?', help='a scope file elsewhere (default: <data folder>/scope.json)')
    sc.set_defaults(fn=cmd_scope)

    db = sub.add_parser('db', help='local database: build, reports, compare, sql, coverage'); db.add_argument('--data-dir', help=DATA_DIR_HELP)
    db.add_argument('db_args', nargs=argparse.REMAINDER, help='arguments for pg_db (build | reports | report <name> | compare <feature> | coverage | sql "<SELECT>")')
    db.set_defaults(fn=cmd_db)

    asr = sub.add_parser('assess', help='rule-based reading of an office action text file'); asr.add_argument('file')
    asr.add_argument('--field', help='a field name from your scope (adds context; no provider or network query is made from the text)'); asr.add_argument('--art-unit'); asr.add_argument('--cpc')
    asr.add_argument('--markdown', action='store_true', help='print a Markdown report instead of JSON'); asr.add_argument('--no-statistics', action='store_true')
    asr.add_argument('--include-excerpts', action='store_true', help='include short matched spans of the text (default: offsets only)')
    asr.add_argument('--out', help='write the result to this file (the only thing this command ever writes)'); asr.add_argument('--force', action='store_true')
    asr.add_argument('--data-dir', help=DATA_DIR_HELP); asr.set_defaults(fn=cmd_assess)

    co = sub.add_parser('collect', help='collect office-action rejection rows for the applications in your frames'); co.add_argument('--data-dir', help=DATA_DIR_HELP)
    csub = co.add_subparsers(dest='collect_cmd', required=True)
    cleaf = argparse.ArgumentParser(add_help=False); cleaf.add_argument('--data-dir', default=argparse.SUPPRESS, help=DATA_DIR_HELP)
    cleaf.add_argument('--frame', action='append', help='only this frame (repeatable)')
    cp = csub.add_parser('plan', parents=[cleaf], help='dry run: applications, requests, time and disk (no provider request)'); cp.add_argument('--max-requests', type=int)
    cr = csub.add_parser('run', parents=[cleaf], help='collect; resumable'); cr.add_argument('--yes', action='store_true', help='start (without it only the estimate is shown)')
    cr.add_argument('--max-requests', type=int, help='stop after this many requests'); cr.add_argument('--restart', action='store_true', help='discard saved progress and collect again')
    cr.add_argument('--max-wait', type=float, default=3600.0, help='longest single wait for a Hub limit, in seconds (default 3600)')
    csub.add_parser('status', parents=[cleaf], help='progress per frame and recent runs')
    cb = csub.add_parser('probe', parents=[cleaf], help='the provider field list, and optionally one application from your frames'); cb.add_argument('--application', help='an 8-digit application number that is in your frames')
    co.set_defaults(fn=cmd_collect)

    def opts(sp):
        sp.add_argument('--field'); sp.add_argument('--art-unit'); sp.add_argument('--cpc'); sp.add_argument('--examiner', help='examiner as named on the application record')
        sp.add_argument('--min-score', type=float); sp.add_argument('--limit', type=int); sp.add_argument('--min-n', type=int, help='smallest group shown; 5 or more (a smaller value is refused)'); sp.add_argument('--exclude', action='append', help='an application to leave out (repeatable)')
        sp.add_argument('--require', action='append', choices=['statute_set', 'references', 'examiner', 'art_unit', 'cpc', 'ground_101', 'finality']); sp.add_argument('--weights', help='JSON object overriding some weights for this call')
        sp.add_argument('--application', help='an 8-digit application number: left out of the results with its family; its record fills missing features')
        sp.add_argument('--out', help='write the result to this file (the only thing this command ever writes)'); sp.add_argument('--force', action='store_true'); sp.add_argument('--data-dir', help=DATA_DIR_HELP)
    sm = sub.add_parser('similar', help='similar office actions in your database'); sm.add_argument('file', nargs='?', help='office action as a text file'); sm.add_argument('--features', help='JSON features instead of a file')
    opts(sm); sm.set_defaults(fn=cmd_similar)
    pk = sub.add_parser('pack', help='response material pack (typed facts and pointers; no argument text)'); pk.add_argument('file')
    pk.add_argument('--include-quotes', action='store_true', help='add fenced, capped untrusted_quote fields (off by default)'); pk.add_argument('--no-shell', action='store_true'); pk.add_argument('--markdown', action='store_true')
    opts(pk); pk.set_defaults(fn=cmd_pack)
    sh = sub.add_parser('shell', help='a structural shell; with --from-pack the office-action-response shell with fact slots filled'); sh.add_argument('key')
    sh.add_argument('--from-pack', help='a pack file written by: pg pack --out'); sh.add_argument('--out'); sh.add_argument('--force', action='store_true'); sh.add_argument('--data-dir', help=DATA_DIR_HELP); sh.set_defaults(fn=cmd_shell)

    dr = sub.add_parser('doctor', help='check the installation (no network)'); dr.add_argument('--json', action='store_true'); dr.add_argument('--data-dir', help=DATA_DIR_HELP)
    dr.set_defaults(fn=cmd_doctor)

    a = ap.parse_args(argv)
    for name in ('file', 'path', 'out', 'from_pack', 'data_dir', 'db'):                # every path a user types: no NTFS alternate data streams
        v = getattr(a, name, None)
        if isinstance(v, str) and paths.has_stream_syntax(v): print(f'error: the {name.replace("_", "-")} argument has a ":" beyond the drive letter (NTFS alternate data streams are never read or written)', file=sys.stderr); return 2
    for v in (getattr(a, 'db_args', None) or []):
        if isinstance(v, str) and paths.has_stream_syntax(v) and not v.lstrip().upper().startswith(('SELECT', 'WITH')): print('error: refused: a ":" beyond the drive letter in a path (NTFS alternate data streams are never read or written)', file=sys.stderr); return 2
    if getattr(a, 'data_dir', None): os.environ[paths.ENV_DATA] = str(Path(a.data_dir).expanduser().resolve())
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
