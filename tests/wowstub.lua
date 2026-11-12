-- Minimal WoW API stubs so addon Lua can run under plain Lua for tests.
local W = { clock = 0, epoch = 1794300000, timers = {}, tickers = {}, frames = {}, printed = {}, raised = 0, slots = {}, loaded = {}, pushed = {}, texts = {} }
local function newStub(kind)
	local o = { kind = kind, name = false, scripts = {}, events = {}, shown = true, text = "", children = {} }
	local methods = {
		GetFrameLevel = function() return 1 end, GetStringHeight = function() return 14 end, GetStringWidth = function() return 100 end,
		GetWidth = function(self) return self.w or 100 end, GetHeight = function(self) return self.h or 20 end,
		GetPoint = function() return "TOPRIGHT", nil, "TOPRIGHT", -24, -280 end, GetScale = function() return 1 end,
		GetEffectiveScale = function() return 1 end, GetAlpha = function() return 1 end,
		SetText = function(self, t) self.text = t; local x = W.texts; if x and #x < 20000 then x[#x + 1] = t end end, GetText = function(self) return self.text end,
		SetSize = function(self, w, h) self.w, self.h = w, h end, SetWidth = function(self, w) self.w = w end, SetHeight = function(self, h) self.h = h end,
		SetScript = function(self, n, f) self.scripts[n] = f end, GetScript = function(self, n) return self.scripts[n] end,
		RegisterEvent = function(self, e) self.events[e] = true end, RegisterUnitEvent = function(self, e) self.events[e] = true end,
		Show = function(self) self.shown = true end, Hide = function(self) self.shown = false end,
		IsShown = function(self) return self.shown end, IsVisible = function(self) return self.shown end,
		SetFont = function() return true end,
		CreateTexture = function(self) return newStub("texture") end, CreateFontString = function(self) return newStub("fontstring") end,
		SetAlpha = function(self, a) self.alpha = a end, SetRotation = function(self, r) self.rotation = r end, EnableMouse = function(self, v) self.mouse = v end,
	}
	return setmetatable(o, { __index = function(t, k) return methods[k] or function() end end })
end
W.newStub = newStub
function W.install()
	local G = _G
	G.UIParent = newStub("frame")
	G.CreateFrame = function(kind, name, parent) local f = newStub(kind); f.name = name or false; W.frames[#W.frames + 1] = f; if name then G[name] = f end; return f end
	G.GetTime = function() return W.clock end
	G.STANDARD_TEXT_FONT = "Fonts\\FRIZQT__.TTF"
	G.UnitName = function() return "Tester" end; G.UnitLevel = function() return 5 end
	G.UnitClass = function() return "Warlock", "WARLOCK" end; G.UnitRace = function() return "Human" end
	G.GetZoneText = function() return "Elwynn Forest" end; G.GetSubZoneText = function() return "Northshire" end
	G.UnitXP = function() return 40 end; G.UnitXPMax = function() return 400 end; G.GetMoney = function() return 12345 end
	G.C_Container = { GetContainerNumSlots = function() return 16 end, GetContainerNumFreeSlots = function() return 10 end }
	G.C_Timer = { After = function(s, f) W.timers[#W.timers + 1] = f end, NewTicker = function(s, f) W.tickers[#W.tickers + 1] = f end }
	G.InCombatLockdown = function() return false end; G.UnitAffectingCombat = function() return W.combat or false end
	G.MouseIsOver = function() return false end; G.GetInventoryItemDurability = function() return 50, 100 end
	G.IsMounted = function() return false end; G.IsInInstance = function() return false end
	G.PlaySound = function(id) W.sounds = (W.sounds or 0) + 1 end
	G.PlaySoundFile = function(path)
		local n = path:match("sig\\(%w+)%.wav")
		if n == "ok" then return true end
		if tonumber(n) and tonumber(n) <= W.raised then return true end
		return false
	end
	G.C_AddOns = { LoadAddOn = function(name) W.loaded[#W.loaded + 1] = name; G.WoWFeedInbox = W.slots[name]; return true end, GetAddOnMetadata = function(name, field) return W.meta end }
	G.SlashCmdList = {}
	G.print = function(...) W.printed[#W.printed + 1] = table.concat({ ... }, " ") end
	G.DEFAULT_CHAT_FRAME = { AddMessage = function(_, m) W.printed[#W.printed + 1] = m end }
	G.TomTom = { AddWaypoint = function(_, m, x, y, o) W.wp = { m, x, y, o.title }; return "uid1" end, RemoveWaypoint = function() W.wp = nil end }
	G.FlashClientIcon = function() W.flashed = true end
	G.GetItemInfo = function(id) return "Hearthstone", "|cffffffff|Hitem:6948|h[Hearthstone]|h|r" end
	G.C_Spell = { GetSpellLink = function(id) return "|cff71d5ff|Hspell:" .. id .. "|h[Spell]|h|r" end, GetSpellTexture = function() return 135 end }
	G.C_SkillInfo = { GetNumSkillLines = function() return 8 end, GetSkillLineInfo = function(i)
		local t = { { name = "Professions", isHeader = true }, { name = "Engineering", rank = 12, maxRank = 75 }, { name = "Secondary Skills", isHeader = true },
			{ name = "Cooking", rank = 5, maxRank = 75 }, { name = "Weapon Skills", isHeader = true }, { name = "Daggers", rank = 10, maxRank = 25 },
			{ name = "Armor Proficiencies", isHeader = true }, { name = "Cloth", rank = 1, maxRank = 1 } }
		return t[i] end }
	G.GetInventoryItemID = function(_, slot) if slot == 5 or slot == 7 or slot == 8 or slot == 16 then return 1 end end
	G.UnitCharacterPoints = function() return 2 end
	G.IsShiftKeyDown = function() return W.shift or false end
	G.WoWFeedDB = {}
end
-- Secret values as the 12.0 client models them for tainted code: any arithmetic, comparison, concatenation, length, indexing,
-- calling or table-key use raises at once; type() still reports the real type. (A boolean test cannot be trapped in plain Lua:
-- tests/test_addon_lint.lua checks those sites statically.) Use W.secret("number"), W.secret("string"), W.secret("boolean").
local realType = type
function W.isSecret(v)
	if realType(v) ~= "table" then return false end
	local m = getmetatable(v)
	return m ~= nil and m.__secret_type ~= nil
end
function W.secret(realType)
	local function bad(op) return function() error("attempt to " .. op .. " a secret " .. realType .. " value", 2) end end
	return setmetatable({}, { __secret_type = realType,
		__add = bad("perform arithmetic on"), __sub = bad("perform arithmetic on"), __mul = bad("perform arithmetic on"), __div = bad("perform arithmetic on"),
		__mod = bad("perform arithmetic on"), __pow = bad("perform arithmetic on"), __unm = bad("perform arithmetic on"), __concat = bad("concatenate"),
		__len = bad("get the length of"), __lt = bad("compare"), __le = bad("compare"), __index = bad("index"), __newindex = bad("index"),
		__call = bad("call"), __tostring = bad("convert") })
end
-- make type() report the hidden real type of a secret (as the game does); W.unhookType() restores it
local realToNumber = tonumber
function W.hookType()
	W.realtype = realType
	-- tonumber(secret number) hands the secret back (it cannot be turned into a plain number)
	_G.tonumber = function(v, ...)
		if realType(v) == "table" and W.isSecret(v) then return v end
		return realToNumber(v, ...)
	end
	_G.type = function(v)
		if realType(v) == "table" then
			local m = getmetatable(v)
			if m and m.__secret_type then return m.__secret_type end
		end
		return realType(v)
	end
end
function W.unhookType() _G.type = realType; _G.tonumber = realToNumber end

-- Faster, deterministic fake world for the coach tests (tests/test_coach.lua). Existing tests never call this.
-- Everything the addon reads comes from the W.world table, so a test changes the world and fires events.
function W.installCoach()
	local G = _G
	W.epoch = 1794300000
	G.time = function() return math.floor(W.epoch) end
	G.date = os.date
	G.GetXPExhaustion = function() return 0 end
	G.GetPhysicalScreenSize = function() return 2560, 1600 end      -- a typical Mac window: MAXPAY 261 bytes
	G.issecretvalue = function(v) return W.isSecret(v) end
	local w = {
		combat = false, dead = false, ghost = false, afk = false, level = 5, zone = "Elwynn Forest", subzone = "Northshire Valley",
		map = 1429, pos = { 0.479, 0.427 }, facing = 0, mapSize = { 1000, 700 }, money = 12345, powerType = 0,
		units = {}, plates = {}, spells = {}, book = {}, actions = {}, macros = {}, auras = {}, secretAuras = false,
		bag = { slots = 16, free = 10 }, bagItems = {}, itemDB = {}, itemStats = {}, equipped = {}, dura = {}, bound = {},
		quests = {}, flagged = {}, choices = {}, trainer = {}, loot = {}, known = {}, counts = {}, skills = {},
	}
	W.world = w
	w.units.player = { exists = true, name = "Tester", level = 5, hp = 100, hpmax = 100, power = 50, powermax = 60, guid = "Player-1-0001" }
	w.units.pet = { exists = false }
	w.units.target = { exists = false }
	local function u(unit) return w.units[unit] or { exists = false } end
	G.UnitExists = function(unit) return u(unit).exists == true end
	G.UnitName = function(unit) return u(unit).name end
	G.UnitLevel = function(unit) return u(unit).level end
	G.UnitGUID = function(unit) return u(unit).guid end
	G.UnitClassification = function(unit) return u(unit).class or "normal" end
	G.UnitCreatureType = function(unit) return u(unit).ctype end
	G.UnitCreatureFamily = function(unit) return u(unit).family end
	G.UnitCanAttack = function(a, b) return u(b).attackable == true end
	G.UnitIsDead = function(unit) return u(unit).dead == true end
	G.UnitIsPlayer = function(unit) return u(unit).isPlayer == true end
	G.UnitIsUnit = function(a, b)
		if a == b then return true end
		if u(a).isUnit == b or u(b).isUnit == a then return true end
		return false
	end
	G.UnitHealth = function(unit) return u(unit).hp end
	G.UnitHealthMax = function(unit) return u(unit).hpmax end
	G.UnitPower = function(unit) return u(unit).power end
	G.UnitPowerMax = function(unit) return u(unit).powermax end
	G.UnitPowerType = function() return w.powerType end
	G.UnitIsDeadOrGhost = function() return w.dead or w.ghost end
	G.UnitIsAFK = function() return w.afk end
	G.UnitAffectingCombat = function() return w.combat end
	G.InCombatLockdown = function() return w.combat end
	G.GetZoneText = function() return w.zone end
	G.GetRealZoneText = function() return w.zone end
	G.GetSubZoneText = function() return w.subzone end
	G.GetMinimapZoneText = function() return w.subzone end
	G.GetRealmName = function() return "TestRealm" end
	G.GetBindLocation = function() return "Goldshire" end
	G.GetPlayerFacing = function() return w.facing end
	G.GetMoney = function() return w.money end
	G.C_Map = {
		GetBestMapForUnit = function() return w.map end,
		GetPlayerMapPosition = function() local p = w.pos; return { GetXY = function() return p[1], p[2] end } end,
		GetMapInfo = function() return { name = w.zone } end,
		GetMapWorldSize = function() return w.mapSize[1], w.mapSize[2] end,
	}
	w.plateFrames = {}
	G.C_NamePlate = { GetNamePlates = function() local t = {}; for i, tok in ipairs(w.plates) do t[i] = { namePlateUnitToken = tok } end; return t end,
		GetNamePlateForUnit = function(tok) for _, t in ipairs(w.plates) do if t == tok then w.plateFrames[tok] = w.plateFrames[tok] or newStub("Frame"); return w.plateFrames[tok] end end return nil end }
	G.C_Spell = {
		GetSpellName = function(id) return w.spells[id] and w.spells[id].name end,
		GetSpellSubtext = function(id) return w.spells[id] and w.spells[id].rank end,
		GetSpellPowerCost = function(id) local s = w.spells[id]; if s and s.cost then return { { type = 0, cost = s.cost } } end end,
		GetSpellLink = G.C_Spell.GetSpellLink, GetSpellTexture = G.C_Spell.GetSpellTexture,
	}
	G.Enum = { SpellBookSpellBank = { Player = 0, Pet = 1 }, SpellBookItemType = { Spell = 1, FutureSpell = 2 } }
	G.C_SpellBook = {
		GetNumSpellBookSkillLines = function() return #w.book end,
		GetSpellBookSkillLineInfo = function(i)
			local off = 0
			for k = 1, i - 1 do off = off + #w.book[k].items end
			return { name = w.book[i].name, itemIndexOffset = off, numSpellBookItems = #w.book[i].items }
		end,
		GetSpellBookItemInfo = function(idx)
			local n = 0
			for _, tab in ipairs(w.book) do
				for _, it in ipairs(tab.items) do
					n = n + 1
					if n == idx then return { name = it.name, spellID = it.id, subName = it.sub, isPassive = it.passive, itemType = it.future and 2 or 1 } end
				end
			end
		end,
	}
	G.GetActionInfo = function(slot) local a = w.actions[slot]; if a then return a[1], a[2], a[3] end end
	G.GetMacroInfo = function(id) local m = w.macros[id]; if m then return m[1], 1, m[2] end end
	G.C_Secrets = { ShouldAurasBeSecret = function() return w.secretAuras end }
	G.C_UnitAuras = { GetAuraDataByIndex = function(unit, i, filter)
		local want = tostring(filter):find("HARMFUL") ~= nil
		local n = 0
		for _, a in ipairs(w.auras[unit] or {}) do
			if (a.harmful == true) == want then
				n = n + 1
				if n == i then return { name = a.name, spellId = a.id, duration = a.duration or 0, expirationTime = a.exp or 0, applications = a.count or 1, sourceUnit = a.src, dispelName = a.dispel } end
			end
		end
	end }
	local function numBags() return 5 end
	G.C_Container = {
		GetContainerNumSlots = function(b) if b <= 4 then return w.bag.slots end return 0 end,
		GetContainerNumFreeSlots = function(b) if b <= 4 then return (w.bag.free_by and w.bag.free_by[b]) or (w.bag.free), 0 end return 0, 0 end,
		GetContainerItemInfo = function(b, s) return w.bagItems[b .. ":" .. s] end,
	}
	G.ItemLocation = { CreateFromBagAndSlot = function(_, b, s) return { b = b, s = s } end, CreateFromEquipmentSlot = function(_, slot) return { eq = slot } end }
	local function itemFromLink(l) local id = tonumber(tostring(l):match("item:(%d+)")) or tonumber(l); return id, w.itemDB[id] end
	G.GetItemInfo = function(l)
		local id, d = itemFromLink(l)
		if not d then return nil end
		return d.name, d.link, d.quality, d.ilvl or 1, d.minLevel or 1, d.type or "Armor", d.sub or "Cloth", 1, d.equipLoc or "", 0, d.price or 0, d.classID or 4, 0, d.bindType or 0
	end
	G.C_Item = {
		GetItemStats = function(l) local id = itemFromLink(l); return w.itemStats[id] end,
		IsBound = function(loc) if loc.eq then return w.bound["eq" .. loc.eq] or false end return w.bound[loc.b .. ":" .. loc.s] or false end,
		GetItemCount = function(id) return w.counts[id] or 0 end,
	}
	G.GetInventoryItemLink = function(_, slot) return w.equipped[slot] end
	G.GetInventoryItemDurability = function(slot) local d = w.dura[slot]; if d then return d[1], d[2] end return 50, 100 end
	G.GetItemCount = function(id) return w.counts[id] or 0 end
	G.IsPlayerSpell = function(id) return w.known[id] == true end
	G.C_QuestLog = {
		GetNumQuestLogEntries = function() return #w.quests end,
		GetInfo = function(i) return w.quests[i] end,
		IsComplete = function(id) for _, q in ipairs(w.quests) do if q.questID == id then return q.complete == true end end return false end,
		IsQuestFlaggedCompleted = function(id) return w.flagged[id] == true end,
		GetTitleForQuestID = function(id) for _, q in ipairs(w.quests) do if q.questID == id then return q.title end end end,
	}
	G.GetNumQuestChoices = function() return #w.choices end
	G.GetQuestItemInfo = function(_, i) local c = w.choices[i]; return c.name, 1, 1, c.quality, true, c.id end
	G.GetQuestItemLink = function(_, i) return w.choices[i].link end
	G.GetNumTrainerServices = function() return #w.trainer end
	G.GetTrainerServiceInfo = function(i) local t = w.trainer[i]; return t.name, t.rank, t.category end
	G.GetTrainerServiceCost = function(i) return w.trainer[i].cost end
	G.GetTrainerServiceLevelReq = function(i) return w.trainer[i].req end
	G.GetMerchantNumItems = function() return 7 end
	G.GetNumLootItems = function() return #w.loot end
	G.GetLootSlotLink = function(i) return w.loot[i] end
	G.GetPetHappiness = nil
	-- nothing in the addon may ever call these (display and advice only)
	W.unsafe = {}
	for _, name in ipairs({ "EquipItemByName", "UseContainerItem", "UseItemByName", "DressUpItemLink", "DressUpBattlePet", "PickupContainerItem", "EquipPendingItem", "CastSpellByName", "TargetUnit", "BuyMerchantItem", "AcceptQuest", "GetQuestReward", "PickupInventoryItem", "AutoEquipCursorItem" }) do
		G[name] = function(...) W.unsafe[#W.unsafe + 1] = name; error("forbidden call " .. name) end
	end
	G.C_Item.EquipItem = function() W.unsafe[#W.unsafe + 1] = "C_Item.EquipItem"; error("forbidden") end
	return w
end

-- deliver an event to every frame that registered for it
function W.fire(event, ...)
	for _, f in ipairs(W.frames) do
		if f.events[event] and f.scripts.OnEvent then f.scripts.OnEvent(f, event, ...) end
	end
end

-- load the addon files in .toc order (everything the coach tests need)
function W.loadAddon()
	local files = { "Codec", "JSON", "WoWFeed", "UI", "Coach", "CoachState", "CoachRules" }
	for _, n in ipairs(files) do
		local chunk = assert(loadfile("wowmentor/addon/WoWFeed/" .. n .. ".lua"))
		chunk("WoWFeed")
	end
end

function W.advance(dt) W.clock = W.clock + dt; W.epoch = W.epoch + dt end
function W.runTimers() local t = W.timers; W.timers = {}; for _, f in ipairs(t) do f() end end
function W.runTickers() for _, f in ipairs(W.tickers) do f() end end
function W.runDriver(dt)
	for _, f in ipairs(W.frames) do if f.scripts.OnUpdate and f.kind == "Frame" and not f.name then f.scripts.OnUpdate(f, dt) end end
end
return W
