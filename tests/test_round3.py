"""Regression tests for the verification round: NTFS alternate data streams are never read or written, a Repair keeps the chosen Hub (see
test_installer.py), whole ANSI/VT escape sequences are removed from copied text, a bad `db sql` column is one line, min_n below 5 is refused, and
numFound must not change between the pages of one batch."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import pg_db
from pg_tool import collect_oa, paths, service, untrusted
from test_review_fixes import Base

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
ESC = chr(27)
BEL = chr(7)
SECRET = 'SECRET-IN-THE-HIDDEN-STREAM'
OA = 'Claim Rejections - 35 USC 103\n\nClaims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Hale (US 2099/9999901).\n'


def make_carrier(folder):
    """carrier.txt with an alternate data stream hidden.txt (NTFS only); returns the carrier path, or None where streams are not available."""
    carrier = Path(folder) / 'carrier.txt'; carrier.write_text(OA, encoding='utf-8')
    try:
        with open(str(carrier) + ':hidden.txt', 'w', encoding='utf-8') as f: f.write(OA + SECRET)
        with open(str(carrier) + ':hidden.txt', encoding='utf-8') as f:
            if SECRET not in f.read(): return None
    except OSError: return None
    return carrier


class Streams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); (self.root / 'inputs').mkdir()
        self.carrier = make_carrier(self.root / 'inputs')
        p = mock.patch.dict(os.environ, {'PG_TOOL_INPUT_ROOTS': str(self.root / 'inputs')}); p.start(); self.addCleanup(p.stop)

    def need_streams(self):
        if self.carrier is None: self.skipTest('this folder does not support NTFS alternate data streams')

    def test_the_helpers_refuse_a_colon_beyond_the_drive_letter(self):
        for ok in ('C:' + chr(92) + 'a' + chr(92) + 'b.txt', 'relative' + chr(92) + 'b.txt', '/data/files/b.txt', chr(92) * 2 + '?' + chr(92) + 'C:' + chr(92) + 'a.txt', 'D:'):
            self.assertFalse(paths.has_stream_syntax(ok), ok)
        for bad in ('b.txt:hidden.txt', 'b.txt:hidden.txt:$DATA', 'C:' + chr(92) + 'a' + chr(92) + 'b.txt:s', 'dir:x' + chr(92) + 'b.txt', 'b.txt::$DATA', chr(92) * 2 + '?' + chr(92) + 'C:' + chr(92) + 'a.txt:s'):
            self.assertTrue(paths.has_stream_syntax(bad), bad)
            with self.assertRaises(paths.PathError): paths.refuse_stream_syntax(bad)
            with self.assertRaises(paths.PathError): paths.checked_path(bad)
            with self.assertRaises(SystemExit): paths.checked_or_exit(bad)
            with self.assertRaises(paths.PathError): paths.refuse_install_write(bad)
        with self.assertRaises(paths.PathError): paths.resolve_data_dir('data:stream')

    def test_the_mcp_tools_refuse_both_stream_spellings_and_still_read_the_plain_file(self):
        self.need_streams()
        for tool in ('pg_assess_office_action', 'pg_response_pack', 'pg_similar_office_actions', 'pg_report'):
            for suffix in (':hidden.txt', ':hidden.txt:$DATA'):
                arg = {'file_path': str(self.carrier) + suffix}
                e = service.call(tool, {'subject': {'office_action': arg}} if tool == 'pg_report' else arg)
                self.assertEqual(e['status'], 'error', (tool, suffix)); self.assertEqual(e['error']['code'], 'out_of_scope_path', (tool, suffix))
                self.assertNotIn(SECRET, json.dumps(e)); self.assertNotIn('Hale', json.dumps(e))
        e = service.call('pg_assess_office_action', {'file_path': str(self.carrier)}); self.assertIn(e['status'], ('ok', 'partial'), e.get('error'))
        self.assertTrue(e['data']['analysis']['rejections']); self.assertNotIn(SECRET, json.dumps(e))
        with self.assertRaises(service.ToolError): service._read_allowed_file(str(self.carrier) + ':hidden.txt')

    def test_the_resolved_path_is_the_opened_file(self):
        src = (ROOT / 'pg_tool' / 'service.py').read_text(encoding='utf-8'); self.assertIn('samestat', src); self.assertIn('has_stream_syntax(raw)', src)

    def cli(self, *argv):
        env = {**os.environ, 'PG_TOOL_DATA_DIR': str(self.root / 'data'), 'PYTHONDONTWRITEBYTECODE': '1'}
        return subprocess.run([sys.executable, '-B', str(ROOT / 'pg_cli.py'), *argv], capture_output=True, text=True, encoding='utf-8', env=env, cwd=str(self.root), timeout=120)

    def test_the_command_line_refuses_streams_for_every_path_argument(self):
        self.need_streams()
        s = str(self.carrier) + ':hidden.txt'
        for argv in (('assess', s), ('assess', s + ':$DATA'), ('similar', s), ('pack', s), ('shell', 'office-action-response', '--from-pack', s), ('scope', 'validate', s),
                     ('assess', str(self.carrier), '--out', str(self.root / 'o.json:s')), ('doctor', '--data-dir', str(self.root) + chr(92) + 'dd:s')):
            r = self.cli(*argv)
            self.assertNotEqual(r.returncode, 0, argv); self.assertNotIn(SECRET, r.stdout + r.stderr, argv); self.assertNotIn('Traceback', r.stderr, argv)
            self.assertRegex(r.stderr, r'":"|alternate data|beyond the drive', argv)
        self.assertFalse((self.root / 'o.json').exists())
        r = self.cli('assess', str(self.carrier)); self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_python_scripts_use_the_checked_path_helpers(self):
        expect = {'frame_build.py': 'checked_or_exit(args[0]', 'frame_select.py': 'checked_or_exit(args[0]', 'cpc_expand.py': 'checked_or_exit(zp', 'ocr_run.py': 'checked_or_exit(a.app_dir',
                  'compact.py': 'checked_or_exit(x', 'frame_contracts.py': 'checked_path(path', 'pg_tool/scope.py': 'checked_path(path'}
        for f, needle in expect.items(): self.assertIn(needle, (ROOT / f).read_text(encoding='utf-8'), f)
        for f in ('frame_select.py', 'cpc_expand.py', 'frame_build.py', 'frame_plan.py', 'pg_cli.py'): self.assertIn('refuse_install_write', (ROOT / f).read_text(encoding='utf-8'), f)   # outputs go through it
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'compact.py'), 'normalize', str(self.root) + ':s', 'r1', '--data-dir', str(self.root / 'data')], capture_output=True, text=True, cwd=str(self.root))
        self.assertNotEqual(r.returncode, 0); self.assertIn('alternate data streams', r.stderr); self.assertNotIn('Traceback', r.stderr)
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'frame_contracts.py'), str(self.root / 'x.json:s')], capture_output=True, text=True, cwd=str(self.root))
        self.assertNotEqual(r.returncode, 0); self.assertIn('alternate data streams', r.stderr)

    @unittest.skipUnless(NODE, 'node not available')
    def test_the_node_scripts_refuse_streams_too(self):
        with tempfile.TemporaryDirectory() as t:
            js = Path(t) / 'probe.mjs'
            js.write_text("import * as m from " + json.dumps((ROOT / 'hub_config.mjs').as_uri()) + ";\n"
                          "const out = {};\n"
                          "for (const p of ['C:/a/b.json', 'b.json', String.raw`C:\\a\\b.json`]) out['ok:' + p] = m.hasStreamSyntax(p);\n"
                          "for (const p of ['b.json:hidden.json', 'b.json:hidden.json:$DATA', String.raw`C:\\a\\b.json:s`]) { out['bad:' + p] = m.hasStreamSyntax(p); try { m.checkedPath(p); out['threw:' + p] = false; } catch (e) { out['threw:' + p] = true; } }\n"
                          "console.log(JSON.stringify(out));\n", encoding='utf-8')
            r = subprocess.run([NODE, str(js)], capture_output=True, text=True, encoding='utf-8', timeout=60); self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
        for k, v in out.items(): self.assertEqual(v, not k.startswith('ok:'), k)
        for f, needle in (('acquire_family.mjs', "checkedPath(framePath"), ('frame_run.mjs', "checkedPath(planPath"), ('hub_config.mjs', "checkedPath(process.env.PG_TOOL_DATA_DIR")):
            self.assertIn(needle, (ROOT / f).read_text(encoding='utf-8'), f)


class Ansi(unittest.TestCase):
    def test_whole_escape_sequences_are_removed_not_just_the_escape_character(self):
        cases = {
            'Hale' + ESC + '[31m(US 2099/9999901)' + ESC + '[0m': 'Hale(US 2099/9999901)',                         # colour
            'a' + ESC + '[1;31;40mb' + ESC + '[m': 'ab',
            'cur' + ESC + '[2J' + ESC + '[1;1H' + ESC + '[?25lsor': 'cursor',                                     # erase screen, cursor home, hide cursor
            'x' + ESC + '[5A' + ESC + '[10Gy' + ESC + '[K': 'xy',                                                  # cursor up, column, erase line
            'a' + ESC + ']0;evil window title' + BEL + 'b': 'ab',                                                  # OSC ended by BEL
            'a' + ESC + ']8;;http://example.invalid/' + ESC + chr(92) + 'link' + ESC + ']8;;' + ESC + chr(92) + 'b': 'alinkb',   # OSC hyperlink ended by ST
            'a' + ESC + 'cb': 'ab', 'a' + ESC + '7b' + ESC + '8c': 'abc', 'a' + ESC + 'Mb': 'ab',                  # two-character sequences
            'a' + ESC + '(Bb': 'ab',                                                                               # charset selection
            'a' + ESC + 'Pq;payload' + ESC + chr(92) + 'b': 'ab',                                                  # DCS
            'a' + chr(0x9b) + '31mb': 'ab',                                                                        # 8-bit CSI
            'a' + ESC + ']0;unterminated title': 'a',                                                              # OSC with no end: to the end of the line
            ESC + '[31m' + ESC + '[0m': '',
        }
        for raw, want in cases.items(): self.assertEqual(untrusted.clean(raw), want, repr(raw))
        for raw in cases:
            out = untrusted.clean(raw)
            for frag in ('[31m', '[2J', '[0m', '[1;1H', '[?25l', '0;evil', '8;;', 'payload'): self.assertNotIn(frag, out, (raw, frag))
        self.assertEqual(untrusted.clean('plain text, 100% [bracketed] and (parenthesised); no escape'), 'plain text, 100% [bracketed] and (parenthesised); no escape')
        self.assertEqual(untrusted.clean_line('a' + ESC + '[31m' + chr(10) + chr(9) + 'b'), 'a b')

    def test_a_citation_with_colour_osc_and_cursor_sequences_leaves_no_residue_in_the_assess_json(self):
        oa = ('Claim Rejections - 35 USC 103' + chr(10) * 2 + 'Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over ' + ESC + '[31mHale' + ESC + ']0;title' + BEL +
              ' (US 2099/9999901)' + ESC + '[0m' + ESC + '[2J in view of ' + ESC + '[1;1HIves' + ESC + ']8;;http://example.invalid/' + ESC + chr(92) + '.' + chr(10))
        e = service.call('pg_assess_office_action', {'text': oa}); self.assertEqual(e['status'], 'ok', e.get('error'))
        text = json.dumps(e, ensure_ascii=False)
        for frag in ('[31m', '[0m', '[2J', '[1;1H', '0;title', '8;;http', ESC, BEL): self.assertNotIn(frag, text, frag)
        cites = [r['citation'] for r in e['data']['analysis']['references']]; self.assertTrue(any('Hale' in c for c in cites), cites); self.assertTrue(any('Ives' in c for c in cites), cites)
        ids = [r['id'] for r in e['data']['analysis']['references']]
        for i in ids: self.assertEqual(i, untrusted.clean_line(i, 60)); self.assertNotIn('[', i)


class DbSqlAndMinN(Base):
    def test_a_bad_column_in_db_sql_is_one_line_not_a_traceback(self):
        with self.assertRaises(SystemExit) as c: pg_db.main(['sql', 'SELECT nosuchcolumn FROM applications', '--db', str(self.db)])
        self.assertTrue(str(c.exception.code).startswith('sql error:')); self.assertNotIn('\n', str(c.exception.code))
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'pg_cli.py'), 'db', 'sql', 'SELECT nosuchcolumn FROM applications', '--data-dir', str(self.data)], capture_output=True, text=True, encoding='utf-8', cwd=str(self.data))
        self.assertEqual(r.returncode, 1); self.assertNotIn('Traceback', r.stderr); self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr); self.assertIn('no such column', r.stderr)
        with self.assertRaises(SystemExit) as c: pg_db.main(['sql', 'SELECT * FROM nosuchtable', '--db', str(self.db)])
        self.assertIn('no such table', str(c.exception.code))
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()): pg_db.main(['sql', 'SELECT COUNT(*) FROM applications', '--db', str(self.db)])                      # a good query still runs
        with self.assertRaises(SystemExit): pg_db.main(['sql', 'DELETE FROM applications', '--db', str(self.db)])

    def test_min_n_below_5_is_refused_over_mcp_and_on_the_command_line_and_the_docs_say_so(self):
        for tool, req in (('pg_trends', {'group_by': 'stratum', 'min_n': 4}), ('pg_similar_office_actions', {'features': {'statutes': ['103']}, 'min_n': 1}), ('pg_response_pack', {'text': OA, 'min_n': 3})):
            e = service.call(tool, req); self.assertEqual((e['status'], e['error']['code']), ('error', 'invalid_input'), tool); self.assertIn('below 5', e['error']['message'])
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'pg_cli.py'), 'similar', '--features', '{"statutes": ["103"]}', '--min-n', '1', '--data-dir', str(self.data)], capture_output=True, text=True, encoding='utf-8', cwd=str(self.data))
        self.assertNotEqual(r.returncode, 0); self.assertIn('below 5', r.stdout + r.stderr)
        readme = (ROOT / 'README.md').read_text(encoding='utf-8'); self.assertIn('never below 5', readme); self.assertIn('refused', readme); self.assertNotIn('raised to 5', readme)
        sim = (ROOT / 'docs' / 'SIMILAR.md').read_text(encoding='utf-8'); self.assertIn('refuse a smaller value', sim); self.assertNotIn('raise a smaller value', sim)


class NumFound(unittest.TestCase):
    def test_a_changing_numfound_between_pages_stops_the_batch(self):
        gw = mock.Mock(); gw.call.return_value = {'status': 200}
        a = mock.Mock(); a.page_rows = 100; a.request.return_value = {}
        seq = iter([(250, [{'k': 1}] * 100), (260, [{'k': 1}] * 100), (260, [{'k': 1}] * 60)]); a.parse_page.side_effect = lambda env: next(seq)
        col = collect_oa.Collector('.', a, gw, sleep=lambda s: None, say=lambda *_: None)
        with self.assertRaises(collect_oa.Stop) as c: col.collect_batch(['1'])
        self.assertEqual(c.exception.reason, 'bad_response'); self.assertIn('must not change', str(c.exception)); self.assertEqual(gw.call.call_count, 2); self.assertTrue(c.exception.resumable)
        seq = iter([(250, [{'k': 1}] * 100), (250, [{'k': 1}] * 100), (250, [{'k': 1}] * 50)])                  # steady: the batch is complete
        col = collect_oa.Collector('.', a, gw, sleep=lambda s: None, say=lambda *_: None); a.parse_page.side_effect = lambda env: next(seq)
        rows, found = col.collect_batch(['1']); self.assertEqual((len(rows), found), (250, 250))


class Hardening(unittest.TestCase):
    def test_no_invisible_characters_in_the_source_files(self):
        import unicodedata
        for f in subprocess.run(['git', 'ls-files'], capture_output=True, text=True, cwd=str(ROOT)).stdout.split():
            if not f.endswith(('.py', '.mjs', '.ps1', '.json', '.md', '.txt', '.toml', '.cmd')): continue
            try: t = (ROOT / f).read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError): continue
            bad = sorted({hex(ord(c)) for c in t if unicodedata.category(c) in ('Cf', 'Zl', 'Zp') or (unicodedata.category(c) == 'Cc' and c not in chr(10) + chr(13) + chr(9) + chr(12))})
            self.assertEqual(bad, [], f)


if __name__ == '__main__':
    unittest.main()
