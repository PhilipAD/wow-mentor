"""The macOS host: CoreGraphics window capture, the Swift screen mask, launchd services, pbcopy/open.

Everything here is imported on every OS (the tests do that); the native libraries are only loaded inside methods, so importing this
module never touches ctypes.CDLL.

Files written outside the config folder:
  ~/Applications/WoW Mentor Bridge.app           a tiny launcher so macOS lists it under Screen Recording
  ~/Library/LaunchAgents/com.wowmentor.*.plist   start the server and bridge at login
"""
import ctypes, os, plistlib, subprocess, sys, time
from ctypes import c_double, c_uint32, c_void_p, c_size_t, c_long, c_int, c_int64, c_char_p, c_ubyte, Structure, POINTER, byref

from . import base
from .common import CommonFs, scan_wow_roots
from .. import config as cfgmod
from ..install import PKG, PKG_ROOT

LABELS = {"server": "com.wowmentor.server", "bridge": "com.wowmentor.bridge", "tunnel": "com.wowmentor.tunnel"}
APP_NAME = "WoW Mentor Bridge.app"
WOW_OWNERS = ("Wow", "World of Warcraft")


def label(kind):
    """The launchd label. With a custom $WOWMENTOR_HOME (tests, a second install) it gets a short suffix, so it can never
    stop, replace or remove the services of the real install."""
    base_label = LABELS[kind]
    if os.environ.get("WOWMENTOR_HOME"):
        import hashlib
        return "%s.%s" % (base_label, hashlib.sha1(cfgmod.home().encode()).hexdigest()[:6])
    return base_label


# ---------------------------------------------------------------- locations
class DarwinLocations(base.Locations):
    def config_home(self):
        return os.path.abspath(os.path.expanduser("~/.config/wowmentor"))

    def log_dir(self):
        return os.path.expanduser("~/Library/Logs/WoWMentor")

    def find_wow_dirs(self, search_roots=None):
        roots = search_roots or ["/Applications/World of Warcraft", os.path.expanduser("~/Applications/World of Warcraft"), "/Applications/Blizzard/World of Warcraft"]
        return scan_wow_roots(roots)

    def extra_bin_dirs(self):
        return ["/opt/homebrew/bin", "/usr/local/bin"]


# ---------------------------------------------------------------- capture: CGWindowListCreateImage via ctypes
# Lossless, ~30 ms, reads the game window's own backing store so overlays, other windows and other Spaces do not matter.
# Needs Screen Recording permission.
class _Pt(Structure): _fields_ = [("x", c_double), ("y", c_double)]
class _Sz(Structure): _fields_ = [("w", c_double), ("h", c_double)]
class _Rect(Structure): _fields_ = [("origin", _Pt), ("size", _Sz)]

_UTF8 = 0x08000100
_native = None


class _Native(object):
    """The loaded CoreGraphics/CoreFoundation libraries with their signatures. Built on first use only."""

    def __init__(self):
        self.cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg, cf = self.cg, self.cf

        def sig(lib, name, res, args):
            f = getattr(lib, name); f.restype = res; f.argtypes = args
        sig(cg, "CGWindowListCreateImage", c_void_p, [_Rect, c_uint32, c_uint32, c_uint32])
        sig(cg, "CGWindowListCopyWindowInfo", c_void_p, [c_uint32, c_uint32])
        sig(cg, "CGImageGetWidth", c_size_t, [c_void_p]); sig(cg, "CGImageGetHeight", c_size_t, [c_void_p])
        sig(cg, "CGImageGetBytesPerRow", c_size_t, [c_void_p]); sig(cg, "CGImageGetDataProvider", c_void_p, [c_void_p])
        sig(cg, "CGDataProviderCopyData", c_void_p, [c_void_p])
        sig(cg, "CGRectMakeWithDictionaryRepresentation", c_ubyte, [c_void_p, POINTER(_Rect)])
        sig(cf, "CFDataGetLength", c_long, [c_void_p]); sig(cf, "CFDataGetBytePtr", POINTER(c_ubyte), [c_void_p])
        sig(cf, "CFRelease", None, [c_void_p]); sig(cf, "CFArrayGetCount", c_long, [c_void_p])
        sig(cf, "CFArrayGetValueAtIndex", c_void_p, [c_void_p, c_long]); sig(cf, "CFDictionaryGetValue", c_void_p, [c_void_p, c_void_p])
        sig(cf, "CFStringCreateWithCString", c_void_p, [c_void_p, c_char_p, c_uint32])
        sig(cf, "CFStringGetCString", c_ubyte, [c_void_p, c_char_p, c_long, c_uint32]); sig(cf, "CFNumberGetValue", c_ubyte, [c_void_p, c_int, c_void_p])
        self.keys = {k: cf.CFStringCreateWithCString(None, k.encode(), _UTF8)
                     for k in ("kCGWindowOwnerName", "kCGWindowLayer", "kCGWindowNumber", "kCGWindowBounds", "kCGWindowAlpha")}
        self.rect_null = _Rect(_Pt(float("inf"), float("inf")), _Sz(0, 0))

    def num(self, ref):
        v = c_int64(0)
        if not ref or not self.cf.CFNumberGetValue(ref, 4, byref(v)): return None
        return v.value

    def double(self, ref):
        v = c_double(0)
        if not ref or not self.cf.CFNumberGetValue(ref, 6, byref(v)): return None      # 6 = kCFNumberFloat64Type
        return v.value

    def owner(self, d):
        """The owner name of a window-info dictionary as str, or None."""
        ref = self.cf.CFDictionaryGetValue(d, self.keys["kCGWindowOwnerName"])
        if not ref: return None
        buf = ctypes.create_string_buffer(256)
        if not self.cf.CFStringGetCString(ref, buf, 256, _UTF8): return None
        return buf.value.decode("utf-8", "ignore")


def _load():
    global _native
    if _native is None:
        _native = _Native()
    return _native


class DarwinCapture(base.Capture):
    def available(self):
        try:
            _load()
        except (OSError, AttributeError):
            return False, "The WoW Mentor bridge reads the game window with a macOS API and only runs on a Mac."
        return True, ""

    def foreground_is_game(self, handle=None):
        try:
            n = _load()
            arr = n.cg.CGWindowListCopyWindowInfo(0x1 | 0x10, 0)      # on screen only, without desktop elements; ordered front to back
            if not arr: return None
            try:
                for i in range(n.cf.CFArrayGetCount(arr)):
                    d = n.cf.CFArrayGetValueAtIndex(arr, i)
                    if n.num(n.cf.CFDictionaryGetValue(d, n.keys["kCGWindowLayer"])) != 0: continue
                    a = n.double(n.cf.CFDictionaryGetValue(d, n.keys["kCGWindowAlpha"]))
                    if a is not None and a <= 0.0: continue
                    owner = n.owner(d)
                    if owner is None: continue
                    return owner in WOW_OWNERS
            finally:
                n.cf.CFRelease(arr)
        except Exception:
            return None
        return None

    def find_game(self):
        """CGWindowID of the biggest WoW window, or None if the game is not running."""
        n = _load()
        arr = n.cg.CGWindowListCopyWindowInfo(0, 0)
        if not arr: return None
        best, area = 0, 0.0
        try:
            for i in range(n.cf.CFArrayGetCount(arr)):
                d = n.cf.CFArrayGetValueAtIndex(arr, i)
                if n.owner(d) not in WOW_OWNERS: continue
                if n.num(n.cf.CFDictionaryGetValue(d, n.keys["kCGWindowLayer"])) != 0: continue
                r = _Rect()
                if not n.cg.CGRectMakeWithDictionaryRepresentation(n.cf.CFDictionaryGetValue(d, n.keys["kCGWindowBounds"]), byref(r)): continue
                if r.size.w * r.size.h > area: area, best = r.size.w * r.size.h, n.num(n.cf.CFDictionaryGetValue(d, n.keys["kCGWindowNumber"])) or 0
        finally:
            n.cf.CFRelease(arr)
        return best or None

    def grab_top(self, handle, rows):
        from ..bridge.strip import Frame
        n = _load()
        img = n.cg.CGWindowListCreateImage(n.rect_null, 8, handle, 9)   # IncludingWindow, BoundsIgnoreFraming | BestResolution
        if not img: return None
        try:
            w, h, bpr = n.cg.CGImageGetWidth(img), n.cg.CGImageGetHeight(img), n.cg.CGImageGetBytesPerRow(img)
            data = n.cg.CGDataProviderCopyData(n.cg.CGImageGetDataProvider(img))
            try: raw = ctypes.string_at(n.cf.CFDataGetBytePtr(data), min(rows, h) * bpr)
            finally: n.cf.CFRelease(data)
        finally:
            n.cf.CFRelease(img)
        return Frame(raw, w, min(rows, h), bpr)

    def game_running(self):
        try:
            return subprocess.run(["pgrep", "-f", "World of Warcraft"], capture_output=True).returncode == 0
        except OSError:
            return False


# ---------------------------------------------------------------- mask: the compiled Swift helper (bridge/mask/wowmask.swift)
class DarwinMask(base.Mask):
    """Runs the `wowmask` helper and feeds it over stdin: "x y w h capW" lines and "R <base64 row>" lines (protocol unchanged)."""

    def __init__(self):
        self.path = None
        self.proc = None
        self.sent = None
        self.row_at = 0.0

    def available(self):
        return bool(self.path) and os.path.exists(self.path)

    def start(self, cfg):
        self.path = (cfg or {}).get("mask_path")

    def update(self, strip_rect, capture_width, row_provider=None):
        if not self.available(): return
        msg = "%d %d %d %d %d\n" % (tuple(strip_rect) + (capture_width,))
        alive = self.proc and self.proc.poll() is None
        now = time.time()
        row = None
        if row_provider is not None and now - self.row_at >= 0.05:
            try: row = row_provider()
            except Exception: row = None
        if self.sent == msg and alive and row is None: return
        try:
            if not alive:
                self.proc = subprocess.Popen([self.path], stdin=subprocess.PIPE)
            if self.sent != msg or not alive:
                self.proc.stdin.write(msg.encode()); self.sent = msg
            if row:
                self.proc.stdin.write(b"R " + row + b"\n"); self.row_at = now
            self.proc.stdin.flush()
        except Exception as e:
            print("mask error:", e, flush=True); self.proc = None

    def stop(self):
        p, self.proc, self.sent = self.proc, None, None
        if p is not None:
            try:
                p.stdin.close()                      # the helper exits when stdin closes
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                try: p.kill()
                except Exception: pass


# ---------------------------------------------------------------- compiled helpers (need Xcode command line tools)
def have_tool(name):
    try:
        return subprocess.run(["xcrun", "--find", name], capture_output=True).returncode == 0
    except OSError:
        return False


def build_mask():
    """Optional: a tiny black overlay window that hides the data strip on your screen. Returns the path or None."""
    out = os.path.join(cfgmod.path("bin"), "wowmask")
    src = os.path.join(PKG, "bridge", "mask", "wowmask.swift")
    if not have_tool("swiftc"):
        return None
    os.makedirs(os.path.dirname(out), exist_ok=True)
    r = subprocess.run(["swiftc", "-O", src, "-o", out], capture_output=True)
    return out if r.returncode == 0 and os.path.exists(out) else None


LAUNCHER_C = r"""
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    setenv("PYTHONPATH", PYPATH, 1);
    execl(PYTHON, "python3", "-m", "wowmentor", "bridge", (char *)0);
    return 1;
}
"""


def c_string(s):
    """`s` as a C string literal. Paths come from the user's machine (spaces, quotes, backslashes, non-ASCII are all legal), so they are
    written into a generated header as escaped literals instead of being passed through -D (where a quote would end the macro)."""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if c in '"\\':
            out.append("\\" + c)
        elif 32 <= b < 127 and c != "?":                # "?" is escaped to rule out trigraphs
            out.append(c)
        else:
            out.append("\\%03o" % b)
    return '"%s"' % "".join(out)


def launcher_source(python, pypath):
    return "#define PYTHON %s\n#define PYPATH %s\n%s" % (c_string(python), c_string(pypath), LAUNCHER_C)


def app_path():
    return os.path.expanduser("~/Applications/" + APP_NAME)


def build_launcher(python=None):
    """The bridge must run under an app macOS can list in Screen Recording; this is a 6-line launcher that execs Python."""
    if not have_tool("clang"):
        return None
    python = python or sys.executable
    app = app_path()
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos, exist_ok=True)
    plist = {"CFBundleIdentifier": "com.wowmentor.bridge", "CFBundleName": "WoW Mentor Bridge", "CFBundleExecutable": "wowmentor-bridge",
             "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSUIElement": True}
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)
    csrc = os.path.join(cfgmod.path("bin"), "launcher.c")
    os.makedirs(os.path.dirname(csrc), exist_ok=True)
    with open(csrc, "w", encoding="utf-8", newline="\n") as f:
        f.write(launcher_source(python, PKG_ROOT))
    exe = os.path.join(macos, "wowmentor-bridge")
    r = subprocess.run(["clang", "-O2", "-o", exe, csrc], capture_output=True)
    if r.returncode != 0:
        return None
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", app], capture_output=True)
    return app


# ---------------------------------------------------------------- launchd
def plist_path(kind):
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label(kind))


def make_plist(kind, python=None, bridge_app=None):
    python = python or sys.executable
    env = {"PYTHONPATH": PKG_ROOT, "PYTHONUNBUFFERED": "1"}
    if os.environ.get("WOWMENTOR_HOME"):
        env["WOWMENTOR_HOME"] = os.environ["WOWMENTOR_HOME"]
    logs = cfgmod.log_dir()
    if kind == "bridge" and bridge_app:
        args = [os.path.join(bridge_app, "Contents", "MacOS", "wowmentor-bridge")]
    else:
        args = [python, "-m", "wowmentor", kind]
    return {"Label": label(kind), "ProgramArguments": args, "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 5, "StandardOutPath": os.path.join(logs, kind + ".log"), "StandardErrorPath": os.path.join(logs, kind + ".log")}


def write_plist(kind, **kw):
    p = plist_path(kind)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        plistlib.dump(make_plist(kind, **kw), f)
    return p


def _domain():
    return "gui/%d" % os.getuid()


def launchctl(*args):
    if sys.platform != "darwin":
        raise base.HostUnsupported("launchd services exist on macOS only; run `wowmentor server` / `wowmentor bridge` by hand on this system")
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")


def start_agent(kind):
    p = plist_path(kind)
    launchctl("bootout", "%s/%s" % (_domain(), label(kind)))
    return launchctl("bootstrap", _domain(), p).returncode == 0


def stop_agent(kind):
    return launchctl("bootout", "%s/%s" % (_domain(), label(kind))).returncode == 0


def agent_running(kind):
    r = launchctl("print", "%s/%s" % (_domain(), label(kind)))
    return r.returncode == 0 and "state = running" in r.stdout


def remove_plist(kind):
    try:
        os.remove(plist_path(kind))
    except OSError:
        pass


def open_screen_recording_settings():
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"], capture_output=True)


class DarwinServices(base.Services):
    def install(self, kind, cfg, helpers=None):
        return write_plist(kind, bridge_app=(helpers or {}).get("launcher"))

    def start(self, kind):
        return start_agent(kind)

    def stop(self, kind):
        return stop_agent(kind)

    def is_running(self, kind):
        return agent_running(kind)

    def is_installed(self, kind):
        return os.path.exists(plist_path(kind))

    def remove(self, kind):
        stop_agent(kind)
        remove_plist(kind)

    def build_helpers(self, cfg):
        mask = build_mask()
        app = build_launcher()
        summary = "launcher %s, screen overlay %s" % ("ok" if app else "FAILED (needs Xcode tools: xcode-select --install)", "ok" if mask else "skipped")
        return {"launcher": app, "mask": mask, "summary": summary}

    def remove_helpers(self):
        import shutil
        shutil.rmtree(app_path(), ignore_errors=True)


class DarwinShell(base.Shell):
    def copy_to_clipboard(self, text):
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            return False

    def open_url(self, url):
        try:
            subprocess.run(["open", url], capture_output=True)
        except OSError:
            pass

    def open_text_file(self, path):
        try:
            subprocess.run(["open", "-t", path])
        except OSError:
            pass

    def open_privacy_settings(self):
        try:
            open_screen_recording_settings()
        except OSError:
            pass


TEXT = {
    "os_name": "macOS",
    "this_machine": "this Mac",
    "terminal": "Terminal",
    "example_wow_path": "/Applications/World of Warcraft/_forever_",
    "python_help": "Install the Xcode command line tools (`xcode-select --install`) and run again.",
    "python_fix": "Install Apple's command line tools: xcode-select --install",
    "privacy_settings": "System Settings > Privacy & Security > Screen & System Audio Recording",
    "permission_lines": (
        "5/5  One permission is needed: opening System Settings > Privacy & Security > Screen & System Audio Recording.",
        "     Click +, choose  %(app)s  (in your Applications folder inside your home folder), turn it on.",
    ),
    "capture_failed_hint": "Screen Recording permission?",
    "capture_fix": "Open System Settings > Privacy & Security > Screen & System Audio Recording, turn on '%(app)s', then: wowmentor restart",
    "package_manager": "brew",
    "cloudflared_hint": "Install cloudflared first:  brew install cloudflared   (or see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)",
    "not_supported_title": "This is not a Mac",
}


def make():
    return base.Host("darwin", DarwinLocations(), DarwinCapture(), DarwinMask(), DarwinServices(), DarwinShell(), CommonFs(), TEXT)
