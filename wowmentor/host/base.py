"""The host layer: every operating-system specific thing WoW Mentor does, behind six small interfaces.

A `Host` bundles:

    host.name        short id: "darwin", "win32", "unsupported" or "fake"
    host.locations   where config, logs and the WoW client live
    host.capture     find the game window and read pixels from it (the bridge's eyes)
    host.mask        the on-screen overlay that hides the data strip from the player
    host.services    background processes: the server, the bridge and the optional tunnel
    host.shell       clipboard, "open this", OS settings pages
    host.fs          atomic replace and private files
    host.text        user-facing wording that names the OS (a dict, keys in TEXT_DEFAULTS)

Rules every implementation follows (so a module can be imported and unit-tested on any OS):
  * Importing a host module never touches ctypes.windll, ctypes.CDLL, winreg or any other native binding. Bind lazily inside methods.
  * "Query" methods (`available`, `foreground_is_game`, `is_running`, `is_installed`, `log_path`, `locations.*`) never raise for an
    unsupported system; they answer False/None. "Action" methods (`start`, `install`, `grab_top`, ...) raise `HostUnsupported`
    when the system cannot do them, with a message that says what IS supported.
  * Nothing here sends input to the game. Capture is read-only.

Pure strip decoding (`Frame`, `find_hint`, `decode`) is OS independent and lives in wowmentor.bridge.strip. `Capture.grab_top`
returns a `wowmentor.bridge.strip.Frame(raw, width, height, stride)`: `raw` is BGRA (or BGRx) bytes, top row first, `stride` bytes per row;
the alpha byte is never read, so an undefined alpha (Windows GDI) is fine.
"""
import abc, os

KINDS = ("server", "bridge", "tunnel")      # the three background services

# Default wording. A host passes only the keys it changes. Every value is a plain str (no format placeholders unless named).
TEXT_DEFAULTS = {
    "os_name": "this system",                    # "macOS"
    "this_machine": "this computer",             # "this Mac": used in "links only work on <this_machine>"
    "terminal": "a terminal",                    # "Terminal"
    "example_wow_path": "/path/to/World of Warcraft/_forever_",     # an example --wow-dir value
    "python_help": "Install Python 3.9 or newer and run again.",      # shown by `setup` when Python is too old
    "python_fix": "Install Python 3.9 or newer.",                     # doctor fix for the Python row
    "privacy_settings": "",                      # where the capture permission lives; "" = none needed
    "permission_lines": (),                      # `setup` step 5: lines to print; "%(app)s" is the helper app's name
    "capture_failed_hint": "is the game window visible?",             # log text after "could not capture the game window (...)"
    "capture_fix": "Check that WoW is visible, then: wowmentor restart",   # doctor fix when capture fails; may use %(app)s
    "package_manager": "",                       # "brew": used to offer installing cloudflared; "" = just print the hint
    "cloudflared_hint": "Install cloudflared first (see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)",
    "not_supported_title": "This system is not supported yet",
}


class HostUnsupported(RuntimeError):
    """Raised by an action the current host cannot perform. The message says what is supported."""


class Locations(abc.ABC):
    @abc.abstractmethod
    def config_home(self):
        """Default folder for config.json, profile.md, data/ and bin/ (absolute). `$WOWMENTOR_HOME` is applied by
        wowmentor.config.home(), not here."""

    @abc.abstractmethod
    def log_dir(self):
        """Folder for the service logs (absolute; created by the caller). `$WOWMENTOR_LOGS` is applied by wowmentor.config.log_dir().
        Implementations that put logs under the config folder must use wowmentor.config.home(), so a custom $WOWMENTOR_HOME is honoured."""

    @abc.abstractmethod
    def find_wow_dirs(self, search_roots=None):
        """Client folders (like ".../World of Warcraft/_forever_") that contain Interface/AddOns, Forever first, then sorted.
        `search_roots` are folders that CONTAIN client folders; None = this host's usual install locations. Never raises."""

    def addons_dir(self, wow_dir):
        return os.path.join(wow_dir, "Interface", "AddOns")

    def extra_bin_dirs(self):
        """Extra folders searched for command line tools such as cloudflared (after $PATH). Default: none."""
        return []


class Capture(abc.ABC):
    """Read-only access to the game window's pixels."""

    @abc.abstractmethod
    def available(self):
        """(ok, reason). ok False means the bridge cannot run here; `reason` is one user-facing sentence saying why and what is supported.
        True does not mean the capture permission is granted (that is only known by trying `grab_top`). Never raises."""

    @abc.abstractmethod
    def find_game(self):
        """An opaque, truthy handle for the biggest visible WoW window, or None when the game is not running. The handle is only
        ever passed back to this object's `grab_top`/`foreground_is_game`. Called about once every 15 s by the bridge (and once a
        second by presence), so it may do a cheap window enumeration."""

    @abc.abstractmethod
    def grab_top(self, handle, rows):
        """Frame of the top `rows` rows of the game's client area (full width), or None if the capture failed (permission missing,
        window gone). Must be lossless (no scaling or compression) and fast (the bridge calls it 10 times a second); the strip's
        cells are 3x2 device pixels, so DPI scaling must not resample."""

    @abc.abstractmethod
    def game_running(self):
        """True if a WoW game process exists (the window may be hidden or not yet created)."""

    def foreground_is_game(self, handle=None):
        """True if the front-most normal window is WoW, False if another app's is, None if unknown. Never raises, needs no permission."""
        return None


class Mask(abc.ABC):
    """An opaque overlay over the data strip, so the player does not see it. Window capture must not see the overlay (macOS:
    capture of a specific window ignores other windows; Windows: the overlay is excluded from capture). Optional: `available()` may be False."""

    @abc.abstractmethod
    def available(self):
        """True if the overlay can be shown right now (helper built/present, OS new enough). Cheap: called 10 times a second. Never raises."""

    @abc.abstractmethod
    def start(self, cfg):
        """Configure for this bridge run. `cfg` is the bridge's CFG dict (macOS reads `mask_path`, the compiled helper). Must not
        show anything yet and must not raise when the mask is unavailable."""

    @abc.abstractmethod
    def update(self, strip_rect, capture_width, row_provider=None):
        """Show/move the overlay (called each decoded frame, ~10 Hz; idempotent). `strip_rect` = (x, y, w, h) in pixels of the
        captured window's top-left origin, `capture_width` = pixel width of the captured frame (w and capture_width let the
        host scale to screen units). `row_provider` is None or a callable returning the screen row just below the strip as bytes
        (base64 of RGB, every 4th pixel) or None; the host may call it at most when it wants a refresh (it is not free). The
        overlay should paint that row stretched over the strip, falling back to black. Must swallow its own errors."""

    @abc.abstractmethod
    def stop(self):
        """Remove the overlay and release the helper process/thread. Idempotent."""


class Services(abc.ABC):
    """The three background services, started at login and restarted if they die. `kind` is one of KINDS."""

    @abc.abstractmethod
    def install(self, kind, cfg, helpers=None):
        """Register `kind` to start at login (idempotent, no admin rights). `cfg` is the wowmentor config dict; `helpers` is the
        dict returned by `build_helpers` (or None when helpers were not built). Returns a short description (path) of what was
        written. Does not start it."""

    @abc.abstractmethod
    def start(self, kind):
        """(Re)start `kind` now. True on success."""

    @abc.abstractmethod
    def stop(self, kind):
        """Stop `kind` now (it starts again at the next login). True if something was stopped."""

    @abc.abstractmethod
    def is_running(self, kind):
        """True if `kind` is running right now."""

    @abc.abstractmethod
    def is_installed(self, kind):
        """True if `kind` is registered to start at login."""

    @abc.abstractmethod
    def remove(self, kind):
        """Unregister `kind` (stop it first). Idempotent."""

    @abc.abstractmethod
    def build_helpers(self, cfg):
        """Build/prepare native helpers. Returns {"launcher": path|None, "mask": path|None, "summary": str} where `summary` is one
        short line for `setup` ("launcher ok, screen overlay ok"). macOS compiles the mask and a launcher app; Windows has
        nothing to build and returns {"summary": ""}. Never raises for a missing compiler."""

    def remove_helpers(self):
        """Delete what build_helpers made outside the config folder. Idempotent. Default: nothing."""

    def log_path(self, kind):
        from .. import config
        return os.path.join(config.log_dir(), kind + ".log")


class Shell(abc.ABC):
    @abc.abstractmethod
    def copy_to_clipboard(self, text):
        """Put `text` on the clipboard. True on success, False if there is no clipboard tool. Never raises."""

    @abc.abstractmethod
    def open_url(self, url):
        """Open `url` (or a file) with the default application. Never raises."""

    def open_text_file(self, path):
        """Open a text file for editing. Default: open_url(path)."""
        self.open_url(path)

    @abc.abstractmethod
    def open_privacy_settings(self):
        """Open the OS page where the capture permission is granted (macOS: Screen Recording). May be a no-op."""


class Fs(abc.ABC):
    @abc.abstractmethod
    def atomic_replace(self, src, dst):
        """Rename `src` over `dst`, atomically where the OS allows. Retries briefly when the destination is held open by another
        process (Windows sharing violations, winerror 5/32). Raises OSError after the retries."""

    @abc.abstractmethod
    def write_private(self, path, data, mode=0o600):
        """Write bytes `data` to `path` atomically (temp file in the same folder, fsync, atomic_replace), readable only by this user
        on POSIX (the temp file is created with `mode`, so there is no moment it is world readable). On Windows rely on the user
        profile's ACL. Raises OSError on failure, leaving any existing file intact and no temp file behind."""


class Host(object):
    """Plain container; see the module docstring. `text` is TEXT_DEFAULTS updated with the host's own wording."""

    def __init__(self, name, locations, capture, mask, services, shell, fs, text=None):
        self.name = name
        self.locations, self.capture, self.mask = locations, capture, mask
        self.services, self.shell, self.fs = services, shell, fs
        self.text = dict(TEXT_DEFAULTS, **(text or {}))

    def __repr__(self):
        return "<Host %s>" % self.name
