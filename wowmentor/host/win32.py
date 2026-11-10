"""The Windows host: registry and folder discovery of the WoW client, per-user autostart without admin rights, clipboard and files.

Capture (`win32_capture.Win32Capture`) and the screen mask (`win32_mask.Win32Mask`) are imported lazily, so a missing or unfinished
module never breaks `import wowmentor.host.win32`. Everything that needs a Windows API is bound inside a method (ctypes, winreg), so this
module imports on every OS; the pure decisions (path normalisation, command lines, retry policy) are plain functions tested everywhere.

Files and registry values this host creates (all per user, none needs administrator rights):
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\WoWMentor   starts the supervisors at login (see win32_supervisor.py)
  <config>\\run\\<kind>.installed / .pid / .stop                      which services are installed, and the supervisor's pid file
"""
import ctypes, hashlib, ntpath, os, subprocess, sys, tempfile, time, webbrowser

from . import base, win32_supervisor as sup
from .common import CommonFs, scan_wow_roots, write_private_file
from .. import config as cfgmod

CLIENT_DIRS = ("_forever_", "_classic_era_", "_classic_", "_anniversary_", "_retail_", "_classic_beta_")
WOW_KEYS = (r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft", r"SOFTWARE\Blizzard Entertainment\World of Warcraft")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "WoWMentor"
RUN_VALUE_MAX = 259                 # the Run key ignores longer command lines
RETRY_WINERRORS = (5, 32, 33, 1224)  # access denied, sharing violation, lock violation, user-mapped section open
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
DRIVE_FIXED = 3


# ---------------------------------------------------------------- registry (a seam: tests replace `registry`)
class Registry(object):
    """The few registry reads and writes this host needs. `winreg` is imported inside each method."""

    def wow_install_paths(self):
        """Every InstallPath value Battle.net left for WoW, HKLM then HKCU, 64-bit then 32-bit view. Never raises."""
        import winreg
        out = []
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                for sub in WOW_KEYS:
                    try:
                        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as k:
                            value, _ = winreg.QueryValueEx(k, "InstallPath")
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        out.append(value.strip())
        return out

    def get_run(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def set_run(self, name, command):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command)

    def delete_run(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass


registry = Registry()


# ---------------------------------------------------------------- locations
def client_base(path, isdir=os.path.isdir):
    """The folder that CONTAINS the client folders (`World of Warcraft`), derived from a registry value. Battle.net's InstallPath is
    sometimes the game folder, sometimes a client folder or the launcher file inside it, so walk up (at most 4 levels) until a child
    named _forever_, _retail_, ... exists. None if there is none."""
    p = os.path.normpath((path or "").strip().strip('"'))
    if not p or p == ".":
        return None
    if os.path.basename(p).lower() in CLIENT_DIRS:
        p = os.path.dirname(p)
    elif not isdir(p):
        p = os.path.dirname(p)                      # a file such as ...\World of Warcraft Launcher.exe
    for _ in range(5):
        if any(isdir(os.path.join(p, c)) for c in CLIENT_DIRS):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def default_roots(env=None, drives=()):
    """The usual `World of Warcraft` folders on Windows (written with ntpath so the list is the same when tested on another OS)."""
    env = os.environ if env is None else env
    pf86 = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pf = env.get("ProgramFiles") or r"C:\Program Files"
    roots = [ntpath.join(pf86, "World of Warcraft"), ntpath.join(pf, "World of Warcraft"),
             r"C:\Program Files (x86)\World of Warcraft", r"C:\Program Files\World of Warcraft"]
    for d in drives:
        d = d.rstrip("\\/")
        roots.append(d + r"\Games\World of Warcraft")
        roots.append(d + r"\Blizzard\World of Warcraft")
        roots.append(d + r"\World of Warcraft")
    return roots


def fixed_drives():
    """['C:', 'D:', ...] for the fixed disks (not network, optical or USB-removable drives). ['C:'] if Windows cannot say."""
    try:
        k32 = ctypes.WinDLL("kernel32")
        mask = k32.GetLogicalDrives()
        out = []
        for i in range(26):
            if mask & (1 << i):
                letter = chr(ord("A") + i) + ":"
                if k32.GetDriveTypeW(letter + "\\") == DRIVE_FIXED:
                    out.append(letter)
        return out or ["C:"]
    except (AttributeError, OSError):
        return ["C:"]


def dedupe_paths(paths):
    seen, out = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def permission_message(path):
    """What to tell the user when the AddOns folder is not writable (typically a game under C:\\Program Files). WoW Mentor never
    asks for administrator rights, so the answer is a folder the user owns."""
    return ("Windows does not let WoW Mentor write to %s (it is probably under C:\\Program Files, which needs administrator rights). "
            "WoW Mentor never asks for those. Either move the game to a folder you own, for example C:\\Games\\World of Warcraft, using "
            "Battle.net, and run `wowmentor setup` again, or choose a client folder you can write to with --wow-dir "
            "\"D:\\Games\\World of Warcraft\\_forever_\"." % path)


class Win32Locations(base.Locations):
    def config_home(self):
        base_dir = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base_dir, "WoWMentor")

    def log_dir(self):
        base_dir = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base_dir, "WoWMentor", "logs")

    def find_wow_dirs(self, search_roots=None):
        if search_roots:
            return scan_wow_roots(search_roots)
        roots = []
        try:
            for value in registry.wow_install_paths():
                b = client_base(value)
                if b:
                    roots.append(b)
        except Exception:                           # winreg missing (not Windows) or a broken hive: fall through to the folder scan
            pass
        try:
            drives = fixed_drives()
        except Exception:
            drives = ["C:"]
        roots.extend(default_roots(drives=drives))
        return scan_wow_roots(dedupe_paths(roots))

    def extra_bin_dirs(self):
        local, pf, pf86 = os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")
        dirs = []
        if local:
            dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))      # where `winget install Cloudflare.cloudflared` puts it
        for p in (pf, pf86):
            if p:
                dirs.append(os.path.join(p, "cloudflared"))
        return dirs

    def addons_write_problem(self, wow_dir):
        """None if WoW Mentor can create files in <wow_dir>\\Interface\\AddOns, else the message to show (see permission_message).
        Call it before copying the addon so setup can stop with advice instead of a traceback. Never raises."""
        d = self.addons_dir(wow_dir)
        try:
            with tempfile.TemporaryFile(dir=d):
                pass
        except PermissionError:
            return permission_message(d)
        except OSError:
            return None                              # missing folder and other errors surface in the normal install path
        return None


# ---------------------------------------------------------------- services
def run_value_name():
    """'WoWMentor', or 'WoWMentor-<hash>' under a custom $WOWMENTOR_HOME so a test or a second install cannot touch the real entry."""
    if os.environ.get("WOWMENTOR_HOME"):
        return "%s-%s" % (RUN_VALUE, hashlib.sha1(cfgmod.home().encode("utf-8")).hexdigest()[:6])
    return RUN_VALUE


def run_command(exe=None):
    """The command line stored in the Run key: pythonw.exe (no console window) running the supervisor's autostart pass."""
    home = cfgmod.home() if os.environ.get("WOWMENTOR_HOME") else None
    return subprocess.list2cmdline(sup.supervisor_argv(None, home, exe))


class Win32Services(base.Services):
    """Per-user services: one supervisor process per kind (win32_supervisor.py), started at login by one Run-key value."""

    def __init__(self, sleep=time.sleep):
        self.sleep = sleep

    @staticmethod
    def _check(kind):
        if kind not in base.KINDS:
            raise ValueError("unknown service %r (expected one of %s)" % (kind, ", ".join(base.KINDS)))

    def install(self, kind, cfg, helpers=None):
        self._check(kind)
        cmd = run_command()
        if len(cmd) > RUN_VALUE_MAX:
            raise base.HostUnsupported("The autostart command is %d characters, but Windows ignores Run-key commands over %d. Install WoW Mentor "
                                       "in a shorter folder path (for example C:\\WoWMentor)." % (len(cmd), RUN_VALUE_MAX))
        name = run_value_name()
        try:
            registry.set_run(name, cmd)
        except OSError as e:
            raise base.HostUnsupported("Could not register WoW Mentor to start at login (%s). Start it by hand with `wowmentor start`." % e)
        marker = sup.installed_marker(kind)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        write_private_file(marker, ('{"kind": "%s", "installed": %d}\n' % (kind, int(time.time()))).encode("utf-8"))
        return "HKCU\\%s\\%s" % (RUN_KEY, name)

    def start(self, kind):
        self._check(kind)
        if self.is_running(kind):
            self.stop(kind)
        home = cfgmod.home() if os.environ.get("WOWMENTOR_HOME") else None
        try:
            sup.launch_detached(kind, home)
        except OSError:
            return False
        self.sleep(0.5)                              # a supervisor that dies at once (broken Python) should read as a failed start
        info = sup.read_pidfile(sup.pidfile_path(kind))
        return bool(info) and sup.pid_alive(info["pid"])

    def stop(self, kind, wait=4.0):
        self._check(kind)
        pidpath = sup.pidfile_path(kind)
        info = sup.read_pidfile(pidpath)
        # A stale heartbeat means the pid may belong to some unrelated program by now, so only a fresh one is ever killed.
        was = bool(info) and sup.is_fresh(info) and sup.pid_alive(info["pid"])
        if was:
            try:
                with open(sup.stopfile_path(kind), "w", encoding="utf-8", newline="\n") as f:
                    f.write("stop\n")                # the supervisor ends its child and exits by itself
            except OSError:
                pass
            waited = 0.0
            while waited < wait and sup.pid_alive(info["pid"]):
                self.sleep(0.2)
                waited += 0.2
            if sup.pid_alive(info["pid"]):
                sup.kill_tree(info["pid"])           # taskkill /PID n /T /F, no shell
                self.sleep(0.2)
        for p in (pidpath, sup.stopfile_path(kind)):
            try:
                os.remove(p)
            except OSError:
                pass
        return was

    def is_running(self, kind):
        if kind not in base.KINDS:
            return False
        try:
            return sup.supervisor_running(kind)
        except Exception:
            return False

    def is_installed(self, kind):
        if kind not in base.KINDS:
            return False
        try:
            return os.path.exists(sup.installed_marker(kind)) and bool(registry.get_run(run_value_name()))
        except Exception:
            return False

    def remove(self, kind):
        self._check(kind)
        self.stop(kind)
        try:
            os.remove(sup.installed_marker(kind))
        except OSError:
            pass
        if not sup.installed_kinds():
            registry.delete_run(run_value_name())

    def build_helpers(self, cfg):
        return {"launcher": None, "mask": None, "summary": ""}       # nothing to compile on Windows


# ---------------------------------------------------------------- shell
def clipboard_bytes(text):
    """`text` as the clipboard wants it: UTF-16LE, CRLF line ends, terminating NUL."""
    return (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")


_clip_api = None


def _clipboard_api():
    """user32 and kernel32 with full 64-bit-safe signatures (default int arguments truncate handles). Built on first use."""
    global _clip_api
    if _clip_api is None:
        from ctypes import c_int, c_size_t, c_uint, c_void_p
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        for lib, name, res, args in (
                (u, "OpenClipboard", c_int, [c_void_p]), (u, "CloseClipboard", c_int, []), (u, "EmptyClipboard", c_int, []),
                (u, "SetClipboardData", c_void_p, [c_uint, c_void_p]), (u, "GetClipboardData", c_void_p, [c_uint]),
                (k, "GlobalAlloc", c_void_p, [c_uint, c_size_t]), (k, "GlobalLock", c_void_p, [c_void_p]),
                (k, "GlobalUnlock", c_int, [c_void_p]), (k, "GlobalFree", c_void_p, [c_void_p])):
            f = getattr(lib, name)
            f.restype, f.argtypes = res, args
        _clip_api = (u, k)
    return _clip_api


def _open_clipboard(user32, sleep=time.sleep, tries=10):
    """Another program may hold the clipboard for a moment; retry briefly."""
    for _ in range(tries):
        if user32.OpenClipboard(None):
            return True
        sleep(0.05)
    return False


def set_clipboard_text(text):
    """CF_UNICODETEXT through ctypes. https://learn.microsoft.com/en-us/windows/win32/dataxchg/using-the-clipboard
    True on success."""
    user32, kernel32 = _clipboard_api()
    data = clipboard_bytes(text)
    if not _open_clipboard(user32):
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)                   # on success the system owns the memory, on failure we still do
            return False
        return True
    finally:
        user32.CloseClipboard()


def get_clipboard_text():
    """The clipboard's text, or None. Used by the Windows-only tests to prove the round trip."""
    user32, kernel32 = _clipboard_api()
    if not _open_clipboard(user32):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clip_exe_copy(text, run=subprocess.run):
    """Fallback when the clipboard API is unavailable: clip.exe reads UTF-16 text that starts with a byte order mark (not verified
    against a primary source; failures return False)."""
    try:
        r = run(["clip"], input=b"\xff\xfe" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=sup.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Win32Shell(base.Shell):
    def copy_to_clipboard(self, text):
        try:
            if set_clipboard_text(text):
                return True
        except Exception:
            pass
        return clip_exe_copy(text)

    def open_url(self, url):
        try:
            if str(url).lower().startswith(("http://", "https://")):
                webbrowser.open(url)
            else:
                os.startfile(url)                    # folders and files: the default app
        except Exception:
            pass

    def open_text_file(self, path):
        try:
            os.startfile(path)
        except Exception:
            self.open_url(path)

    def open_privacy_settings(self):
        pass                                          # reading the game window needs no permission on Windows


# ---------------------------------------------------------------- fs
def tighten_acl(path, run=subprocess.run, env=None):
    """Best effort: leave only the current user on the file. Grant first, THEN drop inheritance (the other order can leave a file nobody
    can open). Never raises; the user profile's own ACL is the fallback protection."""
    env = os.environ if env is None else env
    user = env.get("USERNAME")
    if not user:
        return False
    who = "%s\\%s" % (env["USERDOMAIN"], user) if env.get("USERDOMAIN") else user
    flags = sup.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        for args in (["/grant:r", "%s:(F)" % who], ["/inheritance:r"]):
            r = run(["icacls", path] + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, timeout=15)
            if r.returncode != 0:
                return False
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def replace_retrying(src, dst, tries=10, delay=0.05, sleep=time.sleep, replace=os.replace):
    """os.replace, retried (about 3 s in all) while an indexer, antivirus scanner or reader holds the destination: winerror 5, 32, 33,
    1224. Any other error, or the last failure, is raised."""
    for attempt in range(tries):
        try:
            replace(src, dst)
            return
        except PermissionError as e:
            if getattr(e, "winerror", None) not in RETRY_WINERRORS or attempt == tries - 1:
                raise
            sleep(min(delay * (attempt + 1), 0.4))


class Win32Fs(CommonFs):
    def __init__(self, acl=tighten_acl, sleep=time.sleep):
        self._acl, self._sleep = acl, sleep

    def atomic_replace(self, src, dst):
        replace_retrying(src, dst, sleep=self._sleep)

    def write_private(self, path, data, mode=0o600):
        def replace(tmp, dst):
            self._acl(tmp)                           # restrict the temp file before it takes the final name: no readable moment
            self.atomic_replace(tmp, dst)
        write_private_file(path, data, mode, replace)


# ---------------------------------------------------------------- the host
TEXT = {
    "os_name": "Windows",
    "this_machine": "this PC",
    "terminal": "PowerShell",
    "example_wow_path": "C:\\Program Files (x86)\\World of Warcraft\\_forever_",
    "python_help": "Install Python 3.9 or newer from https://www.python.org/downloads/windows/ (or run: winget install -e --id Python.Python.3.12) and run again.",
    "python_fix": "Install Python 3.9 or newer: winget install -e --id Python.Python.3.12",
    "privacy_settings": "",
    "permission_lines": (),
    "capture_failed_hint": "is WoW visible, in Windowed or Windowed (Fullscreen) mode, and not covered by another window?",
    "capture_fix": "Put WoW in Windowed or Windowed (Fullscreen) mode (Options > Graphics), keep its window uncovered, then restart WoW Mentor: wowmentor restart",
    "package_manager": "winget",
    "cloudflared_hint": "Install cloudflared first:  winget install --id Cloudflare.cloudflared   (then open a new PowerShell window), or see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
    "not_supported_title": "This is not Windows",
}


class Win32Host(base.Host):
    """Windows. `capture` and `mask` are built on first use from win32_capture / win32_mask, so those modules may be missing or
    half written without breaking the import of this one."""

    def __init__(self):
        base.Host.__init__(self, "win32", Win32Locations(), None, None, Win32Services(), Win32Shell(), Win32Fs(), TEXT)
        self._capture = self._mask = None

    @property
    def capture(self):
        if self._capture is None:
            from .win32_capture import Win32Capture
            self._capture = Win32Capture()
        return self._capture

    @capture.setter
    def capture(self, value):
        self._capture = value

    @property
    def mask(self):
        if self._mask is None:
            from .win32_mask import Win32Mask
            self._mask = Win32Mask()
        return self._mask

    @mask.setter
    def mask(self, value):
        self._mask = value


def make():
    return Win32Host()
