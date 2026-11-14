"""Keep the Quests section of the "What to get here" squares panel in step with the player's quest log and zone, with no agent round trip.

The quest set starts from the feed's QUESTS section and is corrected at once by quest_accepted / quest_turned_in / quest_removed events
(the feed section can lag minutes behind them); the map comes from FAST and zone_changed. When either changes, the Quests rows are rebuilt
from the offline database: every creature in this map that is a kill objective or drops an objective item (best drop chance first, from
Tnny's Mob Loot when the bridge uploaded it), the objects that hold objective items, and a target-all square. Spawns inside dungeons are
left out. Rows an agent added under other headings (Cooking, First Aid, ...) are kept as they are. The game itself hides a square the
moment its quest is complete, so this only has to add and drop rows. Pure stdlib.
"""
import json, re, threading, time

QUEST_ROW = re.compile(r"^q\d+$")
OURS = ("hq", "qall")
FAST_MAP = re.compile(r"Position: map=(\d+)")
QUEST_LINE = re.compile(r"\[(COMPLETE|in progress|failed)\] (.+?) \(L\d+\) #(\d+)")
STICKY_S = 600              # an accept or removal overrides a QUESTS section older than this many seconds
MAX_ROWS = 6
TEXTURES = (("Moonstalker", "Ability_Hunter_Pet_Cat"), ("Crawler", "Ability_Hunter_Pet_Crab"), ("Crab", "Ability_Hunter_Pet_Crab"),
            ("Bear", "Ability_Hunter_Pet_Bear"), ("Wolf", "Ability_Hunter_Pet_Wolf"), ("Boar", "Ability_Hunter_Pet_Boar"),
            ("Spider", "Ability_Hunter_Pet_Spider"), ("Murloc", "INV_Misc_Head_Murloc_01"), ("Murk", "INV_Misc_Head_Murloc_01"),
            ("Strider", "Ability_Hunter_Pet_TallStrider"), ("Raptor", "Ability_Hunter_Pet_Raptor"), ("Bat", "Ability_Hunter_Pet_Bat"),
            ("Owl", "Ability_Hunter_Pet_Owl"), ("Gorilla", "Ability_Hunter_Pet_Gorilla"), ("Scorpid", "Ability_Hunter_Pet_Scorpid"),
            ("Kobold", "INV_Misc_Head_Kobold_01"), ("Gnoll", "INV_Misc_Head_Gnoll_01"), ("Trogg", "INV_Misc_Head_Troll_01"),
            ("Furbolg", "Ability_Druid_Mangle"), ("Fanatic", "Spell_Shadow_ShadowWordPain"), ("Cultist", "Spell_Shadow_ShadowWordPain"),
            ("Elemental", "Spell_Nature_EarthElemental_Totem"), ("Defias", "INV_Misc_Bandana_01"), ("Satyr", "Spell_Shadow_SummonFelHunter"))
TARGETABLE = re.compile(r"^[A-Za-z0-9' ,.:\-]{1,60}$")


def texture(name):
    for key, tex in TEXTURES:
        if key.lower() in (name or "").lower():
            return tex
    return "Ability_Hunter_SniperShot"


def feed_state(body):
    """(uiMapId or None, {questId: status} or None when the QUESTS section is missing) from a feed body."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    m = FAST_MAP.search(text)
    i = text.find("--- QUESTS")
    quests = None
    if i >= 0:
        j = text.find("\n--- ", i + 5)
        quests = {int(q): st for st, _t, q in QUEST_LINE.findall(text[i:j if j > 0 else len(text)])}
    return (int(m.group(1)) if m else None), quests


class AutoSquares:
    def __init__(self, gdb, lock, get_doc, save, apply_ops, delay=1.0):
        self.gdb, self.lock, self.get_doc, self.save, self.apply_ops = gdb, lock, get_doc, save, apply_ops
        self.delay = delay
        self.map = None
        self.feed_quests = None         # in-progress ids from the last QUESTS section
        self.added, self.removed = {}, {}   # quest id -> time of the event, for events newer than the section
        self.sig = None
        self.timer = None
        self.mu = threading.Lock()
        self.last = {"t": 0, "rows": 0, "error": None}

    # ------------------------------------------------------------------ inputs
    def on_feed(self, body):
        m, quests = feed_state(body)
        with self.mu:
            if m:
                self.map = m
            if quests is not None:
                self.feed_quests = {q for q, st in quests.items() if st == "in progress"}
        self.schedule()

    def on_events(self, events):
        hit = False
        now = time.time()
        with self.mu:
            for e in events if isinstance(events, list) else []:
                if not isinstance(e, dict):
                    continue
                t, d = e.get("type"), e.get("data") if isinstance(e.get("data"), dict) else {}
                q = d.get("quest")
                if t == "quest_accepted" and isinstance(q, int):
                    self.added[q] = now; self.removed.pop(q, None); hit = True
                elif t in ("quest_turned_in", "quest_removed") and isinstance(q, int):
                    self.removed[q] = now; self.added.pop(q, None); hit = True
                elif t == "zone_changed" and isinstance(d.get("map"), int) and d["map"] != self.map:
                    self.map = d["map"]; hit = True
        if hit:
            self.schedule()

    def quests(self):
        now = time.time()
        with self.mu:
            base = set(self.feed_quests or ())
            for q, t in list(self.added.items()):
                if now - t < STICKY_S:
                    base.add(q)
                else:
                    self.added.pop(q)
            for q, t in list(self.removed.items()):
                if now - t < STICKY_S:
                    base.discard(q)
                else:
                    self.removed.pop(q)
            return self.map, sorted(base)

    def schedule(self):
        with self.mu:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self.run)
            self.timer.daemon = True
            self.timer.start()

    # ------------------------------------------------------------------ build
    def build(self, m, quests):
        """The Quests heading, the target-all square and the rows, from the database (no doc access)."""
        g = self.gdb
        with g.lock:
            c = g.connect()
            try:
                instances = set()
                if g._has(c, "boss_loot"):
                    instances = {r[0].lower() for r in c.execute("SELECT DISTINCT instance_name FROM boss_loot") if r[0]}

                def spawns_here(i):
                    n = 0
                    for r in c.execute("SELECT area_id FROM spawn WHERE kind='npc' AND id=? AND uimap_id=?", (i, m)):
                        name = (g._zone(c, r[0]).get("name") or "").lower()
                        if name not in instances:
                            n += 1
                    return n

                cells, names = [], []
                for qid in quests:
                    row = c.execute("SELECT name, json FROM quest WHERE id=?", (qid,)).fetchone()
                    if not row:
                        continue
                    o = g._lua(json.loads(row["json"]).get("objectives"))
                    for rr in (o.get("1") or o.get(1) or []):
                        nid = rr[0]
                        n = spawns_here(nid)
                        nm = g._name(c, "npc", nid)
                        if n and nm and TARGETABLE.match(nm):
                            cells.append([{"type": "icon", "npc": nid, "tip": nm, "texture": texture(nm), "iconSize": 26, "quest": qid}])
                            names.append((n, 0, nm))
                    for it in (o.get("3") or o.get(3) or []):
                        iid = it[0]
                        r = c.execute("SELECT name, json FROM item WHERE id=?", (iid,)).fetchone()
                        if not r:
                            continue
                        j = json.loads(r["json"])
                        rates = {d["npc"]: d for d in g._rates(c, item=iid)}
                        drops = []
                        for nid in dict.fromkeys((j.get("npcDrops") or []) + list(rates)):
                            n = spawns_here(nid)
                            nm = g._name(c, "npc", nid)
                            if n and nm and TARGETABLE.match(nm):
                                drops.append((rates.get(nid, {}).get("pct") or 0, n, nid, nm))
                        drops.sort(key=lambda d: (-d[0], -d[1]))
                        for pct, n, nid, nm in drops[:3]:
                            tip = "%s (%s from %s)" % (r["name"], ("%g%%" % round(pct, 1)) if pct else "drops", nm)
                            cells.append([{"type": "icon", "npc": nid, "tip": nm, "texture": texture(nm), "iconSize": 26, "quest": qid},
                                          {"type": "icon", "item": iid, "tip": tip, "iconSize": 26, "quest": qid}])
                            names.append((n, pct, nm))
                        if not drops:
                            objs = [g._name(c, "object", oid) for oid in (j.get("objectDrops") or [])
                                    if c.execute("SELECT 1 FROM spawn WHERE kind='object' AND id=? AND uimap_id=? LIMIT 1", (oid, m)).fetchone()]
                            if objs:
                                cells.append([{"type": "icon", "item": iid, "tip": "%s (from %s)" % (r["name"], objs[0]), "iconSize": 26, "quest": qid}])
            finally:
                c.close()
        rows, cur = [], []
        for cl in cells:
            if len(cur) + len(cl) > 4:
                rows.append(cur); cur = []
            cur += cl
        if cur:
            rows.append(cur)
        dropped = max(0, len(rows) - MAX_ROWS)
        rows = rows[:MAX_ROWS]
        out = [{"type": "heading", "id": "hq", "text": "Quests: kill, then loot" if rows else "Quests: nothing to kill or loot here"}]
        order = [nm for _n, _p, nm in sorted(names, key=lambda x: (-x[0], x[1]))]          # common first, the rare ones last (the last found wins)
        order = list(dict.fromkeys(order))[-8:]
        if order:
            out.append({"type": "icon", "id": "qall", "targets": order, "tip": "Target the nearest quest enemy", "texture": "Ability_Hunter_SniperShot",
                        "iconSize": 26, "text": "Target any"})
        for k, r in enumerate(rows):
            out.append({"type": "row", "id": "q%d" % (k + 1), "gap": 4, "children": r + [{"type": "spacer"}] * (4 - len(r))})
        return out, dropped

    def run(self):
        try:
            m, quests = self.quests()
            if not m:
                return
            sig = (m, tuple(quests))
            if sig == self.sig:
                return
            with self.lock:
                doc = self.get_doc()
                panel = next((p for p in doc.panels if p.get("id") == "squares"), None)
                if panel is None:
                    return                              # the agent has not made the squares panel: nothing to keep in step
            ours, dropped = self.build(m, quests)
            with self.lock:
                doc = self.get_doc()
                panel = next((p for p in doc.panels if p.get("id") == "squares"), None)
                if panel is None:
                    return
                rest = [ch for ch in panel.get("children", []) if not (ch.get("id") in OURS or QUEST_ROW.match(str(ch.get("id") or "")))]
                self.apply_ops(doc, [{"op": "panel", "id": "squares", "children": ours + rest}])
                self.save()
            self.sig = sig
            self.last = {"t": time.time(), "map": m, "quests": quests, "rows": len(ours), "rows_left_out": dropped, "error": None}
        except Exception as e:                              # never break the request path; the next change retries
            self.last = {"t": time.time(), "error": "%s: %s" % (type(e).__name__, e)}
