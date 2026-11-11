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
"""
INDEXES = """CREATE INDEX q_name ON quest(name COLLATE NOCASE); CREATE INDEX q_zone ON quest(zone_or_sort, minlevel);
CREATE INDEX n_name ON npc(name COLLATE NOCASE); CREATE INDEX i_name ON item(name COLLATE NOCASE); CREATE INDEX o_name ON object(name COLLATE NOCASE);
CREATE INDEX s_id ON spawn(kind,id); CREATE INDEX s_area ON spawn(area_id); CREATE INDEX s_map ON spawn(uimap_id);"""


def _j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _first(g):
    g = qdb._lua(g or {})
    return _j(g.get(1) or [])


def toc_path(addons):
    return os.path.join(addons, "QuestieDB", "QuestieDB_Forever.toc")


SCHEMA_TAG = "s2"          # bump when the tables change so every installed bridge rebuilds and re-uploads


def fingerprint(addons):
    """Cheap change detector for the source file: size, mtime and the schema tag."""
    st = os.stat(toc_path(addons))
    return "%d:%d:%s" % (st.st_size, int(st.st_mtime), SCHEMA_TAG)


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
