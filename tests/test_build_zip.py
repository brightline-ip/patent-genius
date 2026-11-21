"""tools/build_zip.py builds the release zip from git HEAD intersected with RELEASE_FILES.txt. Tested in a throwaway git repository."""
import hashlib, os, shutil, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which('git')


def git(repo, *a):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.com', *a], capture_output=True, text=True, check=True)


@unittest.skipUnless(GIT, 'git not available')
class BuildZip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'; (self.repo / 'tools').mkdir(parents=True)
        shutil.copyfile(ROOT / 'tools' / 'build_zip.py', self.repo / 'tools' / 'build_zip.py')
        (self.repo / 'package.json').write_text('{"version": "9.9.9"}', encoding='utf-8')
        (self.repo / 'RELEASE_FILES.txt').write_text('# allowlist\nRELEASE_FILES.txt\npackage.json\n*.py\ndocs/*.md\nSetup.cmd\ntools/*.py\n', encoding='utf-8')
        (self.repo / 'docs').mkdir(); (self.repo / 'docs' / 'a.md').write_text('doc\n'); (self.repo / 'x.py').write_text('print(1)\n'); (self.repo / 'secret.db').write_text('private')
        (self.repo / 'Setup.cmd').write_bytes(b'@echo off\r\necho hi\r\n')
        (self.repo / '.gitattributes').write_text('* text=auto eol=lf\n*.cmd text eol=crlf\n')
        git(self.repo, 'init', '-q', '-b', 'main'); git(self.repo, 'add', '-A', '-f'); git(self.repo, 'commit', '-q', '-m', 'one')
        self.out = Path(self.tmp.name) / 'dist'

    def build(self, *extra):
        return subprocess.run([sys.executable, str(self.repo / 'tools' / 'build_zip.py'), '--out', str(self.out), *extra], capture_output=True, text=True)

    def test_only_committed_and_allowlisted_files_go_in_under_the_prefix_with_the_right_line_endings(self):
        (self.repo / 'untracked.py').write_text('not committed\n')              # allowlisted by pattern but not committed: left out
        r = self.build(); self.assertEqual(r.returncode, 0, r.stderr)
        z = self.out / 'patent-genius-9.9.9.zip'; self.assertTrue(z.is_file())
        with zipfile.ZipFile(z) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith('/')); self.assertEqual(names, sorted('patent-genius-9.9.9/' + n for n in ('RELEASE_FILES.txt', 'Setup.cmd', 'docs/a.md', 'package.json', 'tools/build_zip.py', 'x.py')))
            self.assertEqual(zf.read('patent-genius-9.9.9/Setup.cmd'), b'@echo off\r\necho hi\r\n'); self.assertEqual(zf.read('patent-genius-9.9.9/x.py'), b'print(1)\n')
        self.assertIn(hashlib.sha256(z.read_bytes()).hexdigest(), r.stdout); self.assertIn('2 committed file(s) not in the allowlist are left out: .gitattributes, secret.db', r.stdout)

    def test_a_tracked_change_not_yet_committed_stops_the_build(self):
        (self.repo / 'x.py').write_text('print(2)\n'); r = self.build(); self.assertEqual(r.returncode, 2); self.assertIn('differ from HEAD', r.stderr); self.assertFalse(self.out.exists())
        self.assertEqual(self.build('--allow-dirty').returncode, 0)

    def test_a_stale_allowlist_line_stops_the_build_and_check_lists_without_building(self):
        (self.repo / 'RELEASE_FILES.txt').write_text('RELEASE_FILES.txt\nnothing/*.zzz\n'); git(self.repo, 'add', '-A'); git(self.repo, 'commit', '-q', '-m', 'two')
        r = self.build(); self.assertEqual(r.returncode, 2); self.assertIn('nothing/*.zzz', r.stderr)
        (self.repo / 'RELEASE_FILES.txt').write_text('RELEASE_FILES.txt\npackage.json\n'); git(self.repo, 'add', '-A'); git(self.repo, 'commit', '-q', '-m', 'three')
        r = self.build('--check'); self.assertEqual(r.returncode, 0); self.assertIn('package.json', r.stdout); self.assertFalse(self.out.exists())

    def test_the_same_commit_gives_the_same_zip(self):
        self.assertEqual(self.build().returncode, 0); a = (self.out / 'patent-genius-9.9.9.zip').read_bytes()
        self.assertEqual(self.build().returncode, 0); self.assertEqual(hashlib.sha256(a).hexdigest(), hashlib.sha256((self.out / 'patent-genius-9.9.9.zip').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
