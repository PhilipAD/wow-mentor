# WoW Mentor

**What it is for:** letting an AI assistant watch your World of Warcraft game and coach you with a small, see-through overlay on your screen. **Who it is for:** Mac players on the WoW Forever client who use an AI tool such as Claude Code or Codex. You do not need to be technical. If you can copy and paste, you can install it.

WoW Mentor has three parts that run on your Mac:

1. A small **addon** inside WoW draws a thin strip of coloured squares along the very top edge of the game window. The squares are your game data (position, quests, bags, gear and so on) written as colours.
2. A **bridge** program reads that strip from the screen (read-only, nothing is ever sent to the game as clicks or keys), turns it back into text, and keeps a live text snapshot of your game.
3. A tiny **local server** shows that snapshot to your AI tool, and lets the AI draw checklists, progress bars, waypoints, toasts and coach messages in your game. It also lets you set up instant "rules" that run inside the game (for example, warn me when my bags are almost full).

Your AI reads the snapshot, thinks about it, and puts a short plan on your screen. You still do all the playing.

## Quick start (about 5 minutes)

Before you start: you need a Mac, the WoW Forever game installed and started at least once, and an AI tool (see "Supported AI tools" below).

**Step 1. Install.** Open the Terminal app (press Command and Space, type `Terminal`, press Return), paste this line, press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.sh | bash
```

It downloads WoW Mentor, finds your WoW folder, installs the addon, and starts two small background services. You will see lines numbered `1/5` to `5/5`, and System Settings opens at the end. If Terminal says Python is missing, click Install in the window macOS shows, wait, then paste the same line again. If it says it could not find World of Warcraft, start the game once and paste the line again. Still stuck? See [docs/QUICKSTART.md](docs/QUICKSTART.md#way-a-paste-one-line-into-terminal-recommended).

Prefer not to use Terminal commands to install? [docs/QUICKSTART.md](docs/QUICKSTART.md) has a double-click way.

**Step 2. Allow screen reading, then restart WoW once.**

- In System Settings, go to Privacy & Security, then Screen & System Audio Recording. Click the plus button, add **WoW Mentor Bridge** (it is in the `Applications` folder inside your home folder), and turn it on.
- Fully quit World of Warcraft (Command and Q) and start it again, then log in to a character. The restart is needed once so the game can see the files the installer added.

**Step 3. Connect your AI.** In Terminal run:

```bash
wowmentor links
```

If Terminal says `command not found`, run `~/.wowmentor/app/wowmentor.sh links` instead. Copy the text between the two dashed lines and paste it to your AI tool. That is it. (Treat that text like a password: it contains your private link.)

Want to check that everything works first? Run `wowmentor doctor`. It lists what is fine and what to fix. See [docs/QUICKSTART.md](docs/QUICKSTART.md) for the slow, step by step version with what you should see after each step.

## What you will see

- In Terminal, `wowmentor doctor` prints a short checklist. Green ticks are good. Each warning or cross has a line underneath telling you what to do.
- In the game, after you log in, one chat line appears: `WoWFeed active (hairline strip, top edge)`.
- At the very top edge of the game window there is a hairline strip of coloured squares. It is meant to be invisible: if you built the optional helper (the installer tries to), a thin black bar covers it on your screen. If you do see coloured dots, that is harmless, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
- Once your AI starts, small panels appear in a corner of your game: a short list of next steps you can tick, maybe a progress bar, maybe a map pin. Mouse clicks pass through the panels, except that you can click a checklist item to tick it. Panels dim in combat and can fade when idle. Drag a panel by its title after typing `/wfui unlock`.
- A built-in "Character" panel with your professions, skills, empty gear slots, durability and bags is drawn by the addon itself.
- Level and XP are never shown on the overlay. The AI can still see them in the data.

## What you need

- A Mac. WoW Mentor reads the game window with a macOS feature, so Windows and Linux are not supported.
- The **WoW Forever** client, installed in the usual place. The addon targets interface 16001. It is built for Forever only.
- Python 3.9 or newer. If you do not have it, the installer tells macOS to offer Apple's free Command Line Tools. Click Install, wait, then paste the same install line again. Those tools also let the installer build the two small helpers (the launcher macOS lists under Screen Recording and the bar that hides the strip).
- An AI tool that can run commands or fetch web addresses. See "Supported AI tools" below.
- Optional: the **Guidelime** addon and a guide pack. If you have them, your AI can read and carefully edit your leveling guide, with a backup before every change. Not needed for anything else.

## Supported AI tools

The AI side is plain text over HTTP, so any agent that can either run shell commands or fetch URLs can work:

- Agents that run commands on your Mac (Claude Code, Codex CLI and similar) are the easiest. They use the `wowui` command and the local links. See [docs/AGENT.md](docs/AGENT.md).
- Chat assistants and agents on a website or another computer need `wowmentor share on`, which makes a free Cloudflare tunnel so they can reach your Mac. Read [docs/PRIVACY.md](docs/PRIVACY.md) before you turn this on.

The project has not been tested against every product. Tests cover the commands and the HTTP interface, not any specific AI service.

## Privacy and safety in short

- Nothing leaves your Mac until you run `wowmentor share on`, set a webhook, or hand your links to an AI tool. The server only listens on `127.0.0.1` (your own Mac).
- Your links contain long random secrets. Anyone who has a link can read your game snapshot or change your overlay. Do not post them. `wowmentor rotate-secrets` makes new ones.
- The snapshot is not a screenshot. The bridge copies only the top rows of the game window into memory to find the strip, and nothing is saved to disk.
- Chat from other players (say, yell, whisper, party, raid, guild, channels) is left out of the snapshot unless you turn it on.
- WoW Mentor cannot cast spells, move, target, accept quests or press keys for you. It only displays things and prints chat lines to you.

Details: [docs/PRIVACY.md](docs/PRIVACY.md) and [SECURITY.md](SECURITY.md).

## More documentation

| If you want to... | Read |
| --- | --- |
| Install slowly, with screenshots in words | [docs/QUICKSTART.md](docs/QUICKSTART.md) |
| Quick answers | [docs/FAQ.md](docs/FAQ.md) |
| Fix a problem or understand a doctor message | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| Understand how it works | [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) |
| Connect Claude Code, Codex or another AI | [docs/AGENT.md](docs/AGENT.md) |
| Know exactly what data goes where | [docs/PRIVACY.md](docs/PRIVACY.md) |
| See every data section the AI gets | [docs/FEED.md](docs/FEED.md) |
| Write instant in-game rules, use webhooks | [docs/RULES.md](docs/RULES.md) |
| See what the Forever game client does and does not allow | [docs/FOREVER-API.md](docs/FOREVER-API.md) |
| Report a security problem | [SECURITY.md](SECURITY.md) |
| Help build it | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See what changed | [CHANGELOG.md](CHANGELOG.md) |

## Honest limits

- **macOS only.** The screen reader uses a macOS API.
- **Forever client only.** Other WoW versions are not tested and may not work.
- **One restart of WoW is needed** after the first install. The game only discovers addon folders that exist when it starts. After that, updates to the overlay arrive while you play.
- Guide edits (Guidelime) only take effect after you type `/reload` in the game.
- The game hides some numbers from addons. In combat your exact health and mana are hidden, so they are estimated from the status bars. Some things (the exact killer in a fight, auction prices, other players' gear) are not available at all. See [docs/FOREVER-API.md](docs/FOREVER-API.md).
- Overlay updates reach the game through a side door and can take a few seconds (up to about 30 in the worst case). The game must be running with your character logged in.
- This is a first release. If something does not behave as the docs describe, please open an issue.

## Disclaimer

WoW Mentor is an independent, unofficial project. It is **not affiliated with, endorsed by or approved by Blizzard Entertainment.** World of Warcraft is a trademark of Blizzard Entertainment, Inc.

It is designed to be display-only: it reads the game window (screen capture, read-only), never sends keyboard or mouse input to the game, and does not automate gameplay. The addon only shows information and cannot take game actions.

Blizzard has not reviewed or blessed external tools that read the screen, and their rules can change. **Use it at your own risk.** It is provided "as is", without warranty, under the [MIT License](LICENSE). See [NOTICE](NOTICE) for third-party credits.
