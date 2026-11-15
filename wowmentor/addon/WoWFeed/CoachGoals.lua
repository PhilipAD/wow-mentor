-- Coach goals: feed sections for the player's three goals (60 fast, top PvP warlock, professions that pay off). Read-only, coach only.
--   LEVELING     XP boosts (Well Fed, rested, sleeping bag), riding fund toward level 40, pace per quest and per zone (kept across reloads)
--   ENEMIES      enemy players on nameplates (class, level, guild, PvP flag) and an enemy_player event for a dangerous one
--   DUELS        duel wins and losses (system messages), and the record against each class (duels + Warlords Forever's world PvP)
--   PVP_KIT      potions, Engineering devices and their cooldowns, healthstones / soulstone / shards, with a verdict
--   PVP_GEAR     bag items scored with PvP weights (Stamina first) against what is equipped in that slot
--   PROF_TARGETS the PvP potions and devices to work toward: skill needed, where the recipe comes from, known or not (Crafter's Journal data)
local floor, max, min = math.floor, math.max, math.min
local strfmt, tconcat = string.format, table.concat
local C = WoWFeedCoach
local API = WoWFeed_API or {}
local S, U, ST = C.S, C.U, C.state
local try, secret = U.try, U.secret

local G = {}
C.goals = G

local function clean(s) if API.clean then return API.clean(s) end return tostring(s) end
local function val(v) if v == nil then return "unknown" end return tostring(v) end
local function itemName(id) return U.str(try(C_Item and C_Item.GetItemNameByID, id)) or U.str(try(GetItemInfo, id)) end
local function count(id) return U.num(try(C_Item and C_Item.GetItemCount, id)) or U.num(try(GetItemCount, id)) or 0 end
-- his names: Forever characters have a first name and a surname; UnitName gives the first, chat messages may carry both
local function myNames()
	local first = U.unitName("player")
	local full = U.str(try(GetUnitName, "player", true))
	if not full or full == first then
		local n, sur = try(UnitFullName, "player")
		n, sur = U.str(n), U.str(sur)
		if n and sur and sur ~= "" then full = n .. " " .. sur end
	end
	return first, full
end
G.myNames = myNames
local function isMe(n)
	local first, full = myNames()
	return n ~= nil and (n == first or n == full)
end

-- account-wide saved variables: one record per character
local function db()
	if type(WoWFeedDB) ~= "table" then return {} end
	WoWFeedDB.goals = type(WoWFeedDB.goals) == "table" and WoWFeedDB.goals or {}
	local first, full = myNames()
	local key = full or first or "?"
	WoWFeedDB.goals[key] = type(WoWFeedDB.goals[key]) == "table" and WoWFeedDB.goals[key] or {}
	local g = WoWFeedDB.goals[key]
	g.accepted = type(g.accepted) == "table" and g.accepted or {}
	g.done = type(g.done) == "table" and g.done or {}
	g.zones = type(g.zones) == "table" and g.zones or {}
	g.duels = type(g.duels) == "table" and g.duels or {}
	return g
end
G.db = db

---------------------------------------------------------------- LEVELING
local RIDING_GOLD = 100                     -- Apprentice Riding plus the mount at 40, about 100 gold (Forever speed-leveling guides)

local function hasAura(pred)
	local a = U.auras("player", "HELPFUL")
	if not a then return nil end
	for _, x in ipairs(a) do if pred(x.name) then return true, x end end
	return false
end

function G.levelingLines()
	local L = {}
	local lvl = U.unitLevel("player")
	local fed = hasAura(function(n) return n == "Well Fed" or n:find("Well Fed", 1, true) end)
	local bag = hasAura(function(n) return n:find("Sleeping Bag", 1, true) or n:find("Cozy", 1, true) end)
	local rested = U.num(try(GetXPExhaustion)) or 0
	local resting = U.bool(try(IsResting))
	local miss = {}
	if fed == false then miss[#miss + 1] = "eat cooked food (Well Fed, +5% kill XP)" end
	if rested == 0 then miss[#miss + 1] = "log out in an inn or city (rested XP doubles kill XP)" end
	L[#L + 1] = strfmt("xp boosts: well_fed=%s sleeping_bag=%s rested_xp=%d resting_now=%s%s", fed == nil and "unknown" or (fed and "yes" or "no"),
		bag == nil and "unknown" or (bag and "yes" or "no"), rested, val(resting), #miss > 0 and (" | missing: " .. tconcat(miss, "; ")) or "")
	if lvl and lvl < 40 then
		local money = U.num(try(GetMoney)) or 0
		L[#L + 1] = strfmt("riding fund: %dg %ds of about %dg for riding at 40 (%d levels to go)", floor(money / 10000), floor(money / 100) % 100, RIDING_GOLD, 40 - lvl)
	end
	-- pace per zone (XP per active hour) and per quest (XP per minute from accept to hand-in), kept across reloads
	local g = db()
	local zones = {}
	for z, r in pairs(g.zones) do if type(r) == "table" and (r.s or 0) >= 300 then zones[#zones + 1] = { z = z, xp = r.xp or 0, s = r.s, deaths = r.deaths or 0 } end end
	table.sort(zones, function(a, b) return a.s > b.s end)
	local zp = {}
	for i = 1, min(#zones, 6) do local z = zones[i]; zp[#zp + 1] = strfmt("%s %d xp/h over %d min, %d deaths", clean(z.z), floor(z.xp * 3600 / z.s), floor(z.s / 60), z.deaths) end
	L[#L + 1] = "zone pace: " .. (#zp > 0 and tconcat(zp, "; ") or "not enough play yet")
	local q = {}
	for _, r in ipairs(g.done) do if type(r) == "table" and r.min and r.min > 0 and r.xp and r.xp > 0 then q[#q + 1] = r end end
	table.sort(q, function(a, b) return a.xp / a.min < b.xp / b.min end)
	local slow, fast = {}, {}
	for i = 1, min(#q, 4) do slow[#slow + 1] = strfmt("%s %d xp in %d min", clean(q[i].title or ("#" .. q[i].id)), q[i].xp, q[i].min) end
	for i = #q, max(1, #q - 3), -1 do if #q >= 6 then fast[#fast + 1] = strfmt("%s %d xp in %d min", clean(q[i].title or ("#" .. q[i].id)), q[i].xp, q[i].min) end end
	L[#L + 1] = strfmt("quests timed: %d (minutes from accept to hand-in, other quests done meanwhile count too)", #q)
	if #slow > 0 then L[#L + 1] = "  slowest xp/min: " .. tconcat(slow, "; ") end
	if #fast > 0 then L[#L + 1] = "  fastest xp/min: " .. tconcat(fast, "; ") end
	return L
end

local function zoneName() return U.str(try(GetRealZoneText)) or U.str(try(GetZoneText)) or "unknown" end

C.on("QUEST_ACCEPTED", function(a, b)
	local qid = U.num(b) or U.num(a)
	if not qid or qid <= 0 then return end
	local g = db()
	local now = U.epoch()
	g.accepted[qid] = { t = now, zone = zoneName() }                   -- a re-accepted quest starts its clock again
	for id, r in pairs(g.accepted) do if type(r) ~= "table" or now - (r.t or 0) > 3 * 86400 then g.accepted[id] = nil end end   -- abandoned ones
end)
C.on("QUEST_TURNED_IN", function(qid, xp)
	qid, xp = U.num(qid), U.num(xp)
	if not qid then return end
	local g = db()
	local acc = g.accepted[qid]
	g.accepted[qid] = nil
	local title = C.questTitle and C.questTitle(qid) or nil
	g.done[#g.done + 1] = { id = qid, xp = xp, title = U.str(title), zone = acc and acc.zone or zoneName(),
		min = acc and acc.t and max(1, floor((U.epoch() - acc.t) / 60 + 0.5)) or nil, t = U.epoch() }
	while #g.done > 80 do table.remove(g.done, 1) end
	S.dirtyLeveling = true
end)
C.on("PLAYER_DEAD", function()
	local g, z = db(), zoneName()
	g.zones[z] = g.zones[z] or { xp = 0, s = 0, deaths = 0 }
	g.zones[z].deaths = (g.zones[z].deaths or 0) + 1
end)

-- per-zone XP and active time, from the XP rate tracker in CoachData (S.xpt.gained and S.xpt.active)
local lastGained, lastActive
function G.trackZone()
	local X = S.xpt
	if not X or not X.active then return end
	local gained, active = X.gained or 0, X.active
	if lastGained and active >= lastActive then
		local g, z = db(), zoneName()
		local r = g.zones[z] or { xp = 0, s = 0, deaths = 0 }
		g.zones[z] = r
		r.xp = r.xp + max(0, gained - lastGained)
		r.s = r.s + (active - lastActive)
	end
	lastGained, lastActive = gained, active
end

---------------------------------------------------------------- ENEMIES (enemy players on nameplates; the enemy_player warning event comes from Coach.lua)
local SEEN_KEEP = 120

local function scanUnit(u, now)
	if type(u) ~= "string" or secret(u) then return end
	if not U.bool(try(UnitIsPlayer, u)) or not U.bool(try(UnitCanAttack, "player", u)) then return end
	local guid = U.guid(u)
	local name = U.str(try(GetUnitName, u, true)) or U.unitName(u)       -- "First Last" when the game gives it, like the duel messages
	if not (guid or name) then return end
	local key = guid or name
	local _, cls = try(UnitClass, u)
	local guild = U.str(try(GetGuildInfo, u))
	local r = S.enemies[key] or {}
	r.name, r.class, r.level, r.guild = name or r.name, U.str(cls) or r.class, U.unitLevel(u) or r.level, guild or r.guild
	r.pvp = U.bool(try(UnitIsPVP, u))
	r.dead = U.bool(try(UnitIsDead, u))
	r.last = now
	r.first = r.first or now
	if not S.enemies[key] or r.dead ~= S.enemies[key]._dead or r.level ~= S.enemies[key]._level then S.dirtyEnemies = true end
	r._dead, r._level = r.dead, r.level
	S.enemies[key] = r
	-- the warning event is Coach.lua's enemy_player (class, level, race, flagged; no names): nothing to send here
end

function G.scanEnemies()
	S.enemies = S.enemies or {}
	local now = U.epoch()
	local plates = C_NamePlate and try(C_NamePlate.GetNamePlates)
	if type(plates) == "table" then
		for _, p in ipairs(plates) do pcall(scanUnit, p.namePlateUnitToken or p.unitToken or (p.UnitFrame and p.UnitFrame.unit), now) end
	end
	pcall(scanUnit, "target", now)
	for k, r in pairs(S.enemies) do if now - (r.last or 0) > SEEN_KEEP then S.enemies[k] = nil; S.dirtyEnemies = true end end
end

function G.enemyLines()
	local list = {}
	for _, r in pairs(S.enemies or {}) do list[#list + 1] = r end
	if #list == 0 then return { "no enemy players seen in the last 2 min" } end
	table.sort(list, function(a, b) return (a.last or 0) > (b.last or 0) end)
	local L = { strfmt("enemy players seen in the last 2 min: %d", #list) }
	local now = U.epoch()
	for i = 1, min(#list, 12) do
		local r = list[i]
		-- no "seconds ago" here: the section is only resent when it changes, and a ticking age would resend it every scan
		L[#L + 1] = strfmt("%s | %s | L%s | guild %s | pvp=%s%s | %s", clean(r.name or "?"), tostring(r.class or "?"), r.level and (r.level < 0 and "??" or tostring(r.level)) or "?",
			clean(r.guild or "none"), val(r.pvp), r.dead and " | dead" or "", now - (r.last or now) <= 10 and "in view" or "seen in the last 2 min")
	end
	return L
end

---------------------------------------------------------------- DUELS and the record against each class
-- "%1$s has defeated %2$s in a duel" / "%2$s has fled from %1$s in a duel": winner is %1$s
local function formatPattern(fmt)
	if type(fmt) ~= "string" then return nil end
	local order = {}
	-- placeholders first (%1$s, %s) to a marker, then escape the rest, then the marker becomes a capture
	local t = fmt:gsub("%%(%d)%$s", function(n) order[#order + 1] = tonumber(n); return "\1" end)
	t = t:gsub("%%s", function() order[#order + 1] = #order + 1; return "\1" end)
	t = t:gsub("([%^%$%(%)%%%.%[%]%*%+%-%?])", "%%%1")
	t = t:gsub("\1", "(.+)")
	return "^" .. t .. "$", order
end
G.formatPattern = formatPattern

local function duelResult(msg)
	for _, fmt in ipairs({ DUEL_WINNER_KNOCKOUT or "%1$s has defeated %2$s in a duel", DUEL_WINNER_RETREAT or "%2$s has fled from %1$s in a duel" }) do
		local pat, order = formatPattern(fmt)
		if pat then
			local a, b = msg:match(pat)
			if a and b then
				local caps = { a, b }
				local winner, loser = caps[order[1] == 1 and 1 or 2], caps[order[1] == 1 and 2 or 1]
				return winner, loser
			end
		end
	end
end
G.duelResult = duelResult

C.on("CHAT_MSG_SYSTEM", function(msg)
	if type(msg) ~= "string" or secret(msg) then return end
	local winner, loser = duelResult(msg)
	if not winner then return end
	local won
	if isMe(winner) then won = true elseif isMe(loser) then won = false else return end      -- someone else's duel nearby
	local opp = won and loser or winner
	local oppFirst = opp:match("^(%S+)")
	local cls, lvl
	for _, r in pairs(S.enemies or {}) do if r.name == opp or r.name == oppFirst then cls, lvl = r.class, r.level end end
	if not cls then
		local tn, tf = U.unitName("target"), U.str(try(GetUnitName, "target", true))
		if tn and (tf == opp or tn == opp or tn == oppFirst) then local _, c = try(UnitClass, "target"); cls, lvl = U.str(c), U.unitLevel("target") end
	end
	local g = db()
	g.duels[#g.duels + 1] = { t = U.epoch(), opp = opp, class = cls, won = won, level = lvl }
	while #g.duels > 150 do table.remove(g.duels, 1) end
	S.dirtyDuels = true
	C.event("duel_result", { won = won, opponent = opp, class = cls })
end)

function G.duelLines()
	local g = db()
	local w, l = 0, 0
	local byClass = {}
	local function cls(k) byClass[k] = byClass[k] or { dw = 0, dl = 0, k = 0, d = 0 }; return byClass[k] end
	for _, d in ipairs(g.duels) do
		if d.won then w = w + 1 else l = l + 1 end
		local c = cls(d.class or "UNKNOWN")
		if d.won then c.dw = c.dw + 1 else c.dl = c.dl + 1 end
	end
	local wf = type(WarlordsForeverCharDB) == "table" and type(WarlordsForeverCharDB.players) == "table" and WarlordsForeverCharDB.players
	for _, p in pairs(wf or {}) do
		if type(p) == "table" and p.class then local c = cls(p.class); c.k = c.k + (tonumber(p.kills) or 0); c.d = c.d + (tonumber(p.deaths) or 0) end
	end
	local L = { strfmt("duels: won %d lost %d", w, l) }
	local keys = {}
	for k in pairs(byClass) do keys[#keys + 1] = k end
	table.sort(keys)
	for _, k in ipairs(keys) do
		local c = byClass[k]
		L[#L + 1] = strfmt("  vs %s: duels %d-%d, world pvp kills %d deaths %d", k, c.dw, c.dl, c.k, c.d)
	end
	for i = #g.duels, max(1, #g.duels - 5), -1 do
		local d = g.duels[i]
		L[#L + 1] = strfmt("  %s %s %s (%s)", date and date("%m-%d %H:%M", d.t) or tostring(d.t), d.won and "beat" or "lost to", clean(d.opp or "?"), tostring(d.class or "?"))
	end
	return L
end

---------------------------------------------------------------- PVP_KIT
local KIT = {
	{ "Free Action Potion", { 5634, 287976 } }, { "Limited Invulnerability Potion", { 3387 } }, { "Restorative Potion", { 9030 } },
	{ "Swiftness Potion", { 2459, 287975, 288898 } }, { "Shadow Protection Potion", { 6048, 13459 } }, { "Living Action Potion", { 20008 } },
	{ "healing potions", { 118, 858, 929, 1710, 3928, 13446, 223913, 223914, 287977, 288896 } }, { "mana potions", { 2455, 3385, 3827, 6149, 13443, 13444 } },
}
local DEVICES = {
	{ "grenades and bombs (shared 1 min cooldown)", { 4360, 4370, 4374, 4380, 4390, 4394, 10514, 10562, 15993, 4358, 4365, 6714, 10507, 18641, 4852 } },
	{ "Net-o-Matic", { 10720 } }, { "target dummies", { 4366, 4392, 16023 } }, { "Goblin Rocket Boots", { 7189 } }, { "Discombobulator Ray", { 4388 } },
	{ "Gnomish Cloaking Device", { 4397 } }, { "Goblin Land Mine", { 4395 } }, { "Goblin Sapper Charge", { 10646 } },
}
local HEALTHSTONES = { 5512, 19004, 19005, 5511, 19006, 19007, 5509, 19008, 19009, 5510, 19010, 19011, 9421, 19012, 19013 }
local SOULSTONES = { 5232, 16892, 16893, 16895, 16896 }

local function sum(ids)
	local n, have = 0, nil
	for _, id in ipairs(ids) do local c = count(id); if c > 0 then n = n + c; have = have or id end end
	return n, have
end
local function cd(id)
	if not id then return nil end
	local s, d = try(C_Container and C_Container.GetItemCooldown, id)
	if s == nil then s, d = try(GetItemCooldown, id) end
	s, d = U.num(s), U.num(d)
	if not (s and d) then return nil end
	if s == 0 or d == 0 then return 0 end
	return max(0, floor(s + d - U.now()))
end
G.cd = cd

function G.kitLines()
	local L = {}
	local parts, missing = {}, {}
	for _, k in ipairs(KIT) do
		local n = sum(k[2])
		parts[#parts + 1] = strfmt("%s=%d", k[1], n)
	end
	L[#L + 1] = "potions: " .. tconcat(parts, ", ")
	local dev = {}
	for _, k in ipairs(DEVICES) do
		local n, id = sum(k[2])
		if n > 0 then
			local c = cd(id)
			dev[#dev + 1] = strfmt("%s=%d (%s)", k[1], n, c == nil and "cooldown unknown" or (c == 0 and "ready" or (c .. "s")))
		end
	end
	L[#L + 1] = "engineering devices: " .. (#dev > 0 and tconcat(dev, ", ") or "none in bags")
	local hs = sum(HEALTHSTONES)
	local ss = sum(SOULSTONES)
	local shards = count(6265)
	L[#L + 1] = strfmt("warlock: healthstones=%d soulstones=%d soul_shards=%d", hs, ss, shards)
	if hs == 0 then missing[#missing + 1] = "a Healthstone" end
	if shards < 3 then missing[#missing + 1] = "3+ soul shards (Fear chains, resummon, Healthstone)" end
	if sum(KIT[7][2]) == 0 then missing[#missing + 1] = "a healing potion" end
	local lvl = U.unitLevel("player") or 0
	if lvl >= 20 and sum(KIT[4][2]) == 0 then missing[#missing + 1] = "a Swiftness Potion (escape)" end
	if lvl >= 30 and sum(KIT[1][2]) == 0 then missing[#missing + 1] = "a Free Action Potion (drink before melee reaches you)" end
	if lvl >= 30 and sum(DEVICES[1][2]) == 0 then missing[#missing + 1] = "grenades or bombs (stun, shared 1 min cooldown)" end
	L[#L + 1] = (#missing == 0 and "pvp ready: yes" or ("pvp ready: no, missing " .. tconcat(missing, "; ")))
	return L
end
C.on("BAG_UPDATE_DELAYED", function() S.dirtyKit = true; S.dirtyPvpGear = true end)

---------------------------------------------------------------- PVP_GEAR (PvP weights, Stamina first)
local W = { STAMINA = 1.0, SPELL_POWER = 0.9, SPELL_DAMAGE_DONE = 0.9, SHADOW_DAMAGE = 0.8, INTELLECT = 0.35, SPIRIT = 0.25, AGILITY = 0.05, STRENGTH = 0.02,
	CRIT_SPELL_RATING = 0.5, HIT_SPELL_RATING = 0.6, SPELL_HIT = 8, SPELL_CRIT = 7, ARMOR = 0.02, RESILIENCE_RATING = 1.0 }
local SLOTS = { INVTYPE_HEAD = { 1 }, INVTYPE_NECK = { 2 }, INVTYPE_SHOULDER = { 3 }, INVTYPE_CHEST = { 5 }, INVTYPE_ROBE = { 5 }, INVTYPE_WAIST = { 6 },
	INVTYPE_LEGS = { 7 }, INVTYPE_FEET = { 8 }, INVTYPE_WRIST = { 9 }, INVTYPE_HAND = { 10 }, INVTYPE_FINGER = { 11, 12 }, INVTYPE_TRINKET = { 13, 14 },
	INVTYPE_CLOAK = { 15 }, INVTYPE_WEAPON = { 16 }, INVTYPE_2HWEAPON = { 16, 17, both = true }, INVTYPE_WEAPONMAINHAND = { 16 }, INVTYPE_WEAPONOFFHAND = { 17 },
	INVTYPE_HOLDABLE = { 17 }, INVTYPE_SHIELD = { 17 }, INVTYPE_RANGED = { 18 }, INVTYPE_RANGEDRIGHT = { 18 }, INVTYPE_THROWN = { 18 } }

-- what a warlock can use: cloth, and daggers, one-handed swords, staves, wands and off-hand items (neck, rings, trinkets, cloaks have no type)
local WEARABLE = { Cloth = true, Miscellaneous = true, Daggers = true, ["One-Handed Swords"] = true, Staves = true, Wands = true }
local NO_TYPE = { INVTYPE_NECK = true, INVTYPE_FINGER = true, INVTYPE_TRINKET = true, INVTYPE_CLOAK = true, INVTYPE_HOLDABLE = true }

local function pvpScore(stats)
	if type(stats) ~= "table" then return nil end
	local s = 0
	for k, v in pairs(stats) do
		local w = W[k]
		if not w then for key, kw in pairs(W) do if k:find(key, 1, true) then w = kw; break end end end
		if w and tonumber(v) then s = s + w * v end
	end
	return s
end
G.pvpScore = pvpScore

function G.pvpGearLines()
	local ok, rows = pcall(ST.bagItems)
	if not ok or type(rows) ~= "table" then return { "pvp gear: unknown (bags unreadable)" } end
	local L, n = {}, 0
	local lvl = U.unitLevel("player") or 0
	for _, r in ipairs(rows) do
		local slots = r.equipLoc and SLOTS[r.equipLoc]
		local info = r.link and U.itemInfo(r.link) or {}
		local usable = slots and (NO_TYPE[r.equipLoc] or (r.armor and WEARABLE[r.armor])) and (info.minLevel or 0) <= lvl
		local mine = usable and pvpScore(r.stats)
		if mine then
			local best, unknown = nil, false
			local function slotScore(slot)
				local link = U.str(try(GetInventoryItemLink, "player", slot))
				if not link then return 0 end
				local s = pvpScore(U.itemStats(link))
				if s == nil then unknown = true; return 0 end                 -- stats not cached yet: no comparison this time
				return s
			end
			if slots.both then best = slotScore(16) + slotScore(17)
			else for _, slot in ipairs(slots) do local cur = slotScore(slot); if not best or cur < best then best = cur end end end
			local gain = (not unknown) and (mine - (best or 0)) or 0
			if gain > 0.5 then
				n = n + 1
				L[#L + 1] = strfmt("%s (%s) pvp_gain=+%.1f%s", clean(r.name or "?"), r.equipLoc:gsub("INVTYPE_", ""):lower(), gain, r.keep and " (BoE, could sell)" or "")
			end
		end
	end
	table.insert(L, 1, strfmt("pvp gear upgrades in bags: %d (weights: Stamina 1, spell power 0.9, Intellect 0.35, Spirit 0.25; equip it yourself)", n))
	return L
end

---------------------------------------------------------------- PROF_TARGETS (the PvP kit to craft, from Crafter's Journal's data)
local TARGETS = {
	Alchemy = { 2459, 3827, 1710, 6048, 5634, 9030, 3928, 3387, 6149, 13446, 13459, 20008 },
	Engineering = { 4360, 4365, 4366, 4370, 4374, 4380, 4390, 4388, 4392, 4394, 4395, 4397, 7189, 10720, 15993 },
	FirstAid = { 14530 },
}

function G.profTargetLines()
	local ES = CraftersJournal
	if type(ES) ~= "table" or type(ES.Data) ~= "table" then return { "profession targets: unknown (Crafter's Journal not loaded)" } end
	local L = {}
	for _, prof in ipairs({ "Alchemy", "Engineering", "FirstAid" }) do
		local data = ES.Data[prof]
		if type(data) == "table" and type(data.recipes) == "table" then
			local ok, rank = pcall(ES.GetProfessionSkill, ES, data.skillLine)
			rank = ok and U.num(rank) or nil
			local byItem = {}
			for spell, r in pairs(data.recipes) do
				if type(r) == "table" and type(r.out) == "table" and r.out[1] then byItem[r.out[1]] = { spell = spell, r = r } end
			end
			local parts = {}
			for _, item in ipairs(TARGETS[prof] or {}) do
				local e = byItem[item]
				if e then
					local learn = type(e.r.t) == "table" and e.r.t[1] or nil
					local known = ES.IsRecipeKnown and select(2, pcall(ES.IsRecipeKnown, ES, e.spell))
					local src = e.r.scroll and ("recipe item " .. e.r.scroll) or (e.r.train and "trainer" or "unknown source")
					parts[#parts + 1] = strfmt("%s skill %s, %s%s", clean(e.r.name or itemName(item) or ("item " .. item)), val(learn), src, known == true and ", known" or "")
				end
			end
			if #parts > 0 then L[#L + 1] = strfmt("%s %s: %s", prof, rank and ("(skill " .. rank .. ")") or "(not learned)", tconcat(parts, "; ")) end
		end
	end
	if #L == 0 then L[1] = "profession targets: none found in Crafter's Journal's data" end
	return L
end

---------------------------------------------------------------- scheduling
local nextEnemy, nextZone = -1e9, -1e9
local prevTick = ST.tick
ST.tick = function(now)
	prevTick(now)
	local run = ST.run
	local function part(name, f, ...) local ok, err = pcall(f, ...); if not ok then C.err(name, err) end end
	if now >= nextEnemy then nextEnemy = now + 2; part("enemies", G.scanEnemies) end
	if now >= nextZone then nextZone = now + 15; part("zonepace", G.trackZone) end
	run("leveling", now, 30, 300, "dirtyLeveling", G.levelingLines, 600)
	run("enemies", now, 3, 120, "dirtyEnemies", G.enemyLines, 240)
	run("duels", now, 2, 900, "dirtyDuels", G.duelLines, 1800)
	run("pvp_kit", now, 30, 300, "dirtyKit", G.kitLines, 600)
	run("pvp_gear", now, 60, 600, "dirtyPvpGear", G.pvpGearLines, 1200)
	run("prof_targets", now, 60, 900, "dirtyProfTargets", G.profTargetLines, 1800)
end
C.on("SKILL_LINES_CHANGED", function() S.dirtyProfTargets = true end)
