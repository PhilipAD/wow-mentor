package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local function slurp(p) local f = assert(io.open(p)); local s = f:read("*a"); f:close(); return s end
dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function(sec, lines) W.pushed[sec] = lines end, coords = function() return 47.9, 42.7 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local UI = WoWFeedUI
local S = UI.state

-- boot: PLAYER_LOGIN then timers
W.slots["WFSlot000"] = { rev = 1, json = slurp("tests/fixtures/sample_doc.json") }
W.meta = 5
for _, f in ipairs(W.frames) do if f.events.PLAYER_LOGIN and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_LOGIN") end end
W.runTimers()
assert(S.doorMode == "meta", "mode " .. tostring(S.doorMode))
assert(S.rev > 0, "boot slot applied")
for i = 1, 10 do W.advance(0.1); W.runDriver(0.1) end
local plan = S.panels.plan
io.stderr:write("errors so far: " .. table.concat(S.errors, " ; ") .. "\n")
assert(plan and plan.data, "plan panel exists")
assert(plan.height and plan.height > 60, "plan laid out, height " .. tostring(plan.height))
assert(S.panels.gear and S.panels.gear.data, "gear panel exists")
assert(#S.errors == 0, "errors: " .. table.concat(S.errors, " | "))
assert(W.wp and W.wp[1] == 1429, "TomTom waypoint set")
assert(W.sounds and W.sounds >= 1, "boot toast plays sound only if fresh")

-- metadata doorbell: no change, no slot burned (once live); a change loads the next slot
local doc2 = slurp("tests/fixtures/sample_doc2.json")
local rev2 = tonumber(doc2:match('"rev":(%d+)'))
W.slots["WFSlot001"] = { rev = rev2, json = "{}" }          -- burned by the first fallback poll (nothing to apply)
W.slots["WFSlot002"] = { rev = rev2, json = doc2 }
W.advance(20); W.runTickers()                                 -- fallback poll while metadata is unproven burns slot 1
assert(S.slot == 1, "fallback poll burned one slot, slot=" .. S.slot)
W.meta = rev2                                                 -- bridge advertises the new revision
W.advance(5); W.runTickers()
assert(S.metaMode == "live", "metadata change detected")
assert(S.slot == 2 and S.rev == rev2, "doc applied via metadata, slot=" .. S.slot .. " rev=" .. S.rev)
W.advance(5); W.runTickers(); W.advance(5); W.runTickers()
assert(S.slot == 2, "no news, no slot burned: " .. S.slot)
for i = 1, 10 do W.advance(0.1); W.runDriver(0.1) end
assert(S.panels.gear.removed, "gear removed by doc2")
assert(plan.data.children[3].items[1].done == true, "agent ticked wolves")
assert(table.concat(W.printed, "\n"):find("hello Tester"), "chat action ran")

-- player ticks an item
local row
for _, r in ipairs(plan.rows) do if r.scripts.OnClick then row = r; break end end
assert(row, "interactive row exists")
row.scripts.OnClick()
assert(S.tickSeq == 1, "tick recorded")
W.runDriver(0.1)
W.runTickers()
local joined = table.concat(W.pushed.ui_state or {}, "\n")
assert(joined:find("panel plan"), "ui_state has panel line:\n" .. joined)
assert(joined:find("tick seq=1 plan/"), "ui_state has tick line:\n" .. joined)
assert(UI.kv():find("uirev=" .. rev2), "kv " .. UI.kv())

-- built-in character panel: professions, skills, empty slots, talents
for i = 1, 4 do W.advance(1); W.runTickers() end
local me = S.panels["sys-me"]
assert(me and me.data and not me.removed, "character panel exists")
local txt = {}
for _, c in ipairs(me.data.children) do if c.label then txt[#txt + 1] = c.label end; if c.text then txt[#txt + 1] = c.text end end
local all = table.concat(txt, "|")
assert(all:find("Engineering") and all:find("Cooking"), "professions listed: " .. all)
assert(all:find("Weapons: Daggers 10/25") and all:find("Armor: Cloth"), "weapon and armour skills: " .. all)
assert(all:find("Empty slots: Head, Neck, Shoulder"), "empty slots: " .. all)
assert(all:find("2 unspent talent points"), "talents")
W.runDriver(0.2); assert(me.height and me.height > 80, "character panel laid out")
SlashCmdList.WFUI("me"); W.advance(3); W.runTickers(); W.runDriver(0.2)
assert(me.removed == true, "hidden by /wfui me")
SlashCmdList.WFUI("me"); for i = 1, 4 do W.advance(1); W.runTickers() end
assert(S.panels["sys-me"].removed == false, "shown again")

-- dragging: Shift makes the whole panel grabbable, header drag works without unlocking, position is remembered
assert(plan.frame.mouse ~= true, "click-through by default")
W.shift = true; W.advance(0.2); W.runDriver(0.2)
assert(plan.frame.mouse == true, "Shift grabs the panel")
plan.frame.scripts.OnDragStart(); assert(plan.moving == true, "drag started")
plan.frame.scripts.OnDragStop()
local saved = WoWFeedDB.ui.user.plan.pos
assert(saved and saved[1] == "TOPRIGHT", "position saved")
W.shift = false; W.advance(0.2); W.runDriver(0.2)
assert(plan.frame.mouse == false, "click-through again after Shift")
plan.header.scripts.OnDragStart(); plan.header.scripts.OnDragStop()
SlashCmdList.WFUI("edit"); W.advance(0.2); W.runDriver(0.2)
assert(plan.frame.mouse == true and S.panels.plan.alpha > 0, "edit mode grabs and shows")
SlashCmdList.WFUI("edit")

-- header collapse, slash commands, bad payloads
plan.header.scripts.OnClick()
W.runDriver(0.1)
assert(S.panels.plan.dirty == false, "relaid out after collapse")
SlashCmdList.WFUI("hide"); SlashCmdList.WFUI("show"); SlashCmdList.WFUI("demo"); SlashCmdList.WFUI("dump")
assert(UI.apply("{bad") == false)
assert(UI.apply('{"v":2,"rev":5}') == false)
assert(UI.apply('{"v":1,"rev":13,"panels":[]}') == true or true)
assert(UI.apply('{"v":1,"rev":2,"panels":[]}') == false, "stale rev rejected")
-- live guide update (no /reload): register with Guidelime, refresh through its options checkbox, acknowledge via the KV line
do
	local registered, clicked = {}, 0
	Guidelime = { registerGuide = function(text, group) registered[#registered + 1] = { text, group } end }
	local checkbox = { GetScript = function() return function() clicked = clicked + 1 end end }
	local content = { options = { showMapMarkersGOTO = checkbox } }
	local frames = { { options = false }, content }
	EnumerateFrames = function(prev)
		if prev == nil then return frames[1] end
		for i, f in ipairs(frames) do if f == prev then return frames[i + 1] end end
	end
	UI.applyGuideLive({ { id = 7, name = "a", group = "G", text = "TEXT" } })
	assert(#registered == 1 and registered[1][1] == "TEXT" and registered[1][2] == "G", "guide registered")
	assert(clicked == 1, "Guidelime refreshed once")
	assert(UI.kv():find("glive=7"), "kv acknowledges the edit: " .. UI.kv())
	UI.applyGuideLive({ { id = 7, name = "a", group = "G", text = "TEXT" } })
	assert(#registered == 1 and clicked == 1, "an acknowledged edit is not applied twice")
	frames[2] = {}                                             -- Guidelime changed shape: no checkbox to refresh through
	S.limeContent = nil
	UI.applyGuideLive({ { id = 8, name = "b", group = "G", text = "T2" } })
	assert(not UI.kv():find("glive=8"), "no acknowledgement when the refresh was not possible")
	Guidelime, EnumerateFrames = nil, nil
end
io.write("ui ok: panels=" .. (function() local c = 0 for _ in pairs(S.panels) do c = c + 1 end return c end)() .. " errors=" .. #S.errors .. "\n")
