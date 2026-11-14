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

QUEST_ROW = re.compile(r"^(q\d+|hp\d+|p\d+_\d+|rr\d+)$")  # quest rows, profession headings and rows, rare rows (all generated here)
OURS = ("hq", "qall", "hr")
PROF_ROW = re.compile(r"^(hp\d+|p\d+_\d+)$")
LEGACY = {"hc", "c1", "c2", "hf", "f1", "he", "e1", "ha", "a1"}  # hand-made profession rows from before: replaced by generated ones
CRAFTING = ("Engineering", "Alchemy", "Blacksmithing", "Leatherworking", "Tailoring", "Enchanting", "Cooking", "First Aid")
RANKS = ((75, 0, 0, "Apprentice"), (150, 50, 10, "Journeyman"), (225, 125, 20, "Expert"), (300, 200, 35, "Artisan"))   # cap, skill, level to train
TRAINABLE = ("Trainer", "Starting recipe", "Trainer · Recipe")
CAPITALS = {1453: "Stormwind City", 1455: "Ironforge", 1457: "Darnassus"}     # Alliance capitals (uiMap ids)
SKILL_LINE = re.compile(r"^\s+([A-Za-z][A-Za-z ]+): (\d+)/(\d+)\s*$", re.M)
LEVEL_KV = re.compile(r"\blevel=(\d+)")
SECTION = re.compile(r"^--- ([A-Z_]+)(?: \((?:updated (\d+)s ago|[^)]*)\))?(?: ---)?\s*$", re.M)
FAST_MAP = re.compile(r"Position: map=(\d+)")
QUEST_LINE = re.compile(r"\[(COMPLETE|in progress|failed)\] (.+?) \(L\d+\) #(\d+)")
AH_LINE = re.compile(r"^(\d+) ah=(\d+) ", re.M)                                  # CoachData AH_PRICES (Auctionator)
HAVE_LINE = re.compile(r"^(\d+) bank=(\d+) mail=(\d+) bags=(\d+)\s*$", re.M)        # CoachData BANK (BagBrother)
RARE_SEEN_S = 900      # a rare seen alive this recently on a nameplate goes first in the Rares row
MAX_ROWS = 6
SLACK_S = 5.0          # game event times are whole seconds and section ages are approximate: within this window the event wins
# A section's time is when it reached the server, not when the game built it: a big section can wait this long in the send queue.
# So an event the section does not agree with is kept until the section is this much newer (by then it was built after the event).
TRANSIT_S = 180.0
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


def _newer(at, seq, old):
    """Is the event (at, seq) newer than the remembered one? Without sequence numbers a tie goes to the one that arrived last."""
    if at != old[0]:
        return at > old[0]
    return seq > old[2] if seq and old[2] else True


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


def feed_skills(body):
    """({profession: (skill, max)} or None, level or None) from PROFESSIONS and SUMMARY."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    sec = sections(text)
    skills = None
    if "PROFESSIONS" in sec:
        skills = {n.strip(): (int(a), int(b)) for n, a, b in SKILL_LINE.findall(sec["PROFESSIONS"][0]) if n.strip() in CRAFTING}
    lv = LEVEL_KV.search(sec["SUMMARY"][0]) if "SUMMARY" in sec else None
    return skills, (int(lv.group(1)) if lv else None)


def feed_market(body):
    """({item: AH copper} or None, {item: (bags, bank incl. mail)} or None) from the AH_PRICES and BANK sections."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    sec = sections(text)
    prices = {int(i): int(p) for i, p in AH_LINE.findall(sec["AH_PRICES"][0])} if "AH_PRICES" in sec else None
    have = {int(i): (int(bg), int(bk) + int(ml)) for i, bk, ml, bg in HAVE_LINE.findall(sec["BANK"][0])} if "BANK" in sec else None
    return prices, have


def money(c):
    c = int(c)
    return " ".join(s for s in ("%dg" % (c // 10000) if c >= 10000 else "", "%ds" % (c // 100 % 100) if c >= 100 else "", "%dc" % (c % 100) if c % 100 or c < 100 else "") if s)


def skill_chance(cur, yellow, grey):
    """Chance one craft gives a point (Classic rule, the one Forever guides use): orange 1, yellow/green falls linearly to 0 at grey."""
    if cur >= grey:
        return 0.0
    if cur < yellow:
        return 1.0
    return max(0.0, min(1.0, (grey - cur) / float(max(1, grey - yellow))))


class AutoSquares:
    def __init__(self, gdb, lock, get_doc, save, apply_ops, delay=0.6, normalize=None, worker=True, state_path=None):
        self.gdb, self.lock, self.get_doc, self.save, self.apply_ops = gdb, lock, get_doc, save, apply_ops
        self.normalize = normalize
        self.delay = delay
        self.mu = threading.Lock()
        self.map, self.map_t = None, -1.0
        self.feed_q, self.feed_t = set(), None        # in-progress quests from the newest QUESTS section, and when it was true
        self.ev = {}                                  # quest -> (time, in progress?, seq) from events; (time, seq) orders them
        self.skills, self.level = {}, None            # {profession: (skill, max)}, character level
        self.prices, self.have = {}, {}               # {item: AH copper} (Auctionator), {item: (in bags, in bank + mail)} (BagBrother)
        self.rare_seen = {}                           # npc -> (time, map) of the last rare_seen event
        self.state_path = state_path                  # what it knew survives a server restart (the feed's sections take minutes to come back)
        self._load()
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
            sk, lv = feed_skills(body)
            prices, have = feed_market(body)
        except Exception:
            return
        with self.mu:
            if prices is not None:                    # merge: an item that leaves the overlay keeps its last price (no recipe flip-flop)
                self.prices = {**self.prices, **prices}
            if have is not None:
                self.have = have
            if m and mt >= self.map_t:
                self.map, self.map_t = m, mt
            if sk:
                self.skills = sk
            if lv:
                self.level = lv
            if q is not None and (self.feed_t is None or qt >= self.feed_t):
                self.feed_q, self.feed_t = {i for i, st in q.items() if st == "in progress"}, qt
                self.ev = {i: v for i, v in self.ev.items() if not self._reflected(i, v[0], v[1])}
        self.wake.set()

    def on_events(self, events):
        now = time.time()
        with self.mu:
            for e in events if isinstance(events, list) else []:
                if not isinstance(e, dict):
                    continue
                t, d = e.get("type"), e.get("data") if isinstance(e.get("data"), dict) else {}
                at = e.get("t") if isinstance(e.get("t"), (int, float)) and not isinstance(e.get("t"), bool) else now
                sq = e.get("seq") if isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool) else 0
                q = _qid(d.get("quest"))
                if t == "quest_accepted" and q:
                    self._event(q, at, True, sq)
                elif t in ("quest_turned_in", "quest_removed", "quest_complete") and q:
                    self._event(q, at, False, sq)
                elif t == "level_up" and isinstance(d.get("level"), int) and not isinstance(d.get("level"), bool):
                    self.level = d["level"]
                elif t == "rare_seen" and _qid(d.get("npc")):
                    self.rare_seen[d["npc"]] = (at, d.get("map") if _qid(d.get("map")) else None)
                    if len(self.rare_seen) > 200:
                        self.rare_seen.pop(min(self.rare_seen, key=lambda k: self.rare_seen[k][0]))
                elif t == "zone_changed" and _qid(d.get("map")) and at >= self.map_t - SLACK_S:
                    self.map, self.map_t = d["map"], at
        self.wake.set()

    def _reflected(self, q, at, present):
        """The QUESTS section already shows this event: it agrees with it and is newer, or is newer by more than any send delay."""
        if self.feed_t is None:
            return False
        if (q in self.feed_q) == present:
            return at <= self.feed_t - SLACK_S
        return at <= self.feed_t - TRANSIT_S

    def _event(self, q, at, present, seq=0):
        # the game stamps whole seconds, so an accept and its instant completion share a time: the sequence number orders them,
        # and a redelivered copy of an older event (same or lower key) never overwrites a newer one
        old = self.ev.get(q)
        if self._reflected(q, at, present):
            if old and _newer(at, seq, old):
                del self.ev[q]                         # the section shows this newer event: an older remembered one is void
            return
        if not old or _newer(at, seq, old):
            self.ev[q] = (at, present, seq)

    def quests(self):
        with self.mu:
            cur = set(self.feed_q)
            for q, (_t, present, _s) in self.ev.items():
                (cur.add if present else cur.discard)(q)
            return self.map, sorted(cur)

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            self.map, self.map_t = st.get("map"), float(st.get("map_t") or -1.0)
            self.feed_q, self.feed_t = set(int(q) for q in st.get("feed_q") or []), st.get("feed_t")
            self.ev = {int(q): (float(v[0]), bool(v[1]), int(v[2]) if len(v) > 2 else 0) for q, v in (st.get("ev") or {}).items()}
            self.skills = {k: (int(v[0]), int(v[1])) for k, v in (st.get("skills") or {}).items()}
            self.level = st.get("level")
            self.prices = {int(k): int(v) for k, v in (st.get("prices") or {}).items()}
            self.have = {int(k): (int(v[0]), int(v[1])) for k, v in (st.get("have") or {}).items()}
            self.rare_seen = {int(k): (float(v[0]), v[1]) for k, v in (st.get("rare_seen") or {}).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self):
        if not self.state_path:
            return
        with self.mu:
            st = {"map": self.map, "map_t": self.map_t, "feed_q": sorted(self.feed_q), "feed_t": self.feed_t,
                  "ev": {str(q): [t, p, sq] for q, (t, p, sq) in self.ev.items()}, "skills": self.skills, "level": self.level,
                  "prices": {str(k): v for k, v in self.prices.items()}, "have": {str(k): list(v) for k, v in self.have.items()},
                  "rare_seen": {str(k): list(v) for k, v in self.rare_seen.items()}}
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            import os
            os.replace(tmp, self.state_path)
        except OSError:
            pass

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
        # the game's macro takes the FIRST living match, so the rare ones (few spawns, the best drop chance) lead and the common ones follow
        order = [nm for _n, _p, nm in sorted(names, key=lambda x: (x[0], -x[1])) if nm in shown]
        order = list(dict.fromkeys(order))[:16]
        if order:
            out.append({"type": "icon", "id": "qall", "targets": order, "tip": "Target the nearest quest enemy", "texture": "Ability_Hunter_SniperShot",
                        "iconSize": 26, "text": "Target any"})
        for k, r in enumerate(rows):
            out.append({"type": "row", "id": "q%d" % (k + 1), "gap": 4, "children": r + [{"type": "spacer"}] * (4 - len(r))})
        return out, dropped, skipped


    # ------------------------------------------------------------------ professions (generated, like the quests)
    def _source(self, c, m, iid, cnt=1):
        """Where he gets one reagent from here: his bags or bank (BagBrother), a creature in this map that drops it (measured chance >= 5%),
        a vendor here, a vendor in a capital, the auction house (Auctionator's last price when known) or a remote vendor."""
        g = self.gdb
        r = c.execute("SELECT name, json FROM item WHERE id=?", (iid,)).fetchone()
        name = r["name"] if r else "item %d" % iid
        bags, bank = self.have.get(iid, (0, 0))
        if bags >= cnt:
            return {"kind": "have", "where": "in your bags: %d" % bags, "name": name, "cost": 0.0}
        if bags + bank >= cnt:
            return {"kind": "have", "where": "in your bank: %d%s" % (bank, (", bags %d" % bags) if bags else ""), "name": name, "cost": 0.1}
        j = json.loads(r["json"]) if r and r["json"] else {}
        vend = [v for v in (j.get("vendors") or []) if isinstance(v, int)]
        for v in vend:                                  # a vendor in this zone beats farming it
            if c.execute("SELECT 1 FROM spawn WHERE kind='npc' AND id=? AND uimap_id=? LIMIT 1", (v, m)).fetchone():
                return {"kind": "vendor_here", "who": g._name(c, "npc", v), "name": name, "cost": 0.2}
        best = None
        for d in g._rates(c, item=iid):
            if (d.get("pct") or 0) < 5 or (d.get("src") == "forever" and (d.get("kills") or 0) < 10):
                continue                                # rare world drops and thin samples are not a farming source
            row = c.execute("SELECT name, flags FROM npc WHERE id=?", (d["npc"],)).fetchone()
            if not row or (row["flags"] or 0) != 0:
                continue                                # quest givers, vendors, trainers: not something he kills
            nm = row["name"]
            if not (nm and TARGETABLE.match(nm)):
                continue
            n = c.execute("SELECT COUNT(*) FROM spawn WHERE kind='npc' AND id=? AND uimap_id=?", (d["npc"], m)).fetchone()[0]
            if n < 2:
                continue                                # a one-off rare or event mob
            score = d["pct"] * min(n, 10)
            if best is None or score > best["score"]:
                best = {"kind": "drop", "npc": d["npc"], "who": nm, "pct": d["pct"], "cost": 0.0, "score": score}
        if best:
            best["name"] = name
            return best
        ah = {"kind": "ah", "name": name, "cost": 1.0}
        p = self.prices.get(iid)
        if p:                                           # Auctionator's last price: cheap stacks beat a trip, dear ones lose to farming
            ah.update(copper=p, cost=0.7 + p / 2500.0)
        if vend:                                        # a capital he passes anyway beats a remote vendor; a remote one is worse than the AH
            for v in vend:
                sp = c.execute("SELECT uimap_id FROM spawn WHERE kind='npc' AND id=? AND uimap_id IN (%s) LIMIT 1" % ",".join(str(x) for x in CAPITALS), (v,)).fetchone()
                if sp:
                    return {"kind": "vendor", "who": g._name(c, "npc", v), "zone": CAPITALS[sp[0]], "name": name, "cost": 0.6}
            if not p or ah["cost"] > 1.5:                # a remote vendor beats an unknown or dear AH price
                sp = c.execute("SELECT area_id FROM spawn WHERE kind='npc' AND id=? LIMIT 1", (vend[0],)).fetchone()
                zone = (g._zone(c, sp[0]).get("name") if sp else None) or ""
                return {"kind": "vendor", "who": g._name(c, "npc", vend[0]), "zone": zone, "name": name, "cost": 1.5}
        return ah

    @staticmethod
    def _where(sv, zone=False):
        k = sv["kind"]
        if k == "drop":
            return "%g%% from %s" % (round(sv["pct"], 1), sv["who"])
        if k == "craft":
            return "craft it: " + ", ".join("%s x%d (%s)" % (n2, c2, w2) for n2, c2, w2 in sv["parts"])
        if k == "have":
            return sv["where"]
        if k == "vendor_here":
            return "vendor here: %s" % sv["who"]
        if k == "vendor":
            return "vendor: %s%s" % (sv["who"], (", " + sv["zone"]) if zone and sv.get("zone") else "")
        return "buy on the AH" + (" ~%s each" % money(sv["copper"]) if sv.get("copper") else "")

    def _craftable(self, c, m, prof, cur, iid):
        """When a trainer recipe of the same profession he can already make produces this reagent, its cost is its own reagents' cost."""
        r = c.execute("SELECT name, reagents, learned FROM recipe WHERE profession=? COLLATE NOCASE AND product=? AND learn<=? LIMIT 1", (prof, iid, cur)).fetchone()
        if not r or (r["learned"] or "") not in TRAINABLE:
            return None
        try:
            parts = [(int(a), int(b)) for a, b in json.loads(r["reagents"] or "[]")]
        except (ValueError, TypeError):
            return None
        cost, desc = 0.1, []
        for pid, pc in parts:
            sv = self._source(c, m, pid, pc)
            cost += sv["cost"] * pc
            w = self._where(sv)
            desc.append((sv["name"], pc, w))
        r2 = c.execute("SELECT name FROM item WHERE id=?", (iid,)).fetchone()
        return {"kind": "craft", "name": r2["name"] if r2 else r["name"], "cost": cost, "parts": desc}

    def build_professions(self, m, skills, level):
        g = self.gdb
        out = []
        if not skills:
            return out
        with g.lock:
            c = g.connect()
            try:
                if not g._has(c, "recipe"):
                    return out
                order = sorted(skills, key=lambda n: (n in ("Cooking", "First Aid"), CRAFTING.index(n) if n in CRAFTING else 99))
                for k, prof in enumerate(order, 1):
                    cur, mx = skills[prof]
                    nxt = next((r for r in RANKS if r[0] > mx), None)
                    can_train = bool(nxt and cur >= nxt[1] and (level is None or level >= nxt[2]))
                    best = None
                    if cur < mx:
                        for r in c.execute("SELECT * FROM recipe WHERE profession=? COLLATE NOCASE AND learn<=? AND grey>?", (prof, cur, cur)):
                            if (r["learned"] or "") not in TRAINABLE:
                                continue                    # vendor, drop and quest recipes need the recipe item first
                            ch = skill_chance(cur, r["yellow"], r["grey"])
                            if ch <= 0:
                                continue
                            try:
                                reag = [(int(a), int(b)) for a, b in json.loads(r["reagents"] or "[]")]
                            except (ValueError, TypeError):
                                continue
                            srcs = []
                            for iid, cnt in reag:
                                sv = self._source(c, m, iid, cnt)
                                craft = self._craftable(c, m, prof, cur, iid)       # an intermediate he makes himself (Rough Blasting Powder)
                                if craft and craft["cost"] < sv["cost"]:
                                    sv = craft
                                srcs.append((iid, cnt, sv))
                            score = (sum(sv["cost"] * cnt for _i, cnt, sv in srcs) + (1.0 / ch - 1.0), -r["learn"])
                            if best is None or score < best[0]:
                                best = (score, r, ch, srcs)
                    hid, rows = "hp%d" % k, []
                    if cur >= mx:
                        if can_train:
                            text = "%s %d/%d: at the cap, train %s now" % (prof, cur, mx, nxt[3])
                        elif nxt:
                            text = "%s %d/%d: at the cap; %s needs skill %d and level %d" % (prof, cur, mx, nxt[3], nxt[1], nxt[2])
                        else:
                            text = "%s %d/%d: maxed" % (prof, cur, mx)
                    elif best:
                        r, ch = best[1], best[2]
                        color = "orange" if cur < r["yellow"] else ("yellow" if cur < r["green"] else "green")
                        text = "%s %d/%d: %s (%s%s)" % (prof, cur, mx, r["name"], color, "" if ch >= 1 else ", ~%d%% a point" % round(ch * 100))
                        if can_train:
                            text += "; you can train %s" % nxt[3]
                        cells = []
                        if r["product"]:
                            cells.append({"type": "icon", "item": r["product"], "tip": "Makes %s (%s)" % (r["name"], "trainer recipe" if r["learned"] != "Starting recipe" else "known"), "iconSize": 26})
                        drops = []
                        for iid, cnt, sv in best[3]:
                            if sv["kind"] == "drop":
                                where = "%g%% from %s" % (round(sv["pct"], 1), sv["who"])
                                drops.append(sv)
                            else:
                                where = self._where(sv, zone=True)
                            cells.append({"type": "icon", "item": iid, "tip": "%s x%d (%s)" % (sv["name"], cnt, where), "iconSize": 26})
                        for n in range(0, len(cells), 4):
                            part = cells[n:n + 4]
                            rows.append(part + [{"type": "spacer"}] * (4 - len(part)))
                        seen = set()
                        npc_cells = []
                        for sv in drops:
                            if sv["npc"] not in seen:
                                seen.add(sv["npc"])
                                npc_cells.append({"type": "icon", "npc": sv["npc"], "tip": sv["who"], "texture": texture(sv["who"]), "iconSize": 26})
                        if npc_cells:
                            rows.append(npc_cells[:4] + [{"type": "spacer"}] * (4 - len(npc_cells[:4])))
                    else:
                        text = "%s %d/%d: no trainer recipe gives points now%s" % (prof, cur, mx, ("; train %s" % nxt[3]) if can_train else "")
                    out.append({"type": "heading", "id": hid, "text": text})
                    for n, rr in enumerate(rows, 1):
                        out.append({"type": "row", "id": "p%d_%d" % (k, n), "gap": 4, "children": rr})
            finally:
                c.close()
        return out

    def build_rares(self, m, level, now=None):
        """A Rares heading and one row of up to 4 rare creatures that spawn here near his level (Questie rank), the ones seen alive on a
        nameplate in the last 15 minutes first. Clicking one targets it (out of combat). Nothing when there are none."""
        g, now = self.gdb, time.time() if now is None else now
        with g.lock:
            c = g.connect()
            try:
                rares = g._rares(c, m, level)
            finally:
                c.close()
        rares = [r for r in rares if TARGETABLE.match(r["name"] or "")]
        if not rares:
            return []
        with self.mu:
            seen = {n: t for n, (t, mm) in self.rare_seen.items() if now - t <= RARE_SEEN_S and mm in (None, m)}
        rares.sort(key=lambda r: (r["npc"] not in seen, -seen.get(r["npc"], 0)))
        rares = rares[:4]
        hot = [r for r in rares if r["npc"] in seen]
        if hot:
            text = "Rares here: %s seen alive in the last %d min" % (hot[0]["name"], RARE_SEEN_S // 60)
        else:
            text = "Rares here (none seen yet)"
        cells = [{"type": "icon", "npc": r["npc"], "tip": r["name"], "texture": "Ability_Hunter_MarkedForDeath", "iconSize": 26} for r in rares]
        return [{"type": "heading", "id": "hr", "text": text},
                {"type": "row", "id": "rr1", "gap": 4, "children": cells + [{"type": "spacer"}] * (4 - len(cells))}]

    def _current_ours(self, panel):
        return [ch for ch in panel.get("children", []) if ch.get("id") in OURS or QUEST_ROW.match(str(ch.get("id") or ""))]

    def run(self):
        try:
            m, quests = self.quests()
            if not m:
                return
            with self.mu:
                skills, level, known = dict(self.skills), self.level, self.feed_t is not None
            if not known:
                return                                  # right after a restart: wait for the QUESTS section, never blank the panel
            with self.mu:
                market = (tuple(sorted(self.prices.items())), tuple(sorted(self.have.items())))
                hot = tuple(sorted(n for n, (t, mm) in self.rare_seen.items() if time.time() - t <= RARE_SEEN_S))
            sig = (m, tuple(quests), tuple(sorted(skills.items())), level, market, hot)
            with self.lock:
                panel = next((p for p in self.get_doc().panels if p.get("id") == "squares"), None)
                if panel is None:
                    self.sig = None
                    return                              # the agent has not made the squares panel: nothing to keep in step
                if sig == self.sig and self.ours is not None and json.dumps(self._current_ours(panel), sort_keys=True) == self.ours:
                    return                              # nothing changed and nobody rewrote our rows
            ours, dropped, skipped = self.build(m, quests)
            try:
                ours = ours + self.build_rares(m, level)
            except Exception as e:                      # rares must never block the quest squares
                skipped.append("rares: %s" % type(e).__name__)
            prof, keep_prof = [], not skills            # PROFESSIONS not seen yet: keep whatever profession rows are there
            if skills:
                try:
                    prof = self.build_professions(m, skills, level)
                except Exception as e:                  # professions must never block the quest squares
                    skipped.append("professions: %s" % type(e).__name__)
                    keep_prof = True
            invalid = []
            with self.lock:
                doc = self.get_doc()
                panel = next((p for p in doc.panels if p.get("id") == "squares"), None)
                if panel is None:
                    return
                legacy = LEGACY if prof else set()      # hand-made profession rows go only once generated ones replace them
                old_prof = [ch for ch in panel.get("children", []) if PROF_ROW.match(str(ch.get("id") or ""))] if keep_prof else []
                ours = ours + (prof or old_prof)
                rest = [ch for ch in panel.get("children", []) if not (ch.get("id") in OURS or ch.get("id") in legacy or QUEST_ROW.match(str(ch.get("id") or "")))]
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
            self._save()
            self.last = {"t": time.time(), "map": m, "quests": quests, "skills": skills, "level": level, "rows": len(ours), "rows_left_out": dropped,
                         "quests_skipped": skipped, "dropped_invalid_children": invalid, "error": None}
        except Exception as e:                              # never break the worker; the next input retries
            self.last = {"t": time.time(), "error": "%s: %s" % (type(e).__name__, e)}
