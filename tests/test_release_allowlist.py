"""Release hygiene: what may ship is fixed by RELEASE_FILES.txt (an allowlist), and a generic denylist of data folders,
data files, absolute user paths, e-mail addresses and credential shapes must not appear in any shipped file.
These rules are generic on purpose. Patterns that are specific to one person or one data set belong in a private scanner
outside the repository, never in a file that ships."""
import os, re, subprocess, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()


def glob_to_re(pat):
    out, i = '', 0
    while i < len(pat):
        if pat.startswith('**', i): out += '.*'; i += 2
        elif pat[i] == '*': out += '[^/]*'; i += 1
        elif pat[i] == '?': out += '[^/]'; i += 1
        else: out += re.escape(pat[i]); i += 1
    return re.compile(out + r'\Z')


def allowlist():
    lines = [l.strip() for l in (ROOT / 'RELEASE_FILES.txt').read_text(encoding='utf-8').splitlines()]
    return [(l, glob_to_re(l)) for l in lines if l and not l.startswith('#')]


def release_files():
    """Tracked files when this is a git checkout; otherwise every file in the tree (an extracted archive)."""
    if (ROOT / '.git').exists():
        try:
            out = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                 capture_output=True, check=True).stdout.decode('utf-8')
            return sorted(p for p in out.split('\0') if p and (ROOT / p).is_file())
        except (OSError, subprocess.CalledProcessError):
            pass
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ('__pycache__', '.git')]
        for f in filenames: found.append((Path(dirpath) / f).relative_to(ROOT).as_posix())
    return sorted(found)


DENY_PATHS = [  # generic: data folders, databases, internal-notes names
    re.compile(r'(^|/)(corpus|research|coordination|public-originals|integrations|locks|inputs|packs)/', re.I),
    re.compile(r'(^|/)templates/meta/', re.I),
    re.compile(r'(^|/)templates/(oa-[a-z0-9-]*-response|restriction-election-response|after-final-rce-appeal|amendment-practice|appeal-brief|examiner-interview)\.', re.I),
    re.compile(r'\.(db|sqlite|sqlite3|jsonl|gz|zip|pdf|docx|tif|tiff|log)$', re.I),
    re.compile(r'(^|/)(HANDOFFS?|NOTES|INTERNAL|TODO|SCRATCH)\.md$', re.I),
    re.compile(r'(^|/)(\.env|keys\.env|secrets?\.[a-z]+)$', re.I),
]
DENY_TEXT = {
    'absolute user path (Windows)': re.compile(r'\b[A-Za-z]:[\\/]+(Users|Documents and Settings)[\\/]+[^\\/\s"\']+', re.I),
    'absolute user path (POSIX)': re.compile(r'(?<![\w.])/(home|Users)/(?!user\b|you\b|name\b|me\b|<)[A-Za-z0-9._-]+/'),
    'private key block': re.compile('-----BEGIN [A-Z ]*PRIV' + 'ATE KEY'),
    'provider key shape': re.compile(r'\b(sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{20,})'),
    'bearer token': re.compile(r'Bearer\s+[A-Za-z0-9._~+/=-]{24,}'),
}
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b')
EMAIL_OK = re.compile(r'(^|\.)(example\.(com|org|invalid)|invalid|localhost|users\.noreply\.github\.com)$|^noreply\.', re.I)


def text_of(rel):
    data = (ROOT / rel).read_bytes()
    if b'\0' in data[:4096]: return None
    return data.decode('utf-8', errors='replace')


class ReleaseAllowlist(unittest.TestCase):
    def test_every_file_is_allowlisted(self):
        rules = allowlist(); bad = [f for f in release_files() if not any(rx.match(f) for _, rx in rules)]
        self.assertEqual(bad, [], 'files not covered by RELEASE_FILES.txt')

    def test_no_stale_allowlist_lines(self):
        files = release_files(); stale = [l for l, rx in allowlist() if not any(rx.match(f) for f in files)]
        self.assertEqual(stale, [], 'RELEASE_FILES.txt lines that match no file')

    def test_denylisted_paths_absent(self):
        bad = [f for f in release_files() if any(rx.search(f) for rx in DENY_PATHS)]
        self.assertEqual(bad, [])

    def test_denylisted_text_absent(self):
        problems = []
        for f in release_files():
            if f == SELF: continue
            t = text_of(f)
            if t is None: continue
            for name, rx in DENY_TEXT.items():
                if rx.search(t): problems.append(f'{f}: {name}')
            for m in EMAIL.finditer(t):
                if not EMAIL_OK.search(m.group(1)) and f not in ('LICENSE', 'NOTICE'): problems.append(f'{f}: e-mail address')
        self.assertEqual(problems, [])

    def test_allowlist_has_no_absolute_or_parent_patterns(self):
        for line, _ in allowlist():
            self.assertFalse(line.startswith('/') or '..' in line or ':' in line, line)


if __name__ == '__main__':
    unittest.main()
