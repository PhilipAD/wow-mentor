"""Reader for the QuestieDB database that ships with Forever (a TOC file of base64 CBOR metadata lines). Stdlib only.

The addon reads these with C_AddOns.GetAddOnMetadata at runtime; we read the file directly. See data/build.py for the sqlite index
that the server answers lookups from. Read-only: nothing here ever writes to the game folder.
"""
import base64, re, struct, zlib

# ---------- CBOR ----------
def cbor_decode(b):
    v, i = _dec(b, 0)
    return v

def _dec(b, i):
    ib = b[i]; mt = ib >> 5; ai = ib & 31; i += 1
    if ai < 24: n = ai
    elif ai == 24: n = b[i]; i += 1
    elif ai == 25: n = int.from_bytes(b[i:i+2], 'big'); i += 2
    elif ai == 26: n = int.from_bytes(b[i:i+4], 'big'); i += 4
    elif ai == 27: n = int.from_bytes(b[i:i+8], 'big'); i += 8
    elif ai == 31: n = None
    else: raise ValueError("bad cbor ai %d" % ai)
    if mt == 0: return n, i
    if mt == 1: return -1 - n, i
    if mt == 2: return bytes(b[i:i+n]).decode('utf-8', 'replace'), i+n  # Lua strings arrive as CBOR byte strings
    if mt == 3: return b[i:i+n].decode('utf-8', 'replace'), i+n
    if mt == 4:
        out = []
        if n is None:
            while b[i] != 0xff:
                v, i = _dec(b, i); out.append(v)
            return out, i+1
        for _ in range(n):
            v, i = _dec(b, i); out.append(v)
        return out, i
    if mt == 5:
        out = {}
        if n is None:
            while b[i] != 0xff:
                k, i = _dec(b, i); v, i = _dec(b, i); out[k] = v
            return out, i+1
        for _ in range(n):
            k, i = _dec(b, i); v, i = _dec(b, i); out[k] = v
        return out, i
    if mt == 6: return _dec(b, i)
    if mt == 7:
        if ai == 20: return False, i
        if ai == 21: return True, i
        if ai in (22, 23): return None, i
        if ai == 25: return struct.unpack('>e', b[i-2:i])[0], i
        if ai == 26: return struct.unpack('>f', b[i-4:i])[0], i
        if ai == 27: return struct.unpack('>d', b[i-8:i])[0], i
        return None, i
    raise ValueError("bad cbor")

def as_list(v):
    """Lua sequence may arrive as list or {1:..,2:..} map."""
    if isinstance(v, dict):
        return [v[k] for k in sorted(k for k in v if isinstance(k, int))]
    return v

def _lua(v):
    """A dense Lua table arrives as a CBOR array; sparse as a map. Normalise to dict for rows."""
    if isinstance(v, list): return {i + 1: x for i, x in enumerate(v)}
    return v

# ---------- schema ----------
QUEST = ['name','startedBy','finishedBy','requiredLevel','questLevel','requiredRaces','requiredClasses','objectivesText','triggerEnd','objectives','sourceItemId','preQuestGroup','preQuestSingle','childQuests','inGroupWith','exclusiveTo','zoneOrSort','requiredSkill','requiredMinRep','requiredMaxRep','requiredSourceItems','nextQuestInChain','questFlags','specialFlags','parentQuest','reputationReward','breadcrumbForQuestId','breadcrumbs','extraObjectives','requiredSpell','requiredSpecialization','requiredMaxLevel','availableUntilCompleted','availableStartingWith','requiredRanks','disabledByQuest']
NPC = ['name','minLevelHealth','maxLevelHealth','minLevel','maxLevel','rank','spawns','waypoints','zoneID','questStarts','questEnds','factionID','friendlyToFaction','subName','npcFlags']
ITEM = ['name','npcDrops','objectDrops','itemDrops','startQuest','questRewards','flags','foodType','itemLevel','requiredLevel','ammoType','class','subClass','vendors','relatedQuests','teachesSpell']
OBJECT = ['name','questStarts','questEnds','spawns','zoneID','factionID','waypoints']
SCHEMA = {'Quest': QUEST, 'Npc': NPC, 'Item': ITEM, 'Object': OBJECT}
TABLE_FIELDS = {  # which fields are 'table' typed (stored in separate keys, bit (idx-1) of p)
 'Quest': {2,3,8,9,10,12,13,14,15,16,18,19,20,21,26,28,29,35},
 'Npc': {7,8,10,11}, 'Item': {2,3,4,6,14,15}, 'Object': {2,3,4,7}}
FACTION = {0:'A',1:'H',2:'AH',3:None}

class QDB:
    def __init__(self, toc):
        self.toc = toc
        self.meta = {}
        with open(toc, encoding='utf-8') as f:
            for line in f:
                if line.startswith('## X-') and ': ' in line:
                    k, v = line[3:].rstrip('\n').split(': ', 1)
                    if k.startswith('X-l10n'): continue   # skip translations
                    self.meta[k] = v
        self.ids = {}
        for ent in SCHEMA:
            self.ids[ent] = set(cbor_decode(zlib.decompress(base64.b64decode(self._stored('X-%s-IDS' % ent)))))
        self._names = None

    def _stored(self, key):
        v = self.meta.get(key)
        if v is None: return None
        if v.startswith('~'):
            m = re.fullmatch(r'~(\d+)~', v)
            if m:
                return ''.join(self.meta[key + '-%d' % i] for i in range(1, int(m.group(1)) + 1))
        return v

    def get(self, ent, id_):
        if id_ not in self.ids[ent]: return None
        names = SCHEMA[ent]
        s = self._stored('X-%s-%d-S' % (ent, id_))
        row = _lua(cbor_decode(base64.b64decode(s))) if s else {}
        p = row.get('p', 0)
        out = {'id': id_}
        for idx, nm in enumerate(names, 1):
            if idx in TABLE_FIELDS[ent]:
                if (p >> (idx - 1)) & 1:
                    t = self._stored('X-%s-%d-%d' % (ent, id_, idx))
                    if t:
                        v = cbor_decode(base64.b64decode(t))
                        if nm in ('spawns', 'waypoints') : v = _lua(v)   # zone-keyed map; dense keys (e.g. area 1) arrive as a list
                        out[nm] = v
            else:
                if idx in row: out[nm] = row[idx]
        if ent == 'Npc':
            out.pop('minLevelHealth', None); out.pop('maxLevelHealth', None)
            if 'friendlyToFaction' in out and isinstance(out['friendlyToFaction'], int):
                out['friendlyToFaction'] = FACTION.get(out['friendlyToFaction'])
        return out

    def name_index(self, ent):
        """lowercase name -> [ids] (one pass over the S rows for that entity)."""
        if self._names is None: self._names = {}
        if ent not in self._names:
            d = {}
            for i in self.ids[ent]:
                s = self._stored('X-%s-%d-S' % (ent, i))
                if not s: continue
                r = _lua(cbor_decode(base64.b64decode(s)))
                n = r.get(1)
                if n: d.setdefault(n.lower(), []).append(i)
            self._names[ent] = d
        return self._names[ent]

    def find(self, ent, text):
        t = text.lower(); idx = self.name_index(ent)
        exact = idx.get(t, [])
        return sorted(exact) if exact else sorted(i for n, l in idx.items() if t in n for i in l)

# ---------- zones / enums ----------
def _lua_int_map(text):
    """parse '[123] = 456, -- comment' lines -> {123: (456, comment)}"""
    out = {}
    for m in re.finditer(r'^\s*\[(\d+)\]\s*=\s*(\d+),?\s*(?:--\s*(.*))?$', text, re.M):
        out[int(m.group(1))] = (int(m.group(2)), (m.group(3) or '').strip())
    return out

def _enum_block(path, table):
    t = open(path, encoding='utf-8').read()
    i = t.index(table + ' = {'); j = t.index('\n}', i)
    return {m.group(1): int(m.group(2)) for m in re.finditer(r'^\s*([A-Z0-9_]+)\s*=\s*(-?\d+)', t[i:j], re.M)}

class Zones:
    def __init__(self, addons):
        ADDONS = addons
        base = ADDONS + "/QuestieDB/support/Forever/Zones/"
        t = open(base + "areaIdToUiMapId.lua", encoding="utf-8").read()
        self.area2map = {}; self.area_name = {}; self.map_name = {}
        for k, (v, c) in _lua_int_map(t).items():
            self.area2map[k] = v
            if c and 'Referenced' not in c and 'Synthetic' not in c and 'retain' not in c and 'fail-safe' not in c:
                if '->' in c:
                    self.area_name[k] = c.split('->')[0].strip()
                else:
                    self.area_name[k] = c; self.map_name.setdefault(v, c)
        u = open(base + "uiMapIdToAreaId.lua", encoding="utf-8").read()
        self.map2area = {k: v for k, (v, c) in _lua_int_map(u).items()}
        e = ADDONS + "/QuestieDB/src/corrections/enum/"
        self.zone_enum = _enum_block(e + "zones.lua", "constants.zoneIDs")
        for n, i in self.zone_enum.items():
            self.area_name.setdefault(i, n.replace('_', ' ').title())
        self.sort_enum = _enum_block(e + "quests.lua", "constants.questSorts") if 'questSorts' in open(e+"quests.lua").read() else {}
        sub = open(base + "subZoneToParentZone.lua", encoding="utf-8").read()
        self.parent = {k: v for k, (v, c) in _lua_int_map(sub).items()}

    def info(self, area):
        parent = self.parent.get(area)
        m = self.area2map.get(area) or (self.area2map.get(parent) if parent else None)
        return {'areaId': area, 'name': self.area_name.get(area) or self.area_name.get(parent), 'uiMapId': m,
                'uiMapName': self.map_name.get(m)}

CLASS = {1:'Warrior',2:'Paladin',4:'Hunter',8:'Rogue',16:'Priest',32:'Death Knight',64:'Shaman',128:'Mage',256:'Warlock',512:'Monk',1024:'Druid'}
RACE = {1:'Human',2:'Orc',4:'Dwarf',8:'Night Elf',16:'Undead',32:'Tauren',64:'Gnome',128:'Troll',256:'Goblin',512:'Blood Elf',1024:'Draenei',2097152:'Worgen',4294967296:'Skyborne Alliance',8589934592:'Skyborne Horde'}
NPCFLAG = {1:'gossip',2:'quest',4:'vendor',8:'flight master',16:'trainer',32:'spirit healer',64:'spirit guide',128:'innkeeper',256:'banker',512:'petitioner',1024:'tabard',2048:'battlemaster',4096:'auctioneer',8192:'stable master',16384:'repair'}
def bits(mask, table): return [n for b, n in table.items() if mask & b]


CLASS = {1: 'Warrior', 2: 'Paladin', 4: 'Hunter', 8: 'Rogue', 16: 'Priest', 32: 'Death Knight', 64: 'Shaman', 128: 'Mage', 256: 'Warlock', 512: 'Monk', 1024: 'Druid'}
RACE = {1: 'Human', 2: 'Orc', 4: 'Dwarf', 8: 'Night Elf', 16: 'Undead', 32: 'Tauren', 64: 'Gnome', 128: 'Troll', 256: 'Goblin', 512: 'Blood Elf', 1024: 'Draenei', 2097152: 'Worgen', 4294967296: 'Skyborne Alliance', 8589934592: 'Skyborne Horde'}
NPCFLAG = {1: 'gossip', 2: 'quest', 4: 'vendor', 8: 'flight master', 16: 'trainer', 32: 'spirit healer', 64: 'spirit guide', 128: 'innkeeper', 256: 'banker', 512: 'petitioner', 1024: 'tabard', 2048: 'battlemaster', 4096: 'auctioneer', 8192: 'stable master', 16384: 'repair'}


def bits(mask, table):
    return [n for b, n in table.items() if mask & b]


def sort_names(addons):
    """questSort id -> name (negative ids are quest categories such as -61 Warlock, -666 Camping)."""
    p = addons + "/QuestieDB/src/corrections/enum/quests.lua"
    try:
        t = open(p, encoding="utf-8").read()
        return {v: k for k, v in _enum_block(p, "constants.sortKeys").items()} if "sortKeys" in t else {}
    except (OSError, ValueError):
        return {}
