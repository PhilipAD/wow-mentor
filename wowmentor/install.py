"""What `wowmentor setup` does to the WoW folder, as small testable functions (no operating-system specifics: those are in wowmentor.host).

No step touches your characters or WTF settings. Files written outside the config folder:
  <WoW>/Interface/AddOns/WoWFeed        the addon (draws the data strip and the overlay)
  <WoW>/Interface/AddOns/WFSlot000-1000 empty load-on-demand slots the overlay is delivered through
plus whatever the host installs for the services (see wowmentor/host/darwin.py).
"""
import os, shutil

POOL = 1000
ADDON = "WoWFeed"
PKG = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(PKG)


# ---------------------------------------------------------------- finding WoW
def find_wow_dirs(search_roots=None):
    """Client folders (like .../World of Warcraft/_forever_) that have an Interface/AddOns folder, Forever first.
    Where to look by default is the host's business (wowmentor.host)."""
    from . import host
    return host.get().locations.find_wow_dirs(search_roots)


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
            with open(toc, "w", encoding="utf-8", newline="\n") as f:
                f.write("## Interface: 16001\n## Title: %s\n## Notes: WoW Mentor overlay slot (load on demand, safe to leave enabled)\n## LoadOnDemand: 1\n\nInbox.lua\n" % name)
            new += 1
        inbox = os.path.join(d, "Inbox.lua")
        if not os.path.exists(inbox):
            with open(inbox, "w", encoding="utf-8", newline="\n") as f:
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
        with open(toc, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("## Version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None
