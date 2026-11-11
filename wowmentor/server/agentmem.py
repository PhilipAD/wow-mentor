"""Persistent notes for the mentor agent and the one-shot "brief" that rebuilds its context after it forgets.

The overlay, the guide edits and the game feed already live on the server; what an agent loses when its context resets is
what it decided and why. `memory` keeps that (a running summary plus short dated notes), and `brief` stitches memory,
the overlay, the guide-editing state and a trimmed copy of the game feed into one page an agent can read first.
Pure stdlib; called by feed_server.py with the UI lock held.
"""
import base64, json, re, sys, time

from .. import config as cfgmod
from . import events as eventsmod

MAX_NOTES = 200                 # the oldest note is dropped past this; the reply warns when the list is nearly full
MAX_NOTE = 600
MAX_SUMMARY = 3000
SECTIONS = ["SUMMARY", "SESSION", "FAST", "VITALS", "PROFESSIONS", "EQUIPMENT", "BAG_VALUE", "QUESTS", "GUIDES", "TALENTS_TRAITS", "PVP", "UI_STATE", "GROUP"]


class MemoryError_(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class AgentMemory:
    def __init__(self, path):
        self.path = path
        self.d = {"notes": [], "summary": "", "seq": 0, "updated": 0}
        d = cfgmod.read_json(path)          # corrupt file: kept as .bak, start empty
        if isinstance(d, dict) and isinstance(d.get("notes", []), list) and isinstance(d.get("summary", ""), str):
            self.d.update(d)
        elif d is not None:
            cfgmod.set_aside(path)

    def save(self):
        try:
            cfgmod.write_json_atomic(self.path, self.d)
        except OSError as e:           # full disk: memory still holds the notes; the next write retries
            print("memory: could not save (%s)" % type(e).__name__, file=sys.stderr, flush=True)

    def handle(self, method, route, q, body):
        try:
            if route == "memory":
                return 200, {"ok": True, **self.d}
            if route == "memory/ops":
                if method == "GET":
                    try:
                        raw = q["b"][0]
                        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                    except Exception as e:
                        raise MemoryError_(["GET /memory/ops needs ?b=<base64url JSON>: %s" % e])
                ops = body if isinstance(body, list) else [body]
                for op in ops:
                    self._apply(op)
                self.d["updated"] = time.time()
                self.save()
                res = {"ok": True, "notes": len(self.d["notes"]), "limit": MAX_NOTES, "summary_chars": len(self.d["summary"])}
                if len(self.d["notes"]) >= MAX_NOTES - 10:
                    res["warning"] = "memory holds %d of %d notes: the oldest is dropped on the next add. Fold what matters into `wowui memory summary` and remove stale notes." % (len(self.d["notes"]), MAX_NOTES)
                return 200, res
        except MemoryError_ as e:
            return 400, {"ok": False, "errors": e.errors}
        return 404, {"ok": False, "errors": ["unknown memory route"]}

    def _apply(self, op):
        if not isinstance(op, dict) or "op" not in op:
            raise MemoryError_(['each op is an object with "op": add, summary, remove, clear'])
        name = op["op"]
        if name == "add":
            text = op.get("text")
            if not isinstance(text, str) or not text.strip():
                raise MemoryError_(["add.text: required"])
            self.d["seq"] += 1
            note = {"id": self.d["seq"], "t": time.time(), "text": text.strip()[:MAX_NOTE]}
            if isinstance(op.get("tag"), str):
                note["tag"] = op["tag"][:20]
            self.d["notes"] = (self.d["notes"] + [note])[-MAX_NOTES:]
        elif name == "summary":
            text = op.get("text")
            if not isinstance(text, str):
                raise MemoryError_(["summary.text: required (the running summary replaces the old one)"])
            self.d["summary"] = text.strip()[:MAX_SUMMARY]
        elif name == "remove":
            n = len(self.d["notes"])
            self.d["notes"] = [x for x in self.d["notes"] if x["id"] != op.get("id")]
            if len(self.d["notes"]) == n:
                raise MemoryError_(["remove: no note with id %r" % op.get("id")])
        elif name == "clear":
            self.d["notes"], self.d["summary"] = [], ""
        else:
            raise MemoryError_(["unknown op %r (add, summary, remove, clear)" % name])


def feed_sections(body):
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    out, cur, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^--- ([A-Z_]+)(?: \(.*)? ---$", line)
        if m:
            if cur:
                out[cur] = buf
            cur, buf = m.group(1), []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = buf
    head = [l for l in text.splitlines()[:6] if l.startswith("seconds_since_last_game_update") or l.startswith("snapshot_utc")]
    return out, head


def _when_text(w):
    if not isinstance(w, dict):
        return "?"
    if w.get("type") in ("all", "any"):
        return (" %s " % w["type"]).join(_when_text(x) for x in w.get("of", []))
    extra = ",".join("%s=%s" % (k, v) for k, v in sorted(w.items()) if k != "type")
    return w.get("type", "?") + ("(%s)" % extra if extra else "")


def rule_summary(r):
    acts = ", ".join(a.get("do", "?") + (":" + str(a.get("text", ""))[:40] if a.get("text") else "") for a in r.get("then", []))
    flags = []
    if r.get("once"):
        flags.append("once")
    if r.get("enabled") is False:
        flags.append("DISABLED")
    return "%s: when %s -> %s (cooldown %ss%s)" % (r.get("id"), _when_text(r.get("when")), acts, r.get("cooldown_s", 60), ", " + ", ".join(flags) if flags else "")


def session_lines(s):
    if not s or not s.get("known", True):
        return ["(no session report yet: the Mac bridge is not running or has not reported)"]
    def f(v):
        return "unknown" if v is None else v
    lines = ["state=%s foreground=%s" % (s.get("state"), f(s.get("foreground"))),
             "last_login_at=%s last_logout_at=%s" % (_ts(s.get("last_login_at")), _ts(s.get("last_logout_at"))),
             "bridge heartbeat: %s%s" % (_ts(s.get("uploader_alive_at")), "  (STALE: the Mac side is not reporting)" if s.get("uploader_stale") else "")]
    return lines


def _ts(t):
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return "never"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t))


def build_brief(mem, doc, tug_status, feed_body, base, feed_age, session=None, events=None, questions=None):
    sec, head = feed_sections(feed_body)
    L = ["# WoW mentor brief: read this first after any context loss", ""]
    L.append("Endpoint: %s   (skill: GET %s/skill?t=<n>  |  CLI: wowui guide, wowui tug --help, wowui memory --help)" % (base, base))
    L.append("You are the player's live mentor. Your operating skill and the player's profile are in the skill page; this page is the current state.")
    L += ["", "## Your running summary (you wrote this)", mem.d["summary"] or "(empty: write one with `wowui memory summary \"...\"` as soon as you have decided a plan)"]
    L += ["", "## Your notes (newest last)"]
    notes = mem.d["notes"][-25:]
    if notes:
        for n in notes:
            L.append("- #%d %s%s %s" % (n["id"], time.strftime("%m-%d %H:%M", time.localtime(n["t"])), " [%s]" % n["tag"] if n.get("tag") else "", n["text"]))
    else:
        L.append("(none yet: `wowui memory add \"decision or fact worth keeping\"`)")
    L += ["", "## What is on the player's screen (overlay rev %s)" % doc.rev]
    if not doc.panels and not doc.waypoints:
        L.append("(nothing: build a plan with `wowui plan`)")
    for p in doc.panels:
        L.append("- panel %s: %s" % (p["id"], p.get("title", "")))
        for c in p.get("children", []):
            if c.get("type") == "checklist":
                for it in c["items"]:
                    L.append("    [%s] %s: %s" % ("x" if it.get("done") else " ", it["id"], it["text"][:90]))
            elif c.get("type") in ("text", "callout", "heading"):
                L.append("    %s: %s" % (c["type"], c.get("text", "")[:100]))
    for w in doc.waypoints:
        L.append("- waypoint %s: map %s %.1f,%.1f %s" % (w["id"], w["map"], w["x"], w["y"], w.get("title", "")))
    L += ["", "## Session (is the player in the game right now?)"] + session_lines(session)
    L += ["", "## Active rules (run instantly in the game; `wowui rule list`)"]
    L += ["- " + rule_summary(r) for r in doc.rules] or ["(none: `wowui rule preset bags-full` and friends add the common ones)"]
    L += ["", "## Open coach questions (the player asked with /coach; answer with `wowui coach \"...\" --answer <id>`)"]
    L += ["- [%s] %s (asked %s)" % (q["id"], q["text"][:200], _ts(q.get("asked_at"))) for q in questions or []] or ["(none)"]
    L += ["", "## Last 10 events (`wowui events --since N` for more; newest last)"]
    L += [eventsmod.event_line(e) for e in (events or [])[-10:]] or ["(no events yet)"]
    L += ["", "## Guide editing"]
    L.append("pending edits: %s, reload needed: %s, bridge age: %ss, guides cached: %s" % (tug_status["pending"], tug_status["reload"]["needed"], tug_status["bridge_age_s"], tug_status["guides"]))
    for r in tug_status["recent"][-5:]:
        L.append("- #%s %s %s %s" % (r["id"], r["guide"], "ok" if r["ok"] else "FAILED " + "; ".join(r.get("errors", [])), r.get("note", "")))
    L += ["", "## The player now (trimmed game feed, %s)" % (("%.0fs old" % feed_age) if feed_age is not None else "no data")]
    L += head
    for name in SECTIONS:
        if name in sec:
            L.append("--- %s" % name)
            body = [l for l in sec[name] if l.strip()]
            text = "\n".join(body)
            L.append(text[:1800] + ("\n...(trimmed)" if len(text) > 1800 else ""))
    L += ["", "## Do next",
          "1. Read your summary and notes above, then the feed lines. 2. Fix anything on screen that no longer matches the game (done quests, wrong level).",
          "3. Keep the overlay short (3 to 7 tasks) and the guide edits surgical (`wowui tug show` first). 4. After each decision: `wowui memory add \"...\"`;",
          "   rewrite the summary when the plan changes. 5. The player's own panel with skills, professions and gear is built in (`wowui builtin me off` hides it)."]
    return "\n".join(L) + "\n"
