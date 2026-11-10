# Troubleshooting

**What this is for:** fixing problems on Mac and Windows, and explaining every message that `wowmentor doctor` can print. **Who it is for:** players. No programming needed. Copy and paste the commands.

Start here, always:

```bash
wowmentor doctor
```

Fix the red crosses first, from the top. A fix for one often clears the ones below it. Under each warning or cross, doctor prints a line starting with `->` that says what to do. If `wowmentor` says `command not found`, use `~/.wowmentor/app/wowmentor.sh` in its place (or `./wowmentor.sh` from your unzipped folder). On Windows, open a **new** PowerShell window (the installer only changes the PATH for windows opened after it), or run `& "$env:LOCALAPPDATA\WoWMentor\bin\wowmentor.cmd" doctor`; from an unzipped folder use `.\wowmentor.cmd`.

Log files live in `~/Library/Logs/WoWMentor/` on a Mac and in `%LOCALAPPDATA%\WoWMentor\logs` on Windows (`server.log`, `bridge.log`, and `tunnel.log` if you use share). `wowmentor logs` prints the last lines of each. Add `-n 100` for more.

## Every doctor message

Status icons: a green tick means fine, a warning sign means "works but look at this", a red cross means "broken".

| Message | What it means | What to do |
| --- | --- | --- |
| This is not a Mac / This is not Windows | You ran it on an unsupported system (Linux, or a Mac installer on Windows or the other way round). | Mac and Windows are supported. See [FAQ](FAQ.md); on Windows use the PowerShell line from the [README](../README.md). |
| Python X.Y (red) | Python is older than 3.9. | Mac: run `xcode-select --install`, wait for it to finish, run the installer again. Windows: install Python 3.9 or newer (`winget install --id Python.Python.3.12 -e --scope user`, or from python.org), open a new PowerShell window, run the installer again. |
| Not set up yet | No config file exists. | Run the install line from the [README](../README.md), or `wowmentor setup`. |
| WoW folder not found (path) | The saved WoW location is missing or has no `Interface/AddOns` folder. | Run `wowmentor setup --wow-dir "/Applications/World of Warcraft/_forever_"` with your own path (Windows: `--wow-dir "D:\Games\World of Warcraft\_forever_"`). Start the game once so the folder exists. |
| Addon is not installed | The `WoWFeed` folder is not in your AddOns folder. | Run `wowmentor setup`, then restart WoW. |
| Addon is out of date (a, newest b) | You updated WoW Mentor but the game still has the old addon files. | Run `wowmentor setup`, then type `/reload` in the game. |
| Overlay slots: N of 1001 | The empty "slot" folders are missing. Fewer than 1001 means setup did not finish or something deleted them. | Run `wowmentor setup`, then fully quit and restart WoW. See [Slots](#slots-and-the-restart). |
| Server is not running | The local server is down. | Run `wowmentor start`. If it fails again run `wowmentor logs`. |
| Bridge is not running | The screen reader is down. | Run `wowmentor start`. See also [Permission](#screen-recording-permission). |
| WoW is not running | No process named "World of Warcraft" was found. | Start the game and log in to a character. |
| Screen capture works, but the data strip is not visible | The bridge can see the game window but not the strip. | Log in to a character (the login screen has no strip), wait 10 seconds, type `/reload`. If it stays, see [Colored dots](#i-see-colored-dots) and `wowmentor logs`. |
| Cannot capture the game window | Mac: macOS is blocking screen capture. Windows: WoW is not visible as a normal window. | Mac: see [Permission](#screen-recording-permission). Windows: see [Windows capture](#windows-the-feed-is-stale-or-capture-fails). |
| Could not test screen capture (reason) | The doctor itself failed to try. | Run `wowmentor logs`. Usually the permission again. |
| Feed not readable | The server did not answer the feed request. | Run `wowmentor restart`. |
| Feed is stale (the game is closed, minimised or on a login screen) | The newest data is older than 15 seconds. | Log in to a character and keep the game window visible for a few seconds. A minimised window is not captured reliably. |
| Live feed is flowing | Good. | Nothing. |
| Overlay slots not detected in the game | The feed is live but the addon does not report using any slot. | Fully quit and restart WoW once, then `/reload`. |
| Overlay is connected (slot X of 1000 used this session) | Good. Each overlay update uses up one slot until the next `/reload`. | Nothing. |
| Overlay is empty (your agent has not drawn anything yet) | Good, just nothing to show yet. | Connect your AI. |
| Overlay is in sync (revision N) | Good. | Nothing. |
| Overlay update is on its way (game shows X, newest Y) | The AI sent a change that the game has not received yet. | Wait up to 30 seconds. If it stays stuck, see [Overlay not updating](#the-overlay-is-not-updating). |
| Guidelime guide packs: none found (guide editing will be unavailable) | No guide pack in AddOns. | Only matters if you want the AI to edit your leveling guide. See [Guidelime not found](#guidelime-not-found). |
| Public link reachable / not reachable | Only shown when `share` is on. | If not reachable run `wowmentor share on` again. The free address changes whenever the tunnel restarts. |

At the end doctor prints one of three lines: "Everything needed is working.", "Mostly fine; see the warning notes.", or "Fix the red items first." It exits with code 1 if anything is red.

### Session and webhook messages (new in 1.0.0)

These appear after the server check, when the server is running.

| Message | What it means | What to do |
| --- | --- | --- |
| Could not read the session state from the server | The server answered the health check but not the session request. | Run `wowmentor restart`. |
| The bridge has not reported the game session yet | The server is up but the bridge has not checked in since it started. | Run `wowmentor start`, wait a few seconds, run the doctor again. |
| The bridge stopped reporting (N s ago) | The helper has not checked in for over 30 seconds. | Run `wowmentor start`; if it keeps happening, `wowmentor logs`. |
| Game session: STATE (WoW is in front / another app is in front / front window unknown) | Green when you are in the world, loading or AFK. `STATE` is `in_world`, `loading`, `afk`, `logged_out` or `game_closed`. | Warning for `logged_out` ("Log in to a character") and `game_closed` ("Start WoW and log in to a character"). "Front window unknown" means the system gave no answer; it is harmless. |
| Webhook URL in config.json is not a valid http(s) URL | The saved address is broken. | `wowmentor webhook set <url>`. |
| Webhook (host) is failing (reason), N event(s) queued | Your receiver did not answer 2xx, or could not be reached. The bridge keeps retrying. | `wowmentor webhook test` shows the answer. Check your receiver. |
| Webhook (host) delivers (last success N s ago) | Good. | Nothing. |
| Webhook (host) is not active in the running bridge yet | You just set it; the bridge picks it up within a few seconds. | Wait, or `wowmentor restart`. |
| Webhook (host) configured, nothing delivered yet | Good; nothing has happened in the game yet. | Nothing. |
| Remote server (host) is reachable / not reachable | Only when you use `wowmentor server-url`. | Check that the other server runs and that the address is right: `wowmentor server-url status`. |

If a message is not in these tables, run `wowmentor logs` and open an issue with the exact text.

## Screen Recording permission

(Mac only. Windows has no such permission: see the next section.)

Symptoms: "Cannot capture the game window", "could not capture the game window (Screen Recording permission?)" in the bridge log, or the doctor says the feed is stale although the game is running.

1. Open System Settings, then Privacy & Security, then **Screen & System Audio Recording**.
2. Look for **WoW Mentor Bridge** in the list. If it is missing, click **+** and add it from the `Applications` folder inside your home folder (not the main Applications folder).
3. Make sure its switch is on. If it was already on and still fails, turn it off and on again, or remove it with the minus button and add it again. This happens after some macOS updates and after a re-install, because macOS ties the permission to the exact file.
4. Run `wowmentor restart`.

If there is no "WoW Mentor Bridge" in `~/Applications`, the helper could not be built because Apple's Command Line Tools are missing. Run `xcode-select --install`, wait, and run the installer again. Without the launcher the services run under plain Python, and macOS then has nothing sensible to list under Screen Recording, so install the tools and run the installer again rather than trying to work around it.

## Windows: the feed is stale or capture fails

Symptoms on Windows: "Cannot capture the game window", "Feed is stale" while the game is running, or a capture that works but never sees the strip. There is nothing to allow in Windows settings. Check these, in order:

1. In WoW, **Options > Graphics**, set the display mode to **Windowed** or **Windowed (Fullscreen)**. Exclusive fullscreen cannot be read. Then run `wowmentor restart`.
2. Make sure the top edge of the WoW window is on screen and **not covered** by another window (a browser, Discord, a window on your second monitor dragged across). The strip is read from the screen, so whatever is on top is what gets read.
3. Do not minimize WoW. A minimized window has no picture.
4. Log in to a character and wait about 10 seconds. The login screen has no strip.
5. Remote desktop, a locked screen, or a monitor that is switched off give Windows no real picture to read. Use the PC at its own screen, or keep the session unlocked.
6. Still stuck? Run `wowmentor logs` and `wowmentor doctor -v`, then open an issue.

More about Windows: [WINDOWS.md](WINDOWS.md).

## I see colored dots

You are seeing the data strip. It is a thin row of tiny coloured squares along the very top edge of the game window (each square is about 3 pixels wide, the whole strip is only a few pixels tall, and a white square comes first). It is harmless.

Why it can show: a tiny black bar normally covers it on your screen. That bar is built by the installer only if Apple's Command Line Tools (the Swift compiler) are installed, and it is started by the bridge when the bridge sees the strip.

Fixes, in order:

1. Run `xcode-select --install` if you never did, then run the installer again.
2. Check the bridge is running: `wowmentor doctor`.
3. Check the bar exists: `ls ~/.config/wowmentor/bin/wowmask`. If that file is missing the bar was not built (step 1).
4. Run `wowmentor restart`.
5. Hiding it does not change anything else: the bridge reads the game window's own picture, which still contains the strip even when something is laid over it on screen.

Still there? It does not hurt anything. You can also put a UI element over the top edge of the game.

## The overlay is not updating

Symptoms: you or your AI changed something and the game still shows the old panels.

1. `wowmentor doctor`. Fix any red items first.
2. Ask your AI to run `wowui state`, or run it yourself (`~/.wowmentor/app/wowui state`). Look at `in_sync`. If it is `true`, the game has the newest version. If `false`, the game has not yet received it.
3. The game must be running, with a character logged in and the window visible. The bridge delivers by writing files and the game picks them up by loading a "slot". This takes a few seconds, and up to about 30 seconds on the slowest path.
4. Type `/reload` in the game. That reloads the interface and the addon reads the newest version immediately.
5. If you see the toast "Overlay inbox is 85% used. Type /reload when convenient to refill it.", do that. See [Slots](#slots-and-the-restart).
6. `wowmentor logs`. A line like `overlay slot pool nearly used up (950/1000): /reload in game to refresh` means the same thing.
7. If your AI keeps rewriting the same panel every few seconds it will use up slots quickly. Tell it to update on events, not on a timer.

## Slots and the restart

The overlay is delivered through 1001 pre-made, empty "load on demand" addons named `WFSlot000` to `WFSlot1000` that setup created in your AddOns folder. The game only learns about addon folders when it starts, so it needed one restart after the installer created them. Each overlay update uses one slot. A `/reload` gives all of them back.

| Message you see | What it means | Fix |
| --- | --- | --- |
| Doctor: `Overlay slots: N of 1001` | Folders are missing on disk. | `wowmentor setup`, then fully quit and restart WoW. |
| Doctor: `Overlay slots not detected in the game` | The folders exist but the running game does not know them (you did not restart after setup). | Fully quit and restart WoW (Command and Q), not just `/reload`. |
| `/wfui dump` in the game lists an error mentioning "slot pool used up" | The game has used all slots it knows about. | `/reload`. If it keeps happening soon after a reload, restart WoW. |
| Toast "Overlay inbox is 85% used" | Slots are running low. | `/reload` when convenient (not in combat). |

## Guidelime not found

The doctor line "Guidelime guide packs: none found" means WoW Mentor did not find a **guide pack**: an addon folder whose name starts with `Guidelime_` (for example a pack named `Guidelime_SomethingForever`) that contains guide files. The main `Guidelime` addon folder alone does not count.

Fixes:

1. Install a guide pack that supports Forever, using your usual addon manager. WoW Mentor does not ship Guidelime or any pack (see [NOTICE](../NOTICE)).
2. Check the folder name: look in `/Applications/World of Warcraft/_forever_/Interface/AddOns/` (your path may differ) for a folder starting with `Guidelime_`. Case does not matter.
3. Check the pack really contains guides: the bridge looks for `.lua` files that call `registerGuide(` near the top.
4. Run `wowmentor restart` so the bridge rescans.

If you do not use Guidelime you can ignore this warning. Everything else works.

## Type /reload prompts

You may see a banner in the game: **Guide changed: type /reload**. It appears after your AI edited a Guidelime guide file. The game only reads guide files when the interface loads, so type `/reload` in the chat box when you are out of combat. The banner goes away on its own after the game reloads. Your guide progress is stored by step position, so ask your AI to prefer edits in place over inserting steps earlier than where you are.

If an edit went wrong, your AI can restore the original (`wowui tug restore <guide> orig`). Backups are in `~/.config/wowmentor/data/guide-backups`.

Other `/reload` prompts: after `wowmentor setup` updates the addon ("Addon is out of date"), and when the slot pool is nearly used up (see above). Do not reload in the middle of a fight.

## Other problems

**The addon message "WoWFeed: message too large" appears in chat.** One section of data did not fit in the strip at your screen resolution. Nothing breaks; that one section may be missing from your AI's snapshot. Please open an issue with your screen resolution.

**Nothing at all appears in the game after login.** There is no green "WoWFeed active" line. The addon is not loaded. On the character select screen click **AddOns** and make sure **WoW Feed** is ticked. Then restart the game. If you installed the addon into a different WoW version's folder by mistake, run `wowmentor setup --wow-dir` with the Forever folder.

**The AI says it cannot reach the links.** Links with `127.0.0.1` only work for programs on your computer. For a website or another computer run `wowmentor share on`, then `wowmentor links` again and paste the new text. If you ran `rotate-secrets`, old links stop working.

**`wowmentor share on` fails.** It needs Cloudflare's free `cloudflared` tool. Install Homebrew from https://brew.sh then `brew install cloudflared`, or let `wowmentor share on` offer to do it for you. Then check `wowmentor logs`.

**The public link stopped working.** The free tunnel address changes whenever the tunnel restarts (for example after a reboot). Run `wowmentor links` for the new one and give it to your AI again.

**You pasted your links somewhere public.** Run `wowmentor rotate-secrets`. Old links stop working at once and new ones are created. Then run `wowmentor links`.

**Things are slow or your fans spin up.** Screen capture runs several times a second. Close other heavy programs. Tell your AI not to rewrite panels more often than every 10 seconds.

**Start fresh without losing settings.** `wowmentor restart`. To also clear the overlay, ask your AI to run `wowui reset`.

**Remove everything.** See the end of [QUICKSTART.md](QUICKSTART.md).

## Still stuck

Open an issue using the bug report form. Include: your macOS or Windows version, the output of `wowmentor doctor`, and the last lines of `wowmentor logs` with any links or tokens removed (links contain a long random secret; never paste them). For a security problem do not open a public issue; read [SECURITY.md](../SECURITY.md).
