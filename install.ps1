# WoW Mentor installer for Windows. Paste into PowerShell (a normal window; no admin needed):
#   irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 | iex
# It downloads the app into %LOCALAPPDATA%\WoWMentor\app, then runs `wowmentor setup`. Safe to run again to update.
#
# Why `irm | iex`: the text goes straight into PowerShell without a file on disk, so the execution policy
# (which only checks script FILES) never blocks it, and nothing you download carries the "from the internet" mark.
# It is also easy to inspect first. If you would rather read it before it runs:
#   irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 -OutFile install.ps1
#   notepad install.ps1
#   Get-Content -Raw .\install.ps1 | iex
# (Windows blocks unsigned .ps1 files you downloaded in a browser, which is why the last line reads the file
# and pipes it in instead of running it. This file is plain ASCII on purpose, so you can read all of it.)
#
# Extra options go to `wowmentor setup`. With the one-liner use the environment variable, or the script-block form:
#   $env:WOWMENTOR_SETUP_ARGS = '--wow-dir "D:\Games\World of Warcraft\_forever_"'
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1))) -WowDir "D:\Games\World of Warcraft\_forever_"
# -WowDir, -NoServices and -NoBuild are accepted as aliases of --wow-dir, --no-services and --no-build.
#
# Environment overrides (all optional; mainly for testing and for pinning a release):
#   WOWMENTOR_REPO         owner/name on GitHub              (default PhilipAD/wow-mentor)
#   WOWMENTOR_REF          branch, tag or commit to download (default main; for example v1.0.0)
#   WOWMENTOR_DIR          where the program is installed    (default %LOCALAPPDATA%\WoWMentor\app)
#   WOWMENTOR_TARBALL_URL  download this .zip (or .tar.gz) instead of GitHub (https://, file://, or a local path)
#   WOWMENTOR_SRC          copy this local folder instead of downloading anything
#   WOWMENTOR_SETUP_ARGS   extra words for `wowmentor setup`, for example: --wow-dir "D:\Games\World of Warcraft\_forever_"
#   WOWMENTOR_NO_PATH     set to 1 to leave your PATH alone (tests)
#   WOWMENTOR_NO_BROADCAST set to 1 to skip telling Windows the PATH changed (tests)
#   WOWMENTOR_PS_NOMAIN    set to 1 to load the functions without installing (tests)
#
# Everything is inside functions so that pasting it into your session does not leave variables behind,
# and nothing calls `exit` (which would close your PowerShell window when run through iex).

function WmSay {
    param([string]$Message)
    Write-Host $Message
}

function WmFail {
    param([string]$Message)
    throw (New-Object System.InvalidOperationException($Message))
}

# ---------------------------------------------------------------------------------------------
# Pure helpers (no machine state): these are unit-tested by scripts/test_install.ps1.
# ---------------------------------------------------------------------------------------------

function Split-WmArgString {
    # Split 'a --b "c d"' into words, honouring double quotes. Why not -split: paths with spaces.
    param([string]$Text)
    $out = New-Object System.Collections.Generic.List[string]
    $cur = New-Object System.Text.StringBuilder
    $inQuote = $false
    $has = $false
    if ($Text) {
        foreach ($ch in $Text.ToCharArray()) {
            if ($ch -eq [char]'"') {
                $inQuote = -not $inQuote
                $has = $true
                continue
            }
            if ((-not $inQuote) -and [char]::IsWhiteSpace($ch)) {
                if ($has) {
                    [void]$out.Add($cur.ToString())
                    [void]$cur.Clear()
                    $has = $false
                }
                continue
            }
            [void]$cur.Append($ch)
            $has = $true
        }
        if ($has) { [void]$out.Add($cur.ToString()) }
    }
    return $out.ToArray()
}

function ConvertTo-WmSetupArgs {
    # PowerShell style flags -> the words `wowmentor setup` understands. Anything else passes through.
    param([object[]]$Raw = @())
    $map = @{ '-wowdir' = '--wow-dir'; '-noservices' = '--no-services'; '-nobuild' = '--no-build' }
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($item in $Raw) {
        $s = [string]$item
        $key = $s.ToLowerInvariant()
        if ($map.ContainsKey($key)) { [void]$out.Add($map[$key]) } else { [void]$out.Add($s) }
    }
    return $out.ToArray()
}

function Test-WmStorePython {
    # The "python.exe" in ...\Microsoft\WindowsApps is only a stub that opens the Microsoft Store.
    # https://docs.python.org/3/using/windows.html (App Execution Aliases)
    param([string]$Path)
    if (-not $Path) { return $true }
    return ($Path -like '*\Microsoft\WindowsApps\*')
}

function Get-WmPathWith {
    # Return the PATH text with $Dir added, or $null when it is already in there.
    param([string]$Current, [string]$Dir)
    $want = $Dir.TrimEnd('\').ToLowerInvariant()
    if ($Current) {
        foreach ($part in ($Current -split ';')) {
            $p = [Environment]::ExpandEnvironmentVariables($part.Trim()).TrimEnd('\').ToLowerInvariant()
            if ($p -eq $want) { return $null }
        }
    }
    if ([string]::IsNullOrEmpty($Current)) { return $Dir }
    return ($Current.TrimEnd(';') + ';' + $Dir)
}

function Get-WmShimText {
    # A .cmd file that runs one of our Python scripts with the interpreter found at install time.
    # -X utf8 so output never fails on a legacy code page. CRLF because cmd.exe wants it.
    param([string]$PythonExe, [string]$Script)
    $lines = @(
        '@echo off',
        'rem Written by the WoW Mentor installer. Safe to delete; run install.ps1 again to recreate it.',
        ('"' + $PythonExe + '" -X utf8 "' + $Script + '" %*'),
        'exit /b %errorlevel%'
    )
    return (($lines -join "`r`n") + "`r`n")
}

function Test-WmUnsafeDest {
    # Refuse to wipe a folder that is obviously not ours (the installer deletes DEST before copying).
    param([string]$Dest)
    if (-not $Dest) { return $true }
    $full = [IO.Path]::GetFullPath($Dest).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -eq $root.TrimEnd('\')) { return $true }
    foreach ($name in @('USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'ProgramFiles', 'SystemRoot', 'TEMP')) {
        $v = [Environment]::GetEnvironmentVariable($name)
        if ($v -and ($full -eq $v.TrimEnd('\'))) { return $true }
    }
    return $false
}

# ---------------------------------------------------------------------------------------------
# Machine helpers
# ---------------------------------------------------------------------------------------------

function Test-WmInteractive {
    try {
        return ([Environment]::UserInteractive -and (-not [Console]::IsInputRedirected))
    } catch {
        return $false
    }
}

function Read-WmYes {
    param([string]$Question)
    if (-not (Test-WmInteractive)) { return $false }
    $answer = Read-Host ($Question + ' [Y/n]')
    return (($answer -eq '') -or ($answer -match '^(y|yes)$'))
}

function Test-WmWritable {
    param([string]$Dir)
    try {
        $probe = Join-Path $Dir ('.wowmentor-write-test-' + [Guid]::NewGuid().ToString('N'))
        [IO.File]::WriteAllText($probe, 'x')
        Remove-Item -LiteralPath $probe -Force
        return $true
    } catch {
        return $false
    }
}

function Test-WmPythonExe {
    # Run one interpreter and return its real path if it is Python 3.9 or newer, else $null.
    param([string]$Exe, [string[]]$Pre = @())
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Pre -c "import sys; print(sys.executable); print(int(sys.version_info >= (3, 9)))" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $lines = @($out | ForEach-Object { ([string]$_).Trim() })
        if ($lines.Count -lt 2) { return $null }
        if ($lines[1] -ne '1') { return $null }
        if (Test-WmStorePython $lines[0]) { return $null }
        if (-not (Test-Path -LiteralPath $lines[0])) { return $null }
        return $lines[0]
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
}

function Get-WmRegistryPythons {
    # PEP 514: every python.org / winget Python writes its location under ...\Python\PythonCore\<version>\InstallPath.
    # Reading it works even right after an install, before PATH has caught up.
    $found = New-Object System.Collections.Generic.List[string]
    $roots = @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $keys = @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)
        $keys = $keys | Sort-Object { try { [version]($_.PSChildName -replace '-.*$', '') } catch { [version]'0.0' } } -Descending
        foreach ($k in $keys) {
            try {
                $props = Get-ItemProperty -LiteralPath ($k.PSPath + '\InstallPath') -ErrorAction Stop
                $exe = $props.ExecutablePath
                if (-not $exe) { $exe = Join-Path $props.'(default)' 'python.exe' }
                if ($exe -and (Test-Path -LiteralPath $exe)) { [void]$found.Add([string]$exe) }
            } catch {
                continue
            }
        }
    }
    return $found.ToArray()
}

function Find-WmPython {
    # py -3 first (it ignores the Store stubs), then python/python3 on PATH, then the registry.
    $py = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        $r = Test-WmPythonExe -Exe $py.Source -Pre @('-3')
        if ($r) { return $r }
    }
    foreach ($name in @('python', 'python3')) {
        foreach ($c in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($c.CommandType -ne 'Application') { continue }
            $src = [string]$c.Source
            if (Test-WmStorePython $src) { continue }
            $r = Test-WmPythonExe -Exe $src
            if ($r) { return $r }
        }
    }
    foreach ($exe in (Get-WmRegistryPythons)) {
        if (Test-WmStorePython $exe) { continue }
        $r = Test-WmPythonExe -Exe $exe
        if ($r) { return $r }
    }
    return $null
}

function Update-WmSessionPath {
    # winget changes the PATH in the registry, not in this window. Rebuild ours from the registry.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($machine, $user, $env:Path) | Where-Object { $_ }
    $env:Path = ($parts -join ';')
}

function Get-WmPythonOrInstall {
    $py = Find-WmPython
    if ($py) { return $py }
    WmSay ''
    WmSay 'Python 3.9 or newer was not found on this PC.'
    WmSay '(The "python" that opens the Microsoft Store does not count. It is only a shortcut.)'
    $winget = Get-Command winget -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($winget) {
        WmSay 'Windows can install it for you, just for your user account, with this command (no admin needed):'
        WmSay '  winget install --id Python.Python.3.12 -e --scope user'
        if (Read-WmYes 'Run that now?') {
            $old = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                & $winget.Source install --id Python.Python.3.12 -e --scope user | Out-Host
            } finally {
                $ErrorActionPreference = $old
            }
            Update-WmSessionPath
            $py = Find-WmPython
            if ($py) { return $py }
            WmFail ('Python was installed but this window cannot see it yet.' + "`n" + 'Close this PowerShell window, open a new one, and paste the install line again.')
        }
        WmFail ('OK, nothing was installed.' + "`n" + 'Install Python 3.9 or newer from https://www.python.org/downloads/windows/ and paste the install line again.')
    }
    WmFail ('Install Python 3.9 or newer from https://www.python.org/downloads/windows/ (the normal installer is fine, no admin needed),' + "`n" + 'then paste the install line again.')
}

function Get-WmArchive {
    param([string]$Url, [string]$OutFile)
    if ($Url -match '^file:') {
        Copy-Item -LiteralPath ([Uri]$Url).LocalPath -Destination $OutFile -Force
        return
    }
    $isLocal = $false
    try { $isLocal = Test-Path -LiteralPath $Url -PathType Leaf } catch { $isLocal = $false }
    if ($isLocal) {
        Copy-Item -LiteralPath $Url -Destination $OutFile -Force
        return
    }
    # Invoke-WebRequest adds no "downloaded from the internet" mark to what it saves.
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile
}

function Expand-WmArchive {
    param([string]$File, [string]$Dest)
    if ($File -match '\.(tar\.gz|tgz)$') {
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        & tar.exe -xzf $File -C $Dest
        if ($LASTEXITCODE -ne 0) { WmFail 'The download is not a valid archive. Please try again.' }
    } else {
        Expand-Archive -LiteralPath $File -DestinationPath $Dest -Force
    }
}

function Copy-WmSource {
    # Copy a local checkout without version-control data and caches (same idea as install.sh).
    # robocopy treats a trailing backslash before a quote as an escape, so trim it.
    param([string]$Src, [string]$Target)
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    & robocopy.exe $Src.TrimEnd('\') $Target.TrimEnd('\') /E /XD .git __pycache__ .venv .pytest_cache /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { WmFail 'Could not copy WoW Mentor from the local folder.' }
}

function Remove-WmFolder {
    # Antivirus and Explorer sometimes hold a file for a moment, so try a few times.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    for ($i = 1; $i -le 4; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force
            return
        } catch {
            if ($i -eq 4) {
                WmFail ('Could not replace ' + $Path + ' because a file in it is in use.' + "`n" + 'Run `wowmentor stop`, close any window showing that folder, and paste the install line again.')
            }
            Start-Sleep -Seconds 1
        }
    }
}

function Add-WmUserPath {
    # Edit HKCU\Environment\Path directly. [Environment]::SetEnvironmentVariable would expand %VARS% and
    # turn the value into plain text; the raw read keeps the user's own entries exactly as they were.
    param([string]$Dir)
    $reg = [Microsoft.Win32.Registry]::CurrentUser
    $key = $reg.OpenSubKey('Environment', $true)
    if ($null -eq $key) { $key = $reg.CreateSubKey('Environment') }
    try {
        $cur = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $new = Get-WmPathWith -Current $cur -Dir $Dir
        if ($null -ne $new) {
            $key.SetValue('Path', $new, [Microsoft.Win32.RegistryValueKind]::ExpandString)
        }
    } finally {
        $key.Close()
    }
    $changed = ($null -ne $new)
    if ($null -ne (Get-WmPathWith -Current $env:Path -Dir $Dir)) {
        $env:Path = $env:Path.TrimEnd(';') + ';' + $Dir
    }
    return $changed
}

function Send-WmEnvironmentChange {
    # Best effort: tell Explorer the environment changed so the NEXT PowerShell you open from the Start menu
    # sees the new PATH without signing out. WM_SETTINGCHANGE = 0x1A, SMTO_ABORTIFHUNG = 2.
    # https://learn.microsoft.com/windows/win32/winmsg/wm-settingchange
    if ($env:WOWMENTOR_NO_BROADCAST) { return }
    try {
        $sig = '[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);'
        if (-not ('WoWMentorInstall.Native' -as [type])) {
            Add-Type -Namespace WoWMentorInstall -Name Native -MemberDefinition $sig
        }
        $res = [UIntPtr]::Zero
        [void][WoWMentorInstall.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 2000, [ref]$res)
    } catch {
        return
    }
}

function Get-WmReadOnlyAddOnDirs {
    # Which AddOns folders is this user not allowed to write to? (C:\Program Files is the usual culprit.)
    param([string[]]$Extra = @())
    $bad = New-Object System.Collections.Generic.List[string]
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($e in $Extra) { if ($e) { [void]$candidates.Add((Join-Path $e 'Interface\AddOns')) } }
    foreach ($root in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $root) { continue }
        $base = Join-Path $root 'World of Warcraft'
        if (-not (Test-Path -LiteralPath $base)) { continue }
        foreach ($c in @(Get-ChildItem -LiteralPath $base -Directory -Filter '_*_' -ErrorAction SilentlyContinue)) {
            [void]$candidates.Add((Join-Path $c.FullName 'Interface\AddOns'))
        }
    }
    foreach ($ao in $candidates) {
        if ((Test-Path -LiteralPath $ao) -and (-not (Test-WmWritable $ao))) { [void]$bad.Add($ao) }
    }
    return $bad.ToArray()
}

function Write-WmSetupFailedHelp {
    param([string[]]$SetupArgs)
    WmSay ''
    WmSay 'Setup did not finish. Read the lines above for the reason, then:'
    $wowDir = $null
    for ($i = 0; $i -lt $SetupArgs.Count - 1; $i++) {
        if ($SetupArgs[$i] -eq '--wow-dir') { $wowDir = $SetupArgs[$i + 1] }
    }
    $bad = @(Get-WmReadOnlyAddOnDirs -Extra @($wowDir))
    if ($bad.Count -gt 0) {
        $me = $env:USERDOMAIN + '\' + $env:USERNAME
        WmSay ''
        WmSay 'Windows will not let your account change this folder (it is probably under C:\Program Files):'
        foreach ($b in $bad) { WmSay ('  ' + $b) }
        WmSay 'Pick ONE of these, then paste the install line again:'
        WmSay '  1. Easiest: in the Battle.net app, move or reinstall WoW into a folder you own, for example'
        WmSay '     C:\Games\World of Warcraft. Then run the installer with -WowDir pointing at its _forever_ folder.'
        WmSay '  2. Or allow your account to write there, once. Open PowerShell with "Run as administrator" and run:'
        WmSay ('     icacls "' + (Split-Path -Parent (Split-Path -Parent $bad[0])) + '" /grant "' + $me + ':(OI)(CI)M"')
        WmSay '     Then close that admin window and paste the install line again in a normal one.'
        WmSay 'WoW Mentor never needs admin rights to run, only (maybe) this one-time permission.'
    } else {
        WmSay 'Run `wowmentor doctor` for what to fix, or see docs/TROUBLESHOOTING.md. If WoW is in an unusual place:'
        WmSay '  wowmentor setup --wow-dir "D:\Games\World of Warcraft\_forever_"'
    }
}

# ---------------------------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------------------------

function Install-WoWMentor {
    param([object[]]$RawArgs = @())
    # Set here, not at the top of the file: with iex a top-level assignment would stay in the user's session.
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    try {
        # Windows PowerShell 5.1 may still offer only old TLS versions, and GitHub needs 1.2.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        $null = $_
    }

    $repo = $env:WOWMENTOR_REPO
    if (-not $repo) { $repo = 'PhilipAD/wow-mentor' }
    $ref = $env:WOWMENTOR_REF
    if (-not $ref) { $ref = 'main' }
    $localApp = $env:LOCALAPPDATA
    if (-not $localApp) { $localApp = Join-Path $HOME 'AppData\Local' }
    $dest = $env:WOWMENTOR_DIR
    if (-not $dest) { $dest = Join-Path $localApp 'WoWMentor\app' }
    $binDir = Join-Path $localApp 'WoWMentor\bin'

    $setupArgs = @(ConvertTo-WmSetupArgs -Raw $RawArgs) + @(ConvertTo-WmSetupArgs -Raw (Split-WmArgString $env:WOWMENTOR_SETUP_ARGS))

    if (-not [Environment]::OSVersion.Platform.ToString().StartsWith('Win')) {
        WmFail 'This installer is for Windows. On a Mac use install.sh (see the README).'
    }
    if (Test-WmUnsafeDest $dest) {
        WmFail ("Refusing to install into '" + $dest + "'. Unset WOWMENTOR_DIR or point it at an empty folder.")
    }

    $py = Get-WmPythonOrInstall
    WmSay ('Using Python at ' + $py)

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('wowmentor-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        $stage = Join-Path $tmp 'stage'
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        if ($env:WOWMENTOR_SRC) {
            if (-not (Test-Path -LiteralPath (Join-Path $env:WOWMENTOR_SRC 'wowmentor.sh'))) {
                WmFail ('WOWMENTOR_SRC (' + $env:WOWMENTOR_SRC + ') has no wowmentor.sh.')
            }
            WmSay ('Copying WoW Mentor from ' + $env:WOWMENTOR_SRC + ' ...')
            Copy-WmSource -Src $env:WOWMENTOR_SRC -Target (Join-Path $stage 'wow-mentor')
        } else {
            $url = $env:WOWMENTOR_TARBALL_URL
            if (-not $url) { $url = 'https://github.com/' + $repo + '/archive/' + $ref + '.zip' }
            WmSay ('Downloading WoW Mentor (' + $repo + '@' + $ref + ')...')
            $name = 'download.zip'
            if ($url -match '\.(tar\.gz|tgz)(\?.*)?$') { $name = 'download.tar.gz' }
            $file = Join-Path $tmp $name
            try {
                Get-WmArchive -Url $url -OutFile $file
            } catch {
                WmFail ('The download failed. Check your internet connection and paste the same command again.' + "`n" + 'If it keeps failing, see docs/TROUBLESHOOTING.md or open an issue.')
            }
            try {
                Expand-WmArchive -File $file -Dest $stage
            } catch {
                WmFail 'The download is not a valid archive. Please try again.'
            }
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        }
        $src = Get-ChildItem -LiteralPath $stage -Directory | Select-Object -First 1
        if ((-not $src) -or (-not (Test-Path -LiteralPath (Join-Path $src.FullName 'wowmentor.sh')))) {
            WmFail 'Download looks wrong (no wowmentor.sh). Please report this.'
        }

        # An older copy may be running from DEST (its files would be locked), so stop it first. Best effort.
        $oldEntry = Join-Path $dest 'wowmentor.sh'
        if (Test-Path -LiteralPath $oldEntry) {
            $prev = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try { & $py -X utf8 $oldEntry stop 2>$null | Out-Null } catch { $null = $_ }
            $ErrorActionPreference = $prev
        }
        Remove-WmFolder -Path $dest
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Move-Item -LiteralPath $src.FullName -Destination $dest
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # The short commands: wowmentor and wowui. Absolute interpreter path, so they work in any window.
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    Set-Content -LiteralPath (Join-Path $binDir 'wowmentor.cmd') -Value (Get-WmShimText -PythonExe $py -Script (Join-Path $dest 'wowmentor.sh')) -Encoding Oem -NoNewline
    Set-Content -LiteralPath (Join-Path $binDir 'wowui.cmd') -Value (Get-WmShimText -PythonExe $py -Script (Join-Path $dest 'wowui')) -Encoding Oem -NoNewline

    WmSay ''
    WmSay 'Running setup...'
    WmSay ''
    $code = 0
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $dest
    try {
        & $py -X utf8 -m wowmentor setup @setupArgs | Out-Host
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        Write-WmSetupFailedHelp -SetupArgs $setupArgs
        WmFail ('Setup stopped (exit code ' + $code + ').')
    }

    if ($env:WOWMENTOR_NO_PATH) { return }
    $added = $false
    try {
        $added = Add-WmUserPath -Dir $binDir
    } catch {
        WmSay ''
        WmSay ('Could not add ' + $binDir + ' to your PATH automatically (' + $_.Exception.Message + ').')
        WmSay ('You can still run:  & "' + (Join-Path $binDir 'wowmentor.cmd') + '" doctor')
    }
    if ($added) {
        Send-WmEnvironmentChange
        WmSay ''
        WmSay 'Added the wowmentor and wowui commands to your PATH.'
        WmSay 'Open a NEW PowerShell window to use them. (Still "not recognized"? Sign out of Windows and back in once.)'
    }
    WmSay ''
    WmSay 'Windows reminder: set WoW to Windowed or Windowed (Fullscreen), and keep its window uncovered. There is no permission prompt.'
}

function Invoke-WmInstall {
    param([object[]]$RawArgs = @())
    try {
        Install-WoWMentor -RawArgs $RawArgs | Out-Null
        return 0
    } catch {
        WmSay ''
        WmSay $_.Exception.Message
        return 1
    }
}

if (-not $env:WOWMENTOR_PS_NOMAIN) {
    # A global on purpose: iex cannot return an exit code, and the .cmd wrapper and the CI test read this.
    $global:WoWMentorExitCode = Invoke-WmInstall -RawArgs $args
    if ($PSCommandPath) { exit $global:WoWMentorExitCode }
}
