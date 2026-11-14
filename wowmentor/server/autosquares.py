"""Keep the Quests section of the "What to get here" squares panel in step with the player's quest log and zone, with no agent round trip.

Inputs: the feed (POST /push), whose QUESTS and FAST sections say which quests are in progress and which map he is on, each with its age
("updated Ns ago"), and the events (quest_accepted, quest_turned_in, quest_removed, quest_complete, zone_changed), each with the time it
happened in the game. For every quest and for the map the newer of the two wins, so a lagging feed section cannot undo an event and a
replayed old event cannot undo a newer section.

When the result changes (or the panel's Quests rows no longer match what was built, say after an agent rewrote the panel), one worker
thread rebuilds the rows from the offline database: every creature in this map that is a kill objective (kill-credit objectives included)
or drops an objective item, best drop chance first (Tnny's Mob Loot rates when the bridge uploaded them), objects that hold objective items,
and a target-all square. Dungeon and raid quests are skipped. Rows an agent added under other headings are kept (invalid ones are dropped and
reported in `last`). The game hides a finished quest's squares itself, the moment the quest completes. Pure stdlib.
"""
import json, re, threading, time

QUEST_ROW = re.compile(r"^q\d+$")
OURS = ("hq", "qall")
SECTION = re.compile(r"^--- ([A-Z_]+)(?: \((?:updated (\d+)s ago|[^)]*)\))?(?: ---)?\s*$", re.M)
FAST_MAP = re.compile(r"Position: map=(\d+)")
QUEST_LINE = re.compile(r"\[(COMPLETE|in progress|failed)\] (.+?) \(L\d+\) #(\d+)")
MAX_ROWS = 6
SLACK_S = 5.0          # game event times are whole seconds and section ages are approximate: within this window the event wins
MAX_QUEST_ID = 10_000_000
TARGETABLE = re.compile(r"^[\w' ,.:\-]{1,60}$")          # what the server accepts as a /targetexact name (unicode letters included)
# Classic instance areas (QuestieDB/AreaTable ids): their quests' mobs are inside, even when Questie files spawns under the outdoor zone
INSTANCE_AREAS = {2437, 718, 1581, 209, 719, 717, 721, 491, 796, 722, 1337, 2100, 1176, 1477, 1584, 1583, 2017, 2057, 2557, 2717, 2677,
                  2159, 1977, 3429, 3428, 3456}
TEXTURES = (("moonstalker", "Ability_Hunter_Pet_Cat"), ("crawler", "Ability_Hunter_Pet_Crab"), ("crab", "Ability_Hunter_Pet_Crab"),
            ("bear", "Ability_Hunter_Pet_Bear"), ("wolf", "Ability_Hunter_Pet_Wolf"), ("boar", "Ability_Hunter_Pet_Boar"),
            ("spider", "Ability_Hunter_Pet_Spider"), ("murloc", "INV_Misc_Head_Murloc_01"), ("murkdeep", "INV_Misc_Head_Murloc_01"),
            ("strider", "Ability_Hunter_Pet_TallStrider"), ("foreststrider", "Ability_Hunter_Pet_TallStrider"), ("raptor", "Ability_Hunter_Pet_Raptor"),
            ("bat", "Ability_Hunter_Pet_Bat"), ("owl", "Ability_Hunter_Pet_Owl"), ("gorilla", "Ability_Hunter_Pet_Gorilla"),
            ("scorpid", "Ability_Hunter_Pet_Scorpid"), ("kobold", "INV_Misc_Head_Kobold_01"), ("gnoll", "INV_Misc_Head_Gnoll_01"),
            ("trogg", "INV_Misc_Head_Troll_01"), ("furbolg", "Ability_Druid_Mangle"), ("fanatic", "Spell_Shadow_ShadowWordPain"),
            ("cultist", "Spell_Shadow_ShadowWordPain"), ("elemental", "Spell_Nature_EarthElemental_Totem"), ("defias", "INV_Misc_Bandana_01"),
            ("satyr", "Spell_Shadow_SummonFelHunter"))


def texture(name):
    words = set(re.findall(r"[a-z]+", (name or "").lower()))
    for key, tex in TEXTURES:
        if key in words:
            return tex
    return "Ability_Hunter_SniperShot"


def _qid(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= MAX_QUEST_ID else None


def sections(text):
    """{NAME: (body, age seconds or None)}"""
    out, marks = {}, list(SECTION.finditer(text))
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        out[m.group(1)] = (text[m.end():end], int(m.group(2)) if m.group(2) else None)
    return out


def feed_state(body, now=None):
    """(map, map_time, {quest: status} or None, quests_time) from a feed body; times are server epoch seconds (None when unknown)."""
    now = time.time() if now is None else now
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    sec = sections(text)
    m = mt = q = qt = None
    if "FAST" in sec:
        mm = FAST_MAP.search(sec["FAST"][0])
        if mm:
            m, mt = int(mm.group(1)), now - (sec["FAST"][1] or 0)
    if "QUESTS" in sec:
        q = {int(i): st for st, _t, i in QUEST_LINE.findall(sec["QUESTS"][0]) if _qid(int(i))}
        qt = now - (sec["QUESTS"][1] or 0)
    return m, mt, q, qt


class AutoSquares:
    def __init__(self, gdb, lock, get_doc, save, apply_ops, delay=0.6, normalize=None, worker=True):
        self.gdb, self.lock, self.get_doc, self.save, self.apply_ops = gdb, lock, get_doc, save, apply_ops
        self.normalize = normalize
        self.delay = delay
        self.mu = threading.Lock()
        self.map, self.map_t = None, -1.0
        self.feed_q, self.feed_t = set(), None        # in-progress quests from the newest QUESTS section, and when it was true
        self.ev = {}                                  # quest -> (time, in progress?) from events
        self.sig = None
        self.ours = None
        self.wake = threading.Event()
        self.last = {"t": 0, "rows": 0, "error": None}
        if worker:
            self.worker = threading.Thread(target=self._loop, name="autosquares", daemon=True)
            self.worker.start()

    # ------------------------------------------------------------------ inputs (request threads: cheap, never raise)
    def on_feed(self, body):
        try:
            m, mt, q, qt = feed_state(body)
        except Exception:
            return
        with self.mu:
            if m and mt >= self.map_t:
                self.map, self.map_t = m, mt
            if q is not None and (self.feed_t is None or qt >= self.feed_t):
                self.feed_q, self.feed_t = {i for i, st in q.items() if st == "in progress"}, qt
                self.ev = {i: v for i, v in self.ev.items() if v[0] > qt - SLACK_S}   # the section is clearly newer: those events are in it now
        self.wake.set()

    def on_events(self, events):
        now = time.time()
        with self.mu:
            for e in events if isinstance(events, list) else []:
                if not isinstance(e, dict):
                    continue
                t, d = e.get("type"), e.get("data") if isinstance(e.get("data"), dict) else {}
                at = e.get("t") if isinstance(e.get("t"), (int, float)) and not isinstance(e.get("t"), bool) else now
                q = _qid(d.get("quest"))
                if t == "quest_accepted" and q:
                    self._event(q, at, True)
                elif t in ("quest_turned_in", "quest_removed", "quest_complete") and q:
                    self._event(q, at, False)
                elif t == "zone_changed" and _qid(d.get("map")) and at >= self.map_t - SLACK_S:
                    self.map, self.map_t = d["map"], at
        self.wake.set()

    def _event(self, q, at, present):
        if self.feed_t is not None and at <= self.feed_t - SLACK_S:
            return                                     # clearly older than the QUESTS section we have: it already reflects this
        old = self.ev.get(q)
        if not old or at >= old[0]:
            self.ev[q] = (at, present)

    def quests(self):
        with self.mu:
            cur = set(self.feed_q)
            for q, (_t, present) in self.ev.items():
                (cur.add if present else cur.discard)(q)
            return self.map, sorted(cur)

    def schedule(self):
        self.wake.set()

    # ------------------------------------------------------------------ worker
    def _loop(self):
        while True:
            self.wake.wait()
            time.sleep(self.delay)                    # let a burst of events and feed parts settle
            self.wake.clear()
            self.run()

    def build(self, m, quests):
        """The Quests heading, the target-all square and the rows, from the database (no doc access)."""
        g = self.gdb
        skipped = []
        with g.lock:
            c = g.connect()
            try:
                inst_names = set()
                if g._has(c, "boss_loot"):
                    inst_names = {r[0].lower() for r in c.execute("SELECT DISTINCT instance_name FROM boss_loot") if r[0]}

                def is_instance(area):
                    if not area:
                        return False
                    if area in INSTANCE_AREAS:
                        return True
                    z = c.execute("SELECT name, parent FROM zone WHERE area_id=?", (area,)).fetchone()
                    return bool(z and ((z[0] or "").lower() in inst_names or (z[1] and z[1] in INSTANCE_AREAS)))

                def spawns_here(i):
                    return sum(1 for r in c.execute("SELECT area_id FROM spawn WHERE kind='npc' AND id=? AND uimap_id=?", (i, m)) if not is_instance(r[0]))

                cells, names, seen_kill = [], [], set()
                for qid in quests:
                    try:
                        row = c.execute("SELECT name, json, zone_or_sort FROM quest WHERE id=?", (qid,)).fetchone()
                        if not row or is_instance(row["zone_or_sort"]):
                            continue
                        o = g._lua(json.loads(row["json"]).get("objectives"))
                        kills = [rr[0] for rr in (o.get("1") or o.get(1) or []) if isinstance(rr, list) and rr]
                        for kc in (o.get("5") or o.get(5) or []):          # kill credit: {{npc ids}, base npc, text}
                            if isinstance(kc, list) and kc:
                                kills += [x for x in (kc[0] if isinstance(kc[0], list) else [kc[0]]) if isinstance(x, int)]
                        for nid in kills:
                            if not isinstance(nid, int) or nid in seen_kill:
                                continue
                            n = spawns_here(nid)
                            nm = g._name(c, "npc", nid)
                            if n and nm and TARGETABLE.match(nm):
                                seen_kill.add(nid)
                                cells.append([{"type": "icon", "npc": nid, "tip": nm, "texture": texture(nm), "iconSize": 26, "quest": qid}])
                                names.append((n, 0, nm))
                        for it in (o.get("3") or o.get(3) or []):
                            if not (isinstance(it, list) and it and isinstance(it[0], int)):
                                continue
                            iid = it[0]
                            r = c.execute("SELECT name, json FROM item WHERE id=?", (iid,)).fetchone()
                            if not r:
                                continue
                            j = json.loads(r["json"])
                            npc_drops = j.get("npcDrops") if isinstance(j.get("npcDrops"), list) else []
                            rates = {d["npc"]: d for d in g._rates(c, item=iid)}
                            drops = []
                            for nid in dict.fromkeys([x for x in npc_drops if isinstance(x, int)] + list(rates)):
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
                                obj_drops = j.get("objectDrops") if isinstance(j.get("objectDrops"), list) else []
                                objs = [g._name(c, "object", oid) for oid in obj_drops if isinstance(oid, int)
                                        and c.execute("SELECT 1 FROM spawn WHERE kind='object' AND id=? AND uimap_id=? LIMIT 1", (oid, m)).fetchone()]
                                if objs:
                                    cells.append([{"type": "icon", "item": iid, "tip": "%s (from %s)" % (r["name"], objs[0]), "iconSize": 26, "quest": qid}])
                    except Exception as e:                       # one malformed quest never blocks the others
                        skipped.append("%s: %s" % (qid, type(e).__name__))
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
        shown = {k["tip"] for r in rows for k in r if k.get("npc")}
        out = [{"type": "heading", "id": "hq", "text": "Quests: kill, then loot" if rows else "Quests: nothing to kill or loot here"}]
        order = [nm for _n, _p, nm in sorted(names, key=lambda x: (-x[0], x[1])) if nm in shown]   # common first, the rare ones last (last found wins)
        order = list(dict.fromkeys(order))[-8:]
        if order:
            out.append({"type": "icon", "id": "qall", "targets": order, "tip": "Target the nearest quest enemy", "texture": "Ability_Hunter_SniperShot",
                        "iconSize": 26, "text": "Target any"})
        for k, r in enumerate(rows):
            out.append({"type": "row", "id": "q%d" % (k + 1), "gap": 4, "children": r + [{"type": "spacer"}] * (4 - len(r))})
        return out, dropped, skipped

    def _current_ours(self, panel):
        return [ch for ch in panel.get("children", []) if ch.get("id") in OURS or QUEST_ROW.match(str(ch.get("id") or ""))]

    def run(self):
        try:
            m, quests = self.quests()
            if not m:
                return
            sig = (m, tuple(quests))
            with self.lock:
                panel = next((p for p in self.get_doc().panels if p.get("id") == "squares"), None)
                if panel is None:
                    self.sig = None
                    return                              # the agent has not made the squares panel: nothing to keep in step
                if sig == self.sig and self.ours is not None and json.dumps(self._current_ours(panel), sort_keys=True) == self.ours:
                    return                              # nothing changed and nobody rewrote our rows
            ours, dropped, skipped = self.build(m, quests)
            invalid = []
            with self.lock:
                doc = self.get_doc()
                panel = next((p for p in doc.panels if p.get("id") == "squares"), None)
                if panel is None:
                    return
                rest = [ch for ch in panel.get("children", []) if not (ch.get("id") in OURS or QUEST_ROW.match(str(ch.get("id") or "")))]
                try:
                    self.apply_ops(doc, [{"op": "panel", "id": "squares", "children": ours + rest}])
                except Exception:
                    if not self.normalize:
                        raise
                    keep = []                           # an invalid agent-made child must not block the rebuild: drop it and say so
                    for k, ch in enumerate(rest):
                        errs = []
                        try:
                            self.normalize(ch, errs, "c%d" % k)
                        except Exception as e:
                            errs.append(str(e))
                        (invalid.append(str(ch.get("id") or k)) if errs else keep.append(ch))
                    self.apply_ops(doc, [{"op": "panel", "id": "squares", "children": ours + keep}])
                self.save()
                panel = next((p for p in doc.panels if p.get("id") == "squares"), None)
                self.ours = json.dumps(self._current_ours(panel), sort_keys=True) if panel else None
            self.sig = sig
            self.last = {"t": time.time(), "map": m, "quests": quests, "rows": len(ours), "rows_left_out": dropped,
                         "quests_skipped": skipped, "dropped_invalid_children": invalid, "error": None}
        except Exception as e:                              # never break the worker; the next input retries
            self.last = {"t": time.time(), "error": "%s: %s" % (type(e).__name__, e)}
