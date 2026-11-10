-- "Level and XP are never shown": everything the addon draws or prints for the player (panel text, toasts, chat lines, built-in
-- panels, the waypoint HUD, slash command output) is captured while the character has distinctive level / XP values, and none
-- of them may appear. Placeholders for level and XP expand to nothing and never stay literal.
package.path = "tests/?.lua;" .. package.path
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local W = dofile("tests/wowstub.lua")
W.install()
local w = W.installCoach()
_G.UnitLevel = function() return 58 end
_G.UnitXP = function() return 31337 end
_G.UnitXPMax = function() return 77777 end
w.units.player.level = 58
W.loadAddon()
local C, UI, J, API = WoWFeedCoach, WoWFeedUI, WoWFeedJSON, WoWFeed_API
W.fire("PLAYER_LOGIN"); W.runTimers()
W.fire("PLAYER_ENTERING_WORLD", true, false)
w.pos = { 0.1234, 0.5678 }
W.texts = {}
W.printed = {}

local hostile = "{level}{xp}{xpmax}{xppct}[{level}] {xp}/{xpmax} ({xppct}%)"
local doc = { v = 1, rev = 50, now = W.epoch, theme = {},
	panels = { { id = "plain", title = "Lvl {level}", w = 280, children = {
		{ type = "text", text = hostile }, { type = "heading", text = "{xp}" }, { type = "callout", level = "tip", title = "{level}", text = hostile },
		{ type = "progress", bind = "xp", label = "XP" }, { type = "progress", bind = "level", label = "Level" },
		{ type = "checklist", items = { { id = "a", text = "Reach {level}", note = "{xp}" } } },
		{ type = "row", children = { { type = "text", text = "{level}" }, { type = "progress", bind = "xpmax" } } },
	} } },
	waypoints = { { id = "wp", map = 1429, x = 40, y = 40, title = "Go {level} {xp}" } },
	toasts = { { id = 1, text = hostile, title = hostile, t = W.epoch } },
	actions = { { id = 1, name = "chat", text = hostile, t = W.epoch } },
	inbox = { { id = 1, text = hostile, chat = true, toast = true, t = W.epoch } },
	rules = { { id = "ding", when = { type = "level_up" }, cooldown_s = 0, ["then"] = {
		{ ["do"] = "toast", text = "Ding " .. hostile, title = hostile }, { ["do"] = "chat", text = "Ding " .. hostile }, { ["do"] = "panel_text", panel = "plain", child = "x", text = hostile } } } } }
doc.panels[1].children[1].id = "x"
local ok, err = UI.apply(assert(J.encode(doc)))
check(ok, "document applied: " .. tostring(err))
W.fire("PLAYER_LEVEL_UP", 58)
for _ = 1, 6 do W.advance(1); C.tick(); W.runDriver(0.2) end
UI.refreshBuiltin()
-- hidden progress components draw nothing
local plain = UI.state.panels.plain
check(plain and plain.n.bars == 0, "xp/level progress components draw no bars: bars=" .. tostring(plain and plain.n.bars) .. " dirty=" .. tostring(plain and plain.dirty) .. " fs=" .. tostring(plain and plain.n.fs))
-- the built-in panels and the demo, slash commands
pcall(SlashCmdList.WFUI, "demo"); pcall(SlashCmdList.WFUI, "dump"); pcall(SlashCmdList.WFUI, "me")
pcall(SlashCmdList.WFUI, "me")
pcall(SlashCmdList.COACH, "how do I level fast {level}?")
for _ = 1, 6 do W.advance(1); C.tick(); W.runDriver(0.2) end
-- panels keep their text on the stubs' font strings: read them too
for id, P in pairs(UI.state.panels) do
	for i = 1, P.n.fs do W.texts[#W.texts + 1] = P.fs[i].text end
end
if C.rules.hud then W.texts[#W.texts + 1] = C.rules.hud.title.text; W.texts[#W.texts + 1] = C.rules.hud.dist.text end

local all = {}
for _, t in ipairs(W.texts) do all[#all + 1] = tostring(t) end
for _, t in ipairs(W.printed) do all[#all + 1] = tostring(t) end
check(#all > 20, "the capture saw the overlay draw text (" .. #all .. " strings)")
local sawTitle = false
for _, t in ipairs(all) do
	local low = t:lower()
	if t:find("Go ") or t:find("Ding") then sawTitle = true end
	check(not t:find("31337") and not t:find("77777"), "XP value leaked: " .. t)
	check(not t:find("%f[%d]58%f[%D]"), "level value leaked: " .. t)
	check(not low:find("%f[%a]xp%f[%A]") and not low:find("experience"), "XP wording on the overlay: " .. t)
	check(not low:find("%f[%a]level%f[%A]") or low:find("how do i level fast"), "level wording on the overlay: " .. t)
	check(not t:find("{level}") and not t:find("{xp") and not t:find("{xppct}"), "unexpanded level/xp placeholder: " .. t)
end
check(sawTitle, "rule and waypoint texts were captured")
if fails > 0 then error(fails .. " overlay text test(s) failed", 0) end
io.write("overlay text ok\n")
