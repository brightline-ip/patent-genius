# Shared helpers for the Patent Genius Windows installer. Windows PowerShell 5.1 compatible. ASCII only.
# Dot-source this file; it defines functions only.

# Started from a PowerShell 7 window, Windows PowerShell inherits a module path it cannot use.
if ($PSVersionTable.PSEdition -ne 'Core') {
    $env:PSModulePath = @(
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'WindowsPowerShell\Modules'),
        (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'),
        (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules')
    ) -join ';'
}

$script:PgMarker = '.patent-genius-install'
$script:PgDataMarker = '.patent-genius-data'
$script:PgVersion = '0.1.0'
$script:PgMinPython = [version]'3.10.0'
$script:PgMinNode = [version]'24.13.0'
$script:PgMinHub = [version]'0.2.0'

function Write-Step([string]$Text) { Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host ('   ' + $Text) }
function Write-Good([string]$Text) { Write-Host ('   OK  ' + $Text) -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host ('   !   ' + $Text) -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host ('   X   ' + $Text) -ForegroundColor Red }

function Read-YesNo([string]$Question, [bool]$Default = $false) {
    $suffix = if ($Default) { ' [Y/n] ' } else { ' [y/N] ' }
    while ($true) {
        $answer = Read-Host -Prompt ('   ' + $Question + $suffix)
        if ($null -eq $answer) { return $Default }
        $answer = $answer.Trim().ToLowerInvariant()
        if ($answer -eq '') { return $Default }
        if ($answer -in @('y', 'yes')) { return $true }
        if ($answer -in @('n', 'no')) { return $false }
        Write-Info 'Please type Y for yes or N for no.'
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

function Get-PgPaths([string]$Root, [string]$DataRoot) {
    return @{
        Root       = $Root
        Marker     = Join-Path $Root $script:PgMarker
        Config     = Join-Path $Root 'installer.json'
        Logs       = Join-Path $Root 'logs'
        Venv       = Join-Path $Root '.venv\Scripts\python.exe'
        Cli        = Join-Path $Root 'pg_cli.py'
        Mcp        = Join-Path $Root 'pg_tool_mcp.py'
        PgCmd      = Join-Path $Root 'PatentGenius.cmd'
        Guide      = Join-Path $Root 'GETTING-STARTED.txt'
        McpJson    = Join-Path $Root 'MCP-setup.json'
        McpToml    = Join-Path $Root 'MCP-setup.codex.toml'
        DataRoot   = $DataRoot
        DataMarker = Join-Path $DataRoot $script:PgDataMarker
        Scope      = Join-Path $DataRoot 'scope.json'
        Inputs     = Join-Path $DataRoot 'inputs'
        Corpus     = Join-Path $DataRoot 'corpus'
        Packs      = Join-Path $DataRoot 'packs'
        Locks      = Join-Path $DataRoot 'locks'
    }
}

# The Hub command recorded by an earlier Setup in this program folder ('' when there is none, or installer.json cannot be read).
function Get-PgRecordedHub([string]$Root) {
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $Root 'installer.json') -PathType Leaf)) { return '' }
        $c = Read-PgConfig $Root
        if ($c -and $c.hub_cmd) { return [string]$c.hub_cmd }
    } catch { }
    return ''
}

function Read-PgConfig([string]$Root) {
    # A leading byte order mark (some editors add one) is removed: ConvertFrom-Json would reject it.
    return ((Get-Content -LiteralPath (Join-Path $Root 'installer.json') -Raw -Encoding UTF8).TrimStart([char]0xFEFF) | ConvertFrom-Json)
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

# The current user's SID, for access lists (names can fail for some Microsoft or work accounts).
function Get-PgUserSid { return ('*' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) }

# Runs icacls.exe without letting $ErrorActionPreference = 'Stop' turn its stderr text into a raw terminating error: the
# exit code decides, and a failure gets a plain-language message.
function Invoke-PgIcacls([string]$Path, [string[]]$IcaclsArgs, [string]$Problem) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $null = & icacls.exe $Path @IcaclsArgs 2>&1; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "$Problem Make sure the folder is on an NTFS drive and belongs to your account, then run Setup again. (icacls code $code, path $Path)" }
}

# Folder or file accessible only by the current user: no SYSTEM or Administrators entry is added. Raises if icacls fails.
function Set-PgUserOnly([string]$Path, [switch]$Folder) {
    $grant = if ($Folder) { (Get-PgUserSid) + ':(OI)(CI)F' } else { (Get-PgUserSid) + ':F' }
    # /reset first: /grant:r alone leaves any extra explicit entry (for example one added by hand) in place.
    Invoke-PgIcacls $Path @('/reset') 'Could not reset the access list of the file or folder.'
    Invoke-PgIcacls $Path @('/inheritance:r', '/grant:r', $grant) 'Could not restrict the file or folder to your account.'
    $null = Assert-PgUserOnly $Path
}

# Same result as Set-PgUserOnly, written with ONE write of the access list (no moment when old and new lists are mixed). Only the
# access section is loaded and written: Set-Acl with a whole descriptor can demand the SeSecurityPrivilege that ordinary accounts
# do not hold. The list is read back before returning.
function Set-PgUserOnlyAcl([string]$Path, [switch]$Folder) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $access = [Security.AccessControl.AccessControlSections]::Access
    # Windows PowerShell (.NET Framework) has static File/Directory methods; PowerShell 7 (.NET) has them as FileSystemAclExtensions.
    $legacy = [bool][IO.File].GetMethod('GetAccessControl', [Type[]]@([string], [Security.AccessControl.AccessControlSections]))
    try {
        $acl = if ($legacy) { if ($Folder) { [IO.Directory]::GetAccessControl($Path, $access) } else { [IO.File]::GetAccessControl($Path, $access) } }
               elseif ($Folder) { [IO.FileSystemAclExtensions]::GetAccessControl((New-Object IO.DirectoryInfo $Path), $access) }
               else { [IO.FileSystemAclExtensions]::GetAccessControl((New-Object IO.FileInfo $Path), $access) }
        foreach ($existing in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { $null = $acl.RemoveAccessRuleSpecific($existing) }
        $acl.SetAccessRuleProtection($true, $false)
        $rule = if ($Folder) {
            New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, ([Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'), [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        } else {
            New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow)
        }
        $acl.AddAccessRule($rule)
        if ($legacy) { if ($Folder) { [IO.Directory]::SetAccessControl($Path, $acl) } else { [IO.File]::SetAccessControl($Path, $acl) } }
        elseif ($Folder) { [IO.FileSystemAclExtensions]::SetAccessControl((New-Object IO.DirectoryInfo $Path), $acl) }
        else { [IO.FileSystemAclExtensions]::SetAccessControl((New-Object IO.FileInfo $Path), $acl) }
    } catch { throw "Could not restrict the file or folder to your account. Make sure it is on an NTFS drive and belongs to your account, then run Setup again. ($($_.Exception.Message))" }
    $null = Assert-PgUserOnly $Path
}

# Reads the access list back. Throws unless inheritance is off and the only principals that are allowed access are the
# current user or, if someone added them, SYSTEM and Administrators (who can read the file anyway). Deny rules are ignored.
function Assert-PgUserOnly([string]$Path) {
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($userSid, 'S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Path
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.AccessControlType -eq 'Allow' })
    $extra = @($rules | Where-Object { $_.IdentityReference.Value -notin $allowed } | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
    if (-not $acl.AreAccessRulesProtected) { throw "The access list of $Path still inherits permissions from its parent folder; it is not private to your account." }
    if ($extra.Count) { throw "$Path can still be accessed by other accounts ($($extra -join ', ')). Setup stopped so nothing private is stored in it." }
    if (-not @($rules | Where-Object { $_.IdentityReference.Value -eq $userSid }).Count) { throw "Your own account has no access to $Path after the permissions change." }
    return @($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
}

# Returns @{ Exe; Version; NewEnough } for Node.js (OPTIONAL: only the collection scripts need it), or $null.
function Find-PgNode {
    $node = (Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
    $exe = if ($node) { $node.Source } elseif (Test-Path -LiteralPath "$env:ProgramFiles\nodejs\node.exe") { "$env:ProgramFiles\nodejs\node.exe" } else { $null }
    if (-not $exe) { return $null }
    try {
        $v = & $exe -p 'process.versions.node' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
        $version = [version](([string]$v).Trim())
        return @{ Exe = $exe; Version = $version; NewEnough = ($version -ge $script:PgMinNode) }
    } catch { return $null }
}

# Returns @{ Exe; Version } for Python 3.10 or newer (REQUIRED: Patent Genius is a Python program), or $null.
function Find-PgPython {
    # No quote characters: Windows PowerShell 5.1 drops embedded double quotes when calling native programs.
    $probe = 'import sys;print(sys.executable);print(sys.version_info[0],sys.version_info[1],sys.version_info[2],sep=chr(46))'
    $candidates = New-Object Collections.Generic.List[object]
    foreach ($v in @('3.12', '3.13', '3.14', '3.11', '3.10')) { $candidates.Add(@('py', "-$v")) }
    $candidates.Add(@('python'))
    foreach ($v in @('312', '313', '314', '311', '310')) {
        foreach ($base in @("$env:LOCALAPPDATA\Programs\Python\Python$v", "$env:ProgramFiles\Python$v")) {
            $candidates.Add(@((Join-Path $base 'python.exe')))
        }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]; $extra = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $exe @extra -c $probe 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $lines = @($out)
            if ($lines.Count -lt 2) { continue }
            $ver = [version]$lines[1]
            if ($ver -ge $script:PgMinPython -and $lines[0] -notmatch '\\WindowsApps\\') {
                return @{ Exe = $lines[0].Trim(); Version = $ver }
            }
        } catch { continue }
    }
    return $null
}

# --- The Patent Research Hub (optional; collection needs 0.2.0 or later) ---------------------------------------------------

# Runs "<Hub.cmd> gateway --limits" (reads the Hub's settings only: no key, no request). Returns @{ Ok; Version; Detail }.
# The command line is built as the Hub documents it: cmd.exe /d /s /c with the whole line in one quoted string.
function Get-PgHubVersion([string]$HubCmd) {
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
        $psi.Arguments = '/d /s /c ""' + $HubCmd + '" gateway --limits"'
        $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
        $p = [Diagnostics.Process]::Start($psi)
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEndAsync(); $null = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit(30000)) { try { & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null } catch {}; return @{ Ok = $false; Version = $null; Detail = 'it did not answer within 30 seconds' } }
        $text = $out.Result
        $doc = $null; try { $doc = $text | ConvertFrom-Json } catch {}
        if ($p.ExitCode -eq 0 -and $doc -and $doc.hub_version) {
            $v = $null; if ([version]::TryParse(([string]$doc.hub_version), [ref]$v)) { return @{ Ok = $true; Version = $v; Detail = '' } }
        }
        return @{ Ok = $false; Version = $null; Detail = 'it does not answer "gateway --limits" (a Hub older than 0.2.0, or a different program)' }
    } catch { return @{ Ok = $false; Version = $null; Detail = $_.Exception.Message } }
}

# Looks for the Hub: -HubCmd, then PG_HUB_CMD, then the Hub installer's default folder. Never fails. Returns @{ Cmd; Source; State; Version; Detail }
# with State one of ok, old, missing. A Hub that is missing or too old is a warning for the caller, not an error.
function Find-PgHub([string]$Explicit, [string]$ExplicitSource = 'the -HubCmd option') {
    $cands = @()
    if ($Explicit) { $cands += , @($Explicit, $ExplicitSource) }
    if ($env:PG_HUB_CMD) { $cands += , @($env:PG_HUB_CMD, 'PG_HUB_CMD') }
    if ($env:LOCALAPPDATA) { $cands += , @((Join-Path $env:LOCALAPPDATA 'Brightline\PatentResearchHub\Hub.cmd'), 'the Hub installer default folder') }
    foreach ($c in $cands) {
        if (-not (Test-Path -LiteralPath $c[0] -PathType Leaf)) { continue }
        $r = Get-PgHubVersion $c[0]
        if ($r.Ok -and $r.Version -ge $script:PgMinHub) { return @{ Cmd = $c[0]; Source = $c[1]; State = 'ok'; Version = $r.Version; Detail = '' } }
        return @{ Cmd = $c[0]; Source = $c[1]; State = 'old'; Version = $r.Version; Detail = $(if ($r.Ok) { "version $($r.Version) is older than $script:PgMinHub" } else { $r.Detail }) }
    }
    return @{ Cmd = $null; Source = $null; State = 'missing'; Version = $null; Detail = 'no Hub.cmd was found' }
}

# Processes that belong to this installation: python.exe, pythonw.exe or node.exe whose EXECUTABLE or whose SCRIPT (the first argument that
# is not an option) sits inside this program folder (an MCP server your AI assistant started, a command-line run), and anything running from
# this install's .venv. A process that merely has a path from this folder somewhere on its command line (an argument, inline code) is NOT
# matched: the match is on the program that runs, never on a mention.
function Get-PgScriptFromCommandLine([string]$CommandLine) {
    $tokens = @([regex]::Matches($CommandLine, '"[^"]*"|\S+') | ForEach-Object { $_.Value.Trim('"') })
    for ($i = 1; $i -lt $tokens.Count; $i++) {
        $t = $tokens[$i]
        if ($t -in @('-c', '-m')) { return $null }           # inline code or a module: no script file
        if ($t.StartsWith('-')) { continue }                 # an option (including node's --env-file=...)
        return $t
    }
    return $null
}

function Get-PgProcesses([string]$Root) {
    $prefix = $Root.TrimEnd('\') + '\'
    $owned = @{}
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='node.exe'" -ErrorAction SilentlyContinue)) {
        $exe = [string]$p.ExecutablePath
        $script = Get-PgScriptFromCommandLine ([string]$p.CommandLine)
        $mine = ($exe -and $exe.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) -or ($script -and $script.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))
        if ($mine) { $owned[[int]$p.ProcessId] = $true }
    }
    return @($owned.Keys)
}

function Stop-PgProcesses([string]$Root) {
    $ids = @(Get-PgProcesses $Root)
    if (-not $ids.Count) { return 0 }
    # taskkill writes to stderr for a process that already exited; under $ErrorActionPreference = 'Stop' that is a raw
    # terminating error. Run it with 'Continue' and judge the result by listing the processes again.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { foreach ($id in $ids) { $null = & taskkill.exe /PID $id /T /F 2>&1 } }
    finally { $ErrorActionPreference = $old }
    $left = @()
    for ($try = 0; $try -lt 10; $try++) {
        $left = @(Get-PgProcesses $Root | Where-Object { $_ -in $ids })
        if (-not $left.Count) { break }
        Start-Sleep -Milliseconds 300
    }
    if ($left.Count) { throw "Could not stop $($left.Count) process(es) from this installation (process IDs: $($left -join ', ')). Quit your AI assistant, then try again." }
    return $ids.Count
}

# Runs "PatentGenius.cmd doctor" (no network call) and returns its lines. Never throws for a failing check: the lines say what failed.
function Invoke-PgDoctor($Paths) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $psi.Arguments = '/d /s /c ""' + $Paths.PgCmd + '" doctor"'
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit(90000)) { try { & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null } catch {}; return @{ Code = 1; Lines = @('The check did not finish within 90 seconds.') } }
    return @{ Code = $p.ExitCode; Lines = @(($out.Result + $err.Result) -split "`r?`n" | Where-Object { $_.Trim() }) }
}

# A TOML basic string with backslashes and quotes escaped.
function ConvertTo-TomlString([string]$Text) {
    return '"' + $Text.Replace('\', '\\').Replace('"', '\"') + '"'
}

# --- Deleting folders -------------------------------------------------------------------------------------------------

# \\?\ form of a local absolute path: Windows then skips its name rules, so entries such as CON, NUL or a name ending in a
# dot or space (which PowerShell cannot address by the ordinary path) can still be listed and deleted.
function ConvertTo-ExtendedPath([string]$Path) {
    if ($Path.StartsWith('\\?\')) { return $Path }
    return '\\?\' + [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Remove-Links([string]$Path) {
    # Walked by hand: Windows PowerShell's -Recurse can follow a junction into its target. .NET with \\?\ paths, because
    # Get-Item cannot see entries with reserved names.
    if (-not [IO.Directory]::Exists((ConvertTo-ExtendedPath $Path))) { return }
    $dir = New-Object IO.DirectoryInfo (ConvertTo-ExtendedPath $Path)
    foreach ($child in @($dir.EnumerateFileSystemInfos())) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($child -is [IO.DirectoryInfo]) { [IO.Directory]::Delete($child.FullName, $false) } else { [IO.File]::Delete($child.FullName) }
        } elseif ($child -is [IO.DirectoryInfo]) { Remove-Links $child.FullName }
    }
}

# Removes one file or folder (never following links). Tries Remove-Item first; entries PowerShell cannot remove (reserved
# device names, trailing dots or spaces, very long paths) are retried with \\?\ paths and robocopy. No command shell is
# used, so no character in a path can be reinterpreted. Pass only paths inside a folder whose marker file was checked.
function Remove-Tree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        # The folder itself is a link: remove the link, never its target.
        if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName, $false) } else { [IO.File]::Delete($item.FullName) }
        return
    }
    # Unlink junctions and symbolic links first, without following them, so a linked folder's target survives.
    Remove-Links $Path
    if ($item.PSIsContainer) {
        try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop } catch {}
        if (Test-Path -LiteralPath $Path) {
            $ext = ConvertTo-ExtendedPath $Path
            try { [IO.Directory]::Delete($ext, $true) } catch {}
        }
        if (Test-Path -LiteralPath $Path) {
            # Long paths: empty the folder with robocopy, then remove it.
            $empty = Join-Path ([IO.Path]::GetTempPath()) ('pg-empty-' + [Guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $empty | Out-Null
            $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try { $null = & robocopy.exe $empty $Path /MIR /XJ /R:1 /W:1 /NFL /NDL /NJH /NJS /NP } finally { $ErrorActionPreference = $old }
            Remove-Item -LiteralPath $empty -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        try { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop } catch { try { [IO.File]::Delete((ConvertTo-ExtendedPath $Path)) } catch {} }
    }
    if (Test-Path -LiteralPath $Path) { throw "Could not remove $Path. Close any program using files there and try again." }
}

# Deletes a folder that Setup created and marked. Everything is removed first; the entries named in $KeepLast (the program
# folder's installer\ scripts, installer.json and the marker file) go last, in that order, and only if everything else
# succeeded. A partial failure therefore leaves the marker and the uninstaller in place so the uninstall can be run again.
# Returns $true when $Path itself was a link (junction or symbolic link): only the link is removed and its target is left
# exactly as it is. Otherwise returns $false.
function Remove-OwnedTree([string]$Path, [string[]]$KeepLast) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $self = Get-Item -LiteralPath $Path -Force
    if ($self.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        if ($self.PSIsContainer) { [IO.Directory]::Delete($self.FullName, $false) } else { [IO.File]::Delete($self.FullName) }
        if (Test-Path -LiteralPath $Path) { throw "Could not remove the link $Path." }
        return $true
    }
    $failures = New-Object Collections.Generic.List[string]
    foreach ($entry in @([IO.Directory]::EnumerateFileSystemEntries((ConvertTo-ExtendedPath $Path)))) {
        $leaf = [IO.Path]::GetFileName($entry.TrimEnd('\'))
        if ($leaf -in $KeepLast) { continue }
        try { Remove-Tree $entry } catch { $failures.Add($_.Exception.Message) }
    }
    if ($failures.Count) { throw ($failures -join ' ') }
    foreach ($name in $KeepLast) { Remove-Tree (Join-Path $Path $name) }
    Remove-Tree $Path
    return $false
}

# --- PatentGenius.cmd -------------------------------------------------------------------------------------------------

# The command line ("pg"): this installation's data folder, Python and (when found) Hub command and Node. Written as UTF-8 (no BOM)
# and run under code page 65001, so folders with accented or non-Latin characters work. The caller's code page is restored
# afterwards (taken from the last word of chcp's output, digits only: some languages print "Active code page: 850." with a final
# period), and the program's exit code is passed on. It holds paths only: no key, no secret.
# A path that can be written safely inside PatentGenius.cmd (inside set "NAME=...", where %VAR% would be expanded): no percent sign, no quote.
function Test-PgRecordablePath([string]$Path) {
    return -not ($Path.Contains('%') -or $Path.Contains('"'))
}

function Get-PgCmdText([string]$PythonExe, [string]$NodeExe, [string]$HubCmd, $Paths) {
    $lines = @(
        '@echo off',
        'rem Patent Genius command line for this installation. Example: PatentGenius.cmd doctor',
        'setlocal',
        'set "PG_CP="',
        'for /f "delims=" %%a in (''chcp'') do for %%t in (%%a) do set "PG_CP=%%t"',
        'set "PG_CP=%PG_CP:.=%"',
        'echo(%PG_CP%|findstr /r /x "[0-9][0-9]*" >nul || set "PG_CP="',
        'chcp 65001 >nul',
        'set "PYTHONUTF8=1"',
        'set "PYTHONDONTWRITEBYTECODE=1"',
        ('set "PG_TOOL_DATA_DIR=' + $Paths.DataRoot + '"'),
        ('set "PG_PYTHON=' + $PythonExe + '"')
    )
    if ($NodeExe) { $lines += ('set "PG_TOOL_NODE=' + $NodeExe + '"') }
    if ($HubCmd) { $lines += ('set "PG_HUB_CMD=' + $HubCmd + '"') }
    $lines += @(
        ('"' + $PythonExe + '" "%~dp0pg_cli.py" %*'),
        'set "PG_EXIT=%ERRORLEVEL%"',
        'if defined PG_CP chcp %PG_CP% >nul',
        'exit /b %PG_EXIT%',
        ''
    )
    return ($lines -join "`r`n")
}

# --- Shortcuts ----------------------------------------------------------------------------------------------------------
# WScript.Shell stores shortcut paths, arguments and descriptions in the ANSI code page. A character outside it (for example
# Japanese on a Western Windows) is replaced by "?", and Save() fails for a .lnk path containing one.

# Returns $Path unchanged if the ANSI code page can hold it, otherwise its 8.3 short form if that can, otherwise $null.
function Get-PgAnsiSafePath([string]$Path) {
    $ansi = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage)
    $holds = { param($s) $ansi.GetString($ansi.GetBytes($s)) -ceq $s }
    if (& $holds $Path) { return $Path }
    try {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        $short = if ([IO.Directory]::Exists($Path)) { $fso.GetFolder($Path).ShortPath } elseif ([IO.File]::Exists($Path)) { $fso.GetFile($Path).ShortPath } else { $null }
        if ($short -and (& $holds $short)) { return $short }
    } catch {}
    return $null
}

# A folder, existing or not, in which a temporary .lnk can be saved by name that the ANSI code page holds.
function Get-PgShortcutStagingFolder([string[]]$Candidates) {
    foreach ($c in $Candidates) {
        if (-not $c -or -not [IO.Directory]::Exists($c)) { continue }
        $safe = Get-PgAnsiSafePath $c
        if ($safe) { return $safe }
    }
    return $null
}

# Saves a shortcut in an ANSI-safe staging folder, then moves the file with .NET (which is Unicode-safe) into $Folder.
# Returns the final path. Throws a plain message if either step fails.
function Save-PgShortcut([string]$Folder, [string]$Name, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Description, [string]$IconLocation, [string[]]$StagingCandidates) {
    $stage = Get-PgShortcutStagingFolder $StagingCandidates
    if (-not $stage) { throw 'No folder with a plain (ANSI) path was found to build the shortcut in.' }
    $tmp = Join-Path $stage ('pg-' + [Guid]::NewGuid().ToString('N') + '.lnk')
    try {
        $s = (New-Object -ComObject WScript.Shell).CreateShortcut($tmp)
        $s.TargetPath = $Target; $s.Arguments = $Arguments; $s.WorkingDirectory = $WorkDir; $s.Description = $Description
        if ($IconLocation) { $s.IconLocation = $IconLocation }
        $s.Save()
        New-Item -ItemType Directory -Force -Path $Folder | Out-Null
        $dest = Join-Path $Folder ($Name + '.lnk')
        [IO.File]::Copy($tmp, $dest, $true)
        return $dest
    } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
}

# Checks, before anything is installed, that shortcuts can be created in $Folder (which may not exist yet): saves a test
# shortcut through the same route and removes it again. Returns $null when it works, otherwise the reason.
function Test-PgShortcutFolder([string]$Folder, [string[]]$StagingCandidates) {
    # An existing FILE anywhere on the path can never become a folder.
    for ($p = $Folder; $p; $p = Split-Path -Parent $p) { if ([IO.File]::Exists($p)) { return "$p is a file, not a folder." } }
    $probeFolder = $Folder
    while ($probeFolder -and -not [IO.Directory]::Exists($probeFolder)) { $probeFolder = Split-Path -Parent $probeFolder }
    if (-not $probeFolder) { return "No part of the shortcut folder path exists." }
    try {
        $dest = Save-PgShortcut $probeFolder ('pg-test-' + [Guid]::NewGuid().ToString('N')) (Join-Path $env:SystemRoot 'System32\notepad.exe') '' $env:SystemRoot 'test' $null $StagingCandidates
        Remove-Item -LiteralPath $dest -Force
        return $null
    } catch { return $_.Exception.Message }
}
