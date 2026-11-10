"""Server side of live guide editing.

The Mac bridge uploads every guide file (a cache the agent can read), the agent sends edits, the server validates
them against the cache (dry run) and queues them, the bridge applies the queue to the real files and reports back.
After a successful apply the server puts a "type /reload" notice on the player's overlay and clears it when the
bridge sees the game reload. Pure stdlib; called by feed_server.py with the UI lock held.
"""
import base64, json, re, sys, time

from .. import config as cfgmod
from . import tugdoc


class TugAPI:
    def __init__(self, path, overlay):
        self.path = path
        self.overlay = overlay          # callable(list of overlay ops, panel_exists_name=None) -> bool
        self.s = {"guides": {}, "queue": [], "seq": 0, "results": [], "versions": {}, "reload": {"needed": False, "edits": [], "boot": None, "since": 0},
                  "sync_t": 0.0, "bridge_t": 0.0}
        d = cfgmod.read_json(path)          # corrupt file: kept as .bak, start empty
        if isinstance(d, dict):
            try:
                self.s.update(d)
                for k, kind in (("guides", dict), ("queue", list), ("results", list), ("versions", dict), ("reload", dict)):
                    if not isinstance(self.s[k], kind):
                        raise ValueError(k)
            except (ValueError, TypeError):
                cfgmod.set_aside(path)
                self.s.update({"guides": {}, "queue": [], "seq": 0, "results": [], "versions": {}, "reload": {"needed": False, "edits": [], "boot": None, "since": 0}})
        elif d is not None:
            cfgmod.set_aside(path)

    def save(self):
        try:
            cfgmod.write_json_atomic(self.path, self.s)
        except OSError as e:           # full disk: the in-memory state is still right; the next change retries
            print("tug: could not save (%s)" % type(e).__name__, file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- helpers
    def resolve(self, name):
        g = self.s["guides"]
        if name in g:
            return name
        if not name:
            raise tugdoc.TugError(["name: required (see `wowui tug list`)"])
        low = name.lower()
        exact = [k for k in g if k.lower() == low or k.lower() == low + ".lua"]
        if exact:
            return exact[0]
        hits = [k for k in g if low in k.lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise tugdoc.TugError(["no guide matches %r (try `wowui tug list`)" % name])
        raise tugdoc.TugError(["%r matches %d guides: %s" % (name, len(hits), ", ".join(sorted(hits)[:8]))])

    def lines_of(self, name):
        return tugdoc.split_file(self.s["guides"][name]["text"])[1]

    def guide_title(self, lines):
        for l in lines:
            m = re.match(r"^\[N(?!X)\s*(.*?)\]", l)
            if m:
                return m.group(1)
        return ""

    # ---------------------------------------------------------------- reads
    def handle(self, method, route, q, body):
        sub = route.split("/", 1)[1] if "/" in route else ""
        try:
            fn = getattr(self, "r_" + sub.replace("-", "_"), None)
            if not fn:
                return 404, {"ok": False, "errors": ["unknown tug route %r" % sub]}
            return fn(method, q, body)
        except tugdoc.TugError as e:
            return 400, {"ok": False, "errors": e.errors}

    def r_list(self, method, q, body):
        out = []
        for name, g in sorted(self.s["guides"].items()):
            lines = self.lines_of(name)
            nx = next((re.match(r"^\[NX\s*(.*?)\]", l).group(1) for l in lines if re.match(r"^\[NX", l)), "")
            out.append({"name": name, "title": self.guide_title(lines), "next": nx, "lines": len(lines), "bytes": len(g["text"]), "hash": g["hash"]})
        f = (q.get("filter") or [""])[0].lower()
        if f:
            out = [o for o in out if f in o["name"].lower() or f in o["title"].lower()]
        return 200, {"ok": True, "count": len(out), "guides": out, "synced_age_s": round(time.time() - self.s["sync_t"], 1) if self.s["sync_t"] else None}

    def r_show(self, method, q, body):
        name = self.resolve((q.get("name") or [""])[0])
        lines = self.lines_of(name)
        try:
            a = int((q.get("from") or ["1"])[0]); b = int((q.get("to") or [str(len(lines))])[0])
        except ValueError:
            raise tugdoc.TugError(["from and to must be integers"])
        return 200, {"ok": True, "name": name, "title": self.guide_title(lines), "total": len(lines), "hash": self.s["guides"][name]["hash"],
                     "from": a, "to": min(b, len(lines)), "text": tugdoc.numbered(lines, a, b)}

    def r_search(self, method, q, body):
        needle = (q.get("q") or [""])[0]
        if not needle:
            raise tugdoc.TugError(["q: required"])
        only = (q.get("guide") or [""])[0]
        hits = []
        for name in sorted(self.s["guides"]):
            if only and only.lower() not in name.lower():
                continue
            for i, l in enumerate(self.lines_of(name), 1):
                if needle.lower() in l.lower():
                    hits.append({"guide": name, "line": i, "text": l[:200]})
        return 200, {"ok": True, "count": len(hits), "hits": hits[:80], "truncated": len(hits) > 80}

    def r_route(self, method, q, body):
        by_title = {self.guide_title(self.lines_of(n)): n for n in self.s["guides"]}
        start = (q.get("start") or [""])[0]
        if not start:
            def first_num(n):
                m = re.search(r"(\d+)", n)
                return (int(m.group(1)) if m else 999, n)
            start = min(self.s["guides"], key=first_num) if self.s["guides"] else ""
        cur = self.resolve(start)
        chain, seen = [], set()
        while cur and cur not in seen and len(chain) < 80:
            seen.add(cur)
            lines = self.lines_of(cur)
            nx = next((re.match(r"^\[NX\s*(.*?)\]", l).group(1) for l in lines if re.match(r"^\[NX", l)), "")
            chain.append({"name": cur, "title": self.guide_title(lines), "lines": len(lines), "next": nx})
            cur = by_title.get(nx)
        return 200, {"ok": True, "count": len(chain), "route": chain}

    def r_status(self, method, q, body):
        s = self.s
        pending = [e for e in s["queue"] if e["status"] == "queued"]
        return 200, {"ok": True, "guides": len(s["guides"]), "pending": len(pending), "recent": s["results"][-10:], "reload": s["reload"],
                     "bridge_age_s": round(time.time() - s["bridge_t"], 1) if s["bridge_t"] else None}

    def r_history(self, method, q, body):
        name = self.resolve((q.get("name") or [""])[0])
        return 200, {"ok": True, "name": name, "versions": self.s["versions"].get(name, [])}

    # ---------------------------------------------------------------- agent edits
    def r_ops(self, method, q, body):
        if method == "GET":
            try:
                raw = q["b"][0]
                body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            except Exception as e:
                raise tugdoc.TugError(["GET /tug/ops needs ?b=<base64url JSON>: %s" % e])
        edits = body.get("edits") if isinstance(body, dict) and "edits" in body else [body]
        if not isinstance(edits, list) or not edits or not all(isinstance(e, dict) for e in edits):
            raise tugdoc.TugError(['body must be {"guide": ..., "ops": [...]} or {"edits": [that, ...]}'])
        trial, results = {}, []
        for k, e in enumerate(edits):
            name = self.resolve(e.get("guide", ""))
            if "restore" in e:
                if str(e["restore"]) not in [v["id"] for v in self.s["versions"].get(name, [])] + ["orig"]:
                    raise tugdoc.TugError(["edit %d: restore %r is not a known version (see `wowui tug history`)" % (k, e["restore"])])
                results.append((name, None, {"restore": str(e["restore"])}, e.get("note", ""), ["restore " + str(e["restore"])], [], None))
                continue
            text = trial.get(name, self.s["guides"][name]["text"])
            try:
                out, summary, warns, new = tugdoc.apply_to_text(text, e.get("ops"))
            except tugdoc.TugError as err:
                raise tugdoc.TugError(["edit %d (%s): %s" % (k, name, m) for m in err.errors])
            old = tugdoc.split_file(text)[1]
            trial[name] = out
            results.append((name, e["ops"], None, e.get("note", ""), summary, warns, tugdoc.context(None, summary, new, old)))
        replies = []
        for name, ops, restore, note, summary, warns, ctx in results:
            self.s["seq"] += 1
            entry = {"id": self.s["seq"], "guide": name, "ops": ops, "restore": restore, "note": str(note)[:200], "t": time.time(), "status": "queued", "summary": summary}
            self.s["queue"].append(entry)
            if name in trial:
                self.s["guides"][name]["text"] = trial[name]
                self.s["guides"][name]["hash"] = tugdoc.sha(trial[name])
            replies.append({"id": entry["id"], "guide": name, "summary": summary, "warnings": warns, "preview": ctx})
        self.s["queue"] = self.s["queue"][-200:]
        self.save()
        return 200, {"ok": True, "queued": replies, "note": "applied to the guide file within a few seconds; the player then gets a /reload prompt"}

    # ---------------------------------------------------------------- bridge
    def r_file(self, method, q, body):
        self.s["bridge_t"] = time.time()
        name, text = body.get("name"), body.get("text")
        if not isinstance(name, str) or not isinstance(text, str) or len(text) > 200_000:
            raise tugdoc.TugError(["name and text required"])
        self.s["guides"][name] = {"text": text, "hash": tugdoc.sha(text), "t": time.time()}
        if isinstance(body.get("versions"), list):
            self.s["versions"][name] = body["versions"][-30:]
        self.s["sync_t"] = time.time()
        self.save()
        return 200, {"ok": True}

    def r_pending(self, method, q, body):
        self.s["bridge_t"] = time.time()
        return 200, {"ok": True, "queue": [e for e in self.s["queue"] if e["status"] == "queued"][:20]}

    def r_applied(self, method, q, body):
        self.s["bridge_t"] = time.time()
        eid = body.get("id")
        entry = next((e for e in self.s["queue"] if e["id"] == eid), None)
        if not entry:
            raise tugdoc.TugError(["unknown edit id"])
        ok = bool(body.get("ok"))
        entry["status"] = "applied" if ok else "failed"
        entry["errors"] = body.get("errors", [])
        entry["done"] = time.time()
        self.s["results"].append({"id": eid, "guide": entry["guide"], "ok": ok, "note": entry["note"], "summary": entry["summary"][:4], "errors": entry["errors"][:3], "t": time.time()})
        self.s["results"] = self.s["results"][-40:]
        if isinstance(body.get("text"), str):
            self.s["guides"][entry["guide"]] = {"text": body["text"], "hash": tugdoc.sha(body["text"]), "t": time.time()}
        if isinstance(body.get("versions"), list):
            self.s["versions"][entry["guide"]] = body["versions"][-30:]
        if ok:
            r = self.s["reload"]
            line = "%s: %s" % (entry["guide"].split("/")[-1][:30], entry["note"] or "; ".join(entry["summary"][:2]))
            if not r["needed"]:
                r.update(needed=True, edits=[], boot=body.get("boot"), since=time.time())
            r["edits"] = (r["edits"] + [line[:90]])[-6:]
            if body.get("game_live"):
                self._notice()
        else:
            self.overlay([{"op": "toast", "title": "Guide edit failed", "text": ("; ".join(entry["errors"])[:160] or "unknown error"), "level": "bad"}])
        self.save()
        return 200, {"ok": True}

    def _notice(self):
        r = self.s["reload"]
        text = "\n".join(r["edits"])
        self.overlay([
            {"op": "panel", "id": "tug-reload", "title": "Guide changed: type /reload", "anchor": "TOP", "x": 0, "y": -190, "w": 380,
             "accent": "orange", "bgAlpha": 0.7, "collapsible": False, "draggable": True, "visibility": {"combat": "dim", "mouseover": True},
             "children": [
                 {"type": "callout", "level": "warn", "text": "Your mentor edited the leveling guide (%d change(s)). Type /reload when you are out of combat to load it." % len(r["edits"])},
                 {"type": "text", "muted": True, "size": 11, "text": text[:600]}]},
            {"op": "toast", "text": "Guide edited: type /reload to load the changes", "level": "warn", "sound": "ping", "ttl": 10},
        ])

    def r_reloaded(self, method, q, body):
        self.s["bridge_t"] = time.time()
        r = self.s["reload"]
        if r["needed"]:
            r.update(needed=False, edits=[], since=0)
            self.overlay([{"op": "remove", "panel": "tug-reload"}, {"op": "toast", "text": "Guide reloaded", "level": "good", "ttl": 4}], panel_exists="tug-reload")
            self.save()
        return 200, {"ok": True}
