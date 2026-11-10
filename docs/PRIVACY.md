# Privacy: what leaves your computer, and when

**What this is for:** an exact, plain account of where your data goes. **Who it is for:** anyone deciding whether to install WoW Mentor, whether to turn on sharing, or whether to set a webhook.

## The short version

- By default **nothing leaves your computer.** The server listens on `127.0.0.1` (only programs on your own computer can reach it). There is no telemetry, no account, no analytics and no update check in the code.
- Data leaves your computer only if you (1) give your links to an AI tool, (2) run `wowmentor share on`, or (3) set a webhook. Each is explained below.
- The game snapshot is **text**, not screenshots. No picture is stored or sent.
- Other players' chat is left out of the snapshot unless you turn it on.
- Your links contain long random secrets. Treat them like passwords.

## What the program reads

| Source | What is read | Where it goes |
| --- | --- | --- |
| The WoW window picture (Mac: needs Screen Recording permission; Windows: no prompt) | Only the top 220 rows are copied into memory, to find and decode the data strip. Nothing is saved to disk. On a Mac it is the picture of the **WoW window only**. On Windows it is a copy of the **screen area where the top of the WoW window is**, so anything drawn over that area (another window) would be in it for that instant; it is decoded in memory and thrown away. | Decoded into text in memory |
| The WoWFeed addon (data strip) | Game data, listed below | The local server, as text |
| Other addons' saved data on disk | Read-only: `BagBrother`, `Questie` and `Guidelime` saved variables, if you have them (files under `WTF/Account/*/SavedVariables/`) | Into the feed, section `OFFLINE SAVEDVARIABLES` |
| Guidelime guide files | Every guide file in addon folders named `Guidelime_*` | Cached on the local server so your AI can read them; edits are written back with a backup |

The feed can include (see [FEED.md](FEED.md) for the full list): your character name and realm, race, class, level and XP, position and zone, health and power, gold, quests, gear and bags (with item names), professions and skills, talents, cooldowns, buffs and debuffs, nearby players and creatures, party and guild names, **your friends list** (names, level, zone, online status), the **guild message of the day**, saved raid and dungeon lockouts, mail counts, and the **list of your loaded addons**. If you have BagBrother, it also includes **bank and mail contents and gold for every character on that account**. If you turn chat on (below) it includes other players' chat. All of this stays on your computer unless you hand it on. If you do not want something in the feed, do not share the feed.

The addon never reads your account name, password, email, payment details or other apps.

## When data leaves your computer

### 1. You give the links to an AI tool

An AI tool that can reach your links reads your feed and draws on your overlay. What the AI reads is sent to that AI provider (for example, the company behind Claude or Codex), and is covered by their privacy terms, not ours. Things to know:

- The text you paste from `wowmentor links` **contains your secret link.** For local links (`127.0.0.1`) that is useless to anyone off your computer. For shared links it is not.
- AI conversations are often stored by the provider. Do not put anything in your profile (`wowmentor profile`) that you would not want them to have. The profile is added to the AI's instructions.
- Agents can be fooled by text in the feed (such as a player's name or the guild message). See [AGENT.md](AGENT.md).

### 2. You turn on sharing (`wowmentor share on`)

This runs Cloudflare's free `cloudflared` quick tunnel. It gives your local server a temporary public address (`https://<random words>.trycloudflare.com`).

- **Cloudflare's network carries every request and response** between your AI and your computer, including your feed and overlay. Cloudflare terminates the encrypted connection, so it can in principle see the traffic, as with any tunnel or proxy. See Cloudflare's terms for what they log.
- **Anyone who has your link can use it.** There is no password besides the long random part of the address. Anyone on the internet who learns the address can read your game feed (with the read link) or change your overlay, rules and guide files (with the overlay link). The address also appears in the AI's conversation, in the AI provider's logs and, if you paste it into a browser, in your browser history.
- The server's front page (`/`) and `/healthz` answer anyone who finds the tunnel address, without a secret.
- The address changes whenever the tunnel restarts. Run `wowmentor share off` when you are done.
- If you do not have `cloudflared`, `wowmentor share on` offers to install it with Homebrew, which downloads it from the internet.

Agents that can only fetch web addresses write to your overlay with `GET .../ops?b=<base64 of the JSON>`, so the content of what they draw is part of the web address, and can end up in the logs of the tunnel, the AI provider and your browser history.

Anything you need an AI to do from a website or another computer needs this. If your AI runs on your computer, you do not need it.

### 3. You set a webhook (new in 1.0.0)

```bash
wowmentor webhook set https://example.com/hook --secret -
```

The bridge then sends in-game events (deaths, level ups, quest changes, bag and durability warnings, zone changes, rule firings, session state changes and more) as JSON to **that address**, batched so that at most one message goes out every 20 seconds (urgent events such as a death wait about 3 seconds). Details and examples: [RULES.md](RULES.md). Things to know:

- Each message is a JSON body of the form `{"v":1,"sent_at":...,"session":{...},"events":[...],"next":N}`. The `session` part says whether you are in the world, whether WoW is the front window, and when you last logged in or out. The request carries the header `User-Agent: wowmentor-webhook/1`. The data goes to whoever runs that address. Use an address you control or trust, and use `https://`.
- Set a secret. The bridge signs each message with it (`X-WoWMentor-Signature`), so your receiver can check the message really came from your bridge. The secret is stored in your config file and is **not** changed by `rotate-secrets`.
- Events do not include other players' chat. They can include: character name, zone, subzone, map position, names and levels of creatures that attacked you, item names you looted or equipped, quest names, NPC names and positions, and the text you type after `/coach`.
- Remove it with `wowmentor webhook off`. Check it with `wowmentor webhook status`. The address is never printed in full or written to the logs (only `scheme://host`), and redirects are not followed.
- `--secret -` makes the command read the secret from standard input, so it does not stay in your shell history.

### 4. You run the server somewhere else (advanced)

`wowmentor server-url set <address>` points the bridge at a server on another machine instead of the one on your computer. Then the **whole game snapshot and all events are sent over the network to that address** every second. Only do this with a machine you control, and use `https://` (or a private network such as a VPN): the command also accepts plain `http://`. This mode is for advanced users, and this repository does not document how to deploy a server elsewhere. Turn it off with `wowmentor server-url off`.

### That is all

Nothing else makes a connection to the internet: not the addon (the game does not allow addons to open network connections), not the bridge (apart from the webhook and a `server-url` you set), and not the server. The installer downloads from GitHub, `wowmentor update` may use `git pull`, `share on` talks to Cloudflare (and may run `brew install cloudflared` on a Mac, or tell you to run `winget install --id Cloudflare.cloudflared` on Windows), and on a Mac the two helper programs are compiled on your computer from files in the repository (on Windows nothing is compiled: the helpers are Python files in the same folder). To check for yourself, search the code for `urlopen`: all other uses are local.

## Chat filtering (default: on)

The addon records chat events so your AI can see recent activity. By default the bridge **drops these chat types before they reach the feed**: say, yell, emote, text emote, whisper (in and out), custom channels, guild, officer, party, party leader, raid, raid leader, raid warning, instance chat, Battle.net whispers and community channels.

Not dropped, because they are game messages and not other players talking: system messages, loot, money, skill-ups, XP, reputation and honor lines. System messages can contain other players' names (for example "so-and-so has joined the party").

To turn chat **on**, edit the config file `~/.config/wowmentor/config.json`, set `"include_chat": true`, save, and run `wowmentor restart`. Set it back to `false` to turn it off again. Think before you do: other players did not agree to be read by your AI. Chat is also a way for someone to try to give your AI instructions.

## Your secrets

`wowmentor setup` creates three random secrets (each 48 hex characters, 192 bits) and stores them in `~/.config/wowmentor/config.json` on a Mac or `%APPDATA%\WoWMentor\config.json` on Windows, readable only by your own user (mode 600 on a Mac; on Windows the file's permissions are restricted to your account). They are not encrypted at rest. Backups of your home folder (Time Machine or File History, for example) include the file.

| Secret | What it opens | Where it appears |
| --- | --- | --- |
| Read token | The read-only game feed (and, new in 1.0.0, read-only events) | In the feed link |
| Overlay secret | The overlay and rules API, the briefing and memory pages, the guide editor, the event feed | In the overlay link, and in the text you paste to your AI |
| Write secret | Replace the feed snapshot (used only by the bridge, on your computer) | Never printed. It exists only in the config file. |

- **Rotate** after any leak: `wowmentor rotate-secrets`. It creates new ones, restarts the services and the old links stop working immediately. Then run `wowmentor links`.
- The server **does not log request paths**, because they contain secrets. Replies carry `X-Robots-Tag: noindex` and `Cache-Control: no-store`.
- Wrong secrets get a plain "not found" (404), the same as any unknown address.
- Do not put links in screenshots, issues, chats with other people, public repositories or shared prompts.

## What a leaked link allows

| If someone has... | They can... |
| --- | --- |
| The feed link only | Read your game snapshot (everything in [FEED.md](FEED.md)) |
| The overlay link | Do all of the above (the briefing page includes the feed), plus draw anything on your screen, create rules that run in your game, read and change your AI's notes, and **edit your Guidelime guide files** (each edit is checked, written to disk with a backup, and you are asked to `/reload`) |
| The webhook secret | Send fake events to your own receiver (it only signs messages; it does not open anything on your computer) |

Even with the overlay link nobody can press keys or control your character; the game does not allow addons to do that.

## Where your data is stored

| Path | What |
| --- | --- |
| `~/.config/wowmentor/config.json` | Settings and secrets |
| `~/.config/wowmentor/profile.md` | Your profile text |
| `~/.config/wowmentor/data/` | Overlay document and rules, the AI's memory notes, guide cache and edit history, events (new), `guide-backups/` |
| `~/Library/Logs/WoWMentor/` | Logs of the server, bridge and tunnel (request paths are not logged) |
| Your WoW `Interface/AddOns` folder | The addon and slot folders, nothing personal |

The feed snapshot itself is held in memory by the server and is not written to disk.

To delete everything WoW Mentor stored about you: `wowmentor uninstall --purge`. That removes the services, helper app, addon folders, config, secrets and saved data. It never deletes your guide files (the backups of any guide the AI edited are deleted with `--purge`, so restore any guide you want first, or copy `data/guide-backups`).

## Questions

Open an issue for questions (do not paste your links). To report a security problem privately, read [SECURITY.md](../SECURITY.md).
