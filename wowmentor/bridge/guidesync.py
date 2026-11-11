"""Applies the agent's guide edits to your real Guidelime guide files and keeps the server's cache of them current.

  scan   : every few seconds upload guides whose content changed (so the agent always reads what is on disk)
  apply  : fetch queued edits, re-validate against the real file, back it up, write it atomically, report back
  reload : once edits were applied, notice when the game reloads (the addon's uiboot id changes) and say so

Which files: every .lua file inside an addon folder named Guidelime_* (but not Guidelime itself) that registers a guide.
Backups live in <config>/data/guide-backups: `orig` is the file as WoW Mentor first found it, `versions` has a copy
before every agent edit. Nothing is ever deleted from your AddOns folder.
"""
import json, os, re, shutil, time, urllib.error, urllib.request

from . import localnet
from .. import host
from ..server import tugdoc

VERSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")      # backup ids look like "12" or "12-prerestore" (\Z, not `$`: no trailing newline)
MAX_GUIDE_CHARS = 200_000                                     # the server refuses larger uploads; do not retry them every scan
ADDONS = ""
LOCAL = ""
BACKUPS = ""


def configure(addons_dir, local_url, backups_dir):
    global ADDONS, LOCAL, BACKUPS
    ADDONS, LOCAL, BACKUPS = addons_dir, local_url, backups_dir


def read(path):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_atomic(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        host.get().fs.atomic_replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)          # a full disk must not leave a half-written temp file next to the guide
        except OSError:
            pass
        raise


def discover(addons_dir):
    """{display name: absolute path} of every guide file in Guidelime guide packs."""
    files = []
    try:
        entries = sorted(os.listdir(addons_dir))
    except OSError:
        return {}
    for entry in entries:
        low = entry.lower()
        if not low.startswith("guidelime_"):      # a guide pack addon, e.g. GuideLime_TUGs_Forever (not Guidelime itself)
            continue
        root = os.path.join(addons_dir, entry)
        if os.path.islink(root):                  # never follow a link out of the AddOns folder
            continue
        for dirpath, _, names in os.walk(root):
            for f in names:
                if f.endswith(".lua"):
                    p = os.path.join(dirpath, f)
                    if os.path.islink(p):
                        continue
                    try:
                        with open(p, encoding="utf-8", errors="replace") as fh:
                            if "registerGuide(" in fh.read(400):
                                files.append(p)
                    except OSError:
                        pass
    if not files:
        return {}
    common = os.path.commonpath([os.path.dirname(p) for p in files]) if len(files) > 1 else os.path.dirname(files[0])
    return {os.path.relpath(p, common).replace(os.sep, "/")[:-4]: p for p in files}


class GuideSync(object):
    def __init__(self, secret, get_kv, game_age, log=print):
        self.base = "%s/ui/%s/tug" % (LOCAL, secret)
        self.get_kv, self.game_age, self.log = get_kv, game_age, log
        self.sent = {}                  # name -> content hash last uploaded
        self.too_big = set()
        self.pending_reload = False
        self.boot_at_edit = None
        self.last_scan = 0.0
        self.files = {}
        self._rescan()

    # ---------------------------------------------------------------- http
    def _call(self, route, body=None, timeout=10):
        try:
            req = urllib.request.Request("%s/%s" % (self.base, route), json.dumps(body).encode() if body is not None else None,
                                         {"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
            with localnet.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return None
        except Exception:
            return None

    # ---------------------------------------------------------------- files
    def _rescan(self):
        self.files = discover(ADDONS)
        try:
            for name, path in self.files.items():
                dst = os.path.join(BACKUPS, "orig", name + ".lua")
                if not os.path.exists(dst):          # the first time we see a file is the "orig" you can always go back to
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(path, dst)
        except OSError as e:
            self.log("guide backup failed: %r" % (e,))

    def path(self, name):
        return self.files[name]

    def versions(self, name):
        d = os.path.join(BACKUPS, "versions", name)
        out = [{"id": "orig", "label": "as first found"}]
        if os.path.isdir(d):
            for f in sorted(os.listdir(d))[-25:]:
                out.append({"id": f[:-4], "label": "before edit " + f[:-4]})
        return out

    def _backup(self, name, tag):
        d = os.path.join(BACKUPS, "versions", name)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(self.path(name), os.path.join(d, "%s.lua" % tag))

    # ---------------------------------------------------------------- loop
    def scan(self):
        self._rescan()
        for name in sorted(self.files):
            try:
                text = read(self.files[name])
            except OSError:
                continue
            if len(text) > MAX_GUIDE_CHARS:
                if name not in self.too_big:
                    self.too_big.add(name)
                    self.log("guide %s is larger than %d characters: the mentor cannot read or edit it" % (name, MAX_GUIDE_CHARS))
                continue
            h = tugdoc.sha(text)
            if self.sent.get(name) != h:
                r = self._call("file", {"name": name, "text": text, "versions": self.versions(name)})
                if r and r.get("ok"):
                    self.sent[name] = h

    def apply_pending(self):
        r = self._call("pending")
        if not r or not r.get("ok"):
            return
        for e in r["queue"]:
            name = e["guide"]
            try:
                if name not in self.files:
                    raise tugdoc.TugError(["guide %r is not on disk any more" % name])
                if e.get("restore"):
                    ver = str(e["restore"]["restore"])
                    if ver != "orig" and not VERSION_ID.fullmatch(ver):       # the id comes over the network: never let it leave the backup folder
                        raise tugdoc.TugError(["version %r is not a valid restore point id" % ver[:40]])
                    src = os.path.join(BACKUPS, "orig", name + ".lua") if ver == "orig" else os.path.join(BACKUPS, "versions", name, ver + ".lua")
                    if not os.path.exists(src):
                        raise tugdoc.TugError(["version %r not found on disk" % ver])
                    self._backup(name, "%d-prerestore" % e["id"])
                    new_text = read(src)
                else:
                    cur = read(self.path(name))
                    new_text, _, _, _ = tugdoc.apply_to_text(cur, e["ops"])
                    self._backup(name, "%d" % e["id"])
                write_atomic(self.path(name), new_text)
                self.sent[name] = tugdoc.sha(new_text)
                kv = self.get_kv()
                live = self.game_age() is not None and self.game_age() < 15 and kv is not None
                boot = kv.get("uiboot") if live else None
                self._call("applied", {"id": e["id"], "ok": True, "text": new_text, "versions": self.versions(name), "game_live": live, "boot": boot})
                self.pending_reload, self.boot_at_edit = True, boot
                self.log("guide edit %d applied to %s" % (e["id"], name))
            except tugdoc.TugError as err:
                self.sent.pop(name, None)          # the server's optimistic copy may be ahead of the real file: re-upload it
                self._call("applied", {"id": e["id"], "ok": False, "errors": err.errors[:4]})
                self.log("guide edit %d failed: %s" % (e["id"], err))
            except Exception as err:
                self.sent.pop(name, None)
                self._call("applied", {"id": e["id"], "ok": False, "errors": ["bridge error: %r" % (err,)]})

    def watch_reload(self):
        if not self.pending_reload:
            return
        kv = self.get_kv()
        live = self.game_age() is not None and self.game_age() < 15 and kv is not None
        boot = kv.get("uiboot") if live else None
        if boot and boot != self.boot_at_edit:
            self._call("reloaded", {"boot": boot})
            self.pending_reload = False

    def resume_pending(self):
        """After a bridge restart: the server still holds an unanswered reload prompt. Pick it up so it clears at the next reload."""
        st = self._call("status")
        r = (st or {}).get("reload") or {}
        if r.get("needed"):
            self.pending_reload, self.boot_at_edit = True, r.get("boot")
            self.log("guidesync: resumed a pending reload prompt")

    def run(self):
        try:
            self.resume_pending()
        except Exception as e:
            self.log("guidesync resume error: %r" % (e,))
        while True:
            try:
                if time.time() - self.last_scan > 6:
                    self.last_scan = time.time()
                    self.scan()
                self.apply_pending()
                self.watch_reload()
            except Exception as e:
                self.log("guidesync error: %r" % (e,))
            time.sleep(2)
