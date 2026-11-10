# Frequently asked questions

**What this is for:** quick answers about WoW Mentor on Mac and Windows. **Who it is for:** players. For problems, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## About safety and the rules

**Will I get banned?**
We cannot promise. WoW Mentor is an independent project and Blizzard has not reviewed or approved it, or any tool that reads the game window. It is designed to stay on the display-only side of the line: the addon only shows information; the helper only reads the picture of the game window and sends no keys, clicks or commands to the game; nothing plays for you. Rules can change, and you are responsible for following Blizzard's terms and addon policy. Use it at your own risk. This is also stated in the [README](../README.md#disclaimer).

**Does it play the game for me?**
No. It cannot cast, target, move, loot, trade or accept quests, and it does not try. The game does not let addons do those things. The AI advises and shows you things; you press the keys.

**Is my data sent to Blizzard or to you?**
No. There is no telemetry and no account. See [PRIVACY.md](PRIVACY.md) for exactly what leaves your computer and when (only if you share, use a webhook, or hand your links to an AI tool).

**Does the AI see my screen?**
No. It reads a text description of your game that the addon writes. The program reads only the top strip of the game window's own picture (the top 220 rows) to decode that text. No screenshot is stored or sent anywhere.

**Can other players read my game through this?**
Only if they get your links. Treat links like passwords, do not post them, and run `wowmentor rotate-secrets` if one leaks.

## What it works with

**Does it work on Windows or Linux?**
Windows 10 (version 2004 or newer) and 11: yes. Use the PowerShell line in the [README](../README.md) and read [WINDOWS.md](WINDOWS.md). WoW has to be in Windowed or Windowed (Fullscreen) mode, and there is no permission prompt to answer. Linux: no. The screen reader is a different program on each OS and a Linux one is not built. Contributions are welcome (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

**Which WoW version?**
The Forever client (addon interface 16001). Other versions are not tested and are not supported. Retail and classic-era clients have different rules; the installer warns if the folder name does not contain "forever".

**What if Blizzard changes the interface number?**
If the game says the addon is out of date after a patch, tick "Load out of date AddOns" on the AddOns screen and update WoW Mentor when a new version comes out (run the install line again). Please open an issue.

**Which Windows versions?**
Windows 10 version 2004 (May 2020) or newer, and Windows 11. The piece that hides the data strip from your own eyes needs 2004 or newer; the reading itself does not. Windows on ARM is not tested.

**Intel or Apple Silicon Mac?**
The code uses only Python's standard library and Apple's compilers, so both should work. It is not verified on Intel. (On Windows there is nothing to compile.)

**Which AI tools work?**
Any that can run commands (Claude Code, Codex CLI and similar) or fetch web addresses. See [AGENT.md](AGENT.md). We have not tested every product.

**Do I need Guidelime?**
No. It is optional. With a Guidelime guide pack installed, your AI can read and carefully edit your leveling guide, with a backup before every change. Without it, everything else works. WoW Mentor does not include or redistribute Guidelime or any guide pack.

**Do I need Python?**
Yes, version 3.9 or newer. If you do not have it, on a Mac the installer asks macOS to offer Apple's Command Line Tools, which include it. On Windows the installer explains what is missing and offers to run `winget install --id Python.Python.3.12 -e --scope user` (it asks first); the "python" that opens the Microsoft Store does not count. WoW Mentor has no other dependencies.

**Does it cost money?**
WoW Mentor is free and open source (MIT license). The free Cloudflare tunnel (only if you share) has no cost. Your AI tool may have its own price.

## Using it

**Why do I have to restart WoW once?**
The overlay is delivered through a pool of 1001 empty addon folders that the installer creates. The game only notices addon folders when it starts. After this one restart you do not need another. Details: [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

**What are "slots"?**
Those empty addon folders (`WFSlot000` to `WFSlot1000`). Every overlay change uses one until you `/reload`. If you see "Overlay inbox is 85% used", type `/reload` when you are out of combat.

**Can I use it without an AI?**
The overlay and rules are controlled by commands, so you can use them by hand: `/wfui demo` shows a sample panel, and `wowui` can draw panels and install rules from Terminal (PowerShell on Windows). But the point is the AI.

**Does it work while the game is in the background, or covered by other windows?**
On a **Mac**, capture reads the game window's own picture, so other windows on top of it do not matter, and it works on another Space. On **Windows** it reads the screen where the top edge of the game window is, so that edge must be visible: keep WoW in Windowed or Windowed (Fullscreen) mode and do not drag other windows over it. On both, a **minimised** window is not captured reliably (the doctor reports a stale feed), so keep it on screen. In the background the overlay can still be updated, but nothing is visible until you come back.

**Does it slow down my game?**
The addon builds a few text sections about once a second and draws a strip a few pixels tall. We have not measured the cost on different Macs and PCs, so we cannot give a number. If you notice stutter, run `wowmentor stop` and tell us. The screen reader runs several times a second as a separate process.

**Can I move the panels? Hide them? Make them smaller?**
Yes. In game: `/wfui unlock` then drag a panel by its title (or hold Shift and drag anywhere on it); `/wfui lock`; `/wfui hide` and `/wfui show`; `/wfui alpha 0.6`; `/wfui scale 0.8`; `/wfui reset` forgets positions and ticks. `/wfui` alone prints the list. Your AI can also set sizes and positions, but your own moves are remembered.

**Why does the overlay never show my level or XP?**
By design. Level and XP are in the data the AI sees, so it can plan, but they are kept off the overlay.

**Does it work in dungeons, raids and battlegrounds?**
Partly. The game hides a lot of information from addons in combat and inside instances (exact health and mana, buffs, who is attacking you, your map position in instances). The addon marks hidden values as `unknown` and never guesses. Read [FOREVER-API.md](FOREVER-API.md).

**Can it tell what killed me?**
Only roughly. The game's combat log is not available to addons. The addon reconstructs a fight from other signals and reports the best guess of the enemies that were targeting you. It cannot give exact damage by source.

**Can it use more than one character or account?**
It follows the biggest WoW window and whichever character is logged in there. Switching characters works; two game windows at once are not supported.

**What happens when the Mac or PC sleeps?**
Nothing is read while it sleeps. The services are set to keep running and the feed should resume when it wakes; if it does not, run `wowmentor restart`. We have not tested every sleep scenario.

## Managing it

**How do I stop it for a while?**
`wowmentor stop`. Start it again with `wowmentor start`. The services also start at login (at sign-in on Windows). To turn the overlay off in game only: `/wfui hide`.

**How do I update?**
Run the install line again (see the [README](../README.md)). It keeps your settings, links and profile.

**How do I get new private links?**
`wowmentor rotate-secrets`, then `wowmentor links`.

**How do I remove it completely?**
`wowmentor uninstall --purge`. See the end of [QUICKSTART.md](QUICKSTART.md) (and [WINDOWS.md](WINDOWS.md#uninstall) on Windows) for the last steps.

**Where do I report a bug or ask for something?**
Open an issue on GitHub with the form. For a security problem, follow [SECURITY.md](../SECURITY.md) and do not post it publicly.
