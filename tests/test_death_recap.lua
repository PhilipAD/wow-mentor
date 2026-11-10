-- Killer detection: C_DeathRecap (forever + 1.60.1) first, fully guarded, with the nameplate heuristic as the fallback.
-- Stubs: recap present, absent, erroring, empty, secret-valued, and arriving late.
package.path = "tests/?.lua;" .. package.path
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end
local function eq(a, b, msg) check(a == b, (msg or "") .. " expected " .. tostring(b) .. " got " .. tostring(a)) end

local function scenario(label, setup, verify, usedSecrets)
	local W = dofile("tests/wowstub.lua")
	W.install()
	local w = W.installCoach()
	if usedSecrets then W.hookType() end
	W.loadAddon()
	local C, API, J = WoWFeedCoach, WoWFeed_API, WoWFeedJSON
	local S = C.S
	W.fire("PLAYER_LOGIN"); W.runTimers()
	W.fire("PLAYER_ENTERING_WORLD", true, false)
	-- two kobolds are attacking: the nameplate heuristic's answer
	w.units.nameplate1 = { exists = true, name = "Kobold Miner", level = 6, attackable = true, class = "normal", guid = "Creature-0-1-0-0-100-0001" }
	w.units.nameplate1target = { exists = true, isUnit = "player" }
	w.plates = { "nameplate1" }
	setup(W, w)
	w.combat = true
	W.fire("PLAYER_REGEN_DISABLED")
	W.advance(1); C.tick()
	w.dead = true
	local ok, err = pcall(W.fire, "PLAYER_DEAD")
	check(ok, label .. ": PLAYER_DEAD raised " .. tostring(err))
	-- decode the death event from the strip
	local ev
	local chunks = {}
	for _ = 1, 200 do
		local m = API.nextMessage()
		if not m then break end
		local sec, id, part, parts, payload = m:match("^([^\31]*)\31([^\31]*)\31([^\31]*)\31([^\31]*)\31(.*)$")
		if sec == "evj" then
			chunks[id] = chunks[id] or {}; chunks[id][tonumber(part)] = payload
			local complete = true
			for i = 1, tonumber(parts) do if not chunks[id][i] then complete = false end end
			if complete then
				for _, e in ipairs(J.decode(table.concat(chunks[id])) or {}) do if e.type == "death" then ev = ev or e end end
				chunks[id] = nil
			end
		end
	end
	check(ev, label .. ": death event sent")
	eq(#S.errors, 0, label .. ": no addon errors: " .. table.concat(S.errors, " | "))
	verify(ev and ev.data or {}, S.deaths[#S.deaths], W, w, C)
	if usedSecrets then W.unhookType() end
end

local function recapOf(events) return { HasRecapEvents = function() return #events > 0 end, GetRecapEvents = function() return events end } end

-- present and readable: three or more events, the killing blow flagged, order given by timestamp
scenario("present", function(W)
	_G.C_DeathRecap = recapOf({
		{ timestamp = 100.0, event = "SPELL_DAMAGE", spellName = "Fireball", spellId = 133, amount = 40, sourceName = "Defias Mage", sourceGUID = "Creature-0-1-0-0-5-0001", hideCaster = false },
		{ timestamp = 103.5, event = "SWING_DAMAGE", amount = 12, sourceName = "Kobold Miner", hideCaster = false },
		{ timestamp = 102.0, event = "SPELL_DAMAGE", spellName = "Frostbolt", spellId = 116, amount = 31.9, sourceName = "Defias Mage", hideCaster = false },
		{ timestamp = 104.0, event = "SPELL_DAMAGE", spellName = "Pyroblast", spellId = 11366, amount = 70, sourceName = "Defias Wizard", hideCaster = false, tombstone = true },
		{ timestamp = 101.0, event = "ENVIRONMENTAL_DAMAGE", environmentalType = "Fire", amount = 5, hideCaster = true },
	})
end, function(d, rec)
	eq(d.killer.name, "Defias Wizard", "killer comes from the recap's killing blow")
	eq(d.killer.source, "recap", "source marked")
	check(d.killer.level == nil, "the nameplate guess (a different unit) does not lend its level")
	eq(#d.last_hits, 3, "last three damage events")
	eq(d.last_hits[1].spell, "Pyroblast"); eq(d.last_hits[1].amount, 70); eq(d.last_hits[1].source, "Defias Wizard")
	eq(d.last_hits[2].spell, "Melee", "a melee swing is named Melee"); eq(d.last_hits[2].amount, 12)
	eq(d.last_hits[3].spell, "Frostbolt"); eq(d.last_hits[3].amount, 31, "amounts are whole numbers")
	eq(rec.killer.name, "Defias Wizard", "stored in the DEATHS record too")
	local lines = WoWFeedCoach.state.deathLines()
	check(table.concat(lines, "\n"):find("last_hits (newest first): Pyroblast 70 from Defias Wizard; Melee 12 from Kobold Miner; Frostbolt 31", 1, true), "DEATHS section shows the last hits: " .. table.concat(lines, " / "))
end)

-- a recap naming the same unit as the nameplate keeps the nameplate's level and class
scenario("same killer", function()
	_G.C_DeathRecap = recapOf({ { timestamp = 1, event = "SWING_DAMAGE", amount = 9, sourceName = "Kobold Miner", hideCaster = false } })
end, function(d)
	eq(d.killer.name, "Kobold Miner"); eq(d.killer.level, 6, "level kept from the nameplate"); eq(d.killer.source, "recap")
	eq(#d.last_hits, 1)
end)

-- no timestamps: the order the game gave is kept (first = newest)
scenario("no timestamps", function()
	_G.C_DeathRecap = recapOf({ { spellName = "A", amount = 1, sourceName = "One", hideCaster = false }, { spellName = "B", amount = 2, sourceName = "Two", hideCaster = false } })
end, function(d)
	eq(d.killer.name, "One"); eq(d.last_hits[1].spell, "A"); eq(d.last_hits[2].spell, "B")
end)

-- absent: the nameplate heuristic answers
scenario("absent", function() _G.C_DeathRecap = nil end, function(d)
	eq(d.killer.name, "Kobold Miner", "nameplate heuristic"); eq(d.killer.source, "nameplate"); eq(d.killer.level, 6)
	eq(#d.last_hits, 0, "no last hits without a recap")
end)

-- namespace present, functions missing
scenario("half present", function() _G.C_DeathRecap = {} end, function(d) eq(d.killer.name, "Kobold Miner"); eq(d.killer.source, "nameplate") end)

-- HasRecapEvents says no
scenario("has=false", function() _G.C_DeathRecap = { HasRecapEvents = function() return false end, GetRecapEvents = function() error("must not be called") end } end,
	function(d) eq(d.killer.name, "Kobold Miner"); eq(#d.last_hits, 0) end)

-- erroring
scenario("erroring has", function() _G.C_DeathRecap = { HasRecapEvents = function() error("boom") end, GetRecapEvents = function() return { { spellName = "X", amount = 3, sourceName = "Y", hideCaster = false } } end } end,
	function(d) eq(d.killer.name, "Y", "a failing HasRecapEvents does not hide a readable recap"); eq(d.last_hits[1].spell, "X") end)
scenario("erroring get", function() _G.C_DeathRecap = { HasRecapEvents = function() return true end, GetRecapEvents = function() error("boom") end } end,
	function(d) eq(d.killer.name, "Kobold Miner"); eq(d.killer.source, "nameplate") end)

-- garbage shapes
scenario("garbage", function() _G.C_DeathRecap = { HasRecapEvents = function() return true end, GetRecapEvents = function() return { 5, "x", {}, { amount = "many", spellName = {} } } end } end,
	function(d) eq(d.killer.name, "Kobold Miner"); eq(#d.last_hits, 0) end)
scenario("not a table", function() _G.C_DeathRecap = { HasRecapEvents = function() return true end, GetRecapEvents = function() return "nope" end } end,
	function(d) eq(d.killer.name, "Kobold Miner") end)

-- secret-valued: fields hidden -> fallback; some fields readable -> only those are used
scenario("secret fields", function(W)
	local ss, sn = W.secret("string"), W.secret("number")
	_G.C_DeathRecap = recapOf({ { timestamp = sn, event = ss, spellName = ss, spellId = sn, amount = sn, sourceName = ss, hideCaster = W.secret("boolean"), tombstone = W.secret("boolean") } })
end, function(d)
	eq(d.killer.name, "Kobold Miner", "secret recap falls back to the nameplates"); eq(d.killer.source, "nameplate"); eq(#d.last_hits, 0)
end, true)
scenario("secret partly", function(W)
	local ss = W.secret("string")
	_G.C_DeathRecap = recapOf({ { timestamp = 5, spellName = "Shadow Bolt", amount = 44, sourceName = ss, hideCaster = false }, { timestamp = 4, spellName = ss, amount = 7, sourceName = "Imp", hideCaster = false } })
end, function(d)
	eq(d.killer.name, "Imp", "the readable source wins"); eq(d.last_hits[1].spell, "Shadow Bolt"); eq(d.last_hits[1].amount, 44); check(d.last_hits[1].source == nil, "a hidden source stays unknown")
	check(d.last_hits[2].spell == nil and d.last_hits[2].amount == 7, "a hidden spell name stays unknown")
end, true)
scenario("secret table", function(W)
	_G.C_DeathRecap = { HasRecapEvents = function() return W.secret("boolean") end, GetRecapEvents = function() return W.secret("table") end }
end, function(d) eq(d.killer.name, "Kobold Miner"); eq(#d.last_hits, 0) end, true)

-- the recap is not ready at PLAYER_DEAD: it is read again 1.5 s later and enriches the DEATHS record (not the sent event)
do
	local ready = false
	scenario("late", function()
		_G.C_DeathRecap = { HasRecapEvents = function() return ready end, GetRecapEvents = function() return { { timestamp = 1, spellName = "Cleave", amount = 50, sourceName = "Late Killer", hideCaster = false } } end }
	end, function(d, rec, W, w, C)
		eq(d.killer.name, "Kobold Miner", "event sent at once with the heuristic")
		ready = true
		W.advance(2); W.runTimers()
		eq(rec.killer.name, "Late Killer", "record enriched later"); eq(rec.last_hits[1].spell, "Cleave")
	end)
end

if fails > 0 then error(fails .. " death recap test(s) failed", 0) end
io.write("death recap ok\n")
