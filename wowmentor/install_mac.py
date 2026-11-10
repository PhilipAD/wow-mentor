"""Everything `wowmentor setup` does to a Mac, as small testable functions.

No step touches your characters or WTF settings. Files written outside the config folder:
  <WoW>/Interface/AddOns/WoWFeed        the addon (draws the data strip and the overlay)
  <WoW>/Interface/AddOns/WFSlot000-1000 empty load-on-demand slots the overlay is delivered through
  ~/Applications/WoW Mentor Bridge.app  a tiny launcher so macOS lists it under Screen Recording
  ~/Library/LaunchAgents/com.wowmentor.*.plist   start the server and bridge at login
"""
import os, plistlib, shutil, subprocess, sys

from . import config as cfgmod

POOL = 1000
ADDON = "WoWFeed"
LABELS = {"server": "com.wowmentor.server", "bridge": "com.wowmentor.bridge", "tunnel": "com.wowmentor.tunnel"}


def label(kind):
    """The launchd label. With a custom $WOWMENTOR_HOME (tests, a second install) it gets a short suffix, so it can never
    stop, replace or remove the services of the real install."""
    base = LABELS[kind]
    if os.environ.get("WOWMENTOR_HOME"):
        import hashlib
        return "%s.%s" % (base, hashlib.sha1(cfgmod.home().encode()).hexdigest()[:6])
    return base
APP_NAME = "WoW Mentor Bridge.app"
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- finding WoW
def find_wow_dirs(search_roots=None):
    """Client folders (like .../World of Warcraft/_forever_) that have an Interface/AddOns folder, Forever first."""
    roots = search_roots or ["/Applications/World of Warcraft", os.path.expanduser("~/Applications/World of Warcraft"), "/Applications/Blizzard/World of Warcraft"]
    found = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if os.path.isdir(os.path.join(p, "Interface", "AddOns")):
                found.append(p)
    return sorted(found, key=lambda p: (0 if "forever" in p.lower() else 1, p))


def addons_dir(wow_dir):
    return os.path.join(wow_dir, "Interface", "AddOns")


# ---------------------------------------------------------------- addon and slots
def valid_wav():
    from .bridge.uisync import valid_wav as vw
    return vw()


def install_addon(wow_dir):
    """Copy the addon in (replacing its own files, never touching other addons). Returns the AddOns path."""
    src = os.path.join(PKG, "addon", ADDON)
    dst = os.path.join(addons_dir(wow_dir), ADDON)
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
    for name in os.listdir(dst):                       # remove files from older versions that no longer exist
        if name.endswith(".lua") and not os.path.exists(os.path.join(src, name)):
            os.remove(os.path.join(dst, name))
    return dst


def create_slots(wow_dir, pool=POOL):
    """The overlay is delivered through pre-made load-on-demand addons (the client only discovers addons that exist at launch)."""
    root = addons_dir(wow_dir)
    new = 0
    for n in range(0, pool + 1):
        name = "WFSlot%03d" % n
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        toc = os.path.join(d, name + ".toc")
        if not os.path.exists(toc):
            with open(toc, "w") as f:
                f.write("## Interface: 16001\n## Title: %s\n## Notes: WoW Mentor overlay slot (load on demand, safe to leave enabled)\n## LoadOnDemand: 1\n\nInbox.lua\n" % name)
            new += 1
        inbox = os.path.join(d, "Inbox.lua")
        if not os.path.exists(inbox):
            with open(inbox, "w") as f:
                f.write("-- empty slot\n")
    sig = os.path.join(root, ADDON, "sig")
    os.makedirs(sig, exist_ok=True)
    for name, data in (("ok.wav", valid_wav()), ("zero.wav", b"")):
        with open(os.path.join(sig, name), "wb") as f:
            f.write(data)
    return new


def remove_addon(wow_dir):
    root = addons_dir(wow_dir)
    removed = 0
    for name in os.listdir(root):
        if name == ADDON or name.startswith("WFSlot"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            removed += 1
    return removed


def slots_present(wow_dir):
    root = addons_dir(wow_dir)
    try:
        return sum(1 for n in os.listdir(root) if n.startswith("WFSlot"))
    except OSError:
        return 0


def addon_installed_version(wow_dir):
    toc = os.path.join(addons_dir(wow_dir), ADDON, ADDON + ".toc")
    try:
        with open(toc) as f:
            for line in f:
                if line.startswith("## Version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


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
    with open(csrc, "w") as f:
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
        raise RuntimeError("launchd services exist on macOS only; run `wowmentor server` / `wowmentor bridge` by hand on this system")
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True)


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
    if sys.platform != "darwin":
        return
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"], capture_output=True)
