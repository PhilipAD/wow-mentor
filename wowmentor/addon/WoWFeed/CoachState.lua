-- Coach state: feed sections the remote coach could not see before, plus the interaction windows
-- (merchant, trainer, quest reward, loot) and the bag / durability / action bar edges that become events.
-- Sections go through the existing push(section, lines, maxAge). Every value the game hides is written as "unknown".
-- Display and advice only: nothing here equips, previews, uses or buys anything.
local floor, max, min = math.floor, math.max, math.min
local strfmt, tconcat = string.format, table.concat
local J = WoWFeedJSON
local C = WoWFeedCoach
local API = WoWFeed_API or {}
local S, U = C.S, C.U
local try, secret = U.try, U.secret

local ST = {}
C.state = ST

local function clean(s)
	if API.clean then return API.clean(s) end
	return tostring(s)
end
local function money(c)
	c = U.num(c)
	if not c then return "unknown" end
	return strfmt("%dg %ds %dc", floor(c / 10000), floor(c / 100) % 100, c % 100)
end
U.money = money
local function val(v) if v == nil then return "unknown" end return tostring(v) end

---------------------------------------------------------------- bags and durability edges
-- free general-purpose slots (profession bags cannot hold ordinary items, so they do not count) and total general slots
function C.bagFree()
	local free, total, any = 0, 0, false
	for b = 0, 4 do
		local n
		if C_Container and C_Container.GetContainerNumSlots then n = U.num(try(C_Container.GetContainerNumSlots, b)) else n = U.num(try(GetContainerNumSlots, b)) end
		if n and n > 0 then
			local f, fam
			if C_Container and C_Container.GetContainerNumFreeSlots then f, fam = try(C_Container.GetContainerNumFreeSlots, b)
			else f, fam = try(GetContainerNumFreeSlots, b) end
			f = U.num(f)
			if f and (fam == nil or fam == 0) then free = free + f; total = total + n; any = true end
		end
	end
	if not any then return nil end
	return free, total
end

function C.checkBags()
	if S.loginHold and U.now() < S.loginHold then return end
	local free = C.bagFree()
	if not free then return end
	local B = S.bags
	if free <= 2 then
		if B.full ~= true then B.full = true; C.event("bags_full", { free = free }) end
	elseif free >= 4 then B.full = false      -- re-arm with a little hysteresis so 2 <-> 3 does not flap
	elseif B.full == nil then B.full = false end
end

function C.checkDurability()
	if S.loginHold and U.now() < S.loginHold then return end
	local low, slot = C.lowestDurability()
	if not low then return end
	local D = S.dura
	if low <= 20 then
		if D.crit ~= true then D.crit = true; C.event("durability_critical", { pct = floor(low + 0.5), slot = C.SLOT_NAMES[slot] or tostring(slot) }) end
	else D.crit = false end
end

---------------------------------------------------------------- action bars
local MAX_ACTION_SLOT = 180        -- slots 1..180 cover the main, paged and class bars; empty slots report nothing

local function itemName(id)
	local n = U.str(try((C_Item and C_Item.GetItemNameByID) or GetItemInfo, id))
	return n
end

-- spell names (and optional explicit ranks) a macro casts: "/cast [mod] Shadow Bolt(Rank 2); Corruption"
local function macroSpells(body)
	local out = {}
	if type(body) ~= "string" then return out end
	for line in body:gmatch("[^\r\n]+") do
		local cmd, rest = line:match("^%s*/(%a+)%s+(.*)$")
		cmd = cmd and cmd:lower()
		if cmd == "cast" or cmd == "castsequence" or cmd == "use" or cmd == "castrandom" then
			rest = rest:gsub("%b[]", ""):gsub("^%s*reset=%S+%s*", "")
			for piece in rest:gmatch("[^;,]+") do
				local nm, rk = piece:match("^%s*(.-)%s*%(%s*[%a]+%s+(%d+)%s*%)%s*$")
				if not nm then nm = piece:match("^%s*(.-)%s*$") end
				if nm and nm ~= "" then out[#out + 1] = { name = nm, rank = tonumber(rk) } end
			end
		end
	end
	return out
end
ST.macroSpells = macroSpells

local function bookIndex()
	local byName, byId, list = {}, {}, {}
	if not API.spellbook then return byName, byId, list end
	local ok, tabs = pcall(API.spellbook)
	if not ok or type(tabs) ~= "table" then return byName, byId, list end
	local generalTab = _G.GENERAL or "General"      -- the General tab holds professions and utility spells, not rotation spells
	for _, tab in ipairs(tabs) do
		for _, it in ipairs(tab.items) do
			if it.id then byId[it.id] = it end
			if not it.passive then
				local key = U.lower(it.name)
				local e = byName[key]
				local r = it.rank or 0
				if not e then
					e = { name = it.name, rank = r, id = it.id, tab = tab.name, general = (tab.name == generalTab) }
					byName[key] = e
					list[#list + 1] = e
				elseif r > e.rank then e.rank, e.id = r, it.id end
			end
		end
	end
	return byName, byId, list
end
ST.bookIndex = bookIndex

-- Scan all action slots. Returns { slots = {...}, lower = {...}, notOnBar = {...} }
function ST.scanBars()
	local byName, byId, list = bookIndex()
	local slots, onBar, lower = {}, {}, {}
	local counts = { spell = 0, macro = 0, item = 0, other = 0 }
	for slot = 1, MAX_ACTION_SLOT do
		local atype, id, sub = try(GetActionInfo, slot)
		if atype and not secret(atype) then
			local rec = { slot = slot, kind = tostring(atype), id = (type(id) == "number" or type(id) == "string") and not secret(id) and id or nil }
			if atype == "spell" and rec.id then
				counts.spell = counts.spell + 1
				rec.name = U.spellName(rec.id)
				local sub2, rk = U.spellRank(rec.id)
				rec.rankText = sub2; rec.rank = rk or (byId[rec.id] and byId[rec.id].rank)
				if rec.name then
					local key = rec.name:lower()
					onBar[key] = max(onBar[key] or 0, rec.rank or 0)
					local known = byName[key]
					if known and rec.rank and known.rank > rec.rank then
						lower[#lower + 1] = { slot = slot, spell = rec.id, name = rec.name, bar_rank = rec.rank, known_rank = known.rank, key = slot .. ":" .. rec.id .. ":" .. known.rank }
					end
				end
			elseif atype == "macro" and rec.id then
				counts.macro = counts.macro + 1
				local mname, _, body = try(GetMacroInfo, rec.id)
				rec.name = U.str(mname); rec.macro = U.str(body)
				for _, sp in ipairs(macroSpells(rec.macro)) do
					local key = sp.name:lower()
					onBar[key] = max(onBar[key] or 0, sp.rank or 999)
					local known = byName[key]
					if known and sp.rank and known.rank > sp.rank then
						lower[#lower + 1] = { slot = slot, spell = known.id, name = known.name, bar_rank = sp.rank, known_rank = known.rank, macro = true,
							key = slot .. ":m:" .. key .. ":" .. known.rank }
					end
				end
			elseif atype == "item" and rec.id then
				counts.item = counts.item + 1
				rec.name = itemName(rec.id)
			else
				counts.other = counts.other + 1
				rec.name = U.str(sub)
			end
			slots[#slots + 1] = rec
		end
	end
	local notOnBar = {}
	for _, e in ipairs(list) do
		if not e.general and onBar[e.name:lower()] == nil then notOnBar[#notOnBar + 1] = e end
	end
	return { slots = slots, lower = lower, notOnBar = notOnBar, counts = counts }
end

function ST.barLines(scan)
	scan = scan or ST.scanBars()
	local L = {}
	local c = scan.counts
	L[#L + 1] = strfmt("slots used %d (spells %d, macros %d, items %d, other %d)", #scan.slots, c.spell, c.macro, c.item, c.other)
	for _, r in ipairs(scan.slots) do
		local line = strfmt("slot %d: %s %s %s", r.slot, r.kind, r.id ~= nil and ("#" .. tostring(r.id)) or "", clean(r.name or "unknown"))
		if r.rankText then line = line .. " (" .. clean(r.rankText) .. ")" end
		if r.macro then line = line .. " | " .. clean(U.trunc(r.macro:gsub("[\r\n]+", " / "), 110)) end
		L[#L + 1] = line
	end
	local nb = {}
	for i = 1, min(#scan.notOnBar, 60) do
		local e = scan.notOnBar[i]
		nb[#nb + 1] = clean(e.name) .. ((e.rank and e.rank > 0) and (" (Rank " .. e.rank .. ")") or "")
	end
	L[#L + 1] = "known_not_on_bar: " .. (#nb > 0 and tconcat(nb, "; ") or "none")
	if #scan.lower == 0 then L[#L + 1] = "lower_rank_on_bar: none"
	else
		L[#L + 1] = "lower_rank_on_bar:"
		for _, l in ipairs(scan.lower) do
			L[#L + 1] = strfmt("  slot %d: %s rank %d on bar, rank %d known%s", l.slot, clean(l.name), l.bar_rank, l.known_rank, l.macro and " (in a macro)" or "")
		end
	end
	return L
end

-- Edge-triggered: each new (slot, spell, known rank) combination becomes one lower_rank_on_bar event (at most 5 per scan).
-- Fallback for spell_learned when this client accepts neither LEARNED_SPELL_IN_TAB nor LEARNED_SPELL_IN_SKILL_LINE: new spellbook
-- ids between two scans. The first scan only records what is known, and at most 3 events are sent per scan.
function ST.checkLearned()
	if C.registered.LEARNED_SPELL_IN_TAB or C.registered.LEARNED_SPELL_IN_SKILL_LINE then return end
	local _, byId = bookIndex()
	local now = U.now()
	if not ST.knownIds then
		ST.knownIds = {}
		for id in pairs(byId) do ST.knownIds[id] = true end
		return
	end
	local fresh = 0
	for id, it in pairs(byId) do
		if not ST.knownIds[id] then
			ST.knownIds[id] = true
			if fresh < 3 and not (S.spellDedupe[id] and now - S.spellDedupe[id] < 1) then
				fresh = fresh + 1
				S.spellDedupe[id] = now
				C.event("spell_learned", { spell = id, name = U.str(it.name), rank = it.rank })
			end
		end
	end
end

function ST.checkBars()
	pcall(ST.checkLearned)
	local scan = ST.scanBars()
	ST.lastScan = scan
	local now, fresh = {}, 0
	ST.lowerSeen = ST.lowerSeen or {}
	for _, l in ipairs(scan.lower) do
		now[l.key] = true
		if not ST.lowerSeen[l.key] and fresh < 5 then
			fresh = fresh + 1
			ST.lowerSeen[l.key] = true
			C.event("lower_rank_on_bar", { spell = l.spell, name = l.name, bar_rank = l.bar_rank, known_rank = l.known_rank, slot = l.slot })
		end
	end
	for k in pairs(ST.lowerSeen) do if not now[k] then ST.lowerSeen[k] = nil end end
	return scan
end

---------------------------------------------------------------- pet
local function auraLines(unit, filter, label)
	local L = {}
	local list = U.auras(unit, filter)
	if not list then L[#L + 1] = label .. ": unknown (the game hides auras right now)"; return L end
	L[#L + 1] = label .. ":"
	for _, a in ipairs(list) do
		local rem = a.rem and strfmt("%.0fs left", a.rem) or ((a.exp == 0) and "no expiry" or "unknown")
		L[#L + 1] = strfmt("  %s #%s%s %s", clean(a.name), val(a.id), (a.count and a.count > 1) and (" x" .. a.count) or "", rem)
	end
	if list.hidden > 0 then L[#L + 1] = "  (" .. list.hidden .. " hidden by the game)" end
	return L
end

function ST.petLines()
	local L = {}
	if not U.exists("pet") then return { "no pet" } end
	local hp, hpm = U.num(try(UnitHealth, "pet")), U.num(try(UnitHealthMax, "pet"))
	L[#L + 1] = strfmt("name=%s family=%s level=%s", clean(U.unitName("pet") or "unknown"), clean(U.str(try(UnitCreatureFamily, "pet")) or "unknown"), val(U.unitLevel("pet")))
	local hap = try(GetPetHappiness)
	local happy = U.num(hap)
	L[#L + 1] = "happiness: " .. (happy and ((({ "unhappy", "content", "happy" })[happy] or "unknown") .. " (" .. happy .. ")") or "unknown (not a hunter pet, or the client has no happiness)")
	L[#L + 1] = "health: " .. ((hp and hpm and hpm > 0) and strfmt("%d%%", floor(hp / hpm * 100 + 0.5)) or "unknown")
	for _, l in ipairs(auraLines("pet", "HELPFUL", "buffs")) do L[#L + 1] = l end
	for _, l in ipairs(auraLines("pet", "HARMFUL", "debuffs")) do L[#L + 1] = l end
	-- pet spells with autocast: the spellbook (11.0+ API) first, the pet action bar otherwise
	local n
	if C_SpellBook and C_SpellBook.HasPetSpells and Enum and Enum.SpellBookSpellBank then n = U.num((try(C_SpellBook.HasPetSpells))) end
	if n and n > 0 then
		L[#L + 1] = "pet spells:"
		for i = 1, n do
			local it = try(C_SpellBook.GetSpellBookItemInfo, i, Enum.SpellBookSpellBank.Pet)
			if type(it) == "table" and U.str(it.name) then
				local allowed, enabled = try(C_SpellBook.GetSpellBookItemAutoCast, i, Enum.SpellBookSpellBank.Pet)
				allowed, enabled = U.bool(allowed), U.bool(enabled)
				L[#L + 1] = strfmt("  %s #%s autocast=%s", clean(it.name), val(it.spellID), allowed and (enabled and "on" or "off") or "n/a")
			end
		end
	else
		local any = false
		for i = 1, 10 do
			local name, _, isToken, _, allowed, enabled = try(GetPetActionInfo, i)
			isToken, allowed, enabled = U.bool(isToken), U.bool(allowed), U.bool(enabled)
			if U.str(name) then
				if not any then L[#L + 1] = "pet actions:"; any = true end
				local label = (isToken and _G[name]) or name
				L[#L + 1] = strfmt("  %s autocast=%s", clean(label), allowed and (enabled and "on" or "off") or "n/a")
			end
		end
		if not any then L[#L + 1] = "pet spells: unknown" end
	end
	return L
end

---------------------------------------------------------------- target
function ST.hostilePlates()
	local n = 0
	local plates = C_NamePlate and try(C_NamePlate.GetNamePlates)
	if type(plates) ~= "table" then return nil end
	for _, p in ipairs(plates) do
		local u = p.namePlateUnitToken or p.unitToken or (p.UnitFrame and p.UnitFrame.unit)
		if type(u) == "string" and not secret(u) and U.bool(try(UnitCanAttack, "player", u)) and not U.bool(try(UnitIsDead, u)) then n = n + 1 end
	end
	return n
end

function ST.targetLines()
	local L = {}
	if not U.exists("target") then
		L[1] = "no target"
		local n = ST.hostilePlates()
		L[2] = "hostile nameplates: " .. val(n)
		return L
	end
	local lvl = U.unitLevel("target")
	local lvltxt = lvl and (lvl < 0 and "??" or tostring(lvl)) or "unknown"
	local diff = (lvl and lvl > 0 and U.unitLevel("player")) and (lvl - U.unitLevel("player")) or nil
	L[#L + 1] = strfmt("name=%s level=%s classification=%s creature_type=%s", clean(U.unitName("target") or "unknown"), lvltxt,
		U.str(try(UnitClassification, "target")) or "unknown", clean(U.str(try(UnitCreatureType, "target")) or "unknown"))
	L[#L + 1] = strfmt("attackable=%s dead=%s player=%s level_diff=%s", val(U.bool(try(UnitCanAttack, "player", "target"))), val(U.bool(try(UnitIsDead, "target"))),
		val(U.bool(try(UnitIsPlayer, "target"))), val(diff))
	local tt = try(UnitIsUnit, "targettarget", "player")
	L[#L + 1] = "targeting_you: " .. ((secret(tt) or tt == nil) and "unknown" or (tt and "true" or "false"))
	L[#L + 1] = "hostile nameplates: " .. val(ST.hostilePlates())
	return L
end

---------------------------------------------------------------- fights and deaths
local function hms(t) if date then return date("%H:%M:%S", t) end return tostring(t) end

function ST.fightLines()
	local L = {}
	if #S.fights == 0 then return { "no fights recorded this session" } end
	local first = max(1, #S.fights - 4)
	for i = #S.fights, first, -1 do
		local f = S.fights[i]
		L[#L + 1] = strfmt("fight: %s %.1fs result=%s taken=%d dealt=%d kills=%d mana_est=%d casts=%d duplicate_dots=%d", hms(f.start), f.duration or 0, f.result, f.taken, f.dealt, f.kills, f.mana, f.castN, #f.dups)
		local parts = {}
		for _, c in ipairs(f.casts) do
			parts[#parts + 1] = strfmt("%.1f %s%s%s%s", c.t or 0, clean(c.name or "?"), c.rank and (" r" .. c.rank) or "", c.target and (" > " .. clean(c.target)) or "", c.wasted and " WASTED" or "")
		end
		L[#L + 1] = "  casts: " .. (#parts > 0 and tconcat(parts, "; ") or "none")
	end
	return L
end

function ST.deathLines()
	local L = {}
	if #S.deaths == 0 then return { "no deaths this session" } end
	local first = max(1, #S.deaths - 4)
	for i = #S.deaths, first, -1 do
		local d = S.deaths[i]
		L[#L + 1] = strfmt("death: %s %s / %s killer=%s (L%s %s) attackers=%d hp_at_pull=%s mana_at_pull=%s map=%s x=%s y=%s deaths_here_10m=%d durability_after_release=%s",
			hms(d.t), clean(d.zone), clean(d.subzone), clean(d.killer.name), val(d.killer.level), val(d.killer.classification), d.attackers, val(d.hp_at_pull),
			val(d.mana_at_pull), val(d.map), val(d.x), val(d.y), d.n10 or 0, d.durability_pct and (d.durability_pct .. "%") or "unknown")
		if d.killer.source then L[#L] = L[#L] .. " killer_source=" .. d.killer.source end
		if d.last_hits and #d.last_hits > 0 then
			local hits = {}
			for _, h in ipairs(d.last_hits) do hits[#hits + 1] = strfmt("%s %s%s", clean(h.spell or "unknown"), val(h.amount), h.source and (" from " .. clean(h.source)) or "") end
			L[#L + 1] = "  last_hits (newest first): " .. tconcat(hits, "; ")
		end
	end
	return L
end

---------------------------------------------------------------- items (bags)
function ST.bagItems()
	local rows = {}
	for b = 0, 5 do
		local n = (C_Container and U.num(try(C_Container.GetContainerNumSlots, b))) or U.num(try(GetContainerNumSlots, b)) or 0
		for s = 1, n do
			local info = C_Container and C_Container.GetContainerItemInfo and try(C_Container.GetContainerItemInfo, b, s)
			if type(info) == "table" and U.str(info.hyperlink) then
				local q = U.num(info.quality)
				if q and q >= 1 then
					local it = U.itemInfo(info.hyperlink)
					local bound = U.bool(info.isBound)
					if bound == nil then bound = U.isBound(b, s) end
					local r = { bag = b, slot = s, link = info.hyperlink, name = it.name or U.str(info.itemName), id = it.id or U.num(info.itemID), count = U.num(info.stackCount) or 1,
						quality = q, bind = it.bind, bindType = it.bindType, bound = bound, equipLoc = it.equipLoc, armor = it.armor, ilvl = it.ilvl, price = it.price, cached = it.cached }
					r.stats = U.itemStats(info.hyperlink)
					r.keep = (it.bindType == 2 and q >= 2 and bound ~= true) or false
					rows[#rows + 1] = r
				end
			end
		end
	end
	return rows
end

local function statText(st)
	if not st then return "unknown" end
	local keys = {}
	for k in pairs(st) do keys[#keys + 1] = k end
	table.sort(keys)
	local out = {}
	for _, k in ipairs(keys) do out[#out + 1] = k .. "=" .. st[k] end
	return tconcat(out, ",")
end

function ST.itemLines(rows)
	rows = rows or ST.bagItems()
	table.sort(rows, function(a, b)
		if a.keep ~= b.keep then return a.keep end
		if a.quality ~= b.quality then return a.quality > b.quality end
		return (a.bag * 100 + a.slot) < (b.bag * 100 + b.slot)
	end)
	local keep = 0
	for _, r in ipairs(rows) do if r.keep then keep = keep + 1 end end
	local L = { strfmt("bag items quality>=1: %d | BoE green+ to keep for the auction house: %d", #rows, keep) }
	for i, r in ipairs(rows) do
		if i > 80 then L[#L + 1] = "... " .. (#rows - 80) .. " more"; break end
		L[#L + 1] = strfmt("%d.%d %s #%s x%d q%d bind=%s soulbound=%s slot=%s type=%s ilvl=%s stats=%s sell=%s%s", r.bag, r.slot, clean(r.name or "unknown"), val(r.id), r.count,
			r.quality, r.bind or "unknown", r.bound == nil and "unknown" or (r.bound and "yes" or "no"), r.equipLoc or "-", clean(r.armor or "-"), val(r.ilvl),
			statText(r.stats), val(r.price), r.keep and " | KEEP FOR AH" or "")
	end
	return L
end

---------------------------------------------------------------- windows: merchant, trainer, quest reward, loot
local function npcInfo(unit, kind)
	local info = C.noteNpc(unit, kind)
	if info then return info end
	local mid, x, y = U.pos()
	return { name = U.unitName(unit), map = mid, x = x, y = y }
end

function ST.collectRewards()
	local n = U.num(try(GetNumQuestChoices)) or 0
	local out = {}
	for i = 1, n do
		local name, _, num, quality, usable = try(GetQuestItemInfo, "choice", i)
		local link = U.str(try(GetQuestItemLink, "choice", i))
		local it = U.itemInfo(link)
		out[i] = { index = i, link = link, name = U.str(name) or it.name, quality = U.num(quality) or it.quality, equip_slot = it.equipLoc, armor_type = it.armor,
			bind = it.bind, stats = link and U.itemStats(link) or nil, price = it.price, count = U.num(num), usable = U.bool(usable), cached = it.cached }
	end
	return out
end

function ST.rewardLines()
	if not S.windows.quest_reward then return { "no quest reward window open" } end
	local L = {}
	local ch = S.rewardChoices or {}
	L[#L + 1] = strfmt("reward choices: %d (pick one; this list is advice only, nothing is equipped or previewed)", #ch)
	for _, r in ipairs(ch) do
		L[#L + 1] = strfmt("choice %d: %s q%s slot=%s type=%s bind=%s sell=%s usable=%s stats=%s", r.index, clean(r.name or "unknown"), val(r.quality), r.equip_slot or "-", clean(r.armor_type or "-"),
			r.bind or "unknown", val(r.price), val(r.usable), statText(r.stats))
	end
	return L
end

function ST.collectTrainer()
	local out = { all = {}, available = 0, affordable = 0 }
	if not GetNumTrainerServices then return out end
	local n = U.num(try(GetNumTrainerServices)) or 0
	local gold = U.num(try(GetMoney)) or 0
	for i = 1, n do
		local name, rank, category = try(GetTrainerServiceInfo, i)
		name = U.str(name)
		category = U.str(category)
		if name and category ~= "header" then
			local cost = U.num((try(GetTrainerServiceCost, i)))
			local req = U.num(try(GetTrainerServiceLevelReq, i))
			local rec = { index = i, name = name, rank = U.str(rank), category = category or "unknown", cost = cost, req = req }
			out.all[#out.all + 1] = rec
			if category == "available" then
				out.available = out.available + 1
				if cost and cost <= gold then out.affordable = out.affordable + 1 end
			end
		end
	end
	return out
end

function ST.trainerLines()
	if not S.windows.trainer then return { "no trainer window open" } end
	local t = S.trainerInfo or { all = {}, available = 0, affordable = 0 }
	local L = { strfmt("trainer services: %d | available %d | affordable now %d | money %s", #t.all, t.available, t.affordable, money(U.num(try(GetMoney)))) }
	for i, r in ipairs(t.all) do
		if i > 60 then L[#L + 1] = "... more"; break end
		L[#L + 1] = strfmt("[%s] %s%s cost=%s req_level=%s", r.category, clean(r.name), r.rank and r.rank ~= "" and (" (" .. clean(r.rank) .. ")") or "", money(r.cost), val(r.req))
	end
	return L
end

local function contractChoice(r)
	return { index = r.index, link = r.link, name = r.name, quality = r.quality, equip_slot = r.equip_slot, armor_type = r.armor_type, bind = r.bind, stats = r.stats, price = r.price }
end

local function openMerchant()
	S.windows.merchant = true
	local npc = npcInfo("npc", "vendor")
	C.event("window_open", { window = "merchant", npc = npc, count = U.num(try(GetMerchantNumItems)) or U.num(try(C_MerchantFrame and C_MerchantFrame.GetNumItems)), choices = J.array({}) })
end
local function openTrainer()
	S.windows.trainer = true
	S.trainerInfo = ST.collectTrainer()
	local npc = npcInfo("npc", "trainer")
	local ch = {}
	for _, r in ipairs(S.trainerInfo.all) do
		if r.category == "available" and #ch < 8 then ch[#ch + 1] = { index = r.index, name = r.name, rank = r.rank, cost = r.cost, level_req = r.req } end
	end
	S.dirtyTrainer = true
	C.event("window_open", { window = "trainer", npc = npc, count = #S.trainerInfo.all, available = S.trainerInfo.available, affordable = S.trainerInfo.affordable, choices = J.array(ch) })
end
local function openReward()
	local ch = ST.collectRewards()
	local npc = npcInfo("npc", "quest")
	if #ch == 0 then return end      -- no reward to choose between: nothing to advise on
	S.windows.quest_reward = true
	S.rewardChoices = ch
	S.dirtyRewards = true
	local out = {}
	for i, r in ipairs(ch) do out[i] = contractChoice(r) end
	C.event("window_open", { window = "quest_reward", npc = npc, choices = J.array(out) })
	-- item data often arrives a moment later: refresh the section (not the event) once
	C_Timer.After(0.6, function()
		if S.windows.quest_reward then S.rewardChoices = ST.collectRewards(); S.dirtyRewards = true end
	end)
end
local function openLoot()
	S.windows.loot = true
	local ch = {}
	for i = 1, (U.num(try(GetNumLootItems)) or 0) do
		local link = U.str(try(GetLootSlotLink, i))
		if link then
			local it = U.itemInfo(link)
			ch[#ch + 1] = { index = i, link = link, name = it.name, quality = it.quality }
		end
	end
	local npc = { name = U.exists("target") and U.unitName("target") or nil }
	C.event("window_open", { window = "loot", npc = npc, choices = J.array(ch) })
end

local function closing(which, flag)
	return function() S.windows[which] = false; S[flag] = true end
end
C.on("MERCHANT_SHOW", openMerchant)
C.on("MERCHANT_CLOSED", closing("merchant", "dirtyMerchant"))
C.on("TRAINER_SHOW", openTrainer)
C.on("TRAINER_UPDATE", function() if S.windows.trainer then S.trainerInfo = ST.collectTrainer(); S.dirtyTrainer = true end end)
C.on("TRAINER_CLOSED", closing("trainer", "dirtyTrainer"))
C.on("QUEST_COMPLETE", openReward)
C.on("QUEST_FINISHED", closing("quest_reward", "dirtyRewards"))
C.on("LOOT_OPENED", openLoot)
C.on("LOOT_CLOSED", closing("loot", "dirtyLoot"))
C.on("GOSSIP_SHOW", function() npcInfo("npc", "gossip") end)
C.on("QUEST_DETAIL", function() npcInfo("npc", "quest") end)
C.on("QUEST_GREETING", function() npcInfo("npc", "quest") end)
C.on("QUEST_PROGRESS", function() npcInfo("npc", "quest") end)
C.on("TAXIMAP_OPENED", function() npcInfo("npc", "flightmaster") end)
C.on("GET_ITEM_INFO_RECEIVED", function() S.dirtyItems = true; if S.windows.quest_reward then S.dirtyRewards = true end end)

---------------------------------------------------------------- npcs and addon session
function ST.npcLines()
	local list = {}
	for _, key in ipairs(S.npcOrder) do list[#list + 1] = S.npcs[key] end
	table.sort(list, function(a, b) return (a.last or 0) > (b.last or 0) end)
	local L = { strfmt("npcs seen this session: %d (newest 100 shown)", #list) }
	for i = 1, min(#list, 100) do
		local r = list[i]
		L[#L + 1] = strfmt("%s | id %d | map %s | x %s y %s | %s | seen %d", clean(r.name), r.id, val(r.map), val(r.x), val(r.y), r.kind or "unknown", r.count)
	end
	return L
end

function ST.sessionLines()
	local L = {}
	L[#L + 1] = strfmt("afk=%d in_combat=%d loading=%d", S.afk and 1 or 0, U.inCombat() and 1 or 0, S.loading and 1 or 0)
	L[#L + 1] = strfmt("last_login_at=%s last_logout_at=%s", val(S.loginAt), val(S.logoutAt))
	L[#L + 1] = strfmt("event_seq=%d queued=%d dropped=%d", S.seq, #S.q, S.dropped)
	L[#L + 1] = strfmt("uptime_s=%d", floor(U.now() - (S.startedAt or U.now())))
	for _, e in ipairs(S.errors) do L[#L + 1] = "addon error: " .. clean(e) end
	return L
end

---------------------------------------------------------------- scheduling
local last = {}
local function safe(fn, ...)
	local ok, r = pcall(fn, ...)
	if ok then return r end
	C.err("state", r)
	return { "ERROR: " .. tostring(r) }
end

-- push a section when its dirty flag is set (but not more often than minGap), or when maxGap has passed
local function run(name, now, minGap, maxGap, flag, builder, keepalive)
	local t = last[name] or -1e9
	local dirty = flag and S[flag]
	if (dirty and now - t >= minGap) or now - t >= maxGap then
		if flag then S[flag] = false end
		last[name] = now
		if API.push then API.push(name, safe(builder), keepalive or 120) end
	end
end

function ST.tick(now)
	if S.dirtyBags or now - (last.edges or -1e9) >= 5 then
		S.dirtyBags = false
		C.checkBags()
	end
	if S.dirtyDura or now - (last.edges or -1e9) >= 5 then
		S.dirtyDura = false
		C.checkDurability()
	end
	if now - (last.edges or -1e9) >= 5 then last.edges = now end
	if (S.dirtyBars and now - (last.bars or -1e9) >= 1.5) or now - (last.bars or -1e9) >= 30 then
		S.dirtyBars = false
		last.bars = now
		local ok, scan = pcall(ST.checkBars)
		if ok then if API.push then API.push("actionbars", safe(ST.barLines, scan), 300) end else C.err("actionbars", scan) end
		S.dirtyRules = true
	end
	run("pet", now, 2, 10, "dirtyPet", ST.petLines, 60)
	run("target", now, 0.5, 5, "dirtyTarget", ST.targetLines, 30)
	run("fights", now, 1, 60, "dirtyFights", ST.fightLines, 300)
	run("deaths", now, 1, 60, "dirtyDeaths", ST.deathLines, 300)
	run("items", now, 5, 120, "dirtyItems", ST.itemLines, 300)
	run("npcs", now, 5, 120, "dirtyNpcs", ST.npcLines, 300)
	run("rewards", now, 0.3, 30, "dirtyRewards", ST.rewardLines, 30)
	run("trainer", now, 0.3, 30, "dirtyTrainer", ST.trainerLines, 30)
	run("session_addon", now, 5, 5, nil, ST.sessionLines, 10)
	if C.rules and C.rules.sections then pcall(C.rules.sections, now, run) end
end

ST.last = last
ST.run = run
