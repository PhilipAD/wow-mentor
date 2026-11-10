# Security policy

**What this is for:** how to report a security problem, and an honest description of how WoW Mentor protects (and does not protect) you. **Who it is for:** security researchers, contributors, and players who want to know the risks before installing.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

1. Use GitHub's private reporting: on the repository page click **Security**, then **Report a vulnerability**. (This needs the maintainers to have private vulnerability reporting switched on for the repository.)
2. If you cannot see that option, open a public issue that says only "I have a security report, please enable a private way to send it" and **no details**. A maintainer will reply with a private channel.

Include: what you found, the version (`wowmentor --version`), your macOS version, the steps to reproduce, and what an attacker could do. Never include real links, tokens or secrets from a real installation.

What to expect: this is a volunteer project. We will acknowledge a report as soon as we can, work with you on a fix, credit you if you want, and publish the fix and a note in the [CHANGELOG](CHANGELOG.md). We do not offer a bug bounty and make no promises about timing.

Supported versions: the latest release only.

## What is in scope

- The server, bridge, CLI, installer and addon in this repository.
- Anything that lets someone read data they should not, change your overlay, rules or guide files without a valid secret, write outside the intended folders, run code on your Mac, or send input to the game.

Out of scope: problems in the game itself, in macOS, in Cloudflare, in your AI tool or in Guidelime; attacks that need access to your Mac user account (they can already read your config file); and anything that needs you to post your secret links publicly.

## Threat model

### What we protect

- Your game data: the feed contains personal information such as your character, location, gear, friends list and guild message (see [docs/PRIVACY.md](docs/PRIVACY.md)).
- Control of your overlay, your rules, your AI's notes, and your Guidelime guide files.
- Your Mac: WoW Mentor should not give anyone a way to run code on it.

### Who we worry about

1. **Someone who learns a link** (from a screenshot, a shared prompt, an AI provider log, browser history, a tunnel provider).
2. **Someone on the network** who can reach the machine or the tunnel address.
3. **A malicious web page** in your browser trying to talk to the local server.
4. **Another player or the game world** putting text in the data an AI reads (prompt injection).
5. **A hostile or buggy AI agent** that has legitimate links.

### Design decisions and their consequences

**The secret is in the URL.** Every private endpoint lives under a long random path segment: `/<read-token>`, `/ui/<overlay-secret>/...`, `/push/<write-secret>`. We chose this because it works with tunnels and with AI tools that can only fetch web addresses and cannot set headers.

- Each secret is 24 random bytes (48 hex characters, 192 bits) from Python's `secrets` module. Guessing one is not practical.
- Secrets are compared with a constant-time comparison.
- The server does not log request paths. Responses send `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and `X-Robots-Tag: noindex, nofollow`.
- The downside is inherent: a URL ends up in places headers do not. It can appear in an AI conversation, in the logs of an AI provider or a tunnel provider, in browser history, in shell history, in screenshots. Anyone who sees the full URL has the access. This is why the privacy document says to treat links like passwords, and why `wowmentor rotate-secrets` exists (it replaces all three secrets, restarts the services and the old links stop working immediately).
- Writes through GET (`/ops?b=<base64>`) exist because some tunnels and proxies block POST bodies. This means a write is a plain link. It still needs the secret.
- A wrong secret gets the same 404 as any unknown address.

**The server binds to `127.0.0.1` only.** Other computers on your network cannot connect to it. Programs and users on your own Mac can, but they still need a secret, and the config file that holds the secrets is mode 600 (only your user can read it). The server does not check the `Host` header, so a DNS-rebinding attack from a web page could reach it; it still has to guess the secret, so this does not give it access. The only unauthenticated paths are `/` (a one-line message) and `/healthz`.

**What `wowmentor share on` exposes.** It runs Cloudflare's `cloudflared` quick tunnel to your local port. This puts the **whole server** on a public address. The protection is the secret path, nothing else: no password, no IP allow-list, and (unless you add one) no rate limit. Cloudflare terminates the encrypted connection, so it can see the traffic. With the read link anyone can read your game feed; with the overlay link anyone can also change your overlay and rules, read and change your AI's notes, and queue edits to your Guidelime guide files. The write secret is not printed in any link but it is accepted on the same public address. Leave sharing off unless you need it, and turn it off afterwards.

**The webhook secret.** If you set `wowmentor webhook set <url> --secret -` (the `-` reads the secret from standard input so it stays out of your shell history), each message is signed with HMAC-SHA256 of the raw body and sent in `X-WoWMentor-Signature: sha256=<hex>`. Your receiver must verify it with a constant-time comparison. The signature does not on its own stop a captured message from being replayed; a receiver should also check `sent_at` and ignore event numbers (`eid`) it has already seen. The secret lives in `config.json`, and `rotate-secrets` does not change it. Use an `https://` address you control. The URL is never printed in full or logged, redirects are not followed, and the bridge treats only a 2xx answer as delivered. See [docs/RULES.md](docs/RULES.md).

**No input injection.** WoW Mentor never sends keystrokes, mouse clicks or commands to the game, and does not use the Accessibility or Input Monitoring permissions. The bridge only reads a window picture. The addon uses ordinary (non-secure) frames and none of the protected functions, which the game would refuse to run from an addon anyway. The overlay cannot cast, target, move or use items.

**No code injection through the overlay.** The overlay reaches the game as a Lua file (`Inbox.lua`) in a slot addon folder. The bridge wraps the already-validated JSON in a Lua long string whose delimiter is chosen so the data cannot contain it, and the addon parses it with its own JSON reader. The server validates every field, caps sizes (60,000 bytes per document) and rejects unknown fields. So someone with the overlay link can change what is drawn, but not run Lua code in your game.

**Guide editing.** Edits go to files in addon folders named `Guidelime_*` that contain a guide registration, only by names the bridge discovered itself. Each edit is re-validated against the real file (balanced brackets, no `]]` or `[[` that could break the string), a backup is stored first, the write is atomic, and the player is asked to `/reload`. A leaked overlay link can still queue edits to those guides; the backups let you restore them (`wowui tug restore <guide> orig`). We do not guarantee that a validator catches every malicious change to a guide's text.

**A remote server (`wowmentor server-url`).** If you point the bridge at a server on another machine, the snapshot, events and the write secret travel to it over whatever transport you chose. The command accepts `http://`; use `https://` or a private network. The remote server must be protected by you.

**Reading other addons' data.** The bridge reads the saved-variable files of a few other addons (BagBrother, Questie, Guidelime) with a small Lua-table parser that does not execute code.

**Prompt injection.** The feed contains text written by other people (names, guild message, chat if you enabled it, item and quest names). An AI that treats that text as instructions could be manipulated into drawing misleading things, or into leaking your links. The skill page tells agents to treat the feed as data, but we cannot make an AI obey. Keep chat filtering on.

**Supply chain.** WoW Mentor has no third-party dependencies (Python standard library only). The installer is a shell script you pipe into `bash`, which runs whatever is in the repository's `main` branch at that moment. If that worries you, read `install.sh` first, or pin a release: `curl -fsSL https://raw.githubusercontent.com/PhilipAD/wow-mentor/<tag>/install.sh | WOWMENTOR_REF=<tag> bash`. The only automated dependency updates are for GitHub Actions in CI. The installer also compiles two tiny helper programs (a Swift one and a C one) from files in the repository using Apple's tools.

### Known limits

- Secrets are stored in plain text in `~/.config/wowmentor/config.json` (mode 600) and are included in backups of your home folder.
- The local server speaks plain HTTP on loopback.
- There is no per-IP rate limiting.
- A process running as your Mac user can read the config and use WoW Mentor like you can.
- We have not had an independent security review.


## Hardening details

* The server allows at most 64 simultaneous connections and times idle sockets out after 20 seconds.
* `config.json` (your secrets) is created with owner-only permissions (0600) and written atomically.
* A request may carry at most 100 overlay ops; request bodies are capped; JSON nesting is bounded.
* Plain `http://` for a remote `server_url` sends secrets unencrypted: `wowmentor server-url set` warns about it. Prefer https or a private network.
