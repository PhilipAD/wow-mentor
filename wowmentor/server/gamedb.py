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
                raise DbError("unknown db route %r (status quest npc item object search find_quests trainers chain waypoint validate)" % sub, 404)
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
            sql += " AND (n.subname LIKE ? OR n.name LIKE ?)"; args += ["%" + like + "%"] * 2
        ids = [r[0] for r in c.execute(sql + " ORDER BY n.id LIMIT %d" % MAX_ROWS, args)]
        return {"count": len(ids), "trainers": [self._npc_ref(c, i) for i in ids]}

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
