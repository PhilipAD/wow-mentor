"""Host for systems without a native implementation (Linux, BSD, ...): the server and the CLI work, capture/mask/services do not."""
import os

from . import base
from .common import CommonFs, scan_wow_roots

SUPPORTED = ("WoW Mentor's bridge (the part that reads the game window) runs on macOS today; the server, `wowui` and most CLI commands "
             "work on any system with Python 3.9+. Run `wowmentor server` on this machine and the bridge on your Mac with server_url set.")


def _no(what):
    return base.HostUnsupported("%s is not available on this system. %s" % (what, SUPPORTED))


class UnsupportedLocations(base.Locations):
    def config_home(self):
        return os.path.abspath(os.path.expanduser("~/.config/wowmentor"))

    def log_dir(self):
        from .. import config
        return config.path("logs")

    def find_wow_dirs(self, search_roots=None):
        return scan_wow_roots(search_roots or [])


class UnsupportedCapture(base.Capture):
    def available(self):
        return False, SUPPORTED

    def find_game(self):
        raise _no("Reading the game window")

    def grab_top(self, handle, rows):
        raise _no("Reading the game window")

    def game_running(self):
        raise _no("Detecting the game")


class UnsupportedMask(base.Mask):
    def available(self):
        return False

    def start(self, cfg):
        raise _no("The screen mask")

    def update(self, strip_rect, capture_width, row_provider=None):
        raise _no("The screen mask")

    def stop(self):
        raise _no("The screen mask")


class UnsupportedServices(base.Services):
    def install(self, kind, cfg, helpers=None):
        raise _no("Background services")

    def start(self, kind):
        raise _no("Starting services")

    def stop(self, kind):
        raise _no("Stopping services")

    def is_running(self, kind):
        raise _no("Checking services")

    def is_installed(self, kind):
        return False

    def remove(self, kind):
        raise _no("Removing services")

    def build_helpers(self, cfg):
        raise _no("Building helpers")


class UnsupportedShell(base.Shell):
    def copy_to_clipboard(self, text):
        return False

    def open_url(self, url):
        pass

    def open_privacy_settings(self):
        pass


def make():
    return base.Host("unsupported", UnsupportedLocations(), UnsupportedCapture(), UnsupportedMask(), UnsupportedServices(),
                     UnsupportedShell(), CommonFs(), {"not_supported_title": "This system is not supported yet"})
