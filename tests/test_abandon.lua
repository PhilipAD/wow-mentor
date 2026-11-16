-- abandon_quest: drops one quest from the log out of combat, leaves quests that are not in the log alone, never in combat.
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function() end, coords = function() return 50, 50 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local UI = WoWFeedUI
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local log = { [98025] = { idx = 3, title = "WANTED: Jai'vhanel" }, [1141] = { idx = 5, title = "The Family and the Fishing Pole" } }
local selected, armed, abandoned, chat = nil, false, {}, {}
local combat = false
InCombatLockdown = function() return combat end
C_QuestLog = {
	GetLogIndexForQuestID = function(q) return log[q] and log[q].idx or nil end,
	GetInfo = function(i) for q, e in pairs(log) do if e.idx == i then return { title = e.title, questID = q } end end end,
	SetSelectedQuest = function(q) selected = q end,
	SetAbandonQuest = function() armed = true end,
	AbandonQuest = function() if armed and selected then abandoned[#abandoned + 1] = selected; log[selected] = nil end; armed = false end,
}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, m) chat[#chat + 1] = m end }

check(UI.abandonQuest(98025) == "WANTED: Jai'vhanel", "abandons a quest in the log")
check(abandoned[1] == 98025 and log[98025] == nil, "the right quest went")
check(chat[1] and chat[1]:find("abandoned WANTED", 1, true), "says so in chat")
check(UI.abandonQuest(424242) == nil and #abandoned == 1, "a quest not in the log is left alone")
combat = true
check(UI.abandonQuest(1141) == nil and log[1141] ~= nil, "never in combat")
combat = false
check(UI.abandonQuest("1141") == "The Family and the Fishing Pole", "accepts the id as a string")
check(UI.abandonQuest(nil) == nil, "no id: nothing")
if fails > 0 then error(fails .. " failure(s)", 0) end
io.write("abandon ok\n")
