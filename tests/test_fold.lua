-- Dropdown headings: closed sections show one short line and none of their rows; a click opens them; the choice is remembered
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local secure = {}
local baseCF = CreateFrame
CreateFrame = function(kind, name, parent, template)
	local f = baseCF(kind, name, parent)
	f.template, f.attrs = template, {}
	f.SetAttribute = function(self, k, v) self.attrs[k] = v end
	f.GetAttribute = function(self, k) return self.attrs[k] end
	f.parent = parent
	f.GetParent = function(self) return self.parent end
	f.SetParent = function(self, p) self.parent = p end
	if template == "SecureActionButtonTemplate" then secure[#secure + 1] = f end
	return f
end
InCombatLockdown = function() return false end
C_QuestLog = { IsQuestFlaggedCompleted = function() return false end, IsComplete = function() return false end }
dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function(sec, lines) W.pushed[sec] = lines end, coords = function() return 47.9, 42.7 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local S = WoWFeedUI.state
local doc = [[{"v":1,"rev":1,"theme":{},"panels":[{"title":"What to get here","w":140,"id":"squares","children":[
 {"id":"qall","targets":["Murkdeep","Reef Crawler"],"texture":"Ability_Hunter_SniperShot","iconSize":26,"text":"Target any","type":"icon"},
 {"id":"hq","text":"Quests: kill, then loot","short":"Quests (1)","fold":true,"type":"heading"},
 {"id":"q1","type":"row","children":[{"npc":10323,"tip":"Murkdeep","texture":"x","iconSize":26,"quest":4740,"type":"icon"}]},
 {"id":"hr","text":"Rares here: Lady Moongazer seen alive in the last 15 min","short":"Rare up: Lady Moongazer","fold":true,"open":true,"type":"heading"},
 {"id":"rr1","type":"row","children":[{"npc":2184,"tip":"Lady Moongazer","texture":"x","iconSize":26,"type":"icon"}]},
 {"id":"hp1","text":"Cooking 82/150: Crab Cake (orange)","short":"Cooking 82/150","fold":true,"type":"heading"},
 {"id":"p1_1","type":"row","children":[{"item":2674,"tip":"Crawler Meat","iconSize":20,"type":"icon"},{"npc":2235,"tip":"Reef Crawler","texture":"x","iconSize":20,"type":"icon"}]},
 {"id":"hx","text":"A plain heading","type":"heading"},
 {"id":"tx","text":"always shown","type":"text"}]}],"waypoints":[],"toasts":[],"actions":[],"builtin":{},"rules":[],"inbox":[]}]]
W.slots["WFSlot000"] = { rev = 1, json = doc }
W.meta = 1
for _, f in ipairs(W.frames) do if f.events.PLAYER_LOGIN and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_LOGIN") end end
W.runTimers()
local function tick(n) for _ = 1, n or 5 do W.advance(0.1); W.runDriver(0.1) end end
tick(10)
local P = S.panels.squares
assert(P and P.data, "panel exists"); assert(#S.errors == 0, table.concat(S.errors, " | "))
local function shownTexts()
	local t = {}
	for i = 1, P.n.fs do t[P.fs[i].text or ""] = true end
	return t
end
local function shownSecure()
	local n, names = 0, {}
	for _, b in ipairs(secure) do if b.shown then n = n + 1; names[#names + 1] = b.wfKey or "?" end end
	return n, table.concat(names, " ; ")
end
local function clickHeading(prefix)
	for i = 1, P.n.rows do
		local r = P.rows[i]
		if r.shown and r.scripts.OnClick and r.foldTest == nil then
			-- find the heading row by trying each: a fold click marks the panel dirty
		end
	end
	local before = P.foldOpen
	for i = 1, P.n.rows do
		local r = P.rows[i]
		if r.shown and r.scripts.OnClick and r.wfFold == prefix then r.scripts.OnClick(r); tick(3); return true end
	end
	return false
end

-- 1. closed by default: one short line each, no rows; Target any and its secure button stay; the open rare section shows its row
local t = shownTexts()
assert(t["+ Quests (1)"], "closed quests heading shows the short label")
assert(t["+ Cooking 82/150"], "closed profession heading")
assert(t["- Rares here: Lady Moongazer seen alive in the last 15 min"], "a section the document opens shows its full text")
assert(t["A plain heading"] and t["always shown"], "plain headings and what follows them are untouched")
local n, names = shownSecure()
assert(n == 2, "Target any + the rare square only, got " .. n .. ": " .. names)
local hClosed = P.height

-- 2. a click opens the section: full heading text, its rows and their target buttons
assert(clickHeading("hq"), "the quests heading is clickable")
t = shownTexts()
assert(t["- Quests: kill, then loot"], "open quests heading shows the full text")
n, names = shownSecure()
assert(n == 3, "Murkdeep square now shown: " .. names)
assert(P.height > hClosed, "the panel grows when a section opens")
assert(WoWFeedDB.ui.user.squares.fold.hq == true, "the choice is saved")

-- 3. the player's choice wins over the document: closing the rare section sticks through a new document revision
assert(clickHeading("hr"))
W.slots["WFSlot001"] = { rev = 2, json = (doc:gsub('"rev":1', '"rev":2')) }
W.meta = 2
W.advance(5); W.runTickers(); tick(5)
t = shownTexts()
assert(t["+ Rare up: Lady Moongazer"], "closed by the player stays closed")
assert(t["- Quests: kill, then loot"], "opened by the player stays open")
assert(#S.errors == 0, table.concat(S.errors, " | "))
io.stdout:write("fold ok\n")
