# WoW Mentor on Windows

**What this is for:** everything that is different on Windows: what you need, how the game must be shown, where files live, how to remove it, and what does not work yet. **Who it is for:** Windows players. The step-by-step install is in [QUICKSTART](QUICKSTART.md); the short version is in the [README](../README.md).

## What you need

- Windows 10 version 2004 (May 2020) or newer, or Windows 11.
- The **WoW Forever** client, started at least once so its folders exist.
- Python 3.9 or newer. The installer looks for it, and if it is missing it offers to install it for you (see below). It never installs anything without asking.
- No admin rights, no account, no extra downloads. Everything stays in your own user folders.

## Install

Open PowerShell (a normal window, not "as administrator") and paste:

```powershell
irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 | iex
```

It downloads the program, finds Python, runs `wowmentor setup`, and adds the short commands `wowmentor` and `wowui` to your PATH. Open a **new** PowerShell window afterwards so it finds them. Run the same line again any time to update; your settings and links are kept.

**No Python yet?** The installer says so plainly. The "python" that opens the Microsoft Store does not count, it is only a shortcut. If `winget` exists, the installer shows this command and asks before running it:

```powershell
winget install --id Python.Python.3.12 -e --scope user
```

Answer `n` and it stops; install Python from [python.org](https://www.python.org/downloads/windows/) yourself and paste the line again.

**Pin a version or give setup options.** Set variables first, then paste the line:

```powershell
$env:WOWMENTOR_REF = 'v1.0.0'
$env:WOWMENTOR_SETUP_ARGS = '--wow-dir "D:\Games\World of Warcraft\_forever_"'
irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 | iex
```

## Install it the careful way

Why the one-liner is `irm | iex`: the text goes straight into PowerShell, so there is no script file for the execution policy to block and nothing is saved with an "from the internet" mark. It is also short enough to read. If you would rather read the whole installer first (it is plain ASCII, a few hundred lines), download, read, then run:

```powershell
irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 -OutFile install.ps1
notepad install.ps1
Get-Content -Raw .\install.ps1 | iex
```

The last line reads the file and runs it as text, which is why you do not have to change any PowerShell setting. Please do not "fix" a blocked script by turning off the execution policy for your whole PC.

Prefer not to use PowerShell at all? Download the ZIP from GitHub, unzip it and double-click **Install-WoWMentor.cmd**. It runs the same installer from that folder and installs a copy, so you can delete the unzipped folder afterwards. Windows may show a blue "Windows protected your PC" box for a file it has not seen before: click **More info**, then **Run anyway**.

## Windowed mode (the one thing you must do)

There is no screen-capture permission on Windows, so there is no prompt and no switch to turn on. What matters is how WoW is shown.

WoW Mentor reads a thin strip of coloured squares at the **top edge of the game window** from what Windows puts on your screen, ten times a second. That means:

- Set WoW to **Windowed** or **Windowed (Fullscreen)** (also called borderless), in **Options > Graphics**. Windowed (Fullscreen) looks the same as fullscreen.
- Do not use real exclusive fullscreen. In that mode the game draws straight to the monitor and there is nothing on the desktop for WoW Mentor to read.
- Keep the top edge of the WoW window **uncovered**. Another window on top of it (a browser, Discord, a second monitor's window dragged across) hides the strip, and the doctor will say the feed is stale. A minimized window cannot be read.
- Display scaling (125%, 150% and so on) is fine. The reader works in real pixels.

The overlay that hides the strip from your own eyes is a small click-through window. It ignores your mouse and keyboard and is left out of what the reader sees. It needs Windows 10 2004 or newer. On older builds the reading still works but the strip stays visible.

## Antivirus and SmartScreen

- WoW Mentor is not code-signed, so SmartScreen can say "Windows protected your PC" for the double-click installer. **More info**, then **Run anyway**. The one-line install does not trigger it.
- Some antivirus tools dislike any installer that is piped into PowerShell, because that is also how malware arrives. This one uses no hidden windows, no encoded commands and no admin rights, and you can read all of it first (see above). If your tool blocks it, use the careful way, or allow the folder `%LOCALAPPDATA%\WoWMentor`.
- Reading the screen is done with ordinary Windows calls (the same ones screenshot tools use). It never sends a key or a click to the game.

## Where things live

| What | Where |
| --- | --- |
| The program | `%LOCALAPPDATA%\WoWMentor\app` |
| The `wowmentor` and `wowui` commands | `%LOCALAPPDATA%\WoWMentor\bin` (added to your user PATH) |
| Settings, your private links and `profile.md` | `%APPDATA%\WoWMentor` (readable by your Windows account only) |
| Logs (`server.log`, `bridge.log`, `tunnel.log`) | `%LOCALAPPDATA%\WoWMentor\logs`; `wowmentor logs` prints the last lines |
| Start at sign-in | one per-user entry named `WoWMentor` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; it starts a small supervisor that restarts the server and the bridge if they stop |
| The addon | `<your WoW folder>\_forever_\Interface\AddOns\WoWFeed`, plus `WFSlot...` folders next to it |

Paste `%LOCALAPPDATA%` into the File Explorer address bar to open the first one.

## WoW is in Program Files

Battle.net usually installs WoW into `C:\Program Files (x86)\World of Warcraft`. Windows protects that folder, and on some PCs your normal account may not be allowed to add the addon there. Then `wowmentor setup` stops and the installer prints what to do. Pick one:

1. **Easiest:** in the Battle.net app, move or reinstall WoW into a folder you own, for example `C:\Games\World of Warcraft`. Then run `wowmentor setup --wow-dir "C:\Games\World of Warcraft\_forever_"`.
2. **Or allow your account once.** Open PowerShell with **Run as administrator** and run the `icacls` line the installer printed (it grants your account Modify rights on the WoW folder). Close that admin window and run the installer again in a normal one.

WoW Mentor itself never needs admin rights to run.

## Updating

Paste the install line again. If the doctor says the overlay slots are missing afterwards, restart WoW fully.

## Uninstall

In a PowerShell window, first let WoW Mentor clean up after itself (this stops its background programs, removes the start-at-sign-in entry and the addon folders; guide files and other addons are never touched):

```powershell
wowmentor uninstall
```

Add `--purge` to also delete your settings, links and saved data in `%APPDATA%\WoWMentor`. Then delete the program and the commands:

```powershell
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\WoWMentor"
```

Finally take the `bin` folder back out of your user PATH (optional, it is harmless to leave):

```powershell
$bin = "$env:LOCALAPPDATA\WoWMentor\bin"
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$old = $key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
$new = ($old -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ine $bin) }) -join ';'
$key.SetValue('Path', $new, 'ExpandString')
$key.Close()
```

Restart WoW afterwards so it forgets the removed addon folders.

## Known limits

- Windowed or Windowed (Fullscreen) only, and the top edge of the game window must be visible. See above.
- It runs while you are signed in. Signing out or restarting stops it; it starts again at your next sign-in.
- `wowmentor share on` needs `cloudflared`. If it is missing, the command tells you to run `winget install --id Cloudflare.cloudflared` and open a new PowerShell window. Read [PRIVACY](PRIVACY.md) before using it.
- Only one Windows account at a time is supported, and Windows on ARM and very old Windows 10 builds are not tested.
- This is an early Windows release. If something is off, run `wowmentor doctor`, read [TROUBLESHOOTING](TROUBLESHOOTING.md), and please open an issue with the doctor output (remove any links first).
