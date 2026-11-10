# Design: the host layer

**What this is for:** everything WoW Mentor does that depends on the operating system lives behind `wowmentor/host/`. **Who it is for:** whoever writes the Windows host (`host/win32.py`) or another one. The contract is `wowmentor/host/base.py`; its docstrings are the specification.

## Shape

`host.get()` returns a `Host` with `name`, `locations`, `capture`, `mask`, `services`, `shell`, `fs` and `text`. It is chosen by `sys.platform` (`darwin`, `win32` when `host/win32.py` exists, otherwise `unsupported`) and can be forced with `WOWMENTOR_HOST=fake|darwin|win32|unsupported`. Tests use `with host.using(FakeHost(...)):`.

| Module | Role |
| --- | --- |
| `host/base.py` | the abstract interfaces, `HostUnsupported`, `TEXT_DEFAULTS`, `KINDS` |
| `host/darwin.py` | CoreGraphics capture, the Swift mask process, launchd, pbcopy/open |
| `host/unsupported.py` | Linux and others: queries answer "no", actions raise `HostUnsupported` naming what works |
| `host/fake.py` | in-memory host that records every call (tests, `WOWMENTOR_HOST=fake`) |
| `host/common.py` | `replace_with_retry`, `write_private_file`, `scan_wow_roots`, `ensure_utf8_stdio`, `CommonFs` |

OS independent and shared: `bridge/strip.py` (the `Frame` class and the strip decoder), `install.py` (addon and slot files), the server, the addon, `wowui`.

## Rules for a host

1. **Importing a host module never touches a native binding.** No `ctypes.windll`, `CDLL`, `winreg` or `WinDLL` at import time; bind inside methods on first use. Every host module is imported and unit-tested on every OS (`tests/test_host_api.py`, `tests/test_hardening.py::ImportSafetyTests`).
2. **Queries never raise, actions raise `HostUnsupported`.** Queries: `capture.available`, `capture.foreground_is_game`, `mask.available`, `services.is_installed`, `services.log_path`, `locations.*`, `shell.*`. The CLI prints the message of a `HostUnsupported` and exits 1.
3. **No input is ever sent to the game.** Capture is read only.
4. **Wording that names the OS comes from `host.text`** (`this_machine`, `example_wow_path`, `permission_lines`, `capture_fix`, ...). Keys and defaults are in `TEXT_DEFAULTS`.
5. **Every text file is opened with `encoding="utf-8"`** (and `newline="\n"` for generated files). `cli.main` calls `ensure_utf8_stdio()`, so output never raises `UnicodeEncodeError`.

## What each part must do

- **locations**: `config_home()` is the default config folder (`$WOWMENTOR_HOME` is applied by `config.home()`); `log_dir()` the default log folder (`$WOWMENTOR_LOGS` is applied by `config.log_dir()`); `find_wow_dirs(search_roots=None)` returns client folders containing `Interface/AddOns`, Forever first (`common.scan_wow_roots` does the scan; a host supplies the default roots, registry hits and the running game's path); `extra_bin_dirs()` for tools such as cloudflared.
- **capture**: `available() -> (ok, reason)`; `find_game()` returns an opaque truthy handle or `None`; `grab_top(handle, rows)` returns a `bridge.strip.Frame(raw, width, height, stride)` of BGRA bytes, top row first, lossless, 10 times a second, **without resampling** (the strip's cells are 3x2 device pixels, so a Windows process must be per-monitor DPI aware); `game_running()`; `foreground_is_game(handle=None)` returns `True`/`False`/`None`.
- **mask**: `available()`, `start(cfg)`, `update(strip_rect, capture_width, row_provider)`, `stop()`. `strip_rect` is `(x, y, w, h)` in pixels of the captured window; `row_provider()` returns the screen row just under the strip as base64 RGB (every 4th pixel) or `None`. The overlay paints that row over the strip and falls back to black; it must be invisible to `grab_top`. The macOS implementation drives `wowmask` over stdin (`"x y w h capW"` and `"R <base64>"` lines); that protocol is unchanged.
- **services**: `install(kind, cfg, helpers=None)`, `start`, `stop`, `is_running`, `is_installed`, `remove`, `build_helpers(cfg)`, `remove_helpers()`, `log_path(kind)` for kinds `server`, `bridge`, `tunnel`. Services start at login, restart when they die, need no admin rights, and must not be able to touch the services of another `$WOWMENTOR_HOME` (macOS suffixes the launchd label with a hash). `build_helpers` returns `{"launcher": path|None, "mask": path|None, "summary": str}`; a host with nothing to build returns `{"summary": ""}`.
- **shell**: `copy_to_clipboard(text) -> bool`, `open_url(url)`, `open_text_file(path)`, `open_privacy_settings()` (may do nothing).
- **fs**: `atomic_replace(src, dst)` retries briefly on sharing violations (`winerror` 5 and 32); `write_private(path, data, mode=0o600)` writes atomically and, on POSIX, readable by the owner only.

## Testing a new host

Write the pure logic (path building, argument quoting, pid-file parsing, struct sizes) so it runs on any OS with injected fakes, and add the module to `tests/test_host_api.py::EveryHostModuleTests`. Anything that needs a real Windows desktop goes in a separate, skippable test module. The macOS plist, launcher and mask-protocol tests (`tests/test_host_darwin.py`) show the pattern.
