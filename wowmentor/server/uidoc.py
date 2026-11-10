"""Overlay document model for the WoW mentor UI.

One authoritative JSON document describes everything the in-game overlay shows. Agents change it with small
"ops" (upsert a panel, tick a checklist item, show a toast, set a waypoint...). Every op is validated and
normalised here, so the addon only ever receives a clean, bounded document. Pure stdlib; shared by the feed
server and the tests.
"""
import json, re, time

VERSION = 1
MAX_WIRE_BYTES = 60_000
MAX_PANELS = 12
MAX_CHILDREN = 120
MAX_ROW_CHILDREN = 6
MAX_ITEMS = 60
MAX_TEXT = 800
MAX_HISTORY = 10
MAX_OPS = 100

ANCHORS = ("TOPLEFT", "TOP", "TOPRIGHT", "LEFT", "CENTER", "RIGHT", "BOTTOMLEFT", "BOTTOM", "BOTTOMRIGHT")
LEVELS = ("info", "tip", "good", "warn", "bad")
PRIORITIES = ("high", "med", "low")
COMBAT = ("show", "dim", "hide")
FONTS = ("default", "friz", "arial", "morpheus", "skurri")
SOUNDS = ("none", "ping", "warning", "quest", "levelup", "ready")
ACTIONS = ("flash", "sound", "chat", "track_quest")
BINDS = ("bags", "durability")        # XP and level are never shown on the overlay
COLOR_NAMES = {
    "white": "#ffffff", "black": "#000000", "red": "#ff4040", "green": "#40d060", "blue": "#4090ff", "yellow": "#ffd100",
    "orange": "#ff8a1c", "cyan": "#30d5ff", "magenta": "#e060ff", "gray": "#9a9a9a", "grey": "#9a9a9a", "gold": "#f5c518",
}
HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
COMPONENT_TYPES = ("heading", "text", "checklist", "progress", "kv", "callout", "timer", "icon", "divider", "spacer", "row")


class OpError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- field validators
def _str(v, n=MAX_TEXT):
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise ValueError("must be a string")
    return str(v)[:n]


def _num(lo, hi):
    def f(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("must be a number")
        return max(lo, min(hi, float(v))) if not float(v).is_integer() else int(max(lo, min(hi, v)))
    return f


def _int(lo, hi):
    def f(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("must be an integer")
        return int(max(lo, min(hi, v)))
    return f


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _enum(options):
    def f(v):
        if v not in options:
            raise ValueError("must be one of " + ", ".join(options))
        return v
    return f


def _color(v):
    if not isinstance(v, str):
        raise ValueError("must be a colour string like #ff8800 or a name like gold")
    s = v.strip().lower()
    if s in COLOR_NAMES:
        return COLOR_NAMES[s]
    if HEX.match(s):
        return s if s.startswith("#") else "#" + s
    raise ValueError("bad colour %r (use #rrggbb, #rrggbbaa or one of %s)" % (v, ", ".join(sorted(COLOR_NAMES))))


def _bind(v):
    if v not in BINDS:
        raise ValueError("must be one of %s (XP and level are never shown on the overlay)" % ", ".join(BINDS))
    return v


def _id(v):
    if not isinstance(v, (str, int)) or not ID.match(str(v)):
        raise ValueError("ids are 1-40 chars of letters, digits and _ . : -")
    return str(v)


def _visibility(v):
    if not isinstance(v, dict):
        raise ValueError("must be an object")
    spec = {
        "combat": _enum(COMBAT), "dim": _num(0, 1), "mouseover": _bool, "idleFade": _num(0, 3600), "idleAlpha": _num(0, 1),
        "hideWhenMounted": _bool, "hideInInstance": _bool, "minLevel": _int(1, 80), "maxLevel": _int(1, 80),
    }
    out = {}
    for k, val in v.items():
        if k not in spec:
            raise ValueError("unknown visibility field %r (allowed: %s)" % (k, ", ".join(sorted(spec))))
        out[k] = spec[k](val)
    return out


THEME_FIELDS = {
    "font": _enum(FONTS), "size": _num(8, 24), "accent": _color, "bg": _color, "bgAlpha": _num(0, 1), "border": _color,
    "borderAlpha": _num(0, 1), "text": _color, "muted": _color, "good": _color, "warn": _color, "bad": _color,
    "padding": _num(0, 24), "gap": _num(0, 24), "shadow": _bool, "outline": _bool, "cornerAccent": _bool,
}
PANEL_FIELDS = {
    "title": _str, "subtitle": _str, "anchor": _enum(ANCHORS), "x": _num(-4000, 4000), "y": _num(-4000, 4000), "w": _num(120, 900),
    "maxH": _num(40, 1400), "scale": _num(0.5, 2.5), "alpha": _num(0, 1), "bg": _color, "bgAlpha": _num(0, 1), "border": _color,
    "borderAlpha": _num(0, 1), "accent": _color, "fontSize": _num(8, 24), "font": _enum(FONTS), "padding": _num(0, 24),
    "gap": _num(0, 24), "collapsed": _bool, "collapsible": _bool, "draggable": _bool, "interactive": _bool, "header": _bool,
    "strata": _enum(("BACKGROUND", "LOW", "MEDIUM", "HIGH")), "ttl": _num(1, 86400), "visibility": _visibility, "pin": _bool,
}
COMPONENT_COMMON = {"id": _id, "color": _color, "size": _num(8, 28), "align": _enum(("left", "center", "right")), "alpha": _num(0, 1),
                    "indent": _num(0, 60), "when": _enum(("always", "combat", "outofcombat"))}
COMPONENT_FIELDS = {
    "heading": {"text": _str, "icon": _str},
    "text": {"text": _str, "muted": _bool, "wrap": _bool},
    "progress": {"label": _str, "value": _num(-1e9, 1e9), "max": _num(0, 1e9), "bind": _bind, "height": _num(4, 30), "showText": _bool,
                 "fill": _color},
    "callout": {"text": _str, "title": _str, "level": _enum(LEVELS)},
    "timer": {"text": _str, "until": _num(0, 4e9), "seconds": _num(0, 864000), "format": _enum(("clock", "words"))},
    "icon": {"text": _str, "spell": _int(1, 10_000_000), "item": _int(1, 10_000_000), "texture": _str, "iconSize": _num(12, 64)},
    "divider": {"thickness": _num(1, 4)},
    "spacer": {"height": _num(1, 80)},
    "kv": {"keyColor": _color},
    "checklist": {"title": _str, "numbered": _bool},
    "row": {"gap": _num(0, 24)},
}
AUTO_TYPES = {
    "quest_done": {"quest": (_int(1, 10_000_000), True)},
    "spell_known": {"spell": (_int(1, 10_000_000), False), "name": (_str, False)},
    "skill_gte": {"skill": (_str, True), "value": (_int(1, 600), True)},
    "item_in_bags": {"item": (_int(1, 10_000_000), True), "count": (_int(1, 1000), False)},
    "zone": {"zone": (_str, True)},
}


def _auto(v):
    if not isinstance(v, dict) or v.get("type") not in AUTO_TYPES:
        raise ValueError("must be an object with type one of %s" % ", ".join(AUTO_TYPES))
    out = {"type": v["type"]}
    spec = AUTO_TYPES[v["type"]]
    for k, (fn, required) in spec.items():
        if k in v:
            out[k] = fn(v[k])
        elif required:
            raise ValueError("%s needs %s" % (v["type"], k))
    extra = set(v) - set(spec) - {"type"}
    if extra:
        raise ValueError("unknown field(s) %s for %s" % (", ".join(sorted(extra)), v["type"]))
    if v["type"] == "spell_known" and "spell" not in out and "name" not in out:
        raise ValueError("spell_known needs spell (id) or name")
    return out


ITEM_FIELDS = {"auto": _auto, "id": _id, "text": _str, "done": _bool, "note": _str, "priority": _enum(PRIORITIES), "icon": _str, "reset": _bool}
WAYPOINT_FIELDS = {"clear_yards": _num(1, 500), "task": _str, "id": _id, "map": _int(1, 100000), "x": _num(0, 100), "y": _num(0, 100), "title": _str, "track": _bool}


def _apply_fields(spec, src, errs, path, allow_extra=()):
    out = {}
    for k, v in src.items():
        if k in allow_extra:
            continue
        if k not in spec:
            errs.append("%s.%s: unknown field (allowed: %s)" % (path, k, ", ".join(sorted(set(spec) | set(allow_extra)))))
            continue
        try:
            out[k] = spec[k](v)
        except ValueError as e:
            errs.append("%s.%s: %s" % (path, k, e))
    return out


def normalize_item(it, idx, errs, path):
    if isinstance(it, str):
        it = {"text": it}
    if not isinstance(it, dict):
        errs.append("%s: item must be a string or object" % path)
        return None
    out = _apply_fields(ITEM_FIELDS, {k: v for k, v in it.items() if k != "ver"}, errs, path)
    if "text" not in out:
        errs.append("%s: item needs text" % path)
        return None
    out.setdefault("id", "i%d" % (idx + 1))
    if "ver" in it and isinstance(it["ver"], int):
        out["ver"] = it["ver"]
    return out


def normalize_component(c, errs, path, depth=0):
    if not isinstance(c, dict):
        errs.append("%s: component must be an object" % path)
        return None
    t = c.get("type")
    if t not in COMPONENT_TYPES:
        errs.append("%s.type: must be one of %s" % (path, ", ".join(COMPONENT_TYPES)))
        return None
    spec = dict(COMPONENT_COMMON)
    spec.update(COMPONENT_FIELDS[t])
    out = _apply_fields(spec, c, errs, path, allow_extra=("type", "items", "rows", "children"))
    out["type"] = t
    if t == "checklist":
        items = c.get("items")
        if not isinstance(items, list) or not items:
            errs.append("%s.items: checklist needs a non-empty list of items" % path)
        else:
            norm, seen = [], set()
            for i, it in enumerate(items[:MAX_ITEMS]):
                n = normalize_item(it, i, errs, "%s.items[%d]" % (path, i))
                if n:
                    if n["id"] in seen:
                        errs.append("%s.items[%d].id: duplicate item id %r" % (path, i, n["id"]))
                    seen.add(n["id"])
                    norm.append(n)
            out["items"] = norm
    elif t == "kv":
        rows = c.get("rows")
        if not isinstance(rows, list) or not rows:
            errs.append("%s.rows: kv needs a list of [key, value] pairs" % path)
        else:
            out["rows"] = []
            for i, r in enumerate(rows[:40]):
                if isinstance(r, dict) and "k" in r:
                    r = [r.get("k"), r.get("v", "")]
                if not (isinstance(r, (list, tuple)) and len(r) == 2):
                    errs.append("%s.rows[%d]: must be [key, value]" % (path, i))
                    continue
                out["rows"].append([str(r[0])[:80], str(r[1])[:200]])
    elif t == "row":
        kids = c.get("children")
        if depth >= 1:
            errs.append("%s: rows cannot be nested" % path)
        elif not isinstance(kids, list) or not kids:
            errs.append("%s.children: row needs a list of components" % path)
        else:
            out["children"] = [n for n in (normalize_component(k, errs, "%s.children[%d]" % (path, i), depth + 1)
                                           for i, k in enumerate(kids[:MAX_ROW_CHILDREN])) if n]
            for k in out["children"]:
                if k["type"] in ("row", "checklist"):
                    errs.append("%s: rows may only contain simple components" % path)
    if t in ("heading", "text", "callout") and not out.get("text"):
        errs.append("%s.text: required for %s" % (path, t))
    if t == "progress" and "bind" not in out and "value" not in out:
        errs.append("%s: progress needs value (and max) or bind" % path)
    if t == "timer" and "until" not in out and "seconds" not in out:
        errs.append("%s: timer needs until (epoch seconds) or seconds" % path)
    if t == "icon" and not any(k in out for k in ("spell", "item", "texture")):
        errs.append("%s: icon needs spell, item or texture" % path)
    return out


def normalize_panel(p, errs, path):
    if not isinstance(p, dict):
        errs.append("%s: panel must be an object" % path)
        return None
    out = _apply_fields(PANEL_FIELDS, p, errs, path, allow_extra=("id", "children"))
    try:
        out["id"] = _id(p.get("id"))
    except ValueError as e:
        errs.append("%s.id: %s" % (path, e))
        return None
    if "children" in p:
        kids = p["children"]
        if not isinstance(kids, list):
            errs.append("%s.children: must be a list" % path)
        else:
            out["children"] = [n for n in (normalize_component(k, errs, "%s.children[%d]" % (path, i)) for i, k in enumerate(kids[:MAX_CHILDREN])) if n]
            seen = set()
            for k in out["children"]:
                if "id" in k:
                    if k["id"] in seen:
                        errs.append("%s: duplicate child id %r" % (path, k["id"]))
                    seen.add(k["id"])
    return out


def normalize_waypoint(w, errs, path):
    if not isinstance(w, dict):
        errs.append("%s: waypoint must be an object" % path)
        return None
    out = _apply_fields(WAYPOINT_FIELDS, w, errs, path)
    for k in ("id", "map", "x", "y"):
        if k not in out:
            errs.append("%s.%s: required" % (path, k))
    if "x" in out and "y" in out and 0 < out["x"] <= 1 and 0 < out["y"] <= 1:
        out["x"], out["y"] = out["x"] * 100, out["y"] * 100   # allow 0-1 fractions
    return out if not any(e.startswith(path) for e in errs) else None


# ---------------------------------------------------------------- rules (evaluated by the addon, locally and instantly)
WHEN_TYPES = {
    "bags_free_lte": {"n": (_int(0, 200), True)},
    "durability_lte": {"pct": (_num(0, 100), True)},
    "deaths_in_subzone": {"count": (_int(1, 20), False), "minutes": (_num(1, 120), False)},
    "buff_missing": {"name": (_str, False), "spell": (_int(1, 10_000_000), False), "pet": (_bool, False), "ooc": (_bool, False)},
    "level_up": {}, "death": {}, "logout": {}, "entering_world": {}, "merchant_show": {}, "trainer_show": {}, "quest_reward": {},
    "loot_opened": {}, "lower_rank_on_bar": {},
    "spell_learned": {"name": (_str, False)},
    "quest_complete": {"quest": (_int(1, 10_000_000), False)},
    "zone_changed": {"zone": (_str, False), "subzone": (_str, False)},
    "target_elite": {"level_diff": (_int(-20, 20), False), "rare": (_bool, False)},
    "item_looted": {"min_quality": (_int(0, 5), False)},
    "item_equipped": {},
}
DO_TYPES = {
    "toast": {"text": (_str, True), "level": (_enum(LEVELS), False), "sound": (_enum(SOUNDS), False), "title": (_str, False)},
    "chat": {"text": (_str, True), "sound": (_enum(SOUNDS), False)},
    "check": {"panel": (_id, True), "item": (_id, True), "done": (_bool, False)},
    "waypoint": {"id": (_id, True), "map": (_int(1, 100000), True), "x": (_num(0, 100), True), "y": (_num(0, 100), True),
                 "title": (_str, False), "track": (_bool, False), "clear_yards": (_num(1, 500), False)},
    "panel_text": {"panel": (_id, True), "child": (_id, True), "text": (_str, True)},
    "sound": {"sound": (_enum(SOUNDS), True)},
    "flash": {},
}
MAX_RULES = 40


def _typed(v, table, key, what, depth=0):
    if not isinstance(v, dict) or v.get(key) not in table and not (what == "when" and v.get(key) in ("all", "any")):
        raise ValueError("must be an object whose %r is one of %s" % (key, ", ".join(sorted(table) + (["all", "any"] if what == "when" else []))))
    t = v[key]
    if what == "when" and t in ("all", "any"):
        kids = v.get("of")
        if depth >= 1 or not isinstance(kids, list) or not 2 <= len(kids) <= 4:
            raise ValueError("%s needs a list of 2 to 4 conditions and cannot be nested" % t)
        return {key: t, "of": [_typed(k, table, key, what, depth + 1) for k in kids]}
    out = {key: t}
    spec = table[t]
    for k, (fn, required) in spec.items():
        if k in v:
            out[k] = fn(v[k])
        elif required:
            raise ValueError("%s needs %s" % (t, k))
    extra = set(v) - set(spec) - {key}
    if extra:
        raise ValueError("unknown field(s) %s for %s (allowed: %s)" % (", ".join(sorted(extra)), t, ", ".join(sorted(spec)) or "none"))
    if t == "buff_missing" and "name" not in out and "spell" not in out:
        raise ValueError("buff_missing needs name or spell")
    return out


def normalize_rule(r, errs, path):
    if not isinstance(r, dict):
        errs.append("%s: rule must be an object" % path)
        return None
    out = {}
    try:
        out["id"] = _id(r.get("id"))
    except ValueError as e:
        errs.append("%s.id: %s" % (path, e))
    for key, fn in (("cooldown_s", _num(0, 86400)), ("once", _bool), ("enabled", _bool)):
        if key in r:
            try:
                out[key] = fn(r[key])
            except ValueError as e:
                errs.append("%s.%s: %s" % (path, key, e))
    try:
        out["when"] = _typed(r.get("when"), WHEN_TYPES, "type", "when")
    except ValueError as e:
        errs.append("%s.when: %s" % (path, e))
    then = r.get("then")
    if not isinstance(then, list) or not 1 <= len(then) <= 4:
        errs.append("%s.then: a list of 1 to 4 actions, each {\"do\": %s}" % (path, "|".join(sorted(DO_TYPES))))
    else:
        out["then"] = []
        for i, a in enumerate(then):
            try:
                out["then"].append(_typed(a, DO_TYPES, "do", "do"))
            except ValueError as e:
                errs.append("%s.then[%d]: %s" % (path, i, e))
    extra = set(r) - {"id", "when", "then", "cooldown_s", "once", "enabled"}
    if extra:
        errs.append("%s: unknown field(s) %s" % (path, ", ".join(sorted(extra))))
    out.setdefault("cooldown_s", 60)
    out.setdefault("once", False)
    out.setdefault("enabled", True)
    return out if not any(e.startswith(path) for e in errs) else None


# ---------------------------------------------------------------- the document
class Doc:
    def __init__(self, data=None):
        d = data or {}
        self.rev = int(d.get("rev", 0))
        self.theme = dict(d.get("theme", {}))
        self.panels = list(d.get("panels", []))
        self.waypoints = list(d.get("waypoints", []))
        self.toasts = list(d.get("toasts", []))
        self.actions = list(d.get("actions", []))
        self.builtin = dict(d.get("builtin", {}))
        self.rules = list(d.get("rules", []))
        self.inbox = list(d.get("inbox", []))
        self.updated = d.get("updated", time.time())

    def to_dict(self):
        return {"v": VERSION, "rev": self.rev, "updated": self.updated, "theme": self.theme, "panels": self.panels,
                "waypoints": self.waypoints, "toasts": self.toasts, "actions": self.actions, "builtin": self.builtin, "rules": self.rules, "inbox": self.inbox}

    def wire(self):
        d = self.to_dict()
        now = time.time()
        d["now"] = int(now)
        d["toasts"] = [t for t in self.toasts if now - t["t"] < max(60, t.get("ttl", 8) + 30)]
        d["actions"] = [a for a in self.actions if now - a["t"] < 120]
        return json.dumps(d, separators=(",", ":"), ensure_ascii=True)

    # -- helpers
    def _panel(self, pid, errs, must=True):
        for p in self.panels:
            if p["id"] == pid:
                return p
        if must:
            errs.append("panel %r does not exist (existing: %s)" % (pid, ", ".join(p["id"] for p in self.panels) or "none"))
        return None

    @staticmethod
    def _find_child(children, cid):
        for c in children:
            if c.get("id") == cid:
                return c
            if c.get("type") == "row":
                r = Doc._find_child(c.get("children", []), cid)
                if r:
                    return r
        return None

    def _bump(self):
        self.rev += 1
        self.updated = time.time()

    def apply(self, op):
        """Apply one op. Returns a list of warnings. Raises OpError with agent-readable messages."""
        if not isinstance(op, dict) or "op" not in op:
            raise OpError(['every op is an object with an "op" field (set, panel, child, check, remove, theme, toast, waypoint, action, clear, reset)'])
        name, errs, warns = op["op"], [], []
        body = {k: v for k, v in op.items() if k != "op"}
        handler = getattr(self, "_op_" + str(name), None)
        if not handler or str(name).startswith("_"):
            raise OpError(["unknown op %r (use: set, panel, child, check, remove, theme, toast, waypoint, action, clear, reset)" % (name,)])
        handler(body, errs, warns)
        if errs:
            raise OpError(errs)
        self._bump()
        return warns

    # -- ops
    def _op_set(self, b, errs, warns):
        d = b.get("doc", b)
        unknown = set(d) - {"theme", "panels", "waypoints"}
        if unknown:
            errs.append("set: unknown field(s) %s (allowed: theme, panels, waypoints)" % ", ".join(sorted(unknown)))
        theme = _apply_fields(THEME_FIELDS, d.get("theme", {}), errs, "theme") if isinstance(d.get("theme", {}), dict) else {}
        panels = []
        for i, p in enumerate(d.get("panels", [])[:MAX_PANELS]):
            n = normalize_panel(p, errs, "panels[%d]" % i)
            if n:
                panels.append(n)
        ids = [p["id"] for p in panels]
        if len(ids) != len(set(ids)):
            errs.append("set: duplicate panel ids")
        wps = [n for n in (normalize_waypoint(w, errs, "waypoints[%d]" % i) for i, w in enumerate(d.get("waypoints", [])[:20])) if n]
        if not errs:
            self.theme, self.panels, self.waypoints = theme, panels, wps

    def _op_panel(self, b, errs, warns):
        p = normalize_panel(b, errs, "panel")
        if not p or errs:
            return
        cur = self._panel(p["id"], errs, must=False)
        if cur:
            cur.update(p)
        else:
            if len(self.panels) >= MAX_PANELS:
                errs.append("panel: at most %d panels (remove one first)" % MAX_PANELS)
                return
            p.setdefault("children", [])
            self.panels.append(p)

    def _op_child(self, b, errs, warns):
        pid = b.get("panel")
        panel = self._panel(pid, errs)
        comp = b.get("component", {k: v for k, v in b.items() if k != "panel"})
        if not isinstance(comp, dict):
            errs.append("child: component must be an object")
            return
        if "id" not in comp and "id" in b:
            comp["id"] = b["id"]
        n = normalize_component(comp, errs, "component")
        if not panel or not n or errs:
            return
        kids = panel.setdefault("children", [])
        cur = self._find_child(kids, n.get("id")) if "id" in n else None
        if cur is not None:
            cur.clear()
            cur.update(n)
        else:
            if len(kids) >= MAX_CHILDREN:
                errs.append("child: panel %r is full (%d components)" % (pid, MAX_CHILDREN))
                return
            after = b.get("after")
            idx = next((i + 1 for i, c in enumerate(kids) if c.get("id") == after), len(kids)) if after else len(kids)
            kids.insert(idx, n)

    def _op_check(self, b, errs, warns):
        panel = self._panel(b.get("panel"), errs)
        if not panel:
            return
        iid = b.get("item")
        done = b.get("done", True)
        if not isinstance(done, bool):
            errs.append("check.done: must be true or false")
            return
        for c in panel.get("children", []):
            if c.get("type") == "checklist":
                for it in c["items"]:
                    if it["id"] == iid:
                        it["done"] = done
                        it["ver"] = self.rev + 1
                        for k in ("text", "note", "priority"):
                            if k in b:
                                try:
                                    it[k] = ITEM_FIELDS[k](b[k])
                                except ValueError as e:
                                    errs.append("check.%s: %s" % (k, e))
                        return
        known = [i["id"] for c in panel.get("children", []) if c.get("type") == "checklist" for i in c["items"]]
        errs.append("check: no checklist item %r in panel %r (items: %s)" % (iid, b.get("panel"), ", ".join(known) or "none"))

    def _op_remove(self, b, errs, warns):
        if "waypoint" in b:
            before = len(self.waypoints)
            self.waypoints = [w for w in self.waypoints if w["id"] != b["waypoint"]]
            if len(self.waypoints) == before:
                errs.append("remove: no waypoint %r" % b["waypoint"])
            return
        pid = b.get("panel")
        panel = self._panel(pid, errs)
        if not panel:
            return
        if "child" in b:
            kids = panel.get("children", [])
            n = len(kids)
            panel["children"] = [c for c in kids if c.get("id") != b["child"]]
            if len(panel["children"]) == n:
                errs.append("remove: no child %r in panel %r" % (b["child"], pid))
        elif "item" in b:
            hit = False
            for c in panel.get("children", []):
                if c.get("type") == "checklist":
                    n = len(c["items"])
                    c["items"] = [i for i in c["items"] if i["id"] != b["item"]]
                    hit = hit or len(c["items"]) != n
            if not hit:
                errs.append("remove: no item %r in panel %r" % (b["item"], pid))
        else:
            self.panels = [p for p in self.panels if p["id"] != pid]

    def _op_theme(self, b, errs, warns):
        t = _apply_fields(THEME_FIELDS, b, errs, "theme")
        if not errs:
            self.theme.update(t)

    def _op_toast(self, b, errs, warns):
        spec = {"text": _str, "level": _enum(LEVELS), "ttl": _num(2, 120), "sound": _enum(SOUNDS), "title": _str}
        t = _apply_fields(spec, b, errs, "toast")
        if "text" not in t:
            errs.append("toast.text: required")
        if not errs:
            t.update(id=self.rev + 1, t=time.time())
            t.setdefault("ttl", 8)
            self.toasts = (self.toasts + [t])[-MAX_HISTORY:]

    def _op_action(self, b, errs, warns):
        spec = {"name": _enum(ACTIONS), "text": _str, "sound": _enum(SOUNDS), "quest": _int(1, 10_000_000)}
        a = _apply_fields(spec, b, errs, "action")
        if "name" not in a:
            errs.append("action.name: required (one of %s)" % ", ".join(ACTIONS))
        elif a["name"] == "chat" and "text" not in a:
            errs.append("action.text: required for chat")
        elif a["name"] == "track_quest" and "quest" not in a:
            errs.append("action.quest: required for track_quest")
        if not errs:
            a.update(id=self.rev + 1, t=time.time())
            self.actions = (self.actions + [a])[-MAX_HISTORY:]

    def _op_waypoint(self, b, errs, warns):
        w = normalize_waypoint(b, errs, "waypoint")
        if not w or errs:
            return
        self.waypoints = [x for x in self.waypoints if x["id"] != w["id"]]
        if len(self.waypoints) >= 20:
            errs.append("waypoint: at most 20 waypoints")
            return
        self.waypoints.append(w)

    def _op_builtin(self, b, errs, warns):
        spec = {"me": _bool}
        t = _apply_fields(spec, b, errs, "builtin")
        if not t and not errs:
            errs.append("builtin: pass me=true|false (the built-in character panel: professions, skills, gear, talents)")
        if not errs:
            self.builtin.update(t)

    def _op_rule(self, b, errs, warns):
        r = normalize_rule(b, errs, "rule")
        if not r or errs:
            return
        self.rules = [x for x in self.rules if x["id"] != r["id"]]
        if len(self.rules) >= MAX_RULES:
            errs.append("rule: at most %d rules (remove one first)" % MAX_RULES)
            return
        self.rules.append(r)

    def _op_rule_remove(self, b, errs, warns):
        before = len(self.rules)
        self.rules = [x for x in self.rules if x["id"] != b.get("id")]
        if len(self.rules) == before:
            errs.append("rule_remove: no rule %r (existing: %s)" % (b.get("id"), ", ".join(x["id"] for x in self.rules) or "none"))

    def _op_rules_clear(self, b, errs, warns):
        self.rules = []

    def _op_coach(self, b, errs, warns):
        spec = {"text": _str, "level": _enum(LEVELS), "toast": _bool, "chat": _bool, "sound": _enum(SOUNDS), "answer_to": _str}
        c = _apply_fields(spec, b, errs, "coach")
        if "text" not in c:
            errs.append("coach.text: required")
        if not errs:
            c["text"] = c["text"][:400]
            c.update(id=self.rev + 1, t=time.time())
            c.setdefault("chat", True)
            c.setdefault("toast", False)
            self.inbox = (self.inbox + [c])[-MAX_HISTORY:]

    def _op_waypoint_clear(self, b, errs, warns):
        self.waypoints = []

    def _op_clear(self, b, errs, warns):
        self.panels, self.waypoints = [], []

    def _op_reset(self, b, errs, warns):
        self.panels, self.waypoints, self.theme, self.toasts, self.actions, self.rules, self.inbox = [], [], {}, [], [], [], []


def apply_ops(doc, payload):
    """payload: one op, a list of ops, or {"ops": [...]}. Atomic: all ops apply or none do."""
    ops = payload.get("ops") if isinstance(payload, dict) and "ops" in payload else payload
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list) or not ops:
        raise OpError(['body must be an op object, a list of ops, or {"ops": [...]}'])
    if len(ops) > MAX_OPS:
        raise OpError(["at most %d ops per request (got %d); split the batch" % (MAX_OPS, len(ops))])
    trial = Doc(json.loads(json.dumps(doc.to_dict())))
    warns = []
    for i, op in enumerate(ops):
        try:
            warns += trial.apply(op)
        except OpError as e:
            raise OpError(["op %d (%s): %s" % (i, op.get("op") if isinstance(op, dict) else "?", m) for m in e.errors])
    wire = trial.wire()
    if len(wire) > MAX_WIRE_BYTES:
        raise OpError(["document is %d bytes after encoding; the limit is %d. Remove components or shorten text." % (len(wire), MAX_WIRE_BYTES)])
    doc.__dict__.update(trial.__dict__)
    return warns


def apply_ops_partial(doc, payload):
    """Like apply_ops, but valid ops are applied and invalid ones are reported: (warnings, rejected, applied_count).
    Raises OpError only when every op was rejected."""
    ops = payload.get("ops") if isinstance(payload, dict) and "ops" in payload else payload
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list) or not ops:
        raise OpError(['body must be an op object, a list of ops, or {"ops": [...]}'])
    if len(ops) > MAX_OPS:
        raise OpError(["at most %d ops per request (got %d); split the batch" % (MAX_OPS, len(ops))])
    trial = Doc(json.loads(json.dumps(doc.to_dict())))
    warns, rejected, applied = [], [], 0
    for i, op in enumerate(ops):
        keep = json.loads(json.dumps(trial.to_dict()))
        try:
            warns += trial.apply(op)
            applied += 1
        except OpError as e:
            trial = Doc(keep)
            rejected.append({"index": i, "op": op.get("op") if isinstance(op, dict) else None, "errors": e.errors})
    if not applied:
        raise OpError(["; ".join(r["errors"][0] for r in rejected)])
    wire = trial.wire()
    if len(wire) > MAX_WIRE_BYTES:
        raise OpError(["document is %d bytes after encoding; the limit is %d." % (len(wire), MAX_WIRE_BYTES)])
    doc.__dict__.update(trial.__dict__)
    return warns, rejected, applied


# ---------------------------------------------------------------- self description for agents
def catalog():
    def names(spec):
        return sorted(spec)
    return {
        "version": VERSION,
        "overview": "One document, changed by ops. Panels hold components. The in-game addon renders it, click-through by default, dimming in combat.",
        "ops": {
            "set": {"doc": "{theme?, panels?, waypoints?} replaces the whole surface"},
            "panel": "upsert a panel by id (fields below); children replaced only if given",
            "child": {"panel": "panel id", "component": "component object; with an id it updates in place", "after": "optional id to insert after"},
            "check": {"panel": "panel id", "item": "checklist item id", "done": True},
            "remove": "one of {panel}, {panel, child}, {panel, item}, {waypoint}",
            "theme": "merge theme fields",
            "toast": {"text": "...", "level": list(LEVELS), "ttl": "seconds", "sound": list(SOUNDS), "title": "optional"},
            "waypoint": "{id, map (uiMapID), x, y (0-100 or 0-1), title, track}",
            "waypoint_clear": "removes all waypoints",
            "rule": "upsert an addon-side rule: {id, when:{type,...}, then:[{do,...}], cooldown_s, once, enabled}. Evaluated instantly in the game, no polling",
            "rule_remove": {"id": "rule id"}, "rules_clear": "remove every rule",
            "coach": {"text": "message", "chat": "print a chat line (default true)", "toast": "also a banner (default false)", "level": list(LEVELS), "sound": list(SOUNDS)},
            "builtin": {"me": "true|false: show or hide the built-in character panel (professions, skills, gear, talents), on by default"},
            "action": {"name": list(ACTIONS), "text": "for chat", "sound": "for sound", "quest": "questID for track_quest"},
            "clear": "removes panels and waypoints", "reset": "removes everything including theme",
        },
        "theme_fields": names(THEME_FIELDS),
        "panel_fields": names(PANEL_FIELDS),
        "visibility_fields": ["combat", "dim", "mouseover", "idleFade", "idleAlpha", "hideWhenMounted", "hideInInstance", "minLevel", "maxLevel"],
        "components": {t: sorted(set(COMPONENT_COMMON) | set(COMPONENT_FIELDS[t])) + (["items"] if t == "checklist" else ["rows"] if t == "kv" else ["children"] if t == "row" else [])
                       for t in COMPONENT_TYPES},
        "item_fields": names(ITEM_FIELDS),
        "colors": "#rrggbb, #rrggbbaa or a name: " + ", ".join(sorted(COLOR_NAMES)),
        "text_markup": {
            "color": "[c=#ff8800]text[/c]",
            "variables": "{player} {class} {race} {zone} {subzone} {x} {y} {gold} {bags} (live). Level and XP are never shown.",
            "links": "{item:ID} {spell:ID} {quest:ID:Name}",
        },
        "rule_conditions": {k: sorted(v) for k, v in WHEN_TYPES.items()},
        "rule_actions": {k: sorted(v) for k, v in DO_TYPES.items()},
        "auto_complete": {k: sorted(v) for k, v in AUTO_TYPES.items()},
        "limits": {"rules": MAX_RULES, "panels": MAX_PANELS, "components_per_panel": MAX_CHILDREN, "items_per_checklist": MAX_ITEMS, "text_chars": MAX_TEXT, "document_bytes": MAX_WIRE_BYTES},
        "example": [
            {"op": "theme", "accent": "gold", "bgAlpha": 0.4},
            {"op": "panel", "id": "plan", "title": "Mentor plan", "anchor": "TOPRIGHT", "x": -24, "y": -280, "w": 300,
             "visibility": {"combat": "dim", "idleFade": 20},
             "children": [
                 {"type": "text", "text": "{class} in {zone}: next stop, the trainer", "muted": True},
                 {"type": "progress", "bind": "bags", "label": "Bags"},
                 {"type": "checklist", "items": [
                     {"id": "wolves", "text": "Turn in Wolves Across the Border", "priority": "high"},
                     {"id": "kobold", "text": "Kobold Camp Cleanup 3/10", "note": "north of the abbey"}]},
                 {"type": "callout", "level": "tip", "text": "Learn Corruption at level 4."}]},
            {"op": "waypoint", "id": "w1", "map": 1429, "x": 48.9, "y": 41.0, "title": "Eagan Peltskinner", "track": True},
            {"op": "toast", "text": "New plan ready", "level": "good", "sound": "ping"},
        ],
    }


def guide():
    return """WoW mentor overlay: how to control it
=====================================
You can draw an overlay in the player's game client. It is a set of PANELS (task lists, plans, progress
bars, callouts), WAYPOINTS (map pins) and TOASTS (short banners). Panels are click-through and dim in combat
by default, so keep them short and useful: a handful of next steps, not an essay.

Two ways to call it (same operations):
  wowui CLI:   wowui plan "Next steps" --task "Turn in X" --task "Kill 10 Y"     (wowui --help)
  HTTP:        POST <base>/ops with an op, a list of ops, or {"ops": [...]}      (GET <base>/schema for every field)

The player's live game state is in the text feed you already read. What the player did with your overlay
(checklist items they ticked, panels they moved or collapsed, what is currently on screen) is in the
"UI_STATE" section of that feed and in `wowui state`.

Workflow
  1. Read the feed to see level, zone, quests, gear, gold.
  2. Put the plan on screen: one panel per concern (e.g. "plan", "gear", "gold"), 3 to 7 checklist items each.
  3. Pin the next objective with a waypoint, and use a toast only for something that needs attention now.
  4. Re-read the feed. When an item is done (or the player ticked it), `wowui check` / remove it and add the next.
  5. Updates are batched and delivered when the game is in the foreground and not in combat or chat.

Style rules the player asked for: transparent, unobtrusive, flexible. Prefer small width (260-320), muted text for
explanations, a single accent colour, visibility {"combat":"dim"} (default), and idleFade for passive panels.
Use [c=#ff8800]text[/c] for emphasis and {zone} {subzone} {gold} variables for live values. Never show level or XP anywhere.

Instant reactions: rules run inside the game (`wowui rule preset bags-full`, `wowui rule add --json ...`, `wowui rule list`), checklist items can
complete themselves with `auto`, `wowui coach "text"` talks to the player and `wowui questions` shows what they asked with /coach. Events
(`wowui events --since N`) tell you what happened in order; `wowui session` tells you whether the player is in the game. Send ops with an opId
(the CLI does this for you) so a retry never applies twice, and --partial to apply the valid ones of a mixed batch.

What the overlay cannot do: cast spells, target, move, use items or accept quests. Blizzard blocks addons from
those actions; only display, waypoints, chat messages, sounds and quest tracking are available.

Full schema: GET <base>/schema (or `wowui schema`). Example ops: `wowui example`.
"""


def skill(base, feed, profile=""):
    """The copy-paste briefing for the mentor agent. base = https://host/ui/<secret>, feed = private game feed URL."""
    feed_root = feed.split("?")[0]
    text = f"""# WoW mentor: operating skill

You are the player's live World of Warcraft mentor (Forever client, Alliance/Horde, any class). You can SEE their game
through a text feed and you can DRAW on their screen. Goal: take this character from level 1 to 60 efficiently, then make
it a top PvP character: gold, best-in-slot armour, professions and skills. Be a coach, not a wall of text.

## 0. If you lost context (new chat, reset, compaction): run this first
    curl -s "{base}/brief?t=$RANDOM"        or        wowui brief
It prints your own saved summary and notes, the session state (is the player in the game?), the active rules, open coach questions, the last 10
events, what is on the player's screen, the guide-editing state and a trimmed game feed.
Keep that memory current as you work: `wowui memory add "decision or fact"` after each decision, `wowui memory summary "..."` when the plan changes
(`wowui memory show`, `remove <id>`, `clear`). Everything you put on screen and every guide edit also persists on the server, so nothing is lost
except your reasoning: write the reasoning into memory.

## 1. See the game (read-only text feed, refreshes about once a second)
Feed: {feed}
Add a fresh `?t=<random number>` to every fetch (the edge caches otherwise). Sections you will use:
- SUMMARY: a `KV` line (level, xp, money_copper, honorable_kills, class, race, realm, ui* sync fields) plus a human line. Use level and XP for your own logic; never put them on the overlay.
- SESSION: XP per hour, time to next level, gold per hour, honorable kills per hour (derived).
- FAST: position (map id, x/y percent, yards), zone, state (AFK/idle/combat/mounted), speed, XP, target, pet.
- VITALS: health/power measured from status bars (the game hides exact values; treat as approximate).
- STATS, EQUIPMENT (every slot, item level, quality, EMPTY SLOTS list), BAGS, BAG_VALUE (vendor value, junk).
- QUESTS (log with objectives), PROFESSIONS (all skill lines incl. weapon skills and proficiencies), SKILLS (spellbook),
  TALENTS and TALENTS_TRAITS, COOLDOWNS, AURAS, GROUP, NEARBY, PVP (honor, rank, kills, battleground state).
- OFFLINE SAVEDVARIABLES (bank, mail, quest journey from other addons; refreshed only on /reload or logout).
- UI_STATE: what your overlay currently shows and what the player did with it. RECENT EVENTS: chat, loot, casts, zone changes.
- SESSION_STATE (from the Mac helper): state = in_world | loading | afk | logged_out | game_closed, foreground, last_login_at, last_logout_at,
  uploader_alive_at. EVENTS_TAIL: the last 20 structured events (see 2b). Check SESSION_STATE before anything urgent.
- Newer addon versions add: ACTIONBARS (what is on each bar, `known_not_on_bar`, `lower_rank_on_bar`), PET, TARGET, FIGHTS (last 5 fights),
  DEATHS (last 5 deaths), ITEMS (bag items with bind type; BoE green+ marked KEEP FOR AH), REWARDS (quest reward choices while the window is open),
  TRAINER, NPCS, RULES (loaded rules and when they last fired), COACH_INBOX, COACH_QUESTIONS, SESSION_ADDON. A value the game hides is `unknown`
  (health, power and similar are secret in combat; "at pull" numbers are the last out-of-combat sample). Combat-log details are not available:
  fights are reconstructed, and the exact killer is a best guess. If a section is missing, the installed addon is older: do not assume it.
If `seconds_since_last_game_update` is large (or SESSION_STATE says logged_out / game_closed) the game is closed or hidden: do not send urgent toasts then.
Limits: no auction prices, no other players' gear, no exact in-combat numbers. Never invent data the feed does not show.

## 2. Draw on their screen (the overlay)
Endpoint: {base}
Install the CLI (Python 3, no dependencies):
    curl -s "{base}/cli?t=$RANDOM" -o wowui && chmod +x wowui && export WOWUI_URL="{base}"
    ./wowui guide        # short briefing      ./wowui schema   # every field      ./wowui state   # what is on screen
No shell? Use plain HTTP. Writes are GET requests because the public edge blocks POST bodies:
    GET {base}/ops?b=<base64url(JSON)>      where JSON is one op, a list of ops, or {{"ops":[...]}}  (about 5 KB per request)
    GET {base}/state?t=<n>   GET {base}/schema?t=<n>   GET {base}/doc?t=<n>
Python: import base64,json,urllib.request; b=base64.urlsafe_b64encode(json.dumps(ops).encode()).decode().rstrip("=");
        print(urllib.request.urlopen("{base}/ops?b="+b).read())
Replies are JSON: {{"ok":true,"rev":N,"applied_rev":N,"rejected":[]}} or {{"ok":false,"errors":[...]}} (fix the named field and resend; a batch is atomic).
Retries and big batches: send {{"ops":[...],"opId":"<unique per request>","partial":true}}. The same opId with the same ops is answered from memory and never applied twice,
so retry freely after a timeout (the `wowui` CLI adds a fresh opId to every call and reuses it on its own retries; `--op-id X` sets it, `--partial` turns partial on).
With partial the valid ops are applied and the bad ones come back as `rejected:[{{index, op, errors}}]`; without it one bad op rejects the whole batch.

### Model: one document, changed by ops
Ops: panel (upsert), child (upsert a component by id), check (tick an item), remove, theme, toast, waypoint, waypoint_clear,
action (flash | sound | chat | track_quest), builtin (me on/off), rule, rule_remove, rules_clear, coach, coach_answer, set (replace all), clear, reset.
Panel: id, title, subtitle, anchor (TOPLEFT..CENTER), x, y, w, maxH, scale, alpha, bg, bgAlpha, border, accent, fontSize, font,
padding, gap, collapsed, collapsible, draggable, interactive, header, ttl, pin, visibility {{combat: show|dim|hide, dim, mouseover,
idleFade, idleAlpha, hideWhenMounted, hideInInstance, minLevel, maxLevel}}, children[].
Components (type): heading, text, checklist (items: id, text, done, note, priority high|med|low), progress (bind bags|durability,
or value+max; label), kv (rows [[k,v]]), callout (level info|tip|good|warn|bad, title, text), timer (until epoch | seconds), icon
(spell|item|texture id, text), divider, spacer, row (up to 6 side-by-side simple components).
Text markup: [c=#ff8800]colour[/c]; live variables {{player}} {{class}} {{zone}} {{subzone}} {{x}} {{y}} {{gold}} {{bags}};
links {{item:ID}} {{spell:ID}} {{quest:ID:Name}}. Colours are #rrggbb or names (gold, red, green, cyan...). Theme tokens: accent, bg,
bgAlpha, border, text, muted, good, warn, bad, font, size, padding, gap, shadow, outline.
Waypoints: {{op:"waypoint", id, map (uiMapID), x, y (0-100), title, track, clear_yards (default 12), task ("panel/item")}}. A waypoint shows a small HUD with the
distance and a direction arrow and removes itself when the player is within clear_yards, or when the checklist item named by `task` is done. Link it to the step it points at. Use the `map=` and x/y from the feed's FAST section for
the player's own zone; common maps: Elwynn 1429, Dun Morogh 1426, Teldrassil 1438, Durotar 1411, Mulgore 1412, Tirisfal 1420,
Westfall 1436, Loch Modan 1432, Darkshore 1439, Barrens 1413.

The overlay also has a built-in "Character" panel (written by the addon itself, no agent needed): professions and secondary skills as bars, weapon and
armour skills, empty gear slots, durability, unspent talent points, XP, gold and bags. Do not duplicate it; reference it. `wowui builtin me off|on` hides or shows it.

### CLI quick reference
    wowui plan next --title "Next steps" --task "q1::!Turn in Wolves Across the Border::Eagan is north" --task "Kill 10 Kobold Vermin" \\
          --intro "{{class}} in {{zone}}" --progress bags --callout "Learn Corruption at level 4" --anchor TOPRIGHT --x -24 --y -280 --idle-fade 40
    wowui add-task next "Buy bandages" --priority low        wowui check next q1        wowui remove next --item q1
    wowui text gear "Head slot is empty" --muted             wowui callout gear "Replace the shirt-only look" --level warn
    wowui progress plan --bind bags --label Bags             wowui component plan '{{"type":"kv","rows":[["Zone","{{zone}}"]]}}'
    wowui waypoint w1 --map 1429 --x 48.9 --y 41 --title "Eagan" --track        wowui waypoint --clear
    wowui toast "New spells are waiting at the trainer" --level good --sound levelup        wowui say "Tip: sell grey items"
    wowui theme --accent gold --bg-alpha 0.35 --font arial --size 12            wowui clear | reset | state | doc | schema | demo
Task spec: 'Text', 'id::Text', 'id::Text::note'; prefix ! high priority, ~ medium.

## 2b. React instantly: rules, coach messages, events (this is what replaces polling)
Do not wait for your next poll to react to something the game already knows. Put RULES into the game: the addon evaluates them locally the moment a
condition becomes true and runs the actions, with no round trip. A rule is {{id, when:{{type,...}}, then:[{{do,...}}], cooldown_s (default 60), once, enabled}}.
Edge triggered: it fires when the condition goes false to true (event rules: on each matching event), then waits for its cooldown. `once` rules fire once per game session.
Conditions (when.type): bags_free_lte{{n}}, durability_lte{{pct}}, deaths_in_subzone{{count, minutes}}, buff_missing{{name|spell, pet, ooc}}, level_up, death, logout,
entering_world, merchant_show, trainer_show, quest_reward, loot_opened, lower_rank_on_bar, spell_learned{{name}}, quest_complete{{quest}}, zone_changed{{zone, subzone}},
target_elite{{level_diff, rare}}, item_looted{{min_quality}}, item_equipped; combine 2 to 4 with {{"type":"all"|"any","of":[...]}} (not nested).
Actions (then[].do, 1 to 4): toast{{text, level, sound, title}}, chat{{text, sound}}, check{{panel, item, done}}, waypoint{{...}}, panel_text{{panel, child, text}}, sound{{sound}}, flash.
Text placeholders filled from what fired: {{count}} {{subzone}} {{zone}} {{name}} {{free}} {{pct}} {{slot}} {{spell}} {{rank}} {{killer}} (unknown ones stay literal).
Rules never move or restyle panels. Max 40 rules. Every fire is recorded (feed section RULES) and sent as a `rule_fired` event.
    wowui rule preset bags-full|durability|deaths|buff|lower-rank [--set key=value ...]      (buff needs --set name='Power Word: Fortitude')
    wowui rule add --json '{{"id":"ding","when":{{"type":"quest_complete"}},"then":[{{"do":"toast","text":"Quest ready to turn in","level":"good"}}]}}'
    wowui rule list      wowui rule remove <id>      wowui rule clear        (HTTP twin: ops rule / rule_remove / rules_clear)
Presets: bags-full (n, default 2 free slots), durability (pct, default 20), deaths (count 2 in minutes 10 in one subzone), buff (name or spell, out of combat), lower-rank
(an old spell rank is still on the action bar). Change a preset's wording with --set text=... and its timing with --set cooldown_s=...
Checklist items can complete themselves: item.auto = {{type: quest_done{{quest}} | spell_known{{spell|name}} | skill_gte{{skill, value}} | item_in_bags{{item, count}} | zone{{zone}}}}.
The addon ticks them exactly like a player click (once), so they show up as `tick` lines and your `check` bookkeeping stays right. Prefer `auto` over polling for "did they do X yet".

Coach inbox. `wowui coach "text"` puts a message in the player's coach inbox panel (the newest 3 are kept), prints it in chat (--no-chat to skip), and shows a banner with
--toast (--level, --sound). The addon reports back with an `ack` event when it was really shown; no ack means the player has not seen it (game closed, loading or logged out).
The player can type `/coach <question>` in game: it arrives as a `coach_question` event, appears in `wowui questions` and in the feed section COACH_QUESTIONS, and the game
prints "Sent to your coach". Answer with `wowui coach "answer" --answer <question id>` (op coach_answer): it is a normal coach message that also closes the question.
Check `wowui questions` on every cycle; an unanswered question is the player waiting on you.

Events. The addon cannot use the network, so its events travel through the screen strip to the Mac helper, which stores them on the server in order.
    wowui events [--since N] [--types death,fight] [--limit 100] [--follow]       HTTP: GET {base}/events?since=<eid>&types=a,b&limit=100&t=<n>
    GET {feed_root}/events?since=<eid>      (read-only twin on the feed token, for agents without the overlay secret)
Reply: {{"events":[{{"eid","t","received_at","boot","type","data"}}], "next": <eid>, "last_eid", "oldest_eid", "gap"?, "reset"?}}. Pass `next` as `since` on the following call; `gap` means events
were dropped from the 500-event buffer before you read them (re-read the feed); `reset` means the server was reset and `next` restarts your cursor. Without `since` you get the newest events.
Types: login, logout, level_up, death, death_released, quest_complete, quest_turned_in, spell_learned, item_equipped, item_looted (BoE green+ marked boe_keep), zone_changed,
window_open (merchant | trainer | quest_reward | loot, with choices[]), bags_full, durability_critical, rule_fired, coach_question, ack, fight (casts, wasted casts,
duplicate DoTs, damage, kills), npc_seen, buff_missing, lower_rank_on_bar; from the Mac helper: game_started, game_closed, state_changed, foreground_changed.
`wowui npcs [--name x]` lists NPCs the player met with map/x/y (use it for waypoints to a trainer or vendor). `wowui session` = SESSION_STATE as JSON.
Sensible loop: `wowui session` (is the player there?) then `wowui events --since <cursor>`, react (explain a death, suggest a reward choice, praise a fight), then `wowui questions`.
Webhook (set by the player on their Mac with `wowmentor webhook set <url> [--secret S]`): the Mac helper also POSTs batched events to that URL: {{"v":1,"sent_at","session":{{state,foreground,
last_login_at,last_logout_at,uploader_alive_at}},"events":[...],"next":<eid>}}, at most one POST per 20 s (3 s after an urgent event: death, logout, game_closed, bags_full, durability_critical, coach_question),
with header X-WoWMentor-Signature: sha256=<HMAC-SHA256 of the raw body> when a secret is set. Use it to wake yourself; the events endpoint is the source of truth (use `next`/eid to de-duplicate).
Limits that are not bugs: combat-log events are restricted on this client, so fights and the killer are reconstructed best-effort; health and power are `unknown` in combat; the addon has no network access;
logout events may never arrive (the Mac helper then infers logged_out from silence after 20 to 90 seconds). The addon never equips or previews items and never plays for the player.
Level and XP: they are in the feed (SUMMARY KV) for your own reasoning, but never show the player's level or XP in the overlay (no bars, counters, "level N" text in panels, toasts, rules or
coach messages). Say what to do ("visit the trainer", "new quest area available") instead of showing the number.

## 3. How to mentor (rules the player asked for)
- Transparent, unobtrusive, flexible. Default look: width 260-320, one accent colour, muted explanations, visibility combat=dim,
  idleFade 30-60 s on passive panels. Never cover the middle of the screen; anchor to TOPRIGHT/RIGHT/LEFT edges.
- At most 3 panels at once: "plan" (3 to 7 next steps), optionally "gear/gold" and "pvp". Short lines. Put reasons in `note`.
- One waypoint at a time: the next objective. Toasts only for things that need attention right now (level up, danger, a missed trainer).
- Update on events, not on a timer: read `wowui events`, and move recurring reactions into rules (2b). Batch several ops in one request.
  Do not rewrite the same panel more often than every ~10 seconds. Each update costs the game a little work.
- Close the loop: read UI_STATE. Items the player ticked appear as `tick` lines and in the document; do not re-add finished tasks.
  If a panel was moved or collapsed by the player, respect it (do not resend x/y unless it is really wrong).
- Be honest about uncertainty (health is approximate, no auction data). Explain the why in one sentence, then the action.

## 4. Playbooks
1 to 10: finish the starter zone quests in order, learn each trainer spell at even levels, keep bags empty (sell grey items),
  pick gathering professions early (mining/herbalism/skinning) plus first aid; weapon skills at the weapon master.
10 to 40: quest zone by zone at +/-3 levels; do the class quests; at 20 visit the trainer for new ranks; save gold for the level 40
  mount and riding skill (read gold and gold/hour from SESSION); check EQUIPMENT every 5 levels for empty slots and quality upgrades.
40 to 60: elite/dungeon quests with a group, keep professions near the cap for the next tier, buy bags, learn the mount, then
  farm for the epic mount and consumables; start honor early if the realm is PvP (honorable kills, rank progress in PVP).
Gold: sell vendor junk (BAG_VALUE), gather what you can skin/mine/pick on the way, use the auction house for crafted items.
Armour: wear the best armour type for the class at the right level (cloth/leather/mail/plate switch), fill every EMPTY SLOT first,
  prefer quest rewards and dungeon drops, then crafted/auction items; check durability and repair.
Professions: one crafting + one gathering that feed each other; level by crafting what sells; read skill ranks from PROFESSIONS.
PvP at 60: rank up by honorable kills and battlegrounds, then gear from honor/reputation vendors.

## 5. Hard limits (be clear with the player)
You can display, set map pins and waypoints, print chat lines, play sounds, flash the dock icon and track quests. You cannot cast,
target, move, use items, accept quests or trade: Blizzard blocks that for addons. Do not ask the player to run automation or macros
that play the game for them; you advise, they act. Do not state anything as fact that is not in the feed.

## 6. Start here
1. `wowui brief` (session, rules, questions, events, memory), then the feed, then `wowui state` (is the overlay in sync?). 2. Put a first plan on screen with `wowui plan`.
3. Pin the next objective with `wowui waypoint`. 4. Install the standing rules the player needs (`wowui rule preset bags-full`, `durability`, `deaths`). 5. Loop on
`wowui events --since <cursor>` and `wowui questions`; re-read the feed after each meaningful change and update.
"""
    return text.replace("## 4. Playbooks", TUG_SECTION.replace("{BASE}", base) + "\n## 4. Playbooks", 1) + _profile_section(profile)


TUG_SECTION = """## 3b. Edit the leveling guide live (Guidelime)
The player follows a leveling guide in the Guidelime addon (for example a TUGs pack). You can read, search and edit every guide file. Edits are
validated, written to the player's files by their Mac with a backup, and the overlay then shows "Guide changed: type /reload"
until they reload (the game only reads guide files at /reload; you cannot avoid that, so batch your edits).
    wowui tug list [--filter x]            wowui tug show <guide> [--from N --to M]      wowui tug search "text" [--guide x]
    wowui tug route [--start guide]        wowui tug status                             wowui tug history <guide>
    wowui tug replace <guide> --match "unique text" [--expect "text"] "new step"      (\\n inside text makes several steps)
    wowui tug insert  <guide> --after-match "text" | --before-match "text" | --after-line N | --at start|end  "new step(s)"
    wowui tug delete  <guide> --match "text" [--count 2]
    wowui tug sub     <guide> "find" "replace" [--match "line text"] [--all]
    wowui tug set     <guide> -f body.txt   (replace everything; avoid)       wowui tug restore <guide> orig|<version>
    wowui tug ops -f edits.json    ({"edits":[{"guide":"elwynn","note":"why","ops":[{"op":"insert","after_match":"...","text":"..."}]}]})
HTTP twin: GET {BASE}/tug/ops?b=<base64url JSON>, GET {BASE}/tug/show?name=..&t=<n>, /tug/list, /tug/search?q=.., /tug/status
(guide names are file paths such as "Alliance/6-9 Elwynn Forest"; a unique part like "elwynn" is enough).
How to edit safely:
- Always `tug show` or `tug search` first. Address a step by --match (text that appears on exactly one step) plus --expect; line numbers
  shift after every edit. The editor refuses ambiguous matches, wrong expectations, unbalanced brackets and ]] / [[ and tells you why.
- Surgical edits (sub, replace, insert, delete) over `set`. One `tug ops` request per theme so the player gets one reload prompt.
- Keep the pack's quest order, travel path and arrow targets unless a step contradicts the player's rules (below). Edits are atomic per request.
- `tug status` shows queued/applied/failed edits. Every edit is backed up; `tug restore <guide> orig` returns the guide to how WoW Mentor first found it,
  and any version id from `tug history` restores that point. Tell the player what you changed in one sentence; they will see the reload prompt on their screen.
Guide syntax: one step per line. Header lines first: [GA Alliance] [N<this guide>] [NX<next guide>] [D description].
    Accept: [QA783]   TurnIn: [QT783]   Do: [QC783]   Do [QC33- ][QC7] (several objectives)   [QC91733,2] (objective number 2)
    [A Warlock,Mage] limits a step to classes or races, [O] marks optional, (Elite/Hard Optional Quest) is plain text
    [G47.6,41.6 Elwynn Forest] coordinate arrow   [TAR2329] target NPC id   Vendor at [TAR152][G47.6,41.6 Elwynn Forest]
    Buy: [CI2512,600 Rough Arrow]at [TAR152]   [SK First Aid 20] skill target   Use: [SP465]   [H] hearthstone   [S] set hearth
    [P] get flight path   [F Ironforge] fly   [T] train   Learn your Level 6 skills.[A Warlock]   Plain text lines are notes the player ticks.
  Copy the pattern of neighbouring steps (`tug show`). Do not put steps before the player's current step unless needed: the player's
  progress is stored by step position, so inserting earlier lines can shift their checkmarks. Prefer editing in place or inserting ahead.
"""

def _profile_section(profile):
    text = (profile or "").strip()
    if not text:
        text = ("(The player has not written a profile yet. Ask them for their goals, playstyle, professions and gear preferences, "
                "and suggest they save the answers in their profile file: `wowmentor profile`.)")
    return "\n## 7. Player profile and guide policy (from the player; follow it unless they change it)\n" + text + "\n"
