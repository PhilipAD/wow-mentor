-- CoachGoals sections: LEVELING, ENEMIES, DUELS, PVP_KIT, PVP_GEAR, PROF_TARGETS. Runs under plain Lua and LuaJIT; no game needed.
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local w = W.installCoach()

local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local XP = { xp = 100, max = 1000 }
_G.UnitXP = function() return XP.xp end
_G.UnitXPMax = function() return XP.max end
_G.GetXPExhaustion = function() return 0 end
_G.IsResting = function() return false end
_G.GetRealZoneText = function() return w.zone end
_G.UnitClass = function(u) local x = w.units[u]; if x and x.cls then return x.cls, x.cls end return "Warlock", "WARLOCK" end
_G.GetGuildInfo = function(u) local x = w.units[u]; return x and x.guild end
_G.UnitIsPVP = function(u) local x = w.units[u]; return x and x.pvp end
w.units.player.level = 20
w.money = 455000

W.loadAddon()
local API, C = WoWFeed_API, WoWFeedCoach
local S, G = C.S, C.goals
check(G ~= nil, "CoachGoals loaded")
local pushed = {}
local realPush = API.push
API.push = function(sec, lines, age) pushed[sec] = lines; return realPush(sec, lines, age) end
local events = {}
local realEmit = C.emit
C.emit = function(t, d) events[#events + 1] = { t = t, d = d }; return realEmit(t, d) end
for _, f in ipairs(W.frames) do if f.events.PLAYER_LOGIN and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_LOGIN") end end
W.fire("PLAYER_ENTERING_WORLD", true, false)
local function tick(n) for _ = 1, n or 1 do W.advance(1); W.runTickers() end end
local function text(sec) return table.concat(pushed[sec] or {}, "\n") end
local function evs(t) local n, last = 0, nil; for _, e in ipairs(events) do if e.t == t then n = n + 1; last = e end end return n, last end
tick(2)

---------------------------------------------------------------- 1. LEVELING
local lv = table.concat(G.levelingLines(), "\n")
check(lv:find("well_fed=no sleeping_bag=no rested_xp=0"), "boosts: " .. lv)
check(lv:find("missing: eat cooked food"), "missing food: " .. lv)
check(lv:find("log out in an inn"), "missing rested: " .. lv)
check(lv:find("riding fund: 45g 50s of about 100g for riding at 40 %(20 levels to go%)"), "riding fund: " .. lv)
w.auras.player = { { name = "Well Fed", id = 1 }, { name = "Cozy Sleeping Bag", id = 2 } }
_G.GetXPExhaustion = function() return 4000 end
lv = table.concat(G.levelingLines(), "\n")
check(lv:find("well_fed=yes sleeping_bag=yes rested_xp=4000") and not lv:find("missing"), "boosts on: " .. lv)
-- quest pace: accepted then handed in 12 minutes later for 1200 xp
W.fire("QUEST_ACCEPTED", 4740)
W.advance(720)
W.fire("QUEST_TURNED_IN", 4740, 1200, 0)
local done = G.db().done
check(#done == 1 and done[1].xp == 1200 and done[1].min == 12 and done[1].zone == "Elwynn Forest", "quest timed: " .. tostring(done[1] and done[1].min))
for i = 1, 6 do W.fire("QUEST_ACCEPTED", 100 + i); W.advance(60 * i); W.fire("QUEST_TURNED_IN", 100 + i, 600, 0) end
lv = table.concat(G.levelingLines(), "\n")
check(lv:find("quests timed: 7"), "count: " .. lv)
check(lv:find("slowest xp/min: #106 600 xp in 6 min") or lv:find("slowest xp/min: [^\n]*600 xp in 6 min"), "slowest first: " .. lv)
check(lv:find("fastest xp/min: [^\n]*600 xp in 1 min"), "fastest: " .. lv)
-- zone pace: 600 xp over 10 active minutes in Elwynn, one death
for i = 1, 600 do if i % 60 == 0 then XP.xp = XP.xp + 60 end; tick(1) end
W.fire("PLAYER_DEAD"); W.fire("PLAYER_UNGHOST")
lv = table.concat(G.levelingLines(), "\n")
check(lv:find("zone pace: Elwynn Forest %d+ xp/h over %d+ min, 1 deaths"), "zone pace: " .. lv)
check(type(WoWFeedDB.goals) == "table", "pace is kept in the saved variables")

---------------------------------------------------------------- 2. ENEMIES
w.units.nameplate1 = { exists = true, isPlayer = true, attackable = true, name = "Stabby", level = 24, cls = "ROGUE", guild = "Gankers", pvp = true, guid = "Player-1-AAA" }
w.units.nameplate2 = { exists = true, isPlayer = false, attackable = true, name = "Murloc", level = 18, guid = "Creature-0-1-2-3-4-5-6" }
w.units.nameplate3 = { exists = true, isPlayer = true, attackable = true, name = "Lowbie", level = 15, cls = "MAGE", guid = "Player-1-BBB" }
w.plates = { "nameplate1", "nameplate2", "nameplate3" }
tick(3)
local en = text("enemies")
check(en:find("Stabby | ROGUE | L24 | guild Gankers | pvp=true | in view"), "enemy line: " .. en)
check(en:find("Lowbie | MAGE | L15"), "low level enemy listed: " .. en)
check(not en:find("Murloc"), "creatures are not enemy players")
local n, last = evs("enemy_player")
check(n == 1 and last.d.name == "Stabby" and last.d.level == 24, "one warning for the dangerous one, got " .. n)
tick(10)
check(evs("enemy_player") == 1, "no repeat inside 10 minutes")
w.plates = {}
W.advance(200); tick(3)
check(text("enemies"):find("no enemy players seen"), "forgotten after 2 min: " .. text("enemies"))

---------------------------------------------------------------- 3. DUELS and the record by class
_G.DUEL_WINNER_KNOCKOUT = "%1$s has defeated %2$s in a duel"
_G.DUEL_WINNER_RETREAT = "%2$s has fled from %1$s in a duel"
local a, b = G.duelResult("Tester has defeated Stabby in a duel")
check(a == "Tester" and b == "Stabby", "knockout parse")
a, b = G.duelResult("Tester has fled from Stabby in a duel")
check(a == "Stabby" and b == "Tester", "retreat: the one who fled lost")
w.units.target = { exists = true, name = "Stabby", level = 24, cls = "ROGUE" }
W.fire("CHAT_MSG_SYSTEM", "Stabby has defeated Tester in a duel")
W.fire("CHAT_MSG_SYSTEM", "Tester has defeated Stabby in a duel")
W.fire("CHAT_MSG_SYSTEM", "Someone has defeated Other in a duel")             -- not his duel
W.fire("CHAT_MSG_SYSTEM", "You are now AFK")
_G.WarlordsForeverCharDB = { players = { g = { name = "Stabby", class = "ROGUE", kills = 1, deaths = 3 }, h = { name = "Zap", class = "MAGE", kills = 2, deaths = 0 } } }
local du = table.concat(G.duelLines(), "\n")
check(du:find("duels: won 1 lost 1"), "duel totals: " .. du)
check(du:find("vs ROGUE: duels 1%-1, world pvp kills 1 deaths 3"), "rogue record: " .. du)
check(du:find("vs MAGE: duels 0%-0, world pvp kills 2 deaths 0"), "mage record: " .. du)
check(evs("duel_result") == 2, "two duel_result events")
_G.WarlordsForeverCharDB = nil
w.units.target = { exists = false }

---------------------------------------------------------------- 4. PVP_KIT
w.counts = { [858] = 2, [5509] = 1, [6265] = 2, [4390] = 3 }
_G.GetItemCooldown = function(id) if id == 4390 then return W.clock - 20, 60, 1 end return 0, 0, 1 end
local kit = table.concat(G.kitLines(), "\n")
check(kit:find("healing potions=2"), "healing potions: " .. kit)
check(kit:find("grenades and bombs %(shared 1 min cooldown%)=3 %(40s%)"), "device cooldown: " .. kit)
check(kit:find("warlock: healthstones=1 soulstones=0 soul_shards=2"), "warlock: " .. kit)
check(kit:find("pvp ready: no, missing 3%+ soul shards") and kit:find("Swiftness Potion"), "verdict: " .. kit)
check(not kit:find("Free Action Potion %(drink"), "FAP not asked below 30: " .. kit)
w.counts[6265] = 5; w.counts[2459] = 1
check(table.concat(G.kitLines(), "\n"):find("pvp ready: yes"), "ready: " .. table.concat(G.kitLines(), "\n"))

---------------------------------------------------------------- 5. PVP_GEAR
w.itemDB[9001] = { name = "Sturdy Robe", equipLoc = "INVTYPE_ROBE", quality = 2, link = "|Hitem:9001|h[Sturdy Robe]|h" }
w.itemDB[9002] = { name = "Glass Robe", equipLoc = "INVTYPE_CHEST", quality = 2 }
w.itemStats[9001] = { STAMINA = 10, INTELLECT = 2 }
w.itemStats[9002] = { STAMINA = 2, INTELLECT = 4 }
w.equipped[5] = "|Hitem:9002|h[Glass Robe]|h"
w.bagItems["0:1"] = { hyperlink = "|cff1eff00|Hitem:9001::|h[Sturdy Robe]|h|r", quality = 2, stackCount = 1, itemID = 9001 }
local pg = table.concat(G.pvpGearLines(), "\n")
check(pg:find("pvp gear upgrades in bags: 1"), "one upgrade: " .. pg)
check(pg:find("Sturdy Robe %(robe%) pvp_gain=%+7%.3") or pg:find("Sturdy Robe %(robe%) pvp_gain=%+7%.%d"), "gain: " .. pg)
check(G.pvpScore({ STAMINA = 10 }) == 10 and G.pvpScore({ SPELL_POWER = 10 }) == 9, "weights")

---------------------------------------------------------------- 6. PROF_TARGETS
check(G.profTargetLines()[1]:find("not loaded"), "without Crafter's Journal")
_G.CraftersJournal = { Data = { Alchemy = { skillLine = 171, recipes = {
	[6624] = { name = "Free Action Potion", t = { 150, 175, 195, 215 }, scroll = 5642, out = { 5634, 1 } },
	[2335] = { name = "Swiftness Potion", t = { 60, 90, 110, 130 }, train = 100, out = { 2459, 1 } } } },
	Engineering = { skillLine = 202, recipes = { [3962] = { name = "Iron Grenade", t = { 175, 175, 195, 215 }, train = 900, out = { 4390, 1 } } } } },
	GetProfessionSkill = function(self, line) if line == 171 then return 53, 75 elseif line == 202 then return 25, 75 end end,
	IsRecipeKnown = function(self, spell) return spell == 2335 end }
local pt = table.concat(G.profTargetLines(), "\n")
check(pt:find("Alchemy %(skill 53%): Swiftness Potion skill 60, trainer, known; Free Action Potion skill 150, recipe item 5642"), "alchemy targets: " .. pt)
check(pt:find("Engineering %(skill 25%): Iron Grenade skill 175, trainer"), "engineering targets: " .. pt)
_G.CraftersJournal = nil

tick(5)
check(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))
check(#W.unsafe == 0, "unsafe calls: " .. table.concat(W.unsafe, ","))
if fails > 0 then error(fails .. " failure(s)", 0) end
io.write("coach goals ok\n")
