-- "What to get here" in creature tooltips: profession droppers and rares in the squares panel carry a `why` line; hovering that
-- creature adds it to the tooltip. Quest creatures (Questie already covers them) and unknown units add nothing.
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
InCombatLockdown = function() return false end
C_QuestLog = { IsQuestFlaggedCompleted = function() return false end, IsComplete = function() return false end }
local postcalls = {}
Enum = Enum or {}
Enum.TooltipDataType = { Item = 0, Unit = 2 }
TooltipDataProcessor = { AddTooltipPostCall = function(kind, fn) postcalls[kind] = fn end }
dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function(sec, lines) W.pushed[sec] = lines end, coords = function() return 47.9, 42.7 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local UI = WoWFeedUI
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local doc = [[{"v":1,"rev":7,"theme":{},"panels":[{"title":"What to get here","w":140,"id":"squares","children":[
 {"id":"qall","targets":["Murloc Flesheater","Dire Condor","Kazon"],"texture":"Ability_Hunter_SniperShot","iconSize":26,"text":"Target any","type":"icon"},
 {"id":"hq","type":"heading","text":"Quests: kill, then loot","fold":true,"short":"Quests (3)"},
 {"id":"q1","type":"row","children":[{"type":"icon","npc":422,"tip":"Murloc Flesheater","quest":127,"why":"should never show"}]},
 {"id":"hp1","type":"heading","text":"Cooking 82/150","fold":true},
 {"id":"p1_1","type":"row","children":[{"type":"icon","item":1080,"tip":"Tough Condor Meat for Redridge Goulash"},
   {"type":"icon","npc":428,"tip":"Dire Condor","why":"Cooking 82/150: drops Tough Condor Meat 35.1%, for Redridge Goulash"}]},
 {"id":"p1_2","type":"row","children":[{"type":"icon","npc":428,"tip":"Dire Condor","why":"Cooking 82/150: drops Tough Condor Meat 35.1%, for Redridge Goulash"},
   {"type":"icon","npc":428,"tip":"Dire Condor","why":"First Aid 84/150: drops nothing useful"}]},
 {"id":"hr","type":"heading","text":"Rares here (none seen yet)","fold":true},
 {"id":"rr1","type":"row","children":[{"type":"icon","npc":584,"tip":"Kazon","why":"Rare elite, level 27: worth killing for its loot and XP"}]}
]}]}]]
local ok, err = pcall(UI.apply, doc)
check(ok, "apply: " .. tostring(err))

check(UI.npcIdOf("Creature-0-4170-0-31-428-000043AB12") == 428, "npc id from a creature guid")
check(UI.npcIdOf("Player-4170-0ABCDEF") == nil, "players are not creatures")
check(UI.npcIdOf(nil) == nil, "no guid")

local condor = UI.unitWhy("Creature-0-4170-0-31-428-000043AB12")
check(condor and condor:find("Tough Condor Meat", 1, true), "profession dropper has its why")
check(condor and select(2, condor:gsub("Tough Condor Meat", "")) == 1, "the same why is not repeated when the creature is listed twice")
check(condor and condor:find("First Aid", 1, true), "a second, different why for the same creature is kept")
check(UI.unitWhy("Creature-0-4170-0-31-584-0000000001"):find("Rare elite", 1, true), "rare has its why")
check(UI.unitWhy("Creature-0-4170-0-31-422-0000000001") == nil, "quest creatures are left to Questie")
check(UI.unitWhy("Creature-0-4170-0-31-999-0000000001") == nil, "unlisted creature adds nothing")
check(UI.unitWhy(nil, "Kazon"):find("Rare elite", 1, true), "falls back to the name when there is no guid")

-- the tooltip hook adds one line per reason
local hook = postcalls[Enum.TooltipDataType.Unit]
check(type(hook) == "function", "unit tooltip post-call registered")
local added = {}
local tt = { AddLine = function(_, text) added[#added + 1] = text end }
if hook then hook(tt, { guid = "Creature-0-4170-0-31-428-000043AB12" }) end
check(#added == 2 and added[1]:find("What to get here:", 1, true) and added[1]:find("Tough Condor Meat", 1, true), "tooltip lines added for the condor, got " .. #added)
added = {}
if hook then hook(tt, { guid = "Creature-0-4170-0-31-999-0000000001" }) end
check(#added == 0, "nothing added for an unlisted creature")
-- secret values (the game hides some unit data): never error, add nothing
issecretvalue = function(v) return v == "SECRET" end
added = {}
if hook then hook(tt, { guid = "SECRET" }) end
check(#added == 0, "secret guid adds nothing")
issecretvalue = nil

-- at most three reasons, then a count
local many = {}
for i = 1, 5 do many[#many + 1] = string.format('{"type":"icon","npc":505,"tip":"Greater Tarantula","why":"Cooking: reason %d"}', i) end
pcall(UI.apply, (doc:gsub('"rev":7', '"rev":9'):gsub('{"id":"hr"', '{"id":"p9","type":"row","children":[' .. table.concat(many, ",") .. ']},{"id":"hr"')))
added = {}
if hook then hook(tt, { guid = "Creature-0-4170-0-31-505-0000000001" }) end
check(#added == 4 and added[4]:find("and 2 more", 1, true), "three reasons then a count, got " .. #added)

-- a new document without the creature removes its line
local doc2 = doc:gsub('"npc":428,"tip":"Dire Condor","why":"[^"]*"', '"npc":428,"tip":"Dire Condor"')
pcall(UI.apply, (doc2:gsub('"rev":7', '"rev":10')))
check(UI.unitWhy("Creature-0-4170-0-31-428-000043AB12") == nil, "line gone once the server drops the reason")

if fails > 0 then error(fails .. " failure(s)", 0) end
io.write("unit why ok\n")
