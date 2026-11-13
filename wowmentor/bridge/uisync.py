"""Delivers the overlay document into the running game and brings the player's actions back.

Inbound (no input injection): rewrite the next load-on-demand slot addons with the document, then raise a
doorbell (turn a zero-byte sig/NNN.wav into a valid silent wav). The addon notices the sound file, loads the next
slot and applies the document. Outbound: the addon reports rev/slot/door in the pixel-strip KV line and a
"ui_state" section, which this module forwards to the server and turns into `check` ops when the player ticks a box.
"""
import json, os, re, struct, threading, time, urllib.error, urllib.request

from . import localnet
from .. import host

ADDONS = ""      # set by configure()
POOL = 1000
AHEAD = 60        # slots written ahead of the game's reported slot
LOCAL = ""


def configure(addons_dir, local_url):
    global ADDONS, LOCAL
    ADDONS, LOCAL = addons_dir, local_url
TICK = re.compile(r"^tick seq=(\d+) ([^/\s]+)/(\S+)=([01])$")


def valid_wav():
    samples = b"\x80" * 160                       # 20 ms of silence, 8-bit mono 8 kHz
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    return b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(samples)) + samples


def atomic_write(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        host.get().fs.atomic_replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def to_int(v, default=0):
    """Int from a KV value; the game writes `unknown` for values it hides and the strip can carry garbage."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def inbox_lua(rev, wire):
    level = 2
    while "]" + "=" * level + "]" in wire:
        level += 1
    eq = "=" * level
    return ("WoWFeedInbox={rev=%d,json=[%s[%s]%s]}\n" % (rev, eq, wire, eq)).encode()


class UISync:
    def __init__(self, secret, get_kv, get_lines, game_age, log=print):
        self.base = "%s/ui/%s" % (LOCAL, secret)
        self.get_kv, self.get_lines, self.game_age, self.log = get_kv, get_lines, game_age, log
        self.wire = None
        self.rev = None            # newest revision written to disk
        self.last_ring = 0.0
        self.written_rev, self.written_lo, self.written_hi = None, 0, 0
        self.rung_door = 0         # highest doorbell we have raised
        self.tick_seen = {}
        self.last_report = 0.0
        self.last_report_sig = None
        self.stats = {"deliveries": 0, "rings": 0}

    # -- server
    def _get(self, route, timeout=5):
        try:
            with localnet.urlopen("%s/%s" % (self.base, route), timeout=timeout) as r:
                return r.read()
        except Exception:
            return None

    def _post(self, route, obj, timeout=8):
        try:
            req = urllib.request.Request("%s/%s" % (self.base, route), json.dumps(obj).encode(), {"Content-Type": "application/json"}, method="POST")
            with localnet.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return None
        except Exception:
            return None

    # -- disk
    def _slot(self, n):
        return os.path.join(ADDONS, "WFSlot%03d" % n, "Inbox.lua")

    def _write_payload(self, first, last):
        data = inbox_lua(self.rev, self.wire)
        for n in range(first, min(POOL, last) + 1):
            p = self._slot(n)
            if os.path.isdir(os.path.dirname(p)):
                atomic_write(p, data)

    def _write_door(self):
        """Advertise the newest revision as TOC metadata of the boot slot (read by the addon as a free doorbell)."""
        toc = os.path.join(ADDONS, "WFSlot000", "WFSlot000.toc")
        body = ("## Interface: 16001\n## Title: WFSlot000\n## Notes: WoW Feed boot slot (load on demand, do not disable)\n"
                "## LoadOnDemand: 1\n## X-Door: %d\n\nInbox.lua\n" % self.rev)
        try:
            atomic_write(toc, body.encode())
        except OSError:
            pass

    def _ring(self, n):
        p = os.path.join(ADDONS, "WoWFeed", "sig", "%03d.wav" % n)
        if os.path.isdir(os.path.dirname(p)) and n <= POOL:
            atomic_write(p, valid_wav())
            return True
        return False

    # -- one pass
    def step(self):
        raw = self._get("doc")
        doc = None
        if raw:
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = None
        if doc and doc.get("rev") != self.rev:
            self.wire, self.rev = raw.decode(), doc["rev"]
            if os.path.isdir(os.path.dirname(self._slot(0))):
                atomic_write(self._slot(0), inbox_lua(self.rev, self.wire))
                self._write_door()
        kv = self.get_kv()
        live = self.game_age() is not None and self.game_age() < 15
        applied = to_int(kv.get("uirev")) if kv else 0
        if live and kv and self.rev is not None and applied < self.rev:
            self._deliver(kv)
        if doc:
            self._ticks(doc)
        self._report(kv, live, applied)

    def _deliver(self, kv):
        now = time.time()
        if now - self.last_ring < 2.5:
            return
        slot, door, mode = to_int(kv.get("uislot")), to_int(kv.get("uidoor")), kv.get("uidb", "?")
        if slot >= POOL - 5:
            self.log("overlay slot pool nearly used up (%d/%d): /reload in game to refresh" % (slot, POOL))
        # The slot counter in the strip lags the game by a few seconds and the game polls every 2 s, so write far
        # ahead; for an unchanged revision only the newly reachable slots need writing.
        lo, hi = slot + 1, slot + AHEAD
        if self.written_rev == self.rev and self.written_lo <= lo <= self.written_hi:
            lo = self.written_hi + 1          # same revision, already written from here on: only the new tail
        if lo <= hi:
            self._write_payload(lo, hi)
        if self.written_rev == self.rev and self.written_lo <= slot + 1 <= self.written_hi:
            self.written_hi = max(self.written_hi, hi)
        else:
            self.written_rev, self.written_lo, self.written_hi = self.rev, slot + 1, hi
        self.stats["deliveries"] += 1
        self.last_ring = now

    def _ticks(self, doc):
        lines = self.get_lines() or []
        for l in lines:
            m = TICK.match(l.strip())
            if not m:
                continue
            seq, panel, item, val = int(m.group(1)), m.group(2), m.group(3), m.group(4) == "1"
            key = (panel, item)
            if seq <= self.tick_seen.get(key, 0):
                continue
            self.tick_seen[key] = seq
            cur = None
            for p in doc.get("panels", []):
                if p["id"] == panel:
                    for c in p.get("children", []):
                        for it in c.get("items", []) if c.get("type") == "checklist" else []:
                            if it["id"] == item:
                                cur = it.get("done", False)
            if cur is not None and cur != val:
                self._post("ops", {"op": "check", "panel": panel, "item": item, "done": val})

    def _report(self, kv, live, applied):
        lines = self.get_lines() or []
        sig = (applied, kv.get("uislot") if kv else None, kv.get("uidoor") if kv else None, live, tuple(lines))
        if sig == self.last_report_sig and time.time() - self.last_report < 10:
            return
        if time.time() - self.last_report < 2:
            return
        self.last_report, self.last_report_sig = time.time(), sig
        self._post("report", {
            "rev_applied": applied, "game_live": live, "slot": to_int(kv.get("uislot")) if kv else None,
            "door": to_int(kv.get("uidoor")) if kv else None, "doorbell": kv.get("uidb") if kv else None,
            "chat_open": bool(kv and kv.get("chat") == "1"), "in_combat": bool(kv and kv.get("combat") == "1"),
            "bridge": self.stats, "lines": lines[:60],
        })

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:
                self.log("uisync error: %r" % (e,))
            time.sleep(1)
