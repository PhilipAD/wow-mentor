"""Lookups into the Forever quest, NPC, item and object database, answered from one sqlite file the bridge uploads.

The remote agent otherwise has to web-search facts it cannot know (Forever-only quest ids, moved NPCs). Everything here is read-only.
Routes (all GET, under /ui/<secret>/db/): status, quest, npc, item, object, search, find_quests, trainers, chain, waypoint, validate.
POST db/upload takes the file in base64 parts. Pure stdlib; thread-safe (own lock, one connection per call).
"""
import base64, gzip, hashlib, json, os, re, sqlite3, threading, time

from ..data.qdb import CLASS, NPCFLAG, RACE, bits

MAX_ROWS = 40
_ID = re.compile(r"\d+")


class DbError(ValueError):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


def _q(query, key, default=None):
    v = query.get(key)
    return (v[0] if isinstance(v, list) else v) if v else default


class GameDB:
    def __init__(self, data_dir, guide_text=None):
        self.path = os.path.join(data_dir, "forever.sqlite")
        self.tmp = os.path.join(data_dir, "forever.sqlite.upload")
        self.guide_text = guide_text            # callable(name) -> (resolved_name, text) or raises DbError
        self.lock = threading.Lock()
        self.parts = {}                         # sha -> {part index: bytes}

    # ---------------------------------------------------------------- plumbing
    def connect(self):
        if not os.path.exists(self.path):
            raise DbError("no game database on the server yet: the bridge uploads it a minute after it starts (check `wowui db status`)", 503)
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True)
        c.row_factory = sqlite3.Row
        return c

    def handle(self, method, route, query, body):
        sub = route[3:] if route.startswith("db/") else route
        try:
            if sub == "upload" and method == "POST":
                return 200, self.upload(body if isinstance(body, dict) else {})
            if sub == "status":
                return 200, self.status()
            fn = getattr(self, "r_" + sub, None)
            if not fn:
                raise DbError("unknown db route %r (status quest npc item object search find_quests trainers chain waypoint route spots squares drops boss rares training validate)" % sub, 404)
            with self.lock:
                c = self.connect()
                try:
                    return 200, dict({"ok": True}, **fn(c, query))
                finally:
                    c.close()
        except DbError as e:
            return e.code, {"ok": False, "errors": [e.msg]}
        except sqlite3.Error as e:
            return 500, {"ok": False, "errors": ["database error: %s" % e]}

    def status(self):
        if not os.path.exists(self.path):
            return {"ok": True, "present": False}
        try:
            c = self.connect()
            meta = dict(c.execute("SELECT k, v FROM meta").fetchall())
            counts = {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("quest", "npc", "item", "object")}
            c.close()
            return {"ok": True, "present": True, "meta": meta, "counts": counts, "bytes": os.path.getsize(self.path)}
        except sqlite3.Error as e:
            return {"ok": True, "present": False, "error": str(e)}

    def upload(self, b):
        """Parts of a gzip'd sqlite file: {sha, part, of, data(base64)}. The last part triggers verification and an atomic swap."""
        try:
            sha, part, of, data = str(b["sha"]), int(b["part"]), int(b["of"]), base64.b64decode(b["data"])
        except (KeyError, ValueError, TypeError) as e:
            raise DbError("upload needs {sha, part, of, data}: %s" % e)
        if not (0 <= part < of <= 400):
            raise DbError("bad part numbering")
        with self.lock:
            self.parts.setdefault(sha, {})[part] = data
            for k in [k for k in self.parts if k != sha]:
                del self.parts[k]
            if len(self.parts[sha]) < of:
                return {"ok": True, "complete": False, "have": len(self.parts[sha]), "of": of}
            got = self.parts.pop(sha)
            blob = b"".join(got[i] for i in range(of))
            if hashlib.sha256(blob).hexdigest() != sha:
                raise DbError("checksum mismatch: upload again")
            with open(self.tmp, "wb") as f:
                f.write(gzip.decompress(blob))
            try:
                c = sqlite3.connect(self.tmp)
                c.execute("SELECT COUNT(*) FROM quest").fetchone()
                c.close()
            except sqlite3.Error as e:
                os.remove(self.tmp)
                raise DbError("uploaded file is not a valid game database: %s" % e)
            os.replace(self.tmp, self.path)
        return {"ok": True, "complete": True, "bytes": os.path.getsize(self.path)}

    # ---------------------------------------------------------------- helpers
    def _one(self, c, kind, ident):
        tbl = {"quest": "quest", "npc": "npc", "item": "item", "object": "object"}[kind]
        if ident is None:
            raise DbError("pass id=<number> or name=<text>")
        if _ID.fullmatch(str(ident)):
            r = c.execute("SELECT * FROM %s WHERE id=?" % tbl, (int(ident),)).fetchone()
            return [r] if r else []
        rows = c.execute("SELECT * FROM %s WHERE name = ? COLLATE NOCASE ORDER BY id" % tbl, (str(ident),)).fetchall()
        return rows or c.execute("SELECT * FROM %s WHERE name LIKE ? ORDER BY id LIMIT 8" % tbl, ("%" + str(ident) + "%",)).fetchall()

    def _name(self, c, tbl, i):
        r = c.execute("SELECT name FROM %s WHERE id=?" % tbl, (i,)).fetchone()
        return r[0] if r else None

    def _zone(self, c, area):
        r = c.execute("SELECT name, uimap_id, parent FROM zone WHERE area_id=?", (area,)).fetchone()
        if r and r[0] is None and r[2]:
            p = c.execute("SELECT name FROM zone WHERE area_id=?", (r[2],)).fetchone()
            return {"areaId": area, "name": p[0] if p else None, "uiMapId": r[1]}
        return {"areaId": area, "name": r[0] if r else None, "uiMapId": r[1] if r else None}

    def _spawns(self, c, kind, i, limit=4):
        out = []
        for r in c.execute("SELECT area_id, uimap_id, x, y FROM spawn WHERE kind=? AND id=? LIMIT ?", (kind, i, limit)).fetchall():
            z = self._zone(c, r["area_id"])
            out.append({"zone": z["name"], "uiMapId": r["uimap_id"], "x": round(r["x"], 1), "y": round(r["y"], 1)})
        return out

    def _npc_ref(self, c, i):
        r = c.execute("SELECT name, subname, minlevel, maxlevel, flags FROM npc WHERE id=?", (i,)).fetchone()
        if not r:
            return {"id": i, "name": None}
        return {"id": i, "name": r["name"], "subName": r["subname"], "level": [r["minlevel"], r["maxlevel"]], "flags": bits(r["flags"] or 0, NPCFLAG),
                "spawns": self._spawns(c, "npc", i)}

    def _obj_ref(self, c, i):
        return {"id": i, "name": self._name(c, "object", i), "spawns": self._spawns(c, "object", i, 2)}

    def _qrefs(self, c, ids):
        return [{"id": i, "name": self._name(c, "quest", i)} for i in (ids or [])]

    @staticmethod
    def _lua(v):
        return {i + 1: x for i, x in enumerate(v)} if isinstance(v, list) else (v or {})

    def _givers(self, c, g):
        g = self._lua(g)
        return {"npcs": [self._npc_ref(c, i) for i in (g.get(1) or [])], "objects": [self._obj_ref(c, i) for i in (g.get(2) or [])],
                "items": [{"id": i, "name": self._name(c, "item", i)} for i in (g.get(3) or [])]}

    def _quest(self, c, row):
        q = json.loads(row["json"])
        o = self._lua(q.get("objectives"))
        obj = {}
        for key, label, tbl in ((1, "kill or talk", "npc"), (2, "object", "object"), (3, "item", "item")):
            if o.get(str(key)) or o.get(key):
                rows = o.get(str(key)) or o.get(key)
                obj[label] = [{"id": r[0], "name": self._name(c, tbl, r[0]), "text": r[1] if len(r) > 1 else None} for r in rows]
        zs = q.get("zoneOrSort") or 0
        if zs > 0:
            zone = self._zone(c, zs)
        else:
            s = c.execute("SELECT name FROM sort WHERE id=?", (zs,)).fetchone()
            zone = {"sort": zs, "sortName": s[0] if s else None}
        nxt = q.get("nextQuestInChain")
        res = {
            "id": row["id"], "name": row["name"], "questLevel": q.get("questLevel"), "requiredLevel": q.get("requiredLevel"), "maxLevel": q.get("requiredMaxLevel") or None,
            "zone": zone, "races": bits(q.get("requiredRaces", 0), RACE) or "any", "classes": bits(q.get("requiredClasses", 0), CLASS) or "any",
            "startedBy": self._givers(c, q.get("startedBy")), "finishedBy": self._givers(c, q.get("finishedBy")),
            "objectiveText": q.get("objectivesText"), "objectives": obj,
            "preQuestAll": self._qrefs(c, q.get("preQuestGroup")), "preQuestAny": self._qrefs(c, q.get("preQuestSingle")),
            "exclusiveWith": self._qrefs(c, q.get("exclusiveTo")), "nextInChain": self._qrefs(c, [nxt] if nxt else []),
            "parentQuest": self._qrefs(c, [q["parentQuest"]] if q.get("parentQuest") else []), "breadcrumbs": self._qrefs(c, q.get("breadcrumbs")),
            "requiredSkill": q.get("requiredSkill"), "requiredSpell": q.get("requiredSpell"),
            "sourceItem": ({"id": q["sourceItemId"], "name": self._name(c, "item", q["sourceItemId"])} if q.get("sourceItemId") else None),
            "reputationReward": q.get("reputationReward"),
        }
        return {k: v for k, v in res.items() if v not in (None, [], {}, 0)}

    # ---------------------------------------------------------------- routes
    def r_quest(self, c, q):
        rows = self._one(c, "quest", _q(q, "id") or _q(q, "name"))
        if not rows:
            raise DbError("no quest matches", 404)
        out = {"source": "QuestieDB Forever", "quests": [self._quest(c, r) for r in rows[:3]]}
        if len(rows) > 3:
            out["also_matches"] = [{"id": r["id"], "name": r["name"], "level": r["qlevel"]} for r in rows[3:MAX_ROWS]]
        return out

    def r_npc(self, c, q):
        rows = self._one(c, "npc", _q(q, "id") or _q(q, "name"))
        if not rows:
            raise DbError("no NPC matches", 404)
        out = []
        for r in rows[:5]:
            n = json.loads(r["json"])
            d = self._npc_ref(c, r["id"])
            d["startsQuests"] = [{"id": i, "name": self._name(c, "quest", i)} for i in (n.get("questStarts") or [])]
            d["endsQuests"] = [{"id": i, "name": self._name(c, "quest", i)} for i in (n.get("questEnds") or [])]
            out.append(d)
        return {"npcs": out}

    def r_item(self, c, q):
        rows = self._one(c, "item", _q(q, "id") or _q(q, "name"))
        if not rows:
            raise DbError("no item matches", 404)
        out = []
        for r in rows[:5]:
            it = json.loads(r["json"])
            d = {"id": r["id"], "name": r["name"], "itemLevel": it.get("itemLevel"), "requiredLevel": it.get("requiredLevel"), "class": it.get("class"), "subClass": it.get("subClass")}
            d["droppedBy"] = [{"id": i, "name": self._name(c, "npc", i)} for i in (it.get("npcDrops") or [])[:10]]
            d["objectDrops"] = [{"id": i, "name": self._name(c, "object", i)} for i in (it.get("objectDrops") or [])[:5]]
            d["soldBy"] = [self._npc_ref(c, i) for i in (it.get("vendors") or [])[:5]]
            d["startsQuest"] = it.get("startQuest") and {"id": it["startQuest"], "name": self._name(c, "quest", it["startQuest"])}
            d["questRewardFrom"] = self._qrefs(c, (it.get("questRewards") or [])[:8])
            d["relatedQuests"] = self._qrefs(c, (it.get("relatedQuests") or [])[:8])
            d["teachesSpell"] = it.get("teachesSpell")
            out.append({k: v for k, v in d.items() if v not in (None, [], {})})
        return {"note": "no price, quality, slot or stats exist offline: read those from the game (REWARDS, ITEMS) or the web", "items": out}

    def r_object(self, c, q):
        rows = self._one(c, "object", _q(q, "id") or _q(q, "name"))
        if not rows:
            raise DbError("no object matches", 404)
        return {"objects": [dict(self._obj_ref(c, r["id"]), startsQuests=self._qrefs(c, json.loads(r["json"]).get("questStarts")),
                                 endsQuests=self._qrefs(c, json.loads(r["json"]).get("questEnds"))) for r in rows[:5]]}

    def r_search(self, c, q):
        t = _q(q, "q") or _q(q, "name")
        if not t:
            raise DbError("pass q=<text>")
        out = {}
        for tbl in ("quest", "npc", "item", "object"):
            out[tbl] = [{"id": r[0], "name": r[1]} for r in c.execute("SELECT id, name FROM %s WHERE name LIKE ? ORDER BY id LIMIT 15" % tbl, ("%" + t + "%",))]
        return out

    def _area(self, c, text):
        if text is None:
            return None
        if _ID.fullmatch(text):
            return int(text)
        r = c.execute("SELECT area_id FROM zone WHERE name = ? COLLATE NOCASE", (text,)).fetchone() or \
            c.execute("SELECT area_id FROM zone WHERE name LIKE ? ORDER BY area_id LIMIT 1", ("%" + text + "%",)).fetchone()
        if not r:
            raise DbError("unknown zone %r" % text)
        return r[0]

    def r_find_quests(self, c, q):
        area = self._area(c, _q(q, "zone"))
        level = int(_q(q, "level")) if _q(q, "level") else None
        tol = int(_q(q, "tol", 3))
        klass = {v.lower(): k for k, v in CLASS.items()}.get((_q(q, "class") or "").lower())
        race = {v.lower(): k for k, v in RACE.items()}.get((_q(q, "race") or "").lower())
        sql, args = "SELECT id, name, qlevel, minlevel, zone_or_sort, races, classes, start_npc FROM quest WHERE 1=1", []
        if area is not None:
            sql += " AND zone_or_sort=?"; args.append(area)
        if level is not None:
            sql += " AND qlevel BETWEEN ? AND ? AND (minlevel IS NULL OR minlevel <= ?)"; args += [level - tol, level + tol, level]
        rows = []
        for r in c.execute(sql + " ORDER BY minlevel, id", args):
            if klass and r["classes"] and not (r["classes"] & klass):
                continue
            if race and r["races"] and not (r["races"] & race):
                continue
            npcs = json.loads(r["start_npc"] or "[]")
            rows.append({"id": r["id"], "name": r["name"], "level": r["qlevel"], "requiredLevel": r["minlevel"],
                         "giver": (self._npc_ref(c, npcs[0]) if npcs else None)})
            if len(rows) >= MAX_ROWS:
                break
        return {"count": len(rows), "truncated": len(rows) >= MAX_ROWS, "quests": rows}

    def r_trainers(self, c, q):
        area = self._area(c, _q(q, "zone"))
        like = _q(q, "like")
        sql = "SELECT DISTINCT n.id FROM npc n JOIN spawn s ON s.kind='npc' AND s.id=n.id WHERE (n.flags & 16) != 0"
        args = []
        if area is not None:
            sql += " AND s.area_id=?"; args.append(area)
        if like:
            stems = [w for w in re.split(r"\s+", like.strip()) if w]
            stems = [w[:5] if len(w) >= 7 else w for w in stems]          # "alchemy" also finds "Alchemist"
            for w in stems:
                sql += " AND (n.subname LIKE ? OR n.name LIKE ?)"; args += ["%" + w + "%"] * 2
        ids = [r[0] for r in c.execute(sql + " ORDER BY n.id LIMIT %d" % MAX_ROWS, args)]
        out = {"count": len(ids), "trainers": [self._npc_ref(c, i) for i in ids]}
        if not ids:
            out["hint"] = "no trainer matched: `like` matches the NPC's name or title (e.g. 'Warlock Trainer'); try a shorter stem such as 'alchem', or drop `like` to list every trainer in the zone"
        return out

    def r_chain(self, c, q):
        rows = self._one(c, "quest", _q(q, "id") or _q(q, "name"))
        if not rows:
            raise DbError("no quest matches", 404)
        start = rows[0]["id"]
        before, seen, frontier = [], {start}, [start]
        for _ in range(12):
            nxt = []
            for qid in frontier:
                r = c.execute("SELECT prereq_all, prereq_any FROM quest WHERE id=?", (qid,)).fetchone()
                for pid in (json.loads(r[0] or "[]") + json.loads(r[1] or "[]")) if r else []:
                    if pid not in seen:
                        seen.add(pid); nxt.append(pid); before.append(pid)
            frontier = nxt
            if not frontier:
                break
        after, cur = [], start
        for _ in range(20):
            r = c.execute("SELECT next_q FROM quest WHERE id=?", (cur,)).fetchone()
            if not r or not r[0] or r[0] in after:
                break
            after.append(r[0]); cur = r[0]
        q0 = self._quest(c, rows[0])
        return {"quest": {"id": start, "name": rows[0]["name"]}, "mustCompleteBefore": self._qrefs(c, before), "thenNext": self._qrefs(c, after),
                "exclusiveWith": q0.get("exclusiveWith", []), "breadcrumbs": q0.get("breadcrumbs", []), "races": q0.get("races"), "classes": q0.get("classes")}

    def r_waypoint(self, c, q):
        npc, qid = _q(q, "npc"), _q(q, "quest")
        if qid:
            rows = self._one(c, "quest", qid)
            if not rows:
                raise DbError("no quest matches", 404)
            which = _q(q, "which", "start")
            ids = json.loads(rows[0]["finish_npc" if which == "finish" else "start_npc"] or "[]")
            if not ids:
                raise DbError("that quest has no NPC %s point" % which)
            target, title = ids[0], "%s (%s)" % (rows[0]["name"], which)
        else:
            rows = self._one(c, "npc", npc or _q(q, "name"))
            if not rows:
                raise DbError("no NPC matches", 404)
            target, title = rows[0]["id"], rows[0]["name"]
        sp = self._spawns(c, "npc", target, 6)
        if not sp:
            raise DbError("that NPC has no known spawn")
        return {"npc": {"id": target, "name": self._name(c, "npc", target)}, "spawns": sp, "first": dict(sp[0], title=title),
                "command": "wowui waypoint w1 --map %s --x %s --y %s --title %s" % (sp[0]["uiMapId"], sp[0]["x"], sp[0]["y"], json.dumps(title))}

    def r_training(self, c, q):
        """Spells a class can train, from the WhatsTraining data: level, spell id, cost. Names are not offline: pass the ids to `wowui watch --spells`
        and read SPELL_WATCH. class=Warlock [level=N: only what is trainable by then] [from=M: only levels above M]."""
        klass = (_q(q, "class") or "").upper()
        if not klass:
            raise DbError("pass class=<e.g. Warlock>")
        try:
            hi = int(_q(q, "level")) if _q(q, "level") else 80
            lo = int(_q(q, "from")) if _q(q, "from") else 0
            rows = c.execute("SELECT level, spell_id, cost, requires FROM training WHERE class=? AND level BETWEEN ? AND ? ORDER BY level, spell_id", (klass, lo + 1 if lo else 0, hi)).fetchall()
        except sqlite3.OperationalError:
            raise DbError("this database has no training table yet: the bridge uploads a new one within a minute of restarting", 503)
        out, total = [], 0
        for r in rows:
            total += r["cost"]
            out.append({"level": r["level"], "spell": r["spell_id"], "cost_copper": r["cost"], "cost": "%dg %ds %dc" % (r["cost"] // 10000, (r["cost"] % 10000) // 100, r["cost"] % 100),
                        "requires": json.loads(r["requires"] or "[]")})
        return {"class": klass, "count": len(out), "total_cost_copper": total, "spells": out[:120],
                "note": "spell names are not in the offline data: `wowui watch --spells <ids>` then read SPELL_WATCH. Pet and tome spells are not listed."}

    def r_spots(self, c, q):
        """Where to find the creatures a quest wants killed: the densest spawn clusters (6x6 map-unit cells), best first.
        quest=<id> or npc=<id|name>."""
        creatures = []
        if _q(q, "quest"):
            rows = self._one(c, "quest", _q(q, "quest"))
            if not rows:
                raise DbError("no quest matches", 404)
            o = self._lua(json.loads(rows[0]["json"]).get("objectives"))
            for rr in (o.get("1") or o.get(1) or []):
                creatures.append(rr[0])
            for it in (o.get("3") or o.get(3) or []):
                itrow = c.execute("SELECT json FROM item WHERE id=?", (it[0],)).fetchone()
                creatures += (json.loads(itrow[0]).get("npcDrops") or [])[:6] if itrow else []
        elif _q(q, "npc"):
            creatures = [r["id"] for r in self._one(c, "npc", _q(q, "npc"))[:5]]
        if not creatures:
            raise DbError("pass quest=<id> (with creature or item objectives) or npc=<id|name>")
        out = []
        for nid in list(dict.fromkeys(creatures))[:8]:
            cells = {}
            for r in c.execute("SELECT uimap_id, x, y, area_id FROM spawn WHERE kind='npc' AND id=? AND uimap_id IS NOT NULL", (nid,)):
                key = (r["uimap_id"], int(r["x"] // 6), int(r["y"] // 6))
                cell = cells.setdefault(key, {"n": 0, "sx": 0.0, "sy": 0.0, "area": r["area_id"]})
                cell["n"] += 1; cell["sx"] += r["x"]; cell["sy"] += r["y"]
            best = sorted(cells.items(), key=lambda kv: -kv[1]["n"])[:3]
            out.append({"npc": {"id": nid, "name": self._name(c, "npc", nid)},
                        "clusters": [{"uiMapId": k[0], "zone": self._zone(c, v["area"])["name"], "x": round(v["sx"] / v["n"], 1), "y": round(v["sy"] / v["n"], 1), "spawn_points": v["n"]} for k, v in best],
                        "total_spawn_points": sum(v["n"] for v in cells.values())})
        return {"creatures": out, "note": "spawn points are Questie's; patrols and rare spawns have few points. The first cluster is where most of them stand."}

    def _has(self, c, tbl):
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone() is not None

    def _rates(self, c, npc=None, item=None):
        """Drop rates from Tnny's Mob Loot: Forever-measured first (with kills), Classic estimates where Forever has none for that pair."""
        if not self._has(c, "drop_rate"):
            return []
        col, val = ("npc", npc) if npc is not None else ("item", item)
        best = {}
        for r in c.execute("SELECT npc, item, pct, kills, src, quest FROM drop_rate WHERE %s=?" % col, (val,)):
            k = (r["npc"], r["item"])
            if k not in best or (best[k]["src"] == "classic" and r["src"] == "forever"):
                best[k] = dict(r)
        return sorted(best.values(), key=lambda d: -d["pct"])

    def r_drops(self, c, q):
        """What a creature drops, or which creatures drop an item, with drop chances (Tnny's Mob Loot: Forever-measured rates from Everloot
        with their kill counts, else labelled Classic estimates). npc=<id|name> or item=<id|name>; with map=<uiMapId> only creatures there."""
        if not self._has(c, "drop_rate"):
            raise DbError("no drop rates on the server: install Tnny's Mob Loot on the player's Mac; the bridge then rebuilds and uploads the database", 503)
        m = _q(q, "map")
        if _q(q, "npc"):
            rows = self._one(c, "npc", _q(q, "npc"))
            if not rows:
                raise DbError("no npc matches", 404)
            out = []
            for r in rows[:3]:
                drops = [{"item": d["item"], "name": self._name(c, "item", d["item"]), "pct": d["pct"], "kills": d["kills"], "source": d["src"],
                          "quest_item": bool(d["quest"])} for d in self._rates(c, npc=r["id"])[:40]]
                out.append({"npc": r["id"], "name": r["name"], "level": [r["minlevel"], r["maxlevel"]], "drops": drops})
            return {"npcs": out, "note": "source forever = measured on Forever (kills = sample size); classic = CMaNGOS estimate"}
        if _q(q, "item"):
            rows = self._one(c, "item", _q(q, "item"))
            if not rows:
                raise DbError("no item matches", 404)
            out = []
            for r in rows[:3]:
                lst = []
                for d in self._rates(c, item=r["id"]):
                    if m and not c.execute("SELECT 1 FROM spawn WHERE kind='npc' AND id=? AND uimap_id=? LIMIT 1", (d["npc"], int(m))).fetchone():
                        continue
                    lst.append({"npc": d["npc"], "name": self._name(c, "npc", d["npc"]), "pct": d["pct"], "kills": d["kills"], "source": d["src"]})
                out.append({"item": r["id"], "name": r["name"], "droppedBy": lst[:40]})
            return {"items": out, "note": "best chance first; source forever = measured on Forever, classic = estimate"}
        raise DbError("pass npc=<id|name> or item=<id|name> [map=<uiMapId>]")

    def r_boss(self, c, q):
        """Dungeon and raid boss loot with drop chances (NeedRoll's tables, Wowhead Forever rates where known).
        name=<boss or instance name>, or item=<id|name> for which bosses drop it."""
        if not self._has(c, "boss_loot"):
            raise DbError("no boss loot on the server: install NeedRoll on the player's Mac; the bridge then rebuilds and uploads the database", 503)
        if _q(q, "item"):
            ids = [r["id"] for r in self._one(c, "item", _q(q, "item"))[:3]]
            rows = [dict(r) for i in ids for r in c.execute("SELECT * FROM boss_loot WHERE item=?", (i,))]
        elif _q(q, "name"):
            n = "%" + _q(q, "name") + "%"
            rows = [dict(r) for r in c.execute("SELECT * FROM boss_loot WHERE boss LIKE ? OR instance_name LIKE ? OR instance LIKE ? LIMIT 300", (n, n, n))]
        else:
            raise DbError("pass name=<boss or instance> or item=<id|name>")
        out = [{"instance": r["instance_name"], "levels": r["levels"], "boss": r["boss"], "item": r["item"], "name": self._name(c, "item", r["item"]),
                "pct": round(r["rate"] * 100, 2) if r["rate"] is not None else None} for r in rows]
        out.sort(key=lambda d: (d["instance"] or "", d["boss"] or "", -(d["pct"] or -1)))
        return {"count": len(out), "loot": out[:200], "note": "pct is None where NeedRoll has no rate yet"}

    RARE_RANKS = {4: "rare", 2: "rare elite"}           # Questie's npc rank: 1 elite, 2 rare elite, 3 boss, 4 rare

    def _rares(self, c, m, level=None, lo=4, hi=3):
        """Rare creatures that spawn in a map, closest to the level first (level-lo .. level+hi when a level is given)."""
        out = []
        for r in c.execute("SELECT DISTINCT n.id, n.name, n.minlevel, n.maxlevel, n.flags, n.json FROM npc n JOIN spawn s ON s.kind='npc' AND s.id=n.id "
                           "WHERE s.uimap_id=? AND n.json LIKE '%\"rank\":%'", (m,)):
            try:
                j = json.loads(r["json"] or "{}")
            except ValueError:
                continue
            rank = j.get("rank")
            if rank not in self.RARE_RANKS or (r["flags"] or 0) & (2 | 128):   # quest givers and vendors are not hunted
                continue
            mn, mx = r["minlevel"] or 0, r["maxlevel"] or r["minlevel"] or 0
            if level is not None and (mx < level - lo or mn > level + hi):
                continue
            pts = c.execute("SELECT x, y FROM spawn WHERE kind='npc' AND id=? AND uimap_id=? LIMIT 8", (r["id"], m)).fetchall()
            out.append({"npc": r["id"], "name": r["name"], "level": [mn, mx], "rank": self.RARE_RANKS[rank],
                        "spawns": [[round(p["x"], 1), round(p["y"], 1)] for p in pts]})
        out.sort(key=lambda d: (abs(((d["level"][0] + d["level"][1]) / 2.0) - level) if level is not None else 0, d["name"]))
        return out

    def r_rares(self, c, q):
        """Rare creatures in a map (Questie's rank: rare and rare elite) with levels and spawn points, closest to --level first.
        map=<uiMapId> (required), level=<n> keeps level-4 .. level+3."""
        try:
            m = int(_q(q, "map"))
        except (TypeError, ValueError):
            raise DbError("pass map=<uiMapId> (FAST Position: map=...) [level=N]")
        lv = _q(q, "level")
        lv = int(lv) if lv and str(lv).isdigit() else None
        rows = self._rares(c, m, lv)
        for d in rows:
            d["drops"] = [{"item": x["item"], "name": self._name(c, "item", x["item"]), "pct": x["pct"]} for x in self._rates(c, npc=d["npc"])[:6]] if self._has(c, "drop_rate") else []
        return {"map": m, "count": len(rows), "rares": rows}

    def r_squares(self, c, q):
        """Everything worth a preview square in one map: for each given quest, every creature IN THAT MAP that is a kill objective or drops an
        objective item (full drop list, not capped), plus objects there that hold an objective; for each given item, every creature in the map
        that drops it. map=<uiMapId> (required), quests=<id,id,...>, items=<id,id,...>. Creatures are sorted by spawn points in the map."""
        try:
            m = int(_q(q, "map"))
        except (TypeError, ValueError):
            raise DbError("pass map=<uiMapId> (FAST Position: map=...), plus quests=<ids> and/or items=<ids>")
        quests = [int(x) for x in re.findall(r"\d+", _q(q, "quests", "") or "")][:25]
        items = [int(x) for x in re.findall(r"\d+", _q(q, "items", "") or "")][:25]

        def count(kind, i):
            return c.execute("SELECT COUNT(*) FROM spawn WHERE kind=? AND id=? AND uimap_id=?", (kind, i, m)).fetchone()[0]

        def here(kind, ids):
            out = []
            for i in dict.fromkeys(ids):
                n = count(kind, i)
                if n:
                    out.append({"id": i, "name": self._name(c, "npc" if kind == "npc" else "object", i), "spawn_points": n})
            return sorted(out, key=lambda d: -d["spawn_points"])

        def item_info(iid):
            r = c.execute("SELECT name, json FROM item WHERE id=?", (iid,)).fetchone()
            if not r:
                return {"item": iid, "name": None, "droppedHereBy": [], "objectsHere": []}
            j = json.loads(r["json"])
            rates = {d["npc"]: d for d in self._rates(c, item=iid)}
            ids = list(dict.fromkeys((j.get("npcDrops") or []) + list(rates)))
            by = here("npc", ids)
            for b in by:
                d = rates.get(b["id"])
                if d:
                    b["pct"], b["kills"], b["source"] = d["pct"], d["kills"], d["src"]
            by.sort(key=lambda b: (-(b.get("pct") or 0), -b["spawn_points"]))
            return {"item": iid, "name": r["name"], "droppedHereBy": by, "objectsHere": here("object", j.get("objectDrops") or [])}

        out_q = []
        for qid in quests:
            row = c.execute("SELECT name, json FROM quest WHERE id=?", (qid,)).fetchone()
            if not row:
                continue
            o = self._lua(json.loads(row["json"]).get("objectives"))
            kills = here("npc", [rr[0] for rr in (o.get("1") or o.get(1) or [])])
            objs = here("object", [rr[0] for rr in (o.get("2") or o.get(2) or [])])
            its = [item_info(it[0]) for it in (o.get("3") or o.get(3) or [])]
            out_q.append({"quest": qid, "name": row["name"], "kill": kills, "objects": objs, "items": its,
                          "nothing_here": not kills and not objs and not any(i["droppedHereBy"] or i["objectsHere"] for i in its)})
        return {"map": m, "quests": out_q, "items": [item_info(i) for i in items],
                "note": "droppers are sorted by drop chance where Tnny's Mob Loot knows it (pct; source forever = measured on Forever with its kill "
                        "count, classic = estimate), else by spawn points. An empty quests list right after a reload means the QUESTS feed section has "
                        "not arrived yet: pass --quests."}

    def r_route(self, c, q):
        """Order quest stops for the player: turn-ins for finished quests, objective locations for open ones.
        quests=<ids> done=<ids that are complete> map=<uiMapId> x=<0-100> y=<0-100>. Nearest-neighbour inside a map, the player's own map first."""
        ids = [int(i) for i in re.findall(r"\d+", _q(q, "quests") or "")][:40]
        done = {int(i) for i in re.findall(r"\d+", _q(q, "done") or "")}
        if not ids:
            raise DbError("pass quests=<comma list of quest ids>")
        here = (int(_q(q, "map")), float(_q(q, "x")), float(_q(q, "y"))) if _q(q, "map") and _q(q, "x") and _q(q, "y") else None

        def spawn_of(kind, i):
            r = c.execute("SELECT uimap_id, x, y, area_id FROM spawn WHERE kind=? AND id=? AND uimap_id IS NOT NULL ORDER BY (uimap_id=?) DESC LIMIT 1", (kind, i, here[0] if here else -1)).fetchone()
            return r

        stops, unknown = [], []
        for qid in ids:
            row = c.execute("SELECT id, name, qlevel, json, finish_npc FROM quest WHERE id=?", (qid,)).fetchone()
            if not row:
                unknown.append(qid)
                continue
            data = json.loads(row["json"])
            pt, kind, who = None, "turn-in", None
            fin = json.loads(row["finish_npc"] or "[]")
            if qid not in done:
                o = self._lua(data.get("objectives"))
                for key, k2, tbl in ((1, "kill or talk", "npc"), (2, "object", "object")):
                    rows = o.get(str(key)) or o.get(key) or []
                    for rr in rows:
                        pt = spawn_of(tbl, rr[0])
                        if pt:
                            kind, who = "objective (%s)" % k2, self._name(c, tbl, rr[0]); break
                    if pt:
                        break
                if not pt:
                    for it in (o.get("3") or o.get(3) or []):          # an item objective: its drop sources
                        itrow = c.execute("SELECT json FROM item WHERE id=?", (it[0],)).fetchone()
                        drops = (json.loads(itrow[0]).get("npcDrops") or []) if itrow else []
                        for nid in drops[:6]:
                            pt = spawn_of("npc", nid)
                            if pt:
                                kind, who = "objective (item from %s)" % self._name(c, "npc", nid), self._name(c, "npc", nid); break
                        if pt:
                            break
            if not pt and fin:
                pt = spawn_of("npc", fin[0]); who = self._name(c, "npc", fin[0])
            if not pt:
                unknown.append(qid)
                continue
            stops.append({"quest": qid, "name": row["name"], "kind": kind, "who": who, "uiMapId": pt["uimap_id"], "x": round(pt["x"], 1), "y": round(pt["y"], 1),
                          "zone": self._zone(c, pt["area_id"])["name"]})
        order, cur = [], here
        pool = list(stops)
        while pool:
            same = [s for s in pool if cur is None or s["uiMapId"] == cur[0]] or pool
            nxt = min(same, key=lambda s: ((s["x"] - cur[1]) ** 2 + (s["y"] - cur[2]) ** 2) if cur and s["uiMapId"] == cur[0] else 0)
            order.append(nxt); pool.remove(nxt); cur = (nxt["uiMapId"], nxt["x"], nxt["y"])
        return {"stops": order, "no_location_known": unknown,
                "note": "straight-line order inside each map; flight paths, hearthstone and walls are not considered: check the first stop against the guide before pinning"}

    def r_validate(self, c, q):
        """Check every [QA|QC|QT id] in a guide against the database: unknown ids, class/race gates, and [G x,y Zone] too far from the giver."""
        name = _q(q, "guide")
        if not name or not self.guide_text:
            raise DbError("pass guide=<guide name>")
        gname, text = self.guide_text(name)
        klass = {v.lower(): k for k, v in CLASS.items()}.get((_q(q, "class") or "").lower())
        race = {v.lower(): k for k, v in RACE.items()}.get((_q(q, "race") or "").lower())
        issues, checked = [], 0
        for n, line in enumerate(text.splitlines(), 1):
            qs = re.findall(r"\[(QA|QC|QT)(\d+)", line)
            if not qs:
                continue
            at = re.search(r"\[A\s+([^\]]+)\]", line)
            if at and (klass or race):
                wanted = {w.strip().lower().replace(" ", "") for w in at.group(1).split(",") if w.strip()}
                me = {x for x in ((_q(q, "class") or "").lower(), (_q(q, "race") or "").lower().replace(" ", "")) if x}
                if race:
                    me.add("alliance" if RACE.get(race) in ("Human", "Dwarf", "Night Elf", "Gnome", "Draenei", "Worgen", "Skyborne Alliance") else "horde")
                if wanted and not (wanted & me):
                    continue                       # the step is tagged for other classes or races: it never shows for this player
            g = re.search(r"\[G\s*([\d.]+)\s*,\s*([\d.]+)\s*([^\]]*)\]", line)
            for kind, sid in qs:
                qid = int(sid)
                r = c.execute("SELECT id, name, races, classes, start_npc, finish_npc FROM quest WHERE id=?", (qid,)).fetchone()
                checked += 1
                if not r:
                    issues.append({"line": n, "kind": "UNKNOWN_QUEST", "quest": qid, "text": line[:100]})
                    continue
                if klass and r["classes"] and not (r["classes"] & klass):
                    issues.append({"line": n, "kind": "WRONG_CLASS", "quest": qid, "name": r["name"], "classes": bits(r["classes"], CLASS)})
                if race and r["races"] and not (r["races"] & race):
                    issues.append({"line": n, "kind": "WRONG_RACE", "quest": qid, "name": r["name"], "races": bits(r["races"], RACE)})
                if g and kind in ("QA", "QT"):
                    npcs = json.loads(r["start_npc" if kind == "QA" else "finish_npc"] or "[]")
                    zone = g.group(3).strip()
                    zr = c.execute("SELECT uimap_id FROM zone WHERE name = ? COLLATE NOCASE ORDER BY area_id LIMIT 1", (zone,)).fetchone() if zone else None
                    if npcs and zr:
                        gx, gy = float(g.group(1)), float(g.group(2))
                        pts = c.execute("SELECT x, y, id FROM spawn WHERE kind='npc' AND id IN (%s) AND uimap_id=?" % ",".join("?" * len(npcs)), npcs + [zr[0]]).fetchall()
                        if pts:
                            best = min(pts, key=lambda p: (p["x"] - gx) ** 2 + (p["y"] - gy) ** 2)
                            dist = ((best["x"] - gx) ** 2 + (best["y"] - gy) ** 2) ** 0.5
                            if dist > 6:
                                issues.append({"line": n, "kind": "FAR_FROM_NPC", "quest": qid, "name": r["name"], "guide_xy": [gx, gy],
                                               "db_xy": [round(best["x"], 1), round(best["y"], 1)], "distance": round(dist, 1)})
        return {"guide": gname, "quest_tags_checked": checked, "issues": issues, "note": "FAR_FROM_NPC may be a patrol or a different NPC on the same line: confirm before editing"}
