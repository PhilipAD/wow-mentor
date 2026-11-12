#!/usr/bin/env python3
"""wowui: control the in-game mentor overlay of a World of Warcraft client from the command line.

Designed for AI agents: every command prints one JSON object on stdout and exits 0 on success; on failure it
prints {"ok": false, "errors": [...]} on stderr and exits 1. `wowui guide` explains the model, `wowui schema`
lists every field, `wowui example` prints a full set of ops. Python 3 standard library only.

Connection (first match wins): --url, $WOWUI_URL, or the config of WoW Mentor on this Mac (~/.config/wowmentor/config.json).
  WOWUI_URL=https://<your-link>/ui/<secret>      (from `wowmentor links`)
"""
import argparse, base64, json, os, re, sys, time, urllib.parse, urllib.request, urllib.error, random, uuid

BUDGET = 5100   # bytes of JSON per GET request (the {"ops","opId"} wrapper adds ~90); the public zone blocks POST bodies and caps URL length
RETRIES = 3     # attempts per ops request; retries reuse the same opId, so the server never applies them twice


# ---------------------------------------------------------------- connection
def load_env():
    """WoW Mentor's own config on this machine (empty if the agent runs elsewhere and uses $WOWUI_URL)."""
    import json as _json
    home = os.path.abspath(os.path.expanduser(os.environ.get("WOWMENTOR_HOME", "~/.config/wowmentor")))
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            d = _json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def base_url(args):
    if args.url:
        return args.url.rstrip("/")
    if os.environ.get("WOWUI_URL"):
        return os.environ["WOWUI_URL"].rstrip("/")
    env = load_env()
    if env.get("ui_secret"):
        try:
            port = int(env.get("port", 8095))
        except (TypeError, ValueError):
            port = 8095
        root = (env.get("server_url") or "").strip().rstrip("/") or "http://127.0.0.1:%d" % port
        return "%s/ui/%s" % (root, env["ui_secret"])
    fail(["no endpoint: set WOWUI_URL=https://<your-link>/ui/<secret> (see `wowmentor links`) or pass --url"])


def fail(errors, code=1):
    print(json.dumps({"ok": False, "errors": errors}), file=sys.stderr)
    sys.exit(code)


def redact(url):
    """The URL without its query and without the secret path segments (/ui/<secret>, /<read-token>), for error messages and logs."""
    url = url.split("?")[0]
    url = re.sub(r"/ui/[^/]+", "/ui/<secret>", url)
    return re.sub(r"/[0-9a-fA-F]{16,}(?=/|$)", "/<token>", url)


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout):
    """Loopback is reached directly: a system proxy (VPN, debugging proxy) must not see the secret in the URL or swallow the call."""
    host = urllib.parse.urlparse(req.full_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return _direct.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def http(url, data=None, timeout=20, tries=1):
    """(status, body). With tries > 1, network errors and 5xx answers are retried with a short growing pause; callers that retry
    must send something idempotent (ops carry an opId)."""
    last = None
    for attempt in range(max(1, tries)):
        if attempt:
            time.sleep(min(4, 0.5 * (2 ** attempt)))
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json", "User-Agent": "wowui/1.0"})
        try:
            with _open(req, timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            finally:
                e.close()
            if e.code >= 500 and attempt + 1 < tries:
                last = "HTTP %d" % e.code
                continue
            return e.code, body
        except OSError as e:
            last = e
            if attempt + 1 >= tries:
                break
    fail(["cannot reach %s: %s" % (redact(url), last if isinstance(last, str) else type(last).__name__)])


def nocache():
    return "t=%d%d" % (time.time() * 1000, random.randint(0, 999))


def get_json(base, route):
    code, body = http("%s/%s%s%s" % (base, route, "&" if "?" in route else "?", nocache()))
    try:
        return code, json.loads(body)
    except ValueError:
        return code, {"raw": body.decode("utf-8", "replace")[:300]}


def is_local(base):
    host = urllib.parse.urlparse(base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def enc(ops):
    return base64.urlsafe_b64encode(json.dumps(ops, separators=(",", ":")).encode()).decode().rstrip("=")


def size(o):
    return len(json.dumps(o, separators=(",", ":")))


def split_ops(ops):
    """Break ops into groups that fit one GET. Oversized panel ops become a bare panel op plus child ops."""
    out = []
    for op in ops:
        if size(op) <= BUDGET:
            out.append(op)
        elif op.get("op") == "panel" and op.get("children"):
            head = {k: v for k, v in op.items() if k != "children"}
            head["children"] = []
            out.append(head)
            for c in op["children"]:
                if size(c) > BUDGET:
                    fail(["a single component is %d bytes; the public endpoint takes about %d per request. Shorten it." % (size(c), BUDGET)])
                out.append({"op": "child", "panel": op["id"], "component": c})
        elif op.get("op") == "set":
            d = op.get("doc", op)
            out.append({"op": "reset"})
            if d.get("theme"):
                out.append(dict(d["theme"], op="theme"))
            for p in d.get("panels", []):
                out.extend(split_ops([dict(p, op="panel")]))
            for w in d.get("waypoints", []):
                out.append(dict(w, op="waypoint"))
        else:
            fail(["an op is %d bytes; the public endpoint takes about %d per request. Shorten it." % (size(op), BUDGET)])
    return out


def wrap(ops, opid, partial):
    body = {"ops": ops, "opId": opid}
    if partial:
        body["partial"] = True
    return body


def send(base, ops, opid=None, partial=False):
    """Send ops. Every request carries an opId (uuid per invocation by default): a retry after a lost reply is answered by the
    server from memory instead of being applied twice. partial=True applies the valid ops and reports the rest in `rejected`."""
    if isinstance(ops, dict):
        ops = [ops]
    opid = opid or uuid.uuid4().hex
    if is_local(base):
        code, body = http(base + "/ops", json.dumps(wrap(ops, opid, partial)).encode(), tries=RETRIES)
        try:
            return code, json.loads(body)
        except ValueError:
            fail(["unexpected reply (HTTP %d): %s" % (code, body.decode("utf-8", "replace")[:120])])
    groups, cur = [], []
    for op in split_ops(ops):
        if cur and size(cur + [op]) > BUDGET:
            groups.append(cur); cur = []
        cur.append(op)
    if cur:
        groups.append(cur)
    last = (200, {"ok": True})
    for n, g in enumerate(groups):
        gid = opid if len(groups) == 1 else "%s-%d" % (opid, n)
        code, body = http("%s/ops?b=%s" % (base, enc(wrap(g, gid, partial))), tries=RETRIES)
        try:
            res = json.loads(body)
        except ValueError:
            fail(["unexpected reply (blocked by the edge?): " + body.decode("utf-8", "replace")[:120]])
        if code != 200 or not res.get("ok"):
            res["applied_requests"] = n
            return code, res
        last = (code, res)
    if len(groups) > 1:
        last[1]["requests"] = len(groups)
    return last


def run(args, ops):
    code, res = send(base_url(args), ops, getattr(args, "op_id", None), getattr(args, "partial", False))
    if code != 200 or not res.get("ok"):
        fail(res.get("errors") or [json.dumps(res)])
    print(json.dumps(res))


# ---------------------------------------------------------------- op builders
STYLE = {  # CLI flag -> (panel field, type)
    "title": ("title", str), "subtitle": ("subtitle", str), "anchor": ("anchor", str), "x": ("x", float), "y": ("y", float),
    "w": ("w", float), "max_h": ("maxH", float), "scale": ("scale", float), "alpha": ("alpha", float), "bg": ("bg", str),
    "bg_alpha": ("bgAlpha", float), "border": ("border", str), "border_alpha": ("borderAlpha", float), "accent": ("accent", str),
    "font_size": ("fontSize", float), "font": ("font", str), "padding": ("padding", float), "gap": ("gap", float), "ttl": ("ttl", float),
}


def add_style_flags(p):
    for flag, (field, typ) in STYLE.items():
        p.add_argument("--" + flag.replace("_", "-"), type=typ, dest="s_" + flag)
    p.add_argument("--collapsed", action="store_true", default=None)
    p.add_argument("--expanded", action="store_true")
    p.add_argument("--no-header", action="store_true")
    p.add_argument("--click-through", action="store_true", help="no mouse interaction at all (checklists cannot be ticked)")
    p.add_argument("--pin", action="store_true", help="never dim or hide in combat")
    p.add_argument("--combat", choices=["show", "dim", "hide"])
    p.add_argument("--dim", type=float, help="alpha multiplier while dimmed in combat (default 0.3)")
    p.add_argument("--idle-fade", type=float, help="seconds without change before the panel fades")
    p.add_argument("--idle-alpha", type=float)
    p.add_argument("--no-mouseover", action="store_true")
    p.add_argument("--hide-mounted", action="store_true")
    p.add_argument("--hide-instance", action="store_true")
    p.add_argument("--min-level", type=int)
    p.add_argument("--max-level", type=int)
    p.add_argument("--extra", help="JSON object of extra panel fields (see `wowui schema`)")


def style_fields(a):
    out = {}
    for flag, (field, typ) in STYLE.items():
        v = getattr(a, "s_" + flag, None)
        if v is not None:
            out[field] = v
    if a.collapsed:
        out["collapsed"] = True
    if a.expanded:
        out["collapsed"] = False
    if a.no_header:
        out["header"] = False
    if a.click_through:
        out["interactive"] = False
    if a.pin:
        out["pin"] = True
    vis = {}
    if a.combat:
        vis["combat"] = a.combat
    if a.dim is not None:
        vis["dim"] = a.dim
    if a.idle_fade is not None:
        vis["idleFade"] = a.idle_fade
    if a.idle_alpha is not None:
        vis["idleAlpha"] = a.idle_alpha
    if a.no_mouseover:
        vis["mouseover"] = False
    if a.hide_mounted:
        vis["hideWhenMounted"] = True
    if a.hide_instance:
        vis["hideInInstance"] = True
    if a.min_level is not None:
        vis["minLevel"] = a.min_level
    if a.max_level is not None:
        vis["maxLevel"] = a.max_level
    if vis:
        out["visibility"] = vis
    if a.extra:
        try:
            out.update(json.loads(a.extra))
        except ValueError as e:
            fail(["--extra is not valid JSON: %s" % e])
    return out


def parse_task(spec, idx):
    """'Text' | 'id::Text' | 'id::Text::note' | '!Text' (high priority) | '~Text' (medium)."""
    prio = None
    if spec[:1] in ("!", "~"):                      # marker before the whole spec: '!id::Text' or '!Text'
        prio, spec = ("high" if spec[0] == "!" else "med"), spec[1:]
    parts = spec.split("::")
    it = {}
    if len(parts) >= 2:
        it["id"], it["text"] = parts[0], parts[1]
        if len(parts) >= 3:
            it["note"] = parts[2]
    else:
        it["text"] = parts[0]
        it["id"] = "t%d" % (idx + 1)
    if it["text"].startswith("!"):
        it["text"], prio = it["text"][1:], "high"
    elif it["text"].startswith("~"):
        it["text"], prio = it["text"][1:], "med"
    if prio:
        it["priority"] = prio
    return it


def cmd_panel(a):
    op = {"op": "panel", "id": a.id}
    op.update(style_fields(a))
    run(a, op)


def cmd_plan(a):
    kids = []
    if a.intro:
        kids.append({"type": "text", "text": a.intro, "muted": True})
    if a.progress:
        kids.append({"type": "progress", "bind": a.progress, "label": a.progress.upper()})
    kids.append({"type": "checklist", "id": "tasks", "items": [parse_task(t, i) for i, t in enumerate(a.task)]})
    if a.callout:
        kids.append({"type": "callout", "level": a.level, "text": a.callout})
    op = {"op": "panel", "id": a.id, "children": kids}
    op.update(style_fields(a))
    op.setdefault("title", a.id.title())
    run(a, op)


def current_doc(a):
    code, res = get_json(base_url(a), "state")
    if code != 200:
        fail(["could not read state: %s" % json.dumps(res)[:200]])
    return res["doc"]


def cmd_add_task(a):
    doc = current_doc(a)
    panel = next((p for p in doc["panels"] if p["id"] == a.panel), None)
    if not panel:
        fail(["panel %r does not exist" % a.panel])
    cl = next((c for c in panel.get("children", []) if c.get("type") == "checklist" and (a.checklist is None or c.get("id") == a.checklist)), None)
    if not cl:
        fail(["panel %r has no checklist; create one with `wowui plan`" % a.panel])
    item = parse_task(a.text, len(cl["items"]))
    if a.id:
        item["id"] = a.id
    if a.note:
        item["note"] = a.note
    if a.priority:
        item["priority"] = a.priority
    cl["items"] = [i for i in cl["items"] if i["id"] != item["id"]] + [item]
    if "id" not in cl:
        cl["id"] = "tasks"
    run(a, {"op": "child", "panel": a.panel, "component": cl})


def cmd_text(a):
    c = {"type": "text", "text": a.text}
    if a.id:
        c["id"] = a.id
    if a.muted:
        c["muted"] = True
    if a.color:
        c["color"] = a.color
    if a.size:
        c["size"] = a.size
    run(a, {"op": "child", "panel": a.panel, "component": c})


def cmd_progress(a):
    c = {"type": "progress"}
    if a.bind:
        c["bind"] = a.bind
    if a.value is not None:
        c["value"] = a.value
    if a.max is not None:
        c["max"] = a.max
    if a.label:
        c["label"] = a.label
    if a.id:
        c["id"] = a.id
    if a.color:
        c["fill"] = a.color
    run(a, {"op": "child", "panel": a.panel, "component": c})


def cmd_callout(a):
    c = {"type": "callout", "text": a.text, "level": a.level}
    if a.title:
        c["title"] = a.title
    if a.id:
        c["id"] = a.id
    run(a, {"op": "child", "panel": a.panel, "component": c})


def cmd_component(a):
    try:
        c = json.loads(a.json)
    except ValueError as e:
        fail(["component JSON invalid: %s" % e])
    op = {"op": "child", "panel": a.panel, "component": c}
    if a.after:
        op["after"] = a.after
    run(a, op)


def cmd_check(a):
    run(a, {"op": "check", "panel": a.panel, "item": a.item, "done": not a.undo})


def cmd_remove(a):
    op = {"op": "remove", "panel": a.panel}
    if a.child:
        op["child"] = a.child
    if a.item:
        op["item"] = a.item
    run(a, op)


def cmd_toast(a):
    op = {"op": "toast", "text": a.text, "level": a.level}
    for k in ("title", "sound", "ttl"):
        if getattr(a, k) is not None:
            op[k] = getattr(a, k)
    run(a, op)


def cmd_say(a):
    run(a, {"op": "action", "name": "chat", "text": a.text, **({"sound": a.sound} if a.sound else {})})


def cmd_waypoint(a):
    if a.clear:
        run(a, {"op": "remove", "waypoint": a.id} if a.id else {"op": "waypoint_clear"})
        return
    if not a.id or a.map is None or a.x is None or a.y is None:
        fail(["waypoint needs ID --map <uiMapID> --x <0-100> --y <0-100> (or --clear [ID])"])
    op = {"op": "waypoint", "id": a.id, "map": a.map, "x": a.x, "y": a.y, "track": a.track}
    if a.title:
        op["title"] = a.title
    if a.quest:
        op["quest"] = a.quest
    run(a, op)


def cmd_action(a):
    op = {"op": "action", "name": a.name}
    for k in ("text", "sound", "quest", "cmd", "guide"):
        if getattr(a, k, None) is not None:
            op[k] = getattr(a, k)
    run(a, op)


def cmd_theme(a):
    op = {"op": "theme"}
    for flag, field, typ in (("accent", "accent", str), ("bg", "bg", str), ("bg_alpha", "bgAlpha", float), ("border", "border", str),
                             ("border_alpha", "borderAlpha", float), ("text", "text", str), ("muted", "muted", str), ("good", "good", str),
                             ("warn", "warn", str), ("bad", "bad", str), ("font", "font", str), ("size", "size", float),
                             ("padding", "padding", float), ("gap", "gap", float)):
        v = getattr(a, flag, None)
        if v is not None:
            op[field] = v
    if a.shadow is not None:
        op["shadow"] = a.shadow == "on"
    if a.outline is not None:
        op["outline"] = a.outline == "on"
    run(a, op)


def cmd_ops(a):
    raw = sys.stdin.read() if a.file in (None, "-") else open(a.file, encoding="utf-8").read()
    try:
        ops = json.loads(raw)
    except ValueError as e:
        fail(["input is not valid JSON: %s" % e])
    run(a, ops)


def cmd_simple(op):
    return lambda a: run(a, {"op": op})


def cmd_state(a):
    base = base_url(a)
    code, res = get_json(base, "state")
    if code != 200:
        fail(["state request failed: %s" % json.dumps(res)[:200]])
    doc, game = res["doc"], res.get("game", {})
    out = {
        "ok": True, "rev": doc["rev"], "applied_rev": game.get("rev_applied"), "in_sync": game.get("rev_applied") == doc["rev"],
        "game_report_age_s": res.get("game_age_s"), "panels": [p["id"] for p in doc["panels"]], "waypoints": [w["id"] for w in doc["waypoints"]],
        "game": {k: v for k, v in game.items() if k not in ("lines", "t")}, "ui_state": game.get("lines", []),
    }
    print(json.dumps(out, indent=None if a.compact else 2))


def cmd_doc(a):
    code, res = get_json(base_url(a), "state")
    print(json.dumps(res.get("doc", res), indent=2))


def cmd_schema(a):
    code, res = get_json(base_url(a), "schema")
    print(json.dumps(res, indent=2))


def cmd_guide(a):
    code, body = http("%s/guide?%s" % (base_url(a), nocache()))
    sys.stdout.write(body.decode("utf-8", "replace"))


def cmd_example(a):
    code, res = get_json(base_url(a), "schema")
    print(json.dumps(res.get("example", res), indent=2))


def cmd_demo(a):
    code, res = get_json(base_url(a), "schema")
    run(a, res["example"])


def cmd_feed(a):
    base = base_url(a)
    tok = os.environ.get("WOWUI_READ_TOKEN") or load_env().get("read_token")
    if not tok:
        fail(["set WOWUI_READ_TOKEN to the token in your private feed link (wowmentor links) to read the game feed"])
    root = base.split("/ui/")[0]
    code, body = http("%s/%s?%s" % (root, tok, nocache()))
    sys.stdout.write(body.decode("utf-8", "replace"))


def cmd_verdict(a):
    if a.clear:
        run(a, {"op": "verdict", "clear": True})
    elif a.remove:
        run(a, {"op": "verdict", "item": int(a.item), "remove": True})
    else:
        if not a.text:
            fail(["verdict needs --text 'short opinion' (or --remove / --clear)"])
        run(a, {"op": "verdict", "item": int(a.item), "text": a.text})


def cmd_watch(a):
    op = {"op": "watch"}
    if a.spells:
        op["spells"] = [int(i) for i in a.spells]
    if a.clear:
        op["items"] = []
    elif a.ids:
        op["items"] = [int(i) for i in a.ids]
    if a.add:
        op["add"] = [int(i) for i in a.add]
    if a.remove:
        op["remove"] = [int(i) for i in a.remove]
    if len(op) == 1:
        fail(["watch needs item ids, --add, --remove, --spells or --clear. Find ids with `wowui db item --name ...`"])
    run(a, op)


# ---------------------------------------------------------------- game database (Forever quests, NPCs, items; offline, from the player's Mac)
def cmd_db(a):
    route = a.dbcmd.replace("-", "_")
    params = {}
    for k in ("id", "name", "zone", "level", "tol", "like", "which", "guide", "q", "npc", "quest", "frm"):
        v = getattr(a, k, None)
        if v is not None:
            params[k] = v
    if getattr(a, "klass", None):
        params["class"] = a.klass
    if "frm" in params:
        params["from"] = params.pop("frm")
    if getattr(a, "race", None):
        params["race"] = a.race
    q = urllib.parse.urlencode(params)
    code, res = get_json(base_url(a), "db/%s%s" % (route, "?" + q if q else ""))
    if code != 200 or not res.get("ok", True):
        fail(res.get("errors") or [json.dumps(res)[:300]])
    if getattr(a, "set", False) and route == "waypoint":
        f = res["first"]
        op = {"op": "waypoint", "id": a.wid, "map": f["uiMapId"], "x": f["x"], "y": f["y"], "title": f["title"], "track": True}
        if a.quest:
            op["quest"] = int(a.quest) if str(a.quest).isdigit() else None
            if op["quest"] is None:
                del op["quest"]
        run(a, op)
        return
    print(json.dumps(res, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- awareness helpers: sections, contradiction guard, route, autopsy, debrief
def cmd_section(a):
    feed = read_feed(a)
    for name in a.names:
        body = feed_section(feed, name.upper())
        print("--- %s" % name.upper())
        print(body.rstrip() if body else "(section not in the feed: the addon may be older, or nothing to report)")


def _fast(feed):
    f = feed_section(feed, "FAST")
    m = re.search(r"Position: map=(\d+) \(([^)]*)\) x=([\d.]+) y=([\d.]+)", f)
    return (int(m.group(1)), float(m.group(3)), float(m.group(4)), m.group(2)) if m else None


def _quest_log(feed):
    qs = feed_section(feed, "QUESTS")
    return {int(i): (st, t) for st, t, i in re.findall(r"\[(COMPLETE|in progress|failed)\] (.+?) \(L\d+\) #(\d+)", qs)}


def cmd_verify(a):
    """Contradiction guard: compare Guidelime's next step, the overlay tasks, the waypoint and Questie's focus before you say anything."""
    feed = read_feed(a)
    st = live_guide_state(feed)
    problems, info = [], []
    guide = tug_name_for_title(a, st["title"]) if st["title"] else None
    if not guide:
        fail(["no Guidelime guide is loaded in the game, or the GUIDES section is missing (older addon?)"])
    line = first_open_line(a, guide, st["resolved"])
    text = tug_get(a, "show", name=guide, **{"from": line, "to": line + 14})["text"] if line else ""
    steps = [re.match(r"\s*(\d+)\s+(.*)", l) for l in text.splitlines()]
    steps = [(int(m.group(1)), m.group(2)) for m in steps if m]
    nxt = steps[0] if steps else None
    next_q = {int(i) for n, t in steps for i in re.findall(r"\[Q[ACT](\d+)", t)}
    first_q = {int(i) for i in re.findall(r"\[Q[ACT](\d+)", nxt[1])} if nxt else set()
    m = re.search(r"\[G\s*([\d.]+)\s*,\s*([\d.]+)", nxt[1]) if nxt else None
    info.append("Guidelime next step (%s line %s): %s" % (guide, nxt[0] if nxt else "?", (nxt[1][:110] if nxt else "(none: guide finished?)")))
    info.append("Questie focus: %s | arrow source: %s | resolved steps: %d" % (st["focus"] or "none", st["arrow"], len(st["resolved"])))
    doc = current_doc(a)
    open_items, task_q = [], set()
    for p in doc.get("panels", []):
        for c in p.get("children", []):
            if c.get("type") == "checklist":
                for it in c.get("items", []):
                    if not it.get("done"):
                        open_items.append((p["id"], it.get("id"), it.get("text", "")))
                        au = it.get("auto") or {}
                        if isinstance(au.get("quest"), int):
                            task_q.add(au["quest"])
    info.append("Overlay open tasks: %s" % ("; ".join("%s/%s: %s" % (p, i, t[:50]) for p, i, t in open_items[:8]) or "none"))
    wps = doc.get("waypoints", [])
    info.append("Overlay waypoints: %s" % (", ".join("%s map %s %.1f,%.1f %s" % (w["id"], w["map"], w["x"], w["y"], w.get("title", "")) for w in wps) or "none"))
    if st["focus"] and next_q and st["focus"] not in next_q:
        problems.append("FOCUS_MISMATCH Questie focus #%s is not a quest of the next Guidelime steps %s: `wowui action track_quest --quest %s`" % (st["focus"], sorted(next_q)[:4], sorted(first_q or next_q)[0]))
    if not st["focus"] and first_q:
        problems.append("NO_FOCUS the next step is about quest %s but Questie focuses nothing: `wowui action track_quest --quest %s`" % (sorted(first_q)[0], sorted(first_q)[0]))
    if task_q and next_q and not (task_q & next_q):
        problems.append("OVERLAY_OFF_GUIDE the open overlay tasks cover quests %s but the next guide steps are about %s" % (sorted(task_q)[:5], sorted(next_q)[:5]))
    if not open_items:
        problems.append("OVERLAY_EMPTY no open overlay tasks while the guide has a next step: `wowui pin` or `wowui plan`")
    if m and wps:
        gx, gy = float(m.group(1)), float(m.group(2))
        w = next((x for x in wps if x.get("track")), wps[0])
        d = ((w["x"] - gx) ** 2 + (w["y"] - gy) ** 2) ** 0.5
        if d > 6:
            problems.append("ARROW_MISMATCH waypoint %s at %.1f,%.1f is %.0f units from Guidelime's next [G] %.1f,%.1f (same map not checked): align them or use track=false" % (w["id"], w["x"], w["y"], d, gx, gy))
    log = _quest_log(feed)
    for qid in sorted(first_q):
        if qid in log and log[qid][0] == "COMPLETE":
            info.append("note: quest #%d is COMPLETE in the log: the next step is probably its turn-in" % qid)
    print("\n".join("  " + i for i in info))
    print("# %d problem(s)" % len(problems))
    for pr in problems:
        print("PROBLEM " + pr)
    print("# anything the feed hides is UNKNOWN: do not state it as fact. Fix problems before you message the player.")


def cmd_route(a):
    """Order the quests in the log into stops (turn-ins first where done), nearest first; optionally pin the first stops."""
    feed = read_feed(a)
    log = _quest_log(feed)
    if not log:
        fail(["the quest log in the feed is empty"])
    pos = _fast(feed)
    ids = [i for i in log if a.only is None or str(i) in a.only.split(",")]
    done = [i for i in ids if log[i][0] == "COMPLETE"]
    params = {"quests": ",".join(map(str, ids)), "done": ",".join(map(str, done))}
    if pos:
        params.update(map=pos[0], x=pos[1], y=pos[2])
    code, res = get_json(base_url(a), "db/route?" + urllib.parse.urlencode(params))
    if code != 200 or not res.get("ok", True):
        fail(res.get("errors") or [json.dumps(res)[:300]])
    for n, s in enumerate(res["stops"], 1):
        print("%2d. #%-6d %-34s %-22s %s %.1f,%.1f (map %s)%s" % (n, s["quest"], s["name"][:34], s["kind"][:22], s["zone"], s["x"], s["y"], s["uiMapId"], "  <- here" if n == 1 else ""))
    if res["no_location_known"]:
        print("no location in the database for: %s" % res["no_location_known"])
    if a.set and res["stops"]:
        ops = []
        for n, s in enumerate(res["stops"][: a.set], 1):
            op = {"op": "waypoint", "id": "route%d" % n, "map": s["uiMapId"], "x": s["x"], "y": s["y"], "title": "%d. %s" % (n, s["name"]), "track": n == 1}
            if n == 1:
                op["quest"] = s["quest"]
            ops.append(op)
        run(a, ops)


def cmd_autopsy(a):
    """Everything known about the latest death, in one place: events, the nearest fight, position, pet, buffs, bags, durability."""
    feed = read_feed(a)
    code, ev = get_json(base_url(a), "events?types=death,fight&limit=6")
    evs = ev.get("events", []) if code == 200 else []
    print("# latest death and fight events (newest last)")
    for e in evs[-4:]:
        print(json.dumps(e, ensure_ascii=False)[:600])
    for name in ("DEATHS", "FIGHTS", "FAST", "PET", "AURAS", "ACTIONBARS", "BAGS", "EQUIPMENT"):
        body = feed_section(feed, name)
        if body:
            print("--- %s" % name)
            print("\n".join(body.rstrip().splitlines()[: (14 if name in ("DEATHS", "FIGHTS", "FAST", "PET", "AURAS") else 8)]))
    print("# Write the lesson with `wowui memory add --tag lesson ...`; check pet out, Demon Skin, shards, Healthstone, potion; name where it happened and warn before going back.")


def cmd_debrief(a):
    feed = read_feed(a)
    code, ev = get_json(base_url(a), "events?limit=500")
    counts = {}
    for e in ev.get("events", []) if code == 200 else []:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    print("# events in the buffer by type: %s" % (", ".join("%s=%d" % kv for kv in sorted(counts.items(), key=lambda kv: -kv[1])[:14]) or "none"))
    for name in ("SESSION", "DEATHS", "FIGHTS", "QUESTS", "PROFESSIONS"):
        body = feed_section(feed, name)
        if body:
            print("--- %s" % name)
            print("\n".join(body.rstrip().splitlines()[:10]))
    code, m = get_json(base_url(a), "memory")
    lessons = [n for n in (m.get("notes") or []) if n.get("tag") == "lesson"][-8:] if code == 200 else []
    print("--- lessons so far")
    print("\n".join("- #%s %s" % (n["id"], n["text"]) for n in lessons) or "(none yet)")
    print("# Say what went well and one thing to fix next, then `wowui memory add --tag lesson \"...\"`.")


# ---------------------------------------------------------------- live guide editing (Guidelime TUGs on the player's Mac)
def unescape(t):
    return t.replace("\\n", "\n") if t is not None else t


def tug_get(a, route, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    code, res = get_json(base_url(a), "tug/%s%s%s" % (route, "?" + q if q else "", ""))
    if code != 200 or not res.get("ok", True):
        fail(res.get("errors") or [json.dumps(res)[:300]])
    return res


def tug_send(a, edits):
    """Send one edit request or a list. Public endpoint: GET with base64 (chunked), local: POST."""
    base = base_url(a)
    body = edits[0] if len(edits) == 1 else {"edits": edits}
    if is_local(base):
        code, raw = http(base + "/tug/ops", json.dumps(body).encode())
        res = json.loads(raw)
    else:
        groups, cur = [], []
        for e in edits:
            if size(e) > BUDGET:
                fail(["one edit is %d bytes; the public endpoint takes about %d per request. Split it into several inserts." % (size(e), BUDGET)])
            if cur and size(cur + [e]) > BUDGET:
                groups.append(cur); cur = []
            cur.append(e)
        groups.append(cur)
        res = {"ok": True, "queued": []}
        for g in groups:
            b = enc(g[0] if len(g) == 1 else {"edits": g})
            code, raw = http("%s/tug/ops?b=%s" % (base, b))
            r = json.loads(raw)
            if not r.get("ok"):
                r["applied_requests"] = groups.index(g)
                res = r
                break
            res["queued"] += r["queued"]
    if not res.get("ok"):
        fail(res.get("errors") or [json.dumps(res)])
    print(json.dumps(res, indent=2))


def addr(a):
    d = {}
    for k in ("line", "match", "nth", "expect"):
        v = getattr(a, k, None)
        if v is not None:
            d[k] = v
    return d


def cmd_tug_list(a):
    r = tug_get(a, "list", filter=a.filter)
    if getattr(a, "json", False):
        print(json.dumps(r["guides"], indent=2))
        return
    for g in r["guides"]:
        print("%-44s %4d lines  [%s] -> %s" % (g["name"], g["lines"], g["title"], g["next"] or "-"))


def cmd_tug_show(a):
    r = tug_get(a, "show", name=a.guide, **{"from": a.frm, "to": a.to})
    print("# %s  [%s]  lines %d-%d of %d  hash %s" % (r["name"], r["title"], r["from"], r["to"], r["total"], r["hash"]))
    print(r["text"])


def cmd_tug_search(a):
    r = tug_get(a, "search", q=a.text, guide=a.guide)
    for h in r["hits"]:
        print("%s:%d  %s" % (h["guide"], h["line"], h["text"]))
    print("# %d hit(s)%s" % (r["count"], " (truncated)" if r["truncated"] else ""))


def cmd_tug_route(a):
    r = tug_get(a, "route", start=a.start)
    print(json.dumps(r["route"], indent=2))


def cmd_tug_replace(a):
    op = {"op": "replace", "text": unescape(a.text)}
    op.update(addr(a))
    tug_send(a, [{"guide": a.guide, "ops": [op], "note": a.note or ""}])


def cmd_tug_insert(a):
    op = {"op": "insert", "text": unescape(a.text)}
    for k in ("after_line", "after_match", "before_line", "before_match", "at", "nth"):
        v = getattr(a, k, None)
        if v is not None:
            op[k] = v
    tug_send(a, [{"guide": a.guide, "ops": [op], "note": a.note or ""}])


def cmd_tug_delete(a):
    op = {"op": "delete"}
    op.update(addr(a))
    if a.count:
        op["count"] = a.count
    tug_send(a, [{"guide": a.guide, "ops": [op], "note": a.note or ""}])


def cmd_tug_sub(a):
    op = {"op": "sub", "find": a.find, "replace": a.replace}
    op.update(addr(a))
    if a.all:
        op["all"] = True
    tug_send(a, [{"guide": a.guide, "ops": [op], "note": a.note or ""}])


def cmd_tug_set(a):
    text = sys.stdin.read() if a.file in (None, "-") else open(a.file, encoding="utf-8").read()
    tug_send(a, [{"guide": a.guide, "ops": [{"op": "set", "text": text.rstrip("\n")}], "note": a.note or ""}])


def cmd_tug_ops(a):
    raw = sys.stdin.read() if a.file in (None, "-") else open(a.file, encoding="utf-8").read()
    try:
        body = json.loads(raw)
    except ValueError as e:
        fail(["input is not valid JSON: %s" % e])
    tug_send(a, body["edits"] if isinstance(body, dict) and "edits" in body else (body if isinstance(body, list) else [body]))


def cmd_tug_history(a):
    print(json.dumps(tug_get(a, "history", name=a.guide), indent=2))


def cmd_tug_restore(a):
    tug_send(a, [{"guide": a.guide, "restore": a.version, "note": a.note or "restore " + a.version}])

# ---------------------------------------------------------------- align with Guidelime / Questie, pin urgent tasks
GUIDE_PREFIX = "TUGs The Ultimate (Leveling) Guides "


def read_feed(a):
    tok = os.environ.get("WOWUI_READ_TOKEN") or load_env().get("read_token")
    if not tok:
        fail(["set WOWUI_READ_TOKEN to the token in your private feed link (wowmentor links) to read the game feed"])
    root = base_url(a).split("/ui/")[0]
    code, body = http("%s/%s?%s" % (root, tok, nocache()))
    return body.decode("utf-8", "replace")


def feed_section(feed, name):
    m = re.search(r"^--- %s\b[^\n]*\n(.*?)(?=^--- |\Z)" % name, feed, re.S | re.M)
    return m.group(1) if m else ""


def live_guide_state(feed):
    """Parse the GUIDES section: current guide title, resolved step indexes, Questie completed ids, focus."""
    sec = feed_section(feed, "GUIDES")
    out = {"title": None, "resolved": set(), "completed": set(), "focus": None, "arrow": None}
    m = re.search(r"current guide: (.+)", sec)
    if m:
        out["title"] = m.group(1).strip()
    size = None
    ms = re.search(r"Guidelime guide steps: (\d+)", sec)
    if ms:
        size = int(ms.group(1))
    m = re.search(r"resolved step indexes[^:]*: (.+)", sec)
    if m and m.group(1).strip() != "none":
        for part in m.group(1).split(","):
            lo, _, hi = part.strip().partition("-")
            if lo.isdigit():
                out["resolved"].update(range(int(lo), int(hi or lo) + 1))
    if size:
        out["resolved"] = {i for i in out["resolved"] if i <= size}
    m = re.search(r"completed quest ids \(\d+\): ([\d,]*)", sec)
    if m:
        out["completed"] = {int(x) for x in m.group(1).split(",") if x}
    m = re.search(r"focused quest: (\d+)", sec)
    if m:
        out["focus"] = int(m.group(1))
    m = re.search(r"arrow=(\w+)", sec)
    if m:
        out["arrow"] = m.group(1)
    return out


def tug_name_for_title(a, title):
    short = (title or "").replace(GUIDE_PREFIX, "", 1)
    for g in tug_get(a, "list")["guides"]:
        if g["title"] == short or g["name"].endswith("/" + short):
            return g["name"]
    return None


def cmd_align(a):
    """Cross-check the guides ahead of the player against the live quest log and Questie's completed list."""
    feed = read_feed(a)
    st = live_guide_state(feed)
    qs = feed_section(feed, "QUESTS")
    log = {int(i): (s, t) for s, t, i in re.findall(r"\[(COMPLETE|in progress|failed)\] (.+?) \(L\d+\) #(\d+)", qs)}
    done = st["completed"] | {int(x) for x in (a.completed or "").split(",") if x.strip()}
    start = a.guide or tug_name_for_title(a, st["title"])
    if not start:
        fail(["could not tell the current guide; pass it: wowui align \"Alliance/9-11 Dun Morogh&Loch Modan\""])
    route = tug_get(a, "route", start=start)["route"]
    names = [r if isinstance(r, str) else r.get("name") for r in route][: a.ahead + 1] + [x.strip() for x in (a.also or "").split(",") if x.strip()]
    print("# current guide: %s | quest log: %d | Questie completed: %d | focused quest: %s | arrow: %s | checking: %s" % (start, len(log), len(done), st["focus"] or "none", st["arrow"], ", ".join(names)))
    tag, seen, n = re.compile(r"\[(QA|QC|QT|QS)(\d+)"), set(), 0
    for gi, g in enumerate(names):
        for ln in tug_get(a, "show", name=g)["text"].splitlines():
            m = re.match(r"\s*(\d+)\s+(.*)", ln)
            if not m:
                continue
            num, text = int(m.group(1)), m.group(2)
            resolved = gi == 0 and num in st["resolved"]
            for kind, qid in tag.findall(text):
                qid = int(qid); seen.add(qid)
                if resolved:
                    continue
                if kind == "QA" and qid in done:
                    n += 1; print("STALE   %s:%d accept of #%d already completed: %s" % (g, num, qid, text[:90]))
                elif kind == "QA" and qid in log:
                    n += 1; print("HAVE    %s:%d #%d already in your log: %s" % (g, num, qid, text[:90]))
                elif kind in ("QC", "QT") and qid in done:
                    n += 1; print("STALE   %s:%d %s #%d already turned in: %s" % (g, num, kind, qid, text[:90]))
    for qid, (s, t) in sorted(log.items()):
        if qid not in seen:
            n += 1; print("MISSING #%d %s [%s] is in your log but in none of the checked guides" % (qid, t, s))
    if st["focus"] and st["focus"] not in log:
        n += 1; print("FOCUS   Questie focus #%d is not in your log: wowui action unfocus_quest" % st["focus"])
    print("# %d finding(s). Edit only steps ahead of the player; `wowui tug show` before every edit." % n)


def first_open_line(a, guide, resolved):
    """The line a new top-of-list step must go before: the first step the player has not resolved yet."""
    lines = tug_get(a, "show", name=guide)["text"].splitlines()
    first = 1
    for ln in lines:
        m = re.match(r"\s*(\d+)\s+(.*)", ln)
        if m and re.match(r"\[(GA|N|NX|D|DA|DH)\b", m.group(2)):
            first = int(m.group(1)) + 1
    for ln in lines:
        m = re.match(r"\s*(\d+)\s+", ln)
        if m and int(m.group(1)) >= first and int(m.group(1)) not in resolved:
            return int(m.group(1))
    return None


def cmd_pin(a):
    """Put an urgent task at the top of what is left in the player's current guide AND on the overlay, so they agree."""
    feed = read_feed(a)
    st = live_guide_state(feed)
    guide = a.guide or tug_name_for_title(a, st["title"])
    if not guide:
        fail(["could not tell the current guide; pass --guide"])
    text = unescape(a.text)
    if a.go:
        text += " [G%s]" % a.go.replace(" ", "")
    line = first_open_line(a, guide, st["resolved"])
    if line is None:
        op = {"op": "insert", "text": text, "at": "end"}
    else:
        op = {"op": "insert", "text": text, "before_line": line}
    tug_send(a, [{"guide": guide, "ops": [op], "note": a.note or "pinned by the mentor: %s" % a.text[:60]}])
    if not a.no_overlay:
        ns = argparse.Namespace(**{**vars(a), "panel": a.panel, "text": re.sub(r"\[[^\]]*\]", "", a.text).strip()[:90], "id": a.id, "note": None, "priority": "high", "checklist": None})
        cmd_add_task(ns)
    print("pinned before line %s of %s; the player needs /reload to see it in Guidelime" % (line, guide))


def cmd_tug_status(a):
    print(json.dumps(tug_get(a, "status"), indent=2))


# ---------------------------------------------------------------- memory, brief, built-in panel
def mem_send(a, ops):
    base = base_url(a)
    if is_local(base):
        code, raw = http(base + "/memory/ops", json.dumps(ops).encode())
        res = json.loads(raw)
    else:
        code, raw = http("%s/memory/ops?b=%s" % (base, enc(ops)))
        res = json.loads(raw)
    if not res.get("ok"):
        fail(res.get("errors") or [json.dumps(res)])
    print(json.dumps(res))


def cmd_brief(a):
    code, body = http("%s/brief?%s" % (base_url(a), nocache()))
    sys.stdout.write(body.decode("utf-8", "replace"))


def cmd_mem_show(a):
    code, res = get_json(base_url(a), "memory")
    print("# summary:\n%s\n# notes:" % (res.get("summary") or "(empty)"))
    for n in res.get("notes", []):
        print("#%d %s%s  %s" % (n["id"], time.strftime("%m-%d %H:%M", time.localtime(n["t"])), (" [%s]" % n["tag"]) if n.get("tag") else "", n["text"]))


def cmd_mem_add(a):
    op = {"op": "add", "text": a.text}
    if a.tag:
        op["tag"] = a.tag
    mem_send(a, [op])


def cmd_mem_summary(a):
    mem_send(a, [{"op": "summary", "text": a.text}])


def cmd_mem_remove(a):
    mem_send(a, [{"op": "remove", "id": a.id}])


def cmd_mem_clear(a):
    mem_send(a, [{"op": "clear"}])


def cmd_builtin(a):
    run(a, {"op": "builtin", a.which: a.state == "on"})


# ---------------------------------------------------------------- rules, coach messages, events, session
def parse_value(raw):
    """--set key=value values: true/false, ints, floats, else the string."""
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for conv in (int, float):
        try:
            return conv(raw)
        except ValueError:
            pass
    return raw


def parse_sets(pairs):
    out = {}
    for item in pairs or []:
        if "=" not in item:
            fail(["--set needs key=value, got %r" % item])
        k, v = item.split("=", 1)
        out[k.strip()] = parse_value(v)
    return out


def slug(text, n=24):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:n] or "x"


def _take(settings, allowed):
    unknown = sorted(set(settings) - set(allowed))
    if unknown:
        fail(["unknown --set key(s) %s (this preset accepts: %s)" % (", ".join(unknown), ", ".join(sorted(allowed)))])


COMMON_SET = ("id", "cooldown_s", "once", "enabled", "active_only", "defer_max_s", "text", "level", "sound", "toast", "chat", "title")


def preset_rule(name, st):
    """The five example rules of the design doc. Placeholders in text ({free} {pct} {count} {subzone} {name} {slot}) are filled in the
    game when the rule fires. Every text is yours to change with --set text=... . Level and XP are never shown."""
    st = dict(st)
    spec = {
        "bags-full": dict(keys=("n",), when=lambda s: {"type": "bags_free_lte", "n": s.get("n", 2)}, cooldown=300, level="warn",
                          text=lambda s: "Bags nearly full ({free} slots free). Sell the grey items at a vendor."),
        "durability": dict(keys=("pct",), when=lambda s: {"type": "durability_lte", "pct": s.get("pct", 20)}, cooldown=600, level="warn",
                           text=lambda s: "Your gear is at {pct}% durability. Visit a repair vendor soon."),
        "deaths": dict(keys=("count", "minutes"), when=lambda s: {"type": "deaths_in_subzone", "count": s.get("count", 2), "minutes": s.get("minutes", 10)},
                       cooldown=300, level="bad",
                       text=lambda s: "You have died {count} times in {subzone} in the last %s minutes. This area may be too hard right now: pick easier quests or find a group." % s.get("minutes", 10)),
        "buff": dict(keys=("name", "spell", "pet", "ooc"), when=None, cooldown=120, level="tip",
                     text=lambda s: "Buff missing: {name}. Cast it before the next fight."),
        "lower-rank": dict(keys=(), when=lambda s: {"type": "lower_rank_on_bar"}, cooldown=300, level="tip",
                           text=lambda s: "{name} on your bar is a lower rank than the one you know (slot {slot}). Drag the newest rank onto the bar."),
    }[name]
    _take(st, tuple(spec["keys"]) + COMMON_SET)
    if name == "buff":
        if "name" not in st and "spell" not in st:
            fail(["the buff preset needs --set name=<buff name> (or spell=<spell id>), e.g. --set name='Power Word: Fortitude'"])
        when = {"type": "buff_missing", "ooc": st.get("ooc", True)}
        for k in ("name", "spell", "pet"):
            if k in st:
                when[k] = st[k]
        default_id = "preset-buff-" + slug(st.get("name", st.get("spell")))
    else:
        when = spec["when"](st)
        default_id = "preset-" + name
    text = st.get("text") or spec["text"](st)
    acts = []
    if st.get("toast", True):
        t = {"do": "toast", "text": text, "level": st.get("level", spec["level"])}
        for k in ("sound", "title"):
            if k in st:
                t[k] = st[k]
        acts.append(t)
    if st.get("chat", True):
        acts.append({"do": "chat", "text": text})
    if not acts:
        fail(["toast=false and chat=false would leave the rule with nothing to do"])
    rule = {"id": st.get("id", default_id), "when": when, "then": acts, "cooldown_s": st.get("cooldown_s", spec["cooldown"])}
    for k in ("once", "enabled", "active_only", "defer_max_s"):
        if k in st:
            rule[k] = st[k]
    return rule


PRESET_NAMES = ("bags-full", "durability", "deaths", "buff", "lower-rank")


def cmd_rule_add(a):
    raw = a.json if a.json is not None else sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError as e:
        fail(["rule JSON is not valid: %s" % e])
    rules = data if isinstance(data, list) else [data]
    if not rules or not all(isinstance(r, dict) for r in rules):
        fail(["pass one rule object or a list of them: {\"id\", \"when\": {\"type\"...}, \"then\": [{\"do\"...}]}"])
    run(a, [dict(r, op="rule") if r.get("op") in (None, "rule") else r for r in rules])


def cmd_rule_preset(a):
    run(a, dict(preset_rule(a.name, parse_sets(a.set)), op="rule"))


def cmd_rule_remove(a):
    run(a, {"op": "rule_remove", "id": a.id})


def cmd_rule_clear(a):
    run(a, {"op": "rules_clear"})


def cmd_rule_list(a):
    rules = current_doc(a).get("rules", [])
    print(json.dumps({"ok": True, "count": len(rules), "rules": rules}, indent=None if a.compact else 2))


def cmd_coach(a):
    op = {"op": "coach_answer" if a.answer else "coach", "text": a.text, "chat": not a.no_chat, "toast": a.toast}
    if a.answer:
        op["id"] = a.answer
    for k in ("level", "sound"):
        if getattr(a, k) is not None:
            op[k] = getattr(a, k)
    run(a, op)


def get_ok(a, route):
    code, res = get_json(base_url(a), route)
    if code != 200 or not res.get("ok", True):
        fail(res.get("errors") or [json.dumps(res)[:300]])
    return res


def events_route(a, since=None):
    params = {"since": since, "types": a.types, "limit": a.limit, "wait": getattr(a, "wait", None)}
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return "events" + ("?" + q if q else "")


def cmd_events(a):
    if getattr(a, "wait", None):
        if a.since is None:
            fail(["--wait needs --since <eid> (the `next` of your last call)"])
        code, body = http("%s/%s%s%s" % (base_url(a), events_route(a, a.since), "&", nocache()), timeout=a.wait + 15)
        sys.stdout.write(body.decode("utf-8", "replace"))
        return
    if not a.follow:
        print(json.dumps(get_ok(a, events_route(a, a.since)), indent=None if a.compact else 2))
        return
    since = a.since if a.since is not None else get_ok(a, "events?limit=1")["last_eid"]
    deadline = time.time() + a.duration if a.duration else None
    try:
        while True:
            res = get_ok(a, events_route(a, since))
            for e in res["events"]:
                print(json.dumps(e), flush=True)
            since = res["next"]
            if deadline and time.time() >= deadline:
                return
            time.sleep(max(0.2, a.interval))
    except KeyboardInterrupt:
        return


def cmd_npcs(a):
    route = "npcs" + ("?" + urllib.parse.urlencode({"name": a.name}) if a.name else "")
    print(json.dumps(get_ok(a, route), indent=None if a.compact else 2))


def cmd_session(a):
    print(json.dumps(get_ok(a, "session"), indent=None if a.compact else 2))


def cmd_questions(a):
    print(json.dumps(get_ok(a, "coach/questions" + ("?all=1" if a.all else "")), indent=None if a.compact else 2))


def main():
    ap = argparse.ArgumentParser(prog="wowui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="endpoint, e.g. https://host/ui/<secret> (default: $WOWUI_URL, else the config of WoW Mentor on this Mac)")
    ap.add_argument("--partial", action="store_true", help="for ops: apply the valid ones and list the rest under `rejected` instead of failing the whole batch")
    ap.add_argument("--op-id", dest="op_id", help="idempotency key for ops (default: a fresh uuid per invocation; retries reuse it, so nothing applies twice)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")
    common = argparse.ArgumentParser(add_help=False)          # the same two flags after the sub-command: `wowui ops -f x.json --partial`
    common.add_argument("--partial", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--op-id", dest="op_id", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    OP_COMMANDS = {"panel", "plan", "add-task", "check", "remove", "text", "progress", "callout", "component", "toast", "say", "waypoint",
                   "action", "theme", "ops", "clear", "reset", "builtin", "coach", "watch", "verdict"}

    def add(name, fn, help_, style=False):
        p = sub.add_parser(name, help=help_, description=help_, parents=[common] if name in OP_COMMANDS else [])
        p.set_defaults(fn=fn)
        if style:
            add_style_flags(p)
        return p

    p = add("panel", cmd_panel, "create or restyle a panel (children untouched unless --extra has children)", style=True); p.add_argument("id")
    p = add("plan", cmd_plan, "one-shot panel with a checklist: wowui plan next --title 'Next steps' --task 'Turn in X' --task '!Kill 10 Y'", style=True)
    p.add_argument("id"); p.add_argument("--task", action="append", required=True, help="'Text', 'id::Text' or 'id::Text::note'; prefix ! high priority, ~ medium")
    p.add_argument("--intro"); p.add_argument("--progress", choices=["bags", "durability"]); p.add_argument("--callout")
    p.add_argument("--level", default="tip", choices=["info", "tip", "good", "warn", "bad"])
    p = add("add-task", cmd_add_task, "append a task to a panel's checklist"); p.add_argument("panel"); p.add_argument("text")
    p.add_argument("--id"); p.add_argument("--note"); p.add_argument("--priority", choices=["high", "med", "low"]); p.add_argument("--checklist")
    p = add("check", cmd_check, "tick (or --undo) a checklist item"); p.add_argument("panel"); p.add_argument("item"); p.add_argument("--undo", action="store_true")
    p = add("remove", cmd_remove, "remove a panel, or a child/item with --child/--item"); p.add_argument("panel"); p.add_argument("--child"); p.add_argument("--item")
    p = add("text", cmd_text, "add or update a text line in a panel"); p.add_argument("panel"); p.add_argument("text"); p.add_argument("--id")
    p.add_argument("--muted", action="store_true"); p.add_argument("--color"); p.add_argument("--size", type=float)
    p = add("progress", cmd_progress, "add or update a progress bar"); p.add_argument("panel"); p.add_argument("--bind", choices=["bags", "durability"])
    p.add_argument("--value", type=float); p.add_argument("--max", type=float); p.add_argument("--label"); p.add_argument("--id"); p.add_argument("--color")
    p = add("callout", cmd_callout, "add or update a highlighted note"); p.add_argument("panel"); p.add_argument("text")
    p.add_argument("--level", default="tip", choices=["info", "tip", "good", "warn", "bad"]); p.add_argument("--title"); p.add_argument("--id")
    p = add("component", cmd_component, "add or update any component from JSON (kv, timer, icon, row, divider...)"); p.add_argument("panel"); p.add_argument("json"); p.add_argument("--after")
    p = add("toast", cmd_toast, "show a short banner at the top of the screen"); p.add_argument("text"); p.add_argument("--title")
    p.add_argument("--level", default="info", choices=["info", "tip", "good", "warn", "bad"]); p.add_argument("--sound", choices=["none", "ping", "warning", "quest", "levelup", "ready"]); p.add_argument("--ttl", type=float)
    p = add("say", cmd_say, "print a 'Mentor:' line in the player's chat window"); p.add_argument("text"); p.add_argument("--sound", choices=["ping", "warning", "quest", "levelup", "ready"])
    p = add("waypoint", cmd_waypoint, "pin a map location (TomTom if installed, otherwise the native map pin)"); p.add_argument("id", nargs="?")
    p.add_argument("--map", type=int, help="uiMapID, e.g. 1429 Elwynn Forest"); p.add_argument("--x", type=float); p.add_argument("--y", type=float)
    p.add_argument("--title"); p.add_argument("--track", action="store_true", help="make it the active arrow"); p.add_argument("--clear", action="store_true")
    p.add_argument("--quest", type=int, help="also focus this quest in Questie's tracker (questID)")
    p = add("action", cmd_action, "one-shot client actions: flash, sound, chat, track_quest (focus in Questie), unfocus_quest, guidelime --cmd skip|complete|toggle_window|arrow_tomtom|arrow_builtin|arrow_show|arrow_hide|open_guide"); p.add_argument("name", choices=["flash", "sound", "chat", "track_quest", "unfocus_quest", "guidelime", "speak"])
    p.add_argument("--cmd", choices=["skip", "complete", "toggle_window", "arrow_tomtom", "arrow_builtin", "arrow_show", "arrow_hide", "open_guide"]); p.add_argument("--guide")
    p.add_argument("--text"); p.add_argument("--sound", choices=["none", "ping", "warning", "quest", "levelup", "ready"]); p.add_argument("--quest", type=int)
    p = add("theme", cmd_theme, "change global style tokens")
    for f in ("accent", "bg", "border", "text", "muted", "good", "warn", "bad", "font"):
        p.add_argument("--" + f)
    for f in ("bg_alpha", "border_alpha", "size", "padding", "gap"):
        p.add_argument("--" + f.replace("_", "-"), type=float, dest=f)
    p.add_argument("--shadow", choices=["on", "off"]); p.add_argument("--outline", choices=["on", "off"])
    p = add("ops", cmd_ops, "send raw ops JSON from a file or stdin (an op, a list, or {\"ops\": [...]})"); p.add_argument("-f", "--file")
    add("clear", cmd_simple("clear"), "remove all panels and waypoints")
    add("reset", cmd_simple("reset"), "remove everything including theme")
    p = add("state", cmd_state, "what is on the player's screen and what they did (ticked items, moved panels)"); p.add_argument("--compact", action="store_true")
    add("doc", cmd_doc, "print the full overlay document")
    add("schema", cmd_schema, "print every op, component and field (JSON)")
    add("guide", cmd_guide, "print the agent guide")
    add("example", cmd_example, "print an example set of ops")
    add("demo", cmd_demo, "send the example ops to the overlay")
    add("feed", cmd_feed, "print the live game text feed (needs the read token)")
    p = add("brief", cmd_brief, "RUN THIS FIRST after any context loss: your saved memory, what is on screen, guide status, trimmed game feed")
    mp = sub.add_parser("memory", help="your persistent notes (survive context resets): show | add | summary | remove | clear",
                        description="Keep what you decided and why. `wowui brief` prints it back after a context reset.")
    msub = mp.add_subparsers(dest="memcmd", required=True, metavar="memory-command")
    for nm, fn, hp in (("show", cmd_mem_show, "print summary and notes"), ("add", cmd_mem_add, "append a short note"),
                       ("summary", cmd_mem_summary, "replace the running summary (what the plan is and why)"),
                       ("remove", cmd_mem_remove, "delete a note by id"), ("clear", cmd_mem_clear, "forget everything")):
        q = msub.add_parser(nm, help=hp, description=hp)
        q.set_defaults(fn=fn)
        if nm == "add":
            q.add_argument("text"); q.add_argument("--tag")
        elif nm == "summary":
            q.add_argument("text")
        elif nm == "remove":
            q.add_argument("id", type=int)
    p = add("coach", cmd_coach, "send the player a message: shown in the coach inbox and chat (and a banner with --toast)"); p.add_argument("text")
    p.add_argument("--toast", action="store_true", help="also show a banner"); p.add_argument("--no-chat", action="store_true", help="do not print a chat line")
    p.add_argument("--level", choices=["info", "tip", "good", "warn", "bad"]); p.add_argument("--sound", choices=["none", "ping", "warning", "quest", "levelup", "ready"])
    p.add_argument("--answer", metavar="QUESTION_ID", help="answer an open question the player asked with /coach (see `wowui questions`); closes it")
    rp = sub.add_parser("rule", help="rules that run instantly inside the game: add | preset | list | remove | clear",
                        description="A rule is {id, when:{type..}, then:[{do..}], cooldown_s, once, enabled}; the addon evaluates it locally, no polling. `wowui schema` lists every condition and action.")
    rsub = rp.add_subparsers(dest="rulecmd", required=True, metavar="rule-command")
    rcommon = [common]
    q = rsub.add_parser("add", help="add or replace a rule from JSON (--json or stdin)", parents=rcommon); q.set_defaults(fn=cmd_rule_add); q.add_argument("--json")
    q = rsub.add_parser("preset", help="one of: " + ", ".join(PRESET_NAMES), parents=rcommon, description="Ready-made rules. Tune with --set key=value (repeatable): id, cooldown_s, once, enabled, text, level, sound, toast, chat, plus n (bags-full), pct (durability), count and minutes (deaths), name/spell/pet/ooc (buff).")
    q.set_defaults(fn=cmd_rule_preset); q.add_argument("name", choices=PRESET_NAMES); q.add_argument("--set", action="append", metavar="KEY=VALUE")
    q = rsub.add_parser("list", help="rules currently loaded"); q.set_defaults(fn=cmd_rule_list); q.add_argument("--compact", action="store_true")
    q = rsub.add_parser("remove", help="remove a rule by id", parents=rcommon); q.set_defaults(fn=cmd_rule_remove); q.add_argument("id")
    q = rsub.add_parser("clear", help="remove every rule", parents=rcommon); q.set_defaults(fn=cmd_rule_clear)
    p = add("events", cmd_events, "structured game events (death, fight, quest_complete, bags_full...) in the order they happened")
    p.add_argument("--since", type=int, help="only events after this eid (the `next` of your last call); without it: the newest events (or, with --follow, only new ones)")
    p.add_argument("--types", help="comma list, e.g. death,fight"); p.add_argument("--limit", type=int, help="max events (default 100, max 500)")
    p.add_argument("--wait", type=int, help="long-poll: with --since, block up to this many seconds (max 25) until a new event arrives, then print it"); p.add_argument("--follow", action="store_true", help="keep polling and print one JSON event per line"); p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--duration", type=float, help="with --follow: stop after this many seconds"); p.add_argument("--compact", action="store_true")
    p = add("npcs", cmd_npcs, "NPCs the player has met (name, id, map, x, y, kind, count, last_seen)"); p.add_argument("--name"); p.add_argument("--compact", action="store_true")
    p = add("session", cmd_session, "is the player in the game? state, foreground, last login/logout, bridge heartbeat"); p.add_argument("--compact", action="store_true")
    p = add("questions", cmd_questions, "open questions the player asked with /coach <text>"); p.add_argument("--all", action="store_true", help="include answered ones"); p.add_argument("--compact", action="store_true")
    p = add("builtin", cmd_builtin, "show or hide the built-in Character panel (me), or turn spoken advice on or off (voice: the game's text to speech, off by default, the player can override with /wfui voice)"); p.add_argument("which", choices=["me", "voice"]); p.add_argument("state", choices=["on", "off"])
    tp = sub.add_parser("tug", help="live-edit the Guidelime TUGs leveling guide on the player's Mac (see `wowui tug --help`)",
                        description="Read, search and edit the guide files. Edits are validated here, written to the player's files by the bridge with a backup, and the player is asked to /reload.")
    tsub = tp.add_subparsers(dest="tugcmd", required=True, metavar="tug-command")

    def tadd(name, fn, help_):
        p = tsub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    def addr_flags(p):
        p.add_argument("--line", type=int, help="1-based step number as printed by `tug show`")
        p.add_argument("--match", help="text that appears on exactly one step (use --nth if several)")
        p.add_argument("--nth", type=int); p.add_argument("--expect", help="text the step must contain (safety check)")

    p = tadd("list", cmd_tug_list, "list guides (name, lines, title, next guide)"); p.add_argument("--filter"); p.add_argument("--json", action="store_true")
    p = tadd("show", cmd_tug_show, "print a guide with step numbers"); p.add_argument("guide"); p.add_argument("--from", type=int, dest="frm"); p.add_argument("--to", type=int)
    p = tadd("search", cmd_tug_search, "find steps containing text across all guides"); p.add_argument("text"); p.add_argument("--guide")
    p = tadd("route", cmd_tug_route, "follow the [NX] chain from a guide"); p.add_argument("--start")
    p = tadd("replace", cmd_tug_replace, "replace one step (text may hold \\n for several steps)"); p.add_argument("guide"); addr_flags(p); p.add_argument("text"); p.add_argument("--note")
    p = tadd("insert", cmd_tug_insert, "insert step(s) after/before a step, or at start/end"); p.add_argument("guide"); p.add_argument("text")
    p.add_argument("--after-line", type=int); p.add_argument("--after-match"); p.add_argument("--before-line", type=int); p.add_argument("--before-match")
    p.add_argument("--at", choices=["start", "end"]); p.add_argument("--nth", type=int); p.add_argument("--note")
    p = tadd("delete", cmd_tug_delete, "delete a step (or --count of them)"); p.add_argument("guide"); addr_flags(p); p.add_argument("--count", type=int); p.add_argument("--note")
    p = tadd("sub", cmd_tug_sub, "replace text inside a step (or everywhere with --all)"); p.add_argument("guide"); p.add_argument("find"); p.add_argument("replace")
    addr_flags(p); p.add_argument("--all", action="store_true"); p.add_argument("--note")
    p = tadd("set", cmd_tug_set, "replace the whole guide body from a file or stdin"); p.add_argument("guide"); p.add_argument("-f", "--file"); p.add_argument("--note")
    p = tadd("ops", cmd_tug_ops, "send raw edit JSON ({guide, ops:[...]} or {edits:[...]}) from a file or stdin"); p.add_argument("-f", "--file")
    p = tadd("history", cmd_tug_history, "list restore points for a guide"); p.add_argument("guide")
    p = tadd("restore", cmd_tug_restore, "restore a guide: orig (as first found) or a version id from `tug history`"); p.add_argument("guide"); p.add_argument("version"); p.add_argument("--note")
    p = add("watch", cmd_watch, "ask the game to report details, stats, vendor and AH prices and owned count for items (feed section ITEM_WATCH, up to 30)"); p.add_argument("ids", nargs="*")
    p.add_argument("--add", nargs="+"); p.add_argument("--remove", nargs="+"); p.add_argument("--clear", action="store_true"); p.add_argument("--spells", nargs="+", help="spell ids to report in SPELL_WATCH: known, cooldown, usable, in range of the target")
    p = add("verdict", cmd_verdict, "show a one-line opinion in an item's tooltip in the game (Mentor: keep for AH, about 3s); up to 40 items"); p.add_argument("item", nargs="?"); p.add_argument("--text"); p.add_argument("--remove", action="store_true"); p.add_argument("--clear", action="store_true")
    dp = add("db", cmd_db, "look up Forever quests, NPCs, items, trainers and chains from the offline game database (no web search needed)")
    dsub = dp.add_subparsers(dest="dbcmd", required=True, metavar="db-command")
    def dadd(name, help_, *flags):
        p = dsub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=cmd_db)
        for f in flags:
            p.add_argument("--" + f.replace("_", "-") if f != "klass" else "--class", dest=f, help=f)
        return p
    dsub.add_parser("status", help="is the database loaded on the server, and which build").set_defaults(fn=cmd_db)
    for nm in ("quest", "npc", "item", "object", "chain"):
        dadd(nm, "%s by --id or --name (partial names match)" % nm, "id", "name")
    dadd("search", "find quests, NPCs, items and objects by name fragment", "q")
    dadd("find-quests", "quests in a zone for a level: --zone Elwynn --level 10 --class Warlock --race Human [--tol 3]", "zone", "level", "tol", "klass", "race")
    dadd("trainers", "trainers in a zone: --zone 'Stormwind City' --like warlock", "zone", "like")
    p = dadd("waypoint", "where is an NPC or a quest's start/finish: --npc NAME | --quest ID [--which start|finish] [--set] (set pins it with TomTom and focuses the quest)", "npc", "quest", "which")
    p.add_argument("--set", action="store_true"); p.add_argument("--wid", default="w1", help="waypoint id used with --set")
    dadd("spots", "where to find the creatures a quest wants (densest spawn clusters): --quest ID or --npc NAME", "quest", "npc")
    dadd("training", "spells a class can train with cost (names via `wowui watch --spells`): --class Warlock [--level N] [--from M]", "klass", "level", "frm")
    dadd("validate", "check a guide's quest ids, class/race gates and [G] coordinates: --guide NAME [--class Warlock --race Human]", "guide", "klass", "race")
    p = add("section", cmd_section, "print feed sections by name (e.g. wowui section FAST PVP TALENTS_TRAITS): no raw feed link needed"); p.add_argument("names", nargs="+")
    p = add("verify", cmd_verify, "contradiction guard: Guidelime's next step vs overlay tasks, waypoint and Questie focus; run it before every message that names a next step")
    p = add("route", cmd_route, "order the quests in the log into stops (turn-ins first), nearest first; --set N pins the first N stops (the first one with the arrow and quest focus)"); p.add_argument("--set", type=int); p.add_argument("--only", help="comma list of quest ids")
    add("autopsy", cmd_autopsy, "everything about the latest death in one place (events, fight, position, pet, buffs, bags, durability)")
    add("debrief", cmd_debrief, "session summary data: events by type, deaths, fights, quests, professions and your lesson notes")
    p = add("align", cmd_align, "audit the guides ahead of the player against the live quest log and Questie's completed list (STALE / HAVE / MISSING / FOCUS)"); p.add_argument("guide", nargs="?", help="current guide (default: read from the game)")
    p.add_argument("--ahead", type=int, default=2, help="how many guides after it to check"); p.add_argument("--also", help="comma list of extra guides (side trips off the [NX] chain)"); p.add_argument("--completed", help="extra completed quest ids")
    p = add("pin", cmd_pin, "put an urgent task at the top of what is left in the current guide AND add it to the overlay panel, so they agree"); p.add_argument("text", help="a Guidelime step, e.g. 'Buy 2 Linen Cloth [AH]'")
    p.add_argument("--go", help="coordinates like '61.9,30.6Stormwind City' (adds [G..])"); p.add_argument("--guide"); p.add_argument("--panel", default="next"); p.add_argument("--id"); p.add_argument("--note"); p.add_argument("--no-overlay", action="store_true")
    tadd("status", cmd_tug_status, "pending/applied edits and whether the player still needs to /reload")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
