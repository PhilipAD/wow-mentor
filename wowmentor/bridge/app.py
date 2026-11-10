"""The WoW Mentor bridge.

Reads the thin pixel strip the in-game addon draws along the top edge of the game window (lossless window capture,
no input is ever sent to the game), rebuilds the addon's sections, and pushes a text snapshot to the local server.
It also delivers the mentor overlay and guide edits back into the game (uisync.py, guidesync.py).
The strip format is the wow-ai codec (MIT, chelinho139): 3 bits per cell, each of R/G/B fully on or off.
"""
import collections, datetime, glob, json, os, re, struct, sys, threading, time, urllib.error, urllib.request

from . import events as evmod, localnet, luasv, presence as presmod, webhook as whmod
from .. import host as hostmod
from ..server import events as srvevents

CFG = {}          # filled by configure(); keys: wow_dir, local_url, write_secret, ui_secret, include_chat, mask_path, data_dir
ORDER = ["summary","ui_state","secrets_diagnostic","talents_traits","social","misc","system","fast","pvp","professions","recipes","bag_value","auras","group","nearby","stats","equipment","bags","quests","cooldowns","talents","skills","reputation","currency"]
sections, partial = {}, {}
events = collections.deque(maxlen=300); combat = collections.deque(maxlen=150)
meta = {"last_line": 0, "last_mono": 0.0, "file": "", "started": time.time()}
dirty = threading.Event(); lock = threading.Lock()
# Event pipeline (see docs/DESIGN-coach-events.md). Inert until run() wires the HTTP post; tests replace these.
presence = presmod.Presence()
pipe = evmod.EventPipe()
sender = whmod.WebhookSender(log=lambda m: print(m, flush=True))
evj_stats = {"messages": 0, "events": 0, "malformed": 0}

SOCIAL = {"SAY", "YELL", "EMOTE", "TEXT_EMOTE", "WHISPER", "WHISPER_INFORM", "CHANNEL", "GUILD", "OFFICER", "PARTY", "PARTY_LEADER",
          "RAID", "RAID_LEADER", "RAID_WARNING", "INSTANCE_CHAT", "INSTANCE_CHAT_LEADER", "BN_WHISPER", "BN_WHISPER_INFORM", "COMMUNITIES_CHANNEL"}
_CHAT = re.compile(r"\| chat:([A-Z_]+) \|")

def _filter_events(lines):
    """Other players' chat is private: drop it from the feed unless the player turned include_chat on."""
    if CFG.get("include_chat"):
        return lines
    out = []
    for l in lines:
        m = _CHAT.search(l)
        if m and m.group(1) in SOCIAL:
            continue
        out.append(l)
    return out

MAX_PARTS = 64            # parts of one strip message (a 4 kB payload per part); more is garbage, not a message
PARTIAL_TTL_S = 45.0      # a multi-part message whose remaining parts do not arrive within this is abandoned
MAX_SECTIONS = 80         # distinct section names kept
SEC_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")

def _assemble(sec, sid, i, n, txt, now=None):
    """Collect the numbered parts of one strip message. Returns the parts in order once all n arrived, else None. Caller holds lock.

    Parts may arrive in any order and more than once (the capture can see a frame twice, or miss one). A new message starts when the
    id, the part count or the age says so, or when a part arrives again with different text (an addon reload restarts its ids): parts
    of two different messages are never stitched together."""
    if not (1 <= i <= n <= MAX_PARTS):
        partial.pop(sec, None)
        return None
    now = time.monotonic() if now is None else now
    cur = partial.get(sec)
    if cur is None or cur[0] != sid or cur[2] != n or now - cur[3] > PARTIAL_TTL_S or cur[1].get(i, txt) != txt:
        cur = partial[sec] = [sid, {}, n, now]
    cur[1][i] = txt
    cur[3] = now
    if len(cur[1]) == n:
        del partial[sec]
        return [cur[1][k] for k in range(1, n + 1)]
    return None

def handle(sec, sid, i, n, txt):
    meta["last_line"] = time.time(); meta["last_mono"] = time.monotonic()
    if not SEC_NAME.fullmatch(sec):
        return
    evj = None
    with lock:
        if sec == "hb": pass
        elif sec == "ev": events.extend(_filter_events(txt.split("\n")))
        elif sec == "evj":
            parts = _assemble(sec, sid, i, n, txt)
            if parts is not None: evj = "".join(parts)
        elif sec in sections or len(sections) < MAX_SECTIONS:
            parts = _assemble(sec, sid, i, n, txt)
            if parts is not None:
                lines = []
                for p in parts: lines += p.split("\n")
                sections[sec] = (time.time(), lines)
    if evj is not None: ingest_evj(evj)       # outside the lock: parsing must never delay the capture loop
    dirty.set()

def _boot_of(kv):
    b = (kv or {}).get("uiboot")
    return int(b) if isinstance(b, str) and b.isdigit() else b

class BootTracker(object):
    """One boot label per addon load, even while the KV `uiboot` still shows the previous load.

    After a /reload the addon's event counter (`seq`) restarts at 1 but the SUMMARY section (and its uiboot) can be several seconds old,
    so events of the new load would carry the old boot id and collide with the old load's (boot, seq) pairs. A seq that goes back down
    inside the same KV boot therefore starts a new generation: the label becomes "<uiboot>+<n>" until the KV catches up."""
    def __init__(self):
        self.kv_boot, self.gen, self.last_seq = None, 0, None

    def label(self, kv_boot, evs):
        seqs = [e["seq"] for e in evs if isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool)]
        if kv_boot is not None and kv_boot != self.kv_boot:
            self.kv_boot, self.gen = kv_boot, 0
        elif seqs and self.last_seq is not None and seqs[0] <= self.last_seq:
            self.gen += 1
        if seqs:
            self.last_seq = seqs[-1]
        if kv_boot is None or self.kv_boot is None:
            return None
        return self.kv_boot if self.gen == 0 else "%s+%d" % (self.kv_boot, self.gen)

boots = BootTracker()

_noted = collections.deque(maxlen=400)       # (boot, seq) of login/logout events already given to presence


def ingest_evj(text):
    """An `evj` strip message: JSON array of addon events. Malformed input is counted and ignored."""
    try:
        evs = evmod.parse_evj(text)
        evj_stats["messages"] += 1
        if not evs:
            evj_stats["malformed"] += 1
            return
        now = time.time()
        sm = sections.get("summary")
        # the KV boot id can lag a reload by a few seconds: only trust it when the summary is fresh; the addon should send `boot` itself
        boot = boots.label(_boot_of(parse_kv(sm[1])) if sm and now - sm[0] < 30 else None, evs)
        for e in evs:
            if e["type"] in ("login", "logout"):
                key = (e.get("boot", boot), e.get("seq"))
                if key in _noted:                    # the addon resends every event once: presence must see it once
                    continue
                _noted.append(key)
                presence.note_event(e["type"], now, e.get("t"))
        evj_stats["events"] += len(evs)
        pipe.add(evs, boot=boot, received_at=now)
    except Exception as e:
        evj_stats["malformed"] += 1
        print("evj error:", type(e).__name__, flush=True)

sess = {"t0": None, "kv0": None, "prev": None, "gained_xp": 0, "levelups": []}
def parse_kv(lines):
    for l in lines:
        if l.startswith("KV "): return dict(x.split("=", 1) for x in l[3:].split() if "=" in x)
    return None
def vitals():
    sm = sections.get("summary"); kv = parse_kv(sm[1]) if sm else None
    if not kv or time.time() - bars["t"] > 5: return ["(no recent bar measurement)"]
    out = []
    try:
        hpm = float(kv.get("hpmax", "nan")); pwm = float(kv.get("pwmax", "nan"))
        hp = bars["health"]
        if hp is not None: out.append(f"Health: ~{hp*hpm:.0f}/{hpm:.0f} ({hp*100:.1f}%)  [measured from game status bar; game hides the exact number]")
        pw = bars["power"]
        if pw is not None: out.append(f"Power (type {kv.get('pwtype')}): ~{pw*pwm:.0f}/{pwm:.0f} ({pw*100:.1f}%)  [measured]")
        if kv.get("tgt") == "1" and bars["target"] is not None: out.append(f"Target health: ~{bars['target']*100:.1f}% of its max  [measured]")
        elif kv.get("tgt") == "0": out.append("Target: none")
    except Exception as e: out.append(f"vitals error {e}")
    return out

def derive():
    sm = sections.get("summary")
    kv = parse_kv(sm[1]) if sm else None
    if not kv: return ["(waiting for summary data)"]
    now = time.time()
    try:
        lvl, xp, xpm, money, hk = int(kv["level"]), int(kv["xp"]), int(kv["xpmax"]), int(kv["money_copper"]), int(kv["honorable_kills"])
    except (KeyError, ValueError):
        return ["(session rates unavailable: the game hides level or xp right now)"]       # values the game hides arrive as `unknown`
    if sess["kv0"] is None: sess["kv0"] = (lvl, xp, money, hk); sess["t0"] = now
    pv = sess["prev"]
    if pv:
        if lvl > pv[0]:
            sess["gained_xp"] += (pv[2] - pv[1]) + xp; sess["levelups"].append(f"{datetime.datetime.now():%H:%M:%S} reached level {lvl}")
        elif lvl == pv[0] and xp > pv[1]: sess["gained_xp"] += xp - pv[1]
    sess["prev"] = (lvl, xp, xpm)
    hrs = max((now - sess["t0"]) / 3600, 1 / 3600)
    gold_d = money - sess["kv0"][2]; hk_d = hk - sess["kv0"][3]
    rate = sess["gained_xp"] / hrs
    out = [f"Bridge session started: {datetime.datetime.fromtimestamp(sess['t0']):%H:%M:%S} ({hrs*60:.0f} min ago)",
           f"Levels this session: {lvl - sess['kv0'][0]} | XP gained: {sess['gained_xp']} | XP/hour: {rate:.0f}",
           f"Estimated time to next level at this rate: " + (f"{(xpm - xp) / rate * 60:.0f} min" if rate > 0 else "n/a (no XP gained yet)"),
           f"Gold change this session: {gold_d/10000:+.2f}g | Gold/hour: {gold_d/10000/hrs:+.2f}g",
           f"Honorable kills this session: {hk_d} | HK/hour: {hk_d/hrs:.1f}"]
    out += sess["levelups"][-10:]
    return out

GUIDE = ["=== READ ME (for the AI mentor) ===",
 "Live snapshot of one WoW (Forever/Classic-style, interface 16001) character, rebuilt ~1/s from an in-game addon.",
 "Sections: SUMMARY (key facts + KV line), UI_STATE (what the mentor overlay shows and what the player did with it), SESSION (derived rates), FAST (position/state/targets), STATS, EQUIPMENT (with empty-slot list),",
 "BAGS + BAG_VALUE (vendor/junk value), QUESTS, TALENTS, SKILLS (spellbook), PROFESSIONS (skill lines incl. weapon/armor/secondary),",
 "RECIPES (captured when a profession window is opened), PVP (honor, ranks, weekly stats, battleground state), REPUTATION, CURRENCY,",
 "AURAS, GROUP, NEARBY, COOLDOWNS, SYSTEM, RECENT EVENTS (chat, loot, casts, zone changes, quests).",
 "SESSION_STATE (bridge-computed: in_world | loading | afk | logged_out | game_closed, foreground, last login/logout, uploader heartbeat) and",
 "EVENTS_TAIL (the last 20 structured events: death, fight, quest, loot, rule_fired...; full history via `wowui events --since N`). Never act on a",
 "stale feed: check SESSION_STATE first. Other addon sections (ACTIONBARS, PET, TARGET, FIGHTS, DEATHS, ITEMS, REWARDS, TRAINER, NPCS, RULES,",
 "COACH_INBOX, COACH_QUESTIONS, SESSION_ADDON) appear when the installed addon version provides them.",
 "Money is in copper in KV (10000 copper = 1 gold). Positions: map x/y are percent; world position is in yards.",
 "Known limits: the game hides some values from addons ('restricted'); bank contents, auction prices, other players' gear and map pins are not available.",
 "If seconds_since_last_game_update is large, the game is closed/hidden and data is stale.",
 "MENTOR OVERLAY: you can draw tasks, plans, progress bars, callouts, waypoints and toasts in the player's game with the `wowui` CLI",
 "(or its HTTP endpoint). Run `wowui guide` first. The UI_STATE section shows what is on screen and which checklist items the player ticked.", ""]

_svcache = {}
def sv(name):
    out = {}
    for p in glob.glob(os.path.join(CFG.get("wow_dir", ""), "WTF", "Account", "*", "SavedVariables", name + ".lua")):
        try:
            m = os.path.getmtime(p)
            c = _svcache.get(p)
            if c is None or c[0] != m:
                try: c = (m, luasv.parse_file(p), None)
                except Exception as e: c = (m, {}, "%s: %s" % (type(e).__name__, str(e)[:80]))      # a broken file is not re-parsed every second
                _svcache[p] = c
            out.update(c[1])
            if c[2]: out["_error"] = c[2]
        except Exception as e:
            out["_error"] = str(e)
    return out
def itemstr(v):
    if not isinstance(v, str): return str(v)
    head = v.split(";")[0].split(":")[0]; cnt = v.split(";")[1] if ";" in v else "1"
    return f"item#{head} x{cnt}"
def offline():
    out = ["(Data other addons wrote to disk; refreshed only on /reload or logout, so may lag the live sections.)"]
    try:
        bb = sv("BagBrother").get("BrotherBags", {})
        for realm, chars in bb.items():
            if realm == "account" or not isinstance(chars, dict): continue
            for name, c in chars.items():
                if not isinstance(c, dict) or "money" not in c: continue
                out.append(f"BagBrother {name} ({realm}): L{c.get('level')} {c.get('race')} {c.get('class')}, money {c.get('money', 0)/10000:.2f}g")
                bank = c.get(-1, {}); mail = c.get("mail", {})
                items = [itemstr(v) for v in (bank.get("items", {}) or {}).values()] if isinstance(bank, dict) else []
                out.append(f"  bank ({bank.get('size') if isinstance(bank, dict) else '?'} slots): " + (", ".join(items) if items else "empty / not visited"))
                mi = [itemstr(v) for v in (mail.values() if isinstance(mail, dict) else [])]
                out.append("  mail: " + (", ".join(mi) if mi else "none recorded"))
                out.append("  currency: " + ", ".join(f"{k}={v}" for k, v in (c.get("currency", {}) or {}).items() if isinstance(v, int)))
    except Exception as e: out.append(f"BagBrother parse error: {e}")
    try:
        qc = sv("Questie").get("QuestieConfig", {}).get("char", {})
        for name, c in qc.items():
            comp = c.get("complete", {}) or {}
            out.append(f"Questie {name}: completed quests recorded: {len(comp)}")
            j = c.get("journey", {}) or {}
            ev = [j[k] for k in sorted(j) if isinstance(k, int)][-30:]
            for e in ev: out.append("  journey: " + ", ".join(f"{k}={v}" for k, v in e.items() if not isinstance(v, dict)))
    except Exception as e: out.append(f"Questie parse error: {e}")
    try:
        g = sv("Guidelime").get("GuidelimeData", {}).get("chars", {})
        for guid, c in g.items():
            prog = c.get("progress", {}) or {}
            out.append(f"Guidelime {guid}: current guide '{c.get('currentGuide')}', guides with progress: {len(prog)}")
    except Exception as e: out.append(f"Guidelime parse error: {e}")
    return out

def _fmt_time(t):
    try: return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError): return "?"

def _strip_age(now):
    """Seconds since the last decoded strip message. If the wall clock was set back (last_line is in the future) the monotonic
    clock answers instead, so a clock change can neither freeze the game as live nor make it look stale."""
    if not meta["last_line"]:
        return None
    age = now - meta["last_line"]
    if age < -1 and meta.get("last_mono"):
        age = time.monotonic() - meta["last_mono"]
    return max(0.0, age)

def session_snapshot(now=None):
    now = time.time() if now is None else now
    return presence.snapshot(now, _strip_age(now), uploader_alive_at=now)

def session_state_lines(now):
    return presmod.session_lines(session_snapshot(now), _fmt_time)

def events_tail_lines(n=20):
    rows = pipe.recent(n)
    return [srvevents.event_line(e) for e in rows] or ["(no events yet)"]

def render():
    now = time.time(); out = []
    age = now - meta["last_line"] if meta["last_line"] else -1
    out += ["=== WOW LIVE FEED ===", f"snapshot_utc: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%S}",
            f"seconds_since_last_game_update: {age:.0f}" + ("  (STALE: game closed or addon not logging)" if age < 0 or age > 15 else "  (live)"),
            f"source: screen capture of WoWFeed pixel strip", ""]
    out = out + GUIDE
    off = offline()                 # reads and parses other addons' files: never while holding the capture lock
    with lock:
        out += ["--- VITALS (health/power measured from bars; exact values are secret in-game) ---"] + vitals() + [""]
        out += ["--- SESSION (derived by bridge) ---"] + derive() + [""]
        out += ["--- SESSION_STATE (updated 0s ago) ---"] + session_state_lines(now) + [""]
        out += ["--- EVENTS_TAIL (updated 0s ago) ---"] + events_tail_lines() + [""]
        for s in ORDER + [k for k in sections if k not in ORDER]:
            if s in sections:
                t, lines = sections[s]
                out += [f"--- {s.upper()} (updated {now - t:.0f}s ago) ---"] + lines + [""]
        out += ["--- OFFLINE SAVEDVARIABLES (bank, mail, quest journey, guide progress) ---"] + off + [""]
        out += ["--- RECENT EVENTS (newest last) ---"] + list(events)
    return "\n".join(out) + "\n"

# The strip decoder lives in strip.py (pure code); the names stay importable from here.
from .strip import (Frame, CW, CH, BARW, BARGAP, CROP, cellv, white, cell_xy, magic, decode, find_hint, bars, BAR_TESTS, measure_bars)  # noqa: E402,F401


# ---- window capture and the screen mask live in the host layer (wowmentor/host): macOS uses CoreGraphics (lossless, reads the
# game window's own backing store, needs Screen Recording permission). The functions below are thin wrappers so the rest of the
# bridge, the doctor and the tests keep one stable place to call.
def frontmost_is_wow():
    """True if the front-most normal window belongs to WoW, False if another app's does, None if unknown (unsupported system,
    no windows reported, or any error). Needs no permission: only owner names are read."""
    try:
        return hostmod.get().capture.foreground_is_game()
    except Exception:
        return None

def wow_window():
    """The host's handle for the biggest WoW window (None if the game is not running)."""
    return hostmod.get().capture.find_game()

def grab_top(handle, rows):
    """A Frame of the top rows of the window, or None if the capture failed."""
    return hostmod.get().capture.grab_top(handle, rows)

def frames():
    cap = hostmod.get().capture
    wid = None; wid_t = 0.0; warned = 0.0
    while True:
        t0 = time.time()
        if not wid or t0 - wid_t > 15: wid, wid_t = cap.find_game(), t0
        if not wid:
            if t0 - warned > 30: warned = t0; print("game window not found", flush=True)
            time.sleep(3); continue
        im = cap.grab_top(wid, CROP)
        if im is None:
            wid = None
            if t0 - warned > 30: warned = t0; print("could not capture the game window (%s)" % hostmod.get().text["capture_failed_hint"], flush=True)
            time.sleep(2); continue
        yield im
        time.sleep(max(0, 0.1 - (time.time() - t0)))

def mask_row(px, y, W, H):
    """One row of the scene just below the strip, as base64 RGB every 4th pixel, so the mask can copy it and vanish into the picture."""
    if y >= H or not hasattr(px, "row"): return None
    import base64
    row = px.row(y)
    n = len(row) // 16
    if n < 2: return None
    out = bytearray(3 * n)
    out[0::3], out[1::3], out[2::3] = row[2:16 * n:16], row[1:16 * n:16], row[0:16 * n:16]
    return base64.b64encode(bytes(out))

def update_mask(h, W, px=None, H=0):
    """Tell the host's overlay helper where the strip is so it can paint over it on screen (window capture ignores overlays)."""
    m = hostmod.get().mask
    if not m.available(): return
    y0 = max(0, h[2] - 1)
    row_y = y0 + 2 * CH * h[0] + 2
    m.update((0, y0, W, 2 * CH * h[0] + 2), W, (lambda: mask_row(px, row_y, W, H)) if px is not None else None)

def tail():
    hint = None; last_id = None; warned = 0; last_ok = time.time()
    for im in frames():
        try:
            W, H = im.size; px = im
            res = None
            if hint and magic(px, W, H, hint): res = decode(px, W, H, hint)
            if not res:
                h2 = find_hint(px, W, H)
                if h2:
                    res = decode(px, W, H, h2)
                    if res: hint = h2
                    else: hint = None
                else: hint = None
            if hint:
                try: measure_bars(px, W, H, hint)
                except Exception: pass
                update_mask(hint, W, px, H)
            if res:
                last_ok = time.time()
                if res[0] != last_id:
                    last_id = res[0]
                    parts = res[1].split("\x1f", 4)
                    if len(parts) == 5: handle(parts[0], parts[1], int(parts[2]), int(parts[3]), parts[4])
            elif time.time() - last_ok > 30 and time.time() - warned > 30:
                warned = time.time(); print("no strip visible (game closed, or addon not drawing)", flush=True)
        except Exception as e:
            if time.time() - warned > 30: warned = time.time(); print("decode error:", e, flush=True)

def _why(e):
    """A short, secret-free description of a network error: the push URL contains the write secret, and urllib messages can quote it."""
    if isinstance(e, urllib.error.HTTPError): return "HTTP %s" % e.code
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, OSError): return type(e.reason).__name__
    return type(e).__name__

def push():
    last = 0; fails = 0
    while True:
        dirty.wait(timeout=5); dirty.clear()
        if time.time() - last < 1: time.sleep(1 - (time.time() - last))
        try:
            body = render().encode()
        except Exception as e:
            print("render error:", type(e).__name__, flush=True); time.sleep(2); continue
        try:
            with localnet.urlopen(urllib.request.Request(CFG["push_url"], body, {"Content-Type": "text/plain"}, method="POST"), timeout=10) as r:
                r.read()
            if fails: print("server reachable again", flush=True)
            last = time.time(); fails = 0
        except Exception as e:
            fails += 1
            if fails == 1 or fails % 30 == 0: print("push failed (%s), retrying; the server is down or the secrets differ" % _why(e), flush=True)
            time.sleep(min(30, 5 * fails))         # the snapshot is rebuilt on every attempt, so nothing queues up while the server is away

def supervise(name, fn, log=None, delay=1, max_delay=30):
    """Run fn in this thread forever. If it raises, say so (the exception class only) and start it again after a growing pause, so one
    bad frame or a broken file can never silently end a background thread. A normal return ends the loop."""
    log = log or (lambda m: print(m, flush=True))
    wait = delay
    while True:
        t0 = time.monotonic()
        try:
            return fn()
        except Exception as e:
            log("%s thread crashed (%s); restarting in %ds" % (name, type(e).__name__, wait))
        time.sleep(wait)
        wait = delay if time.monotonic() - t0 > 60 else min(max_delay, wait * 2)

def _kv():
    sm = sections.get("summary")
    return parse_kv(sm[1]) if sm else None

def _ui_lines():
    st = sections.get("ui_state")
    return st[1] if st else []

def _game_age():
    return _strip_age(time.time())

class Api(object):
    """Tiny JSON client for the server's /ui/<secret>/ routes. Errors become (None, None); URLs and secrets are never printed."""
    def __init__(self, base, timeout=8):
        self.base, self.timeout = base, timeout

    def post(self, route, obj):
        try:
            req = urllib.request.Request("%s/%s" % (self.base, route), json.dumps(obj, separators=(",", ":")).encode(),
                                         {"Content-Type": "application/json"}, method="POST")
            with localnet.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, None
            finally:
                e.close()
        except Exception:
            return None, None


def _afk_from_sections(kv):
    """The addon's afk flag: KV `afk=1`, else an `afk=1` token on any SESSION_ADDON line."""
    if kv and "afk" in kv: return kv
    sa = sections.get("session_addon")
    if sa:
        for l in sa[1]:
            for tok in l.split():
                if tok.startswith("afk="): return dict(kv or {}, afk=tok[4:])
    return kv

def presence_tick(now, api=None, window_fn=None, foreground_fn=None, push_session=True):
    """One presence evaluation: recompute the state, queue bridge events, push the session to the server (if api and push_session).
    Returns (snapshot, bridge events)."""
    win = (window_fn or wow_window)()
    fg = (foreground_fn or frontmost_is_wow)()
    age = _strip_age(now)
    evs = presence.update(now, age, bool(win), _afk_from_sections(_kv()), fg)
    if evs: pipe.add([dict(e, t=int(now)) for e in evs], received_at=now)
    snap = presence.snapshot(now, age, uploader_alive_at=now)
    snap["webhook"] = sender.status()
    if api is not None and (push_session or evs): api.post("session", snap)
    return snap, evs

def presence_loop(api):
    last = 0.0
    while True:
        now = time.time()
        try:
            due = now - last >= 4          # heartbeat: the server learns the Mac side is alive even when nothing changes
            presence_tick(now, api, push_session=due)
            if due: last = now
        except Exception as e:
            print("presence error:", type(e).__name__, flush=True)
        time.sleep(1)

def config_watch():
    """Apply webhook changes made by `wowmentor webhook set|off` without restarting the bridge."""
    from .. import config as cfgmod
    seen = None
    while True:
        try:
            m = os.path.getmtime(cfgmod.path("config.json"))
            if m != seen:
                seen = m
                c = cfgmod.peek(strict=True)
                if c is not None:                 # a half-edited or corrupt file keeps the current webhook settings
                    sender.configure(c.get("webhook_url"), c.get("webhook_secret"), c.get("webhook_auth"))
        except Exception:
            pass
        time.sleep(3)

def configure(cfg):
    """cfg is the wowmentor config dict (see wowmentor.config)."""
    from .. import config as cfgmod
    CFG.clear()
    root = cfgmod.server_root(cfg)           # server_url when the server runs elsewhere, else http://127.0.0.1:<port>
    CFG.update(
        wow_dir=cfg["wow_dir"], local_url=root, write_secret=cfg["write_secret"], ui_secret=cfg["ui_secret"],
        include_chat=bool(cfg.get("include_chat")), mask_path=os.path.join(cfgmod.path("bin"), "wowmask"), data_dir=cfgmod.data_dir(),
        push_url="%s/push/%s" % (root, cfg["write_secret"]), ui_base="%s/ui/%s" % (root, cfg["ui_secret"]),
    )


def run(cfg):
    ok, why = hostmod.get().capture.available()
    if not ok:
        print(why, flush=True)
        return
    configure(cfg)
    hostmod.get().mask.start(CFG)
    from . import guidesync, uisync
    uisync.configure(os.path.join(CFG["wow_dir"], "Interface", "AddOns"), CFG["local_url"])
    guidesync.configure(os.path.join(CFG["wow_dir"], "Interface", "AddOns"), CFG["local_url"], os.path.join(CFG["data_dir"], "guide-backups"))
    log = lambda m: print(m, flush=True)
    api = Api(CFG["ui_base"])
    sender.log = log
    sender.session_fn = lambda: presmod.webhook_session(session_snapshot())
    sender.configure(cfg.get("webhook_url"), cfg.get("webhook_secret"), cfg.get("webhook_auth"))
    pipe.post, pipe.on_stored, pipe.log = api.post, sender.enqueue, log
    def start(name, fn, *args):
        threading.Thread(target=supervise, args=(name, lambda: fn(*args), log), name=name, daemon=True).start()
    start("events", pipe.run)
    start("webhook", sender.run)
    start("presence", presence_loop, api)
    start("config", config_watch)
    start("uisync", lambda: uisync.UISync(CFG["ui_secret"], _kv, _ui_lines, _game_age, log=log).run())
    start("guidesync", lambda: guidesync.GuideSync(CFG["ui_secret"], _kv, _game_age, log=log).run())
    start("capture", tail)
    push()
