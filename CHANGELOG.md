# Changelog

**What this is for:** what changed in each release. **Who it is for:** players deciding whether to update, and contributors. The format is based on Keep a Changelog; version numbers follow Semantic Versioning.

## Unreleased

### Windows support

- Windows 10 (2004 or newer) and 11 now work. Install with one PowerShell line (`install.ps1`) or by double-clicking `Install-WoWMentor.cmd`. No admin needed.
- The bridge reads the top rows of the WoW window itself (no permission prompt). WoW must be in Windowed or Windowed (Fullscreen) mode.
- The strip-hiding bar is a click-through window that is left out of screen capture, so it never feeds back into what the bridge reads.
- Starts at sign-in through a per-user Run entry. Settings in `%APPDATA%\WoWMentor`, logs in `%LOCALAPPDATA%\WoWMentor\logs`. Details: `docs/WINDOWS.md`.
- Internals: macOS-only code moved behind a small per-OS layer (`wowmentor/host`). CI now also runs the full test suite on Windows.

## 1.0.0 (first public release)

The first release. Everything below is new.

### Install and run (macOS)

- One-line installer (`install.sh`) and a double-click installer (`Install WoW Mentor.command`). Safe to run again to update. Needs Python 3.9 or newer and offers Apple's Command Line Tools if Python is missing.
- `wowmentor setup` finds the WoW Forever folder, installs the addon, creates 1001 empty load-on-demand "slot" addons, builds two small helpers (a launcher app that macOS can list under Screen Recording, and a bar that hides the data strip on screen), writes launchd services for the server and the bridge, and opens the Screen Recording settings.
- `wowmentor doctor` checks the whole chain and prints a fix under every problem. Other commands: `status`, `links`, `prompt`, `share on|off|status`, `profile`, `start`, `stop`, `restart`, `logs`, `rotate-secrets`, `update`, `uninstall [--purge]`.
- Private links with long random secrets, stored in a file only your user can read. `rotate-secrets` replaces them.
- `wowmentor share on` makes the links work from anywhere through a free Cloudflare quick tunnel.
- A profile file (`wowmentor profile`) that is added to the AI's instructions.
- No dependencies: Python standard library only, plain Lua.

### Seeing the game

- The WoWFeed addon writes game data as a hairline strip of coloured squares along the top edge of the game window. The bridge reads the picture of the WoW window only, read-only and without ever sending input, decodes it, and keeps a live text snapshot. Nothing is written to disk.
- The strip codec is adapted from chelinho139/wow-ai (MIT). See `NOTICE`.
- Health, power and target health are measured from three status bars, because the game hides exact values from addons in combat.
- The feed covers position and state, vitals, stats, equipment (with empty slots and durability), bags and vendor value, quests, talents (including the trait system), skills and the spellbook, professions and recipes, PvP, reputation, currencies, buffs and debuffs with time left, group, nearby nameplates, cooldowns, social, system and more. See `docs/FEED.md`.
- Derived session numbers: XP per hour, time to next level, gold per hour, honorable kills per hour.
- Read-only look at what BagBrother, Questie and Guidelime saved to disk.
- Other players' chat is left out of the feed by default (`include_chat` in the config turns it on).

### Drawing on the screen

- An overlay of panels, checklists, progress bars, key/value lists, callouts, timers, icons, rows, toasts, waypoints (using TomTom when installed), chat lines, sounds and quest tracking. Mouse clicks pass through panels except on checklist items; panels dim in combat and can fade when idle. Players can move, hide, scale and reset panels (`/wfui`).
- A built-in character panel (professions, skills, empty gear slots, durability, bags) written by the addon itself.
- The overlay document is validated by the server before it reaches the game: unknown fields, bad colours, oversize text and too many items are refused with a message that names the problem. Operations are all-or-nothing, can be retried safely (`opId`), and can be applied partially (`partial`).
- Updates reach the game while it runs, through the slot addons, with a boot slot that restores the newest overlay after a `/reload`. One full restart of WoW is needed after the first install.
- What the player does (ticks an item, moves or collapses a panel) is reported back to the AI.
- Level and XP are never shown on the overlay.

### Connecting an AI

- A briefing page (`/skill`) that tells an agent how to mentor, and a one-command prompt to paste (`wowmentor links`).
- `wowui`, a dependency-free command for agents: panels, tasks, ticking, text, progress bars, callouts, components, toasts, chat lines, waypoints, themes, raw operations, state, schema, guide and example.
- Persistent notes for the agent (`wowui memory`) and `wowui brief`, which rebuilds its context after a reset.
- Writes by GET (`/ops?b=<base64>`) for tools that can only fetch web addresses.
- Live editing of Guidelime guides (`wowui tug`): list, show, search, replace, insert, delete, substitute, history and restore, with validation, atomic writes, and a backup before every change.

### Instant coaching: rules, events, presence (new in 1.0.0)

- **Rules** that run inside the game, with no polling: bags nearly full, durability low, repeated deaths in a subzone, a missing buff, a lower spell rank on the action bar, level up, zone change, loot, equipment, elite targets and more. Conditions can be combined (`all` / `any`). Actions: toast, chat line, tick an item, waypoint, change panel text, sound, flash. Five ready-made presets: `wowui rule preset bags-full|durability|deaths|buff|lower-rank`. See `docs/RULES.md`.
- **Events** reported from the game: login, logout, level up, death (with a best-guess killer), spell learned, quest ready and turned in, items looted and equipped, zone changes, vendor, trainer, reward and loot windows, bag and durability warnings, fights (casts, wasted damage-over-time spells), NPCs met, rule firings, acknowledgements. Served with `GET .../events?since=` and `wowui events`, also through a read-only feed-link twin.
- **Presence**: `in_world`, `loading`, `afk`, `logged_out`, `game_closed`, whether WoW is the front window, and a heartbeat from the Mac helper. Available as `SESSION_STATE`, `wowui session`, and as events.
- **Webhook**: `wowmentor webhook set|off|status|test` sends batched events to your URL, signed with HMAC-SHA256 if you set a secret, with debounce, retries and a queue.
- **Coach inbox**: `wowui coach` messages shown in chat, as banners and in a small panel; the player can ask with `/coach <question>` and the AI answers with `wowui coach --answer`.
- **Checklist auto-complete** (quest done, spell known, skill level, item in bags, zone) and **waypoints that clear themselves** when you arrive.
- New feed sections: `ACTIONBARS`, `PET`, `TARGET`, `FIGHTS`, `DEATHS`, `ITEMS` (with binding and soulbound state), `REWARDS`, `TRAINER`, `NPCS`, `RULES`, `COACH_INBOX`, `COACH_QUESTIONS`, `SESSION_ADDON`, `SESSION_STATE`, `EVENTS_TAIL`, and richer `AURAS`.
- `wowui npcs` (NPCs the player met, with where the player stood), `wowui questions`.
- `wowmentor server-url` to push the snapshot to a server running on another machine (advanced).
- Doctor rows for the game session, the webhook and a remote server.

### Documentation and project

- README, quickstart, FAQ, troubleshooting (every doctor message), how it works, agent guide, privacy, security policy, feed and rules references, and a researched report on what the Forever client does and does not allow addons to do (`docs/FOREVER-API.md`).
- MIT license, `NOTICE` with third-party credits, contributing guide, issue and pull request templates.
- GitHub Actions: Python tests on Ubuntu and macOS with Python 3.9 and 3.12, Lua 5.1 tests and syntax checks, ShellCheck, an installer test on macOS, a package build check, and a documentation link and command check; a release workflow that attaches the source archive, sdist and wheel to a tagged release. Dependabot for GitHub Actions.

### Known limits

- macOS only. Forever client only (interface 16001).
- The game hides many values in combat and in instances; those appear as `unknown`. The killer in a death is a best guess. See `docs/FOREVER-API.md`.
- WoW must be fully restarted once after the first install so it discovers the slot addons.
- Guide edits take effect after `/reload`.
- Not independently reviewed for security. See `SECURITY.md`.
- Unofficial: not affiliated with or endorsed by Blizzard Entertainment.
