-- The addon must never raise when the client lacks an API, an API raises, or the game hides values: everything degrades to "unknown".
-- Worlds: (1) almost every optional API missing, (2) every API raises, (3) every value is a STRICT secret (arithmetic, comparison,
-- concatenation, length, indexing, calling all raise, type() still reports the real type), once with auras readable and once hidden.
-- Boolean tests of secret booleans cannot be trapped in plain Lua; tests/test_addon_lint.lua checks those sites statically.
package.path = "tests/?.lua;" .. package.path
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local function run(label, prepare, after)
	local W = dofile("tests/wowstub.lua")
	W.install()
	W.installCoach()
	local usedSecrets = prepare(W)
	if usedSecrets then W.hookType() end
	W.loadAddon()
	local C, UI, API, J = WoWFeedCoach, WoWFeedUI, WoWFeed_API, WoWFeedJSON
	local texts = {}
	local function drain()
		for _ = 1, 600 do
			local m = API.nextMessage()
			if not m then break end
			texts[#texts + 1] = m
		end
	end
	local function ticks(n)
		for _ = 1, n do
			W.advance(1)
			C.tick()
			W.runTimers(); W.runTickers(); W.runDriver(0.2)
			drain()
		end
	end
	W.fire("PLAYER_LOGIN"); W.runTimers()
	W.fire("PLAYER_ENTERING_WORLD", true, false)
	-- every event name the addon registered, with missing or odd arguments (including secret ones)
	local names = {}
	for _, f in ipairs(W.frames) do for e in pairs(f.events) do names[#names + 1] = e end end
	table.sort(names)
	local argSets = { {}, { "player" }, { "player", "x", 1 }, { 1, 2, 3, 4 }, { "WOUND", "", "", 5 }, { "player", "WOUND", "", 12 } }
	if usedSecrets then
		local ss, sn, sb = W.secret("string"), W.secret("number"), W.secret("boolean")
		argSets[#argSets + 1] = { ss }
		argSets[#argSets + 1] = { ss, ss, sn }
		argSets[#argSets + 1] = { sn, sn, sn, sn }
		argSets[#argSets + 1] = { "player", ss, sn, sn }
		argSets[#argSets + 1] = { "player", "WOUND", sb, sn }
		argSets[#argSets + 1] = { sb, sb }
	end
	for _, e in ipairs(names) do
		for _, args in ipairs(argSets) do
			local ok, err = pcall(W.fire, e, (table.unpack or unpack)(args))
			check(ok, label .. ": event " .. e .. " raised out of its handler: " .. tostring(err))
		end
	end
	ticks(8)
	-- a realistic fight and death with whatever the world gives
	for _, step in ipairs({
		function() W.fire("PLAYER_REGEN_DISABLED") end,
		function() W.fire("UNIT_SPELLCAST_SUCCEEDED", "player", "cast-guid", 172) end,
		function() W.fire("UNIT_COMBAT", "player", "WOUND", "", 17, 1) end,
		function() W.fire("UNIT_COMBAT", "target", "WOUND", "", 30, 1) end,
		function() W.fire("PLAYER_TARGET_CHANGED") end,
		function() W.fire("CHAT_MSG_LOOT", "You receive loot: |cff1eff00|Hitem:2589:0|h[Linen Cloth]|h|r.") end,
		function() W.fire("PLAYER_DEAD") end,
		function() W.fire("PLAYER_ALIVE") end,
		function() W.fire("PLAYER_REGEN_ENABLED") end,
		function() W.fire("MERCHANT_SHOW") end,
		function() W.fire("TRAINER_SHOW") end,
		function() W.fire("QUEST_COMPLETE") end,
		function() W.fire("LOOT_OPENED") end,
		function() W.fire("BAG_UPDATE_DELAYED") end,
		function() W.fire("UPDATE_INVENTORY_DURABILITY") end,
		function() W.fire("PLAYER_EQUIPMENT_CHANGED", 5, false) end,
		function() W.fire("PLAYER_LEVEL_UP", 6) end,
	}) do
		local ok, err = pcall(step)
		check(ok, label .. ": scripted step raised " .. tostring(err))
		ticks(2)
	end
	W.runTimers()
	-- a document with rules of every kind, auto items and a waypoint
	local doc = { v = 1, rev = 5, now = W.epoch, panels = { { id = "p", title = "T", w = 280, children = { { id = "t", type = "text", text = "x {level}" },
			{ type = "checklist", items = { { id = "a", text = "a", auto = { type = "quest_done", quest = 1 } }, { id = "b", text = "b", auto = { type = "spell_known", name = "Fireball" } },
				{ id = "c", text = "c", auto = { type = "skill_gte", skill = "Cooking", value = 1 } }, { id = "d", text = "d", auto = { type = "item_in_bags", item = 1 } },
				{ id = "e", text = "e", auto = { type = "zone", zone = "Z" } } } } } } },
		waypoints = { { id = "w", map = 1, x = 10, y = 10, task = "p/a" } },
		inbox = { { id = 1, text = "hello", chat = true, toast = true, t = W.epoch } },
		rules = {} }
	local whens = { { type = "bags_free_lte", n = 3 }, { type = "durability_lte", pct = 50 }, { type = "deaths_in_subzone" }, { type = "buff_missing", name = "A", pet = true }, { type = "buff_missing", spell = 5 },
		{ type = "level_up" }, { type = "death" }, { type = "logout" }, { type = "entering_world" }, { type = "merchant_show" }, { type = "trainer_show" }, { type = "quest_reward" }, { type = "loot_opened" },
		{ type = "lower_rank_on_bar" }, { type = "spell_learned" }, { type = "quest_complete" }, { type = "zone_changed" }, { type = "target_elite" }, { type = "item_looted" }, { type = "item_equipped" },
		{ type = "any", of = { { type = "level_up" }, { type = "bags_free_lte", n = 1 } } } }
	for i, wh in ipairs(whens) do
		doc.rules[i] = { id = "r" .. i, when = wh, cooldown_s = 0, ["then"] = { { ["do"] = "toast", text = "t {free}" }, { ["do"] = "chat", text = "c" }, { ["do"] = "check", panel = "p", item = "a" },
			{ ["do"] = "waypoint", id = "rw", map = 1, x = 1, y = 1 }, { ["do"] = "panel_text", panel = "p", child = "t", text = "z" }, { ["do"] = "sound", sound = "ping" }, { ["do"] = "flash" } } }
	end
	local ok, err = UI.apply(assert(J.encode(doc)))
	check(ok, label .. ": document applied " .. tostring(err))
	for _, e in ipairs(names) do pcall(W.fire, e) end
	ticks(8)
	SlashCmdList.COACH("anything")
	if SlashCmdList.WFUI then pcall(SlashCmdList.WFUI, "dump") end
	drain()
	check(#C.S.errors == 0, label .. ": addon errors: " .. table.concat(C.S.errors, " | "))
	check(#UI.state.errors == 0, label .. ": UI errors: " .. table.concat(UI.state.errors, " | "))
	check(#W.unsafe == 0, label .. ": forbidden call " .. table.concat(W.unsafe, ","))
	-- a section collector that raised shows up as an "ERROR:" line (WoWFeed.lua safe()) or a wowfeed_error section
	local bad, seenBad = {}, {}
	for _, m in ipairs(texts) do
		if m:find("ERROR:", 1, true) or m:find("^wowfeed_error") then
			local sec = m:match("^([^\31]*)")
			if not seenBad[sec] then seenBad[sec] = true; bad[#bad + 1] = (m:gsub("\31", "|")):sub(1, 220) end
		end
	end
	check(#bad == 0, label .. ": collectors raised:\n  " .. table.concat(bad, "\n  "))
	-- the worlds must really have been exercised: sections were produced and the secret values reached the collectors
	local secs = {}
	for _, m in ipairs(texts) do secs[m:match("^([^\31]*)")] = true end
	for _, need in ipairs({ "summary", "fast", "equipment", "bags", "quests", "system" }) do check(secs[need], label .. ": section " .. need .. " was produced") end
	if after then after(W, C, texts, label) end
	W.unhookType()
	io.write(label .. " ok: " .. #names .. " events registered, queued=" .. #C.S.q .. "\n")
end

-- (1) bare client: only what loading the addon needs
run("bare", function(W)
	for _, name in ipairs({ "C_Map", "C_NamePlate", "C_Spell", "C_SpellBook", "Enum", "C_Secrets", "C_UnitAuras", "C_Item", "ItemLocation", "C_QuestLog", "GetActionInfo", "GetMacroInfo",
		"GetNumQuestChoices", "GetQuestItemInfo", "GetQuestItemLink", "GetNumTrainerServices", "GetTrainerServiceInfo", "GetNumLootItems", "GetLootSlotLink", "GetPlayerFacing",
		"GetInventoryItemLink", "GetInventoryItemDurability", "GetItemInfo", "GetItemCount", "IsPlayerSpell", "UnitGUID", "UnitClassification", "UnitCanAttack", "UnitIsUnit",
		"GetRealZoneText", "UnitIsAFK", "C_SkillInfo", "TomTom", "GetMerchantNumItems", "C_DeathRecap", "issecretvalue" }) do _G[name] = nil end
	_G.C_Container = { GetContainerNumSlots = function() return nil end }
	_G.time = function() return 1794300000 end
end)

-- (2) every optional API raises
run("throwing", function(W)
	local function boom() error("api exploded") end
	for _, name in ipairs({ "UnitName", "UnitLevel", "UnitGUID", "UnitClassification", "UnitHealth", "UnitHealthMax", "UnitPower", "UnitPowerMax", "GetZoneText", "GetRealZoneText",
		"GetSubZoneText", "GetInventoryItemDurability", "GetInventoryItemLink", "GetMoney", "UnitIsUnit", "UnitCanAttack", "UnitIsDead", "GetActionInfo", "GetMacroInfo", "GetItemInfo",
		"GetPlayerFacing", "IsPlayerSpell", "GetNumQuestChoices", "GetNumLootItems", "GetNumTrainerServices", "UnitCreatureType" }) do _G[name] = boom end
	_G.C_DeathRecap = { HasRecapEvents = boom, GetRecapEvents = boom }
	for _, tbl in ipairs({ "C_Map", "C_NamePlate", "C_Spell", "C_SpellBook", "C_Secrets", "C_UnitAuras", "C_Item", "C_QuestLog", "C_Container" }) do
		local t = _G[tbl]
		for k, v in pairs(t) do if type(v) == "function" then t[k] = boom end end
	end
	_G.ItemLocation = nil
end)

-- (3) the game hides everything: every value is a strict secret
local function secretWorld(aurasHidden, recapMode)
	return function(W)
		local ss, sn, sb = W.secret("string"), W.secret("number"), W.secret("boolean")
		local function str() return ss end
		local function num() return sn end
		local function bool() return sb end
		for _, name in ipairs({ "UnitName", "UnitClassification", "UnitCreatureType", "UnitCreatureFamily", "GetZoneText", "GetRealZoneText", "GetSubZoneText", "GetMinimapZoneText",
			"UnitGUID", "GetRealmName", "GetBindLocation" }) do _G[name] = str end
		for _, name in ipairs({ "UnitLevel", "UnitHealth", "UnitHealthMax", "UnitPower", "UnitPowerMax", "UnitPowerType", "GetMoney", "GetPlayerFacing", "UnitXP", "UnitXPMax",
			"GetXPExhaustion", "GetUnitSpeed", "GetItemCount" }) do _G[name] = num end
		for _, name in ipairs({ "UnitIsUnit", "UnitCanAttack", "UnitIsDead", "UnitIsDeadOrGhost", "UnitIsPlayer", "UnitAffectingCombat", "UnitIsAFK", "UnitIsEnemy", "UnitIsFriend",
			"UnitIsGhost", "InCombatLockdown", "IsPlayerSpell" }) do _G[name] = bool end
		_G.GetInventoryItemDurability = function() return sn, sn end
		_G.UnitClass = function() return ss, ss end
		_G.UnitRace = function() return ss, ss end
		_G.UnitPosition = function() return sn, sn, sn, sn end
		_G.UnitCastingInfo = function() return ss, ss, ss, sn, sn end
		_G.UnitChannelInfo = _G.UnitCastingInfo
		_G.GetActionInfo = function(slot) if slot <= 3 then return ss, sn, ss end end
		_G.GetMacroInfo = function() return ss, sn, ss end
		_G.GetItemInfo = function() return ss, ss, sn, sn, sn, ss, ss, sn, ss, sn, sn, sn, sn, sn end
		_G.GetInventoryItemLink = function() return ss end
		_G.GetQuestItemInfo = function() return ss, sn, sn, sn, sb, sn end
		_G.GetQuestItemLink = function() return ss end
		_G.GetLootSlotLink = function() return ss end
		_G.GetNumQuestChoices = function() return 2 end
		_G.GetNumLootItems = function() return 2 end
		_G.GetNumTrainerServices = function() return 2 end
		_G.GetTrainerServiceInfo = function() return ss, ss, ss end
		_G.GetTrainerServiceCost = num; _G.GetTrainerServiceLevelReq = num
		_G.GetMerchantNumItems = num
		_G.C_Secrets = { ShouldAurasBeSecret = function() return aurasHidden end }
		_G.C_UnitAuras = { GetAuraDataByIndex = function(_, i)
			if i == 1 then return { name = ss, spellId = sn, duration = sn, expirationTime = sn, applications = sn, sourceUnit = ss, dispelName = ss } end
			if i == 2 then return W.secret("table") end      -- a whole record the game refuses to let us read
		end }
		_G.C_Map.GetPlayerMapPosition = function() return { GetXY = function() return sn, sn end } end
		_G.C_Map.GetBestMapForUnit = num
		_G.C_Map.GetMapWorldSize = function() return sn, sn end
		_G.C_Map.GetMapInfo = function() return { name = ss } end
		_G.C_Container.GetContainerNumFreeSlots = function() return sn, sn end
		_G.C_Container.GetContainerNumSlots = function(b) if b <= 4 then return 16 end return 0 end
		_G.C_Container.GetContainerItemInfo = function() return { hyperlink = ss, quality = sn, stackCount = sn, isBound = sb, itemID = sn } end
		_G.C_Spell.GetSpellName = str; _G.C_Spell.GetSpellSubtext = str
		_G.C_Spell.GetSpellInfo = function() return { name = ss } end
		_G.C_Spell.GetSpellPowerCost = function() return { { type = sn, cost = sn } } end
		_G.C_Spell.GetSpellCooldown = function() return { startTime = sn, duration = sn, isEnabled = true } end
		_G.C_Item.GetItemStats = function() return { ITEM_MOD_STAMINA_SHORT = sn } end
		_G.C_Item.IsBound = bool
		_G.C_Item.GetItemCount = num
		_G.C_QuestLog.GetNumQuestLogEntries = function() return 2 end
		_G.C_QuestLog.GetInfo = function(i) return { title = ss, questID = sn, isHeader = false, level = sn } end
		_G.C_QuestLog.IsComplete = bool; _G.C_QuestLog.IsQuestFlaggedCompleted = bool
		_G.C_QuestLog.GetTitleForQuestID = str
		_G.C_NamePlate.GetNamePlates = function() return { { namePlateUnitToken = ss }, { namePlateUnitToken = "nameplate2" } } end
		W.world.units.target = { exists = true, name = "x" }
		W.world.units["nameplate2"] = { exists = true }
		W.world.book = { { name = "General", items = { { id = 1, name = "Fireball", sub = "Rank 1" } } } }
		-- the death recap: readable stubs elsewhere; here every field is secret, or the table itself is
		if recapMode == "fields" then
			_G.C_DeathRecap = { HasRecapEvents = bool, GetRecapEvents = function() return { { spellName = ss, spellId = sn, sourceName = ss, amount = sn, overkill = sn, timestamp = sn, event = ss, hideCaster = sb, tombstone = sb } } end }
		elseif recapMode == "table" then
			_G.C_DeathRecap = { HasRecapEvents = function() return true end, GetRecapEvents = function() return W.secret("table") end }
		end
		return true
	end
end
run("secret/auras-hidden", secretWorld(true, "fields"))
run("secret/auras-readable", secretWorld(false, "fields"))
run("secret/recap-table", secretWorld(false, "table"))

if fails > 0 then error(fails .. " degraded-mode test(s) failed", 0) end
