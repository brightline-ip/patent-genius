<#
.SYNOPSIS
  Installs Patent Genius for the current Windows user. Started by Setup.cmd.

.DESCRIPTION
  Copies Patent Genius into a per-user program folder, creates a separate data folder (your scope, inputs, collected data and
  database; readable only by your account) with an example scope.json, writes the command-line launcher PatentGenius.cmd and MCP
  setup snippets for AI assistants (paths only, never a key) and adds Start Menu shortcuts. It installs no service, scheduled
  task or background process, asks for no key and edits no AI assistant configuration.
  Python 3.10 or newer is required; Node.js 24.13 or newer is only needed for the frame harvest and document download scripts (*.mjs); `pg collect` is Python. Missing programs are explained
  and offered through winget; nothing is installed without your "yes". PyMuPDF (for OCR of scanned PDFs) goes into a private
  .venv only if you say yes. If the Patent Research Hub is found, its command is recorded (PG_HUB_CMD); a missing or older Hub
  (before 0.2.0) is only a warning.
  Re-running Setup (or the Repair shortcut) repairs or updates the installation and keeps your data and settings.
  No administrator rights are needed.

.PARAMETER InstallRoot
  Program folder. Default: %LOCALAPPDATA%\Brightline\PatentGenius
.PARAMETER DataRoot
  Data folder (scope.json, inputs, corpus, packs, locks). Default: %LOCALAPPDATA%\Brightline\PatentGenius-Data
.PARAMETER ShortcutFolder
  Where shortcuts go. Default: Start Menu, folder "Patent Genius".
.PARAMETER HubCmd
  Full path of the Patent Research Hub's Hub.cmd. Default: the Hub recorded by an earlier Setup in the same program folder (Repair and update keep it), then PG_HUB_CMD, then the Hub installer's default folder. An explicit -HubCmd always wins.
.PARAMETER DryRun
  Check this computer and show what Setup would do, without installing, writing or starting anything (the Hub is asked only
  for its version, with "gateway --limits", which reads settings and makes no request).
.PARAMETER SimulateMissing
  With -DryRun only: pretend these are missing (Node, Python, Hub).
.PARAMETER NoOptional
  Do not offer Node.js or the optional OCR package. Never approves installing software. (Python is still required.)
.PARAMETER NoOpen
  Do not open the getting-started guide in Notepad at the end.
.PARAMETER Repair
  Started from the Repair shortcut: read the folders from this installation's settings and wait for Enter at the end.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Brightline\PatentGenius'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'Brightline\PatentGenius-Data'),
    [string]$ShortcutFolder = (Join-Path ([Environment]::GetFolderPath('Programs')) 'Patent Genius'),
    [string]$HubCmd = '',
    [switch]$DryRun,
    [string[]]$SimulateMissing = @(),
    [switch]$NoOptional,
    [switch]$NoOpen,
    [switch]$Repair
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$bundle = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
# Accept "-SimulateMissing Node,Python" from both -File (one string) and -Command (an array).
$SimulateMissing = @($SimulateMissing | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($item in $SimulateMissing) { if ($item -notin @('Node', 'Python', 'Hub')) { throw "-SimulateMissing accepts Node, Python and Hub (got '$item')." } }
if ($SimulateMissing.Count -and -not $DryRun) { throw '-SimulateMissing can only be used together with -DryRun.' }

$script:WingetInstalled = @()
$script:BadChars = [char[]]'"%!&,;=^'
$script:Reserved = @('con', 'prn', 'aux', 'nul') + @(1..9 | ForEach-Object { "com$_"; "lpt$_" })

function Get-WingetArgs([string]$Id) {
    return @('install', '--id', $Id, '--exact', '--source', 'winget', '--accept-source-agreements', '--accept-package-agreements')
}

# Explain a missing program and, after an explicit yes, install it with winget.
# Returns 'installed', 'declined', 'failed' or 'dryrun'.
function Request-Program([string]$Name, [string]$Id, [string[]]$Why, [string]$ManualUrl) {
    foreach ($line in $Why) { Write-Info $line }
    Write-Info 'Setup can install it with Windows Package Manager (free; no account needed):'
    Write-Info ('    winget ' + ((Get-WingetArgs $Id) -join ' '))
    Write-Info "Or install it yourself from $ManualUrl and run Setup again."
    if ($DryRun) { Write-Info "[dry run] Setup would ask: Install $Name now?"; return 'dryrun' }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Bad 'Windows Package Manager (winget) is not available. Install "App Installer" from the Microsoft Store, or use the link above.'
        return 'failed'
    }
    if (-not (Read-YesNo "Install $Name now?" $false)) { return 'declined' }
    Write-Info "Installing $Name. Windows may ask for permission; choose Yes."
    & $winget.Source @(Get-WingetArgs $Id)
    $code = $LASTEXITCODE
    Update-SessionPath
    # -1978335189 (0x8A15002B): already installed, no applicable upgrade. Nothing was installed then, so it is not recorded as something Setup installed.
    if ($code -eq -1978335189) { return 'installed' }
    if ($code -eq 0) { $script:WingetInstalled += $Name; return 'installed' }
    Write-Bad "winget reported an error ($code) while installing $Name."
    return 'failed'
}

# Characters and names that break command files, shortcuts or Windows itself are refused in every folder path.
function Test-PgPathText([string]$Path, [string]$Label) {
    if ($Path.IndexOfAny($script:BadChars) -ge 0) { throw "The $Label path cannot contain any of these characters: "" % ! & , ; = ^ . Choose another folder." }
    foreach ($segment in ($Path -split '[\\/]')) {
        if (-not $segment -or $segment -match '^[A-Za-z]:$') { continue }
        $stem = ($segment.TrimEnd(' ', '.') -split '\.')[0].ToLowerInvariant()
        if ($stem -in $script:Reserved) { throw "The $Label path contains the reserved Windows device name '$segment'. Choose another folder." }
        if ($segment -ne $segment.TrimEnd(' ', '.')) { throw "The $Label path has a folder name ending in a space or a dot ('$segment'). Choose another folder." }
    }
}

function Test-PgFolder([string]$Path, [string]$Label, [int]$MaxLength) {
    Test-PgPathText $Path $Label
    if ($Path.Length -gt $MaxLength) { throw "The $Label path is too long (at most $MaxLength characters, so the files inside stay under the Windows path limit). Choose a shorter folder." }
    $drive = [IO.Path]::GetPathRoot($Path)
    if (-not $drive -or $drive.StartsWith('\\')) { throw "The $Label must be on a local drive, not a network path." }
    if ($Path.TrimEnd('\') -ieq $drive.TrimEnd('\')) { throw "The $Label cannot be the root of a drive." }
    $info = New-Object IO.DriveInfo($drive)
    # Your data folder relies on NTFS permissions.
    if ($info.DriveFormat -notin @('NTFS', 'ReFS')) { throw "The $Label is on a $($info.DriveFormat) drive, which cannot restrict access to your files. Choose an NTFS drive." }
    return $info
}

# Shortcuts are saved in a plain-path staging folder and moved into place (see Save-PgShortcut), so the shortcut folder may
# contain any character. Paths stored INSIDE a shortcut (target, arguments) must be ANSI-safe: those use the 8.3 short form
# when the long form has characters the code page cannot hold. Returns $null (and warns) if a shortcut cannot be made.
function New-PgShortcut([string]$Name, [string]$Target, [scriptblock]$Arguments, [string]$Description, [string]$Icon) {
    try {
        $args2 = & $Arguments
        # Never the program folder: a process whose current directory is inside it blocks Uninstall and Repair from deleting it.
        return (Save-PgShortcut $ShortcutFolder $Name $Target $args2 $env:SystemRoot $Description "$env:SystemRoot\System32\$Icon" @([IO.Path]::GetTempPath(), $InstallRoot, $DataRoot))
    } catch {
        Write-Warn2 "The shortcut '$Name' was not created: $($_.Exception.Message) Use $(Join-Path $InstallRoot 'installer') instead."
        return $null
    }
}

function Get-ShortcutPath([string]$Path) {
    $safe = Get-PgAnsiSafePath $Path
    if (-not $safe) { throw "The path $Path has characters a Windows shortcut cannot store and no short (8.3) name exists." }
    return $safe
}

function Get-PsArgs([string]$Script, [string]$Extra = '') {
    $a = '-NoProfile -ExecutionPolicy Bypass -File "' + (Get-ShortcutPath (Join-Path $InstallRoot "installer\$Script")) + '"'
    if ($Extra) { $a += ' ' + $Extra }
    return $a
}
$exit = 0
try {
    if ($Repair) {
        # The Repair shortcut runs this script inside the installation: use its recorded folders.
        if (-not (Test-Path -LiteralPath (Join-Path $bundle $script:PgMarker) -PathType Leaf)) { throw 'The Repair shortcut must run from an installed copy.' }
        $recorded = Read-PgConfig $bundle
        $InstallRoot = $bundle
        $DataRoot = [string]$recorded.data_root
        $ShortcutFolder = [string]$recorded.shortcut_folder
    }
    # The text as typed is checked first: GetFullPath would quietly drop a trailing dot or space.
    Test-PgPathText $InstallRoot 'program folder'; Test-PgPathText $DataRoot 'data folder'; Test-PgPathText $ShortcutFolder 'shortcut folder'
    $InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    $DataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
    $ShortcutFolder = [IO.Path]::GetFullPath($ShortcutFolder).TrimEnd('\')
    $paths = Get-PgPaths $InstallRoot $DataRoot

    Write-Host ''
    Write-Host "Patent Genius - Setup (public preview $script:PgVersion)" -ForegroundColor White
    Write-Info 'Reads US office actions with fixed rules, finds similar ones in your own data, and builds fact packs. Not legal advice.'
    Write-Info "Program folder: $InstallRoot"
    Write-Info "Data folder:    $DataRoot"

    # ---- 1. This computer --------------------------------------------------------------------
    Write-Step 'Step 1 of 5: Checking this computer and the folders'
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) { throw 'Patent Genius needs 64-bit Windows 10 or 11.' }
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'pg_cli.py')) -or -not (Test-Path -LiteralPath (Join-Path $bundle 'pg_tool\contract.json'))) {
        throw 'The download is incomplete. Extract the whole zip file (right-click, Extract All) and run Setup.cmd from the extracted folder.'
    }
    $info = Test-PgFolder $InstallRoot 'program folder' 120
    $null = Test-PgFolder $DataRoot 'data folder' 120
    Test-PgPathText $ShortcutFolder 'shortcut folder'
    # Before anything is copied or created: can a shortcut be saved in that folder at all (a test shortcut is made and removed)?
    $lnkProblem = Test-PgShortcutFolder $ShortcutFolder @([IO.Path]::GetTempPath())
    if ($lnkProblem) { throw "Shortcuts cannot be created in $ShortcutFolder ($lnkProblem). Nothing was installed. Run Setup again with -ShortcutFolder set to a folder whose path uses only basic Latin letters and digits, for example C:\PatentGenius-Shortcuts." }
    $a = $InstallRoot + '\'; $b = $DataRoot + '\'
    if ($a.StartsWith($b, [StringComparison]::OrdinalIgnoreCase) -or $b.StartsWith($a, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The program folder and the data folder must be separate (neither inside the other), so uninstalling never touches your data.'
    }
    foreach ($forbidden in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, [Environment]::GetFolderPath('MyDocuments'), [Environment]::GetFolderPath('Desktop'))) {
        if ($forbidden -and ($InstallRoot -ieq $forbidden.TrimEnd('\') -or $DataRoot -ieq $forbidden.TrimEnd('\'))) { throw "Choose a dedicated folder, not $forbidden itself." }
    }
    $hadInstall = Test-Path -LiteralPath $paths.Marker -PathType Leaf
    if ($bundle -ieq $InstallRoot -and -not $hadInstall) { throw 'Run Setup.cmd from the extracted download, not from inside the program folder.' }
    # The program folder is mirrored from the download folder (files only in the program folder are deleted), so the
    # two must not contain one another.
    $dl = $bundle + '\'; $pf = $InstallRoot + '\'
    if ($bundle -ine $InstallRoot -and ($dl.StartsWith($pf, [StringComparison]::OrdinalIgnoreCase) -or $pf.StartsWith($dl, [StringComparison]::OrdinalIgnoreCase))) {
        throw "The download folder ($bundle) and the program folder ($InstallRoot) must be separate (neither inside the other), because Setup replaces the program folder's contents with the download. Move the extracted download elsewhere or choose another -InstallRoot."
    }
    if (-not $hadInstall -and (Test-Path -LiteralPath $InstallRoot) -and (Get-ChildItem -LiteralPath $InstallRoot -Force | Select-Object -First 1)) {
        throw "$InstallRoot already has files that Setup did not put there. Choose an empty folder with -InstallRoot."
    }
    $hadData = Test-Path -LiteralPath $paths.DataMarker -PathType Leaf
    if (-not $hadData -and (Test-Path -LiteralPath $DataRoot) -and (Get-ChildItem -LiteralPath $DataRoot -Force | Select-Object -First 1)) {
        throw "$DataRoot already has files that Setup did not put there. Choose an empty folder with -DataRoot."
    }
    if ($info.AvailableFreeSpace -lt 1GB) { Write-Warn2 ("Only {0:N1} GB free. Allow at least 1 GB plus room for the data you collect." -f ($info.AvailableFreeSpace / 1GB)) }
    Write-Good ('Windows ' + [Environment]::OSVersion.Version.ToString() + ', 64-bit; folders are on ' + $info.DriveFormat)
    if ($hadInstall) { Write-Info 'An earlier installation was found: Setup will repair or update it and keep your data and settings.' }
    if ($hadData) { Write-Info 'Your existing data folder will be kept as it is.' }

    # ---- 2. Programs --------------------------------------------------------------------------
    Write-Step 'Step 2 of 5: Checking programs'
    $python = if ('Python' -in $SimulateMissing) { $null } else { Find-PgPython }
    if (-not $python) {
        Write-Warn2 "Python $script:PgMinPython or newer was not found (required)."
        $r = Request-Program 'Python 3.12' 'Python.Python.3.12' @('Patent Genius is a Python program: its tools, command line and MCP server run on Python.', 'Python is free, open-source software. Patent Genius uses only its standard library.') 'https://www.python.org/downloads/windows/'
        if ($r -eq 'installed') { $python = Find-PgPython }
        if ($r -ne 'dryrun' -and -not $python) {
            throw "Python $script:PgMinPython or newer is required. Install it and run Setup again. If you just installed it, close this window and run Setup again."
        }
    }
    if ($python) { Write-Good "Python $($python.Version) ($($python.Exe))" }
    # Checked here, before anything is changed: the launcher and the MCP files record this path, and a percent sign or a quote in it would be read by cmd.exe as something else.
    if ($python -and -not (Test-PgRecordablePath $python.Exe)) { throw "The Python found at $($python.Exe) has a % or a quote in its path, which PatentGenius.cmd cannot record safely. Install Python in a folder without them and run Setup again." }

    $node = $null
    if (-not $NoOptional) {
        $node = if ('Node' -in $SimulateMissing) { $null } else { Find-PgNode }
        if (-not $node -or -not $node.NewEnough) {
            if ($node) { Write-Info "Node.js $($node.Version) was found; the *.mjs scripts need $script:PgMinNode or newer. It is OPTIONAL." }
            else { Write-Info 'Node.js was not found. It is OPTIONAL: only the frame harvest and document download scripts (*.mjs) need it (pg collect and everything else work without).' }
            $r = Request-Program 'Node.js LTS (optional)' 'OpenJS.NodeJS.LTS' @('Node.js runs the frame harvest and document download scripts (*.mjs). Nothing else uses it: pg collect is Python.', 'Node.js is free, open-source software.') 'https://nodejs.org/'
            if ($r -eq 'installed') { $node = Find-PgNode }
            if ($r -ne 'dryrun' -and (-not $node -or -not $node.NewEnough)) { Write-Info 'Continuing without a usable Node.js. Run Setup again later to add it.'; $node = $null }
        }
    } else { Write-Info 'Skipping the optional Node.js check (-NoOptional).' }
    if ($node -and $node.NewEnough) { Write-Good "Node.js $($node.Version) ($($node.Exe)) - optional, for the *.mjs scripts" }
    $nodeExe = if ($node -and $node.NewEnough) { $node.Exe } else { '' }

    $wantVenv = $false
    $hasVenv = Test-Path -LiteralPath $paths.Venv -PathType Leaf
    if (-not $NoOptional -and $python) {
        if ($hasVenv) { $wantVenv = $true; Write-Info 'The optional OCR package is already set up; Setup will refresh it.' }
        else {
            Write-Info 'OCR of scanned PDFs needs PyMuPDF (and the Tesseract program). Setup can put PyMuPDF in a private environment'
            Write-Info 'inside the program folder (about 25 MB from the Python Package Index, one pinned version, hash-checked). It is'
            Write-Info 'licensed AGPL-3.0 or commercially: check that suits your use. OPTIONAL; nothing else needs it.'
            if ($DryRun) { Write-Info '[dry run] Setup would ask: Set up the optional OCR package now?' }
            else { $wantVenv = Read-YesNo 'Set up the optional OCR package now?' $false }
        }
    }

    # The Hub path chosen at the first Setup is kept in installer.json and used again by Repair and by a later Setup over the same folder; an explicit -HubCmd wins.
    $recordedHub = if ($HubCmd) { '' } else { Get-PgRecordedHub $InstallRoot }
    $hub = if ('Hub' -in $SimulateMissing) { @{ Cmd = $null; Source = $null; State = 'missing'; Version = $null; Detail = 'simulated' } } elseif ($recordedHub) { Find-PgHub $recordedHub 'the Hub command recorded by the earlier Setup (installer.json)' } else { Find-PgHub $HubCmd }
    if ($hub.State -eq 'ok') { Write-Good "Patent Research Hub $($hub.Version): $($hub.Cmd) (from $($hub.Source)); PG_HUB_CMD will be recorded" }
    elseif ($hub.State -eq 'old') {
        Write-Warn2 "A Hub was found at $($hub.Cmd), but $($hub.Detail). Collecting data needs Patent Research Hub $script:PgMinHub or later. Setup continues; the command is recorded anyway."
    } else {
        Write-Warn2 'The Patent Research Hub was not found. It is OPTIONAL: only collecting data from the USPTO needs it (Hub 0.2.0 or later, with your own USPTO key).'
        Write-Info 'Install it, then run the Repair shortcut (or Setup again), or set PG_HUB_CMD to its Hub.cmd yourself.'
    }
    $hubCmd = if ($hub.State -in @('ok', 'old')) { $hub.Cmd } else { '' }

    # ---- 3. Running copies --------------------------------------------------------------------
    Write-Step 'Step 3 of 5: Checking for running copies of this installation'
    $running = if ($hadInstall) { @(Get-PgProcesses $InstallRoot) } else { @() }
    if ($running.Count) {
        Write-Info "$($running.Count) process(es) from this installation are running, probably an MCP server started by an AI assistant."
        Write-Info 'They hold files that Setup must replace, so they have to stop first. Stopping them ends them immediately (a call in'
        Write-Info 'progress is cut off; saved files are not affected). Your AI assistant starts the server again when needed.'
        Write-Info 'Only processes running files from this program folder are stopped; nothing else is touched.'
        if ($DryRun) { Write-Info '[dry run] Setup would ask: Stop them now?' }
        elseif (Read-YesNo 'Stop them now?' $false) {
            $null = Stop-PgProcesses $InstallRoot
            Start-Sleep -Seconds 1
            Write-Good 'Stopped them so the files can be updated. Restart your AI assistant if it does not reconnect by itself.'
        } else {
            throw 'Setup did not change anything because the running processes were left running. Quit your AI assistant (or let any running call finish), then run Setup again.'
        }
    } else { Write-Good 'None running' }

    if ($DryRun) {
        Write-Host ''
        Write-Good 'Dry run finished. Nothing was changed.'
        Write-Info "Setup would copy the program to $InstallRoot (there is nothing to download: no packages are installed)."
        Write-Info "It would create $DataRoot (readable only by your account) with an example scope.json and the folders inputs, corpus, packs and locks,"
        Write-Info "write PatentGenius.cmd, MCP-setup.json and MCP-setup.codex.toml (paths only, no keys), and add shortcuts in $ShortcutFolder."
        exit 0
    }

    # ---- 4. Install ---------------------------------------------------------------------------
    Write-Step 'Step 4 of 5: Installing'
    $createdParent = $false
    $parent = Split-Path -Parent $InstallRoot
    if (-not $hadInstall) {
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { $createdParent = $true }
    } else {
        try { $createdParent = [bool](Read-PgConfig $InstallRoot).created_parent } catch {}
    }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    if (-not $hadInstall) { Write-Utf8NoBom $paths.Marker "Patent Genius program folder. The uninstaller removes only folders that contain this file.`r`n" }
    New-Item -ItemType Directory -Force -Path $paths.Logs | Out-Null
    if ($bundle -ine $InstallRoot) {
        $null = & robocopy.exe $bundle $InstallRoot /MIR /XJ /R:1 /W:1 /XD .git .venv logs __pycache__ _dist /XF installer.json MCP-setup.json MCP-setup.codex.toml PatentGenius.cmd GETTING-STARTED.txt $script:PgMarker /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -ge 8) { throw "Copying the program files failed (robocopy code $LASTEXITCODE)." }
    }
    # Clear the "downloaded from the Internet" mark on the copies this installation runs.
    Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Include *.ps1, *.cmd, *.mjs, *.py | Unblock-File -ErrorAction SilentlyContinue
    if ($bundle -ine $InstallRoot) { Write-Good 'Program files copied (nothing to download: no packages are installed)' } else { Write-Good 'Program files left as they are (Repair does not replace them)' }

    $pythonExe = $python.Exe
    # The launcher and the MCP files record these paths: a percent sign or a quote in one would be read by cmd.exe as something else.
    if ($nodeExe -and -not (Test-PgRecordablePath $nodeExe)) { Write-Warn2 "Node.js at $nodeExe has a % or a quote in its path: it is not recorded (the *.mjs scripts will not find it through PatentGenius.cmd)."; $nodeExe = '' }
    if ($hubCmd -and -not (Test-PgRecordablePath $hubCmd)) { Write-Warn2 "The Hub command $hubCmd has a % or a quote in its path: PG_HUB_CMD is not recorded."; $hubCmd = '' }
    if ($wantVenv) {
        $pyLog = Join-Path $paths.Logs 'python.log'
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            if (-not (Test-Path -LiteralPath $paths.Venv -PathType Leaf)) { & $python.Exe -m venv (Join-Path $InstallRoot '.venv') *> $pyLog }
            if (Test-Path -LiteralPath $paths.Venv -PathType Leaf) {
                & $paths.Venv -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes -r (Join-Path $InstallRoot 'requirements-ocr.txt') *>> $pyLog
                $pyCode = $LASTEXITCODE
            } else { $pyCode = 1 }
        } finally { $ErrorActionPreference = $old }
        if ($pyCode -eq 0) { Write-Good 'OCR package installed in a private environment (optional)' }
        else { Write-Warn2 "The optional OCR package could not be installed; everything else works. Details: $pyLog" }
    }
    if (Test-Path -LiteralPath $paths.Venv -PathType Leaf) { $pythonExe = $paths.Venv }

    # Data folder: created by Setup and readable only by your account, before anything is written into it.
    if (-not $hadData) {
        New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
        Set-PgUserOnly $DataRoot -Folder
        Write-Good 'Data folder created (access list read back: inheritance off, only your account has access)'
        Write-Utf8NoBom $paths.DataMarker "Patent Genius data folder (scope, inputs, collected data, database, packs). The uninstaller deletes it only if you ask.`r`n"
    } else {
        try { $null = Assert-PgUserOnly $DataRoot }
        catch { Write-Warn2 "Your existing data folder is not private to your account. Setup did not change it. $($_.Exception.Message)" }
    }
    foreach ($d in @($paths.Inputs, $paths.Corpus, $paths.Packs, $paths.Locks)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    if (-not (Test-Path -LiteralPath $paths.Scope -PathType Leaf)) {
        Copy-Item -LiteralPath (Join-Path $InstallRoot 'examples\scope.example.json') -Destination $paths.Scope
        Write-Good 'Example scope.json created (invented fields: edit every value before you collect anything)'
    } else { Write-Good 'Existing scope.json kept' }

    # Command-line launcher (see Get-PgCmdText in common.ps1).
    Write-Utf8NoBom $paths.PgCmd (Get-PgCmdText $pythonExe $nodeExe $hubCmd $paths)

    # MCP setup snippets. They hold paths only: no key exists anywhere in Patent Genius.
    $envBlock = [ordered]@{ PG_TOOL_DATA_DIR = $DataRoot }
    if ($hubCmd) { $envBlock['PG_HUB_CMD'] = $hubCmd }
    $mcp = [ordered]@{ mcpServers = [ordered]@{ patent_genius = [ordered]@{ command = $pythonExe; args = @($paths.Mcp); env = $envBlock } } }
    Write-Utf8NoBom $paths.McpJson (($mcp | ConvertTo-Json -Depth 6) + "`r`n")
    $toml = @(
        '# Patent Genius for Codex: add these lines to %USERPROFILE%\.codex\config.toml',
        '[mcp_servers.patent_genius]',
        ('command = ' + (ConvertTo-TomlString $pythonExe)),
        ('args = [' + (ConvertTo-TomlString $paths.Mcp) + ']'),
        'startup_timeout_sec = 30',
        'tool_timeout_sec = 240',
        '',
        '[mcp_servers.patent_genius.env]',
        ('PG_TOOL_DATA_DIR = ' + (ConvertTo-TomlString $DataRoot))
    )
    if ($hubCmd) { $toml += ('PG_HUB_CMD = ' + (ConvertTo-TomlString $hubCmd)) }
    $toml += ''
    Write-Utf8NoBom $paths.McpToml ($toml -join "`r`n")

    $guide = [IO.File]::ReadAllText((Join-Path $InstallRoot 'installer\GETTING-STARTED.template.txt'))
    $hubLine = if ($hubCmd) { "$hubCmd (recorded as PG_HUB_CMD)" } else { 'not found: collecting data needs the Patent Research Hub 0.2.0 or later' }
    $guide = $guide.Replace('{{ROOT}}', $InstallRoot).Replace('{{DATA}}', $DataRoot).Replace('{{HUB}}', $hubLine)
    Write-Utf8NoBom $paths.Guide ($guide -replace "`r?`n", "`r`n")
    Write-Good 'PatentGenius.cmd, MCP setup files and getting-started guide written (no AI assistant configuration was changed)'

    # ---- 5. Shortcuts and check ---------------------------------------------------------------
    Write-Step 'Step 5 of 5: Shortcuts and first check'
    # The settings and marker are written BEFORE the shortcuts, so a failure from here on still leaves installer\remove.ps1 usable.
    $config = [ordered]@{ version = $script:PgVersion; data_root = $DataRoot; shortcut_folder = $ShortcutFolder; shortcuts = @(); python = $python.Exe; node = $nodeExe; hub_cmd = $hubCmd; venv = [bool](Test-Path -LiteralPath $paths.Venv -PathType Leaf); created_parent = $createdParent }
    Write-Utf8NoBom $paths.Config ($config | ConvertTo-Json -Depth 4)
    $shortcuts = @(
        (New-PgShortcut 'Patent Genius - Check' $PowerShellExe { Get-PsArgs 'check.ps1' } 'Check Python, the data folder, your scope and the Hub (no network call)' 'shell32.dll,23'),
        (New-PgShortcut 'Patent Genius - Data folder' (Join-Path $env:SystemRoot 'explorer.exe') { '"' + (Get-ShortcutPath $DataRoot) + '"' } 'Open your scope, inputs, collected data and packs' 'shell32.dll,3'),
        (New-PgShortcut 'Patent Genius - Documentation' (Join-Path $env:SystemRoot 'System32\notepad.exe') { '"' + (Get-ShortcutPath $paths.Guide) + '"' } 'Getting started: scope, collecting, MCP setup, documentation' 'shell32.dll,1'),
        (New-PgShortcut 'Patent Genius - Repair' $PowerShellExe { Get-PsArgs 'setup.ps1' '-Repair -NoOpen' } 'Re-detects Python, Node and the Hub, refreshes the optional OCR package if installed, and rewrites PatentGenius.cmd, MCP files, guide and shortcuts; program files and data are not replaced' 'shell32.dll,238'),
        (New-PgShortcut 'Patent Genius - Uninstall' $PowerShellExe { Get-PsArgs 'remove.ps1' } 'Remove Patent Genius' 'shell32.dll,31')
    ) | Where-Object { $_ }
    if ($shortcuts.Count -eq 5) { Write-Good "Shortcuts created in $ShortcutFolder" } else { Write-Warn2 "Only $($shortcuts.Count) of 5 shortcuts could be created. To remove Patent Genius later, run $(Join-Path $InstallRoot 'installer\remove.ps1')." }
    $config.shortcuts = @($shortcuts)
    Write-Utf8NoBom $paths.Config ($config | ConvertTo-Json -Depth 4)

    $report = Invoke-PgDoctor $paths
    foreach ($line in $report.Lines) { Write-Info $line }
    if ($report.Code -ne 0) { Write-Warn2 'The first check reported a problem (see above). Setup finished copying; fix it and run the Check shortcut.' }

    Write-Host ''
    Write-Good 'Setup finished.'
    Write-Info 'No key was asked for and none is stored: Patent Genius has no keys. Collecting data from the USPTO uses the Patent Research Hub,'
    Write-Info 'which holds your own USPTO key (the USPTO needs an ID.me-verified account, and deletes a key unused for 90 days).'
    Write-Info 'What leaves this computer: nothing, until you run "collect run --yes". Then only application numbers from your own frames.'
    Write-Info "Edit your scope: $($paths.Scope)  (invented example; replace every value)"
    Write-Info "To connect an AI assistant, merge $($paths.McpJson) (Claude Desktop, Claude Code) or $($paths.McpToml) (Codex) into its configuration."
    Write-Info "Command line: `"$($paths.PgCmd)`" doctor"
    Write-Info "Your data stays in $DataRoot, also after uninstalling, unless you choose to delete it."
    Write-Info 'Read DISCLAIMER.md: output is not legal advice and must be checked against the official record.'
    if (-not $NoOpen) { Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\notepad.exe') -ArgumentList ('"' + $paths.Guide + '"') }
} catch {
    Write-Host ''
    Write-Bad $_.Exception.Message
    if ($script:WingetInstalled.Count) { Write-Info ('Setup installed ' + ($script:WingetInstalled -join ' and ') + ' with winget before this error; that program stays installed.') }
    $apart = if ($script:WingetInstalled.Count) { 'Apart from that, nothing' } else { 'Nothing' }
    Write-Info "$apart outside the program folder, data folder and shortcuts was changed. You can fix the problem and run Setup again."
    $exit = 1
}
if ($Repair) { Read-Host 'Press Enter to close' | Out-Null }
exit $exit
