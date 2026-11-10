# Test install.ps1 without GitHub and without touching your real folders. Runs on Windows PowerShell 5.1 and PowerShell 7:
#   powershell -NoProfile -File scripts\test_install.ps1
#   pwsh -NoProfile -File scripts/test_install.ps1
# The ASCII and syntax checks run anywhere. The rest needs Windows: it installs from this checkout (WOWMENTOR_SRC)
# into a temporary LOCALAPPDATA with a fake WoW folder, runs `wowmentor setup` in the no-services, no-build mode,
# and checks the shims, the addon files and the user PATH. It puts your PATH back when it finishes.
# This file is ASCII only, like install.ps1.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$onWindows = ([Environment]::OSVersion.Platform -eq 'Win32NT')

function Pass { param([string]$Message) Write-Host ('ok: ' + $Message) }
function Fail { param([string]$Message) throw ('FAIL: ' + $Message) }
function Assert { param($Condition, [string]$Message) if (-not $Condition) { Fail $Message } }
function Join-Items { param($Items) return (@($Items) -join '|') }

# 1. Plain ASCII: Windows PowerShell 5.1 reads a BOM-less file as ANSI, so a stray dash would corrupt it.
$asciiFiles = @('install.ps1', 'scripts\test_install.ps1', 'Install-WoWMentor.cmd', 'wowmentor.cmd', 'wowui.cmd')
foreach ($rel in $asciiFiles) {
    $path = Join-Path $root $rel
    Assert (Test-Path -LiteralPath $path) ($rel + ' is missing')
    $bytes = [IO.File]::ReadAllBytes($path)
    $bad = @($bytes | Where-Object { $_ -gt 127 })
    Assert ($bad.Count -eq 0) ($rel + ' has ' + $bad.Count + ' non-ASCII bytes')
}
Pass 'installer files are ASCII only'

# 2. The PowerShell parser accepts both scripts (the same parser the engine uses).
foreach ($rel in @('install.ps1', 'scripts\test_install.ps1')) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root $rel), [ref]$tokens, [ref]$errors)
    Assert ($errors.Count -eq 0) ($rel + ' has syntax errors: ' + (($errors | ForEach-Object { $_.Message }) -join '; '))
}
Pass 'install.ps1 and this test parse without errors'

# 3. No admin tricks and no unsafe habits (the script must stay readable and boring for antivirus tools).
$text = [IO.File]::ReadAllText((Join-Path $root 'install.ps1'))
foreach ($needle in @('-EncodedCommand', '-ExecutionPolicy', 'WindowStyle Hidden', 'RunAs', 'Set-ExecutionPolicy')) {
    Assert (-not $text.Contains($needle)) ('install.ps1 must not contain ' + $needle)
}
Assert ($text -match 'Tls12') 'install.ps1 must force TLS 1.2'
Assert ($text -match "ProgressPreference = 'SilentlyContinue'") 'install.ps1 must silence progress bars'
Assert ($text -match 'WindowsApps') 'install.ps1 must reject the Microsoft Store python stub'
Pass 'install.ps1 has no hidden-window, bypass or admin patterns'

if (-not $onWindows) {
    Write-Host 'skip: the install run needs Windows'
    Write-Host 'install.ps1 static tests passed'
    return
}

# 4. Pure logic, loaded without installing anything.
$env:WOWMENTOR_PS_NOMAIN = '1'
. (Join-Path $root 'install.ps1')
Remove-Item Env:\WOWMENTOR_PS_NOMAIN

$words = Join-Items (Split-WmArgString '--wow-dir "D:\Games\World of Warcraft\_forever_" --no-build')
Assert ($words -eq '--wow-dir|D:\Games\World of Warcraft\_forever_|--no-build') ('Split-WmArgString gave: ' + $words)
Assert ((@(Split-WmArgString $null)).Count -eq 0) 'Split-WmArgString of nothing should be empty'
$conv = Join-Items (ConvertTo-WmSetupArgs @('-WowDir', 'C:\x y', '-NoServices', '-nobuild', '--keep'))
Assert ($conv -eq '--wow-dir|C:\x y|--no-services|--no-build|--keep') ('ConvertTo-WmSetupArgs gave: ' + $conv)
Assert (Test-WmStorePython 'C:\Users\a\AppData\Local\Microsoft\WindowsApps\python.exe') 'Store stub not rejected'
Assert (Test-WmStorePython 'c:\users\a\appdata\local\microsoft\windowsapps\PYTHON3.EXE') 'Store stub check must ignore case'
Assert (-not (Test-WmStorePython 'C:\Python312\python.exe')) 'real python wrongly rejected'
Assert (-not (Test-WmStorePython 'C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_x\python.exe')) 'a Store-installed real python must be allowed'
Assert (Test-WmStorePython '') 'empty path must be rejected'
Assert ($null -eq (Get-WmPathWith -Current 'C:\a;C:\Tools\Bin\;C:\b' -Dir 'c:\tools\bin')) 'present entry (case, slash) must not be added again'
Assert ((Get-WmPathWith -Current 'C:\a;' -Dir 'C:\z') -eq 'C:\a;C:\z') 'entry should be appended once'
Assert ((Get-WmPathWith -Current '' -Dir 'C:\z') -eq 'C:\z') 'empty PATH should become the entry'
$shim = Get-WmShimText -PythonExe 'C:\Py 3\python.exe' -Script 'C:\App Dir\wowmentor.sh'
Assert ($shim.Contains('"C:\Py 3\python.exe" -X utf8 "C:\App Dir\wowmentor.sh" %*')) 'shim line is wrong'
Assert ($shim.Contains("`r`n") -and (-not $shim.Replace("`r`n", '').Contains("`n"))) 'shim must use CRLF line ends only'
Assert (Test-WmUnsafeDest 'C:\') 'drive root must be refused'
Assert (Test-WmUnsafeDest $env:USERPROFILE) 'home folder must be refused'
Assert (Test-WmUnsafeDest '') 'empty destination must be refused'
Assert (-not (Test-WmUnsafeDest (Join-Path $env:USERPROFILE 'WoWMentorTest\app'))) 'a normal subfolder must be allowed'
$found = Find-WmPython
Assert ($found -and (-not (Test-WmStorePython $found))) 'Find-WmPython found no usable python on this runner'
Pass 'pure helpers behave (args, python filter, PATH merge, shim text, unsafe folders)'

# 5. A full install into a sandbox.
$shell = (Get-Process -Id $PID).Path   # the shell running this test: 5.1 or 7. -ExecutionPolicy Bypass is for this test only; users run the installer through iex.
$sb = Join-Path ([IO.Path]::GetTempPath()) ('wm test ' + [Guid]::NewGuid().ToString('N').Substring(0, 8))   # a space on purpose
$local = Join-Path $sb 'local'
$roaming = Join-Path $sb 'roaming'
$config = Join-Path $sb 'config'
$wow = Join-Path $sb 'World of Warcraft\_forever_'
$app = Join-Path $local 'WoWMentor\app'
$bin = Join-Path $local 'WoWMentor\bin'
foreach ($d in @($local, $roaming, $config, (Join-Path $wow 'Interface\AddOns'))) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

$managed = @('LOCALAPPDATA', 'APPDATA', 'WOWMENTOR_HOME', 'WOWMENTOR_LOGS', 'WOWMENTOR_SRC', 'WOWMENTOR_DIR', 'WOWMENTOR_TARBALL_URL',
             'WOWMENTOR_SETUP_ARGS', 'WOWMENTOR_NO_BROADCAST', 'WOWMENTOR_NO_PAUSE', 'WOWMENTOR_PS_NOMAIN')
$saved = @{}
foreach ($n in $managed) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }

$envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$hadPath = ($envKey.GetValueNames() -contains 'Path')
$origPath = $null
$origKind = [Microsoft.Win32.RegistryValueKind]::ExpandString
if ($hadPath) {
    $origPath = $envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $origKind = $envKey.GetValueKind('Path')
}

function Invoke-Native {
    # Run a program, capture all output and the exit code. Errors on stderr must not stop this script.
    param([string]$Exe, [string[]]$Arguments = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $text = (& $Exe @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ Output = $text; Code = $code }
}

function Set-TestEnv {
    param([hashtable]$Values)
    foreach ($n in $managed) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
    $base = @{ LOCALAPPDATA = $local; APPDATA = $roaming; WOWMENTOR_HOME = $config; WOWMENTOR_LOGS = (Join-Path $sb 'logs')
               WOWMENTOR_NO_BROADCAST = '1'; WOWMENTOR_NO_PAUSE = '1' }
    foreach ($k in $Values.Keys) { $base[$k] = $Values[$k] }
    foreach ($k in $base.Keys) { [Environment]::SetEnvironmentVariable($k, [string]$base[$k], 'Process') }
}

function Get-PathCount {
    $raw = [string]$envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    return @($raw -split ';' | Where-Object { $_.TrimEnd('\') -ieq $bin.TrimEnd('\') }).Count
}

try {
    # 5a. Refusals first: nothing may be installed.
    Set-TestEnv @{ WOWMENTOR_SRC = $root; WOWMENTOR_DIR = $env:USERPROFILE }
    $r = Invoke-Native $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $root 'install.ps1'))
    Assert ($r.Code -ne 0) 'installing into the home folder should fail'
    Assert ($r.Output -match 'Refusing') ('no refusal message: ' + $r.Output)
    Set-TestEnv @{ WOWMENTOR_SRC = $sb }
    $r = Invoke-Native $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $root 'install.ps1'))
    Assert (($r.Code -ne 0) -and ($r.Output -match 'has no wowmentor.sh')) ('empty WOWMENTOR_SRC should fail: ' + $r.Output)
    Set-TestEnv @{ WOWMENTOR_TARBALL_URL = ('file:///' + ($sb -replace '\\', '/') + '/missing.zip') }
    $r = Invoke-Native $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $root 'install.ps1'))
    Assert (($r.Code -ne 0) -and ($r.Output -match 'download failed')) ('missing download should fail clearly: ' + $r.Output)
    Assert (-not (Test-Path -LiteralPath $app)) 'a failed install left a partial app folder'
    Set-Content -LiteralPath (Join-Path $sb 'bad.zip') -Value 'not a zip'
    Set-TestEnv @{ WOWMENTOR_TARBALL_URL = (Join-Path $sb 'bad.zip') }
    $r = Invoke-Native $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $root 'install.ps1'))
    Assert (($r.Code -ne 0) -and ($r.Output -match 'not a valid archive')) ('bad archive should fail clearly: ' + $r.Output)
    Assert (-not (Test-Path -LiteralPath $app)) 'a bad archive left a partial app folder'
    Pass 'refuses unsafe folders, empty sources and bad downloads cleanly'

    # 5b. Full install from this checkout, options given the PowerShell way.
    Set-TestEnv @{ WOWMENTOR_SRC = $root }
    $r = Invoke-Native $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $root 'install.ps1'), '-WowDir', $wow, '-NoServices', '-NoBuild')
    Assert ($r.Code -eq 0) ('install.ps1 failed: ' + $r.Output)
    Assert ($r.Output -match '1/5  Found WoW at') ('no setup output: ' + $r.Output)
    Assert ($r.Output -match 'Running setup') 'no "Running setup" message'
    Assert (Test-Path -LiteralPath (Join-Path $app 'wowmentor.sh')) 'program not installed'
    Assert (-not (Test-Path -LiteralPath (Join-Path $app '.git'))) '.git was copied'
    Assert ((Test-Path -LiteralPath (Join-Path $wow 'Interface\AddOns\WoWFeed')) -and (Test-Path -LiteralPath (Join-Path $wow 'Interface\AddOns\WFSlot1000'))) 'addon files did not land'
    $toc = Join-Path $wow 'Interface\AddOns\WoWFeed\WoWFeed.toc'
    Assert (Test-Path -LiteralPath $toc) 'WoWFeed.toc missing'
    Assert (-not ([IO.File]::ReadAllText($toc).Contains("`r"))) 'WoWFeed.toc must use LF line ends'
    Assert ((Test-Path -LiteralPath (Join-Path $bin 'wowmentor.cmd')) -and (Test-Path -LiteralPath (Join-Path $bin 'wowui.cmd'))) 'shims missing'
    Assert ((Get-PathCount) -eq 1) 'bin folder should be on the user PATH exactly once'
    $v = Invoke-Native (Join-Path $bin 'wowmentor.cmd') @('--version')
    Assert (($v.Code -eq 0) -and ($v.Output -match '^wowmentor ')) ('wowmentor.cmd --version: ' + $v.Output)
    $h = Invoke-Native (Join-Path $bin 'wowui.cmd') @('--help')
    Assert ($h.Code -eq 0) ('wowui.cmd --help failed: ' + $h.Output)
    $links = Invoke-Native (Join-Path $bin 'wowmentor.cmd') @('links')
    Assert (($links.Code -eq 0) -and ($links.Output -match 'Paste this to your AI')) ('wowmentor.cmd links: ' + $links.Output)
    Assert (Test-Path -LiteralPath (Join-Path $config 'config.json')) 'config.json not created in WOWMENTOR_HOME'
    Pass 'install.ps1 -WowDir: program, addon, shims and PATH entry are in place'

    # 5c. The double-click wrapper over the top: updates in place, keeps the secrets, does not duplicate PATH.
    $before = (Get-FileHash -LiteralPath (Join-Path $config 'config.json')).Hash
    Set-TestEnv @{ WOWMENTOR_SETUP_ARGS = ('--wow-dir "' + $wow + '" --no-services --no-build') }
    $r = Invoke-Native (Join-Path $root 'Install-WoWMentor.cmd')
    Assert ($r.Code -eq 0) ('Install-WoWMentor.cmd failed: ' + $r.Output)
    Assert ($r.Output -match '1/5  Found WoW at') ('wrapper did not run setup: ' + $r.Output)
    Assert ((Get-FileHash -LiteralPath (Join-Path $config 'config.json')).Hash -eq $before) 're-install changed the secrets'
    Assert ((Get-PathCount) -eq 1) 'PATH entry was duplicated by the second install'
    $v = Invoke-Native (Join-Path $bin 'wowmentor.cmd') @('--version')
    Assert ($v.Code -eq 0) 'shim stopped working after the update'
    Pass 'Install-WoWMentor.cmd updates in place and keeps the config'

    # 5d. The shims that live in the repo folder (for people who unzip and never install).
    Set-TestEnv @{}
    $v = Invoke-Native (Join-Path $root 'wowmentor.cmd') @('--version')
    Assert (($v.Code -eq 0) -and ($v.Output -match '^wowmentor ')) ('repo wowmentor.cmd: ' + $v.Output)
    $h = Invoke-Native (Join-Path $root 'wowui.cmd') @('--help')
    Assert ($h.Code -eq 0) ('repo wowui.cmd --help: ' + $h.Output)
    Pass 'wowmentor.cmd and wowui.cmd in the repo folder work'
} finally {
    foreach ($n in $managed) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    if ($hadPath) { $envKey.SetValue('Path', $origPath, $origKind) } else { $envKey.DeleteValue('Path', $false) }
    $envKey.Close()
    Remove-Item -LiteralPath $sb -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host 'install.ps1 tests passed'
