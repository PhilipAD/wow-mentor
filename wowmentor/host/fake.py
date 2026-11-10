"""An in-memory host for tests (and WOWMENTOR_HOST=fake): nothing touches the OS, every call is recorded.

    fake = FakeHost(name="win32", wow_dirs=[...])
    with host.using(fake):
        cli.main(["setup", "--wow-dir", wow, "--no-build"])
    assert fake.services.calls == [("install", "server"), ...]

File operations (`fs`) are real: tests work in temp folders.
"""
from . import base
from .common import CommonFs, scan_wow_roots


class FakeLocations(base.Locations):
    def __init__(self, config_home="/fake/config", log_dir="/fake/logs", wow_dirs=()):
        self._config_home, self._log_dir, self.wow_dirs = config_home, log_dir, list(wow_dirs)

    def config_home(self):
        return self._config_home

    def log_dir(self):
        return self._log_dir

    def find_wow_dirs(self, search_roots=None):
        if search_roots:
            return scan_wow_roots(search_roots)
        return sorted(self.wow_dirs, key=lambda p: (0 if "forever" in p.lower() else 1, p))


class FakeCapture(base.Capture):
    """Scripted capture. `game` is the handle find_game returns (None = game not running); `frames` are returned by grab_top in order
    (the last one repeats; an empty list means every grab fails)."""

    def __init__(self, ok=True, reason="", game="fake-window", frames=(), running=None, foreground=None):
        self.ok, self.reason, self.game, self.frames = ok, reason or ("" if ok else "fake capture is unavailable"), game, list(frames)
        self.running = (game is not None) if running is None else running
        self.foreground = foreground
        self.calls = []

    def available(self):
        return self.ok, self.reason

    def find_game(self):
        self.calls.append(("find_game",))
        return self.game

    def grab_top(self, handle, rows):
        self.calls.append(("grab_top", handle, rows))
        if not self.frames:
            return None
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def game_running(self):
        return self.running

    def foreground_is_game(self, handle=None):
        return self.foreground


class FakeMask(base.Mask):
    def __init__(self, ok=True):
        self.ok, self.calls, self.updates, self.started = ok, [], [], False

    def available(self):
        return self.ok

    def start(self, cfg):
        self.started = True
        self.calls.append(("start",))

    def update(self, strip_rect, capture_width, row_provider=None):
        self.calls.append(("update", tuple(strip_rect), capture_width))
        self.updates.append((tuple(strip_rect), capture_width, row_provider() if row_provider else None))

    def stop(self):
        self.calls.append(("stop",))


class FakeServices(base.Services):
    def __init__(self, helpers=None):
        self.calls, self.installed, self.running, self.start_ok = [], set(), set(), True
        self.helpers = {"launcher": None, "mask": None, "summary": ""} if helpers is None else helpers
        self.helpers_removed = False

    def install(self, kind, cfg, helpers=None):
        self.calls.append(("install", kind))
        self.installed.add(kind)
        return "fake:%s" % kind

    def start(self, kind):
        self.calls.append(("start", kind))
        if self.start_ok:
            self.running.add(kind)
        return self.start_ok

    def stop(self, kind):
        self.calls.append(("stop", kind))
        was = kind in self.running
        self.running.discard(kind)
        return was

    def is_running(self, kind):
        return kind in self.running

    def is_installed(self, kind):
        return kind in self.installed

    def remove(self, kind):
        self.calls.append(("remove", kind))
        self.running.discard(kind)
        self.installed.discard(kind)

    def build_helpers(self, cfg):
        self.calls.append(("build_helpers",))
        return dict(self.helpers)

    def remove_helpers(self):
        self.helpers_removed = True


class FakeShell(base.Shell):
    def __init__(self, clipboard_ok=True):
        self.clipboard_ok, self.clipboard, self.opened, self.privacy_opened = clipboard_ok, None, [], 0

    def copy_to_clipboard(self, text):
        if self.clipboard_ok:
            self.clipboard = text
        return self.clipboard_ok

    def open_url(self, url):
        self.opened.append(url)

    def open_privacy_settings(self):
        self.privacy_opened += 1


class FakeHost(base.Host):
    def __init__(self, name="fake", wow_dirs=(), text=None, capture=None, mask=None, services=None, shell=None, locations=None):
        self.wow_dirs = list(wow_dirs)
        super().__init__(name, locations or FakeLocations(wow_dirs=self.wow_dirs), capture or FakeCapture(), mask or FakeMask(),
                         services or FakeServices(), shell or FakeShell(), CommonFs(), text)


def make():
    return FakeHost()
