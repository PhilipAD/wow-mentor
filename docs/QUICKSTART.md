# Quickstart: install WoW Mentor step by step

**What this is for:** the slow, hand-held install. Every step says what to do, what you should see, and what to do if you do not see it. **Who it is for:** anyone who has never used Terminal or PowerShell. If you already know your way around, the short version is in the [README](../README.md).

You will need about 10 minutes, a Mac or a Windows PC (Windows 10 or Windows 11), the WoW Forever client already installed (start it once so its folders exist), and an AI tool.

## Before you start

1. Make sure World of Warcraft Forever is installed and you have started it at least once.
2. Know which AI tool you will use. Claude Code and Codex run on your computer (Mac or Windows) and are the easiest. A website chat assistant needs one extra step (see [AGENT.md](AGENT.md)).
3. You do not need an account anywhere to install WoW Mentor. It downloads from GitHub.

There are two ways to install. Pick one. Every step below says **Mac** or **Windows** where they differ. Windows-only details (where files live, uninstall, limits) are in [WINDOWS.md](WINDOWS.md).

## Way A: paste one line (recommended)

### A1. Open Terminal (Mac) or PowerShell (Windows)

**Mac:** press Command and Space, type `Terminal`, press Return. A window with a blinking cursor opens. That is normal.

**Windows:** press the Windows key, type `PowerShell`, press Enter. Use the normal one (**Windows PowerShell** or **PowerShell 7**). Do not choose "Run as administrator": WoW Mentor never needs it.

### A2. Paste the install line

**Mac:**

```bash
curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.sh | bash
```

**Windows:**

```powershell
irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 | iex
```

Press Return (Enter). On the Mac you should see, in order (Windows prints the same numbered lines, with Windows paths, and ends step 5 with `No extra permission is needed on Windows.`):

```text
Downloading WoW Mentor (PhilipAD/wow-mentor@main)...

Running setup...

WoW Mentor 1.0.0 setup

1/5  Found WoW at /Applications/World of Warcraft/_forever_
2/5  Installed the addon (1001 overlay slots created)
3/5  Built the helpers: launcher ok, screen overlay ok
4/5  Started the server and the bridge (running)
5/5  One permission is needed: opening System Settings > Privacy & Security > Screen & System Audio Recording.
```

The path on the first line and the word `created` or `already there` on the second may differ. It is safe to run the line again later: it updates WoW Mentor and keeps your settings and links.

**If Terminal says "Python 3 is not ready yet":** macOS pops up a window offering Apple's Command Line Tools. Click Install, wait for it to finish (a few minutes), then paste the install line again.

**If it says "The download failed":** check your internet connection and paste the line again.

**If it says "needs a Mac" (on Linux):** WoW Mentor runs on macOS and Windows only.

**Windows: if it says "Python 3.9 or newer was not found":** the installer cannot use the "python" that opens the Microsoft Store (that is only a shortcut). It shows the command `winget install --id Python.Python.3.12 -e --scope user` and asks if it may run it. Say yes (it installs for your user only, no admin), or install Python from [python.org](https://www.python.org/downloads/windows/) yourself. Then paste the install line again. It never installs anything without asking.

**Windows: if PowerShell says the line is blocked or "running scripts is disabled":** that only affects script files, and this line does not use one. If your company blocks it anyway, use the careful way in [WINDOWS.md](WINDOWS.md#install-it-the-careful-way).

**Windows: if it says it cannot write to the WoW folder:** WoW is under `C:\Program Files`, which Windows protects. The installer prints the exact fix (move WoW to a folder you own, or allow your account once). See [WINDOWS.md](WINDOWS.md#wow-is-in-program-files).

**If it says "I could not find World of Warcraft":** start the game once, or tell the installer where it is. Run this, with your own path:

```bash
~/.wowmentor/app/wowmentor.sh setup --wow-dir "/Applications/World of Warcraft/_forever_"
```

(The folder you want is the one that contains `Interface/AddOns`. In Finder you can drag it onto the Terminal window instead of typing its path.)

On Windows, open a new PowerShell window and run this, with your own path (in Battle.net, the cogwheel next to Play, then **Show in Explorer**, opens the right place):

```powershell
wowmentor setup --wow-dir "D:\Games\World of Warcraft\_forever_"
```

**Mac: if step 3 says `launcher FAILED`:** the Command Line Tools are missing or incomplete. Run `xcode-select --install`, wait, then run the install line again.

### A3. Skip to "Give it permission to read the screen" below (on Windows there is nothing to allow, but read that section).

## Way B: double-click (no pasting)

1. On the project's GitHub page, click the green **Code** button, then **Download ZIP**.
2. Open your Downloads folder and double-click the ZIP to unzip it.
3. Open the unzipped folder and double-click **Install WoW Mentor.command** (Mac) or **Install-WoWMentor.cmd** (Windows).
4. **Windows:** a blue "Windows protected your PC" box may appear because the file is new to Microsoft. Click **More info**, then **Run anyway**. A window shows the same numbered lines as Way A and ends with "Press any key to continue". It installs a copy under `%LOCALAPPDATA%\WoWMentor`, so you may delete the unzipped folder afterwards. Skip the rest of this list.
   **Mac:** macOS may say it cannot be opened because it is from an unidentified developer. Right-click the file, choose **Open**, then click **Open** again. You only do this once.
5. A Terminal window opens and shows the numbered lines `1/5` to `5/5` from A2 (but not the "Downloading" lines, because you already downloaded it). When it finishes it says `Press Return to close this window.`

Note for Way B on the Mac: the program stays in the folder you unzipped. Do not delete or move that folder afterwards, or WoW Mentor stops working. (Way A keeps its own copy in `~/.wowmentor/app`.) Way B creates the same short `wowmentor` and `wowui` commands as Way A, pointing into that folder. If you ever see `command not found`, open Terminal, type `cd ` (with a space), drag the unzipped folder onto the Terminal window, press Return, and then type `./wowmentor.sh` and the command word instead of `wowmentor`.

To install a specific release instead of the newest code, use Way A with a tag: `curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/v1.0.0/install.sh | WOWMENTOR_REF=v1.0.0 bash`. To pass options to setup, add them after `bash -s --`, for example `... | bash -s -- --wow-dir "/path/to/_forever_"`.

## Give it permission to read the screen

WoW Mentor needs to read the game window to see your data strip.

**Windows:** there is no permission prompt and nothing to switch on. What matters is how WoW is shown. In WoW, open **Options > Graphics** and set the display mode to **Windowed** or **Windowed (Fullscreen)** (also called borderless). Keep the WoW window uncovered while you play (another window on top of the top edge hides the strip from the reader), and not minimized. Real exclusive fullscreen cannot be read. Then run `wowmentor restart` and go on to "Restart World of Warcraft once".

**Mac:** macOS makes you allow it.

1. System Settings should have opened by itself at **Privacy & Security, Screen & System Audio Recording**. If not, open System Settings yourself and go there.
2. Click the **+** button under the list. You may be asked for your password or fingerprint.
3. In the file window, open the **Applications** folder inside your home folder (the one with your name, not the main Applications folder). Pick **WoW Mentor Bridge** and click Open.
4. Make sure the switch next to it is **on**. macOS may offer to quit and reopen it. Say yes or later; both are fine.
5. Back in Terminal, restart the helper so it picks up the permission:

```bash
wowmentor restart
```

You should see `stopped (they start again at your next login, or run `wowmentor start`)` and then `started`.

**Mac: if `wowmentor` says `command not found`:** your Terminal does not know the shortcut yet. Use the long form instead, anywhere you see `wowmentor`:

```bash
~/.wowmentor/app/wowmentor.sh restart
```

(To fix it permanently, the installer printed a tip: add `export PATH="$HOME/.local/bin:$PATH"` to a file called `.zshrc` in your home folder and open a new Terminal window.)

**Windows: if `wowmentor` says it is not recognized:** the installer added the short commands to your PATH, but a PowerShell window that was already open does not know yet. Close it and open a new PowerShell window.

## Restart World of Warcraft once

1. If WoW is open, quit it completely: Command and Q on the Mac (closing the window is not enough). On Windows, close the game and check that no "World of Warcraft" is left in Task Manager.
2. Start WoW again and log in to a character.
3. In the chat window you should see a green line: **WoWFeed active (hairline strip, top edge)**.

Why a restart: the game only notices new addon folders when it starts. The installer added many small empty folders (the "slots") that the overlay uses to get updates while you play. You only need this restart once. If you want to know why, see [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

## Check that it all works

```bash
wowmentor doctor
```

When everything is good it looks like this (this is the Mac version; on Windows the paths look like `C:\Users\you\AppData\Roaming\WoWMentor` and `C:\Program Files (x86)\World of Warcraft\_forever_`, and everything else is the same. Yours will differ a little; the "Overlay" lines appear only once the game is running with your character):

```text
WoW Mentor 1.0.0

✅ Python 3.9
✅ Config found (/Users/you/.config/wowmentor)
✅ WoW folder: /Applications/World of Warcraft/_forever_
✅ Addon installed (version 1.0.0)
✅ Overlay slots: 1001 of 1001
✅ Server is running
✅ Bridge is running
✅ WoW is running
✅ Screen capture works
✅ Live feed is flowing
✅ Overlay is connected (slot 1 of 1000 used this session)
✅ Overlay is empty (your agent has not drawn anything yet)
✅ Game session: in_world (WoW is in front)
⚠️  Guidelime guide packs: none found (guide editing will be unavailable)
     -> Optional: install Guidelime and a guide pack if you want the mentor to edit your leveling guide

Mostly fine; see the ⚠️ notes.
```

Before you have restarted WoW, expect warnings or crosses for the game itself ("WoW is not running", "Feed is stale"). That is normal; fix them by doing the steps above.

A warning about Guidelime is fine if you do not use it. If you see a red cross, read the line under it. Every message and its fix is listed in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). One common one right after login: `Feed is stale`. Wait 10 seconds with your character standing in the world and run `wowmentor doctor` again.

## Tell it about yourself (optional but worth it)

```bash
wowmentor profile
```

A small text file opens in your text editor (TextEdit on the Mac, Notepad on Windows). Replace the `<angle bracket>` examples with your character, goals, playstyle, professions and anything the mentor must never say. Save. Your AI reads this file the next time it fetches its instructions. You can change it any time.

## Connect your AI

```bash
wowmentor links
```

You will see three links and a block of text between two dashed lines:

```text
Your private links (anyone with a link can read your game feed or control your overlay: do not post them).

Game feed (read only):   http://127.0.0.1:8095/<long random text>?t=1
Overlay and guide API:   http://127.0.0.1:8095/ui/<long random text>
Agent briefing page:     http://127.0.0.1:8095/ui/<long random text>/skill

These work on this computer only. Using an AI that runs somewhere else (a website or another computer)? Run:  wowmentor share on

Paste this to your AI agent:
----------------------------------------------------------------
You are my live World of Warcraft mentor. Read your operating skill first, then follow it: ...
----------------------------------------------------------------
```

Copy the block between the dashed lines (or run `wowmentor prompt` to put it on your clipboard) and paste it to your AI.

- **AI runs on this computer (Claude Code, Codex and similar):** just paste it. It will fetch its instructions, install the small `wowui` command and start.
- **AI runs on a website or another computer:** first run `wowmentor share on`, then run `wowmentor links` again and paste the new text. Read [PRIVACY.md](PRIVACY.md) first so you know what that does.

Treat the links like passwords. Do not post them in public chats or screenshots.

## What happens next

Within a minute or so your AI should read your game and put a short checklist panel on your screen. You can:

- Click a checklist item to tick it. The AI sees that.
- Type `/wfui unlock`, then drag a panel by its title to move it. `/wfui lock` locks it again.
- Type `/wfui` to see all commands (hide, show, alpha, scale, reset).
- Type `/wfui demo` to see a sample panel without any AI.

## Updating and removing

Update (keeps your settings and links): run the same install line from step A2 again. It downloads the newest version and runs setup again. (`wowmentor update` does the same only when the program folder is a git clone; otherwise it just reminds you to run the install line.) After an update, type `/reload` in the game, and fully restart WoW if the doctor says the overlay slots are missing.

Remove everything (services, helper app, addon folders). Your config and saved data stay unless you add `--purge`:

```bash
wowmentor uninstall
```

`wowmentor uninstall` never deletes your guide files or other addons. It does not delete the downloaded program itself. To remove that too on the Mac, run `rm -rf ~/.wowmentor` and `rm -f ~/.local/bin/wowmentor ~/.local/bin/wowui` (Way A), or delete the unzipped folder (Way B). On Windows delete the folder `%LOCALAPPDATA%\WoWMentor` (and take its `bin` folder out of your PATH; the exact steps are in [WINDOWS.md](WINDOWS.md#uninstall)). Restart WoW afterwards so it forgets the removed addon folders.

## If something goes wrong

- Run `wowmentor doctor` and follow the line under any warning or cross.
- Read [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- Run `wowmentor logs` and include its output (after removing anything you do not want to share) if you open an issue.
