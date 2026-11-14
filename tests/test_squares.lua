-- Preview squares in the game: secure target buttons, the target-all macro, instant hiding of finished quests, combat lockdown
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local function slurp(p) local f = assert(io.open(p)); local s = f:read("*a"); f:close(); return s end

-- record templates, attributes and click scripts the way the real client keeps them
local secure = {}
local baseCF = CreateFrame
CreateFrame = function(kind, name, parent, template)
	local f = baseCF(kind, name, parent)
	f.template, f.attrs = template, {}
	f.SetAttribute = function(self, k, v)
		if self.template == "SecureActionButtonTemplate" and W.combat then error("ADDON_ACTION_BLOCKED: SetAttribute in combat") end
		self.attrs[k] = v
	end
	f.GetAttribute = function(self, k) return self.attrs[k] end
	f.parent = parent
	f.GetParent = function(self) return self.parent end
	f.SetParent = function(self, p)
		if self.template == "SecureActionButtonTemplate" and W.combat then error("ADDON_ACTION_BLOCKED: SetParent in combat") end
		self.parent = p
	end
	if template == "SecureActionButtonTemplate" then
		secure[#secure + 1] = f
		f.scripts.OnClick = "SecureActionButton_OnClick"            -- the template's own handler
		local show, hide, setp = f.Show, f.Hide, f.SetPoint
		f.Show = function(self) if W.combat then error("ADDON_ACTION_BLOCKED: Show in combat") end return show(self) end
		f.Hide = function(self) if W.combat then error("ADDON_ACTION_BLOCKED: Hide in combat") end return hide(self) end
	end
	return f
end
InCombatLockdown = function() return W.combat == true end
local done = {}
C_QuestLog = { IsQuestFlaggedCompleted = function(id) return done[id] == "turned_in" end, IsComplete = function(id) return done[id] == "complete" end }
GetQuestLogIndexByID = function() return 0 end

dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function(sec, lines) W.pushed[sec] = lines end, coords = function() return 47.9, 42.7 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local S = WoWFeedUI.state

W.slots["WFSlot000"] = { rev = 1, json = slurp("tests/fixtures/squares_doc.json") }
W.meta = 1
for _, f in ipairs(W.frames) do if f.events.PLAYER_LOGIN and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_LOGIN") end end
W.runTimers()
local function tick(n) for _ = 1, n or 5 do W.advance(0.1); W.runDriver(0.1) end end
tick(10)
assert(S.panels.squares and S.panels.squares.data, "squares panel exists")
assert(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))

local function shownSecure()
	local out = {}
	for _, b in ipairs(secure) do if b.shown then out[#out + 1] = b end end
	return out
end
local function macros()
	local m = {}
	for _, b in ipairs(shownSecure()) do m[#m + 1] = b.attrs.macrotext end
	table.sort(m)
	return m
end

-- 1. every creature square and the target-all square get a secure button with a clean macro; the template's OnClick is never replaced
local m = macros()
assert(#m == 3, "three secure buttons (qall, Sire, Murkdeep), got " .. #m .. ": " .. table.concat(m, " || "))
local D = "\n/cleartarget [dead]"
assert(m[1] == "/cleartarget\n/targetexact Moonstalker Sire" .. D, m[1])
assert(m[2] == "/cleartarget\n/targetexact Moonstalker Sire" .. D .. "\n/targetexact Murkdeep" .. D, m[2])
assert(m[3] == "/cleartarget\n/targetexact Murkdeep" .. D, m[3])
local body = S.panels.squares.body; assert(body, "panel body exists")
for _, b in ipairs(shownSecure()) do assert(b.parent == body, "secure buttons sit on the panel body out of combat") end
for _, b in ipairs(secure) do assert(b.scripts.OnClick == "SecureActionButton_OnClick", "secure OnClick replaced") end
for _, b in ipairs(shownSecure()) do assert(b.attrs.type == "macro", "type macro") end

-- 2. a quest's objectives complete: its squares disappear and its name leaves the target-all macro on the next quest event
done[986] = "complete"
for _, f in ipairs(W.frames) do if f.events.QUEST_LOG_UPDATE and f.scripts.OnEvent then f.scripts.OnEvent(f, "QUEST_LOG_UPDATE") end end
tick(5)
m = macros()
assert(#m == 2, "Sire button gone, got " .. table.concat(m, " || "))
assert(m[1] == "/cleartarget\n/targetexact Murkdeep" .. D and m[2] == m[1], table.concat(m, " || "))
assert(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))

-- 3. combat starts: every secure button leaves the panel (onto UIParent, hidden) BEFORE the lockdown, so the panel never becomes protected;
--    a quest that finishes mid-fight touches nothing secure (no ADDON_ACTION_BLOCKED); after combat the buttons come back on the panel
for _, f in ipairs(W.frames) do if f.events.PLAYER_REGEN_DISABLED and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_REGEN_DISABLED") end end
for _, b in ipairs(secure) do assert(not b.shown and b.parent == UIParent, "secure button detached as combat starts") end
W.combat = true
done[4740] = "turned_in"
for _, f in ipairs(W.frames) do if f.events.QUEST_TURNED_IN and f.scripts.OnEvent then f.scripts.OnEvent(f, "QUEST_TURNED_IN", 4740) end end
tick(5)
assert(#S.errors == 0, "no blocked secure calls in combat: " .. table.concat(S.errors, " | "))
W.combat = false
tick(5)
m = macros()
assert(#m == 0, "all quest buttons gone after combat, got " .. table.concat(m, " || "))
-- and with a live quest again they come back onto the panel after combat
done[4740] = nil
for _, f in ipairs(W.frames) do if f.events.QUEST_LOG_UPDATE and f.scripts.OnEvent then f.scripts.OnEvent(f, "QUEST_LOG_UPDATE") end end
tick(5)
m = macros()
assert(#m == 2, "Murkdeep buttons back after combat: " .. table.concat(m, " || "))
for _, b in ipairs(shownSecure()) do assert(b.parent == body, "back on the panel body") end
assert(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))

-- 4. a name smuggled past the server still cannot add a macro line
local evil = slurp("tests/fixtures/squares_doc.json"):gsub('"tip":"Murkdeep"', '"tip":"Murkdeep\\n/run Logout()"'):gsub('"rev":1', '"rev":2')
done[4740] = nil
W.slots["WFSlot001"] = { rev = 2, json = evil }
W.meta = 2
W.advance(5); W.runTickers(); tick(5)
local mm = macros()
assert(#mm >= 1, "the Murkdeep buttons are back for the injection check")
assert(mm[1]:find("^/cleartarget\n/targetexact Murkdeep"), "unexpected: " .. mm[1])
for _, t in ipairs(mm) do
	assert(not t:find("/run"), "injected line survived: " .. t)
	for line in t:gmatch("[^\n]+") do assert(line == "/cleartarget" or line == "/cleartarget [dead]" or line:find("^/targetexact [%w ',%.:%-]+$"), "unexpected macro line: " .. line) end
end
io.stdout:write("squares ok: " .. #secure .. " secure buttons created, last macros: " .. table.concat(macros(), " || "):gsub("\n", "\\n") .. "\n")

-- 5. eight long names: the macro stays within 255 characters and keeps the LAST (most important) names
done = {}
local doc2 = slurp("tests/fixtures/squares_doc2.json"):gsub('"rev":1', '"rev":3')
W.slots["WFSlot002"] = { rev = 3, json = doc2 }
W.meta = 3
W.advance(5); W.runTickers(); tick(5)
local longest
for _, t in ipairs(macros()) do if not longest or #t > #longest then longest = t end end
assert(longest and #longest <= 255, "macro over 255: " .. tostring(longest and #longest))
assert(longest:find("/targetexact Murkdeep\n/cleartarget %[dead%]$"), "the last (most important) name survives: " .. longest)
assert(not longest:find("Foreststrider Fledgling"), "the first names are the ones dropped: " .. longest)

-- 6. a creature two quests need stays in the target-all square when only one of them is done
done[986] = "complete"
for _, f in ipairs(W.frames) do if f.events.QUEST_LOG_UPDATE and f.scripts.OnEvent then f.scripts.OnEvent(f, "QUEST_LOG_UPDATE") end end
tick(5)
local hasSire = false
for _, t in ipairs(macros()) do if t == "/cleartarget\n/targetexact Moonstalker Sire\n/cleartarget [dead]" then hasSire = true end end
assert(hasSire, "Moonstalker Sire still targetable for quest 5555: " .. table.concat(macros(), " || "))
assert(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))
io.stdout:write("squares long/shared ok\n")
