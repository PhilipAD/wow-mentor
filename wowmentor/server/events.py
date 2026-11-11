"""Event store, seen-NPC cache, coach questions and the session (presence) object.

The addon cannot reach the network, so its events travel over the pixel strip to the bridge, which pushes them here
(POST events/push). This module keeps the newest RING events with a monotonic id (`eid`), persists them in the data
directory, and serves them to the agent (GET events?since=). It also derives two small caches from the event stream so
the agent does not have to replay history: the seen-NPC table (from `npc_seen`) and the open coach questions (from
`coach_question`; closed by the `coach_answer` op sugar handled in app.py).

Thread safe: every public method takes the store lock. Pure standard library, Python 3.9 compatible.
"""
import json, re, sys, threading, time

from .. import config as cfgmod

RING = 500                    # events kept (oldest dropped)
MAX_EVENT_BYTES = 12_000      # serialized size of one event; larger ones are rejected, never truncated silently
MAX_PUSH = 200                # events accepted in one POST
MAX_NPCS = 1000
MAX_QUESTIONS = 50            # open and answered questions remembered
DEFAULT_LIMIT, MAX_LIMIT = 100, 500
TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
STATES = ("in_world", "loading", "afk", "logged_out", "game_closed")
SESSION_KEYS = ("state", "foreground", "last_login_at", "last_logout_at", "uploader_alive_at", "seconds_since_last_game_update",
                "state_since", "boot", "window_present", "webhook")
UPLOADER_STALE_S = 30         # the bridge heartbeats the session every few seconds; older than this means the Mac side is not running
REDACTED = "[redacted]"
MAX_DEPTH = 24                # nesting allowed inside an event's data (real events are 3 deep); a deeper one is a bomb, not a game event


def too_deep(obj, limit=MAX_DEPTH):
    """True if obj nests lists/dicts deeper than `limit`. Iterative, so a hostile document cannot blow the recursion limit."""
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if isinstance(o, dict):
            kids = o.values()
        elif isinstance(o, list):
            kids = o
        else:
            continue
        if d > limit:
            return True
        stack.extend((k, d + 1) for k in kids if isinstance(k, (dict, list)))
    return False


class StoreError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def dedupe_key(ev):
    """What makes a pushed event "the same one again" (a retry after a lost reply): the sender's own id (`src`, assigned by the bridge per
    process and event) or, for clients without one, the addon's (boot, seq). None means it cannot be deduplicated."""
    if ev.get("src"):
        return ("src", ev["src"])
    if ev.get("boot") is not None and ev.get("seq") is not None:
        return ("bs", ev["boot"], ev["seq"])
    return None


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def redact(obj, secrets):
    """Copy of obj with every occurrence of a secret (8+ chars) inside a string replaced. For the read-only token routes."""
    secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 8]
    if not secrets:
        return obj

    def walk(o):
        if isinstance(o, str):
            for s in secrets:
                if s in o:
                    o = o.replace(s, REDACTED)
            return o
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, dict):
            return {(walk(k) if isinstance(k, str) else k): walk(v) for k, v in o.items()}
        return o
    return walk(obj)


def event_line(ev, limit=220):
    """One human line for an event: `#412 12:03:20 death {"zone":...}` (used in the brief and the EVENTS_TAIL feed section)."""
    try:
        hhmmss = time.strftime("%H:%M:%S", time.localtime(ev.get("t") or 0))
    except (OverflowError, OSError, ValueError):
        hhmmss = "??:??:??"
    data = json.dumps(ev.get("data", {}), separators=(",", ":"), ensure_ascii=True, sort_keys=True)
    if len(data) > limit:
        data = data[:limit - 3] + "..."
    eid = ev.get("eid")
    return ("#%s %s %s %s" % ("-" if eid is None else eid, hhmmss, ev.get("type", "?"), data if data != "{}" else "")).rstrip()


def _qs(q, name, default=None):
    v = (q.get(name) or [default])[0]
    return v


def _int_param(q, name, default, lo, hi):
    raw = _qs(q, name)
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise StoreError(["%s must be an integer" % name])
    return max(lo, min(hi, n))


MAX_WAIT_S = 25                 # longest long-poll (events?since=N&wait=25)


class EventStore(object):
    def __init__(self, path=None, ring=RING, clock=time.time):
        self.path, self.ring, self.clock = path, ring, clock
        self.lock = threading.RLock()
        self.events = []              # newest last
        self.next_eid = 1
        self.keys = set()             # dedupe keys of the events in the ring: makes a retried push idempotent
        self.npcs = {}                # key -> record
        self.questions = {}           # qid -> record (insertion ordered on py3.7+)
        self.session = None
        self.session_t = 0.0
        self.save_failures = 0
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        if not self.path:
            return
        d = cfgmod.read_json(self.path)          # corrupt file: kept as events.json.bak, start empty
        if not isinstance(d, dict):
            if d is not None:
                cfgmod.set_aside(self.path)
            return
        evs = [e for e in d.get("events", []) if isinstance(e, dict) and isinstance(e.get("eid"), int) and isinstance(e.get("type"), str)]
        self.events = evs[-self.ring:]
        self.keys = {k for k in (dedupe_key(e) for e in self.events) if k}
        top = max([e["eid"] for e in self.events] + [0])
        nxt = d.get("next_eid")
        self.next_eid = max(top + 1, nxt if isinstance(nxt, int) else 1)
        self.npcs = {n["key"]: n for n in d.get("npcs", []) if isinstance(n, dict) and isinstance(n.get("key"), str)}
        self.questions = {}
        for qn in d.get("questions", []):
            if isinstance(qn, dict) and isinstance(qn.get("id"), str):
                self.questions[qn["id"]] = qn

    def save(self):
        if not self.path:
            return
        d = {"v": 1, "next_eid": self.next_eid, "events": self.events, "npcs": list(self.npcs.values()), "questions": list(self.questions.values())}
        try:
            cfgmod.write_json_atomic(self.path, d, separators=(",", ":"))
            self.save_failures = 0
        except OSError as e:
            # a full disk or a vanished data dir must not take the server down; the ring stays in memory and the next push retries
            self.save_failures += 1
            if self.save_failures in (1, 50):
                print("events: could not save %s (%s)" % ("events.json", type(e).__name__), file=sys.stderr, flush=True)

    # ------------------------------------------------------------ writes
    def push(self, events, boot=None, received_at=None):
        """Store events. Returns (stored envelopes, rejected [{index, errors}], duplicates). Raises StoreError for a bad body."""
        if not isinstance(events, list):
            raise StoreError(["events must be a list"])
        if len(events) > MAX_PUSH:
            raise StoreError(["at most %d events per push" % MAX_PUSH])
        now = self.clock()
        stored, rejected, dups = [], [], 0
        with self.lock:
            for i, raw in enumerate(events):
                ev, err = self._normalize(raw, boot, received_at, now)
                if err:
                    rejected.append({"index": i, "errors": [err]})
                    continue
                key = dedupe_key(ev)
                if key and key in self.keys:
                    dups += 1
                    continue
                ev["eid"] = self.next_eid
                self.next_eid += 1
                ordered = {"eid": ev["eid"], "t": ev["t"], "received_at": ev["received_at"], "boot": ev.get("boot"), "type": ev["type"], "data": ev["data"]}
                if ev.get("seq") is not None:
                    ordered["seq"] = ev["seq"]
                if ev.get("src"):
                    ordered["src"] = ev["src"]
                self.events.append(ordered)
                if key:
                    self.keys.add(key)
                stored.append(ordered)
                self._derive(ordered)
            if stored:
                if len(self.events) > self.ring:
                    for old in self.events[:-self.ring]:
                        self.keys.discard(dedupe_key(old))
                    self.events = self.events[-self.ring:]
                self.save()
        return stored, rejected, dups

    def _normalize(self, raw, boot, received_at, now):
        if not isinstance(raw, dict):
            return None, "event must be an object"
        typ = raw.get("type")
        if not isinstance(typ, str) or not TYPE_RE.match(typ):
            return None, "type must be lowercase letters, digits and _ (max 40)"
        data = raw.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return None, "data must be an object"
        if too_deep(data):
            return None, "data is nested deeper than %d levels" % MAX_DEPTH
        t = raw.get("t")
        if not _is_num(t) or t < 0 or t > 4e9:
            t = int(now)
        seq = raw.get("seq")
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
            seq = None
        b = raw.get("boot", boot)
        if isinstance(b, bool) or not isinstance(b, (int, str)) or (isinstance(b, str) and len(b) > 40):
            b = None
        r = raw.get("received_at", received_at)
        if not _is_num(r) or r < 0 or r > 4e9:
            r = now
        src = raw.get("src")
        if not isinstance(src, str) or not src or len(src) > 60:
            src = None
        ev = {"t": t, "received_at": round(float(r), 3), "boot": b, "type": typ, "data": data, "seq": seq, "src": src}
        try:
            size = len(json.dumps(ev, separators=(",", ":")))
        except (TypeError, ValueError, RecursionError):
            return None, "event is not JSON serializable"
        if size > MAX_EVENT_BYTES:
            return None, "event is %d bytes; the limit is %d" % (size, MAX_EVENT_BYTES)
        return ev, None

    # ------------------------------------------------------------ derived caches
    def _derive(self, ev):
        if ev["type"] == "npc_seen":
            self._note_npc(ev)
        elif ev["type"] == "coach_question":
            self._note_question(ev)

    def _note_npc(self, ev):
        d = ev["data"]
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            return
        name = name.strip()[:80]
        nid = d.get("id")
        if isinstance(nid, str) and nid.isdigit():
            nid = int(nid)
        if isinstance(nid, bool) or not isinstance(nid, int):
            nid = None
        key = ("id:%d" % nid) if nid is not None else ("name:" + name.lower())
        rec = self.npcs.get(key)
        if rec is None:
            rec = {"key": key, "name": name, "id": nid, "map": None, "x": None, "y": None, "kind": None, "kinds": [], "count": 0, "first_seen": ev["t"], "last_seen": ev["t"]}
            self.npcs[key] = rec
        newer = ev["t"] >= rec["last_seen"]           # events can arrive late (a retried batch): an older sighting never moves the NPC back
        rec["count"] += 1
        rec["name"] = name
        rec["last_seen"] = max(rec["last_seen"], ev["t"])
        mp = d.get("map")
        if isinstance(mp, (int, str)) and not isinstance(mp, bool) and (newer or rec["map"] is None):
            rec["map"] = mp
        for axis in ("x", "y"):
            v = d.get(axis)
            if _is_num(v) and (newer or rec[axis] is None):
                rec[axis] = round(float(v), 2)
        kind = d.get("kind")
        if isinstance(kind, str) and kind:
            kind = kind[:24]
            if newer or rec["kind"] is None:
                rec["kind"] = kind
            if kind not in rec["kinds"] and len(rec["kinds"]) < 8:
                rec["kinds"].append(kind)
        if len(self.npcs) > MAX_NPCS:
            for k, _ in sorted(self.npcs.items(), key=lambda kv: kv[1]["last_seen"])[:len(self.npcs) - MAX_NPCS]:
                del self.npcs[k]

    def _note_question(self, ev):
        d = ev["data"]
        text = d.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        raw_id = d.get("id")
        addon_id = str(raw_id)[:40] if (isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) and str(raw_id)) else None
        qid = addon_id or ("e%d" % ev["eid"])
        old = self.questions.get(qid)
        if old is not None:
            # a reload restarted the addon's counter (or a stale boot tag hid it): keep both questions apart, never overwrite one
            qid = (("%s-%s" % (ev.get("boot"), qid)) if old.get("boot") != ev.get("boot") else ("%s-e%d" % (qid, ev["eid"])))[:60]
        self.questions[qid] = {"id": qid, "addon_id": addon_id, "text": text.strip()[:400], "eid": ev["eid"], "asked_at": ev["t"], "boot": ev.get("boot"),
                               "status": "open", "answered_at": None, "answer": None}
        if len(self.questions) > MAX_QUESTIONS:
            done = [k for k, v in self.questions.items() if v["status"] != "open"]
            for k in (done + list(self.questions))[:len(self.questions) - MAX_QUESTIONS]:
                self.questions.pop(k, None)

    # ------------------------------------------------------------ queries
    def last_eid(self):
        with self.lock:
            return self.next_eid - 1

    def query(self, since=None, types=None, limit=DEFAULT_LIMIT):
        """since=None -> the newest `limit` events (oldest first). since=N -> events with eid > N, oldest first, at most `limit`."""
        limit = max(1, min(MAX_LIMIT, int(limit)))
        with self.lock:
            last = self.next_eid - 1
            oldest = self.events[0]["eid"] if self.events else None
            wanted = set(types) if types else None
            pool = self.events if since is None else [e for e in self.events if e["eid"] > since]
            if wanted:
                pool = [e for e in pool if e["type"] in wanted]
            if since is None:
                out, nxt = pool[-limit:], last
            else:
                out = pool[:limit]
                nxt = out[-1]["eid"] if len(pool) > len(out) else last
            res = {"events": [dict(e) for e in out], "next": nxt, "last_eid": last, "oldest_eid": oldest}
            if since is not None:
                if since > last:
                    res["reset"] = True            # the caller's cursor is ahead of this server (data was reset): start over from `next`
                elif oldest is not None and since < oldest - 1:
                    res["gap"] = True              # events between the cursor and oldest_eid were dropped from the ring buffer
            return res

    def npc_list(self, name=None):
        with self.lock:
            rows = sorted(self.npcs.values(), key=lambda n: n["last_seen"], reverse=True)
            if name:
                low = name.lower()
                rows = [n for n in rows if low in n["name"].lower()]
            return [{k: v for k, v in n.items() if k != "key"} for n in rows]

    def open_questions(self, include_all=False):
        with self.lock:
            return [dict(v) for v in self.questions.values() if include_all or v["status"] == "open"]

    def find_open_question(self, ident):
        """The qid of an open question: exact id, else the addon's own id when exactly one open question has it. None if unknown."""
        ident = str(ident)
        with self.lock:
            q = self.questions.get(ident)
            if q is not None and q["status"] == "open":
                return ident
            hits = [k for k, v in self.questions.items() if v["status"] == "open" and v.get("addon_id") == ident]
            return hits[0] if len(hits) == 1 else None

    def close_question(self, qid, answer=None, status="answered"):
        with self.lock:
            q = self.questions.get(qid)
            if q is None or q["status"] != "open":
                return False
            q["status"], q["answered_at"] = status, self.clock()
            q["answer"] = (answer or "")[:400] or None
            self.save()
            return True

    # ------------------------------------------------------------ session
    def set_session(self, obj):
        if not isinstance(obj, dict):
            raise StoreError(["session must be an object"])
        errs = []
        out = {}
        for k, v in obj.items():
            if k not in SESSION_KEYS:
                continue
            if k == "state" and v not in STATES:
                errs.append("state must be one of %s" % ", ".join(STATES))
            elif k == "foreground" and v not in (True, False, "unknown"):
                errs.append("foreground must be true, false or \"unknown\"")
            elif k in ("last_login_at", "last_logout_at", "uploader_alive_at", "seconds_since_last_game_update", "state_since") and v is not None and not _is_num(v):
                errs.append("%s must be a number or null" % k)
            elif k == "webhook" and not isinstance(v, dict):
                errs.append("webhook must be an object")
            out[k] = v
        if "state" not in out:
            errs.append("state is required")
        if errs:
            raise StoreError(errs)
        with self.lock:
            self.session, self.session_t = out, self.clock()

    def get_session(self):
        with self.lock:
            if not self.session:
                return {"known": False, "state": "unknown", "uploader_stale": True}
            now = self.clock()
            s = dict(self.session)
            s["known"] = True
            s["pushed_age_s"] = round(now - self.session_t, 1)
            alive = s.get("uploader_alive_at")
            s["uploader_stale"] = (now - self.session_t) > UPLOADER_STALE_S or (_is_num(alive) and now - alive > UPLOADER_STALE_S)
            return s

    # ------------------------------------------------------------ HTTP routes (called by app.py)
    def read_events(self, q, secrets=()):
        """Read-only view used by GET /<read-token>/events: same shape, secrets scrubbed."""
        code, obj = self.handle("GET", "events", q, None)
        return code, redact(obj, secrets)

    def handle(self, method, route, q, body):
        """(status, json) for events, events/push, npcs, session, coach/questions; None if the route is not ours."""
        try:
            if route == "events" and method == "GET":
                since = _int_param(q, "since", None, 0, 2 ** 53)
                limit = _int_param(q, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
                types = [t for t in (_qs(q, "types", "") or "").split(",") if t.strip()]
                for t in types:
                    if not TYPE_RE.match(t.strip()):
                        raise StoreError(["types: %r is not an event type" % t[:40]])
                res = self.query(since, [t.strip() for t in types], limit)
                wait = _int_param(q, "wait", 0, 0, MAX_WAIT_S)
                if wait and since is not None:              # long-poll: hold the request open until something new arrives (or `wait` seconds pass)
                    deadline = self.clock() + wait
                    while not res["events"] and not res.get("reset") and self.clock() < deadline:
                        time.sleep(0.4)
                        res = self.query(since, [t.strip() for t in types], limit)
                res["ok"] = True
                return 200, res
            if route == "events/push" and method == "POST":
                if not isinstance(body, dict):
                    raise StoreError(['body must be {"boot": ..., "events": [...]}'])
                stored, rejected, dups = self.push(body.get("events"), body.get("boot"), body.get("received_at"))
                return 200, {"ok": True, "stored": stored, "rejected": rejected, "duplicates": dups, "next": self.last_eid()}
            if route == "npcs" and method == "GET":
                rows = self.npc_list(_qs(q, "name"))
                return 200, {"ok": True, "count": len(rows), "npcs": rows}
            if route == "session" and method == "GET":
                s = self.get_session()
                s["ok"] = True
                return 200, s
            if route == "session" and method == "POST":
                self.set_session(body)
                return 200, {"ok": True}
            if route == "coach/questions" and method in ("GET", "POST"):
                dismissed = []
                if method == "POST" and isinstance(body, dict) and body.get("dismiss") is not None:
                    ids = body["dismiss"] if isinstance(body["dismiss"], list) else [body["dismiss"]]
                    for ident in ids[:50]:
                        qid = self.find_open_question(ident)
                        if qid and self.close_question(qid, status="dismissed"):
                            dismissed.append(qid)
                rows = self.open_questions(include_all=_qs(q, "all") in ("1", "true"))
                res = {"ok": True, "count": len(rows), "questions": rows}
                if dismissed:
                    res["dismissed"] = dismissed
                return 200, res
        except StoreError as e:
            return 400, {"ok": False, "errors": e.errors}
        return None
