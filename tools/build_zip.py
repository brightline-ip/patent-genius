#!/usr/bin/env python3
"""Build the release zip from what is COMMITTED: the files of git HEAD that RELEASE_FILES.txt (the allowlist) allows, nothing else.

    python tools/build_zip.py [--out <folder or .zip>] [--version 0.1.0] [--check]

The archive is made by `git archive --format=zip --prefix=patent-genius-<version>/ HEAD -- <files>`, so no uncommitted change, no
untracked file and nothing outside the allowlist can enter it, and line endings follow .gitattributes (LF; CRLF for .cmd). The files
are the intersection of `git ls-tree -r HEAD` and the allowlist. The tool then reads the zip back and checks that its names are
exactly that list under the prefix, and prints the SHA-256. With --check it only prints what would be included.
Refuses to run when a tracked file differs from HEAD (build what you tested) unless --allow-dirty is given."""
import argparse, hashlib, re, subprocess, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = 'patent-genius'


def glob_to_re(pat):
    out, i = '', 0
    while i < len(pat):
        if pat.startswith('**', i): out += '.*'; i += 2
        elif pat[i] == '*': out += '[^/]*'; i += 1
        elif pat[i] == '?': out += '[^/]'; i += 1
        else: out += re.escape(pat[i]); i += 1
    return re.compile(out + r'\Z')


def git(*args, check=True):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, check=check)


def allowlist():
    pats = [l.strip() for l in (ROOT / 'RELEASE_FILES.txt').read_text(encoding='utf-8').splitlines()]
    return [(p, glob_to_re(p)) for p in pats if p and not p.startswith('#')]


def head_files():
    out = git('ls-tree', '-r', '-z', '--name-only', 'HEAD').stdout.decode('utf-8')
    return sorted(p for p in out.split('\0') if p)


def release_set():
    allow = allowlist(); files = head_files()
    chosen = [f for f in files if any(rx.match(f) for _, rx in allow)]
    left_out = [f for f in files if f not in set(chosen)]
    stale = [p for p, rx in allow if not any(rx.match(f) for f in files)]
    return chosen, left_out, stale


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default=str(ROOT.parent / '_dist'), help='an output folder, or a .zip path (default: ../_dist)')
    ap.add_argument('--version', default=None, help='default: the version in package.json')
    ap.add_argument('--check', action='store_true', help='list what would be included and stop')
    ap.add_argument('--allow-dirty', action='store_true')
    a = ap.parse_args(argv)
    import json
    version = a.version or json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    dirty = [l for l in git('status', '--porcelain', '--untracked-files=no').stdout.decode('utf-8').splitlines() if l.strip()]
    if dirty and not a.allow_dirty:
        print('refusing: tracked files differ from HEAD (the zip is built from HEAD, so it would not be what you tested):\n  ' + '\n  '.join(dirty[:10]), file=sys.stderr); return 2
    chosen, left_out, stale = release_set()
    if stale: print('refusing: allowlist lines that match no committed file: ' + ', '.join(stale), file=sys.stderr); return 2
    if not chosen: print('refusing: nothing to include', file=sys.stderr); return 2
    print(f'{len(chosen)} files from HEAD {git("rev-parse", "--short", "HEAD").stdout.decode().strip()}; {len(left_out)} committed file(s) not in the allowlist are left out' + (': ' + ', '.join(left_out[:8]) if left_out else ''))
    if a.check: print('\n'.join(chosen)); return 0
    out = Path(a.out)
    zip_path = out if out.suffix == '.zip' else out / f'{NAME}-{version}.zip'
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists(): zip_path.unlink()
    prefix = f'{NAME}-{version}/'
    r = subprocess.run(['git', '-C', str(ROOT), '-c', 'core.autocrlf=false', 'archive', '--format=zip', f'--prefix={prefix}', '-o', str(zip_path), 'HEAD', '--', *chosen], capture_output=True)
    if r.returncode: print(r.stderr.decode('utf-8', 'replace'), file=sys.stderr); return 1
    with zipfile.ZipFile(zip_path) as z:
        names = sorted(n for n in z.namelist() if not n.endswith('/'))
        bad = z.testzip()
    expected = sorted(prefix + c for c in chosen)
    if names != expected or bad:
        print('the zip does not match the allowlist set: ' + str(sorted(set(names) ^ set(expected))[:10]) + (f'; corrupt member {bad}' if bad else ''), file=sys.stderr); return 1
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f'{zip_path}\n{digest}  {zip_path.name}\n{len(names)} files under {prefix}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
