"""PG_TOOL_INPUT_ROOTS allowlist for file_path: traversal, symlink escape, prefix tricks, special files and size bounds.
Everything happens in temporary directories; nothing is written to the source tree."""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pg_tool import service

OA = 'Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Example Reference A in view of Example Reference B.\n'


def call(path, roots):
    with mock.patch.dict(os.environ, {'PG_TOOL_INPUT_ROOTS': roots}):
        return service.call('pg_assess_office_action', {'file_path': str(path)})


def code(env): return (env.get('error') or {}).get('code')


class InputRoots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'allowed'; self.root.mkdir()
        self.outside = self.base / 'outside'; self.outside.mkdir()
        (self.root / 'oa.txt').write_text(OA, encoding='utf-8')
        (self.outside / 'secret.txt').write_text('SECRET-OUTSIDE ' + OA, encoding='utf-8')

    def symlink(self, link, target, directory=False):
        try: os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError):
            # Windows without symlink privilege: a directory junction needs none and is followed by resolve() the same way
            if directory and os.name == 'nt':
                r = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, text=True)
                if r.returncode == 0: return
            self.skipTest('symlinks are not available for this user')

    def test_file_inside_root_is_read(self):
        e = call(self.root / 'oa.txt', str(self.root)); self.assertEqual(e['status'], 'ok', e.get('error'))

    def test_dot_dot_traversal_is_refused(self):
        sub = self.root / 'sub'; sub.mkdir()
        for p in (sub / '..' / '..' / 'outside' / 'secret.txt', str(self.root) + '/../outside/secret.txt', str(self.root) + '\\..\\outside\\secret.txt'):
            e = call(p, str(self.root)); self.assertEqual(code(e), 'out_of_scope_path', p); self.assertNotIn('SECRET-OUTSIDE', repr(e))

    def test_sibling_directory_with_same_prefix_is_refused(self):
        sibling = self.base / 'allowed-evil'; sibling.mkdir(); (sibling / 'x.txt').write_text(OA, encoding='utf-8')
        self.assertEqual(code(call(sibling / 'x.txt', str(self.root))), 'out_of_scope_path')

    def test_file_symlink_escape_is_refused(self):
        self.symlink(self.root / 'link.txt', self.outside / 'secret.txt')
        e = call(self.root / 'link.txt', str(self.root)); self.assertEqual(code(e), 'out_of_scope_path'); self.assertNotIn('SECRET-OUTSIDE', repr(e))

    def test_directory_symlink_escape_is_refused(self):
        self.symlink(self.root / 'dirlink', self.outside, directory=True)
        e = call(self.root / 'dirlink' / 'secret.txt', str(self.root)); self.assertEqual(code(e), 'out_of_scope_path'); self.assertNotIn('SECRET-OUTSIDE', repr(e))

    def test_symlink_inside_root_to_inside_root_is_allowed(self):
        self.symlink(self.root / 'alias.txt', self.root / 'oa.txt')
        self.assertEqual(call(self.root / 'alias.txt', str(self.root))['status'], 'ok')

    def test_root_itself_may_be_a_symlink(self):
        self.symlink(self.base / 'rootlink', self.root, directory=True)
        self.assertEqual(call(self.base / 'rootlink' / 'oa.txt', str(self.base / 'rootlink'))['status'], 'ok')

    def test_default_roots_do_not_include_the_data_corpus_or_cwd(self):
        with mock.patch.dict(os.environ, {'PG_TOOL_INPUT_ROOTS': ''}):
            os.environ.pop('PG_TOOL_INPUT_ROOTS')
            names = {r.name for r in service._allowed_roots()}
            self.assertEqual(names, {'examples', 'inputs'})
            self.assertEqual(code(service.call('pg_assess_office_action', {'file_path': str(self.root / 'oa.txt')})), 'out_of_scope_path')

    def test_relative_and_filesystem_roots_are_ignored(self):
        fs_root = Path(self.base.anchor)
        with mock.patch.dict(os.environ, {'PG_TOOL_INPUT_ROOTS': os.pathsep.join(['allowed', '.', str(fs_root)])}):
            self.assertEqual(service._allowed_roots(), [])
        self.assertEqual(code(call(self.root / 'oa.txt', 'allowed')), 'out_of_scope_path')

    def test_wrong_extension_directory_and_missing(self):
        (self.root / 'x.pdf').write_bytes(b'%PDF'); (self.root / 'noext').write_text(OA)
        for n in ('x.pdf', 'noext'): self.assertEqual(code(call(self.root / n, str(self.root))), 'out_of_scope_path', n)
        (self.root / 'dir.txt').mkdir()
        self.assertNotEqual(call(self.root / 'dir.txt', str(self.root))['status'], 'ok')
        self.assertEqual(code(call(self.root / 'absent.txt', str(self.root))), 'not_found')

    def test_nul_byte_and_non_string_paths(self):
        with mock.patch.dict(os.environ, {'PG_TOOL_INPUT_ROOTS': str(self.root)}):
            e = service.call('pg_assess_office_action', {'file_path': str(self.root / 'oa.txt') + '\x00.pdf'})
            self.assertNotEqual(e['status'], 'ok')
            self.assertNotEqual(service.call('pg_assess_office_action', {'file_path': 7})['status'], 'ok')

    @unittest.skipUnless(os.name == 'nt', 'Windows-only path forms')
    def test_windows_device_names_and_streams_are_refused(self):
        for n in ('CON.txt', 'NUL.txt', 'oa.txt:hidden.txt', 'oa.txt::$DATA.txt'):
            e = call(self.root / n, str(self.root)); self.assertNotEqual(e['status'], 'ok', n)
        e = call('\\\\?\\' + str(self.outside / 'secret.txt'), str(self.root)); self.assertEqual(code(e), 'out_of_scope_path')

    @unittest.skipUnless(os.name == 'nt', 'Windows paths are case-insensitive')
    def test_windows_case_variants_resolve_to_the_same_file(self):
        self.assertEqual(call(str(self.root / 'oa.txt').upper(), str(self.root).lower())['status'], 'ok')
        self.assertEqual(code(call(str(self.outside / 'secret.txt').upper(), str(self.root).lower())), 'out_of_scope_path')

    def test_oversize_file_is_refused_without_reading_it_whole(self):
        big = self.root / 'big.txt'
        with open(big, 'wb') as fh: fh.write(b'a' * (service.MAX_INPUT_BYTES + 1))
        self.assertEqual(code(call(big, str(self.root))), 'too_large')

    def test_front_matter_is_stripped_and_content_not_echoed(self):
        (self.root / 'fm.md').write_text('---\ntitle: x\n---\nCLIENTWORD ' + OA, encoding='utf-8')
        e = call(self.root / 'fm.md', str(self.root)); self.assertEqual(e['status'], 'ok'); self.assertNotIn('CLIENTWORD', repr(e))


if __name__ == '__main__':
    unittest.main()
