-- Coach core: structured event capture + queue ("evj" messages), fight/death reconstruction, NPC cache.
-- Contract: docs/DESIGN-coach-events.md. Companion files (loaded after this one, see WoWFeed.toc):
--   CoachState.lua  state sections (ACTIONBARS, PET, TARGET, ITEMS, REWARDS, TRAINER, ...)
--   CoachRules.lua  rules engine, checklist auto-complete, coach inbox, /coach, waypoint HUD
-- Everything optional is wrapped in pcall or a feature check: an API this client lacks degrades to "unknown", never raises.
-- Level and XP only ever appear in event/feed data for the coach, never on the overlay. Display and advice only: nothing in
-- these files equips, previews, uses or casts anything.
local floor, max, min = math.floor, math.max, math.min
local strfmt, tconcat = string.format, table.concat
local J = WoWFeedJSON
local API = WoWFeed_API or {}

local C = {}
WoWFeedCoach = C

local QUEUE_CAP = 200          -- design: oldest events are dropped beyond this
local EVENT_MAX = 1800         -- one encoded event is shrunk below this (long lists are halved)
local RESEND_AFTER = 2.5       -- each event is sent once more at least this many seconds after its first send (>= 2 s)
local RESEND_WINDOW = 30       -- ... but only while the strip has spare capacity; an event unsent after this long is dropped
local ATT_CAP, DOT_CAP, NPC_CAP, NPCSEEN_CAP = 40, 300, 400, 800   -- bounded tables

---------------------------------------------------------------- state
local S = {
	seq = 0, q = {}, dropped = 0, inflight = nil, errors = {},
	rs = {}, resent = 0, resendDropped = 0,       -- rs: events sent once, waiting for their second (redundant) send
	loading = true, loginAt = nil, logoutAt = nil, lastEnter = nil, firstEnter = true, started = false,
	pull = nil, fight = nil, fights = {}, deaths = {}, pendingDeath = nil,
	att = {}, attN = 0, recent = {},
	dotDur = {}, dotUntil = {},
	npcs = {}, npcOrder = {}, npcSeen = {},
	quests = { ready = {}, titles = {}, scanned = false, dirty = false, last = -99 },
	windows = {}, lastZone = nil, afk = false, ticks = 0,
	bags = { full = nil }, dura = { crit = nil },
	spellDedupe = {}, dotN = 0,
}
C.S = S

---------------------------------------------------------------- helpers
local U = {}
C.U = U

-- true when the game hides this value. issecretvalue is 12.0+; canaccessvalue is the fallback; a client with neither has no secrets.
local function secret(v)
	if issecretvalue ~= nil then return issecretvalue(v) == true end
	if canaccessvalue ~= nil then
		local ok, r = pcall(canaccessvalue, v)
		return ok and r == false
	end
	return false
end
U.secret = secret

-- pcall that returns all the results, or nothing on error
local function tryResult(ok, ...)
	if not ok then return nil end
	return ...
end
local function try(f, ...)
	if type(f) ~= "function" then return nil end
	return tryResult(pcall(f, ...))
end
U.try = try

-- a usable string (never a secret value), numbers are converted
function U.str(v)
	if v == nil or secret(v) then return nil end
	local t = type(v)
	if t == "string" then return v end
	if t == "number" then return tostring(v) end
	return nil
end
-- a usable number (never a secret value)
function U.num(v)
	if v == nil or secret(v) then return nil end
	return tonumber(v)
end
function U.bool(v)
	if v == nil or secret(v) then return nil end
	return v and true or false
end
function U.trunc(s, n)
	if type(s) ~= "string" then return s end
	if #s > n then return s:sub(1, n) end
	return s
end
function U.round1(v) if not v then return nil end return floor(v * 10 + 0.5) / 10 end
function U.now() return GetTime and GetTime() or 0 end
function U.epoch() return (time and time()) or 0 end
function U.inCombat() return (UnitAffectingCombat and U.bool(UnitAffectingCombat("player"))) or false end
function U.unitName(u) return U.str(try(UnitName, u)) end
function U.unitLevel(u) return U.num(try(UnitLevel, u)) end
function U.guid(u) return U.str(try(UnitGUID, u)) end
function U.exists(u) return U.bool(try(UnitExists, u)) or false end
function U.lower(s) return type(s) == "string" and s:lower() or nil end

-- "Rank 3" style subtext -> 3
function U.rankNum(sub)
	if type(sub) ~= "string" then return nil end
	return tonumber(sub:match("(%d+)"))
end

function U.spellName(id)
	id = U.num(id)
	if not id then return nil end
	if C_Spell and C_Spell.GetSpellName then local n = U.str(try(C_Spell.GetSpellName, id)); if n then return n end end
	if C_Spell and C_Spell.GetSpellInfo then
		local i = try(C_Spell.GetSpellInfo, id)
		if type(i) == "table" and U.str(i.name) then return i.name end
	end
	if GetSpellInfo then local n = try(GetSpellInfo, id); if type(n) == "string" and not secret(n) then return n end end
	return nil
end
-- rank text ("Rank 2") and number for a spell id; nil when the client has no such API or the spell has no rank
function U.spellRank(id)
	id = U.num(id)
	if not id then return nil, nil end
	local sub
	if C_Spell and C_Spell.GetSpellSubtext then sub = U.str(try(C_Spell.GetSpellSubtext, id)) end
	if (not sub or sub == "") and GetSpellSubtext then sub = U.str(try(GetSpellSubtext, id)) end
	if (not sub or sub == "") and GetSpellInfo then
		local _, r = try(GetSpellInfo, id)   -- Classic-style GetSpellInfo returns the rank as its second value
		if type(r) == "string" and not secret(r) then sub = r end
	end
	if not sub or sub == "" then
		-- Forever has no C_Spell.GetSpellSubtext / Classic-style GetSpellInfo rank: the spellbook entry carries the subtext
		local b = U.bookRank(id)
		if b then return b, U.rankNum(b) end
		return nil, nil
	end
	return sub, U.rankNum(sub)
end

-- spell id -> spellbook subtext ("Rank 2"), rebuilt at most every 30 s (and after the spellbook changed)
local rankCache = { t = -1e9, byId = {} }
function U.invalidateRanks() rankCache.t = -1e9 end
function U.bookRank(id)
	local now = U.now()
	if now - rankCache.t > 30 and API.spellbook then
		rankCache.t = now
		local byId = {}
		local ok, tabs = pcall(API.spellbook)
		if ok and type(tabs) == "table" then
			for _, tab in ipairs(tabs) do
				for _, it in ipairs(tab.items or {}) do
					local sid, sub = U.num(it.id), U.str(it.sub)
					if sid and sub and sub:find("%d") then byId[sid] = sub end
				end
			end
		end
		rankCache.byId = byId
	end
	return rankCache.byId[id]
end

function U.zone()
	local z = U.str(try(GetRealZoneText)) or U.str(try(GetZoneText)) or ""
	local sz = U.str(try(GetSubZoneText)) or ""
	return z, sz
end

-- mapID, x, y of the player (x, y as fractions 0..1); nil parts when unknown. When the map coordinates have to be read from
-- another addon's text (see WoWFeed.lua coordText) that can mean walking the frame list, so results are cached: 0.4 s when
-- known, 5 s when unknown. fresh = true skips the cache.
local posCache = { t = -1e9 }
function U.posFrac(fresh)
	local now = U.now()
	if not fresh and now - posCache.t < (posCache.ok and 0.4 or 5) then return posCache.mid, posCache.x, posCache.y end
	posCache.t = now
	posCache.mid, posCache.x, posCache.y, posCache.ok = nil, nil, nil, false
	if API.position then
		local ok, mid, x, y = pcall(API.position)
		if ok then
			posCache.mid, posCache.x, posCache.y = U.num(mid), U.num(x), U.num(y)
			posCache.ok = posCache.x ~= nil and posCache.y ~= nil
		end
	end
	return posCache.mid, posCache.x, posCache.y
end
-- mapID, x, y in percent with one decimal (the form events use)
function U.pos(fresh)
	local mid, x, y = U.posFrac(fresh)
	return mid, x and U.round1(x * 100) or nil, y and U.round1(y * 100) or nil
end

-- item facts shared by loot, equip, reward and bag scans. Everything optional: "unknown" is nil here.
local BIND = { [0] = "none", "pickup", "equip", "use", "quest" }
function U.itemInfo(link)
	local o = {}
	if type(link) ~= "string" or secret(link) then return o end
	o.link = link
	local fn = (C_Item and C_Item.GetItemInfo) or GetItemInfo
	local name, _, quality, ilvl, minLevel, itype, isub, _, equipLoc, _, price, classID, subclassID, bindType = try(fn, link)
	o.name = U.str(name) or U.str(link:match("%[(.-)%]"))
	o.id = tonumber(link:match("item:(%d+)"))
	o.cached = name ~= nil
	o.quality = U.num(quality); o.ilvl = U.num(ilvl); o.minLevel = U.num(minLevel)
	o.itype = U.str(itype); o.isub = U.str(isub); o.equipLoc = U.str(equipLoc)
	o.price = U.num(price); o.classID = U.num(classID)
	o.bindType = U.num(bindType); o.bind = o.bindType and BIND[o.bindType] or nil
	if o.equipLoc == "" then o.equipLoc = nil end
	if o.classID == 4 or o.itype == "Armor" or o.itype == "Weapon" then o.armor = o.isub end
	return o
end
-- stat table with the ITEM_MOD_ prefix and _SHORT suffix stripped: { STAMINA = 5, ... }; nil when not cached
function U.itemStats(link)
	if type(link) ~= "string" then return nil end
	local fn = (C_Item and C_Item.GetItemStats) or GetItemStats
	local t = try(fn, link)
	if type(t) ~= "table" then return nil end
	local out, n = {}, 0
	for k, v in pairs(t) do
		if type(k) == "string" and type(v) == "number" and not secret(v) then
			local name = k:gsub("^ITEM_MOD_", ""):gsub("_SHORT$", ""):gsub("_NAME$", "")
			if name == "RESISTANCE0" then name = "ARMOR" end          -- the game files armor under resistance 0
			out[name] = v; n = n + 1
		end
	end
	if n == 0 then return nil end
	return out
end
-- soulbound status of a bag slot or equipment slot. true/false, nil when unknown.
function U.isBound(bag, slot, equipSlot)
	if not (C_Item and C_Item.IsBound and ItemLocation) then return nil end
	local loc
	if equipSlot then loc = try(ItemLocation.CreateFromEquipmentSlot, ItemLocation, equipSlot)
	else loc = try(ItemLocation.CreateFromBagAndSlot, ItemLocation, bag, slot) end
	if not loc then return nil end
	return U.bool(try(C_Item.IsBound, loc))
end

-- player/pet/target auras: { { name, id, dur, exp, rem, count, src, dispel } } or nil when the game hides auras (secret)
function U.auras(unit, filter)
	if C_Secrets and C_Secrets.ShouldAurasBeSecret then
		local r = try(C_Secrets.ShouldAurasBeSecret)
		if r == true and U.inCombat() then return nil end
	end
	local out = { hidden = 0 }
	local now = U.now()
	for i = 1, 80 do
		local name, id, dur, exp, count, src, dispel
		if C_UnitAuras and C_UnitAuras.GetAuraDataByIndex then
			local a = try(C_UnitAuras.GetAuraDataByIndex, unit, i, filter)
			if not a then break end
			-- the whole aura record can be a secret (forbidden) table: indexing it raises, and then it counts as hidden
			local okf = pcall(function() name, id, dur, exp, count, src, dispel = a.name, a.spellId, a.duration, a.expirationTime, a.applications, a.sourceUnit, a.dispelName end)
			if not okf then name = nil end
		elseif UnitAura then
			local n, _, c, d, du, e, s, _, _, sid = try(UnitAura, unit, i, filter)
			if not n then break end
			name, id, dur, exp, count, src, dispel = n, sid, du, e, c, s, d
		else
			break
		end
		local nm = U.str(name)
		if not nm then out.hidden = out.hidden + 1
		else
			local e, d = U.num(exp), U.num(dur)
			out[#out + 1] = { name = nm, id = U.num(id), dur = d, exp = e, rem = (e and e > 0) and (e - now) or nil, count = U.num(count),
				src = U.str(src), dispel = U.str(dispel) }
		end
	end
	return out
end

---------------------------------------------------------------- event queue
local function shrink(data)
	-- halve the longest list in the data until the event fits; mark it
	for _ = 1, 10 do
		local bestK, bestN, holder
		local function look(tbl)
			for k, v in pairs(tbl) do
				if type(v) == "table" and #v > (bestN or 1) then bestK, bestN, holder = k, #v, tbl end
			end
		end
		look(data)
		for _, v in pairs(data) do if type(v) == "table" and #v == 0 then look(v) end end
		if not bestK then return false end
		local half = {}
		for i = 1, floor(bestN / 2) do half[i] = holder[bestK][i] end
		holder[bestK] = J.array(half)
		holder[bestK .. "_truncated"] = true
		local s = J.encode(data)
		if s and #s < EVENT_MAX - 80 then return true end
	end
	return false
end

-- the addon's boot id (the same number UI.kv() reports as uiboot): the bridge dedupes events on (boot, seq)
local function bootId()
	local ui = WoWFeedUI
	return (ui and ui.state and ui.state.bootId) or S.bootId
end
S.bootId = (time and time()) or 0

local function encodeEvent(ev)
	local function build(data)
		local d = J.encode(data)
		if not d then return nil end
		return '{"seq":' .. ev.seq .. ',"boot":' .. bootId() .. ',"t":' .. ev.t .. ',"type":' .. J.encodeString(ev.type) .. ',"data":' .. d .. '}'
	end
	local s = build(ev.data)
	if s and #s <= EVENT_MAX then return s end
	if type(ev.data) == "table" then shrink(ev.data); s = build(ev.data) end
	if s and #s <= EVENT_MAX then return s end
	return build({ truncated = true })
end

-- Queue a structured event for the strip. Returns the envelope.
function C.emit(etype, data)
	S.seq = S.seq + 1
	local ev = { seq = S.seq, t = U.epoch(), type = etype, data = data or {} }
	local s = encodeEvent(ev)
	if not s then S.dropped = S.dropped + 1; return ev end
	S.q[#S.q + 1] = { seq = ev.seq, s = s, type = etype }
	while #S.q > QUEUE_CAP do table.remove(S.q, 1); S.dropped = S.dropped + 1 end
	S.lastEvent = ev
	return ev
end

-- An event that was sent once waits here for its redundant second send (a strip frame can be missed; the bridge dedupes on
-- (boot, seq), so a repeat is harmless). Bounded: at most QUEUE_CAP entries, and an entry that never got spare capacity expires.
local function queueResend(e)
	if e.again then return end
	local now = U.now()
	S.rs[#S.rs + 1] = { seq = e.seq, s = e.s, type = e.type, again = true, due = now + RESEND_AFTER, expire = now + RESEND_WINDOW }
	while #S.rs > QUEUE_CAP do table.remove(S.rs, 1); S.resendDropped = S.resendDropped + 1 end
end

-- Called by WoWFeed.lua's nextMessage (cap = max payload bytes for one strip message; spare = no section is waiting for the
-- strip, so a repeat costs nothing). Returns payload, id, part, parts. A batch is a JSON array; an event too large for one
-- message is split over parts that share an id (the output is pure ASCII, so any byte offset is a safe cut).
function C.nextEvj(cap, spare)
	cap = max(cap or 100, 40)
	if S.inflight then
		local f = S.inflight
		f.i = f.i + 1
		local chunk = f.chunks[f.i]
		if f.i >= #f.chunks then S.inflight = nil end
		return chunk, f.id, f.i, #f.chunks
	end
	local list = S.q
	if #list == 0 then
		-- repeats: only with spare strip capacity, and only entries that are due
		local now = U.now()
		while #S.rs > 0 and now > S.rs[1].expire do table.remove(S.rs, 1); S.resendDropped = S.resendDropped + 1 end
		if not spare or #S.rs == 0 or now < S.rs[1].due then return nil end
		list = S.rs
	end
	local parts, size, lastSeq = {}, 2, nil
	while #list > 0 do
		local e = list[1]
		local add = #e.s + (#parts > 0 and 1 or 0)
		if size + add > cap then break end
		if list == S.rs and #parts > 0 and U.now() < e.due then break end
		table.remove(list, 1)
		parts[#parts + 1] = e.s; size = size + add; lastSeq = e.seq
		if list == S.rs then S.resent = S.resent + 1 else queueResend(e) end
	end
	if #parts > 0 then return "[" .. tconcat(parts, ",") .. "]", lastSeq, 1, 1 end
	local e = table.remove(list, 1)
	if list == S.rs then S.resent = S.resent + 1 else queueResend(e) end
	local full = "[" .. e.s .. "]"
	local chunks = {}
	for i = 1, #full, cap do chunks[#chunks + 1] = full:sub(i, i + cap - 1) end
	if #chunks > 1 then S.inflight = { id = e.seq, chunks = chunks, i = 1 } end
	return chunks[1], e.seq, 1, #chunks
end

if API.setEvjProvider then API.setEvjProvider(C.nextEvj) end

-- Types that never reach the rules engine (they are its own output or bookkeeping)
local NO_RULES = { rule_fired = true, ack = true, coach_question = true, npc_seen = true, fight = true, buff_missing = true, logout = false }
-- window_open maps to the rule names merchant_show / trainer_show / quest_reward / loot_opened
local WINDOW_RULE = { merchant = "merchant_show", trainer = "trainer_show", quest_reward = "quest_reward", loot = "loot_opened" }

-- Queue the event (unless opts.quiet) and let the rules see it
function C.event(etype, data, opts)
	data = data or {}
	local ev
	if not (opts and opts.quiet) then ev = C.emit(etype, data) end
	if not NO_RULES[etype] and C.rules and C.rules.onEvent then
		local rname = etype
		if etype == "window_open" then rname = WINDOW_RULE[data.window] or etype end
		local ok, err = pcall(C.rules.onEvent, rname, data)
		if not ok then C.err("rules", err) end
	end
	return ev
end

function C.err(where, err)
	S.errors[#S.errors + 1] = tostring(where) .. ": " .. tostring(err):sub(1, 140)
	if #S.errors > 5 then table.remove(S.errors, 1) end
end

---------------------------------------------------------------- NPC cache
local function npcIdFromGuid(guid)
	if type(guid) ~= "string" then return nil end
	local kind, _, _, _, _, id = guid:match("^(%a+)%-(%d+)%-(%d+)%-(%d+)%-(%d+)%-(%d+)")
	if kind == "Creature" or kind == "Vehicle" then return tonumber(id) end
	return nil
end
U.npcIdFromGuid = npcIdFromGuid

-- unit: "npc" (open interaction window) or "target". kind: vendor|trainer|quest|gossip|target
function C.noteNpc(unit, kind)
	local name = U.unitName(unit)
	local id = npcIdFromGuid(U.guid(unit))
	if not name or not id then return nil end
	local mid, x, y = U.pos()
	local key = tostring(id)
	local rec = S.npcs[key]
	if not rec then
		rec = { name = name, id = id, count = 0, kinds = {} }
		S.npcs[key] = rec
		S.npcOrder[#S.npcOrder + 1] = key
		if #S.npcOrder > NPC_CAP then local old = table.remove(S.npcOrder, 1); S.npcs[old] = nil end
	end
	rec.count = rec.count + 1
	rec.map, rec.x, rec.y, rec.kind, rec.last = mid, x, y, kind, U.epoch()
	rec.kinds[kind] = true
	S.dirtyNpcs = true
	local sk = key .. "/" .. kind
	if not S.npcSeen[sk] then
		S.npcSeen[sk] = true
		S.npcSeenN = (S.npcSeenN or 0) + 1
		if S.npcSeenN > NPCSEEN_CAP then S.npcSeen, S.npcSeenN = { [sk] = true }, 1 end     -- bounded: at worst an old NPC is announced twice
		C.event("npc_seen", { name = name, id = id, map = mid, x = x, y = y, kind = kind })
	end
	return { name = name, id = id, map = mid, x = x, y = y }
end

---------------------------------------------------------------- fights and deaths
local ELITE = { elite = true, rareelite = true, worldboss = true }

local function unitFacts(u)
	local cls = U.str(try(UnitClassification, u))
	return { name = U.unitName(u), level = U.unitLevel(u), classification = cls, elite = cls and ELITE[cls] or nil }
end

-- Remember hostile units that currently have the player as their target (nameplate units). The exact killer is not
-- available on this client, so a death reports the most recently seen of these.
function C.sampleAttackers()
	local now = U.now()
	local units = {}
	local plates = C_NamePlate and try(C_NamePlate.GetNamePlates)
	if type(plates) == "table" then
		for _, p in ipairs(plates) do
			local u = p.namePlateUnitToken or p.unitToken or (p.UnitFrame and p.UnitFrame.unit)
			if type(u) == "string" and not secret(u) then units[#units + 1] = u end
		end
	end
	units[#units + 1] = "target"
	for _, u in ipairs(units) do
		if U.exists(u) and U.bool(try(UnitCanAttack, "player", u)) and not U.bool(try(UnitIsDead, u)) then
			local same = try(UnitIsUnit, u .. "target", "player")
			if not secret(same) and same == true then
				local f = unitFacts(u)
				local key = U.guid(u) or ((f.name or "?") .. "/" .. u)
				S.attN = S.attN + 1
				f.t = now; f.n = S.attN
				S.att[key] = f
			end
		end
	end
	local n = 0
	for k, f in pairs(S.att) do
		if now - f.t > 20 then S.att[k] = nil else n = n + 1 end
	end
	if n > ATT_CAP then S.att = {} end      -- a crowd of unique units: start over rather than grow
end

local function recentAttackers()
	local now, list = U.now(), {}
	for _, f in pairs(S.att) do if now - f.t <= 15 then list[#list + 1] = f end end
	table.sort(list, function(a, b) if a.n ~= b.n then return a.n > b.n end return (a.level or 0) > (b.level or 0) end)
	return list
end

local function snapshotVitals()
	if U.inCombat() or U.bool(try(UnitIsDeadOrGhost, "player")) then return end
	local hp, hpm = U.num(try(UnitHealth, "player")), U.num(try(UnitHealthMax, "player"))
	local mp, mpm = U.num(try(UnitPower, "player")), U.num(try(UnitPowerMax, "player"))
	if hp and hpm then S.pull = { hp = hp, hpmax = hpm, mana = mp, manamax = mpm, t = U.now() } end
end
C.snapshotVitals = snapshotVitals

local function lowestDurability()
	local lowest, slot, tot, totm = nil, nil, 0, 0
	for i = 1, 19 do
		local cur, mx = try(GetInventoryItemDurability, i)
		cur, mx = U.num(cur), U.num(mx)
		if cur and mx and mx > 0 then
			tot = tot + cur; totm = totm + mx
			local p = cur / mx * 100
			if not lowest or p < lowest then lowest, slot = p, i end
		end
	end
	return lowest, slot, (totm > 0) and (tot / totm * 100) or nil
end
C.lowestDurability = lowestDurability

local SLOT_NAMES = { "Head", "Neck", "Shoulder", "Shirt", "Chest", "Waist", "Legs", "Feet", "Wrist", "Hands", "Finger1", "Finger2", "Trinket1", "Trinket2", "Back", "MainHand", "SecondaryHand", "Ranged", "Tabard" }
C.SLOT_NAMES = SLOT_NAMES

function C.deathsInSubzone(zone, sub, minutes)
	local cutoff = U.epoch() - (minutes or 10) * 60
	local n = 0
	for _, d in ipairs(S.deaths) do
		if d.t >= cutoff and d.zone == zone and d.subzone == sub then n = n + 1 end
	end
	return n
end

-- Death recap (C_DeathRecap, tagged forever + 1.60.1): who hit the player last and for how much. Fully guarded: the API may be
-- missing, may raise, may return nothing yet, and any field may be secret. Returns nil, or
-- { source = name or nil, hits = { { spell, amount, source } ... up to 3, newest first } }.
-- Order: events carry a timestamp, so they are sorted newest first when every timestamp is readable; otherwise the array order
-- the game gave is kept. The killing blow is the event flagged `tombstone`, else the first one in that order.
local RECAP_MAX_EVENTS = 40
local function readRecap()
	local D = C_DeathRecap
	if type(D) ~= "table" or type(D.GetRecapEvents) ~= "function" then return nil end
	if type(D.HasRecapEvents) == "function" then
		local ok, has = pcall(D.HasRecapEvents)
		if ok and not secret(has) and has == false then return nil end
	end
	local ok, evs = pcall(D.GetRecapEvents)
	if not ok or type(evs) ~= "table" then return nil end
	local rows = {}
	local ok2 = pcall(function()
		for i = 1, min(#evs, RECAP_MAX_EVENTS) do
			local e = evs[i]
			if type(e) == "table" then
				local di = type(e.DamageInfo) == "table" and e.DamageInfo or nil
				local ev = U.str(e.event)
				local spell = U.str(e.spellName)
				if not spell and ev == "SWING_DAMAGE" then spell = "Melee" end
				if not spell and di then spell = U.str(di.spellName) end
				local amount = U.num(e.amount)
				local r = { i = i, ts = U.num(e.timestamp), spell = spell, amount = amount and floor(amount) or nil,
					source = (not secret(e.hideCaster) and e.hideCaster ~= true) and (U.str(e.sourceName) or (di and U.str(di.caster))) or nil,
					tomb = U.bool(e.tombstone) == true }
				if r.spell or r.amount or r.source then rows[#rows + 1] = r end      -- an event with nothing readable says nothing
			end
		end
	end)
	if not ok2 or #rows == 0 then return nil end
	local allTs = true
	for _, r in ipairs(rows) do if not r.ts then allTs = false end end
	if allTs then table.sort(rows, function(a, b) if a.ts ~= b.ts then return a.ts > b.ts end return a.i < b.i end) end
	local killer
	for _, r in ipairs(rows) do if r.tomb and r.source then killer = r.source; break end end
	if not killer then for _, r in ipairs(rows) do if r.source then killer = r.source; break end end end
	local hits = {}
	for i = 1, min(#rows, 3) do
		local r = rows[i]
		hits[i] = { spell = r.spell, amount = r.amount, source = r.source }
	end
	return { source = killer, hits = hits }
end
U.deathRecap = function() local ok, r = pcall(readRecap); if ok then return r end return nil end

local function applyRecap(rec, recap)
	if not recap then return false end
	rec.last_hits = recap.hits
	if recap.source then
		rec.killer.name = recap.source
		rec.killer.source = "recap"
	end
	return true
end

local function onDeath()
	pcall(C.sampleAttackers)
	local list = recentAttackers()
	local killer = list[1]
	if not killer and U.exists("target") and U.bool(try(UnitCanAttack, "player", "target")) then
		local same = try(UnitIsUnit, "targettarget", "player")
		if not secret(same) and same == true then killer = unitFacts("target"); list = { killer } end
	end
	local k
	if killer then k = { name = killer.name or "unknown", level = killer.level, classification = killer.classification or "unknown", elite = killer.elite or false, source = "nameplate" }
	else k = { name = "unknown", classification = "unknown", source = "unknown" } end
	local al = {}
	for i = 1, min(#list, 6) do al[i] = { name = list[i].name or "unknown", level = list[i].level, classification = list[i].classification or "unknown" } end
	local mid, x, y = U.pos(true)
	local zone, sub = U.zone()
	local pull = S.pull or {}
	local rec = { t = U.epoch(), zone = zone, subzone = sub, killer = k, attackers = #list, map = mid, x = x, y = y }
	-- the recap names the real killer when it is readable; the nameplate guess stays as the fallback (and keeps level / class)
	local recap = U.deathRecap()
	if recap and recap.source and killer and killer.name ~= recap.source then k.level, k.classification, k.elite = nil, "unknown", false end
	applyRecap(rec, recap)
	S.deaths[#S.deaths + 1] = rec
	while #S.deaths > 30 do table.remove(S.deaths, 1) end
	rec.n10 = C.deathsInSubzone(zone, sub, 10)
	rec.hp_at_pull, rec.mana_at_pull = pull.hp, pull.mana
	S.pendingDeath = rec
	if S.fight then S.fight.died = true end
	S.dirtyDeaths = true
	C.event("death", { killer = k, attackers = #list, attackers_list = J.array(al), last_hits = rec.last_hits and J.array(rec.last_hits) or J.array({}),
		hp_at_pull = pull.hp, hpmax = pull.hpmax, mana_at_pull = pull.mana,
		manamax = pull.manamax, map = mid, x = x, y = y, zone = zone, subzone = sub, deaths_in_subzone_10m = rec.n10 })
	-- the recap is sometimes not ready the instant PLAYER_DEAD fires: look once more and enrich the record (the DEATHS section)
	if not recap and C_DeathRecap and C_Timer and C_Timer.After then
		C_Timer.After(1.5, function()
			if rec.last_hits then return end
			if applyRecap(rec, U.deathRecap()) then S.dirtyDeaths = true end
		end)
	end
end

local function newFight()
	S.fight = { start = U.epoch(), t0 = U.now(), casts = {}, dups = {}, taken = 0, dealt = 0, mana = 0, kills = 0, died = false, castN = 0 }
	-- opening casts made just before the combat flag came up (the pull spell)
	local f = S.fight
	for _, r in ipairs(S.recent) do
		if f.t0 - r.at <= 3 and r.hostile then
			f.castN = f.castN + 1
			f.casts[#f.casts + 1] = { t = 0, spell = r.spell, name = r.name, rank = r.rank, target = r.target, wasted = r.wasted or nil }
			f.mana = f.mana + (r.mana or 0)
			if r.wasted then f.dups[#f.dups + 1] = { spell = r.spell, name = r.name, target = r.target, at = 0 } end
		end
	end
	S.recent = {}
end

local function finishFight()
	local f = S.fight
	S.fight = nil
	if not f then return end
	local dur = U.now() - f.t0
	local targetDead = U.exists("target") and U.bool(try(UnitIsDead, "target"))
	local result
	if f.died then result = "died" elseif f.kills > 0 or targetDead then result = "won" else result = "fled" end
	if f.castN == 0 and f.taken == 0 and f.dealt == 0 and f.kills == 0 and not f.died then return end
	local casts = {}
	for i = 1, min(#f.casts, 20) do casts[i] = f.casts[i] end
	local rec = { start = f.start, ["end"] = U.epoch(), duration = U.round1(dur), result = result, casts = casts, dups = f.dups, taken = f.taken,
		dealt = f.dealt, mana = floor(f.mana), kills = f.kills, castN = f.castN }
	S.fights[#S.fights + 1] = rec
	while #S.fights > 10 do table.remove(S.fights, 1) end
	S.dirtyFights = true
	local dd = {}
	for i = 1, min(#f.dups, 10) do dd[i] = f.dups[i] end
	C.event("fight", { start = f.start, ["end"] = rec["end"], duration = rec.duration, result = result, casts = J.array(casts), duplicate_dots = J.array(dd),
		damage_taken = f.taken, damage_dealt = f.dealt, mana_spent_est = rec.mana, kills = f.kills })
end
C.finishFight = finishFight

local function powerCost(spellID)
	local pt = U.num(try(UnitPowerType, "player")) or 0
	local costs
	if C_Spell and C_Spell.GetSpellPowerCost then costs = try(C_Spell.GetSpellPowerCost, spellID)
	elseif GetSpellPowerCost then costs = try(GetSpellPowerCost, spellID) end
	local sum = 0
	if type(costs) == "table" then
		for _, c in ipairs(costs) do
			local ct, cost = U.num(c.type), U.num(c.cost)
			if cost and (ct == nil or ct == pt) then sum = sum + cost end
		end
	end
	return sum
end

-- aura check shortly after a cast: learn how long this spell's debuff lasts on the target (for duplicate DoT detection)
local function learnDot(spellID, lname, tkey, guid)
	if guid and U.guid("target") ~= guid then return end
	local list = U.auras("target", "HARMFUL|PLAYER")
	local now = U.now()
	if list then
		for _, a in ipairs(list) do
			if (a.id == spellID or U.lower(a.name) == lname) and a.dur and a.dur > 0 and a.exp and a.exp > now then
				S.dotDur[lname] = a.dur; S.dotUntil[tkey] = a.exp
				return
			end
		end
		S.dotUntil[tkey] = nil     -- readable and not there: not a debuff spell
		return
	end
	if S.dotDur[lname] then S.dotUntil[tkey] = now + S.dotDur[lname] end   -- auras hidden: use the duration learned earlier
end

local function onCast(spellID)
	spellID = U.num(spellID)
	if not spellID then return end
	local now = U.now()
	local name = U.spellName(spellID) or ("spell " .. spellID)
	local _, rank = U.spellRank(spellID)
	local hostile = U.exists("target") and U.bool(try(UnitCanAttack, "player", "target")) or false
	local target = hostile and U.unitName("target") or nil
	local guid = hostile and U.guid("target") or nil
	local lname = name:lower()
	local tkey = (guid or target or "?") .. "|" .. lname
	local wasted = false
	if hostile and S.dotUntil[tkey] and now < S.dotUntil[tkey] - 0.3 then wasted = true end
	local cost = powerCost(spellID)
	local inFight = S.fight ~= nil
	if not inFight and U.inCombat() then newFight(); inFight = true end
	if hostile then
		S.dotN = S.dotN + 1
		if S.dotN > DOT_CAP then S.dotUntil, S.dotN = {}, 0 end     -- bounded: expired entries are the only ones that matter
		C_Timer.After(0.3, function() pcall(learnDot, spellID, lname, tkey, guid) end)
	end
	if inFight then
		local f = S.fight
		local at = U.round1(now - f.t0)
		f.castN = f.castN + 1
		f.mana = f.mana + cost
		f.casts[#f.casts + 1] = { t = at, spell = spellID, name = name, rank = rank, target = target, wasted = wasted or nil }
		if #f.casts > 60 then table.remove(f.casts, 1) end
		if wasted then f.dups[#f.dups + 1] = { spell = spellID, name = name, target = target, at = at } end
	else
		S.recent[#S.recent + 1] = { at = now, spell = spellID, name = name, rank = rank, target = target, hostile = hostile, wasted = wasted, mana = cost }
		if #S.recent > 6 then table.remove(S.recent, 1) end
	end
end

---------------------------------------------------------------- quests
local function questTitle(qid)
	if C_QuestLog and C_QuestLog.GetTitleForQuestID then local t = U.str(try(C_QuestLog.GetTitleForQuestID, qid)); if t then return t end end
	return S.quests.titles[qid]
end

function C.scanQuests()
	local qs = S.quests
	qs.last = U.now(); qs.dirty = false
	local ready, titles = {}, {}
	local n = 0
	if C_QuestLog and C_QuestLog.GetNumQuestLogEntries and C_QuestLog.GetInfo then
		for i = 1, (U.num(try(C_QuestLog.GetNumQuestLogEntries)) or 0) do
			local q = try(C_QuestLog.GetInfo, i)
			if type(q) == "table" and not q.isHeader and U.num(q.questID) then
				n = n + 1
				titles[q.questID] = U.str(q.title)
				if C_QuestLog.IsComplete and U.bool(try(C_QuestLog.IsComplete, q.questID)) then ready[q.questID] = true end
			end
		end
	elseif GetNumQuestLogEntries then
		for i = 1, (U.num(try(GetNumQuestLogEntries)) or 0) do
			local title, _, _, hdr, _, complete, _, qid = try(GetQuestLogTitle, i)
			if not hdr and U.num(qid) then
				n = n + 1; titles[qid] = U.str(title)
				if complete == 1 or complete == true then ready[qid] = true end
			end
		end
	end
	if qs.scanned then
		for qid in pairs(ready) do
			if not qs.ready[qid] then
				C.event("quest_complete", { quest = qid, title = titles[qid] })
			end
		end
	end
	qs.ready, qs.titles, qs.scanned, qs.count = ready, titles, true, n
end

---------------------------------------------------------------- loot patterns
local function patternFromFormat(fmt)
	if type(fmt) ~= "string" then return nil end
	local p = fmt:gsub("([%^%$%(%)%.%[%]%*%+%-%?])", "%%%1")
	p = p:gsub("%%s", "(.+)"):gsub("%%d", "(%%d+)")
	return "^" .. p .. "$"
end
local LOOT_PATTERNS
local function lootPatterns()
	if LOOT_PATTERNS then return LOOT_PATTERNS end
	LOOT_PATTERNS = {}
	-- multiple-stack formats first (they end in "x%d"), then single items
	for _, g in ipairs({ "LOOT_ITEM_SELF_MULTIPLE", "LOOT_ITEM_PUSHED_SELF_MULTIPLE", "LOOT_ITEM_CREATED_SELF_MULTIPLE", "LOOT_ITEM_SELF", "LOOT_ITEM_PUSHED_SELF", "LOOT_ITEM_CREATED_SELF" }) do
		local f = _G[g]
		local p = patternFromFormat(f)
		if p then LOOT_PATTERNS[#LOOT_PATTERNS + 1] = { p = p, multi = g:find("MULTIPLE") ~= nil } end
	end
	return LOOT_PATTERNS
end
C.lootPatterns = lootPatterns

local function parseOwnLoot(msg)
	if type(msg) ~= "string" or secret(msg) then return nil end
	for _, e in ipairs(lootPatterns()) do
		local a, b = msg:match(e.p)
		if a then return a, e.multi and tonumber(b) or 1 end
	end
	return nil
end
C.parseOwnLoot = parseOwnLoot

---------------------------------------------------------------- handlers
local H = {}
C.H = H

H.PLAYER_LOGIN = function() C.start() end

H.PLAYER_ENTERING_WORLD = function(isInitialLogin, isReloadingUi)
	S.loading = false
	S.lastEnter = U.epoch()
	local zone, sub = U.zone()
	S.lastZone = zone .. "|" .. sub
	if S.firstEnter then
		S.firstEnter = false
		S.loginAt = U.epoch()
		local mid = U.pos()
		C.event("login", { char = U.unitName("player"), realm = U.str(try(GetRealmName)), map = mid, zone = zone, subzone = sub,
			bind = U.str(try(GetBindLocation)) })
	end
	S.loginHold = U.now() + 3     -- bag and durability edges wait until the client has loaded item data
	S.dirtyAll = true
	if C.rules and C.rules.onEvent then pcall(C.rules.onEvent, "entering_world", { zone = zone, subzone = sub }) end
end

H.PLAYER_LEAVING_WORLD = function() S.loading = true end

H.PLAYER_LOGOUT = function()
	S.logoutAt = U.epoch()
	C.event("logout", {})
	-- best effort: draw the strip once more now; the game may close before the next scheduled tick
	if WoWFeed_SendTick then pcall(WoWFeed_SendTick) end
end

H.PLAYER_LEVEL_UP = function(level) C.event("level_up", { level = U.num(level) or U.unitLevel("player") }) end

H.PLAYER_DEAD = function() onDeath() end

H.PLAYER_ALIVE = function()
	local d = S.pendingDeath
	if not d then return end
	S.pendingDeath = nil
	local _, _, avg = lowestDurability()
	d.durability_pct = avg and floor(avg + 0.5) or nil
	S.dirtyDeaths = true
	C.event("death_released", { durability_pct = d.durability_pct })
end
H.PLAYER_UNGHOST = function() S.pendingDeath = nil end

H.PLAYER_REGEN_DISABLED = function()
	if not S.fight then newFight() end
	S.pullAt = U.now()
	pcall(C.sampleAttackers)
end
H.PLAYER_REGEN_ENABLED = function()
	finishFight()
	S.att = {}
	snapshotVitals()
	S.dirtyAll = true
end

H.UNIT_COMBAT = function(unit, event, flag, amount)
	if secret(event) or event ~= "WOUND" then return end
	local f = S.fight
	if not f or secret(unit) or type(unit) ~= "string" then return end
	local amt = U.num(amount)
	if not amt then return end
	if unit == "player" then f.taken = f.taken + amt; return end
	-- damage dealt: the target, and other hostile units that are not the target (one count per unit, no double counting)
	local hostile = U.bool(try(UnitCanAttack, "player", unit))
	if not hostile then return end
	if unit ~= "target" then
		local same = try(UnitIsUnit, unit, "target")
		if secret(same) or same == true then return end
	end
	f.dealt = f.dealt + amt
end

H.UNIT_SPELLCAST_SUCCEEDED = function(unit, _, spellID)
	if secret(unit) or unit ~= "player" then return end
	onCast(spellID)
end

H.CHAT_MSG_COMBAT_XP_GAIN = function(msg)
	if type(msg) ~= "string" or secret(msg) then return end
	-- "<mob> dies, you gain N experience." counts as a kill (the "You gain N experience" form has no mob and is not one)
	local mob = msg:match("^(.-) dies, you gain %d+")
	if not mob and COMBAT_XP_GAIN then
		local p = patternFromFormat(COMBAT_XP_GAIN:gsub("%(.*$", ""):gsub("[%s%.]+$", ""))
		if p then mob = msg:match((p:gsub("%$$", ""))) end
	end
	if mob and S.fight then S.fight.kills = S.fight.kills + 1 end
end

H.CHAT_MSG_LOOT = function(msg)
	local link, count = parseOwnLoot(msg)
	if not link then return end
	local lk = link:match("(|c%x+|Hitem:.-|h%[.-%]|h|r)") or link
	local it = U.itemInfo(lk)
	local q = it.quality
	local boe = (it.bindType == 2 and q and q >= 2) or nil
	local data = { link = lk, name = it.name, quality = q, count = count, bind = it.bind, equip_slot = it.equipLoc, armor_type = it.armor, price = it.price, boe_keep = boe }
	if q and q >= 2 then C.event("item_looted", data) else C.event("item_looted", data, { quiet = true }) end
	S.dirtyItems = true
end

H.QUEST_LOG_UPDATE = function()
	local qs = S.quests
	if U.now() - qs.last >= 0.5 then C.scanQuests() else qs.dirty = true end
	S.dirtyRules = true
end
H.QUEST_TURNED_IN = function(qid)
	qid = U.num(qid)
	if not qid then return end
	C.event("quest_turned_in", { quest = qid, title = questTitle(qid) })
	S.quests.ready[qid] = nil
	S.autoDirty = true
end

local function learned(spellID)
	spellID = U.num(spellID)
	if not spellID then return end
	local now = U.now()
	if S.spellDedupe[spellID] and now - S.spellDedupe[spellID] < 1 then return end
	S.spellDedupe[spellID] = now
	U.invalidateRanks()
	local sub, rank = U.spellRank(spellID)
	C.event("spell_learned", { spell = spellID, name = U.spellName(spellID), rank = rank })
	S.dirtyBars = true; S.autoDirty = true
end
H.LEARNED_SPELL_IN_TAB = learned
H.LEARNED_SPELL_IN_SKILL_LINE = learned
H.SPELLS_CHANGED = function() U.invalidateRanks(); S.dirtyBars = true; S.autoDirty = true end

H.PLAYER_EQUIPMENT_CHANGED = function(slot, hasCurrent)
	slot = U.num(slot)
	if not slot then return end
	S.dirtyDura = true; S.autoDirty = true
	if not secret(hasCurrent) and hasCurrent == false then return end   -- slot emptied
	local link = U.str(try(GetInventoryItemLink, "player", slot))
	if not link then return end
	local it = U.itemInfo(link)
	C.event("item_equipped", { slot = SLOT_NAMES[slot] or tostring(slot), link = link, name = it.name, quality = it.quality, bind = it.bind,
		soulbound = U.isBound(nil, nil, slot) })
end

local function zoneChanged()
	local zone, sub = U.zone()
	local key = zone .. "|" .. sub
	if key == S.lastZone then return end
	S.lastZone = key
	local mid = U.pos()
	C.event("zone_changed", { map = mid, zone = zone, subzone = sub })
	S.autoDirty = true
end
H.ZONE_CHANGED = zoneChanged
H.ZONE_CHANGED_INDOORS = zoneChanged
H.ZONE_CHANGED_NEW_AREA = zoneChanged

H.PLAYER_FLAGS_CHANGED = function(unit) if not secret(unit) and unit == "player" then S.afk = U.bool(try(UnitIsAFK, "player")) or false end end
H.PLAYER_TARGET_CHANGED = function()
	S.dirtyTarget = true
	if C.rules and C.rules.onTarget then pcall(C.rules.onTarget) end
	if U.exists("target") and not U.bool(try(UnitIsPlayer, "target")) and not U.bool(try(UnitCanAttack, "player", "target")) then
		pcall(C.noteNpc, "target", "target")
	end
end
-- An attackable enemy player shows up (nameplate): one event per player per minute with class and level, so a rule can warn or advise.
-- Names are never sent (they can be hidden by the game and the agent does not need them).
S.enemySeen = {}
H.NAME_PLATE_UNIT_ADDED = function(unit)
	if secret(unit) or type(unit) ~= "string" then return end
	if not U.bool(try(UnitIsPlayer, unit)) or not U.bool(try(UnitCanAttack, "player", unit)) then return end
	local okg, guid = pcall(UnitGUID, unit)
	local key = (okg and type(guid) == "string" and not secret(guid)) and guid or unit
	local t = U.now()
	if S.enemySeen[key] and t - S.enemySeen[key] < 60 then return end
	S.enemySeen[key] = t
	local okc, _, class = pcall(UnitClass, unit)
	local okl, lvl = pcall(UnitLevel, unit)
	local okr, _, race = pcall(UnitRace, unit)
	local d = { class = (okc and type(class) == "string" and not secret(class)) and class or nil,
		level = (okl and type(lvl) == "number" and not secret(lvl)) and lvl or nil,
		race = (okr and type(race) == "string" and not secret(race)) and race or nil,
		flagged = U.bool(try(UnitIsPVP, unit)) or false }
	C.event("enemy_player", d)
end
H.BAG_UPDATE_DELAYED = function() S.dirtyBags = true; S.dirtyItems = true; S.autoDirty = true end
H.UPDATE_INVENTORY_DURABILITY = function() S.dirtyDura = true end
H.UNIT_AURA = function(unit) if not secret(unit) and (unit == "player" or unit == "pet") then S.dirtyAuras = true end end
H.UNIT_PET = function(unit) if not secret(unit) and unit == "player" then S.dirtyPet = true end end
H.PET_BAR_UPDATE = function() S.dirtyPet = true end
H.ACTIONBAR_SLOT_CHANGED = function() S.dirtyBars = true end
H.UPDATE_BONUS_ACTIONBAR = function() S.dirtyBars = true end
H.UPDATE_SHAPESHIFT_FORM = function() S.dirtyBars = true end
H.SKILL_LINES_CHANGED = function() S.autoDirty = true end

-- Event registration: a name this client does not know raises on RegisterEvent, so each one is protected.
local F = CreateFrame("Frame")
C.frame = F
local UNIT_ONLY = { UNIT_SPELLCAST_SUCCEEDED = { "player" }, UNIT_AURA = { "player", "pet" }, UNIT_PET = { "player" } }
F:SetScript("OnEvent", function(_, e, ...)
	local h = H[e]
	if h then
		local ok, err = pcall(h, ...)
		if not ok then C.err(e, err) end
	end
end)
C.registered = {}      -- event name -> true when this client accepted it (LEARNED_SPELL_IN_TAB, for one, is gone in 12.0)
function C.register(e)
	local ok
	if UNIT_ONLY[e] and F.RegisterUnitEvent then ok = pcall(F.RegisterUnitEvent, F, e, (unpack or table.unpack)(UNIT_ONLY[e])) end
	if not ok then ok = pcall(F.RegisterEvent, F, e) end
	C.registered[e] = ok and true or nil
	return ok
end
for e in pairs(H) do C.register(e) end
-- other Coach files add handlers here; several handlers for one event run in the order they were added
function C.on(e, fn)
	local prev = H[e]
	if prev then H[e] = function(...) pcall(prev, ...); fn(...) end else H[e] = fn end
	C.register(e)
end

---------------------------------------------------------------- tick (1 s)
-- One ticker drives everything polled: attacker sampling (1 s in combat), checklist auto-complete (2 s), rules (5 s),
-- state sections (their own cadence). Each part is protected so one failure never stops the others.
local function part(name, f, ...)
	local ok, err = pcall(f, ...)
	if not ok then C.err(name, err) end
end

function C.tick()
	local now = U.now()
	S.ticks = S.ticks + 1
	S.afk = U.bool(try(UnitIsAFK, "player")) or false
	if S.quests.dirty then part("quests", C.scanQuests) end
	if U.inCombat() then
		part("attackers", C.sampleAttackers)
		if not S.fight then newFight() end
	else
		part("vitals", snapshotVitals)
		if S.fight then part("fight", finishFight) end
	end
	if C.rules and C.rules.tick then part("rules", C.rules.tick, now) end
	if C.state and C.state.tick then part("state", C.state.tick, now) end
end

function C.start()
	if S.started then return end
	S.started = true
	S.startedAt = U.now()
	if not S.quests.scanned then part("quests", C.scanQuests) end
	C_Timer.NewTicker(1, C.tick)
end

-- afk and loading flags for the SUMMARY KV line (WoWFeed.lua puts them early in the line, where a narrow strip cannot cut them)
function C.flags()
	local afk = U.bool(try(UnitIsAFK, "player"))
	if afk == nil then afk = S.afk end
	return afk and 1 or 0, S.loading and 1 or 0
end
function C.kv()
	local a, l = C.flags()
	return strfmt(" afk=%d loading=%d", a, l)
end

-- test and diagnostics access
C.recentAttackers = recentAttackers
C.onCast = onCast
C.questTitle = questTitle
