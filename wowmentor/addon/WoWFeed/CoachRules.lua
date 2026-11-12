-- Coach rules: the instant-reaction engine, checklist auto-complete, the coach inbox (with acks and /coach) and the
-- waypoint HUD. Rules arrive in the overlay document (schema: WHEN_TYPES / DO_TYPES in wowmentor/server/uidoc.py) and
-- run here, locally, so a reaction does not wait for a remote agent to poll the feed.
-- Rules never touch panel positions or alpha; actions reuse the machinery of UI.lua (toasts, chat, ticks, waypoints).
-- Level and XP are never offered as placeholders and never shown.
local floor, max, min, sqrt, pi = math.floor, math.max, math.min, math.sqrt, math.pi
local strfmt, tconcat = string.format, table.concat
local J = WoWFeedJSON
local C = WoWFeedCoach
local API = WoWFeed_API or {}
local S, U = C.S, C.U
local try, secret = U.try, U.secret

local R = {}
C.rules = R
local UI = WoWFeedUI

local function clean(s) if API.clean then return API.clean(s) end return tostring(s) end
local function val(v) if v == nil then return "unknown" end return tostring(v) end
local atan2 = math.atan2 or function(y, x) return math.atan(y, x) end

R.byId = {}          -- rule id -> runtime state (survives document revisions while the definition is unchanged)
R.list = {}          -- active rule definitions in document order
R.history = {}       -- recent fires, newest last
R.fired = 0
R.dirty = true       -- RULES section needs a refresh

---------------------------------------------------------------- conditions
local STATE_TYPES = { bags_free_lte = true, durability_lte = true, buff_missing = true, pet_missing = true, item_count_lte = true, hostiles_near_gte = true, pet_dead = true }

local function whenKind(w)
	if w.type == "all" or w.type == "any" then
		for _, k in ipairs(w.of or {}) do if not STATE_TYPES[k.type] then return "event" end end
		return "state"
	end
	return STATE_TYPES[w.type] and "state" or "event"
end

-- true when the named aura is missing: true / false, or nil when it cannot be told (hidden auras, no pet, dead, ...)
function R.buffMissing(c)
	local unit = c.pet and "pet" or "player"
	if U.bool(try(UnitIsDeadOrGhost, "player")) then return nil end
	if c.pet and not U.exists("pet") then return false end
	if c.ooc and U.inCombat() then return nil end
	local list = U.auras(unit, "HELPFUL")
	if not list then return nil end
	local want = U.lower(c.name)
	for _, a in ipairs(list) do
		if (c.spell and a.id == c.spell) or (want and U.lower(a.name) == want) then return false end
	end
	if list.hidden > 0 then return nil end      -- something we cannot read might be the buff
	return true
end

local function slotItemName(slot)
	local link = U.str(try(GetInventoryItemLink, "player", slot))
	return link and link:match("%[(.-)%]") or nil
end

local function ctxOf(name, d)
	local c = {}
	if name == "death" then
		c.killer = d.killer and d.killer.name; c.subzone = d.subzone; c.zone = d.zone; c.count = d.deaths_in_subzone_10m
	elseif name == "zone_changed" then c.zone = d.zone; c.subzone = d.subzone
	elseif name == "spell_learned" then c.spell = d.name; c.name = d.name; c.rank = d.rank
	elseif name == "quest_complete" or name == "quest_turned_in" then c.name = d.title
	elseif name == "item_looted" then c.name = d.name; c.count = d.count
	elseif name == "item_equipped" then c.name = d.name; c.slot = d.slot
	elseif name == "lower_rank_on_bar" then c.spell = d.name; c.name = d.name; c.rank = d.known_rank; c.slot = d.slot
	elseif name == "target_elite" then c.name = d.name
	elseif name == "merchant_show" or name == "trainer_show" or name == "quest_reward" or name == "loot_opened" then
		c.name = d.npc and d.npc.name
		c.count = d.choices and #d.choices or d.count
	elseif name == "entering_world" then c.zone = d.zone; c.subzone = d.subzone
	elseif name == "enemy_player" then c.class = d.class; c.level = d.level; c.race = d.race; c.name = d.class
	end
	return c
end

local function ci(a, b) return a ~= nil and b ~= nil and a:lower() == b:lower() end

-- Evaluate one leaf. ev = { name, data } for an event, nil when polling. Returns true / false / nil (unknown), ctx.
local function evalLeaf(c, ev)
	local t = c.type
	if t == "bags_free_lte" then
		local free = C.bagFree()
		if not free then return nil, {} end
		return free <= c.n, { free = free, count = free }
	elseif t == "durability_lte" then
		local low, slot = C.lowestDurability()
		if not low then return nil, {} end
		return low <= c.pct, { pct = floor(low + 0.5), slot = C.SLOT_NAMES[slot] or tostring(slot), name = slotItemName(slot) }
	elseif t == "buff_missing" then
		local m = R.buffMissing(c)
		if m == nil then return nil, {} end
		local nm = c.name or (c.spell and U.spellName(c.spell)) or nil
		return m, { name = nm, spell = c.spell or nm }
	elseif t == "item_count_lte" then
		-- how many of an item the player carries (bags only): soul shards, Healthstones, potions, bandages, reagents
		local want = c.item or c.name
		if not want then return nil, {} end
		local n = U.num(try((C_Item and C_Item.GetItemCount) or GetItemCount, want))
		if n == nil then return nil, {} end
		return n <= c.n, { count = n, name = c.name or tostring(c.item) }
	elseif t == "hostiles_near_gte" then
		-- attackable units with a visible nameplate (players only when c.players): "3 or more hostiles near"
		local plates = C_NamePlate and try(C_NamePlate.GetNamePlates)
		if type(plates) ~= "table" then return nil, {} end
		local n = 0
		for _, pl in ipairs(plates) do
			local unit = pl.namePlateUnitToken or (pl.UnitFrame and pl.UnitFrame.unit)
			if unit and U.bool(try(UnitCanAttack, "player", unit)) and not U.bool(try(UnitIsDead, unit)) and (not c.players or U.bool(try(UnitIsPlayer, unit))) then n = n + 1 end
		end
		return n >= c.n, { count = n }
	elseif t == "pet_dead" then
		if U.bool(try(UnitIsDeadOrGhost, "player")) then return nil, {} end
		if not U.exists("pet") then return false, {} end
		return U.bool(try(UnitIsDead, "pet")) or false, { name = "pet" }
	elseif t == "pet_missing" then
		-- any pet counts (Imp, Voidwalker, Succubus...): true only while the player is alive, not mounted or flying, and has none out
		if U.bool(try(UnitIsDeadOrGhost, "player")) then return nil, {} end
		if c.ooc and U.inCombat() then return nil, {} end
		if U.bool(try(IsMounted)) or U.bool(try(UnitOnTaxi, "player")) then return nil, {} end
		return not U.exists("pet"), { name = "pet" }
	end
	if not ev then return false, {} end
	local d = ev.data or {}
	if t == "deaths_in_subzone" then
		if ev.name ~= "death" then return false, {} end
		local n = C.deathsInSubzone(d.zone, d.subzone, c.minutes or 10)
		local ctx = ctxOf("death", d); ctx.count = n
		return n >= (c.count or 2), ctx
	end
	if ev.name ~= t then return false, {} end
	local ctx = ctxOf(ev.name, d)
	if t == "spell_learned" then return (c.name == nil or ci(c.name, d.name)), ctx
	elseif t == "quest_complete" then return (c.quest == nil or c.quest == d.quest), ctx
	elseif t == "zone_changed" then
		return (c.zone == nil or ci(c.zone, d.zone)) and (c.subzone == nil or ci(c.subzone, d.subzone)), ctx
	elseif t == "item_looted" then
		local q = d.quality
		if c.min_quality and c.min_quality > 0 and not (q and q >= c.min_quality) then return false, ctx end
		return true, ctx
	elseif t == "enemy_player" then
		if c.class and not ci(c.class, d.class) then return false, ctx end
		if c.min_level and not (d.level and d.level >= c.min_level) then return false, ctx end
		if c.flagged and not d.flagged then return false, ctx end
		return true, ctx
	elseif t == "target_elite" then
		if not (d.elite or (c.rare and d.rare)) then return false, ctx end
		if c.level_diff ~= nil and not (d.level_diff and d.level_diff >= c.level_diff) then return false, ctx end
		return true, ctx
	end
	return true, ctx
end

local function evalWhen(w, ev)
	if w.type == "all" or w.type == "any" then
		local ctx, anyTrue, allTrue = {}, false, true
		for _, k in ipairs(w.of or {}) do
			local ok, kc = evalLeaf(k, ev)
			if ok == true then
				anyTrue = true
				for key, v in pairs(kc) do if ctx[key] == nil then ctx[key] = v end end
			else allTrue = false end
		end
		if w.type == "all" then return allTrue and #(w.of or {}) > 0, ctx end
		return anyTrue, ctx
	end
	return evalLeaf(w, ev)
end

---------------------------------------------------------------- firing
local function fill(text, ctx)
	if type(text) ~= "string" then return text end
	return (text:gsub("{(%a+)}", function(k)
		local v = ctx[k]
		if v == nil then return nil end                       -- unknown placeholders stay literal
		return (tostring(v):gsub("[{}]", ""))
	end))
end
R.fill = fill

local function runActions(acts, ctx)
	local names = {}
	for _, a in ipairs(acts or {}) do
		local kind = a["do"]
		local ok, err = pcall(function()
			if kind == "toast" then
				UI.showToast({ title = fill(a.title, ctx), text = fill(a.text, ctx), level = a.level or "info", sound = a.sound, ttl = 8 })
			elseif kind == "chat" then
				UI.runAction({ name = "chat", text = fill(a.text, ctx), sound = a.sound })
			elseif kind == "check" then
				UI.setItem(a.panel, a.item, a.done ~= false)
			elseif kind == "waypoint" then
				UI.setWaypoint({ id = a.id, map = a.map, x = a.x, y = a.y, title = fill(a.title, ctx), track = a.track, clear_yards = a.clear_yards }, "rule")
			elseif kind == "panel_text" then
				UI.panelText(a.panel, a.child, fill(a.text, ctx))
			elseif kind == "sound" then
				UI.playSound(a.sound)
			elseif kind == "flash" then
				UI.runAction({ name = "flash" })
			elseif kind == "speak" then
				UI.speak(fill(a.text, ctx))
			end
		end)
		names[#names + 1] = tostring(kind)
		if not ok then C.err("rule action " .. tostring(kind), err) end
	end
	return names
end

R.deferred = {}      -- rule id -> { e, ctx, t } held while the player is AFK or loading (rules with active_only)

local function inactive()
	return S.afk == true or S.loading == true
end

local function fire(e, ctx, replay)
	local st, def = e.st, e.def
	if def.enabled == false then return false end
	if def.active_only and not replay and inactive() then
		local copy = {}
		for k, v in pairs(ctx) do copy[k] = v end
		R.deferred[tostring(def.id)] = { e = e, ctx = copy, t = U.now() }
		R.dirty = true
		return false
	end
	local now = U.now()
	if def.once and st.onceFired then return false end
	local cd = def.cooldown_s
	if cd == nil then cd = 60 end
	if st.last and now - st.last < cd then return false end
	st.last = now; st.fires = st.fires + 1; st.onceFired = true
	-- every rule can use the current place; specific values from the condition win
	if ctx.zone == nil or ctx.subzone == nil then
		local z, sz = U.zone()
		if ctx.zone == nil then ctx.zone = z end
		if ctx.subzone == nil then ctx.subzone = sz end
	end
	R.fired = R.fired + 1
	local names = runActions(e.acts, ctx)
	local summary = {}
	for k, v in pairs(ctx) do if type(v) == "string" or type(v) == "number" then summary[k] = v end end
	R.history[#R.history + 1] = { t = U.epoch(), rule = def.id, ctx = summary, actions = names }
	while #R.history > 30 do table.remove(R.history, 1) end
	R.dirty = true
	C.emit("rule_fired", { rule = def.id, actions = J.array(names), ctx = summary })
	return true
end

-- The player is active again: run what was held. State rules are re-checked (the condition may have cleared meanwhile);
-- event rules run with the context they were held with unless it is older than defer_max_s (default 900 s).
function R.flushDeferred()
	if inactive() then return 0 end
	local n = 0
	local held = R.deferred
	R.deferred = {}
	local now = U.now()
	for id, d in pairs(held) do
		local e = d.e
		local maxAge = tonumber(e.def.defer_max_s) or 900
		if now - d.t <= maxAge and R.byId[id] == e.st then
			local go, ctx = true, d.ctx
			if e.kind == "state" then
				local okp, ok, c2 = pcall(evalWhen, e.def.when, nil)
				go = okp and ok == true
				if go and c2 then ctx = c2 end
			end
			if go and fire(e, ctx, true) then n = n + 1 end
		end
	end
	R.dirty = true
	return n
end

---------------------------------------------------------------- loading and evaluating rules
function R.load(rules)
	local list, seen = {}, {}
	for _, def in ipairs(type(rules) == "table" and rules or {}) do
		-- the wire key is "then" (a reserved word in Lua, so it can only be read as def["then"])
		if type(def) == "table" and def.id and type(def.when) == "table" and type(def["then"]) == "table" then
			local sig = J.encode(def) or tostring(def.id)
			local id = tostring(def.id)
			local st = R.byId[id]
			if not st or st.sig ~= sig then
				st = { sig = sig, prev = nil, last = nil, fires = st and st.fires or 0, onceFired = false }
				R.byId[id] = st
			end
			seen[id] = true
			list[#list + 1] = { def = def, st = st, kind = whenKind(def.when), acts = def["then"] }
		end
	end
	-- state of removed rules is kept on purpose: a rule marked "once" stays spent for the session even if it is re-added
	R.list = list
	R.dirty = true
	S.dirtyRules = true
end

-- an event happened: event rules fire when their condition matches it
function R.onEvent(name, data)
	local ev = { name = name, data = data or {} }
	for _, e in ipairs(R.list) do
		if e.kind == "event" then
			local okp, ok, ctx = pcall(evalWhen, e.def.when, ev)      -- one malformed rule must not stop the others
			if not okp then C.err("rule " .. tostring(e.def.id), ok)
			elseif ok == true then fire(e, ctx or {}) end
		end
	end
	if name == "zone_changed" or name == "entering_world" then S.autoDirty = true end
end

-- polled conditions: edge triggered (false -> true), unknown keeps the previous state
function R.evalState()
	if next(R.deferred) ~= nil then R.flushDeferred() end      -- also catches the end of a loading screen
	for _, e in ipairs(R.list) do
		if e.kind == "state" then
			local okp, ok, ctx = pcall(evalWhen, e.def.when, nil)
			if not okp then C.err("rule " .. tostring(e.def.id), ok); ok = nil end
			if ok ~= nil then
				local prev = e.st.prev
				e.st.prev = ok
				if ok and prev ~= true then
					if e.def.when.type == "buff_missing" then
						C.emit("buff_missing", { name = (ctx and ctx.name) or "unknown" })
					elseif e.def.when.type == "all" or e.def.when.type == "any" then
						for _, k in ipairs(e.def.when.of) do
							if k.type == "buff_missing" then C.emit("buff_missing", { name = (ctx and ctx.name) or "unknown" }); break end
						end
					end
					fire(e, ctx or {})
				end
			end
		end
	end
end

-- target_elite: called when the target changes
function R.onTarget()
	if #R.list == 0 or not U.exists("target") or not U.bool(try(UnitCanAttack, "player", "target")) then return end
	local cls = U.str(try(UnitClassification, "target"))
	if not cls then return end
	local elite = (cls == "elite" or cls == "rareelite" or cls == "worldboss")
	local rare = (cls == "rare" or cls == "rareelite")
	if not (elite or rare) then return end
	local lvl, mine = U.unitLevel("target"), U.unitLevel("player")
	local diff
	if lvl == -1 then diff = 99 elseif lvl and mine then diff = lvl - mine end
	R.onEvent("target_elite", { name = U.unitName("target"), level = lvl, classification = cls, elite = elite, rare = rare, level_diff = diff })
end

---------------------------------------------------------------- checklist auto-complete
local function skillRanks()
	local map = {}
	local n = (C_SkillInfo and U.num(try(C_SkillInfo.GetNumSkillLines))) or U.num(try(GetNumSkillLines)) or 0
	for i = 1, n do
		local info = C_SkillInfo and try(C_SkillInfo.GetSkillLineInfo, i)
		if type(info) == "table" then
			if not info.isHeader and U.str(info.name) then map[info.name:lower()] = U.num(info.rank) or 0 end
		elseif GetSkillLineInfo then
			local name, hdr, _, rank = try(GetSkillLineInfo, i)
			if name and not hdr and U.str(name) then map[name:lower()] = U.num(rank) or 0 end
		end
	end
	return map
end

local function autoOk(a, cache)
	local t = a.type
	if t == "quest_done" then
		if C_QuestLog and C_QuestLog.IsQuestFlaggedCompleted then return try(C_QuestLog.IsQuestFlaggedCompleted, a.quest) == true end
		local done = try(GetQuestsCompleted)
		return type(done) == "table" and done[a.quest] == true
	elseif t == "quest_group" then
		local qs = type(a.quests) == "table" and a.quests or {}
		if #qs == 0 then return false end
		local any = a.mode == "any"
		for _, q in ipairs(qs) do
			local done = autoOk({ type = "quest_done", quest = q }, cache)
			if any and done then return true end
			if not any and not done then return false end
		end
		return not any
	elseif t == "spell_known" then
		if a.spell then
			if IsPlayerSpell and try(IsPlayerSpell, a.spell) == true then return true end
			if IsSpellKnown and try(IsSpellKnown, a.spell) == true then return true end
			if C_SpellBook and C_SpellBook.IsSpellKnown and try(C_SpellBook.IsSpellKnown, a.spell) == true then return true end
			cache.byName, cache.byId = cache.byName or select(1, C.state.bookIndex()), cache.byId or select(2, C.state.bookIndex())
			if cache.byId[a.spell] then return true end
		end
		if a.name then
			cache.byName = cache.byName or select(1, C.state.bookIndex())
			if cache.byName[a.name:lower()] then return true end
		end
		return false
	elseif t == "skill_gte" then
		cache.skills = cache.skills or skillRanks()
		local r = cache.skills[a.skill:lower()]
		return r ~= nil and r >= a.value
	elseif t == "item_in_bags" then
		local n
		if C_Item and C_Item.GetItemCount then n = U.num(try(C_Item.GetItemCount, a.item, false)) else n = U.num(try(GetItemCount, a.item)) end
		return n ~= nil and n >= (a.count or 1)
	elseif t == "zone" then
		local z, sz = U.zone()
		local mz = U.str(try(GetMinimapZoneText))
		local zt = U.str(try(GetZoneText))
		return ci(a.zone, z) or ci(a.zone, sz) or ci(a.zone, mz) or ci(a.zone, zt)
	end
	return false
end

R.autoDone = {}
-- Tick every checklist item whose auto condition holds. Once per item and document version, through UI.setItem, so the tick
-- is recorded and reported exactly like a player click (the bridge turns the tick line into a "check" op).
function R.autoComplete()
	local cache = {}
	local todo = {}
	UI.eachAutoItem(function(pid, it, done)
		local key = pid .. "/" .. it.id .. "/" .. tostring(it.ver or 0)
		if not done and not R.autoDone[key] then todo[#todo + 1] = { pid = pid, it = it, key = key } end
	end)
	for _, x in ipairs(todo) do
		local ok, res = pcall(autoOk, x.it.auto, cache)
		if ok and res == true then
			R.autoN = (R.autoN or 0) + 1
			if R.autoN > 500 then R.autoDone, R.autoN = {}, 1 end      -- bounded; an item already ticked reads as done anyway
			R.autoDone[x.key] = true
			UI.setItem(x.pid, x.it.id, true)
			R.lastAuto = { panel = x.pid, item = x.it.id }
		elseif not ok then C.err("auto", res) end
	end
end

---------------------------------------------------------------- inbox, acks, questions
local LEVEL_HEX = { info = "#e9ecf2", tip = "#f5c518", good = "#5bd37a", warn = "#ffb020", bad = "#ff5c5c" }
R.seen = {}          -- inbox id -> { chat, toast, shown }
R.questions = {}     -- open /coach questions, newest last
R.qn = 0

function R.ack(kind, id)
	C.emit("ack", { kind = kind, id = id, shown_at = U.epoch() })
end

function C.onToastShown(t)
	if t and t.id ~= nil then R.ack("toast", t.id) end
end

local function newest(n)
	local inbox = UI.state.inbox or {}
	local out = {}
	for i = max(1, #inbox - n + 1), #inbox do out[#out + 1] = inbox[i] end
	return out
end

-- built-in panel "sys-inbox": the newest 3 coach messages; position and alpha belong to the player
local function buildInbox()
	local shown = newest(3)
	if #shown == 0 then return nil end
	local kids = {}
	for i = #shown, 1, -1 do
		local m = shown[i]
		kids[#kids + 1] = { type = "callout", level = LEVEL_HEX[m.level] and m.level or "info", text = tostring(m.text or ""), size = 11 }
	end
	return { id = "sys-inbox", title = "Coach", anchor = "TOPRIGHT", x = -20, y = -560, w = 280, fontSize = 11, bgAlpha = 0.45,
		visibility = { combat = "dim", idleFade = 90, idleAlpha = 0.4 }, children = kids }
end
UI.registerBuiltin({ id = "sys-inbox", key = "inbox", hide = "hideInbox", build = buildInbox })

function R.syncInbox(inbox, first, nowEpoch)
	if type(inbox) ~= "table" then return end
	for _, m in ipairs(inbox) do
		if type(m) == "table" and m.id ~= nil and m.text then
			local rec = R.seen[m.id]
			if not rec then
				rec = { chat = false, toast = false, shown = false }
				R.seen[m.id] = rec
				local old = first and ((nowEpoch or 0) - (m.t or 0)) >= 30     -- on first load only fresh messages are announced
				if old then rec.chat, rec.toast = true, true end
				if m.answer_to ~= nil then
					for i = #R.questions, 1, -1 do if R.questions[i].id == m.answer_to then table.remove(R.questions, i) end end
					R.dirtyQuestions = true
				end
				if not old then
					if m.chat ~= false then
						local ok = pcall(function()
							if DEFAULT_CHAT_FRAME and DEFAULT_CHAT_FRAME.AddMessage then
								DEFAULT_CHAT_FRAME:AddMessage("|cff33ccffCoach|r: " .. UI.expand(tostring(m.text)))
								UI.playSound(m.sound)
							else error("no chat frame") end
						end)
						if ok then rec.chat = true; R.ack("chat", m.id) end
					end
					if m.toast then
						local ok, shownT = pcall(UI.showToast, { title = "Coach", text = tostring(m.text), level = m.level or "info", sound = m.sound, ttl = 10 })
						if ok and shownT then rec.toast = true; R.ack("toast", m.id) end
					end
				end
			end
		end
	end
	R.dirtyInbox = true
end

-- coach messages become "shown" when the inbox panel is actually visible (not removed, hidden or hidden-in-combat)
function R.checkShown()
	if not UI.panelVisible("sys-inbox") then return end
	for _, m in ipairs(newest(3)) do
		local rec = R.seen[m.id]
		if rec and not rec.shown then rec.shown = true; R.ack("coach", m.id); R.dirtyInbox = true end
	end
end

function R.askCoach(text)
	text = (text or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if text == "" then return false end
	R.qn = R.qn + 1
	local id = strfmt("q%d_%d", S.loginAt or U.epoch(), R.qn)
	local q = { id = id, text = U.trunc(text, 300), t = U.epoch() }
	R.questions[#R.questions + 1] = q
	while #R.questions > 20 do table.remove(R.questions, 1) end
	R.dirtyQuestions = true
	C.event("coach_question", { id = id, text = q.text })
	return true, id
end

SLASH_COACH1 = "/coach"
SlashCmdList = SlashCmdList or {}
SlashCmdList.COACH = function(msg)
	local function say(t) if print then print("|cff33ccffCoach|r: " .. t) end end
	if R.askCoach(msg) then say("Sent to your coach.") else say("Usage: /coach <question>   (sent to your coach with your next update)") end
end

---------------------------------------------------------------- waypoint HUD
local hud
local function db()
	local d = WoWFeedDB and WoWFeedDB.ui
	if not d then return nil end
	d.user = d.user or {}
	return d.user
end

local function activeWaypoint()
	local st = UI.state
	for _, id in ipairs(st.wpOrder or {}) do if st.wps[id] then return id, st.wps[id] end end
	local best, bid
	for id, rec in pairs(st.wps) do if not best or (rec.t or 0) > (best.t or 0) then best, bid = rec, id end end
	return bid, best
end

-- Returns distance (yards, may be nil) and the east / north offsets in yards (may be nil) from the player to the waypoint.
function R.vector(rec)
	local w = rec.data
	if not w then return nil end
	local dist, de, dn
	local mid, px, py = U.posFrac()
	if mid and px and py and mid == w.map and C_Map and C_Map.GetMapWorldSize then
		local W, H = try(C_Map.GetMapWorldSize, mid)
		W, H = U.num(W), U.num(H)
		if W and H and W > 0 and H > 0 then
			de = (w.x / 100 - px) * W
			dn = -(w.y / 100 - py) * H
			dist = sqrt(de * de + dn * dn)
		end
	end
	if rec.via == "TomTom" and rec.uid and TomTom and TomTom.GetDistanceToWaypoint then
		local d = U.num(try(TomTom.GetDistanceToWaypoint, TomTom, rec.uid))
		if d then dist = d end
	end
	if not dist and mid and C_Map and C_Map.GetWorldPosFromMapPos and CreateVector2D and UnitPosition then
		-- last resort: world coordinates (UnitPosition returns y first, then x)
		local py2, px2 = try(UnitPosition, "player")
		local _, wp = try(C_Map.GetWorldPosFromMapPos, w.map, CreateVector2D(w.x / 100, w.y / 100))
		py2, px2 = U.num(py2), U.num(px2)
		if py2 and px2 and wp and U.num(wp.x) and U.num(wp.y) then
			local d = sqrt((wp.x - px2) ^ 2 + (wp.y - py2) ^ 2)
			if d < 20000 then dist = d end
		end
	end
	return dist, de, dn
end

local function arrowWord(rel)
	local a = rel % (2 * pi)
	if a > pi then a = a - 2 * pi end              -- -pi..pi, positive = to the left
	local deg = a * 180 / pi
	if deg > -30 and deg < 30 then return "ahead" end
	if deg >= 30 and deg < 150 then return "left" end
	if deg <= -30 and deg > -150 then return "right" end
	return "behind"
end

local function ensureHud()
	if hud then return hud end
	local f = CreateFrame("Frame", "WoWFeedWaypointHUD", UIParent)
	f:SetSize(170, 46); f:SetFrameStrata("MEDIUM"); f:SetClampedToScreen(true); f:EnableMouse(false); f:SetMovable(true)
	f.bg = f:CreateTexture(nil, "BACKGROUND"); f.bg:SetAllPoints(); f.bg:SetColorTexture(0.04, 0.05, 0.08, 0.6)
	f.arrow = f:CreateTexture(nil, "ARTWORK"); f.arrow:SetSize(32, 32); f.arrow:SetPoint("LEFT", f, "LEFT", 6, 0)
	f.arrow:SetTexture("Interface\\Minimap\\ROTATING-MINIMAPARROW")
	f.title = f:CreateFontString(nil, "OVERLAY"); f.title:SetFont(STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", 11, "OUTLINE")
	f.title:SetPoint("TOPLEFT", f, "TOPLEFT", 44, -7); f.title:SetWidth(120); f.title:SetJustifyH("LEFT"); f.title:SetTextColor(0.96, 0.77, 0.09)
	f.dist = f:CreateFontString(nil, "OVERLAY"); f.dist:SetFont(STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", 12, "OUTLINE")
	f.dist:SetPoint("TOPLEFT", f, "TOPLEFT", 44, -24); f.dist:SetWidth(120); f.dist:SetJustifyH("LEFT"); f.dist:SetTextColor(0.91, 0.93, 0.95)
	f:RegisterForDrag("LeftButton")
	f:SetScript("OnDragStart", function(self) self:StartMoving() end)
	f:SetScript("OnDragStop", function(self)
		self:StopMovingOrSizing()
		local u = db()
		if u then
			local point, _, rel, x, y = self:GetPoint()
			u["sys-waypoint"] = u["sys-waypoint"] or {}
			u["sys-waypoint"].pos = { point, rel, x, y }
		end
	end)
	local u = db()
	local pos = u and u["sys-waypoint"] and u["sys-waypoint"].pos
	f:ClearAllPoints()
	if pos then f:SetPoint(pos[1], UIParent, pos[2], pos[3], pos[4]) else f:SetPoint("TOP", UIParent, "TOP", 0, -200) end
	f:Hide()
	hud = f
	R.hud = f
	return f
end

-- Clear waypoints that were reached or whose linked task is done. Called every second and from the HUD loop.
function R.checkWaypoints()
	local st = UI.state
	local ids = {}
	for id in pairs(st.wps) do ids[#ids + 1] = id end
	for _, id in ipairs(ids) do
		local rec = st.wps[id]
		local w = rec and rec.data
		if w then
			if type(w.task) == "string" then
				local pid, iid = w.task:match("^([^/]+)/(.+)$")
				if pid and UI.itemDone(pid, iid) == true then UI.dismissWaypoint(id, "task done"); w = nil end
			end
			if w then
				local dist = R.vector(rec)
				if dist and dist <= (w.clear_yards or 12) then UI.dismissWaypoint(id, "reached") end
			end
		end
	end
end

function R.hudUpdate()
	local id, rec = activeWaypoint()
	if not id or not rec or not rec.data or UI.state.hidden then
		if hud then hud:Hide() end
		return
	end
	local f = ensureHud()
	local w = rec.data
	local dist, de, dn = R.vector(rec)
	local title = UI.expand(w.title or "Waypoint")
	if title ~= f.lastTitle then f.lastTitle = title; f.title:SetText(title) end
	local facing = U.num(try(GetPlayerFacing))
	local word = ""
	if de and dn and facing then
		local bearing = atan2(-de, dn)           -- counter-clockwise from north, like GetPlayerFacing
		local rel = bearing - facing
		pcall(f.arrow.SetRotation, f.arrow, rel)
		f.arrow:Show()
		word = "  " .. arrowWord(rel)
	else
		f.arrow:Hide()
	end
	local dtxt = (dist and strfmt("%d yd", floor(dist + 0.5)) or "distance unknown") .. word
	if dtxt ~= f.lastDist then f.lastDist = dtxt; f.dist:SetText(dtxt) end
	local al = U.inCombat() and 0.5 or 1
	if al ~= f.lastAlpha then f.lastAlpha = al; f:SetAlpha(al) end
	if not f:IsShown() then f:Show() end
	R.hudShown = id
end

---------------------------------------------------------------- sections
function R.ruleLines()
	local nd = 0
	for _ in pairs(R.deferred) do nd = nd + 1 end
	local L = { strfmt("rules loaded: %d | fires this session: %d | held until active: %d", #R.list, R.fired, nd) }
	local now = U.now()
	for _, e in ipairs(R.list) do
		local d, st = e.def, e.st
		L[#L + 1] = strfmt("%s | %s | when %s | cooldown %ss%s | fires %d | last %s", clean(d.id), d.enabled == false and "disabled" or "enabled", clean(d.when.type), val(d.cooldown_s),
			d.once and " | once" or "", st.fires, st.last and strfmt("%.0fs ago", now - st.last) or "never")
	end
	if #R.history > 0 then
		L[#L + 1] = "fire history (newest first):"
		for i = #R.history, max(1, #R.history - 14), -1 do
			local h = R.history[i]
			local parts = {}
			for k, v in pairs(h.ctx) do parts[#parts + 1] = k .. "=" .. tostring(v) end
			table.sort(parts)
			L[#L + 1] = strfmt("  %s %s -> %s %s", date and date("%H:%M:%S", h.t) or tostring(h.t), clean(h.rule), tconcat(h.actions, "+"), clean(tconcat(parts, " ")))
		end
	end
	return L
end

function R.inboxLines()
	local inbox = UI.state.inbox or {}
	if #inbox == 0 then return { "no coach messages" } end
	local L = {}
	for i = #inbox, max(1, #inbox - 2), -1 do
		local m = inbox[i]
		local rec = R.seen[m.id] or {}
		L[#L + 1] = strfmt("id %s | %s | shown=%d chat=%d toast=%d | %s", val(m.id), clean(m.level or "info"), rec.shown and 1 or 0, rec.chat and 1 or 0, rec.toast and 1 or 0, clean(U.trunc(tostring(m.text), 160)))
	end
	return L
end

function R.questionLines()
	if #R.questions == 0 then return { "no open questions" } end
	local L = { strfmt("open questions: %d", #R.questions) }
	for _, q in ipairs(R.questions) do
		L[#L + 1] = strfmt("%s | asked %s | %s", q.id, date and date("%H:%M:%S", q.t) or tostring(q.t), clean(q.text))
	end
	return L
end

function R.sections(now, run)
	run("rules", now, 1, 60, "dirtyRulesSec", R.ruleLines, 300)
	run("coach_inbox", now, 1, 30, "dirtyInboxSec", R.inboxLines, 120)
	run("coach_questions", now, 1, 30, "dirtyQuestionsSec", R.questionLines, 120)
end

---------------------------------------------------------------- tick
local lastEval, lastAuto = -1e9, -1e9
function R.tick(now)
	if (S.dirtyRules and now - lastEval >= 1) or now - lastEval >= 5 then
		S.dirtyRules = false
		lastEval = now
		if S.loginHold == nil or now >= S.loginHold then R.evalState() end
	end
	if (S.autoDirty and now - lastAuto >= 0.5) or now - lastAuto >= 2 then
		S.autoDirty = false
		lastAuto = now
		R.autoComplete()
	end
	R.checkWaypoints()
	R.checkShown()
	if R.dirty then R.dirty = false; S.dirtyRulesSec = true end
	if R.dirtyInbox then R.dirtyInbox = false; S.dirtyInboxSec = true end
	if R.dirtyQuestions then R.dirtyQuestions = false; S.dirtyQuestionsSec = true end
	R.hudUpdate()
end

-- the HUD also follows the player smoothly between the 1 s ticks
local hudDriver = CreateFrame("Frame")
local acc = 0
hudDriver:SetScript("OnUpdate", function(_, dt)
	acc = acc + (dt or 0)
	if acc < 0.1 then return end
	acc = 0
	if hud and hud:IsShown() then pcall(R.hudUpdate) end
	if hud then
		local grab = (IsShiftKeyDown and IsShiftKeyDown() and hud:IsShown()) and true or false
		if grab ~= hud.lastGrab then hud.lastGrab = grab; hud:EnableMouse(grab) end
	end
end)

---------------------------------------------------------------- document hook (called by UI.lua after every applied revision)
function C.onDoc(doc, first, nowEpoch)
	R.load(doc.rules)
	R.syncInbox(doc.inbox, first, nowEpoch)
	S.autoDirty = true
	if UI.refreshBuiltin then UI.refreshBuiltin() end
end

-- events that change a polled condition ask for an early evaluation
for _, e in ipairs({ "BAG_UPDATE_DELAYED", "UPDATE_INVENTORY_DURABILITY", "PLAYER_EQUIPMENT_CHANGED", "PLAYER_REGEN_ENABLED", "PLAYER_REGEN_DISABLED", "UNIT_AURA", "UNIT_PET", "PLAYER_ALIVE", "PLAYER_UNGHOST" }) do
	C.on(e, function(unit)
		if e == "UNIT_AURA" or e == "UNIT_PET" then if unit ~= "player" and unit ~= "pet" then return end end
		S.dirtyRules = true
	end)
end
for _, e in ipairs({ "QUEST_LOG_UPDATE", "SKILL_LINES_CHANGED", "CHAT_MSG_SKILL", "QUEST_TURNED_IN", "PLAYER_ENTERING_WORLD", "ZONE_CHANGED", "ZONE_CHANGED_NEW_AREA", "ZONE_CHANGED_INDOORS", "SPELLS_CHANGED", "BAG_UPDATE_DELAYED" }) do
	C.on(e, function() S.autoDirty = true end)
end
