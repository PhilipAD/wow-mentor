# How WoW Mentor works

**What this is for:** explaining the moving parts, so you can trust it and fix it. **Who it is for:** curious players and developers. You do not need this to use WoW Mentor. For installing, start with the [README](../README.md).

## The big picture

```text
 ┌──────────────────────────── your computer ───────────────────────────────────┐
 │                                                                               │
 │   WoW Forever client                                                          │
 │   ┌───────────────────────────┐                                               │
 │   │ WoWFeed addon             │  1. draws a hairline strip of coloured        │
 │   │  - collects game data     │     squares on the top edge of the window     │
 │   │  - draws the strip        │                                               │
 │   │  - draws the overlay      │◄──────────── 4. overlay updates arrive ──┐    │
 │   │  - runs your rules        │     (files in "slot" addon folders)      │    │
 │   └─────────────┬─────────────┘                                          │    │
 │                 │ pixels                                                 │    │
 │                 ▼                                                        │    │
 │   Bridge (background service, "WoW Mentor Bridge")                       │    │
 │    - reads the game window picture (read-only), decodes the strip        │    │
 │    - 2. pushes a text snapshot of your game to the server ───────┐       │    │
 │    - writes overlay updates into the slot folders ───────────────┼───────┘    │
 │    - applies guide edits to your Guidelime files (with backup)   │            │
 │                                                                  ▼            │
 │   Server (background service, only listens on 127.0.0.1)                      │
 │    - keeps the newest snapshot, the overlay document, rules, events           │
 │    - 3. answers your AI:  GET feed, GET/POST ops, GET events                  │
 │                                                                  ▲            │
 └──────────────────────────────────────────────────────────────────┼────────────┘
                                                                    │
        AI on this computer (Claude Code, Codex...) talks to 127.0.0.1 directly
        AI elsewhere: optional free Cloudflare tunnel ("wowmentor share on")
        Optional: bridge also forwards events to a webhook URL that you set
```

Everything on the left of the line is on your computer. Nothing is sent anywhere until you choose to (see [PRIVACY.md](PRIVACY.md)).

## Why a pixel strip?

A World of Warcraft addon runs in a sandbox. It cannot open a network connection, run programs or write files whenever it likes. (Its normal saved settings file is only written when you log out or `/reload`.) The Forever client uses the modern addon rules, which are even stricter in combat. So the addon has no direct way to tell a program on your computer what is going on in the game while you play.

The one thing an addon can do freely is draw on screen. So the addon draws its data as a strip of colours, and a separate program on your computer reads the strip. This idea comes from the open source project wow-ai (see [NOTICE](../NOTICE)).

### The strip in detail

- It sits at the very top edge of the game window, drawn above everything else. It is a few pixels tall and ignores the mouse.
- **Row 1** starts with a white anchor square, then data squares. Each square is 3 by 2 pixels. Each of red, green and blue is fully on or fully off, so one square holds 3 bits, and the colours survive any brightness, gamma or contrast setting.
- **Row 2** holds three plain status bars: player health (white), player power (yellow) and target health (red). The game fills them itself, even when the game hides the numbers from addons in combat. The bridge measures how full they are, which gives you health and mana as a percentage.
- A message is: two magic bytes (`C7 1A`), a 2 byte message number, a 2 byte length, the text, and a checksum (Fletcher-16). A message that fails the checksum is thrown away.
- The text of a message is `section`, `id`, `part`, `parts`, `body`, separated by an invisible separator character (code 31). Large sections are split into parts.
- The addon sends one message every half second (so a complete refresh of every section takes a while; events go first and the `fast` section, with your position and state, gets every second turn). When there is nothing new it sends a heartbeat (`hb`).
- Plain-text log lines (`ev` messages) are sent before sections. Structured events (new in 1.0.0) travel in their own message type, `evj`, which carries a JSON list of events; at most one `evj` message is sent per tick, and never two in a row while a section is waiting, so a burst of events cannot starve the state sections.
- How much fits depends on your screen resolution. The addon reads your physical screen size to work out the maximum message size.

### The bridge reads it

On a **Mac** the bridge asks macOS for a picture of the **World of Warcraft window only** (not your whole screen), about five times a second, with the CoreGraphics window capture call. This is why macOS asks for the Screen Recording permission. On **Windows** the bridge copies the strip of screen where the top edge of the WoW window is (a screen-DC BitBlt, about ten times a second), which is why WoW must be Windowed or Windowed (Fullscreen) and uncovered; there is no permission prompt. Either way it copies only the top 220 rows into memory, looks for the white anchor and magic bytes, and decodes. Nothing is written to disk and no picture is kept.

Because the picture comes from the window itself, things laid over the game on your screen (like the small black bar that hides the strip) do not get into the captured picture. And the bridge never sends keys or mouse clicks to the game.

## What each background process does

| Process | Started by | Job |
| --- | --- | --- |
| **server** | Mac: `launchd` (label `com.wowmentor.server`). Windows: the sign-in supervisor. Either way it runs `python3 -m wowmentor server` | A small HTTP server using only Python's standard library. Binds to `127.0.0.1` only. Stores the newest snapshot, the overlay document, rules, coach inbox, memory notes and the guide cache. |
| **bridge** | Mac: `launchd` (label `com.wowmentor.bridge`), through the little app `~/Applications/WoW Mentor Bridge.app` which just launches Python. Windows: the same sign-in supervisor | Captures and decodes the strip, pushes the snapshot, delivers overlay updates, applies guide edits, and (new) forwards events to your webhook. Runs several threads. |
| **mask helper** | The bridge (on a Mac this is `wowmask`, if it was built) | A tiny borderless window that covers the strip on your screen. Ignores the mouse and keyboard. |
| **tunnel** | Mac: `launchd` (label `com.wowmentor.tunnel`). Windows: the supervisor. Only after `wowmentor share on` | Runs Cloudflare's `cloudflared` to give you a temporary public address that leads to your server. |

The services start when you sign in and are restarted if they stop. On a Mac that is `launchd` (`KeepAlive`). On Windows one per-user entry named `WoWMentor` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` starts a small supervisor that restarts the server and the bridge. `wowmentor stop` and `wowmentor start` control them. Logs: `~/Library/Logs/WoWMentor/` on a Mac, `%LOCALAPPDATA%\WoWMentor\logs` on Windows. `wowmentor logs` prints the last lines.

Files WoW Mentor writes: your config and secrets (`config.json`, readable by your account only; `~/.config/wowmentor` on a Mac, `%APPDATA%\WoWMentor` on Windows), your profile (`profile.md`), saved state (`data/`), guide backups (`data/guide-backups/`), the addon and slot folders in your WoW `Interface/AddOns`, and the start-at-login setup (on a Mac the launcher app in `~/Applications` and the `launchd` plists in `~/Library/LaunchAgents`; on Windows the `Run` entry above and the program in `%LOCALAPPDATA%\WoWMentor\app`). It never touches your characters or the `WTF` folder (it only reads a few other addons' saved data, see [PRIVACY.md](PRIVACY.md)).

## How the AI sees your game

The bridge puts the decoded sections together into one text page: the **feed** (see [FEED.md](FEED.md)). It adds a few things it computes itself: derived session numbers (XP per hour, gold per hour), health and power as measured from the bars, and a summary of what some other addons saved on disk. It pushes the page to the server about once a second, and the server hands it to your AI at a private address. Your AI only ever reads text.

## How the overlay gets into the game: slots and the doorbell

This is the clever and slightly odd part. The bridge cannot send anything to the game directly, and an addon cannot read new files while it is running. WoW only reads an addon's files when that addon is loaded. So:

1. At install time WoW Mentor creates 1001 tiny empty addons named `WFSlot000` to `WFSlot1000`. They are marked "load on demand", which means the game knows them but does not load them until an addon asks.
2. When your AI changes the overlay, the bridge writes the new overlay document (as a small Lua file called `Inbox.lua`) into the next few unused slots.
3. The WoWFeed addon asks the game to load the next slot. The game runs that file, the file stores the document, and the addon reads it and redraws the panels. The game loads each addon only once per session, which is why every update needs a fresh slot. A `/reload` resets the count, so all slots become available again.
4. To know when to look, the addon watches a "doorbell". The bridge writes the newest revision number into a line of `WFSlot000.toc` (`## X-Door: <revision>`), and the addon reads that line through the game's addon metadata function once a second. If the client does not notice changes to that file while running, the addon falls back to quickly trying the next slot every 6 seconds after activity and every 30 seconds otherwise. The mode in use is reported in the feed (`uimeta` in the `KV` line of `SUMMARY`). (A silent sound file `sig/NNN.wav` was also tried as a doorbell; it is only used for a self test at startup, because empty sound files report as playable on this client.)
5. Slot `WFSlot000` is the **boot slot**: it always holds the newest document and is read every time the interface loads, so after a `/reload` or a restart you get the current overlay at once.
6. The addon reports back through the strip: which revision it applied, which slot it is on, and what you did (for example, you ticked a checklist item). The bridge turns a tick into a `check` operation so your AI sees it.

**Why a restart was needed once:** the game only learns which addon folders exist when it starts. The installer creates the slot folders on disk. A game that was already running at that moment will not know about them until you fully quit and start it again (a game that was closed just discovers them when it starts). Without them the overlay cannot update while you play. After that first restart no more restarts are needed. (This is behaviour we observed and handle in code. If you know of a way around it that does not break the game's rules, please open an issue.)

The document the AI sends is checked by the server before it is stored. Unknown fields, bad colours, over long text and too many items are rejected with a message that says exactly what to fix. The addon only ever receives a clean, size limited document (at most 60,000 bytes). See `GET .../schema` or `wowui schema`.

## Rules and instant reactions

An AI that polls every few minutes reacts late. So rules (for example "when my bags have 2 or fewer free slots, show a warning") are sent to the addon as part of the overlay document and run **inside the game** on the right game event, instantly, without any AI or computer involved. Each time a rule fires the addon records it and reports an event back, so your AI learns about it. Events also go out through the strip to the bridge, which can forward them to a webhook URL if you set one. See [RULES.md](RULES.md).

## Presence

New in 1.0.0. The bridge works out whether you are playing and adds it to the feed (`SESSION_STATE`) and to the server (`GET .../session`):

| State | Meaning |
| --- | --- |
| `in_world` | The strip was decoded within the last 15 seconds and the game says you are not AFK. |
| `afk` | The game says you are AFK. |
| `loading` | The game window exists, the strip is stale for less than 60 seconds and the last event was not a logout. |
| `logged_out` | The window exists but the strip is gone (more than 20 seconds after a logout event, or more than 90 seconds in any case). |
| `game_closed` | No WoW window. |

Also reported: when you last logged in and out, whether WoW is the front window (`foreground`: true, false or unknown), when the bridge itself last checked in (so your AI can tell "game quiet" from "my helper is down"), and how long since the game last updated. The addon cannot find out by itself if it is in the foreground (see [FOREVER-API.md](FOREVER-API.md)), so the bridge asks the operating system (macOS or Windows).

## What WoW Mentor does not do

It does not send keys or clicks, cast spells, target, move, loot, accept quests or trade. The game does not allow addons to do those things, and WoW Mentor does not try to work around that. It only displays information, sets map pins (waypoints), prints chat lines for you, plays sounds and flashes the dock icon.

## For developers

Source layout and how to run the tests: [CONTRIBUTING.md](../CONTRIBUTING.md). The design for events, presence, rules and the webhook: [DESIGN-coach-events.md](DESIGN-coach-events.md).
