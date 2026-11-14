"""Build the lookup index (one sqlite file) from the QuestieDB data that ships with Forever.

    python3 -m wowmentor.data.build [AddOns dir] [out.sqlite]

Takes about two seconds and 25 MB. The bridge runs this when QuestieDB_Forever.toc changes and uploads the result to the server, which
answers `wowui db ...` lookups from it. The raw decoded record of every quest, NPC, item and object is kept as JSON so any field stays
queryable.
"""
import json, os, sqlite3, sys, time

from . import qdb

SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE quest(id INTEGER PRIMARY KEY, name TEXT, qlevel INT, minlevel INT, maxlevel INT, zone_or_sort INT, races INT, classes INT,
  start_npc TEXT, finish_npc TEXT, prereq_all TEXT, prereq_any TEXT, exclusive TEXT, next_q INT, parent_q INT, flags INT, json TEXT);
CREATE TABLE npc(id INTEGER PRIMARY KEY, name TEXT, subname TEXT, minlevel INT, maxlevel INT, flags INT, zone_id INT, json TEXT);
CREATE TABLE spawn(kind TEXT, id INT, area_id INT, uimap_id INT, x REAL, y REAL);
CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT, ilevel INT, reqlevel INT, class INT, subclass INT, start_quest INT, json TEXT);
CREATE TABLE object(id INTEGER PRIMARY KEY, name TEXT, json TEXT);
CREATE TABLE zone(area_id INTEGER PRIMARY KEY, name TEXT, uimap_id INT, parent INT);
CREATE TABLE sort(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE training(class TEXT, level INT, spell_id INT, cost INT, requires TEXT);
CREATE TABLE drop_rate(npc INT, item INT, pct REAL, kills INT, src TEXT, quest INT);
CREATE TABLE boss_loot(instance TEXT, instance_name TEXT, category TEXT, levels TEXT, boss TEXT, npc INT, item INT, rate REAL);
"""
INDEXES = """CREATE INDEX q_name ON quest(name COLLATE NOCASE); CREATE INDEX q_zone ON quest(zone_or_sort, minlevel);
CREATE INDEX n_name ON npc(name COLLATE NOCASE); CREATE INDEX i_name ON item(name COLLATE NOCASE); CREATE INDEX o_name ON object(name COLLATE NOCASE);
CREATE INDEX s_id ON spawn(kind,id); CREATE INDEX s_area ON spawn(area_id); CREATE INDEX s_map ON spawn(uimap_id);
CREATE INDEX d_npc ON drop_rate(npc); CREATE INDEX d_item ON drop_rate(item); CREATE INDEX b_item ON boss_loot(item); CREATE INDEX b_boss ON boss_loot(boss COLLATE NOCASE);"""


def _j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _first(g):
    g = qdb._lua(g or {})
    return _j(g.get(1) or [])


def toc_path(addons):
    return os.path.join(addons, "QuestieDB", "QuestieDB_Forever.toc")


SCHEMA_TAG = "s3"          # bump when the tables change so every installed bridge rebuilds and re-uploads


EXTRA_SOURCES = (("TnnysMobLoot", "TnnysMobLoot_Forever.toc"), ("NeedRoll", "NeedRoll.toc"))


def fingerprint(addons):
    """Cheap change detector for the source files: size, mtime and the schema tag (plus the optional loot addons, so an update re-uploads)."""
    st = os.stat(toc_path(addons))
    fp = "%d:%d:%s" % (st.st_size, int(st.st_mtime), SCHEMA_TAG)
    for folder, toc in EXTRA_SOURCES:
        try:
            e = os.stat(os.path.join(addons, folder, toc))
            fp += ":%s=%d.%d" % (folder, e.st_size, int(e.st_mtime))
        except OSError:
            pass
    return fp


def drop_rows(addons, min_classic_pct=0.5):
    """(npc, item, percent, kills, source, quest flag) from Tnny's Mob Loot when installed: 'forever' rows are measured on Forever by Everloot
    (kills = how many kills the rate rests on), 'classic' rows are CMaNGOS estimates (rare world drops below min_classic_pct are left out)."""
    import re
    d = os.path.join(addons, "TnnysMobLoot")
    rows = []
    try:
        fv = open(os.path.join(d, "ForeverData.lua"), encoding="utf-8", errors="replace").read()
    except OSError:
        fv = ""
    for m in re.finditer(r'\[(\d+)\]=\{"[^"]*","([^"]*)"\}', fv):
        for part in m.group(2).split(","):
            f = part.split(":")
            try:
                if len(f) >= 4 and f[0].isdigit():
                    rows.append((int(m.group(1)), int(f[0]), float(f[1]), int(float(f[3])), "forever", 0))
            except ValueError:                      # one malformed entry (nil, empty) is skipped, not fatal
                continue
    try:
        st = open(os.path.join(d, "StarterData.lua"), encoding="utf-8", errors="replace").read()
    except OSError:
        st = ""
    i = st.find("npc={")
    j = st.find("from=", i) if i >= 0 else -1
    for m in re.finditer(r'\[(\d+)\]=\{"([^"]*)"', st[i:j if j > 0 else len(st)] if i >= 0 else ""):
        for part in m.group(2).split(","):
            f = part.split(":")
            try:
                if len(f) >= 2 and f[0].isdigit():
                    q = 1 if len(f) > 2 and f[2] == "q" else 0
                    pct = float(f[1])
                    if q or pct >= min_classic_pct:
                        rows.append((int(m.group(1)), int(f[0]), pct, None, "classic", q))
            except ValueError:
                continue
    return rows


def boss_rows(addons):
    """(instance id, instance name, category, level range, boss, npc, item, drop rate 0..1 or None) from NeedRoll's dungeon and raid files."""
    import re
    base = os.path.join(addons, "NeedRoll", "Data")
    rows = []
    for sub in ("Dungeons", "Raids"):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".lua"):
                continue
            try:
                t = open(os.path.join(d, fn), encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            iid = re.search(r'\bid\s*=\s*"([A-Z0-9_]+)"', t)
            nm = re.search(r'\bname\s*=\s*"([^"]+)"', t)
            cat = re.search(r'\bcategory\s*=\s*"([A-Z_]+)"', t)
            lv = re.search(r'levelRange\s*=\s*\{\s*(\d+)\s*,\s*(\d+)', t)
            levels = "%s-%s" % lv.groups() if lv else None
            enc = list(re.finditer(r'\{\s*id\s*=\s*"([A-Z0-9_]+)"\s*,\s*name\s*=\s*"([^"]+)"([^{}]*?)loot\s*=\s*\{', t))
            for k, e in enumerate(enc):
                end = enc[k + 1].start() if k + 1 < len(enc) else len(t)
                npc = re.search(r'npc\s*=\s*(\d+)', e.group(3))
                for it in re.finditer(r'itemID\s*=\s*(\d+)(?:\s*,\s*dropRate\s*=\s*([\d.]+))?', t[e.end():end]):
                    rows.append((iid.group(1) if iid else fn[:-4], nm.group(1) if nm else fn[:-4], cat.group(1) if cat else sub.upper(), levels,
                                 e.group(2), int(npc.group(1)) if npc else None, int(it.group(1)), float(it.group(2)) if it.group(2) else None))
    return rows


def training_rows(addons):
    """(class, level, spell id, cost in copper, required spell ids) from the WhatsTraining addon's Forever class files, if installed."""
    import re
    d = os.path.join(addons, "WhatsTraining", "Classes", "Camelot")
    rows = []
    if not os.path.isdir(d):
        return rows
    for fn in sorted(os.listdir(d)):
        m = re.match(r"^([A-Za-z]+)\.lua$", fn)
        if not m or m.group(1) in ("QuestSpells", "TrainerFactions", "HunterPets", "WarlockTomes"):
            continue
        try:
            text = open(os.path.join(d, fn), encoding="utf-8").read()
        except OSError:
            continue
        i = text.find("spellsByLevel")
        if i < 0:
            continue
        marks = [(int(mm.group(1)), mm.start(), mm.end()) for mm in re.finditer(r"\[(\d+)\]\s*=\s*\{", text[i:])]
        marks = [(lv, a + i, b + i) for lv, a, b in marks]
        for k, (lv, a, b) in enumerate(marks):
            end = marks[k + 1][1] if k + 1 < len(marks) else len(text)
            for em in re.finditer(r"\{\s*id\s*=\s*(\d+)\s*,\s*cost\s*=\s*(\d+)(?:\s*,\s*requiredIds\s*=\s*\{([\d,\s]*)\})?", text[b:end]):
                req = [int(x) for x in re.findall(r"\d+", em.group(3) or "")]
                rows.append((m.group(1).upper(), lv, int(em.group(1)), int(em.group(2)), _j(req)))
    return rows


def build(addons, out):
    t0 = time.time()
    db, z = qdb.QDB(toc_path(addons)), qdb.Zones(addons)
    tmp = out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.executescript(SCHEMA)
    for a, m in z.area2map.items():
        c.execute("INSERT INTO zone VALUES(?,?,?,?)", (a, z.area_name.get(a), m, z.parent.get(a)))
    for i, n in qdb.sort_names(addons).items():
        c.execute("INSERT INTO sort VALUES(?,?)", (i, n))
    for i in sorted(db.ids["Quest"]):
        q = db.get("Quest", i)
        c.execute("INSERT INTO quest VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            i, q.get("name"), q.get("questLevel"), q.get("requiredLevel"), q.get("requiredMaxLevel"), q.get("zoneOrSort"),
            q.get("requiredRaces", 0), q.get("requiredClasses", 0), _first(q.get("startedBy")), _first(q.get("finishedBy")),
            _j(q.get("preQuestGroup", [])), _j(q.get("preQuestSingle", [])), _j(q.get("exclusiveTo", [])),
            q.get("nextQuestInChain"), q.get("parentQuest"), q.get("questFlags"), _j(q)))
    for ent in ("Npc", "Object"):
        for i in sorted(db.ids[ent]):
            n = db.get(ent, i)
            if ent == "Npc":
                c.execute("INSERT INTO npc VALUES(?,?,?,?,?,?,?,?)", (i, n.get("name"), n.get("subName"), n.get("minLevel"), n.get("maxLevel"), n.get("npcFlags", 0), n.get("zoneID"), _j(n)))
            else:
                c.execute("INSERT INTO object VALUES(?,?,?)", (i, n.get("name"), _j(n)))
            for area, pts in (n.get("spawns") or {}).items():
                m = z.info(int(area))["uiMapId"]
                for p in pts:
                    c.execute("INSERT INTO spawn VALUES(?,?,?,?,?,?)", (ent.lower(), i, int(area), m, p[0], p[1]))
    for i in sorted(db.ids["Item"]):
        n = db.get("Item", i)
        c.execute("INSERT INTO item VALUES(?,?,?,?,?,?,?,?)", (i, n.get("name"), n.get("itemLevel"), n.get("requiredLevel"), n.get("class"), n.get("subClass"), n.get("startQuest"), _j(n)))
    for r in training_rows(addons):
        c.execute("INSERT INTO training VALUES(?,?,?,?,?)", r)
    for table, fn in (("drop_rate", drop_rows), ("boss_loot", boss_rows)):
        try:                                        # optional addons: a bad file must never stop the quest database from building
            rows = fn(addons)
            if rows:
                c.executemany("INSERT INTO %s VALUES(%s)" % (table, ",".join("?" * len(rows[0]))), rows)
        except Exception as e:
            c.execute("INSERT INTO meta VALUES(?,?)", ("error_" + table, "%s: %s" % (type(e).__name__, e)))
    c.executescript(INDEXES)
    for k, v in (("source", "QuestieDB_Forever.toc"), ("version", db.meta.get("Version", "")), ("commit", db.meta.get("X-BUILD-COMMIT", "")),
                 ("fingerprint", fingerprint(addons)), ("built", str(int(time.time())))):
        c.execute("INSERT INTO meta VALUES(?,?)", (k, v))
    c.commit()
    c.close()
    os.replace(tmp, out)
    return {"seconds": round(time.time() - t0, 1), "bytes": os.path.getsize(out)}


if __name__ == "__main__":
    A = sys.argv[1] if len(sys.argv) > 1 else "/Applications/World of Warcraft/_forever_/Interface/AddOns"
    O = sys.argv[2] if len(sys.argv) > 2 else "forever.sqlite"
    print(build(A, O))
