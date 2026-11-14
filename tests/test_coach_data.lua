-- CoachData sections: DAMAGE (C_DamageMeter, secret in combat), AH_PRICES (Auctionator API), BANK (BagBrother cache), XP rate,
-- THREAT on the pet's target, RARES on nameplates. Runs under plain Lua and LuaJIT; no game needed.
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local w = W.installCoach()

local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

-- world: player guid, XP, damage meter, Auctionator, BagBrother, threat
w.units.player.guid = "Player-1-0001"
local XP = { xp = 100, max = 1000 }
_G.UnitXP = function() return XP.xp end
_G.UnitXPMax = function() return XP.max end
_G.GetXPExhaustion = function() return 250 end
w.spells[686] = { name = "Shadow Bolt" }; w.spells[172] = { name = "Corruption" }; w.spells[6603] = { name = "Melee" }
w.itemDB[2589] = { name = "Linen Cloth" }; w.itemDB[2674] = { name = "Crawler Meat" }; w.itemDB[3173] = { name = "Bear Meat" }; w.itemDB[818] = { name = "Tigerseye" }

local M = { secret = false, done = nil, taken = nil, overall = nil, available = true }
local function session(src, spells)
	if M.secret then return { combatSources = { { isLocalPlayer = true, totalAmount = W.secret("number"), amountPerSecond = W.secret("number"), sourceGUID = W.secret("string") } }, durationSeconds = W.secret("number") } end
	return src
end
_G.C_DamageMeter = {
	IsDamageMeterAvailable = function() return M.available, M.available and "" or "Disabled in this zone" end,
	GetCombatSessionFromType = function(st, mt)
		local d = st == 0 and M.overall or M
		local r = mt == 0 and d.done or d.taken
		if not r then return { combatSources = {}, totalAmount = 0 } end
		return session({ durationSeconds = r.dur, combatSources = { { isLocalPlayer = false, totalAmount = 5, amountPerSecond = 1 },
			{ isLocalPlayer = true, totalAmount = r.total, amountPerSecond = r.dps, sourceGUID = "Player-1-0001" } } })
	end,
	GetCombatSessionSourceFromType = function(st, mt, guid)
		assert(guid == "Player-1-0001", "spells asked for the player's guid")
		local d = st == 0 and M.overall or M
		local r = mt == 0 and d.done or d.taken
		return { combatSpells = r and r.spells or {}, totalAmount = r and r.total or 0 }
	end,
	GetSessionDurationSeconds = function() return 12 end,
}
local AH = { [2589] = 45, [2674] = 120 }
_G.Auctionator = { API = { v1 = {
	GetAuctionPriceByItemID = function(caller, id) assert(caller == "WoWFeed"); return AH[id] end,
	GetAuctionAgeByItemID = function(_, id) return AH[id] and 1 or nil end,
	GetVendorPriceByItemID = function(_, id) return id == 2589 and 10 or nil end,
} } }
_G.BrotherBags = { forever = { Tester = {
	[0] = { items = { "2589;5" }, size = 16 },
	{ items = { "3173;13", [3] = "2589;14" }, size = 8 },
	[5] = {},
	[6] = { items = { "2589;20", "818", "277470::::::::18:1490::11:::::::" }, bankType = 0, name = "Tab 1", size = 48 },
	mail = { "3173;2" }, money = 14244,
} }, account = {} }
local TH = {}
_G.UnitDetailedThreatSituation = function(unit, mob) local t = TH[unit] or {}; return t[1], t[2], t[3] end

W.loadAddon()
local API, C = WoWFeed_API, WoWFeedCoach
local S, D = C.S, C.data
check(D ~= nil, "CoachData loaded")
local pushed = {}
local realPush = API.push
API.push = function(sec, lines, age) pushed[sec] = lines; return realPush(sec, lines, age) end
for _, f in ipairs(W.frames) do if f.events.PLAYER_LOGIN and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_LOGIN") end end
W.fire("PLAYER_ENTERING_WORLD", true, false)
local function tick(n, dt) for _ = 1, n or 1 do W.advance(dt or 1); W.runTickers() end end
local function text(sec) return table.concat(pushed[sec] or {}, "\n") end
tick(2)
check(#S.errors == 0, "errors after login: " .. table.concat(S.errors, " | "))

---------------------------------------------------------------- 1. DAMAGE: secret during the fight, read after it
check(text("damage"):find("no fight read yet"), "damage before any fight: " .. text("damage"))
w.combat = true
W.fire("PLAYER_REGEN_DISABLED")
M.secret = true
M.done = { dur = 12.4, total = 900, dps = 72.6, spells = { { spellID = 686, totalAmount = 600, amountPerSecond = 48, overkillAmount = 0 }, { spellID = 172, totalAmount = 300, amountPerSecond = 24 } } }
M.taken = { dur = 12.4, total = 240, dps = 19, spells = { { spellID = 6603, totalAmount = 240, creatureName = "Murkdeep", isAvoidable = false } } }
M.overall = { done = { dur = 400, total = 5000, dps = 12.5, spells = M.done.spells }, taken = { dur = 400, total = 1000, dps = 2.5, spells = M.taken.spells } }
tick(3)
check(#S.errors == 0, "no errors while the meter is secret: " .. table.concat(S.errors, " | "))
w.combat = false
W.fire("PLAYER_REGEN_ENABLED")
tick(1)                                                         -- still secret right after combat: retried, never an error
check(S.dmg.due ~= nil, "a secret read is retried")
M.secret = false
tick(2)
local dmg = text("damage")
check(dmg:find("last fight: %S+ 12.4s dealt=900 dps=72.6 taken=240"), "last fight line: " .. dmg)
check(dmg:find("dealt by spell: Shadow Bolt 600 %(67%%%); Corruption 300 %(33%%%)"), "spell breakdown: " .. dmg)
check(dmg:find("taken by spell: Melee from Murkdeep 240 %(100%%%)"), "damage taken by source: " .. dmg)
check(dmg:find("session: 400s dealt=5000 dps=12.5 taken=1000"), "session line: " .. dmg)
check(dmg:find("recent fights dps/duration %(newest first%): 72.6/12s"), "history: " .. dmg)
-- the same session read again (no new fight) is not a new history entry
S.dmg.due = 0; tick(2)
check(#S.dmg.hist == 1, "no duplicate fight, got " .. #S.dmg.hist)
-- meter unavailable is reported, not an error
M.available = false; S.dmg.due = 0; tick(2)
check(text("damage"):find("meter: unavailable %(Disabled in this zone%)"), "unavailable: " .. text("damage"))
M.available = true

---------------------------------------------------------------- 2. BANK from the BagBrother cache
S.dirtyBank = true; tick(6)
local bank = text("bank")
check(bank:find("items owned: 4 kinds"), "bank header: " .. bank)
check(bank:find("\n2589 bank=20 mail=0 bags=19"), "linen: " .. bank)
check(bank:find("\n3173 bank=0 mail=2 bags=13"), "mail counts: " .. bank)
check(bank:find("\n818 bank=1 mail=0 bags=0"), "single item: " .. bank)
check(bank:find("\n277470 bank=1 mail=0 bags=0"), "link-style item string: " .. bank)
check(bank:find("cached money=1g 42s 44c"), "money: " .. bank)

---------------------------------------------------------------- 3. AH prices for overlay, bag and bank items
C.onDoc({ panels = { { id = "squares", children = { { type = "row", children = { { type = "icon", item = 2674 }, { type = "icon", item = "x" } } } } } } }, true, 0)
check(S.docItems[2674], "overlay items collected")
w.bagItems["0:1"] = { hyperlink = "|cffffffff|Hitem:2589::|h[Linen Cloth]|h|r", quality = 1, stackCount = 5, itemID = 2589 }
tick(61)
local ah = text("ah_prices")
check(ah:find("^ah prices %(Auctionator, last scan; copper; item ids%): 2 of 5 items priced"), "ah header: " .. ah)
local lines = pushed.ah_prices
check(lines[2] == "2674 ah=120 age=1 vendor=unknown overlay", "overlay items first: " .. tostring(lines[2]))
check(ah:find("\n2589 ah=45 age=1 vendor=10 bags"), "bag item: " .. ah)
check(not ah:find("\n818 "), "an unpriced bank item is left out: " .. ah)
check(#lines == 3, "header + 2 priced: " .. #lines)
-- without Auctionator the section says so
local saveA = _G.Auctionator; _G.Auctionator = nil
check(D.ahLines()[1]:find("Auctionator not loaded"), "no Auctionator")
_G.Auctionator = saveA

---------------------------------------------------------------- 4. XP per hour and time to level (AFK time does not count)
-- 600 xp in 10 active minutes = 3600/h; 20 AFK minutes in between change nothing
for _ = 1, 40 do XP.xp = XP.xp + 15; tick(15) end                  -- 10 min active, +600
w.afk = true
tick(80, 15)                                                       -- 20 min AFK, no XP
w.afk = false
tick(2, 15)
local rate = D.xpRate()
check(rate and math.abs(rate - 3600 * 600 / (600 + 30 + 15)) < 300, "xp/h about 3300-3600, got " .. tostring(rate))
S.dirtyXp = true; tick(11)
local xpt = text("xp")
check(xpt:find("level=5 xp=700 xpmax=1000 pct=70 rested=250"), "xp line: " .. xpt)
check(xpt:find("xp_per_hour=%d+ window_min=%d+ time_to_level_min=%d+"), "rate line: " .. xpt)
-- a level-up keeps the cumulative count going
XP.xp, XP.max = 50, 1200; w.units.player.level = 6
tick(16)
check(S.xpt.gained == 950, "gained across the level-up: " .. tostring(S.xpt.gained))
W.fire("CHAT_MSG_COMBAT_XP_GAIN", "Murkdeep dies, you gain 120 experience.")
check(S.xpt.kill == 120, "kill xp counted")

---------------------------------------------------------------- 5. THREAT on the pet's target and the threat_pulled event
w.units.pet = { exists = true, name = "Voidwalker" }
w.units.pettarget = { exists = true, name = "Murkdeep" }
TH.player = { false, 1, 60 }; TH.pet = { true, 3, 100 }
local events = {}
local realEmit = C.emit
C.emit = function(t, d) events[#events + 1] = { t = t, d = d }; return realEmit(t, d) end
w.combat = true
tick(3)
check(text("threat"):find("pet_target=Murkdeep") and text("threat"):find("player: tanking=false status=1 pct=60") and text("threat"):find("pet: tanking=true status=3 pct=100"), "threat: " .. text("threat"))
TH.player = { true, 3, 100 }
tick(2); tick(2)
local pulled = 0
for _, e in ipairs(events) do if e.t == "threat_pulled" then pulled = pulled + 1; check(e.d.mob == "Murkdeep", "pulled mob") end end
check(pulled == 1, "one threat_pulled per fight, got " .. pulled)
-- secret threat values in combat are "unknown", never an error
TH.player = { W.secret("boolean"), W.secret("number"), W.secret("number") }
tick(3)
check(text("threat"):find("player: tanking=unknown status=unknown pct=unknown"), "secret threat: " .. text("threat"))
w.combat = false
tick(2)
check(not S.threat.pulled, "reset after combat")

---------------------------------------------------------------- 6. RARES on nameplates
w.units.nameplate1 = { exists = true, name = "Lady Moongazer", level = 19, class = "rare", guid = "Creature-0-1403-0-125-2184-000ABC" }
w.units.nameplate2 = { exists = true, name = "Moonstalker", level = 18, class = "normal", guid = "Creature-0-1403-0-125-2070-000ABD" }
w.plates = { "nameplate1", "nameplate2" }
tick(3)
local rare = text("rares")
check(rare:find("Lady Moongazer | npc 2184 | L19 rare"), "rare line: " .. rare)
check(not rare:find("Moonstalker"), "normal creatures are not rares")
local seen = 0
for _, e in ipairs(events) do if e.t == "rare_seen" then seen = seen + 1; check(e.d.npc == 2184 and e.d.name == "Lady Moongazer", "rare event data") end end
tick(5)
for _, e in ipairs(events) do if e.t == "rare_seen" then seen = seen + 0 end end
check(seen == 1, "one rare_seen per creature, got " .. seen)

check(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))
check(#W.unsafe == 0, "unsafe calls: " .. table.concat(W.unsafe, ","))
if fails > 0 then error(fails .. " failure(s)", 0) end
io.write("coach data ok\n")
