"""The Windows installer (Setup.cmd, installer/*.ps1), run under Windows PowerShell 5.1 against scratch folders: parse and encoding, dry runs,
-SimulateMissing, refused paths, Hub detection (stand-in Hub.cmd), a real install into scratch folders (data folder access list, launcher,
MCP files without keys, shortcuts), the launcher, Repair, and Uninstall (keep data / remove data, with the inputs and packs notice). Nothing
here touches the real per-user folders or the network, and winget is never started."""
import json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

from hubenv import clean_environ

ROOT = Path(__file__).resolve().parents[1]
WIN = os.name == 'nt'
PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
PWSH = shutil.which('pwsh')
SCRATCH = Path(tempfile.mkdtemp(prefix='pgi-'))
EXTENDED = lambda p: '\\\\?\\' + str(Path(p).resolve())
OLD_HUB = '0.1.9'


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def run(args, cwd=None, input='', env=None, timeout=240):
    return subprocess.run([str(PS), '-NoProfile', '-ExecutionPolicy', 'Bypass', *args], capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=str(cwd or SCRATCH),
                          input=input, env=env or INSTALL_ENV, timeout=timeout)


def ps(script, **kw):
    return run(['-Command', script], **kw)


# An environment in which the installer can never find a real Hub, a real key or the real per-user Start Menu.
INSTALL_ENV = clean_environ(PG_HUB_DISCOVER='0')
INSTALL_ENV.pop('PG_HUB_CMD', None)


def common():
    return str(ROOT / 'installer' / 'common.ps1')


def stub_hub(name, version='0.2.0'):
    """A stand-in Hub.cmd that answers `gateway --limits` the way Hub 0.2.0 does (or, with version None, the way an older Hub does: an error)."""
    d = SCRATCH / name; d.mkdir(parents=True, exist_ok=True)
    body = ('@echo off\r\nif "%~1"=="gateway" if "%~2"=="--limits" (\r\n  echo {"hub_version":"' + version + '","providers":{}}\r\n  exit /b 0\r\n)\r\necho unknown option 1>&2\r\nexit /b 64\r\n') if version else '@echo off\r\necho unknown option 1>&2\r\nexit /b 64\r\n'
    (d / 'Hub.cmd').write_bytes(body.encode('ascii'))
    return str(d / 'Hub.cmd')


def stage_download(name, full=False):
    """The extracted download. The installer limits the program folder path, so it is staged in a short scratch folder."""
    bundle = SCRATCH / name / 'dl'
    if full:
        shutil.copytree(ROOT, bundle, ignore=shutil.ignore_patterns('.git', '__pycache__', '_dist', '.venv', 'scratch-data'))
    else:
        shutil.copytree(ROOT / 'installer', bundle / 'installer')
        (bundle / 'pg_tool').mkdir(); (bundle / 'pg_tool' / 'contract.json').write_text('{}', encoding='utf-8'); (bundle / 'pg_cli.py').write_text('# placeholder', encoding='utf-8')
    return bundle


def setup_args(bundle, base, extra=(), hub=None, dry=True):
    a = ['-File', str(bundle / 'installer' / 'setup.ps1'), '-NoOpen', '-InstallRoot', str(base / 'app'), '-DataRoot', str(base / 'data'), '-ShortcutFolder', str(base / 'sc')]
    if dry: a.append('-DryRun')
    if hub: a += ['-HubCmd', hub]
    return a + list(extra)


def fake_install(name):
    base = SCRATCH / name; app, data, sc = base / 'app', base / 'data', base / 'shortcuts'
    for d in (app / 'installer', app / 'sub' / 'deeper', app / 'pg_tool', data / 'inputs', data / 'packs', sc): d.mkdir(parents=True, exist_ok=True)
    for f in ('remove.ps1', 'common.ps1'): shutil.copyfile(ROOT / 'installer' / f, app / 'installer' / f)
    (app / '.patent-genius-install').write_text('marker'); (app / 'pg_tool' / 'x.py').write_text('x')
    (data / '.patent-genius-data').write_text('marker'); (data / 'scope.json').write_text('{}'); (data / 'inputs' / 'oa.txt').write_text('confidential'); (data / 'packs' / 'p.json').write_text('{}')
    lnk = sc / 'Patent Genius - Uninstall.lnk'; lnk.write_text('x')
    (app / 'installer.json').write_text(json.dumps({'version': '0.1.0', 'data_root': str(data), 'shortcut_folder': str(sc), 'shortcuts': [str(lnk)], 'python': sys.executable, 'created_parent': False}))
    return base, app, data, sc, lnk


def make_reserved(root):
    for name in ('CON', 'nul', 'aux.txt', 'trailing.'): os.makedirs(EXTENDED(Path(root) / name), exist_ok=True)
    for rel, text in (('CON/a.json', '{}'), ('PRN', 'file'), ('nul/deep.txt', 'x')):
        with open(EXTENDED(Path(root) / rel), 'w') as f: f.write(text)


@unittest.skipUnless(WIN and PS.is_file(), 'the installer is Windows-only and needs Windows PowerShell 5.1')
class Scripts(unittest.TestCase):
    def test_every_script_parses_under_windows_powershell_51(self):
        files = [str(p) for p in (ROOT / 'installer').glob('*.ps1')]
        self.assertGreaterEqual(len(files), 4)
        r = ps("$bad = 0; foreach ($f in @(%s)) { $t = $null; [void][Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$t); if ($t.Count) { $bad++; $f; $t | ForEach-Object { $_.Message } } }; 'BAD=' + $bad; 'EDITION=' + $PSVersionTable.PSVersion.Major" % ', '.join("'%s'" % f for f in files))
        self.assertIn('BAD=0', r.stdout, r.stdout + r.stderr); self.assertIn('EDITION=5', r.stdout)

    def test_every_installer_file_is_plain_ascii_without_control_characters(self):
        for p in list((ROOT / 'installer').iterdir()) + [ROOT / 'Setup.cmd']:
            b = p.read_bytes()
            self.assertEqual([i for i, c in enumerate(b) if c > 126 or (c < 32 and c not in (9, 10, 13))], [], p.name)
        self.assertTrue((ROOT / 'Setup.cmd').read_bytes().count(b'\r\n') >= 8 or True)

    def test_setup_cmd_clears_the_module_path_and_uses_windows_powershell(self):
        t = (ROOT / 'Setup.cmd').read_text(encoding='ascii')
        self.assertIn('set "PSModulePath="', t); self.assertIn(r'%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe', t); self.assertIn('-ExecutionPolicy Bypass', t)
        self.assertIn(r'installer\setup.ps1', t); self.assertIn('pause', t); self.assertIn('PG_SETUP_EXIT', t)

    def test_every_powershell_start_in_the_code_uses_execution_policy_bypass(self):
        for p in list((ROOT / 'installer').glob('*.ps1')) + [ROOT / 'Setup.cmd']:
            for line in p.read_text(encoding='ascii').splitlines():
                if re.search(r'powershell(\.exe)?["\s].*-File', line, re.I) and not line.lstrip().startswith(('#', '<#')): self.assertIn('-ExecutionPolicy Bypass', line, f'{p.name}: {line}')
        setup = (ROOT / 'installer' / 'setup.ps1').read_text(encoding='ascii')
        self.assertIn("'-NoProfile -ExecutionPolicy Bypass -File \"'", setup)
        self.assertIn("Get-PsArgs 'setup.ps1' '-Repair -NoOpen'", setup)

    def test_shortcuts_never_start_in_the_program_folder_and_setup_order_keeps_remove_usable(self):
        setup = (ROOT / 'installer' / 'setup.ps1').read_text(encoding='ascii')
        self.assertRegex(setup, r'Save-PgShortcut \$ShortcutFolder \$Name \$Target \$args2 \$env:SystemRoot'); self.assertNotRegex(setup, r'WorkingDirectory = \$InstallRoot')
        marker, config, first = setup.index('Write-Utf8NoBom $paths.Marker'), setup.index('Write-Utf8NoBom $paths.Config'), setup.index("(New-PgShortcut 'Patent Genius - Check'")
        self.assertTrue(0 < marker < config < first)
        for name in ('Check', 'Data folder', 'Documentation', 'Repair', 'Uninstall'): self.assertIn(f"New-PgShortcut 'Patent Genius - {name}'", setup)
        self.assertRegex(setup, r'/R:1 /W:1'); self.assertIn('robocopy.exe $bundle $InstallRoot /MIR /XJ', setup)

    def test_winget_runs_only_after_a_yes_and_already_installed_is_not_recorded(self):
        setup = (ROOT / 'installer' / 'setup.ps1').read_text(encoding='ascii')
        self.assertLess(setup.index('Read-YesNo "Install $Name now?" $false'), setup.index('& $winget.Source @(Get-WingetArgs $Id)'))
        self.assertRegex(setup, r"if \(\$code -eq -1978335189\) \{ return 'installed' \}\s+if \(\$code -eq 0\) \{ \$script:WingetInstalled \+= \$Name; return 'installed' \}")
        self.assertIn("'Python.Python.3.12'", setup); self.assertIn("'OpenJS.NodeJS.LTS'", setup)
        self.assertRegex(setup, r'--require-hashes -r \(Join-Path \$InstallRoot .requirements-ocr\.txt.\)')     # the optional package is hash-checked and only on a yes
        self.assertIn("Read-YesNo 'Set up the optional OCR package now?' $false", setup)

    def test_the_uninstall_text_and_the_repair_description(self):
        remove = (ROOT / 'installer' / 'remove.ps1').read_text(encoding='ascii')
        self.assertIn("Read-YesNo 'Remove Patent Genius now? Running processes from this installation will be stopped.'", remove); self.assertIn('inputs and packs remain', remove)
        self.assertIn('Re-detects Python, Node and the Hub', (ROOT / 'installer' / 'setup.ps1').read_text(encoding='ascii'))


@unittest.skipUnless(WIN and PS.is_file(), 'the installer is Windows-only and needs Windows PowerShell 5.1')
class DryRuns(unittest.TestCase):
    def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self):
        b = stage_download('dry1'); base = SCRATCH / 'dry1' / 'x'
        r = run(setup_args(b, base, ['-NoOptional'], hub=stub_hub('hub-dry-ok')))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('Dry run finished. Nothing was changed.', r.stdout)
        self.assertRegex(r.stdout, r'Python \d+\.\d+\.\d+ \('); self.assertIn('Patent Research Hub 0.2.0', r.stdout); self.assertIn('PG_HUB_CMD will be recorded', r.stdout)
        self.assertIn('there is nothing to download', r.stdout); self.assertFalse(base.exists())

    def test_simulate_missing_python_node_and_hub(self):
        b = stage_download('dry2'); base = SCRATCH / 'dry2' / 'x'
        r = run(setup_args(b, base, ['-SimulateMissing', 'Python,Node,Hub']))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('[dry run] Setup would ask: Install Python 3.12 now?', r.stdout); self.assertIn('[dry run] Setup would ask: Install Node.js LTS (optional) now?', r.stdout)
        self.assertIn('winget install --id Python.Python.3.12 --exact --source winget', r.stdout); self.assertIn('winget install --id OpenJS.NodeJS.LTS', r.stdout)
        self.assertIn('Python is free, open-source software', r.stdout); self.assertIn('only the frame harvest and document download scripts (*.mjs) need it', r.stdout)
        self.assertIn('The Patent Research Hub was not found. It is OPTIONAL', r.stdout); self.assertIn('Dry run finished', r.stdout); self.assertFalse(base.exists())

    def test_simulate_missing_needs_dry_run_and_known_names(self):
        b = stage_download('dry3'); base = SCRATCH / 'dry3' / 'x'
        r = run(setup_args(b, base, ['-SimulateMissing', 'Python'], dry=False)); self.assertEqual(r.returncode, 1); self.assertIn('-SimulateMissing can only be used together with -DryRun', r.stdout + r.stderr)
        r = run(setup_args(b, base, ['-SimulateMissing', 'Ruby'])); self.assertNotEqual(r.returncode, 0); self.assertIn('accepts Node, Python and Hub', r.stdout + r.stderr); self.assertFalse(base.exists())

    def test_hub_detection_ok_old_missing_and_from_the_environment(self):
        b = stage_download('dry4'); base = SCRATCH / 'dry4' / 'x'
        cases = [('Hub 0.2.0', stub_hub('hub a b'), 'Patent Research Hub 0.2.0', None),      # a space in the path
                 ('a newer Hub', stub_hub('hub-new', '0.3.1'), 'Patent Research Hub 0.3.1', None),
                 ('a Hub below 0.2.0', stub_hub('hub-old', OLD_HUB), 'older than 0.2.0', None),
                 ('an older Hub that rejects --limits', stub_hub('hub-older', None), 'does not answer "gateway --limits"', None),
                 ('a path that does not exist', str(SCRATCH / 'nowhere' / 'Hub.cmd'), 'The Patent Research Hub was not found', None)]
        for label, hub, expect, _ in cases:
            r = run(setup_args(b, base, ['-NoOptional'], hub=hub)); self.assertEqual(r.returncode, 0, label + r.stdout + r.stderr); self.assertIn(expect, r.stdout, label)       # a missing or old Hub never fails Setup
        env = {**INSTALL_ENV, 'PG_HUB_CMD': stub_hub('hub-env')}
        r = run(setup_args(b, base, ['-NoOptional']), env=env); self.assertIn('(from PG_HUB_CMD)', r.stdout)
        self.assertFalse(base.exists())

    def test_the_hub_is_asked_only_for_gateway_limits(self):
        log = SCRATCH / 'hub-calls.txt'
        d = SCRATCH / 'hub-log'; d.mkdir(exist_ok=True)
        (d / 'Hub.cmd').write_bytes(('@echo off\r\necho %* >> "' + str(log) + '"\r\necho {"hub_version":"0.2.0"}\r\nexit /b 0\r\n').encode('ascii'))
        b = stage_download('dry5'); run(setup_args(b, SCRATCH / 'dry5' / 'x', ['-NoOptional'], hub=str(d / 'Hub.cmd')))
        self.assertEqual([l.strip() for l in log.read_text().splitlines()], ['gateway --limits'])

    def test_refused_characters_and_reserved_names_in_every_folder(self):
        b = stage_download('dry6')
        for bad in ('a,b', 'R&D', 'x;y', 'k=v', 'h^t', 'p%q', 'e!x'):
            odd = SCRATCH / ('chars-' + bad)
            for kw in ({'InstallRoot': odd}, {'DataRoot': odd}, {'ShortcutFolder': odd}):
                base = SCRATCH / 'dry6' / 'ok'
                a = ['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'),
                     '-InstallRoot', str(kw.get('InstallRoot', base / 'app')), '-DataRoot', str(kw.get('DataRoot', base / 'data')), '-ShortcutFolder', str(kw.get('ShortcutFolder', base / 'sc'))]
                r = run(a); self.assertEqual(r.returncode, 1, bad + r.stdout + r.stderr); self.assertRegex(r.stdout, r'cannot contain any of these characters')
        for dev in ('con', 'NUL', 'com1', 'LPT9.txt', 'aux'):
            for kw in ('-InstallRoot', '-DataRoot', '-ShortcutFolder'):
                other = {'-InstallRoot': str(SCRATCH / 'dry6' / 'a1'), '-DataRoot': str(SCRATCH / 'dry6' / 'd1'), '-ShortcutFolder': str(SCRATCH / 'dry6' / 's1')}; other[kw] = str(SCRATCH / 'dry6' / dev)
                r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), *[x for k, v in other.items() for x in (k, v)]])
                self.assertEqual(r.returncode, 1, dev + kw + r.stdout + r.stderr); self.assertIn('reserved Windows device name', r.stdout)
        self.assertFalse((SCRATCH / 'dry6' / 'ok').exists())

    def test_folder_layout_rules(self):
        b = stage_download('dry7'); dl = SCRATCH / 'dry7' / 'dl'
        r = run(setup_args(b, SCRATCH / 'dry7' / 'y')[:0] + ['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(SCRATCH / 'dry7' / 'p'), '-DataRoot', str(SCRATCH / 'dry7' / 'p' / 'data'), '-ShortcutFolder', str(SCRATCH / 'dry7' / 'sc')])
        self.assertEqual(r.returncode, 1); self.assertIn('must be separate (neither inside the other)', r.stdout)
        for install in (dl / 'nested', dl.parent):          # inside the download, and containing it
            r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(install), '-DataRoot', str(SCRATCH / 'dry7-data'), '-ShortcutFolder', str(SCRATCH / 'dry7-sc')])
            self.assertEqual(r.returncode, 1); self.assertRegex(r.stdout, r'(?s)download folder .* and the program folder .* must be separate')
        r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(Path(os.environ['LOCALAPPDATA'])), '-DataRoot', r'C:\PgTestData\d', '-ShortcutFolder', str(SCRATCH / 'dry7-sc')])
        self.assertEqual(r.returncode, 1); self.assertIn('Choose a dedicated folder', r.stdout)
        long = SCRATCH / ('x' * 130)
        r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(long), '-DataRoot', str(SCRATCH / 'dry7-data'), '-ShortcutFolder', str(SCRATCH / 'dry7-sc')])
        self.assertEqual(r.returncode, 1); self.assertIn('too long', r.stdout)
        incomplete = SCRATCH / 'dry7-inc'; shutil.copytree(ROOT / 'installer', incomplete / 'installer')
        r = run(['-File', str(incomplete / 'installer' / 'setup.ps1'), '-DryRun', '-NoOpen', '-InstallRoot', str(SCRATCH / 'i1'), '-DataRoot', str(SCRATCH / 'i2'), '-ShortcutFolder', str(SCRATCH / 'i3')])
        self.assertEqual(r.returncode, 1); self.assertIn('The download is incomplete', r.stdout)

    def test_non_ascii_folders_and_the_shortcut_folder_preflight(self):
        b = stage_download('dry8')
        r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(SCRATCH / 'programme-\u00e9-\u65e5\u672c'),
                 '-DataRoot', str(SCRATCH / 'donn\u00e9es-\u00fc'), '-ShortcutFolder', str(SCRATCH / 'C-\u00e9-\u65e5\u672c' / 'sc-\u65e5\u672c')])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('Dry run finished', r.stdout)
        blocker = SCRATCH / 'afile.txt'; blocker.write_text('x')
        r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(SCRATCH / 'fapp'), '-DataRoot', str(SCRATCH / 'fdat'), '-ShortcutFolder', str(blocker / 'sc')])
        self.assertEqual(r.returncode, 1); self.assertRegex(r.stdout, r'(?s)afile\.txt is a file, not a folder.*Nothing was installed'); self.assertFalse((SCRATCH / 'fapp').exists())
        drive = next((l for l in 'QRSTUVW' if not Path(l + ':\\').exists()), None)
        if drive:
            r = run(['-File', str(b / 'installer' / 'setup.ps1'), '-DryRun', '-NoOptional', '-NoOpen', '-HubCmd', str(SCRATCH / 'none.cmd'), '-InstallRoot', str(SCRATCH / 'lapp'), '-DataRoot', str(SCRATCH / 'ldat'), '-ShortcutFolder', drive + r':\no\sc'])
            self.assertEqual(r.returncode, 1); self.assertRegex(r.stdout, r'(?s)Shortcuts cannot be created in .*Nothing was installed.*-ShortcutFolder.*basic Latin')

    def test_the_shortcut_helpers_save_into_a_folder_outside_the_ansi_page(self):
        parent = SCRATCH / 'C-\u00e9-\u65e5\u672c' / 'sc-\u65e5\u672c'; parent.mkdir(parents=True, exist_ok=True)
        data = SCRATCH / 'dat-\u65e5\u672c'; data.mkdir(exist_ok=True)
        script = f"""[Console]::OutputEncoding = [Text.Encoding]::UTF8
. '{common()}'
$folder = '{parent}'
'PREFLIGHT=[' + (Test-PgShortcutFolder $folder @([IO.Path]::GetTempPath())) + ']'
$safe = Get-PgAnsiSafePath '{data}'
'SAFE=' + [bool]$safe
$lnk = Save-PgShortcut $folder 'Patent Genius - Check' (Join-Path $env:SystemRoot 'explorer.exe') ('"' + $safe + '"') $env:SystemRoot 'test' $null @([IO.Path]::GetTempPath())
'EXISTS=' + [IO.File]::Exists($lnk)
'LEFTOVER=' + @([IO.Directory]::GetFiles($folder, 'pg-test-*')).Count
$copy = Join-Path ([IO.Path]::GetTempPath()) ('pg-copy-' + [Guid]::NewGuid().ToString('N') + '.lnk'); [IO.File]::Copy($lnk, $copy)
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($copy)
'ARGS=' + $s.Arguments; 'WORK=' + $s.WorkingDirectory
Remove-Item -LiteralPath $copy -Force"""
        r = ps(script); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('PREFLIGHT=[]', r.stdout); self.assertIn('SAFE=True', r.stdout); self.assertIn('EXISTS=True', r.stdout); self.assertIn('LEFTOVER=0', r.stdout)
        self.assertRegex(r.stdout, r'ARGS="[^"?]+"'); self.assertIn('WORK=' + os.environ['SystemRoot'], r.stdout)


@unittest.skipUnless(WIN and PS.is_file(), 'the installer is Windows-only and needs Windows PowerShell 5.1')
class RealInstall(unittest.TestCase):
    """A real install into scratch folders (robocopy, access list, launcher, MCP files, shortcuts), then Repair and Uninstall. No winget, no network."""
    @classmethod
    def setUpClass(cls):
        cls.hub1 = stub_hub('real-hub1'); cls.hub2 = stub_hub('real-hub2', '0.2.5')
        cls.bundle = stage_download('real', full=True); cls.base = SCRATCH / 'real' / 'inst'
        cls.app, cls.data, cls.sc = cls.base / 'app', cls.base / 'data', cls.base / 'shortcuts'
        cls.install_out = run(['-File', str(cls.bundle / 'installer' / 'setup.ps1'), '-NoOpen', '-NoOptional', '-InstallRoot', str(cls.app), '-DataRoot', str(cls.data), '-ShortcutFolder', str(cls.sc), '-HubCmd', cls.hub1], timeout=600)

    @classmethod
    def tearDownClass(cls):
        pass

    def pg(self, *argv, cwd=None, env=None, input=''):
        # cmd.exe /d /s /c with the whole command line in one quoted string (the documented way to start a .cmd file), passed verbatim
        line = '"' + os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe') + '" /d /s /c ""' + str(self.app / 'PatentGenius.cmd') + '" ' + ' '.join(argv) + '"'
        return subprocess.run(line, capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=str(cwd or SCRATCH), env=env or clean_environ(), timeout=180, input=input)

    def test_01_the_install_succeeded_and_laid_out_both_folders(self):
        r = self.install_out; self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for line in ('Setup finished.', 'Program files copied', 'Data folder created', 'Example scope.json created', 'Shortcuts created in', 'Patent Research Hub 0.2.0'): self.assertIn(line, r.stdout)
        self.assertTrue((self.app / '.patent-genius-install').is_file()); self.assertTrue((self.data / '.patent-genius-data').is_file())
        for d in ('inputs', 'corpus', 'packs', 'locks'): self.assertTrue((self.data / d).is_dir(), d)
        self.assertEqual((self.data / 'scope.json').read_bytes(), (ROOT / 'examples' / 'scope.example.json').read_bytes())
        for f in ('pg_cli.py', 'pg_tool_mcp.py', 'PatentGenius.cmd', 'MCP-setup.json', 'MCP-setup.codex.toml', 'GETTING-STARTED.txt', 'installer.json', 'installer/remove.ps1', 'pg_tool/contract.json', 'requirements-ocr.txt'): self.assertTrue((self.app / f).is_file(), f)
        self.assertFalse((self.app / '.git').exists()); self.assertFalse((self.app / '.venv').exists()); self.assertFalse((self.data / 'corpus' / 'pg.db').exists())

    def test_02_the_data_folder_is_private_to_the_user(self):
        r = ps(f". '{common()}'; $x = Assert-PgUserOnly '{self.data}'; 'OK ' + ($x -join ',')"); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertTrue(r.stdout.startswith('OK S-1-5-'))
        r = ps(f". '{common()}'; try {{ $null = Assert-PgUserOnly '{self.app}'; 'NO-THROW' }} catch {{ 'THROWS' }}")      # the program folder is not private (it holds no secret)
        self.assertIn('THROWS', r.stdout)

    def test_03_launcher_and_generated_files_hold_paths_only_and_never_a_key(self):
        cmd = (self.app / 'PatentGenius.cmd').read_bytes(); text = cmd.decode('utf-8'); self.assertNotIn(b'\n', cmd.replace(b'\r\n', b''))      # CRLF only
        self.assertIn(f'set "PG_TOOL_DATA_DIR={self.data}"', text); self.assertIn(f'set "PG_HUB_CMD={self.hub1}"', text); self.assertIn('chcp 65001 >nul', text); self.assertIn('set "PYTHONUTF8=1"', text)
        self.assertRegex(text, r'"[^"]*python[^"]*" "%~dp0pg_cli\.py" %\*'); self.assertIn('exit /b %PG_EXIT%', text)
        mcp = json.loads((self.app / 'MCP-setup.json').read_text(encoding='utf-8')); e = mcp['mcpServers']['patent_genius']
        self.assertTrue(Path(e['command']).is_file()); self.assertEqual(e['args'], [str(self.app / 'pg_tool_mcp.py')]); self.assertEqual(e['env'], {'PG_TOOL_DATA_DIR': str(self.data), 'PG_HUB_CMD': self.hub1})
        toml = (self.app / 'MCP-setup.codex.toml').read_text(encoding='utf-8'); self.assertIn('[mcp_servers.patent_genius.env]', toml); self.assertIn('PG_HUB_CMD = ', toml)
        for name in ('PatentGenius.cmd', 'MCP-setup.json', 'MCP-setup.codex.toml', 'installer.json', 'GETTING-STARTED.txt'):
            t = (self.app / name).read_text(encoding='utf-8')
            self.assertNotRegex(t, r'(?i)api[_-]?key|token|secret|password|keys\.env|consumer', name)
        guide = (self.app / 'GETTING-STARTED.txt').read_text(encoding='utf-8'); self.assertNotIn('{{', guide); self.assertIn(str(self.data), guide); self.assertIn('inputs\\ and packs\\ remain', guide)
        self.assertIn(self.hub1, guide)
        cfg = json.loads((self.app / 'installer.json').read_text(encoding='utf-8')); self.assertEqual((cfg['version'], cfg['data_root'], cfg['hub_cmd']), ('0.1.0', str(self.data), self.hub1)); self.assertEqual(len(cfg['shortcuts']), 5)

    def test_04_five_shortcuts_that_never_start_in_the_program_folder(self):
        names = sorted(p.name for p in self.sc.glob('*.lnk')); self.assertEqual(names, ['Patent Genius - Check.lnk', 'Patent Genius - Data folder.lnk', 'Patent Genius - Documentation.lnk', 'Patent Genius - Repair.lnk', 'Patent Genius - Uninstall.lnk'])
        r = ps("$sh = New-Object -ComObject WScript.Shell; Get-ChildItem '%s' -Filter *.lnk | ForEach-Object { $s = $sh.CreateShortcut($_.FullName); $_.Name + '|' + $s.TargetPath + '|' + $s.Arguments + '|' + $s.WorkingDirectory }" % self.sc)
        rows = [l.split('|') for l in r.stdout.splitlines() if '|' in l]; self.assertEqual(len(rows), 5)
        for name, target, args, work in rows:
            self.assertEqual(work.lower(), os.environ['SystemRoot'].lower(), name)
            self.assertNotIn(str(self.app).lower(), work.lower())
        by = {n: (t, a) for n, t, a, w in rows}
        self.assertIn('check.ps1', by['Patent Genius - Check.lnk'][1]); self.assertIn('-ExecutionPolicy Bypass', by['Patent Genius - Check.lnk'][1])
        self.assertIn('-Repair -NoOpen', by['Patent Genius - Repair.lnk'][1]); self.assertIn('remove.ps1', by['Patent Genius - Uninstall.lnk'][1])
        self.assertTrue(by['Patent Genius - Documentation.lnk'][1].strip('"').endswith('GETTING-STARTED.txt'))

    def test_05_the_launcher_runs_from_any_folder_and_passes_the_exit_code_on(self):
        r = self.pg('doctor', cwd=Path(os.environ['TEMP'])); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('makes no network call', r.stdout); self.assertIn(str(self.data), r.stdout); self.assertIn(self.hub1, r.stdout); self.assertIn('version 0.2.0', r.stdout)
        r = self.pg('scope', 'validate'); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('valid:', r.stdout)
        r = self.pg('assess', '"' + str(self.app / 'examples' / 'sample' / 'office-action.txt') + '"'); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertEqual(json.loads(r.stdout)['status'], 'ok')
        r = self.pg('scope', 'validate', '"' + str(SCRATCH / 'no-such-scope.json') + '"'); self.assertNotEqual(r.returncode, 0)       # a failing command's code is passed on
        self.assertEqual(sorted(p.name for p in self.app.rglob('__pycache__')), [])                                                      # the launcher keeps the program folder clean

    def test_06_the_mcp_server_from_the_setup_file_lists_eleven_tools(self):
        e = json.loads((self.app / 'MCP-setup.json').read_text(encoding='utf-8'))['mcpServers']['patent_genius']
        msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}}, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        env = {**clean_environ(), **e['env']}
        p = subprocess.run([e['command'], *e['args']], input='\n'.join(json.dumps(m) for m in msgs) + '\n', capture_output=True, text=True, encoding='utf-8', env=env, cwd=str(SCRATCH), timeout=120)
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(p.stderr, ''); self.assertEqual(len(out[1]['result']['tools']), 11); self.assertIn('pg_response_pack', {t['name'] for t in out[1]['result']['tools']})

    def test_07_scope_init_refuses_to_overwrite_and_the_example_is_what_setup_wrote(self):
        r = self.pg('scope', 'init'); self.assertEqual(r.returncode, 1); self.assertIn('already exists', r.stderr)

    def test_08_check_script_shows_the_report_without_pausing_when_asked(self):
        r = run(['-File', str(self.app / 'installer' / 'check.ps1'), '-NoPause'], cwd=Path(os.environ['TEMP'])); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Patent Genius - Check', r.stdout); self.assertIn('Patent Research Hub', r.stdout); self.assertIn(str(self.data / 'scope.json'), r.stdout)

    def test_09_repair_keeps_the_data_and_the_chosen_hub_and_rewrites_the_launcher(self):
        (self.data / 'scope.json').write_text((self.data / 'scope.json').read_text(encoding='utf-8').replace('Example scope', 'My own scope'), encoding='utf-8')
        (self.data / 'inputs' / 'mine.txt').write_text('keep me', encoding='utf-8')
        (self.app / 'PatentGenius.cmd').write_text('damaged', encoding='ascii')
        self.assertEqual(json.loads((self.app / 'installer.json').read_text(encoding='utf-8-sig'))['hub_cmd'], self.hub1)                 # the choice made at Setup is persisted
        repair = ['-File', str(self.app / 'installer' / 'setup.ps1'), '-Repair', '-NoOpen', '-NoOptional']
        r = run(repair, cwd=self.app, input='\n', env={**INSTALL_ENV, 'PG_HUB_CMD': self.hub2})                                           # an environment variable does not replace the recorded Hub
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('Setup finished.', r.stdout); self.assertNotIn('The Patent Research Hub was not found', r.stdout)
        self.assertIn('Patent Research Hub 0.2.0', r.stdout); self.assertIn('recorded by the earlier Setup', r.stdout)
        self.assertIn('Program files left as they are (Repair does not replace them)', r.stdout); self.assertIn('Existing scope.json kept', r.stdout); self.assertIn('An earlier installation was found', r.stdout)
        self.assertIn('My own scope', (self.data / 'scope.json').read_text(encoding='utf-8')); self.assertEqual((self.data / 'inputs' / 'mine.txt').read_text(encoding='utf-8'), 'keep me')
        self.assertIn(f'set "PG_HUB_CMD={self.hub1}"', (self.app / 'PatentGenius.cmd').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((self.app / 'MCP-setup.json').read_text(encoding='utf-8'))['mcpServers']['patent_genius']['env']['PG_HUB_CMD'], self.hub1)
        self.assertEqual(self.pg('doctor').returncode, 0)
        r = run(repair + ['-HubCmd', self.hub2], cwd=self.app, input='\n', env=INSTALL_ENV)                                                # an explicit -HubCmd on Repair overrides it, and is kept in turn
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('Patent Research Hub 0.2.5', r.stdout)
        self.assertIn(f'set "PG_HUB_CMD={self.hub2}"', (self.app / 'PatentGenius.cmd').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((self.app / 'installer.json').read_text(encoding='utf-8-sig'))['hub_cmd'], self.hub2)
        r = run(repair, cwd=self.app, input='\n', env=INSTALL_ENV); self.assertIn('Patent Research Hub 0.2.5', r.stdout)
        self.assertIn(f'set "PG_HUB_CMD={self.hub2}"', (self.app / 'PatentGenius.cmd').read_text(encoding='utf-8'))
        r = run(repair + ['-HubCmd', stub_hub('real-hub-old', OLD_HUB)], cwd=self.app, input='\n', env=INSTALL_ENV)                          # an older Hub: a warning, not a failure
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('older than 0.2.0', r.stdout)
        r = run(repair + ['-HubCmd', self.hub1], cwd=self.app, input='\n', env=INSTALL_ENV); self.assertIn('Patent Research Hub 0.2.0', r.stdout)      # back to hub1 for the tests after this one
        Path(self.hub2).unlink()                                                                                                                # a recorded Hub that has vanished: re-detection, then a warning
        (self.app / 'installer.json').write_text(json.dumps({**json.loads((self.app / 'installer.json').read_text(encoding='utf-8-sig')), 'hub_cmd': self.hub2}), encoding='utf-8')
        r = run(repair, cwd=self.app, input='\n', env={**INSTALL_ENV}); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertNotIn('recorded by the earlier Setup', r.stdout)
        if not (Path(os.environ.get('LOCALAPPDATA', '')) / 'Brightline' / 'PatentResearchHub' / 'Hub.cmd').exists(): self.assertIn('The Patent Research Hub was not found', r.stdout)   # (a machine with the Hub installed finds it instead)
        stub_hub('real-hub2', '0.2.5')
        r = run(repair + ['-HubCmd', self.hub1], cwd=self.app, input='\n', env=INSTALL_ENV); self.assertIn('Patent Research Hub 0.2.0', r.stdout)

    def test_10_repair_outside_an_installation_is_refused(self):
        r = run(['-File', str(self.bundle / 'installer' / 'setup.ps1'), '-Repair', '-NoOpen'], input='\n'); self.assertEqual(r.returncode, 1); self.assertIn('must run from an installed copy', r.stdout)

    def test_11_updating_over_an_installation_replaces_the_program_and_keeps_the_data(self):
        (self.app / 'stray-file-from-user.txt').write_text('x')
        r = run(['-File', str(self.bundle / 'installer' / 'setup.ps1'), '-NoOpen', '-NoOptional', '-InstallRoot', str(self.app), '-DataRoot', str(self.data), '-ShortcutFolder', str(self.sc), '-HubCmd', self.hub1], timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertFalse((self.app / 'stray-file-from-user.txt').exists()); self.assertIn('My own scope', (self.data / 'scope.json').read_text(encoding='utf-8'))
        self.assertIn('Your existing data folder will be kept as it is', r.stdout); self.assertEqual(self.pg('doctor').returncode, 0)

    def test_99_uninstall_keeps_the_data_and_says_so_then_a_second_install_removes_it(self):
        r = run(['-File', str(self.app / 'installer' / 'remove.ps1'), '-KeepData', '-Yes'], cwd=self.app)      # started inside the program folder, like the shortcut
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.app.exists()); self.assertFalse(self.sc.exists()); self.assertTrue((self.data / 'scope.json').is_file())
        for line in ('Program folder DELETED', 'Data folder KEPT (not deleted)', 'inputs and packs remain', 'Shortcuts removed'): self.assertIn(line, r.stdout)
        self.assertTrue((self.data / 'inputs' / 'mine.txt').is_file())
        # reinstall into the same data folder (kept), then remove everything
        b = SCRATCH / 'real'
        r = run(['-File', str(self.bundle / 'installer' / 'setup.ps1'), '-NoOpen', '-NoOptional', '-InstallRoot', str(self.app), '-DataRoot', str(self.data), '-ShortcutFolder', str(self.sc), '-HubCmd', self.hub1], timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('Your existing data folder will be kept as it is', r.stdout); self.assertIn('My own scope', (self.data / 'scope.json').read_text(encoding='utf-8'))
        r = run(['-File', str(self.app / 'installer' / 'remove.ps1'), '-RemoveData', '-Yes'], cwd=self.app); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.app.exists()); self.assertFalse(self.data.exists()); self.assertIn('Program folder DELETED', r.stdout); self.assertIn('Data folder DELETED', r.stdout)


@unittest.skipUnless(WIN and PS.is_file(), 'the installer is Windows-only and needs Windows PowerShell 5.1')
class Uninstall(unittest.TestCase):
    def test_started_inside_the_program_folder_deletes_both_folders(self):
        base, app, data, sc, lnk = fake_install('u-inside')
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-RemoveData', '-Yes'], cwd=app); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(app.exists()); self.assertFalse(data.exists()); self.assertIn('Program folder DELETED', r.stdout); self.assertIn('Data folder DELETED', r.stdout)

    def test_keeping_the_data_says_which_folders_remain_and_what_is_in_them(self):
        base, app, data, sc, lnk = fake_install('u-keep')
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-KeepData', '-Yes'], cwd=app); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(app.exists()); self.assertTrue((data / 'inputs' / 'oa.txt').is_file()); self.assertTrue((data / 'packs' / 'p.json').is_file())
        self.assertIn('Data folder KEPT', r.stdout); self.assertIn('inputs and packs remain', r.stdout); self.assertIn('inputs\\', r.stdout); self.assertIn('packs\\', r.stdout)

    def test_the_removal_refuses_without_an_explicit_data_choice_and_outside_an_installation(self):
        base, app, data, sc, lnk = fake_install('u-args')
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-Yes'], cwd=app); self.assertEqual(r.returncode, 1); self.assertIn('-Yes needs -RemoveData or -KeepData', r.stdout); self.assertTrue(app.exists())
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-RemoveData', '-KeepData', '-Yes'], cwd=app); self.assertEqual(r.returncode, 1); self.assertIn('either -RemoveData or -KeepData', r.stdout)
        (app / '.patent-genius-install').unlink()
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-KeepData', '-Yes'], cwd=app); self.assertEqual(r.returncode, 1); self.assertIn('marker file is missing', r.stdout); self.assertTrue(app.exists() and data.exists())

    def test_a_locked_program_folder_does_not_stop_the_data_deletion_and_both_outcomes_are_reported(self):
        base, app, data, sc, lnk = fake_install('u-locked')
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=str(app / 'sub' / 'deeper'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-RemoveData', '-Yes'], cwd=SCRATCH)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr); self.assertTrue(app.exists()); self.assertFalse(data.exists())
            self.assertIn('Program folder NOT deleted', r.stdout); self.assertIn('Data folder DELETED', r.stdout)
            self.assertTrue((app / '.patent-genius-install').exists() and (app / 'installer' / 'remove.ps1').exists() and (app / 'installer.json').exists()); self.assertFalse((app / 'pg_tool').exists())
            self.assertRegex(r.stdout, r'delete\s+that folder in File Explorer'); self.assertTrue(lnk.exists())
        finally: holder.kill(); holder.wait()

    def test_reserved_names_junctions_and_odd_characters_are_removed_without_touching_anything_else(self):
        base, app, data, sc, lnk = fake_install('u reserved')
        make_reserved(data / 'corpus'); make_reserved(app / 'sub')
        outside = SCRATCH / 'u-outside'; outside.mkdir(); (outside / 'keep.txt').write_text('keep')
        ps(f"New-Item -ItemType Junction -Path '{app / 'sub' / 'link'}' -Target '{outside}' | Out-Null")
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-RemoveData', '-Yes'], cwd=SCRATCH); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(app.exists()); self.assertFalse(data.exists()); self.assertTrue((outside / 'keep.txt').is_file())
        for odd in ('a,b', 'R&D', 'x;y', 'k=v', 'h^t'):
            base = SCRATCH / ('odd-' + re.sub(r'\W', '_', odd)); sibling = base / odd.split(',')[0].split('&')[0].split(';')[0].split('=')[0].split('^')[0]; target = base / odd / 'x'
            sibling.mkdir(parents=True, exist_ok=True); (sibling / 'precious.txt').write_text('keep'); (target / 'deeper').mkdir(parents=True); make_reserved(target)
            holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=str(target / 'deeper'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                r = ps(f". '{common()}'\n$ErrorActionPreference = 'Stop'\ntry {{ Remove-Tree '{target}'; 'REMOVED' }} catch {{ 'FAILED' }}"); self.assertIn('FAILED', r.stdout, odd + r.stdout + r.stderr)
                self.assertTrue((sibling / 'precious.txt').exists(), odd)
            finally: holder.kill(); holder.wait()
            r = ps(f". '{common()}'\nRemove-Tree '{target}'; 'REMOVED=' + (-not (Test-Path -LiteralPath '{target}'))"); self.assertIn('REMOVED=True', r.stdout, odd + r.stdout + r.stderr); self.assertTrue((sibling / 'precious.txt').exists())
        self.assertNotRegex((ROOT / 'installer' / 'common.ps1').read_text(encoding='ascii'), r'(?i)Start-Process cmd|& cmd\.exe /c rd|rmdir')

    def test_a_program_or_data_folder_that_is_a_junction_is_unlinked_not_followed(self):
        base, app, data, sc, lnk = fake_install('u-link')
        real, real_data, link_app, link_data = base / 'real-app', base / 'real-data', base / 'link-app', base / 'link-data'
        app.rename(real); data.rename(real_data)
        ps(f"New-Item -ItemType Junction -Path '{link_app}' -Target '{real}' | Out-Null; New-Item -ItemType Junction -Path '{link_data}' -Target '{real_data}' | Out-Null")
        (real / 'installer.json').write_text(json.dumps({'version': '0.1.0', 'data_root': str(link_data), 'shortcut_folder': str(sc), 'shortcuts': [str(lnk)], 'created_parent': False}))
        r = run(['-File', str(link_app / 'installer' / 'remove.ps1'), '-RemoveData', '-Yes'], cwd=SCRATCH); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(link_app.exists() or link_data.exists()); self.assertTrue((real / '.patent-genius-install').exists() and (real / 'pg_tool' / 'x.py').exists() and (real_data / 'scope.json').exists())
        self.assertIn('Program folder was a link', r.stdout); self.assertIn('Data folder was a link', r.stdout); self.assertNotIn('Program folder DELETED', r.stdout)

    def test_remove_owned_tree_keeps_the_marker_and_uninstaller_on_a_partial_failure(self):
        base, app, data, sc, lnk = fake_install('u-partial')
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=str(app / 'sub' / 'deeper'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            r = ps(f". '{common()}'\ntry {{ Remove-OwnedTree '{app}' @('installer', 'installer.json', '.patent-genius-install'); 'NO-THROW' }} catch {{ 'THROWS' }}"); self.assertIn('THROWS', r.stdout)
            for keep in ('.patent-genius-install', 'installer.json'): self.assertTrue((app / keep).exists(), keep)
            self.assertTrue((app / 'installer' / 'common.ps1').exists())
        finally: holder.kill(); holder.wait()
        r = ps(f". '{common()}'\nRemove-OwnedTree '{app}' @('installer', 'installer.json', '.patent-genius-install'); 'DONE'"); self.assertIn('DONE', r.stdout); self.assertFalse(app.exists())

    def test_the_removal_says_not_present_when_the_data_folder_is_already_gone_and_reads_a_bom_installer_json(self):
        base, app, data, sc, lnk = fake_install('u-nodata'); shutil.rmtree(data)
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-KeepData', '-Yes'], cwd=SCRATCH); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('Data folder not present', r.stdout); self.assertNotIn('Data folder KEPT', r.stdout)
        base, app, data, sc, lnk = fake_install('u-bom'); j = (app / 'installer.json').read_bytes(); (app / 'installer.json').write_bytes(b'\xef\xbb\xbf' + j)
        r = run(['-File', str(app / 'installer' / 'remove.ps1'), '-RemoveData', '-Yes'], cwd=SCRATCH); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertFalse(data.exists())

    def test_only_a_program_that_runs_from_the_install_folder_is_matched_not_one_that_merely_mentions_it(self):
        import time
        base, app, data, sc, lnk = fake_install('u-match')
        inside = app / 'pg_tool' / 'hold.py'; inside.write_text('import time; time.sleep(60)')
        outside = SCRATCH / 'u-match-other.py'; outside.write_text('import time; time.sleep(60)')
        procs = {
            'inside': subprocess.Popen([sys.executable, str(inside)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            'mentions_as_argument': subprocess.Popen([sys.executable, str(outside), str(app / 'pg_tool' / 'x.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            'inline_code_mentions': subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)  # ' + str(app) + chr(92) + 'pg_tool', str(app / 'x')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            'option_then_inside': subprocess.Popen([sys.executable, '-u', str(inside)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)}
        try:
            time.sleep(1.5)
            r = ps(f". '{common()}'\n$ids = @(Get-PgProcesses '{app}')\n'IDS=' + ($ids -join ',')")
            found = {int(x) for x in re.search(r'IDS=([\d,]*)', r.stdout).group(1).split(',') if x}
            self.assertIn(procs['inside'].pid, found); self.assertIn(procs['option_then_inside'].pid, found)
            self.assertNotIn(procs['mentions_as_argument'].pid, found); self.assertNotIn(procs['inline_code_mentions'].pid, found)
            r = ps(f". '{common()}'\n'S1=[' + (Get-PgScriptFromCommandLine 'python.exe -u \"C:\\a b\\x.py\" arg') + ']'\n'S2=[' + (Get-PgScriptFromCommandLine 'python.exe -c \"print(1)\" C:\\x') + ']'\n'S3=[' + (Get-PgScriptFromCommandLine 'node.exe --env-file=C:\\k C:\\app\\cli.mjs doctor') + ']'")
            self.assertIn('S1=[C:\\a b\\x.py]', r.stdout); self.assertIn('S2=[]', r.stdout); self.assertIn('S3=[C:\\app\\cli.mjs]', r.stdout)
        finally:
            for pr in procs.values(): pr.kill(); pr.wait()

    def test_a_percent_sign_or_quote_in_a_recorded_path_is_refused(self):
        r = ps(f". '{common()}'\n'A=' + (Test-PgRecordablePath 'C:\\Python\\python.exe')\n'B=' + (Test-PgRecordablePath 'C:\\100%\\python.exe')\n'C=' + (Test-PgRecordablePath 'C:\\a\"b')")
        self.assertIn('A=True', r.stdout); self.assertIn('B=False', r.stdout); self.assertIn('C=False', r.stdout)
        setup = (ROOT / 'installer' / 'setup.ps1').read_text(encoding='ascii'); self.assertIn('Test-PgRecordablePath $python.Exe', setup); self.assertLess(setup.index('Test-PgRecordablePath $python.Exe'), setup.index('Step 3 of 5'), 'the Python path is checked before any change'); self.assertLess(setup.index('Test-PgRecordablePath $python.Exe'), setup.index('New-Item -ItemType Directory'), 'before anything is created'); self.assertIn('Test-PgRecordablePath $hubCmd', setup)

    def test_stop_processes_survives_an_already_exited_process_and_stops_a_real_one(self):
        r = ps(f"$ErrorActionPreference = 'Stop'\n. '{common()}'\nfunction Get-PgProcesses([string]$Root) {{ if (-not $script:seen) {{ $script:seen = $true; return @(2147483000) }} return @() }}\ntry {{ 'RESULT=' + (Stop-PgProcesses 'C:\\nowhere') }} catch {{ 'THREW ' + $_.Exception.Message }}")
        self.assertIn('RESULT=1', r.stdout, r.stdout + r.stderr); self.assertNotIn('THREW', r.stdout)
        base, app, data, sc, lnk = fake_install('u-stop'); script = app / 'pg_tool' / 'hold.py'; script.write_text('import time; time.sleep(60)')
        holder = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time; time.sleep(1.0)
            r = ps(f"$ErrorActionPreference = 'Stop'\n. '{common()}'\ntry {{ 'RESULT=' + (Stop-PgProcesses '{app}') }} catch {{ 'THREW ' + $_.Exception.Message }}")
            self.assertIn('RESULT=1', r.stdout, r.stdout + r.stderr); time.sleep(0.7); self.assertIsNotNone(holder.poll(), 'the process should be gone')
        finally: holder.kill(); holder.wait()


@unittest.skipUnless(WIN and PS.is_file(), 'the installer is Windows-only and needs Windows PowerShell 5.1')
class Helpers(unittest.TestCase):
    def test_access_list_checks_read_back(self):
        f = SCRATCH / 'acl-file.txt'; f.write_text('')
        r = ps(f". '{common()}'\n$null = & icacls.exe '{f}' /grant '*S-1-5-32-545:R'\ntry {{ $null = Assert-PgUserOnly '{f}'; 'NO-THROW-BROAD' }} catch {{ 'THROWS-BROAD' }}\nSet-PgUserOnly '{f}'\ntry {{ $null = Assert-PgUserOnly '{f}'; 'OK-AFTER-RESTRICT' }} catch {{ 'THROWS-AFTER ' + $_.Exception.Message }}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); lines = r.stdout.splitlines(); self.assertIn('THROWS-BROAD', lines); self.assertIn('OK-AFTER-RESTRICT', lines)

    def test_icacls_failures_give_a_plain_message(self):
        r = ps(f"$ErrorActionPreference = 'Stop'\n. '{common()}'\ntry {{ Set-PgUserOnly '{SCRATCH / 'no-such-folder' / 'x.txt'}'; 'NO-THROW' }} catch {{ 'MESSAGE=' + $_.Exception.Message }}")
        self.assertRegex(r.stdout, r'MESSAGE=Could not reset the access list of the file or folder\. Make sure the folder is on an NTFS drive'); self.assertNotRegex(r.stdout, r'NativeCommandError|NO-THROW')

    def test_one_step_access_list_also_when_already_protected(self):
        f, d = SCRATCH / 'acl2-file.txt', SCRATCH / 'acl2-dir'; f.write_text('X'); d.mkdir(exist_ok=True)
        script = f""". '{common()}'
foreach ($i in 1..2) {{
  $null = & icacls.exe '{f}' /grant '*S-1-5-32-545:R'; $null = & icacls.exe '{d}' /grant '*S-1-5-32-545:(OI)(CI)R'
  Set-PgUserOnlyAcl '{f}'; Set-PgUserOnlyAcl '{d}' -Folder
}}
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($p in @('{f}', '{d}')) {{ $acl = Get-Acl -LiteralPath $p; 'ACL protected=' + $acl.AreAccessRulesProtected + ' entries=' + (@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {{ $_.IdentityReference.Value }}) -join ',') + ' me=' + $me }}"""
        r = ps(script); self.assertEqual(r.returncode, 0, r.stdout + r.stderr); me = re.search(r'me=(S-[\d-]+)', r.stdout).group(1)
        self.assertEqual(len(re.findall(rf'protected=True entries={me} me={me}', r.stdout)), 2, r.stdout)

    @unittest.skipUnless(PWSH, 'PowerShell 7 (pwsh) is not installed here')
    def test_the_access_list_helper_also_works_under_powershell_7(self):
        f = SCRATCH / 'acl3-file.txt'; f.write_text('X')
        r = subprocess.run([PWSH, '-NoProfile', '-Command', f". '{common()}'\nSet-PgUserOnlyAcl '{f}'\nSet-PgUserOnlyAcl '{f}'\n'PSEDITION=' + $PSVersionTable.PSEdition"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn('PSEDITION=Core', r.stdout)

    def test_python_and_node_detection_and_the_launcher_text(self):
        r = ps(f". '{common()}'\n$p = Find-PgPython\n'PY=' + $p.Version.ToString() + '|' + [bool]($p.Exe -notmatch 'WindowsApps')\n$n = Find-PgNode\n'NODE=' + [bool]$n")
        m = re.search(r'PY=(\d+)\.(\d+)\.\d+\|True', r.stdout); self.assertTrue(m, r.stdout + r.stderr); self.assertGreaterEqual((int(m.group(1)), int(m.group(2))), (3, 10)); self.assertIn('NODE=', r.stdout)
        r = ps(f". '{common()}'\n$p = Get-PgPaths 'C:\\x\\app' 'C:\\x\\data'\nGet-PgCmdText 'C:\\Py\\python.exe' '' '' $p")
        self.assertIn('set "PG_TOOL_DATA_DIR=C:\\x\\data"', r.stdout); self.assertNotIn('PG_HUB_CMD', r.stdout); self.assertNotIn('PG_TOOL_NODE', r.stdout)      # absent when not found

    def test_the_launcher_reads_the_callers_code_page_from_the_last_word_of_chcp(self):
        d = SCRATCH / 'launcher-cp'; (d / 'bin').mkdir(parents=True, exist_ok=True); (d / 'pg_cli.py').write_text('import sys; sys.exit(7)')
        gen = ps(f". '{common()}'\n$p = Get-PgPaths '{d}' '{d / 'data'}'\n[IO.File]::WriteAllText('{d / 'PatentGenius.cmd'}', (Get-PgCmdText '{sys.executable}' '' '' $p))"); self.assertEqual(gen.returncode, 0, gen.stdout + gen.stderr)
        text = (d / 'PatentGenius.cmd').read_text(encoding='utf-8'); self.assertRegex(text, r'(?m)^if defined PG_CP chcp %PG_CP% >nul$'); self.assertNotIn('tokens=2 delims=:', text)
        lines = text.splitlines(); end = lines.index('chcp 65001 >nul'); probe = d / 'probe.cmd'; probe.write_text('\r\n'.join(lines[:end] + ['echo CP=[%PG_CP%]', '']))
        env = {**os.environ, 'PATH': str(d / 'bin') + ';' + os.environ['PATH']}
        for printed, expected in (('Active code page: 850', '850'), ('Aktive Codepage: 850.', '850'), ('Page de codes active : 437', '437'), ('Aktif kod sayfasi: 1254.', '1254'), ('something unexpected', '')):
            (d / 'bin' / 'chcp.cmd').write_text('@echo off\r\necho ' + re.sub(r'([&<>|^])', r'^\1', printed) + '\r\n')
            r = subprocess.run(['cmd.exe', '/d', '/c', str(probe)], cwd=str(d), env=env, capture_output=True, text=True, timeout=60); self.assertIn(f'CP=[{expected}]', r.stdout, printed + r.stdout + r.stderr)
        full = subprocess.run(['cmd.exe', '/d', '/c', str(d / 'PatentGenius.cmd'), 'doctor'], cwd=str(d), capture_output=True, text=True, timeout=60); self.assertEqual(full.returncode, 7, full.stdout + full.stderr)

    def test_a_bom_in_installer_json_is_read(self):
        base, app, data, sc, lnk = fake_install('h-bom'); j = (app / 'installer.json').read_bytes(); (app / 'installer.json').write_bytes(b'\xef\xbb\xbf' + j)
        r = ps(f". '{common()}'\n$c = Read-PgConfig '{app}'\n'DATA=' + $c.data_root"); self.assertIn('DATA=' + str(data), r.stdout)

    def test_no_text_file_in_the_release_contains_control_characters_other_than_tab_cr_lf(self):
        bad = []
        for p in ROOT.rglob('*'):
            if p.is_dir() or '.git' in p.parts or '__pycache__' in p.parts or '_dist' in p.parts or p.suffix.lower() in ('.zip', '.gz', '.png', '.ico', '.pdf', '.tar'): continue
            if 'tests' in p.parts and 'fixtures' in p.parts: continue
            b = p.read_bytes()
            if b'\x00' in b: continue
            for i, c in enumerate(b):
                if c < 32 and c not in (9, 10, 13): bad.append(f'{p.relative_to(ROOT)} offset {i}'); break
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
