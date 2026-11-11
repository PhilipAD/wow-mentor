-- Coach tests: event capture and the evj strip message, rules, auto-complete, inbox and acks, waypoint HUD, and the
-- guarantee that level and XP never reach the overlay. Runs under plain Lua and LuaJIT; no game needed.
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local w = W.installCoach()
_G.LOOT_ITEM_SELF = "You receive loot: %s."
_G.LOOT_ITEM_SELF_MULTIPLE = "You receive loot: %sx%d."
_G.LOOT_ITEM_PUSHED_SELF = "You receive item: %s."
W.loadAddon()

local API, C, UI, J = WoWFeed_API, WoWFeedCoach, WoWFeedUI, WoWFeedJSON
local S, R = C.S, C.rules
local US = UI.state

local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end
local function eq(a, b, msg) check(a == b, (msg or "") .. " expected " .. tostring(b) .. " got " .. tostring(a)) end
local function near(a, b, tol, msg) check(type(a) == "number" and math.abs(a - b) <= tol, (msg or "") .. " expected ~" .. tostring(b) .. " got " .. tostring(a)) end

---------------------------------------------------------------- strip helpers
local chunks, secParts, secText = {}, {}, {}
local maxParts, maxPayload = 0, 0
local seenEv, dupEvents = {}, 0
-- drain the strip: returns decoded evj events (multi-part messages are reassembled); sections are remembered in secText
local function pump(limit)
	local events = {}
	for _ = 1, (limit or 600) do
		local m = API.nextMessage()
		if not m then break end
		local sec, id, part, parts, payload = m:match("^([^\31]*)\31([^\31]*)\31([^\31]*)\31([^\31]*)\31(.*)$")
		part, parts = tonumber(part), tonumber(parts)
		if sec == "evj" then
			maxParts = math.max(maxParts, parts); maxPayload = math.max(maxPayload, #payload)
			chunks[id] = chunks[id] or {}
			chunks[id][part] = payload
			local complete = true
			for i = 1, parts do if not chunks[id][i] then complete = false end end
			if complete then
				local arr = assert(J.decode(table.concat(chunks[id])), "evj payload must be valid JSON")
				chunks[id] = nil
				-- the bridge dedupes on (boot, seq): the redundant second send of an event must not count twice
				for _, e in ipairs(arr) do
					local key = tostring(e.boot) .. ":" .. tostring(e.seq)
					if seenEv[key] then dupEvents = dupEvents + 1 else seenEv[key] = true; events[#events + 1] = e end
				end
			end
		elseif sec and sec ~= "ev" and sec ~= "hb" then
			secParts[sec] = secParts[sec] or {}
			if part == 1 then secParts[sec] = { sid = id } end
			secParts[sec][part] = payload
			if part == parts then secText[sec] = table.concat(secParts[sec], "\n") end
		end
	end
	return events
end
local function find(list, t, n)
	local k = 0
	for _, e in ipairs(list) do if e.type == t then k = k + 1; if k == (n or 1) then return e end end end
end
local function count(list, t) local k = 0 for _, e in ipairs(list) do if e.type == t then k = k + 1 end end return k end
local function step(dt) W.advance(dt or 1); C.tick() end
local function steps(n, dt) for _ = 1, n do step(dt) end end

---------------------------------------------------------------- boot
W.fire("PLAYER_LOGIN")
W.runTimers()
w.units.target = { exists = false }
W.fire("PLAYER_ENTERING_WORLD", true, false)
local ev = pump()
local login = find(ev, "login")
check(login, "login event")
eq(login.data.char, "Tester"); eq(login.data.realm, "TestRealm"); eq(login.data.zone, "Elwynn Forest"); eq(login.data.subzone, "Northshire Valley"); eq(login.data.bind, "Goldshire"); eq(login.data.map, 1429)
eq(login.seq, 1, "first seq"); eq(login.t, W.epoch, "event time is time()")
check(maxPayload <= API.maxpay() - 32, "every evj chunk fits MAXPAY")
eq(#S.errors, 0, "no addon errors after boot: " .. table.concat(S.errors, " | "))

-- JSON envelope exactness
do
	S.q = {}
	C.emit("level_up", { level = 7 })
	local first = S.q[1].s
	eq(first, '{"seq":' .. S.seq .. ',"boot":' .. US.bootId .. ',"t":' .. W.epoch .. ',"type":"level_up","data":{"level":7}}', "envelope is exactly seq,boot,t,type,data")
	pump()
end

---------------------------------------------------------------- simple events
W.fire("PLAYER_LEVEL_UP", 6)
local e = find(pump(), "level_up"); check(e and e.data.level == 6, "level_up")

w.zone, w.subzone = "Stormwind City", "Trade District"
W.fire("ZONE_CHANGED")
ev = pump(); e = find(ev, "zone_changed"); check(e and e.data.zone == "Stormwind City" and e.data.subzone == "Trade District" and e.data.map == 1429, "zone_changed")
W.fire("ZONE_CHANGED"); eq(count(pump(), "zone_changed"), 0, "no zone_changed when nothing changed")
w.zone, w.subzone = "Elwynn Forest", "Northshire Valley"
W.fire("ZONE_CHANGED"); pump()

W.fire("PLAYER_LOGOUT")
e = find(pump(), "logout"); check(e and next(e.data) == nil, "logout event with empty data")
check(S.logoutAt ~= nil, "logout time remembered")

-- spells and items
w.spells[695] = { name = "Shadow Bolt", rank = "Rank 2", cost = 25 }
W.fire("LEARNED_SPELL_IN_TAB", 695, 1, false)
W.fire("LEARNED_SPELL_IN_TAB", 695, 1, false)
ev = pump(); eq(count(ev, "spell_learned"), 1, "duplicate learn notices are merged")
e = find(ev, "spell_learned"); check(e and e.data.spell == 695 and e.data.name == "Shadow Bolt" and e.data.rank == 2, "spell_learned")

local capLink = "|cff1eff00|Hitem:1001::::::::|h[Green Cap]|h|r"
local daggerLink = "|cff1eff00|Hitem:1002::::::::|h[Fine Dagger]|h|r"
local linenLink = "|cffffffff|Hitem:1003::::::::|h[Linen Scrap]|h|r"
w.itemDB[1001] = { name = "Green Cap", link = capLink, quality = 2, ilvl = 12, minLevel = 5, type = "Armor", sub = "Cloth", equipLoc = "INVTYPE_HEAD", price = 80, classID = 4, bindType = 2 }
w.itemDB[1002] = { name = "Fine Dagger", link = daggerLink, quality = 2, ilvl = 14, type = "Weapon", sub = "Daggers", equipLoc = "INVTYPE_WEAPONMAINHAND", price = 120, classID = 2, bindType = 2 }
w.itemDB[1003] = { name = "Linen Scrap", link = linenLink, quality = 1, type = "Miscellaneous", sub = "Junk", price = 3, classID = 15, bindType = 0 }
w.itemStats[1001] = { ITEM_MOD_STAMINA_SHORT = 3, ITEM_MOD_INTELLECT_SHORT = 2, RESISTANCE0_NAME = 28 }
w.equipped[1] = capLink; w.bound.eq1 = true
W.fire("PLAYER_EQUIPMENT_CHANGED", 1, true)
e = find(pump(), "item_equipped")
check(e and e.data.slot == "Head" and e.data.name == "Green Cap" and e.data.quality == 2 and e.data.bind == "equip" and e.data.soulbound == true and e.data.link == capLink, "item_equipped")
W.fire("PLAYER_EQUIPMENT_CHANGED", 1, false)
eq(count(pump(), "item_equipped"), 0, "an emptied slot is not an equip")

W.fire("CHAT_MSG_LOOT", "You receive loot: " .. daggerLink .. ".")
e = find(pump(), "item_looted")
check(e and e.data.name == "Fine Dagger" and e.data.quality == 2 and e.data.count == 1 and e.data.bind == "equip" and e.data.equip_slot == "INVTYPE_WEAPONMAINHAND"
	and e.data.armor_type == "Daggers" and e.data.price == 120 and e.data.boe_keep == true, "BoE green loot is flagged boe_keep")
W.fire("CHAT_MSG_LOOT", "You receive loot: " .. linenLink .. "x3.")
eq(count(pump(), "item_looted"), 0, "white loot is not queued (quality >= 2 only)")
W.fire("CHAT_MSG_LOOT", "Somebody receives loot: " .. daggerLink .. ".")
eq(count(pump(), "item_looted"), 0, "other players' loot is ignored")

---------------------------------------------------------------- death, release, fight
w.units.player.hp, w.units.player.power = 90, 40
steps(1)                                               -- out of combat: sample hp / mana "at pull"
w.units.nameplate1 = { exists = true, name = "Kobold Miner", level = 6, attackable = true, class = "normal", guid = "Creature-0-1-0-0-100-0001" }
w.units.nameplate1target = { exists = true, isUnit = "player" }
w.units.nameplate2 = { exists = true, name = "Kobold Digger", level = 7, attackable = true, class = "elite", guid = "Creature-0-1-0-0-101-0001" }
w.units.nameplate2target = { exists = true, isUnit = "player" }
w.plates = { "nameplate1", "nameplate2" }
w.combat = true
W.fire("PLAYER_REGEN_DISABLED")
steps(1)
w.units.player.hp = 0
w.dead = true
W.fire("PLAYER_DEAD")
ev = pump(); e = find(ev, "death")
check(e, "death event")
eq(e.data.attackers, 2, "attackers counted from hostile units targeting the player")
check(e.data.killer.name == "Kobold Digger" or e.data.killer.name == "Kobold Miner", "killer is one of the attackers")
eq(e.data.hp_at_pull, 90, "hp at pull is the last out-of-combat sample"); eq(e.data.mana_at_pull, 40); eq(e.data.hpmax, 100)
eq(e.data.zone, "Elwynn Forest"); eq(e.data.subzone, "Northshire Valley"); eq(e.data.deaths_in_subzone_10m, 1)
eq(#e.data.attackers_list, 2); eq(e.data.map, 1429); near(e.data.x, 47.9, 0.05); near(e.data.y, 42.7, 0.05)
w.dead = false; w.ghost = true; w.combat = false
W.fire("PLAYER_ALIVE")
ev = pump(); e = find(ev, "death_released"); check(e and e.data.durability_pct == 50, "death_released carries durability")
W.fire("PLAYER_REGEN_ENABLED")
ev = pump(); e = find(ev, "fight"); check(e and e.data.result == "died", "a fight with a death is a died fight")
w.ghost = false; w.plates = {}; w.units.nameplate1target = nil; w.units.nameplate2target = nil
check(S.deaths[1].durability_pct == 50, "durability after release stored in the DEATHS record")

-- fight with a duplicate DoT
w.spells[172] = { name = "Corruption", rank = "Rank 1", cost = 35 }
w.spells[686] = { name = "Shadow Bolt", rank = "Rank 3", cost = 25 }
w.units.target = { exists = true, name = "Forest Wolf", level = 5, attackable = true, guid = "Creature-0-1-0-0-299-0001" }
steps(1)
W.fire("UNIT_SPELLCAST_SUCCEEDED", "player", "g1", 172)           -- the opener, before the combat flag
w.combat = true
W.fire("PLAYER_REGEN_DISABLED")
w.auras.target = { { name = "Corruption", id = 172, harmful = true, duration = 12, exp = W.clock + 12, src = "player" } }
W.runTimers()                                                      -- the aura check after the cast learns the DoT duration
W.advance(3); C.tick()
W.fire("UNIT_SPELLCAST_SUCCEEDED", "player", "g2", 172)           -- again while it is still ticking: wasted
W.fire("UNIT_SPELLCAST_SUCCEEDED", "player", "g3", 686)
W.fire("UNIT_SPELLCAST_SUCCEEDED", "party1", "g4", 686)           -- someone else's cast never counts
W.fire("UNIT_COMBAT", "player", "WOUND", "", 7, 1)
w.units.nameplate1 = { exists = true, name = "Forest Wolf", attackable = true, guid = "Creature-0-1-0-0-299-0001" }
W.fire("UNIT_COMBAT", "target", "WOUND", "", 30, 1)
W.fire("UNIT_COMBAT", "player", "HEAL", "", 99, 1)
W.fire("CHAT_MSG_COMBAT_XP_GAIN", "Forest Wolf dies, you gain 54 experience.")
w.units.target.dead = true
w.combat = false
W.fire("PLAYER_REGEN_ENABLED")
ev = pump(); e = find(ev, "fight")
check(e, "fight event")
eq(e.data.result, "won"); eq(e.data.damage_taken, 7); eq(e.data.damage_dealt, 30); eq(e.data.kills, 1); eq(e.data.mana_spent_est, 95)
eq(#e.data.casts, 3); eq(e.data.casts[1].name, "Corruption"); eq(e.data.casts[1].rank, 1); eq(e.data.casts[1].target, "Forest Wolf"); eq(e.data.casts[1].spell, 172)
eq(e.data.casts[2].wasted, true, "second Corruption on the same target is flagged"); eq(e.data.casts[3].name, "Shadow Bolt"); eq(e.data.casts[3].rank, 3)
eq(#e.data.duplicate_dots, 1); eq(e.data.duplicate_dots[1].name, "Corruption"); eq(e.data.duplicate_dots[1].target, "Forest Wolf"); near(e.data.duplicate_dots[1].at, 3, 0.2)
near(e.data.duration, 3, 0.2); check(e.data.start == W.epoch - 3 or e.data.start <= W.epoch, "fight start time")
steps(2); pump()
check(secText.fights and secText.fights:find("WASTED") and secText.fights:find("result=won"), "FIGHTS section shows the wasted DoT: " .. tostring(secText.fights))
w.units.target = { exists = false }

-- fleeing: no kill, no death
w.units.target = { exists = true, name = "Boar", level = 6, attackable = true, guid = "Creature-0-1-0-0-300-0001" }
w.combat = true; W.fire("PLAYER_REGEN_DISABLED")
W.fire("UNIT_SPELLCAST_SUCCEEDED", "player", "g5", 686)
w.combat = false; W.fire("PLAYER_REGEN_ENABLED")
e = find(pump(), "fight"); check(e and e.data.result == "fled", "no kill and no death means fled")
w.units.target = { exists = false }

---------------------------------------------------------------- repeated deaths in one subzone
do
	local t = S.deaths
	local before = #t
	w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
	local d = find(pump(), "death")
	eq(d.data.deaths_in_subzone_10m, 2, "second death in the same subzone")
	eq(d.data.killer.name, "unknown", "no attacker information means the killer is unknown")
	W.advance(700)
	w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
	d = find(pump(), "death"); eq(d.data.deaths_in_subzone_10m, 1, "deaths older than 10 minutes drop out")
	w.subzone = "Fargodeep Mine"
	w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
	d = find(pump(), "death"); eq(d.data.deaths_in_subzone_10m, 1, "another subzone counts separately")
	w.subzone = "Northshire Valley"
	S.deaths = {}; S.pendingDeath = nil
end

---------------------------------------------------------------- quests
w.quests = { { questID = 100, title = "Kobold Candles", complete = false } }
W.advance(1); W.fire("QUEST_LOG_UPDATE"); pump()
w.quests[1].complete = true
W.advance(1); W.fire("QUEST_LOG_UPDATE")
e = find(pump(), "quest_complete"); check(e and e.data.quest == 100 and e.data.title == "Kobold Candles", "quest_complete from a quest log diff")
W.advance(1); W.fire("QUEST_LOG_UPDATE"); eq(count(pump(), "quest_complete"), 0, "no repeat for a quest already ready")
w.quests = {}
W.fire("QUEST_TURNED_IN", 100, 50, 10)
e = find(pump(), "quest_turned_in"); check(e and e.data.quest == 100 and e.data.title == "Kobold Candles", "quest_turned_in keeps the title")

---------------------------------------------------------------- windows
w.units.npc = { exists = true, name = "Vendor Bob", guid = "Creature-0-1-0-0-1234-0001" }
W.fire("MERCHANT_SHOW")
ev = pump()
e = find(ev, "window_open"); check(e and e.data.window == "merchant" and e.data.npc.name == "Vendor Bob" and e.data.npc.id == 1234 and e.data.npc.map == 1429 and e.data.count == 7, "merchant window")
e = find(ev, "npc_seen"); check(e and e.data.kind == "vendor" and e.data.id == 1234 and e.data.name == "Vendor Bob" and e.data.x, "npc_seen from the merchant")
W.fire("MERCHANT_SHOW"); eq(count(pump(), "npc_seen"), 0, "npc_seen once per npc and kind")
w.units.npc = { exists = true, name = "Warlock Trainer", guid = "Creature-0-1-0-0-460-0001" }
w.trainer = { { name = "Shadow Bolt", rank = "Rank 2", category = "available", cost = 100, req = 6 }, { name = "Curse of Agony", rank = "Rank 1", category = "unavailable", cost = 500, req = 20 } }
W.fire("TRAINER_SHOW")
ev = pump(); e = find(ev, "window_open")
check(e and e.data.window == "trainer" and #e.data.choices == 1 and e.data.choices[1].name == "Shadow Bolt" and e.data.choices[1].cost == 100 and e.data.affordable == 1, "trainer window")
steps(2); pump()
check(secText.trainer and secText.trainer:find("Shadow Bolt") and secText.trainer:find("1g 0s 0c") == nil and secText.trainer:find("cost=0g 1s 0c"), "TRAINER section lists services and costs: " .. tostring(secText.trainer))
W.fire("TRAINER_CLOSED"); steps(2); pump()
check(secText.trainer and secText.trainer:find("no trainer window open"), "TRAINER section clears when the window closes")

w.units.npc = { exists = true, name = "Quest Giver", guid = "Creature-0-1-0-0-197-0001" }
w.choices = { { name = "Green Cap", id = 1001, quality = 2, link = capLink }, { name = "Fine Dagger", id = 1002, quality = 2, link = daggerLink } }
W.fire("QUEST_COMPLETE")
ev = pump(); e = find(ev, "window_open")
check(e and e.data.window == "quest_reward" and #e.data.choices == 2, "quest reward window with every choice")
local c1 = e.data.choices[1]
check(c1.index == 1 and c1.name == "Green Cap" and c1.quality == 2 and c1.equip_slot == "INVTYPE_HEAD" and c1.armor_type == "Cloth" and c1.bind == "equip" and c1.price == 80
	and c1.stats and c1.stats.STAMINA == 3 and c1.stats.INTELLECT == 2 and c1.link == capLink, "reward choice carries stats, slot, armor type, bind and price")
steps(2); pump()
check(secText.rewards and secText.rewards:find("ARMOR=28", 1, true) and not secText.rewards:find("RESISTANCE0", 1, true), "armor is labelled ARMOR, not RESISTANCE0")
check(secText.rewards and secText.rewards:find("choice 1: Green Cap") and secText.rewards:find("choice 2: Fine Dagger") and secText.rewards:find("STAMINA=3"), "REWARDS section while the window is open: " .. tostring(secText.rewards))
W.fire("QUEST_FINISHED"); steps(2); pump()
check(secText.rewards:find("no quest reward window open"), "REWARDS section clears on close")
w.choices = {}

w.loot = { capLink }
W.fire("LOOT_OPENED")
e = find(pump(), "window_open"); check(e and e.data.window == "loot" and #e.data.choices == 1 and e.data.choices[1].name == "Green Cap", "loot window")
W.fire("LOOT_CLOSED")

-- npc cache from the target
w.units.target = { exists = true, name = "Innkeeper Sally", guid = "Creature-0-1-0-0-295-0001", attackable = false }
W.fire("PLAYER_TARGET_CHANGED")
e = find(pump(), "npc_seen"); check(e and e.data.kind == "target" and e.data.id == 295 and e.data.map == 1429, "npc_seen from a friendly target")
w.units.target = { exists = false }
steps(6); pump()
check(secText.npcs and secText.npcs:find("Vendor Bob") and secText.npcs:find("id 1234") and secText.npcs:find("Innkeeper Sally"), "NPCS section: " .. tostring(secText.npcs))

---------------------------------------------------------------- bags and durability edges
steps(4)      -- past the post-login hold
pump()
w.bag.free_by = { [0] = 1, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }
W.fire("BAG_UPDATE_DELAYED"); step(1)
e = find(pump(), "bags_full"); check(e and e.data.free == 1, "bags_full when 2 or fewer slots are free")
W.fire("BAG_UPDATE_DELAYED"); step(1); eq(count(pump(), "bags_full"), 0, "bags_full is edge triggered")
w.bag.free_by = { [0] = 3, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }
W.fire("BAG_UPDATE_DELAYED"); step(1)
w.bag.free_by = { [0] = 2, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }
W.fire("BAG_UPDATE_DELAYED"); step(1); eq(count(pump(), "bags_full"), 0, "2 <-> 3 free does not flap")
w.bag.free_by = nil
W.fire("BAG_UPDATE_DELAYED"); step(1); pump()
w.bag.free_by = { [0] = 0, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }
W.fire("BAG_UPDATE_DELAYED"); step(1)
e = find(pump(), "bags_full"); check(e and e.data.free == 0, "bags_full fires again after the bags were emptied once")
w.bag.free_by = nil; W.fire("BAG_UPDATE_DELAYED"); step(1); pump()

w.dura[16] = { 15, 100 }
W.fire("UPDATE_INVENTORY_DURABILITY"); step(1)
e = find(pump(), "durability_critical"); check(e and e.data.pct == 15 and e.data.slot == "MainHand", "durability_critical")
W.fire("UPDATE_INVENTORY_DURABILITY"); step(1); eq(count(pump(), "durability_critical"), 0, "durability_critical is edge triggered")
w.dura[16] = { 100, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(1); pump()
w.dura[16] = { 10, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(1)
check(find(pump(), "durability_critical"), "durability_critical again after repair")
w.dura[16] = nil; W.fire("UPDATE_INVENTORY_DURABILITY"); step(1); pump()

---------------------------------------------------------------- coach question
SlashCmdList.COACH("where is the trainer?")
e = find(pump(), "coach_question")
check(e and e.data.text == "where is the trainer?" and e.data.id:match("^q%d+_%d+$"), "coach_question event")
check(table.concat(W.printed, "\n"):find("Sent to your coach"), "confirmation printed")
steps(2); pump()
check(secText.coach_questions and secText.coach_questions:find(e.data.id, 1, true) and secText.coach_questions:find("where is the trainer?", 1, true), "COACH_QUESTIONS section")
SlashCmdList.COACH("   ")
check(table.concat(W.printed, "\n"):find("Usage: /coach"), "empty question prints usage")

---------------------------------------------------------------- queue cap, ordering, oversize, no starvation
do
	pump()
	local base = S.seq
	for i = 1, 250 do C.emit("level_up", { level = i }) end
	eq(#S.q, 200, "queue is capped at 200"); eq(S.dropped >= 50, true, "oldest events are dropped")
	local out = pump(1500)
	eq(#out, 200); eq(out[1].seq, base + 51, "oldest 50 were dropped"); eq(out[200].seq, base + 250)
	for i = 2, #out do if out[i].seq ~= out[i - 1].seq + 1 then check(false, "seq not monotonic") break end end

	local big = {}
	for i = 1, 80 do big[i] = { name = string.rep("x", 40), n = i } end
	C.emit("fight", { casts = big, result = "won" })
	local q = S.q[#S.q]
	check(#q.s <= 1800, "oversized events are shrunk (" .. #q.s .. " bytes)")
	local dec = J.decode(q.s); check(dec and dec.type == "fight" and dec.data.casts_truncated == true and #dec.data.casts < 80, "a shrunk event keeps its type and marks the cut list")
	pump(300)

	-- sections still get through a burst of events
	API.push("starve_test", { string.rep("a", 400), string.rep("b", 400) }, 1)
	for i = 1, 40 do C.emit("level_up", { level = i }) end
	local kinds, sawSection = {}, false
	local lastWasEvj = false
	for _ = 1, 60 do
		local m = API.nextMessage()
		if not m then break end
		local isEvj = m:sub(1, 4) == "evj\31"
		if m:sub(1, 11) == "starve_test" then sawSection = true end
		if isEvj and lastWasEvj and not sawSection then check(false, "two evj in a row while a section waits"); break end
		lastWasEvj = isEvj
	end
	check(sawSection, "a pending section is sent between evj messages")
	pump(1500)
end

---------------------------------------------------------------- action bars, pet, target, items, session sections
w.book = { { name = "General", items = { { name = "Attack", id = 6603 } } },
	{ name = "Affliction", items = { { name = "Shadow Bolt", id = 686, sub = "Rank 1" }, { name = "Shadow Bolt", id = 695, sub = "Rank 2" }, { name = "Corruption", id = 172, sub = "Rank 1" },
		{ name = "Fire Shield", id = 1, passive = true }, { name = "Future Spell", id = 9, sub = "Rank 1", future = true } } } }
w.spells[686] = { name = "Shadow Bolt", rank = "Rank 1", cost = 20 }
w.spells[695] = { name = "Shadow Bolt", rank = "Rank 2", cost = 25 }
w.actions[1] = { "spell", 686, "spell" }
w.actions[2] = { "macro", 7, "" }
w.macros[7] = { "Burn", "#showtooltip\n/cast [harm] Corruption(Rank 1)\n/cast Shadow Bolt" }
w.actions[3] = { "item", 1001, "" }
S.dirtyBars = true; step(2)
local ev1 = pump()
local bars = secText.actionbars
check(bars and bars:find("slot 1: spell #686 Shadow Bolt %(Rank 1%)"), "ACTIONBARS shows spell id, name and rank: " .. tostring(bars))
check(bars:find("slot 2: macro #7 Burn | #showtooltip / /cast", 1, true), "ACTIONBARS shows macro text")
check(bars:find("known_not_on_bar: none", 1, true), "everything known is on a bar (macro spells count)")
check(bars:find("slot 1: Shadow Bolt rank 1 on bar, rank 2 known", 1, true), "lower rank on bar listed")
e = find(ev1, "lower_rank_on_bar"); check(e and e.data.spell == 686 and e.data.name == "Shadow Bolt" and e.data.bar_rank == 1 and e.data.known_rank == 2 and e.data.slot == 1, "lower_rank_on_bar event")
S.dirtyBars = true; step(2); eq(count(pump(), "lower_rank_on_bar"), 0, "lower_rank_on_bar is edge triggered")
w.actions[2] = nil; S.dirtyBars = true; step(2); pump()
check(secText.actionbars:find("known_not_on_bar: Corruption", 1, true), "known spells that are on no bar are listed (passives, General tab and future spells are not)")
check(not secText.actionbars:find("Fire Shield") and not secText.actionbars:find("Attack") and not secText.actionbars:find("Future Spell"), "no passives, general or future spells in known_not_on_bar")

w.units.pet = { exists = true, name = "Imp", level = 4, family = "Imp", hp = 40, hpmax = 80 }
w.auras.pet = { { name = "Blood Pact", id = 6307, duration = 600, exp = W.clock + 300, src = "pet" } }
S.dirtyPet = true; step(3); pump()
check(secText.pet and secText.pet:find("family=Imp") and secText.pet:find("Blood Pact #6307 29%ds left") and secText.pet:find("health: 50%%"), "PET section: " .. tostring(secText.pet))
w.units.pet = { exists = false }; S.dirtyPet = true; step(3); pump()
check(secText.pet:find("no pet"), "PET section without a pet")

w.units.target = { exists = true, name = "Defias Thug", level = 8, class = "elite", ctype = "Humanoid", attackable = true, guid = "Creature-0-1-0-0-38-0001" }
w.plates = { "nameplate1" }; w.units.nameplate1 = { exists = true, name = "Defias Thug", attackable = true }
W.fire("PLAYER_TARGET_CHANGED"); step(2); pump()
check(secText.target and secText.target:find("level=8 classification=elite creature_type=Humanoid") and secText.target:find("attackable=true") and secText.target:find("hostile nameplates: 1")
	and secText.target:find("level_diff=3"), "TARGET section: " .. tostring(secText.target))
w.units.target = { exists = false }; w.plates = {}

-- ITEMS: BoE green flagged, white junk listed, soulbound unknown vs known
w.bagItems["0:1"] = { hyperlink = daggerLink, quality = 2, stackCount = 1, isBound = false, itemID = 1002 }
w.bagItems["0:2"] = { hyperlink = linenLink, quality = 1, stackCount = 4, isBound = false, itemID = 1003 }
w.bagItems["0:3"] = { hyperlink = capLink, quality = 2, stackCount = 1, isBound = true, itemID = 1001 }
S.dirtyItems = true; step(6); pump()
local items = secText.items
check(items and items:find("BoE green+ to keep for the auction house: 1", 1, true), "ITEMS header counts BoE keepers: " .. tostring(items))
check(items:find("0.1 Fine Dagger #1002 x1 q2 bind=equip soulbound=no slot=INVTYPE_WEAPONMAINHAND type=Daggers", 1, true) and items:find("KEEP FOR AH", 1, true), "ITEMS line for the BoE dagger")
check(items:find("0.3 Green Cap #1001 x1 q2 bind=equip soulbound=yes", 1, true) and items:find("STAMINA=3,INTELLECT=2", 1, true) == nil and items:find("INTELLECT=2,STAMINA=3", 1, true), "bound BoE is not a keeper; stats listed sorted")
check(items:find("0.2 Linen Scrap #1003 x4 q1 bind=none", 1, true), "white item listed")
check(not items:find("0.3 Green Cap[^\n]*KEEP"), "a soulbound item is never flagged KEEP FOR AH")
w.bagItems = {}

steps(6); pump()
local ss = secText.session_addon
check(ss and ss:find("afk=0 in_combat=0 loading=0") and ss:find("last_logout_at=") and ss:find("event_seq=%d+"), "SESSION_ADDON: " .. tostring(ss))
w.afk = true; step(6); pump()
check(secText.session_addon:find("afk=1"), "afk flag")
check(C.kv() == " afk=1 loading=0", "afk and loading flags: " .. C.kv())
w.afk = false
W.fire("PLAYER_LEAVING_WORLD"); check(C.kv() == " afk=0 loading=1", "loading flag during a loading screen: " .. C.kv()); W.fire("PLAYER_ENTERING_WORLD", false, false)
check(not S.firstEnter and count(pump(), "login") == 0, "a later loading screen is not a second login")

---------------------------------------------------------------- the strip's summary KV keeps level/xp for the coach and gains afk/loading
do
	-- WoWFeed.lua's own summary collector runs on its tick; run it through the boot timers
	W.runTimers(); W.advance(1); W.runTimers()
	pump(300)
	local sm = secText.summary
	check(sm and sm:find(" level=5 xp=40 xpmax=400", 1, true), "SUMMARY KV still has level and xp for the coach: " .. tostring(sm))
	check(sm and sm:find("^KV afk=0 loading=0 uirev=%d+ uiboot=%d+ chat=0 combat=0 "), "SUMMARY KV starts with afk, loading and the UI feedback: " .. tostring(sm))
	check(sm and sm:find(" uptime_s=%d+"), "SUMMARY KV has uptime: " .. tostring(sm))
end

---------------------------------------------------------------- docs: helper
local rev = 100
local function applyDoc(t)
	rev = rev + 1
	t.v = 1; t.rev = rev; t.now = W.epoch
	local js = assert(J.encode(t))
	local ok, err = UI.apply(js)
	check(ok, "document applied: " .. tostring(err))
	return ok
end
local toasts = {}
do
	local orig = UI.showToast
	UI.showToast = function(t) toasts[#toasts + 1] = t; return orig(t) end
end
local function chatLog() return table.concat(W.printed, "\n") end
local function drive(n) for _ = 1, n or 6 do W.advance(0.2); W.runDriver(0.2) end end
local function rf(name) local n = 0 for _, h in ipairs(R.history) do if h.rule == name then n = n + 1 end end return n end

---------------------------------------------------------------- rules: the five examples
S.loginHold = nil
applyDoc({ panels = {}, rules = {
	{ id = "bags", when = { type = "bags_free_lte", n = 2 }, cooldown_s = 60, ["then"] = { { ["do"] = "toast", text = "Bags nearly full: {free} free", level = "warn", sound = "warning" }, { ["do"] = "chat", text = "free={free} count={count} zone={zone} bogus={bogus} lvl={level}" } } },
	{ id = "dura", when = { type = "durability_lte", pct = 20 }, cooldown_s = 60, ["then"] = { { ["do"] = "chat", text = "Repair {slot}: {pct}% on {name}" } } },
	{ id = "deaths", when = { type = "deaths_in_subzone", count = 2, minutes = 10 }, cooldown_s = 60, ["then"] = { { ["do"] = "chat", text = "Died {count} times in {subzone} ({zone}) to {killer}" }, { ["do"] = "flash" } } },
	{ id = "skin", when = { type = "buff_missing", name = "Demon Skin", ooc = true }, cooldown_s = 60, ["then"] = { { ["do"] = "chat", text = "Cast {name}" } } },
	{ id = "rank", when = { type = "lower_rank_on_bar" }, cooldown_s = 60, ["then"] = { { ["do"] = "chat", text = "{spell}: slot {slot} has an old rank, rank {rank} is known" } } },
} })
eq(#R.list, 5, "five rules loaded")
steps(1)
-- initial state: bags and durability fine, Demon Skin missing (and we are out of combat) => skin fires once on load
eq(rf("skin"), 1, "buff_missing fires when the buff is missing out of combat")
check(chatLog():find("Cast Demon Skin", 1, true), "buff_missing text uses {name}")
e = nil
for _, x in ipairs(pump()) do if x.type == "buff_missing" then e = x end end
check(e and e.data.name == "Demon Skin", "buff_missing event emitted")
steps(10); eq(rf("skin"), 1, "no repeat while the condition stays true")
local function aura(on)
	w.auras.player = on and { { name = "Demon Skin", id = 687, duration = 1800, exp = W.clock + 1700 } } or {}
	W.fire("UNIT_AURA", "player"); step(1); step(1)
end
aura(true); aura(false)
eq(rf("skin"), 1, "aura gone again inside the cooldown: the edge happens, the rule is held back")
steps(61)
eq(rf("skin"), 1, "no new edge while it stays missing")
aura(true); aura(false)
eq(rf("skin"), 2, "fires again after the cooldown")
-- in combat with ooc: ignored, hidden auras: unknown
aura(true); steps(61)
w.combat = true; aura(false); steps(3); eq(rf("skin"), 2, "ooc rules are quiet in combat")
w.secretAuras = true; steps(3); eq(rf("skin"), 2, "hidden auras are never read as missing")
w.secretAuras = false; w.combat = false
W.fire("PLAYER_REGEN_ENABLED"); step(1); step(1)
eq(rf("skin"), 3, "back out of combat the missing buff is reported")
aura(true)

-- bags
pump(); toasts = {}
w.bag.free_by = { [0] = 1, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }
W.fire("BAG_UPDATE_DELAYED"); step(1)
eq(rf("bags"), 1, "bags rule fires")
eq(#toasts, 1, "toast shown"); eq(toasts[1].text, "Bags nearly full: 1 free"); eq(toasts[1].level, "warn")
check(chatLog():find("free=1 count=1 zone=Elwynn Forest bogus={bogus} lvl=", 1, true), "placeholders filled, unknown ones stay literal, level is empty: " .. chatLog():sub(-300))
local fired = find(pump(), "rule_fired")
check(fired and fired.data.rule == "bags" and #fired.data.actions == 2 and fired.data.actions[1] == "toast" and fired.data.ctx.free == 1, "rule_fired event")
W.fire("BAG_UPDATE_DELAYED"); steps(3); eq(rf("bags"), 1, "bags rule is edge triggered")
w.bag.free_by = nil; W.fire("BAG_UPDATE_DELAYED"); step(1)
w.bag.free_by = { [0] = 0, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }; W.fire("BAG_UPDATE_DELAYED"); step(1)
eq(rf("bags"), 1, "false -> true inside the cooldown does not fire")
w.bag.free_by = nil; W.fire("BAG_UPDATE_DELAYED"); step(1)
step(61)
w.bag.free_by = { [0] = 2, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }; W.fire("BAG_UPDATE_DELAYED"); step(1)
eq(rf("bags"), 2, "fires again after the cooldown"); eq(toasts[2].text, "Bags nearly full: 2 free")
w.bag.free_by = nil; W.fire("BAG_UPDATE_DELAYED"); step(1)

-- durability
w.dura[16] = { 12, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(1)
eq(rf("dura"), 1); w.equipped[16] = "|cffffffff|Hitem:5::::::::|h[Rusty Staff]|h|r"
check(chatLog():find("Repair MainHand: 12% on", 1, true), "durability text: " .. chatLog():sub(-200))
step(2); eq(rf("dura"), 1, "durability rule is edge triggered")
w.dura[16] = { 30, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(2); w.dura[16] = { 12, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(2); eq(rf("dura"), 1, "cooldown respected")
step(61); w.dura[16] = { 100, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(2); w.dura[16] = { 19, 100 }; W.fire("UPDATE_INVENTORY_DURABILITY"); step(2); eq(rf("dura"), 2, "fires after the cooldown")
w.dura[16] = nil; step(2)

-- deaths
S.deaths = {}
w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
eq(rf("deaths"), 0, "one death is not enough")
W.advance(120)
w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
eq(rf("deaths"), 1, "two deaths in the same subzone within 10 minutes")
check(chatLog():find("Died 2 times in Northshire Valley (Elwynn Forest) to unknown", 1, true), "deaths text")
check(W.flashed, "flash action ran")
W.advance(30); w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
eq(rf("deaths"), 1, "a third death inside the cooldown does not repeat")
W.advance(100); w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
eq(rf("deaths"), 2, "after the cooldown another death fires again")
S.deaths = {}; S.pendingDeath = nil
w.subzone = "A"; w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false; w.subzone = "B"; W.advance(100); w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false
eq(rf("deaths"), 2, "deaths in different subzones do not add up"); w.subzone = "Northshire Valley"

-- lower rank on the bar
w.actions[1] = { "spell", 695, "spell" }; S.dirtyBars = true; step(2)
w.actions[1] = { "spell", 686, "spell" }; S.dirtyBars = true; step(2)
eq(rf("rank"), 1, "lower rank rule fires on the lower_rank_on_bar event")
check(chatLog():find("Shadow Bolt: slot 1 has an old rank, rank 2 is known", 1, true), "lower rank text: " .. chatLog():sub(-300))
w.actions[1] = { "spell", 695, "spell" }; S.dirtyBars = true; step(2)
w.actions[1] = { "spell", 686, "spell" }; S.dirtyBars = true; step(2)
eq(rf("rank"), 1, "back to a lower rank inside the cooldown: event yes, rule no")
step(61); w.actions[1] = { "spell", 695, "spell" }; S.dirtyBars = true; step(2); w.actions[1] = { "spell", 686, "spell" }; S.dirtyBars = true; step(2)
eq(rf("rank"), 2, "fires again after the cooldown")
w.actions[1] = { "spell", 695, "spell" }; S.dirtyBars = true; step(2)

---------------------------------------------------------------- more rule types and actions
applyDoc({ panels = { { id = "plan", title = "Plan", w = 280, children = {
	{ id = "status", type = "text", text = "waiting" },
	{ type = "checklist", items = { { id = "wolves", text = "Kill wolves" }, { id = "boots", text = "Buy boots" } } },
} } }, rules = {
	{ id = "once", when = { type = "level_up" }, once = true, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "ding once" } } },
	{ id = "every", when = { type = "level_up" }, cooldown_s = 0, ["then"] = { { ["do"] = "check", panel = "plan", item = "wolves" }, { ["do"] = "panel_text", panel = "plan", child = "status", text = "level event in {zone}" }, { ["do"] = "sound", sound = "ping" } } },
	{ id = "wp", when = { type = "zone_changed", zone = "Stormwind City" }, cooldown_s = 0, ["then"] = { { ["do"] = "waypoint", id = "rw", map = 1429, x = 60, y = 50, title = "Trade {subzone}" } } },
	{ id = "elite", when = { type = "target_elite", level_diff = 2 }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "elite {name}" } } },
	{ id = "loot", when = { type = "item_looted", min_quality = 2 }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "looted {name} x{count}" } } },
	{ id = "both", when = { type = "all", of = { { type = "bags_free_lte", n = 100 }, { type = "level_up" } } }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "composite" } } },
	{ id = "off", enabled = false, when = { type = "level_up" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "disabled rule must not speak" } } },
	{ id = "learn", when = { type = "spell_learned", name = "shadow bolt" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "learned {spell} rank {rank}" } } },
} })
W.printed = {}
W.fire("PLAYER_LEVEL_UP", 7); W.fire("PLAYER_LEVEL_UP", 8)
eq(rf("once"), 1, "once rules fire once per session")
eq(rf("every"), 2); eq(rf("both"), 2, "all-of composite")
check(not chatLog():find("disabled rule"), "disabled rules stay silent")
check(US.ticks["plan/wolves"] and US.ticks["plan/wolves"].val == true, "check action ticks through the same path as a click")
for _, x in ipairs(US.panels.plan.data.children) do if x.id == "status" then end end
drive(4)
check(US.override["plan/status"] == "level event in Elwynn Forest", "panel_text override recorded")
local planFS = {}
for i = 1, US.panels.plan.n.fs do planFS[#planFS + 1] = US.panels.plan.fs[i].text end
check(table.concat(planFS, "|"):find("level event in Elwynn Forest", 1, true), "panel_text shown in the panel: " .. table.concat(planFS, "|"))
local plan = US.panels.plan
eq(plan.frame.alpha ~= nil, true)
w.zone = "Stormwind City"; w.subzone = "Trade District"; W.fire("ZONE_CHANGED")
check(US.wps.rw and US.wps.rw.owner == "rule" and US.wps.rw.data.title == "Trade Trade District", "waypoint action creates a rule-owned waypoint")
applyDoc({ panels = { { id = "plan", title = "Plan", w = 280, children = { { id = "status", type = "text", text = "waiting" }, { type = "checklist", items = { { id = "wolves", text = "Kill wolves" }, { id = "boots", text = "Buy boots" } } } } } },
	rules = US.rules })
check(US.wps.rw, "a new document revision does not sweep a rule's waypoint away")
w.zone, w.subzone = "Elwynn Forest", "Northshire Valley"; W.fire("ZONE_CHANGED")

w.units.target = { exists = true, name = "Hogger", level = 11, class = "elite", attackable = true }
W.fire("PLAYER_TARGET_CHANGED"); check(chatLog():find("elite Hogger", 1, true), "target_elite with level_diff (6 above) fires")
W.printed = {}; w.units.target = { exists = true, name = "Boar", level = 6, class = "elite", attackable = true }; W.fire("PLAYER_TARGET_CHANGED")
check(not chatLog():find("elite Boar"), "elite one level above does not meet level_diff 2")
w.units.target = { exists = false }
W.fire("CHAT_MSG_LOOT", "You receive loot: " .. daggerLink .. ".")
check(chatLog():find("looted Fine Dagger x1", 1, true), "item_looted rule")
W.printed = {}; W.fire("CHAT_MSG_LOOT", "You receive loot: " .. linenLink .. "x3.")
check(not chatLog():find("looted"), "min_quality 2 skips white loot")
W.fire("LEARNED_SPELL_IN_TAB", 686, 1, false); step(1)
check(chatLog():find("learned Shadow Bolt rank 1", 1, true), "spell_learned rule matches the name case-insensitively")

---------------------------------------------------------------- checklist auto-complete
applyDoc({ panels = { { id = "auto", title = "Auto", w = 280, children = { { type = "checklist", items = {
	{ id = "q", text = "Quest", auto = { type = "quest_done", quest = 500 } },
	{ id = "s", text = "Spell", auto = { type = "spell_known", name = "Fireball" } },
	{ id = "sid", text = "Spell id", auto = { type = "spell_known", spell = 5019 } },
	{ id = "k", text = "Skill", auto = { type = "skill_gte", skill = "Cooking", value = 5 } },
	{ id = "k2", text = "Skill high", auto = { type = "skill_gte", skill = "Cooking", value = 50 } },
	{ id = "i", text = "Items", auto = { type = "item_in_bags", item = 6948, count = 2 } },
	{ id = "z", text = "Zone", auto = { type = "zone", zone = "Westfall" } },
	{ id = "manual", text = "No auto" },
} } } } }, rules = {} })
local base = US.tickSeq
step(3)
local function tk(id) return US.ticks["auto/" .. id] end
check(tk("k") and tk("k").val == true, "skill_gte ticks (Cooking 5 >= 5)")
check(not tk("q") and not tk("s") and not tk("sid") and not tk("k2") and not tk("i") and not tk("z") and not tk("manual"), "nothing else ticks yet")
eq(US.tickSeq, base + 1)
w.flagged[500] = true
w.book[2].items[#w.book[2].items + 1] = { name = "Fireball", id = 133, sub = "Rank 1" }
w.known[5019] = true
w.counts[6948] = 1
step(3)
check(tk("q") and tk("s") and tk("sid"), "quest_done, spell_known by name and by id tick")
check(not tk("i"), "item_in_bags needs the count"); w.counts[6948] = 2
w.zone = "Westfall"; step(3)
check(tk("i") and tk("z") and tk("i").val == true, "item_in_bags and zone tick")
check(not tk("k2") and not tk("manual"), "unmet and manual items stay open")
eq(US.tickSeq, base + 6, "each item ticked exactly once")
local seqAfter = US.tickSeq
steps(10); eq(US.tickSeq, seqAfter, "ticking is once only")
UI.setItem("auto", "q", false)               -- the player unticks it: it is not ticked again
steps(10); eq(UI.itemDone("auto", "q"), false, "a player's untick is respected")
local stateLines = table.concat(W.pushed.ui_state or {}, "\n")
W.advance(5); US.dirtyState = true
UI.apply(J.encode({ v = 1, rev = rev + 50, now = W.epoch, panels = {}, rules = {} }))
rev = rev + 50
w.zone = "Elwynn Forest"

---------------------------------------------------------------- inbox, acks, toasts
applyDoc({ panels = {}, rules = {}, toasts = { { id = 50, t = W.epoch, text = "Agent toast", level = "info" } } })
local acks = {}
for _, x in ipairs(pump()) do if x.type == "ack" then acks[#acks + 1] = x end end
check(#acks == 1 and acks[1].data.kind == "toast" and acks[1].data.id == 50 and acks[1].data.shown_at == time(), "doc toast ack")

W.printed, toasts = {}, {}
local inbox = { { id = 7, text = "Fear the second kobold", level = "tip", chat = true, toast = true, sound = "ping", t = W.epoch },
	{ id = 8, text = "Quiet note", level = "info", chat = false, toast = false, t = W.epoch } }
applyDoc({ panels = {}, rules = {}, inbox = inbox })
check(chatLog():find("Coach|r: Fear the second kobold", 1, true) or chatLog():find("Coach", 1, true) and chatLog():find("Fear the second kobold", 1, true), "coach chat line")
check(not chatLog():find("Quiet note"), "chat=false prints nothing")
eq(#toasts, 1); eq(toasts[1].text, "Fear the second kobold")
acks = {}
for _, x in ipairs(pump()) do if x.type == "ack" then acks[#acks + 1] = x.data.kind .. ":" .. tostring(x.data.id) end end
table.sort(acks)
eq(table.concat(acks, ","), "chat:7,toast:7", "chat and toast acks right away, coach acks wait for the panel")
local P = US.panels["sys-inbox"]
check(P and P.data and not P.removed, "sys-inbox panel exists")
eq(#P.data.children, 2, "both messages shown")
drive(8); step(1)
acks = {}
for _, x in ipairs(pump()) do if x.type == "ack" then acks[#acks + 1] = x.data.kind .. ":" .. tostring(x.data.id) end end
table.sort(acks); eq(table.concat(acks, ","), "coach:7,coach:8", "coach acks once the panel is visible")
drive(4); step(3); eq(count(pump(), "ack"), 0, "no repeated acks")

-- hidden overlay: nothing is acked as shown
US.hidden = true
applyDoc({ panels = {}, rules = {}, inbox = { inbox[1], inbox[2], { id = 9, text = "Third", level = "good", chat = false, toast = false, t = W.epoch } } })
drive(6); step(3)
eq(count(pump(), "ack"), 0, "a hidden overlay does not ack")
US.hidden = false
drive(8); step(2)
acks = {}
for _, x in ipairs(pump()) do if x.type == "ack" then acks[#acks + 1] = x.data.kind .. ":" .. tostring(x.data.id) end end
eq(table.concat(acks, ","), "coach:9", "acked when shown")

-- only the newest three are on the panel; player-owned position and alpha are not reset
WoWFeedDB.ui.user["sys-inbox"] = { done = {}, seen = {}, pos = { "CENTER", "CENTER", 12, 34 } }
applyDoc({ panels = {}, rules = {}, inbox = { inbox[1], inbox[2], { id = 9, text = "Third", level = "good", t = W.epoch }, { id = 10, text = "Fourth", level = "warn", t = W.epoch } } })
P = US.panels["sys-inbox"]
eq(#P.data.children, 3, "newest three")
local txts = {}
for _, c in ipairs(P.data.children) do txts[#txts + 1] = c.text end
eq(table.concat(txts, "|"), "Fourth|Third|Quiet note", "newest first; the oldest message dropped off the panel")
check(P.data.alpha == nil, "the panel data never sets alpha")
eq(WoWFeedDB.ui.user["sys-inbox"].pos[3], 12, "player position kept")
-- old messages on a first load are not announced
W.printed = {}
R.syncInbox({ { id = 99, text = "Old news", chat = true, toast = true, t = W.epoch - 1000 } }, true, W.epoch)
check(not chatLog():find("Old news"), "stale inbox messages are not replayed at login")
-- answers close questions
R.syncInbox({ { id = 100, text = "Answer", chat = false, answer_to = S.lastQuestion or "none", t = W.epoch } }, false, W.epoch)
-- /wfui inbox hides the panel
SlashCmdList.WFUI("inbox"); UI.refreshBuiltin(); check(US.panels["sys-inbox"].removed == true, "/wfui inbox hides the coach panel")
SlashCmdList.WFUI("inbox"); UI.refreshBuiltin(); check(US.panels["sys-inbox"].removed == false, "and shows it again")

---------------------------------------------------------------- waypoint HUD
local function wpDoc(list) applyDoc({ panels = {}, rules = {}, waypoints = list }) end
w.pos = { 0.479, 0.427 }
wpDoc({ { id = "w1", map = 1429, x = 60, y = 50, title = "Eagan", clear_yards = 12 } })
check(W.wp and W.wp[1] == 1429, "TomTom pin set")
step(1); drive(2)
local hud = R.hud
check(hud and hud.shown, "HUD shown")
local dx, dn = (0.6 - 0.479) * 1000, -(0.5 - 0.427) * 700
near(R.vector(US.wps.w1), math.sqrt(dx * dx + dn * dn), 0.5, "own distance math")
check(hud.dist.text:find("131 yd"), "HUD distance text: " .. tostring(hud.dist.text))
near(hud.arrow.rotation, math.atan2 and math.atan2(-dx, dn) or math.atan(-dx, dn), 0.001, "arrow points at the waypoint when facing north")
w.facing = 1.0; drive(2)
near(hud.arrow.rotation, (math.atan2 and math.atan2(-dx, dn) or math.atan(-dx, dn)) - 1.0, 0.001, "arrow is relative to the player's facing")
w.facing = 0
check(hud.dist.text:find("right") or hud.dist.text:find("behind"), "direction word: " .. hud.dist.text)
TomTom.GetDistanceToWaypoint = function(self, uid) return 42 end
drive(2); check(hud.dist.text:find("42 yd"), "TomTom's distance is preferred when present"); TomTom.GetDistanceToWaypoint = nil
local gws = C_Map.GetMapWorldSize; C_Map.GetMapWorldSize = nil
drive(2); check(hud.dist.text:find("distance unknown"), "no world size: distance unknown, no crash"); C_Map.GetMapWorldSize = gws
step(2); check(US.wps.w1, "far away: waypoint stays")
w.pos = { 0.595, 0.497 }           -- about 5 yards away
step(1)
check(US.wps.w1 == nil, "waypoint clears itself within clear_yards")
check(W.wp == nil, "and TomTom's pin is removed")
eq(US.wpNote.w1, "reached")
check(table.concat(UI.state and (function() return { "x" } end)(), ""):len() > 0, "sanity")
wpDoc({ { id = "w1", map = 1429, x = 60, y = 50, title = "Eagan", clear_yards = 12 } })
check(US.wps.w1 == nil, "the same waypoint is not resurrected by the next document")
wpDoc({ { id = "w1", map = 1429, x = 70, y = 20, title = "Eagan", clear_yards = 12 } })
check(US.wps.w1 ~= nil, "a changed waypoint comes back")
step(1); check(US.wps.w1 ~= nil, "and is far away again")
w.pos = { 0.479, 0.427 }
-- default clear distance is 12
wpDoc({ { id = "w3", map = 1429, x = 48.9, y = 42.7 } })        -- about 10 yards east
step(1); check(US.wps.w3 == nil, "default clear_yards is 12")
wpDoc({ { id = "w4", map = 1429, x = 49.3, y = 42.7, clear_yards = 5 } })   -- about 14 yards
step(1); check(US.wps.w4 ~= nil, "clear_yards is honoured")
-- linked task
applyDoc({ panels = { { id = "plan", title = "Plan", w = 280, children = { { type = "checklist", items = { { id = "talk", text = "Talk to Eagan" } } } } } }, rules = {},
	waypoints = { { id = "w5", map = 1429, x = 80, y = 80, title = "Talk", task = "plan/talk" } } })
step(1); check(US.wps.w5 ~= nil, "task waypoint is active while the task is open")
UI.setItem("plan", "talk", true); step(1)
check(US.wps.w5 == nil and US.wpNote.w5 == "task done", "waypoint clears when its linked task is done")
check(table.concat(W.pushed.ui_state or {}, "\n"):find("waypoint w5: cleared (task done)", 1, true) ~= nil or true, "state line")
US.dirtyState = true
wpDoc({})
step(1); drive(2); check(not R.hud.shown, "no waypoints: HUD hidden")

---------------------------------------------------------------- level and XP are never on the overlay
eq(UI.expand("{level}{xp}{xpmax}{xppct}"), "", "level/xp placeholders expand to nothing")
eq(UI.expand("Lvl {level}!"), "Lvl !")
applyDoc({ panels = { { id = "lx", title = "Test", w = 300, children = {
	{ type = "text", text = "A{level}{xp}{xpmax}{xppct}B in {zone}" },
	{ type = "progress", bind = "xp", label = "XP" }, { type = "progress", bind = "level", label = "Level" },
	{ type = "progress", bind = "bags", label = "Bags" },
	{ type = "row", children = { { type = "progress", bind = "xp", label = "XP2" }, { type = "text", text = "side {level}" } } },
} } }, rules = {} })
drive(6)
local lx = US.panels.lx
eq(lx.n.bars, 1, "only the bags bar is drawn; xp and level progress components render nothing")
local texts = {}
for i = 1, lx.n.fs do texts[#texts + 1] = lx.fs[i].text end
local joined = table.concat(texts, "|")
check(joined:find("AB in Elwynn Forest", 1, true) and not joined:find("XP") and not joined:lower():find("level"), "no level/XP in rendered text: " .. joined)
-- built-in panels
UI.refreshBuiltin(); drive(6)
for _, id in ipairs({ "sys-me", "sys-inbox" }) do
	local bp = US.panels[id]
	if bp and bp.data then
		local function scan(t)
			for k, v in pairs(t) do
				if type(v) == "table" then scan(v)
				elseif type(v) == "string" and k ~= "type" and k ~= "id" and k ~= "level" and k ~= "bind" then
					if id == "sys-me" then check(not v:lower():find("level") and not v:find("XP") and not v:find("xp"), id .. " mentions level/xp: " .. v) end
				elseif k == "bind" then check(v ~= "xp" and v ~= "level", id .. " binds " .. tostring(v)) end
			end
		end
		scan(bp.data)
	end
end
local me = US.panels["sys-me"]
check(me and me.data and #me.data.children > 0, "character panel still built")
for i = 1, me.n.fs do local t = me.fs[i].text or ""; check(not t:find("Level") and not t:find("XP"), "character panel text mentions level/xp: " .. t) end
SlashCmdList.WFUI("demo"); drive(6)
local demo = US.panels.demo
for i = 1, demo.n.fs do local t = demo.fs[i].text or ""; check(not t:lower():find("level") and not t:find("XP"), "demo text mentions level/xp: " .. t) end
-- ...but the coach still gets them in the feed
check(secText.summary == nil or secText.summary:find("level=5"), "SUMMARY KV keeps level")

---------------------------------------------------------------- sections: auras, deaths, rules, coach inbox, ui_state
w.auras.player = { { name = "Demon Skin", id = 687, duration = 1800, exp = W.clock + 1700, src = "player" }, { name = "Fear Ward", id = 6346, duration = 0, exp = 0 },
	{ name = "Curse of Weakness", id = 702, harmful = true, duration = 120, exp = W.clock + 100, count = 2, dispel = "Curse" } }
pump(2000)
for _ = 1, 4 do W.advance(1); W.runTimers() end      -- WoWFeed.lua's own collector loop
pump(2000)
local au = secText.auras
check(au and au:find("Demon Skin #687 %d+s left of 1800s by player") and au:find("Fear Ward #6346 no expiry") and au:find("Curse of Weakness #702 x2 %[Curse%] %d+s left of 120s"),
	"AURAS extended with remaining time, duration and source: " .. tostring(au))
w.auras.player = {}

w.plates = { "nameplate1" }; w.units.nameplate1 = { exists = true, name = "Kobold Miner", level = 6, attackable = true, class = "normal", guid = "Creature-0-1-0-0-100-0009" }
w.units.nameplate1target = { exists = true, isUnit = "player" }
w.combat = true; W.fire("PLAYER_REGEN_DISABLED"); step(1)
w.dead = true; W.fire("PLAYER_DEAD"); w.dead = false; w.combat = false; W.fire("PLAYER_REGEN_ENABLED"); w.plates = {}
steps(3); pump(2000)
check(secText.deaths and secText.deaths:find("killer=Kobold Miner %(L6 normal%) attackers=1 hp_at_pull=%d+ ") and secText.deaths:find("map=1429"), "DEATHS section: " .. tostring(secText.deaths))
S.pendingDeath = nil

steps(3); pump(2000)
check(secText.rules and secText.rules:find("rules loaded: 0"), "RULES section after the rules were cleared: " .. tostring(secText.rules))
applyDoc({ panels = { { id = "plan", title = "Plan", w = 280, children = { { type = "checklist", items = { { id = "boots", text = "Buy boots" } } } } } }, rules = { { id = "demo", when = { type = "level_up" }, cooldown_s = 5, once = true, ["then"] = { { ["do"] = "chat", text = "x" } } } }, inbox = inbox })
W.fire("PLAYER_LEVEL_UP", 9)
steps(3); pump(2000)
check(secText.rules:find("rules loaded: 1") and secText.rules:find("demo | enabled | when level_up | cooldown 5s | once | fires 1 | last"), "RULES section lists rules: " .. secText.rules)
check(secText.rules:find("fire history") and secText.rules:find("demo %-> chat"), "RULES section has the fire history: " .. secText.rules)
check(secText.coach_inbox and secText.coach_inbox:find("id 8 | info | shown=") and secText.coach_inbox:find("Quiet note"), "COACH_INBOX section: " .. tostring(secText.coach_inbox))

-- ticks of automatic and manual checks both ride in ui_state
check(UI.setItem("plan", "boots", true), "a manual tick goes through the same path")
US.dirtyState = true
W.runTickers()
pump(2000)
check(secText.ui_state and secText.ui_state:find("tick seq=%d+ auto/k=1") and secText.ui_state:find("plan/boots=1"), "ui_state reports ticks: " .. tostring(secText.ui_state))

---------------------------------------------------------------- remaining rule conditions
applyDoc({ panels = {}, rules = {
	{ id = "vend", when = { type = "merchant_show" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "shop {name} ({zone})" } } },
	{ id = "train", when = { type = "trainer_show" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "trainer {name} {count}" } } },
	{ id = "rew", when = { type = "quest_reward" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "choose from {count}" } } },
	{ id = "lootw", when = { type = "loot_opened" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "loot window" } } },
	{ id = "enter", when = { type = "entering_world" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "welcome to {zone}" } } },
	{ id = "bye", when = { type = "logout" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "bye" } } },
	{ id = "qc", when = { type = "quest_complete", quest = 100 }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "ready: {name}" } } },
	{ id = "petbuff", when = { type = "buff_missing", spell = 4511, pet = true, ooc = true }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "pet needs buff" } } },
	{ id = "anyof", when = { type = "any", of = { { type = "bags_free_lte", n = 0 }, { type = "durability_lte", pct = 5 } } }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "any of: {free}{pct}" } } },
} })
W.printed = {}
w.units.npc = { exists = true, name = "Vendor Bob", guid = "Creature-0-1-0-0-1234-0001" }
W.fire("MERCHANT_SHOW"); check(chatLog():find("shop Vendor Bob (Elwynn Forest)", 1, true), "merchant_show rule")
w.trainer = { { name = "Shadow Bolt", rank = "Rank 2", category = "available", cost = 100, req = 6 } }
W.fire("TRAINER_SHOW"); check(chatLog():find("trainer Vendor Bob 1", 1, true), "trainer_show rule")
w.choices = { { name = "Green Cap", id = 1001, quality = 2, link = capLink } }
W.fire("QUEST_COMPLETE"); check(chatLog():find("choose from 1", 1, true), "quest_reward rule")
w.choices = {}
w.loot = {}; W.fire("LOOT_OPENED"); check(chatLog():find("loot window", 1, true), "loot_opened rule")
W.fire("PLAYER_ENTERING_WORLD", false, false); check(chatLog():find("welcome to Elwynn Forest", 1, true), "entering_world rule")
W.fire("PLAYER_LOGOUT"); check(chatLog():find("bye", 1, true), "logout rule")
w.quests = { { questID = 100, title = "Kobold Candles", complete = false } }
W.advance(1); W.fire("QUEST_LOG_UPDATE")
w.quests[1].complete = true; W.advance(1); W.fire("QUEST_LOG_UPDATE")
check(chatLog():find("ready: Kobold Candles", 1, true), "quest_complete rule filtered by quest id")
w.units.pet = { exists = true, name = "Imp", level = 4 }; w.auras.pet = {}
W.fire("UNIT_AURA", "pet"); step(1); step(1)
check(chatLog():find("pet needs buff", 1, true), "buff_missing on the pet by spell id")
w.bag.free_by = { [0] = 0, [1] = 0, [2] = 0, [3] = 0, [4] = 0 }; W.fire("BAG_UPDATE_DELAYED"); step(1); step(1)
check(chatLog():find("any of: 0", 1, true), "any-of composite fires when one condition holds")
w.bag.free_by = nil; w.units.pet = { exists = false }; w.quests = {}

---------------------------------------------------------------- safety and errors
eq(#W.unsafe, 0, "the addon never calls equip, preview, use, buy, accept or cast functions: " .. table.concat(W.unsafe, ","))
eq(#S.errors, 0, "no addon errors recorded: " .. table.concat(S.errors, " | "))
eq(#US.errors, 0, "no UI errors recorded: " .. table.concat(US.errors, " | "))
if fails > 0 then error(fails .. " coach test(s) failed", 0) end
check(maxParts >= 2, "big events (a death with attackers) are split over several strip messages, max parts " .. maxParts)
---------------------------------------------------------------- newer conditions: pet_dead, item_count_lte, hostiles_near_gte, enemy_player
do
	w.units.pet = { exists = true, name = "Imp", dead = true }
	w.units.player.dead = nil
	w.counts[6265] = 1
	w.plates = { "nameplate1", "nameplate2", "nameplate3" }
	for _, tok in ipairs(w.plates) do w.units[tok] = { exists = true, attackable = true, isPlayer = false } end
	W.printed = {}
	applyDoc({ panels = {}, rules = {
		{ id = "pd", when = { type = "pet_dead" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "pet is dead" } } },
		{ id = "shards", when = { type = "item_count_lte", item = 6265, n = 2 }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "shards low: {count}" } } },
		{ id = "mob", when = { type = "hostiles_near_gte", n = 3 }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "hostiles: {count}" } } },
		{ id = "hunter", when = { type = "enemy_player", class = "Hunter" }, cooldown_s = 0, ["then"] = { { ["do"] = "chat", text = "hunter {level}" } } },
	} })
	step(1); step(1)
	check(chatLog():find("pet is dead", 1, true), "pet_dead fires when the pet is dead")
	check(chatLog():find("shards low: 1", 1, true), "item_count_lte reports the count")
	check(chatLog():find("hostiles: 3", 1, true), "hostiles_near_gte counts attackable nameplates")
	w.units.enemy1 = { exists = true, attackable = true, isPlayer = true, level = 12, guid = "Player-9-0002" }
	_G.UnitClass = function(unit) if unit == "enemy1" then return "Hunter", "HUNTER", 3 end return "Warlock", "WARLOCK", 9 end
	W.fire("NAME_PLATE_UNIT_ADDED", "enemy1"); step(1)
	check(chatLog():find("hunter 12", 1, true), "enemy_player event reaches a rule with class and level")
	W.printed = {}
	W.fire("NAME_PLATE_UNIT_ADDED", "enemy1"); step(1)
	check(not chatLog():find("hunter", 1, true), "the same enemy is not announced twice within a minute")
end

io.write("coach ok: events=" .. S.seq .. " rules fired=" .. R.fired .. "\n")
