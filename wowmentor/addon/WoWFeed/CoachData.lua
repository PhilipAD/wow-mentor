-- Coach data: feed sections the data audit asked for, all read-only and for the remote coach only (nothing here is drawn).
--   DAMAGE      per-fight and session damage dealt / taken with a per-spell breakdown (Blizzard's C_DamageMeter, read after combat:
--               its numbers are secret while the fight lasts)
--   AH_PRICES   Auctionator's last-seen auction price and age for every item the coach cares about (bags, bank, and every item
--               the overlay shows, which covers the profession reagents the server puts on the squares panel)
--   BANK        bags, bank, reagent bank and mailbox contents from BagBrother's cache (BrotherBags), so the bank is known while closed
--   XP          XP per hour over the last 30 active minutes (AFK and loading time do not count) and the time to the next level
--   THREAT      the player's and the pet's threat on the pet's target (pulling aggro off the pet), plus a threat_pulled event
--   RARES       rare creatures seen on nameplates this session, plus one rare_seen event per creature
-- Every value the game hides is written as "unknown". Optional addons (Auctionator, BagBrother) that are missing just say so.
local floor, max, min = math.floor, math.max, math.min
local strfmt, tconcat = string.format, table.concat
local C = WoWFeedCoach
local API = WoWFeed_API or {}
local S, U, ST = C.S, C.U, C.state
local try, secret = U.try, U.secret

local D = {}
C.data = D

local function clean(s) if API.clean then return API.clean(s) end return tostring(s) end
local function val(v) if v == nil then return "unknown" end return tostring(v) end
local function spellName(id)
	return (U.spellName and U.spellName(id)) or U.str(try(C_Spell and C_Spell.GetSpellName, id)) or ("spell " .. tostring(id))
end

S.dmg = S.dmg or { hist = {}, due = nil, sig = nil, last = nil, overall = nil, reason = nil }
S.xpt = S.xpt or { samples = {}, active = 0, base = 0, lvl = nil, max = nil, lastT = nil, kill = 0, gained = 0, start = nil }
S.rares = S.rares or { byGuid = {}, order = {} }
S.threat = S.threat or { pulled = false }
S.docItems = S.docItems or {}

---------------------------------------------------------------- DAMAGE (C_DamageMeter)
local DM_SESSION = { overall = 0, current = 1 }
local DM_TYPE = { done = 0, taken = 7 }
local TOP_SPELLS = 8

function D.meterAvailable()
	if not (C_DamageMeter and C_DamageMeter.GetCombatSessionFromType) then return false, "no C_DamageMeter in this client" end
	if C_DamageMeter.IsDamageMeterAvailable then
		local ok, avail, reason = pcall(C_DamageMeter.IsDamageMeterAvailable)
		if ok and U.bool(avail) == false then return false, U.str(reason) or "unavailable" end
	end
	return true
end

-- the local player's total, per second and spells for one session and meter type; nil when unreadable (secret in combat)
local function meterRead(session, mtype)
	local s = try(C_DamageMeter.GetCombatSessionFromType, session, mtype)
	if type(s) ~= "table" then return nil end
	local me
	local pguid = U.guid("player")
	for _, src in ipairs(type(s.combatSources) == "table" and s.combatSources or {}) do
		if type(src) == "table" and (U.bool(src.isLocalPlayer) or (pguid and U.str(src.sourceGUID) == pguid)) then me = src; break end
	end
	local out = { duration = U.num(s.durationSeconds) or U.num(try(C_DamageMeter.GetSessionDurationSeconds, session)), total = 0, dps = 0, spells = {} }
	if not me then out.missing = true; return out end                               -- not in it (yet), or its fields are still secret
	out.total = U.num(me.totalAmount)
	out.dps = U.num(me.amountPerSecond)
	if out.total == nil then return nil end                                          -- secret: the fight is still on
	local guid = U.str(me.sourceGUID) or U.guid("player")
	local det = try(C_DamageMeter.GetCombatSessionSourceFromType, session, mtype, guid)
	if type(det) == "table" and type(det.combatSpells) == "table" then
		for _, sp in ipairs(det.combatSpells) do
			if type(sp) == "table" then
				local amt = U.num(sp.totalAmount)
				local id = U.num(sp.spellID)
				if amt and amt > 0 then
					out.spells[#out.spells + 1] = { id = id, name = id and spellName(id) or "unknown", amount = amt, overkill = U.num(sp.overkillAmount),
						from = U.str(sp.creatureName), deadly = U.bool(sp.isDeadly), avoidable = U.bool(sp.isAvoidable) }
				end
			end
		end
	end
	table.sort(out.spells, function(a, b) return a.amount > b.amount end)
	return out
end
D.meterRead = meterRead

local function spellText(r, withFrom)
	if not r or #r.spells == 0 then return "none" end
	local parts = {}
	for i = 1, min(#r.spells, TOP_SPELLS) do
		local sp = r.spells[i]
		local share = (r.total and r.total > 0) and floor(sp.amount * 100 / r.total + 0.5) or 0
		parts[#parts + 1] = strfmt("%s%s %d (%d%%)%s", clean(sp.name), (withFrom and sp.from and sp.from ~= "") and (" from " .. clean(sp.from)) or "", sp.amount, share,
			sp.avoidable and " avoidable" or "")
	end
	if #r.spells > TOP_SPELLS then parts[#parts + 1] = strfmt("+%d more", #r.spells - TOP_SPELLS) end
	return tconcat(parts, "; ")
end

-- read the meter once combat is over; called from the tick until it succeeds (or there is nothing new)
function D.readDamage()
	local ok, reason = D.meterAvailable()
	if not ok then S.dmg.reason = reason; S.dmg.due = nil; S.dirtyDamage = true; return end
	S.dmg.reason = nil
	local done = meterRead(DM_SESSION.current, DM_TYPE.done)
	local taken = meterRead(DM_SESSION.current, DM_TYPE.taken)
	local unreadable = not done or not taken or (done.missing and taken.missing)
	if unreadable and U.now() < (S.dmg.deadline or 0) then return end               -- still secret right after combat: try again next tick
	S.dmg.due = nil
	if not done then return end
	if taken and taken.missing then taken = nil end
	local got = (done.total or 0) > 0 or (taken and (taken.total or 0) > 0)
	local sig = strfmt("%s|%s", val(done.total), val(taken and taken.total))
	if got and sig ~= S.dmg.sig then
		S.dmg.sig = sig
		local fight = { t = U.epoch(), duration = done.duration, done = done, taken = taken }
		local h = S.dmg.hist
		local row = { t = fight.t, duration = done.duration, total = done.total, dps = done.dps, taken = taken and taken.total, top = done.spells[1] and done.spells[1].name }
		if S.dmg.fresh or #h == 0 then
			h[#h + 1] = row                                                           -- a fight that just ended: a new history entry
			if #h > 20 then table.remove(h, 1) end
		else
			h[#h] = row                                                               -- a late update of the same fight (duration settling, fall damage)
		end
		S.dmg.last = fight
		-- the coach's own fight record, when it is the fight that just ended (finishFight skips empty fights, so the newest may be older)
		local f = S.fights and S.fights[#S.fights]
		if f and f["end"] and U.epoch() - f["end"] <= 15 then f.meter = { total = done.total, dps = done.dps, taken = taken and taken.total } end
	end
	S.dmg.fresh = false
	S.dmg.overall = { done = meterRead(DM_SESSION.overall, DM_TYPE.done), taken = meterRead(DM_SESSION.overall, DM_TYPE.taken) }
	S.dirtyDamage = true
end

function D.damageLines()
	local L = {}
	if S.dmg.reason then return { "meter: unavailable (" .. clean(S.dmg.reason) .. ")" } end
	local f = S.dmg.last
	if not f then L[1] = "no fight read yet this session" else
		L[#L + 1] = strfmt("last fight: %s %ss dealt=%s dps=%s taken=%s", date and date("%H:%M:%S", f.t) or tostring(f.t), val(f.duration and U.round1(f.duration)),
			val(f.done.total), val(f.done.dps and U.round1(f.done.dps)), val(f.taken and f.taken.total))
		L[#L + 1] = "  dealt by spell: " .. spellText(f.done)
		L[#L + 1] = "  taken by spell: " .. spellText(f.taken, true)
	end
	local o = S.dmg.overall
	if o and o.done then
		L[#L + 1] = strfmt("session: %ss dealt=%s dps=%s taken=%s", val(o.done.duration and floor(o.done.duration)), val(o.done.total), val(o.done.dps and U.round1(o.done.dps)),
			val(o.taken and o.taken.total))
		L[#L + 1] = "  dealt by spell: " .. spellText(o.done)
		L[#L + 1] = "  taken by spell: " .. spellText(o.taken, true)
	end
	local h, parts = S.dmg.hist, {}
	for i = #h, max(1, #h - 9), -1 do parts[#parts + 1] = strfmt("%s/%ss", val(h[i].dps and U.round1(h[i].dps)), val(h[i].duration and floor(h[i].duration + 0.5))) end
	if #parts > 0 then L[#L + 1] = "recent fights dps/duration (newest first): " .. tconcat(parts, " ") end
	return L
end

C.on("PLAYER_REGEN_ENABLED", function() S.dmg.due = U.now() + 0.5; S.dmg.deadline = U.now() + 10; S.dmg.fresh = true end)
-- chain pulls: a fight that ended less than 0.5 s before the next one starts is read now (out of combat values may still be readable)
C.on("PLAYER_REGEN_DISABLED", function() if S.dmg.due and S.dmg.fresh then S.dmg.deadline = 0; pcall(D.readDamage) end end)
C.on("DAMAGE_METER_CURRENT_SESSION_UPDATED", function() if not U.inCombat() then S.dmg.due = S.dmg.due or (U.now() + 0.5) end end)
C.on("DAMAGE_METER_RESET", function() S.dmg.overall = nil; S.dirtyDamage = true end)

---------------------------------------------------------------- BANK (BagBrother cache)
local function parseItem(s)
	if type(s) ~= "string" then return nil end
	local id = tonumber(s:match("^(%d+)"))
	if not id then return nil end
	return id, tonumber(s:match(";(%d+)$")) or 1
end

-- the BagBrother record of this character, or nil. Forever characters have a surname and BagBrother keys them "Name Surname"
-- under the realm "forever" (UnitFullName there returns name, surname); elsewhere the key is the name under the normalized realm.
function D.brother()
	if type(BrotherBags) ~= "table" then return nil end
	local ids = {}
	local name, surname = try(UnitFullName, "player")
	name, surname = U.str(name), U.str(surname)
	if name and surname and surname ~= "" then ids[#ids + 1] = name .. " " .. surname end
	if name then ids[#ids + 1] = name end
	local plain = U.unitName("player")
	if plain then ids[#ids + 1] = plain end
	for _, me in ipairs(ids) do
		for k, owners in pairs(BrotherBags) do
			if k ~= "account" and type(owners) == "table" and type(owners[me]) == "table" then return owners[me] end
		end
	end
	return nil
end

-- { [id] = { bank = n, reagent = n, mail = n, bags = n } } from the cache; bag ids 0-4 are bags, 5 the reagent bag, 6+ bank tabs
function D.storage()
	local rec = D.brother()
	if not rec then return nil end
	local out = {}
	local function add(id, n, where)
		out[id] = out[id] or {}
		out[id][where] = (out[id][where] or 0) + n
	end
	for key, bag in pairs(rec) do
		if type(key) == "number" and type(bag) == "table" and type(bag.items) == "table" then
			local where = "bank"                                                       -- 6+ bank tabs (bankType set), -1 the classic bank
			if key >= 0 and key <= 4 then where = "bags" elseif key == 5 then where = "reagent_bag" elseif key == -3 then where = "reagent_bank" end
			for _, s in pairs(bag.items) do
				local id, n = parseItem(s)
				if id then add(id, n, where) end
			end
		end
	end
	if type(rec.mail) == "table" then
		for _, s in pairs(rec.mail) do local id, n = parseItem(s); if id then add(id, n, "mail") end end
	end
	return out, rec
end

function D.bankLines()
	if type(BrotherBags) ~= "table" then return { "bank: unknown (BagBrother not loaded)" } end
	local st, rec = D.storage()
	if not st then return { "bank: unknown (no BagBrother record for this character yet: open the bank once)" } end
	local rows = {}
	-- only what is away from the bags (bank, mail) plus the items the overlay shows: keeps the section small for the strip
	for id, w in pairs(st) do
		if (w.bank or 0) + (w.reagent_bank or 0) + (w.mail or 0) > 0 or S.docItems[id] then rows[#rows + 1] = { id = id, w = w } end
	end
	table.sort(rows, function(a, b) return a.id < b.id end)
	local L = { strfmt("items in the bank or mail, and overlay items: %d kinds (BagBrother cache; item ids, names via wowui db item); cached money=%s", #rows,
		U.money and U.money(rec.money) or val(rec.money)) }
	for i = 1, min(#rows, 160) do
		local r = rows[i]
		L[#L + 1] = strfmt("%d bank=%d mail=%d bags=%d", r.id, (r.w.bank or 0) + (r.w.reagent_bank or 0), r.w.mail or 0, (r.w.bags or 0) + (r.w.reagent_bag or 0))
	end
	if #rows > 160 then L[#L + 1] = strfmt("+%d more kinds", #rows - 160) end
	return L
end

---------------------------------------------------------------- AH_PRICES (Auctionator)
local AH_CALLER = "WoWFeed"
local AH_MAX = 60

local function ahApi()
	local A = Auctionator
	if type(A) ~= "table" or type(A.API) ~= "table" or type(A.API.v1) ~= "table" then return nil end
	return A.API.v1
end

-- item ids worth pricing: bags, the bank cache, and every item the overlay shows (reagents on the squares panel)
function D.priceItems()
	local ids, seen = {}, {}
	local function add(id, why) id = tonumber(id); if id and id > 0 and not seen[id] then seen[id] = why; ids[#ids + 1] = id end end
	for id in pairs(S.docItems) do add(id, "overlay") end
	local ok, rows = pcall(ST.bagItems)
	if ok and type(rows) == "table" then for _, r in ipairs(rows) do add(r.id, "bags") end end
	local st = D.storage()
	if st then for id in pairs(st) do add(id, "bank") end end
	table.sort(ids, function(a, b)
		local pa, pb = seen[a] == "overlay" and 0 or 1, seen[b] == "overlay" and 0 or 1
		if pa ~= pb then return pa < pb end
		return a < b
	end)
	return ids, seen
end

function D.ahLines()
	local v1 = ahApi()
	if not v1 or not v1.GetAuctionPriceByItemID then return { "ah prices: unknown (Auctionator not loaded)" } end
	local ids, why = D.priceItems()
	-- compact on purpose (the strip sends a big section back to back): item id, copper, days since seen, vendor sell copper, why it is listed;
	-- unpriced items are listed only when the overlay shows them
	local L, priced = {}, 0
	for i = 1, min(#ids, AH_MAX) do
		local id = ids[i]
		local p = U.num(try(v1.GetAuctionPriceByItemID, AH_CALLER, id))
		if p then priced = priced + 1 end
		if p or why[id] == "overlay" then
			local age = v1.GetAuctionAgeByItemID and U.num(try(v1.GetAuctionAgeByItemID, AH_CALLER, id))
			local vend = v1.GetVendorPriceByItemID and U.num(try(v1.GetVendorPriceByItemID, AH_CALLER, id))
			L[#L + 1] = strfmt("%d ah=%s age=%s vendor=%s %s", id, val(p), val(age), val(vend), why[id])
		end
	end
	table.insert(L, 1, strfmt("ah prices (Auctionator, last scan; copper; item ids): %d of %d items priced%s", priced, min(#ids, AH_MAX), #ids > AH_MAX and strfmt(", %d left out", #ids - AH_MAX) or ""))
	return L
end

-- the overlay document names items through its icons; collect them for pricing whenever a new document arrives
local function collectDocItems(node, out, depth)
	if type(node) ~= "table" or depth > 8 then return end
	if node.type == "icon" and tonumber(node.item) then out[tonumber(node.item)] = true end
	for _, k in ipairs({ "children", "panels" }) do
		if type(node[k]) == "table" then for _, c in ipairs(node[k]) do collectDocItems(c, out, depth + 1) end end
	end
end
D.collectDocItems = collectDocItems
local prevOnDoc = C.onDoc
C.onDoc = function(doc, ...)
	local out = {}
	pcall(collectDocItems, doc, out, 0)
	local changed = false
	for id in pairs(out) do if not S.docItems[id] then changed = true end end
	for id in pairs(S.docItems) do if not out[id] then changed = true end end
	S.docItems = out
	if changed then S.dirtyAh = true; S.dirtyBank = true end
	if prevOnDoc then return prevOnDoc(doc, ...) end
end
C.on("AUCTION_HOUSE_CLOSED", function() S.dirtyAh = true end)
C.on("BANKFRAME_CLOSED", function() S.dirtyBank = true; S.dirtyAh = true end)
C.on("MAIL_CLOSED", function() S.dirtyBank = true end)

---------------------------------------------------------------- XP rate
local XP_WINDOW = 1800

local function xpNow()
	return U.num(try(UnitXP, "player")), U.num(try(UnitXPMax, "player")), U.unitLevel("player")
end

-- cumulative XP since login and active (not AFK, not loading) seconds; one sample every 15 s or on change
function D.sampleXP(now)
	local X = S.xpt
	local xp, xpm, lvl = xpNow()
	if not (xp and xpm and lvl) then return end
	if X.lvl and lvl > X.lvl and X.max then X.base = X.base + X.max * (lvl - X.lvl) end
	X.lvl, X.max = lvl, xpm
	local cum = X.base + xp
	if not X.start then X.start = cum end
	X.gained = cum - X.start
	local smp = X.samples
	local lastS = smp[#smp]
	if not lastS or lastS.cum ~= cum or X.active - lastS.a >= 15 then smp[#smp + 1] = { a = X.active, cum = cum } end
	while #smp > 2 and smp[2].a < X.active - XP_WINDOW do table.remove(smp, 1) end
end

-- XP per active hour over the last 30 active minutes (at least 2 of them), or nil
function D.xpRate()
	local X, smp = S.xpt, S.xpt.samples
	if #smp < 2 then return nil end
	local first, last = smp[1], smp[#smp]
	local span = last.a - first.a
	if X.active - first.a > span then span = X.active - first.a end
	if span < 120 then return nil end
	return (last.cum - first.cum) * 3600 / span, span
end

function D.xpLines()
	local xp, xpm, lvl = xpNow()
	local X = S.xpt
	local rested = U.num(try(GetXPExhaustion))
	local L = {}
	L[#L + 1] = strfmt("level=%s xp=%s xpmax=%s pct=%s rested=%s", val(lvl), val(xp), val(xpm), (xp and xpm and xpm > 0) and tostring(floor(xp * 100 / xpm)) or "unknown", val(rested or 0))
	local rate, span = D.xpRate()
	local need = (xp and xpm) and (xpm - xp) or nil
	local ttl = (rate and rate > 0 and need) and floor(need * 60 / rate + 0.5) or nil
	L[#L + 1] = strfmt("xp_per_hour=%s window_min=%s time_to_level_min=%s", rate and tostring(floor(rate + 0.5)) or "unknown", span and tostring(floor(span / 60)) or "unknown", val(ttl))
	L[#L + 1] = strfmt("session: gained=%d from_kills=%d other=%d active_min=%d", X.gained or 0, X.kill, max(0, (X.gained or 0) - X.kill), floor(X.active / 60))
	return L
end

C.on("PLAYER_XP_UPDATE", function() S.dirtyXp = true end)
C.on("CHAT_MSG_COMBAT_XP_GAIN", function(msg)
	if type(msg) ~= "string" or secret(msg) then return end
	if msg:find(" dies, ") then
		local n = tonumber(msg:match("(%d+) experience"))
		if n then S.xpt.kill = S.xpt.kill + n end
	end
end)

---------------------------------------------------------------- THREAT on the pet's target
local function threat(unit, mob)
	local tanking, status, pct = try(UnitDetailedThreatSituation, unit, mob)
	return U.bool(tanking), U.num(status), U.num(pct)
end

function D.threatLines()
	if not U.exists("pet") then return { "no pet" } end
	if not U.exists("pettarget") then return { "pet has no target" } end
	local name = U.unitName("pettarget") or "unknown"
	local pt, ps, pp = threat("player", "pettarget")
	local qt, qs, qp = threat("pet", "pettarget")
	return { strfmt("pet_target=%s", clean(name)),
		strfmt("player: tanking=%s status=%s pct=%s", val(pt), val(ps), val(pp and floor(pp + 0.5))),
		strfmt("pet: tanking=%s status=%s pct=%s", val(qt), val(qs), val(qp and floor(qp + 0.5))) }
end

-- threat_pulled once per fight when the player takes the pet's target off it (the voidwalker / imp lost aggro)
function D.checkThreat()
	if not U.inCombat() then S.threat.pulled = false; return end
	if S.threat.pulled or not U.exists("pettarget") then return end
	local pt = threat("player", "pettarget")
	if pt == true then
		S.threat.pulled = true
		C.event("threat_pulled", { mob = U.unitName("pettarget"), pet = U.unitName("pet") })
	end
end

---------------------------------------------------------------- RARES on nameplates
local RARE = { rare = true, rareelite = true }

function D.scanRares()
	local plates = C_NamePlate and try(C_NamePlate.GetNamePlates)
	if type(plates) ~= "table" then return end
	for _, p in ipairs(plates) do
		local u = p.namePlateUnitToken or p.unitToken or (p.UnitFrame and p.UnitFrame.unit)
		if type(u) == "string" and not secret(u) then
			local cls = U.str(try(UnitClassification, u))
			local guid = U.guid(u)
			if cls and RARE[cls] and guid and not S.rares.byGuid[guid] then
				local map, x, y = nil, nil, nil
				if U.pos then local okp, m, a, b = pcall(U.pos); if okp then map, x, y = m, a, b end end
				local r = { name = U.unitName(u) or "unknown", level = U.unitLevel(u), cls = cls, guid = guid, t = U.epoch(), dead = U.bool(try(UnitIsDead, u)) or false,
					zone = U.str(try(GetZoneText)), map = map, x = x, y = y, npc = tonumber(guid:match("^Creature%-%d+%-%d+%-%d+%-%d+%-(%d+)")) }
				S.rares.byGuid[guid] = r
				S.rares.order[#S.rares.order + 1] = guid
				if #S.rares.order > 40 then S.rares.byGuid[table.remove(S.rares.order, 1)] = nil end
				S.dirtyRares = true
				if not r.dead then C.event("rare_seen", { name = r.name, level = r.level, classification = cls, npc = r.npc, zone = r.zone, map = map, x = x, y = y }) end
			end
		end
	end
end

function D.rareLines()
	local o = S.rares.order
	if #o == 0 then return { "no rares seen this session" } end
	local L = {}
	for i = #o, max(1, #o - 9), -1 do
		local r = S.rares.byGuid[o[i]]
		if r then
			L[#L + 1] = strfmt("%s | npc %s | L%s %s | %s map=%s x=%s y=%s | seen %s%s", clean(r.name), val(r.npc), val(r.level), r.cls, clean(r.zone or "unknown"), val(r.map), val(r.x), val(r.y),
				date and date("%H:%M", r.t) or tostring(r.t), r.dead and " (dead)" or "")
		end
	end
	return L
end

---------------------------------------------------------------- scheduling (runs after the CoachState sections)
local nextRare, nextXp, nextThreat = -1e9, -1e9, -1e9
local prevTick = ST.tick
ST.tick = function(now)
	prevTick(now)
	local run = ST.run
	local function part(name, f, ...) local ok, err = pcall(f, ...); if not ok then C.err(name, err) end end
	if S.dmg.due and now >= S.dmg.due and not U.inCombat() then part("damage", D.readDamage) end
	local X = S.xpt
	if X.lastT and not S.afk and not S.loading then X.active = X.active + min(max(now - X.lastT, 0), 5) end   -- the tick is 1 s; a stall is not play
	X.lastT = now
	if now >= nextXp or S.dirtyXp then nextXp = now + 15; part("xp", D.sampleXP, now) end
	if now >= nextRare then nextRare = now + 2; part("rares", D.scanRares) end
	if now >= nextThreat then nextThreat = now + 1; part("threat", D.checkThreat) end
	run("damage", now, 1, 120, "dirtyDamage", D.damageLines, 600)
	run("xp", now, 10, 60, "dirtyXp", D.xpLines, 300)
	run("threat", now, 1, U.inCombat() and 2 or 30, nil, D.threatLines, 30)
	run("rares", now, 2, 300, "dirtyRares", D.rareLines, 900)
	run("bank", now, 60, 900, "dirtyBank", D.bankLines, 1800)
	run("ah_prices", now, 120, 900, "dirtyAh", D.ahLines, 1800)
end
