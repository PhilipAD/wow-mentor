# Contributing to WoW Mentor

**What this is for:** how to set up, change, test and submit a change. **Who it is for:** developers. Players who just want to use WoW Mentor should read the [README](README.md).

Thank you for helping. Small fixes, bug reports with a good `wowmentor doctor` output, documentation improvements and Forever API findings (see [docs/FOREVER-API.md](docs/FOREVER-API.md)) are all welcome.

## Ground rules

1. **Display only.** Nothing in this project sends keys, mouse input or commands to the game, plays for the player, equips or previews items, or works around a restriction Blizzard put in place. Pull requests that do are declined.
2. **No third-party dependencies.** Python uses the standard library only. The addon is plain Lua with no libraries. (The only automated dependency updates are for GitHub Actions.)
3. **No personal data.** No real names, character names, hosts, tokens or links in code, tests, docs, screenshots or issues. Use neutral examples such as `Examplechar` and `ExampleRealm`.
4. **Level and XP are never shown on the overlay.** They may appear in feed data for the AI.
5. **Degrade, never raise.** Every optional WoW API is called through `pcall` or a feature check. An API that is missing or secret on the Forever client becomes `unknown`.
6. **Say what is unverified.** Do not document a feature as working if you have not run it. Mark plans and guesses as such.

## Dev setup

You need Python 3.9 or newer and, for the addon tests, Lua 5.1 or LuaJIT.

Mac:

```bash
git clone https://github.com/PhilipAD/wow-mentor.git
cd wow-mentor
./wowmentor.sh --help     # the CLI, straight from the clone (nothing is installed)
./wowui --help            # the agent CLI
```

Windows (PowerShell):

```powershell
git clone https://github.com/PhilipAD/wow-mentor.git
cd wow-mentor
.\wowmentor.cmd --help    # the CLI, straight from the clone (nothing is installed)
.\wowui.cmd --help        # the agent CLI
```

No `pip install` is needed. `./wowmentor.sh`, `./wowui` and the `.cmd` files add the folder to the Python path themselves. `pip install .` (or `python -m build`) also works and gives you `wowmentor` and `wowui` commands, `python -m wowmentor`, and the addon files as package data.

**Never run `wowmentor setup` while developing unless you mean to install on your own computer.** It writes into your WoW folder and installs background services (launchd on a Mac, a login entry plus a supervisor on Windows). On a Mac it also asks for the screen recording permission. Tests and experiments should use a temporary home.

Mac:

```bash
export WOWMENTOR_HOME="$(mktemp -d)/cfg"       # config, secrets and data
export WOWMENTOR_LOGS="$(mktemp -d)"           # logs
```

Windows (PowerShell):

```powershell
$env:WOWMENTOR_HOME = Join-Path ([IO.Path]::GetTempPath()) ("wm-" + [guid]::NewGuid()) # config, secrets and data
$env:WOWMENTOR_LOGS = Join-Path ([IO.Path]::GetTempPath()) ("wml-" + [guid]::NewGuid()) # logs
```

A temporary `WOWMENTOR_HOME` does **not** isolate the background services on a Mac: the launchd names (`com.wowmentor.*`) are the same everywhere, so `wowmentor start`, `stop`, `restart`, `rotate-secrets` and `uninstall` would act on a real installation on the same Mac. (On Windows the login entry gets its own name under a custom `WOWMENTOR_HOME`, so a test cannot touch the real one.) In a sandbox, run the server and the bridge as plain processes (`./wowmentor.sh server`, `./wowmentor.sh bridge` on a Mac, `.\wowmentor.cmd server`, `.\wowmentor.cmd bridge` on Windows, with a free port in `config.json`), and give `setup` the flags `--no-services --no-build`, a fake `--wow-dir` and a temporary home folder (`HOME` on a Mac, `USERPROFILE` on Windows). (`--no-launchd` still works as the old name of `--no-services`.) Also check that the port you chose is really free before you start the bridge: it posts to whatever answers there.

You can run the server by itself against a temporary home without installing anything:

```bash
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from wowmentor import config as c
from wowmentor.server import app
cfg = c.load(); c.ensure_secrets(cfg); c.save(cfg)
print(c.ui_base(cfg))                          # the private overlay address
app.serve(cfg)                                 # Ctrl-C to stop
EOF
```

(Port 8095 is the default; set `cfg["port"]` first if something else uses it.) Then point `wowui` at it: `./wowui --url <the address printed> state` on a Mac, `.\wowui.cmd --url <the address printed> state` on Windows.

## Running the tests

Everything:

```bash
./scripts/test.sh
```

`scripts/test_install.sh` tests `install.sh` and the double-click installer against a local copy of the repository and a temporary home folder (see the comments at the top of `install.sh` for `WOWMENTOR_SRC` and `WOWMENTOR_TARBALL_URL`). It is part of `test.sh`.

`scripts/test_install.ps1` does the same for `install.ps1` on Windows (it also checks that the installer is plain ASCII and parses). It needs a real Windows machine; CI runs it on `windows-latest`. Run it with `powershell -NoProfile -File scripts\test_install.ps1`.

Or each part:

```bash
# Python (the Mac-only and Windows-only parts are skipped on the other system)
python3 -W error::ResourceWarning -m unittest discover -s tests -t . -v      # Mac and Linux
py -3 -W error::ResourceWarning -m unittest discover -s tests -t . -v        # Windows

# Lua (the game runs Lua 5.1; LuaJIT is close and fine for a quick local check)
python3 tests/make_sample.py          # writes tests/fixtures/sample_doc*.json for the Lua tests
luajit tests/test_json.lua            # or: lua5.1 tests/test_json.lua
luajit tests/test_ui.lua
luajit tests/test_coach.lua
```

The Python tests are all you need on Windows. For the Lua tests, install a Lua with `brew install luajit` on a Mac. On Debian or Ubuntu: `sudo apt-get install lua5.1 luajit`. CI uses real Lua 5.1 on Ubuntu, so if something passes on LuaJIT but not on 5.1, 5.1 wins.

`tests/make_sample.py` rewrites the fixture files with a new timestamp; commit them only when the overlay schema changed.

The same checks run in GitHub Actions on every push and pull request: Python 3.9 and 3.12 on Ubuntu, macOS and Windows (the Windows job also runs the Windows-only tests and `scripts/test_install.ps1` in Windows PowerShell 5.1 and PowerShell 7), the Lua tests plus a `luac5.1 -p` syntax check of every addon file, ShellCheck on the installer scripts, the installer test on macOS (`scripts/test_install.sh`), a build-and-install check of the package, and a documentation check. Pushing a tag such as `v1.0.0` runs the release workflow, which builds the source archive, sdist and wheel and attaches them to a GitHub release (the tag must match `__version__` in `wowmentor/__init__.py` and `CHANGELOG.md` needs an entry). You can run the documentation check yourself:

```bash
python3 .github/scripts/check_docs.py
```

It verifies that links between Markdown files resolve, that every `wowmentor ...` and `wowui ...` command written in the docs exists, and that JSON examples parse and, when they contain overlay operations, pass the real server schema.

## Code style

**Python**

* Standard library only. Python 3.9 compatible: no `match` statements, no `X | Y` type unions, no `dict | dict`, no parenthesised multi-line `with`.
* Keep functions small and testable. Pure logic (presence, validation, signing) takes the clock or inputs as arguments so tests do not need real time.
* Never log a secret or a request path (paths contain secrets). Never print a webhook URL in full.
* Close what you open: tests run with `-W error::ResourceWarning`.
* Plain comments that explain *why*. If you state a fact about the game in a comment, make sure it is true (see the sources in `docs/FOREVER-API.md`).

**Lua (the addon)**

* Lua 5.1 only: no `goto`, no `//`, no bitwise operators, no integer-only assumptions. Use `unpack`, not `table.unpack` (the tests have a shim).
* Every optional WoW API goes through `pcall` or a feature check. Use `issecretvalue` before comparing values that can be secret.
* Register events through `pcall(frame.RegisterEvent, frame, name)`: a client that lacks the event raises.
* Never touch protected functions or secure frames, and never open Blizzard windows from addon code.
* Tabs for indentation, as in the existing files.

**Documentation**

* Start every document with what it is for and who it is for. Friendly, plain language, short sentences. No emojis in headings. No marketing words.
* Every command you document must have been run. Prefer copy-paste blocks and say what the reader will see.
* Do not invent features. If something is planned or unverified, say so.

## File layout

```text
install.sh                       one-line installer (downloads the repo, then runs setup)
Install WoW Mentor.command       double-click installer for people who downloaded the ZIP
install.ps1                      one-line Windows installer (PowerShell 5.1 and 7, ASCII only, no admin)
Install-WoWMentor.cmd            double-click installer for Windows people who downloaded the ZIP
wowmentor.cmd, wowui.cmd         run the commands from an unzipped folder on Windows
wowmentor.sh                     the `wowmentor` command (a Python script) run from a clone
wowui                            the `wowui` agent command run from a clone
pyproject.toml                   package metadata
scripts/test.sh                  runs every test
tests/                           Python and Lua tests, fixtures and the WoW API stub (wowstub.lua)

wowmentor/
  cli.py                         wowmentor command: setup, doctor, links, share, webhook, server-url, ...
  config.py                      config file, secrets, links, profile template
  doctor.py                      the checks behind `wowmentor doctor`
  install.py                     addon and slot files (the same on every system)
  install_mac.py                 Mac-only parts of setup (helpers, launchd)
  host/                          everything that depends on the operating system, one module per system:
    base.py                      the interfaces every host fills in (locations, capture, mask, services, shell, fs, text)
    darwin.py                    macOS: CoreGraphics capture, the Swift mask, launchd
    win32.py                     Windows: window capture, the mask, login entry and supervisor, icacls
    win32_capture.py, win32_mask.py, win32_supervisor.py   the Windows pieces behind win32.py
    common.py, fake.py, unsupported.py   shared helpers, the test host, and the answer on other systems
  server/
    app.py                       the local HTTP server and routing
    uidoc.py                     the overlay document model, validation, rules schema, the agent skill text
    events.py                    event store, NPC cache, coach questions, session object
    agentmem.py                  the AI's notes and the `brief` page
    tugapi.py, tugdoc.py         guide editing: API and the guide file model
  bridge/
    app.py                       screen capture, strip decoder, feed assembly, push loop
    presence.py                  in_world / loading / afk / logged_out / game_closed
    events.py                    receives events from the strip and sends them to the server
    webhook.py                   batching, signing and retrying the outgoing webhook
    uisync.py                    delivers overlay updates into the game (slots) and reads ticks back
    guidesync.py                 applies guide edits to Guidelime files with backups
    luasv.py                     read-only parser for other addons' saved variables
    mask/wowmask.swift           the helper that hides the strip on your screen
  agent/wowui.py                 the `wowui` command for AI agents
  addon/WoWFeed/                 the addon that runs in the game
    WoWFeed.toc                  load order and interface number (16001)
    Codec.lua                    the pixel strip codec
    JSON.lua                     small JSON reader and writer
    WoWFeed.lua                  data collectors and the strip transport
    UI.lua                       the overlay renderer and slot loader
    Coach.lua                    event capture, fight and death reconstruction, NPC cache
    CoachState.lua               the extra feed sections
    CoachRules.lua               the rules engine, auto-complete, coach inbox, waypoint HUD
docs/                            user and developer documentation
.github/                         CI, issue and pull request templates, Dependabot
```

How the pieces fit together: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md). The contract between addon, bridge and server for events, presence, rules and the webhook: [docs/DESIGN-coach-events.md](docs/DESIGN-coach-events.md).

## Submitting a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Make your change on a branch. Keep it focused.
3. Add or update tests. A new overlay field needs a case in `tests/test_uidoc.py`; a new server route needs one in `tests/test_server.py`; addon behaviour goes in the Lua tests with the stub in `tests/wowstub.lua`.
4. Run the tests and the docs check (above) on macOS or Windows, whichever you have. State in the pull request which system you used, what you ran and what you could not.
5. Update the docs and [CHANGELOG.md](CHANGELOG.md) when behaviour changes.
6. Fill in the pull request template.

A change that touches how data leaves the machine (new network calls, new things in the feed, new logging) must also update [docs/PRIVACY.md](docs/PRIVACY.md) and [SECURITY.md](SECURITY.md).

Security problems: do not open a public issue. See [SECURITY.md](SECURITY.md).

## Trying it in the real game

Only on a Mac or a Windows PC with the Forever client (on Windows the game must be Windowed or Windowed (Fullscreen)). Install into a copy of your WoW folder if you can (`wowmentor setup --wow-dir <path>`), watch `wowmentor doctor`, and remember that the game only discovers the slot folders after a full restart. Please report the exact client build number (the `SYSTEM` section of the feed shows it) with any findings; the Forever client is still changing.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE). Third-party credits are in [NOTICE](NOTICE).
