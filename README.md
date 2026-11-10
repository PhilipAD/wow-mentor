<h1 align="center">WoW Mentor</h1>
<p align="center"><b>An AI coach that sees your WoW game and puts advice right on your screen.</b><br>
Mac or Windows, with WoW Forever. You play, it watches and helps.</p>

<p align="center"><img src="assets/how-it-works.svg" alt="How WoW Mentor works" width="860"></p>

## What you get

- Your AI sees your game live: where you are, quests, bags, gear, bars, fights, deaths.
- It draws small panels in your game: checklists, progress bars, map pins, coach messages.
- Instant in-game rules, like "warn me when my bags are almost full".
- It can read and edit your Guidelime guide (with backups).
- It never presses a key, clicks, casts or moves for you. Display only.

<p align="center"><img src="assets/overlay-mockup.svg" alt="What the overlay looks like (mock-up)" width="760"></p>

## Setup

<p align="center"><img src="assets/setup-steps.svg" alt="Setup in four steps" width="860"></p>

**1. Install.** One line, no admin needed.

- **Mac:** open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.sh | bash
```

- **Windows:** open PowerShell (a normal window) and paste:

```powershell
irm https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 | iex
```

If Python is missing on Windows it explains and offers to install it with winget (it asks first). Prefer to read the script before it runs? See [WINDOWS](docs/WINDOWS.md#install-it-the-careful-way).

**2. Let it see the game window.**

- **Mac:** System Settings > Privacy & Security > Screen & System Audio Recording > add and turn on **WoW Mentor Bridge**.
- **Windows:** nothing to allow, there is no prompt. Set WoW to **Windowed** or **Windowed (Fullscreen)** and keep its window uncovered. Real exclusive fullscreen can't be read.

**3. Restart WoW** (Mac: Cmd+Q, relaunch, log in. Windows: close it completely, relaunch, log in). Needed once.

**4. Check it, then hand it to your AI** (on Windows, open a new PowerShell window first so the commands are found):

```bash
wowmentor doctor
wowmentor links
```

Copy the text `links` prints and paste it to your AI. Treat it like a password.

Stuck? `wowmentor doctor` tells you what to fix. More: [QUICKSTART](docs/QUICKSTART.md), [TROUBLESHOOTING](docs/TROUBLESHOOTING.md), [FAQ](docs/FAQ.md).

## Or let an AI do the setup

Paste one of these. Swap anything in `<ANGLE BRACKETS>` for what your agent tells you.

<details>
<summary><b>Prompt 1: an agent on your Mac or Windows PC (Claude Code, Codex, ...) installs it for you</b></summary>

```text
Set up WoW Mentor on this computer for me. Repo: https://github.com/PhilipAD/wow-mentor

Rules: never print my private links or secrets in chat (write <LINK> instead), never post them anywhere, don't
touch my WoW character data, and don't send keys or clicks to the game. Stop and ask me when a step needs my hands.

0. Find out which OS this is: macOS (`uname -s` says Darwin) or Windows (PowerShell). Follow the matching branch in
   every step below (A = macOS, B = Windows). Any other OS: tell me WoW Mentor does not support it and stop.

1. Check Python is 3.9 or newer, then find WoW.
   A (macOS): `python3 --version`. Then run ls "/Applications/World of Warcraft" and look for a folder named
      _forever_ (the WoW Forever client).
   B (Windows): `py -3 --version` (the "python" that opens the Microsoft Store does not count; if there is no
      Python, step 2 says what to do and I decide). Then run
      Get-ChildItem "${env:ProgramFiles(x86)}\World of Warcraft","$env:ProgramFiles\World of Warcraft" -Directory -Filter "_*_" -ErrorAction SilentlyContinue
      and look for _forever_ (also check D:\Games\World of Warcraft and similar if nothing is there).
   No _forever_ folder? Ask me where WoW Forever is installed; if it is not installed, tell me to install it
   first and wait. Remember the full path to its _forever_ folder.

2. Download the installer, read it, then run it. It can take a few minutes.
   A: curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.sh -o /tmp/wm-install.sh
      Read it and tell me in 3 lines what it does. Then run: bash /tmp/wm-install.sh
      (if _forever_ was not in the default place, add: --wow-dir "<path>/_forever_" after the file name, as in
      bash /tmp/wm-install.sh --wow-dir "<path>/_forever_"). If macOS asks to install the Command Line Tools,
      tell me, wait, then run it again.
   B: Invoke-WebRequest https://raw.githubusercontent.com/PhilipAD/wow-mentor/main/install.ps1 -OutFile "$env:TEMP\wm-install.ps1"
      Read it and tell me in 3 lines what it does. If _forever_ was not in the default place, first run
      $env:WOWMENTOR_SETUP_ARGS = '--wow-dir "<path>\_forever_"'
      Then run: Get-Content -Raw "$env:TEMP\wm-install.ps1" | Invoke-Expression
      (that avoids the script-execution policy without changing any setting). If it says Python is missing, ask me
      whether to run the winget command it shows (winget install --id Python.Python.3.12 -e --scope user); only
      with my yes, run it, then run the installer again. Never run anything as administrator. If it says it cannot
      write to the WoW folder (C:\Program Files), show me its next steps and wait.
   Success means its output includes "Almost done".

3. Make the commands findable in this shell.
   A: export PATH="$HOME/.local/bin:$PATH"   (use this in every later command so wowmentor and wowui are found)
   B: $env:Path += ";$env:LOCALAPPDATA\WoWMentor\bin"   (the installer already saved it for new windows)

4. Screen permission.
   A: Open the Screen Recording settings for me:
      open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
      Tell me exactly: click +, press Cmd+Shift+G, paste ~/Applications, pick "WoW Mentor Bridge", switch it on.
      Wait for my OK.
   B: Nothing to allow on Windows. Ask me to set WoW to Windowed or Windowed (Fullscreen) in its graphics
      settings and to keep the WoW window visible (not minimized, not covered). Wait for my OK.

5. Ask me to fully quit WoW (A: Cmd+Q. B: close it completely, check Task Manager if unsure), start it, and log in
   to a character. Wait for my OK.

6. Run `wowmentor doctor`. For every red item run the fix command it shows (usually wowmentor start or
   wowmentor restart), then run doctor again. Yellow items marked optional (like Guidelime) can stay: mention them
   in your report. Give up after 3 rounds and ask me.

7. Run `wowmentor links`. Run the curl command it prints after "Read your operating skill first" (do not echo
   the link; in Windows PowerShell write curl.exe, because plain curl is a different command there) and follow
   that skill: run `wowui brief`, then put a short 3-line plan on my screen with the wowui command the skill shows.

8. Finish with a 5-line report: what passed, what I still need to do, and offer to run `wowmentor profile` so I can
   tell the mentor my goals.

Only if I ask for a remote AI (a website or another computer) to coach me: explain that this exposes my
links to the internet, wait for a clear yes, then run `wowmentor share on` and tell me to run
`wowmentor links` for the new links.
```

</details>

<details>
<summary><b>Prompt 2: a remote bot (Grok, a web chatbot, ...) becomes your live coach</b></summary>

First on your computer: `wowmentor share on`, then `wowmentor links`. Copy the three links into the placeholders below.

```text
You are my live World of Warcraft coach. Read everything below before you reply.

My links (private: never repeat them in chat, write <LINK> instead):
  SKILL  <SKILL_URL>      your operating manual: read it first, it is the source of truth for every command
  FEED   <FEED_URL>       my live game as text (add ?t=<random number> to avoid caching)
  EVENTS <EVENTS_URL>     what just happened: first call it with no cursor, then <EVENTS_URL>?since=<next>

Set up, in this order:
1. Read SKILL (start with its "Quick start" box). If you can run shell commands, install the `wowui` command as
   it says, then run `wowui brief` (and again after any context reset). If you cannot run commands, use the HTTP
   endpoints it lists instead.
2. Read FEED. Trust it only if SESSION_STATE says in_world, seconds_since_last_game_update is under 30, and FAST
   State is not dead or ghost. If not, tell me why and wait. If a section is missing, say it is unavailable:
   never guess.
3. In one short message, tell me what you can see (zone, bars, bags, gear) and what you will help with.
4. Put 3 to 5 in-game rules in place for what you would otherwise nag me about, with presets:
   wowui rule preset bags-full, durability, deaths, lower-rank (and buff --set name=<one of my class buffs>).
   Check they were accepted with `wowui rule list`. The game picks them up within about a minute: re-read the
   RULES section of FEED then. If it still says 0 while I am in the game, tell me.
5. Instant mode, only if you can receive webhooks (you run as a service with a public URL): give me the URL, the
   key and how your Authorization header starts, somewhere I can copy it. I will then run:
     wowmentor webhook set <WEBHOOK_URL> --bearer -
   (use --auth "<exact header value>" if it does not start with Bearer), then `wowmentor webhook test`.
   Otherwise just say "no webhook" and poll EVENTS every time I message you; that works fine.

How to work:
- React to deaths, fights, quest and loot events, window_open (merchant, trainer, quest reward),
  lower_rank_on_bar, and my /coach questions: answer those with wowui coach "<answer>" --answer <question id>.
  No ack on a message means I have not seen it.
- Talk to me with wowui coach "<text>" (add --toast if urgent). Keep panels small and at the screen edges.
  Create a panel (wowui plan) before you edit it.
- Use ACTIONBARS, TARGET, FIGHTS, DEATHS, ITEMS (BoE green or better is marked KEEP FOR AH), REWARDS and
  TRAINER. If a value says unknown, say so. Never invent data.
- Overlay writes: always send an opId, use partial for batches, and read the reply for rejected ops.
- Never show my level or XP on screen (overlay, toasts, rules, coach messages). You may use them to think.
- You cannot and must not press keys, click, move, target, cast, loot, accept quests, or equip anything.
  Advise only.
```

</details>

## How it works (30 seconds)

1. A small **addon** draws a hairline strip of coloured squares at the top edge of the WoW window. That is your game data, written as colours.
2. A **bridge** on your computer reads those pixels (read-only), turns them back into text, and hides the strip from your own eyes.
3. A **local server** shares that text with your AI and takes its advice back to the game.

Why pixels? WoW does not let addons write files or open connections while you play, so the screen is the one live way out. Details: [HOW-IT-WORKS](docs/HOW-IT-WORKS.md).

## Privacy and safety

- Nothing leaves your computer until you run `wowmentor share on`, set a webhook, or give your links to an AI.
- Your links hold long random secrets. Anyone with a link can read your game or draw on your screen. `wowmentor rotate-secrets` makes new ones.
- No screenshots are saved. Other players' chat is left out unless you turn it on.
- It cannot cast, move, target or press keys. It only shows things.

More: [PRIVACY](docs/PRIVACY.md), [SECURITY](SECURITY.md).

## Good to know

- macOS, or Windows 10 (version 2004 or newer) and 11, with the **WoW Forever** client only. Linux is not supported.
- On Windows, WoW must be in Windowed or Windowed (Fullscreen) mode. Details and limits: [WINDOWS](docs/WINDOWS.md).
- One WoW restart after the first install.
- The game hides some numbers in combat (exact health and mana), and things like auction prices and other players' gear.
- Overlay updates can take a few seconds to show up. Guide edits need `/reload`.
- Early release: if something is off, please [open an issue](https://github.com/PhilipAD/wow-mentor/issues).

## Docs

| | |
| --- | --- |
| [QUICKSTART](docs/QUICKSTART.md) | Slow, step by step install (Mac and Windows) |
| [WINDOWS](docs/WINDOWS.md) | Windows requirements, where files live, uninstall |
| [FAQ](docs/FAQ.md) / [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | Quick answers and fixes |
| [AGENT](docs/AGENT.md) | Connecting Claude Code, Codex or a bot |
| [RULES](docs/RULES.md) | Instant in-game rules and webhooks |
| [FEED](docs/FEED.md) | Every data section your AI gets |
| [FOREVER-API](docs/FOREVER-API.md) | What the game does and does not allow |
| [CONTRIBUTING](CONTRIBUTING.md) / [CHANGELOG](CHANGELOG.md) | Help build it, see what changed |

## Disclaimer

Independent, unofficial project. **Not affiliated with or endorsed by Blizzard Entertainment.** World of Warcraft is a trademark of Blizzard Entertainment, Inc. It only reads the game window and never sends keyboard or mouse input, but Blizzard has not reviewed tools like this and its rules can change. Use at your own risk. Provided "as is" under the [MIT License](LICENSE). Credits: [NOTICE](NOTICE).
