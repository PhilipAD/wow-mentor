-- Target any cycles: out of combat each click targets the NEXT listed creature among the enemy nameplates in view (priority order,
-- skipping ones already picked this round), so clicking walks through every one nearby; in combat it falls back to the name macro.
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function() end, coords = function() return 50, 50 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local UI = WoWFeedUI
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local clock, combat, plates, target = 100, false, {}, nil
GetTime = function() return clock end
InCombatLockdown = function() return combat end
local function byUnit(u) for _, p in ipairs(plates) do if p.unit == u then return p end end end
UnitExists = function(u) if u == "target" then return target ~= nil end return byUnit(u) ~= nil end
UnitIsDead = function(u) local p = (u == "target") and byUnit(target) or byUnit(u); return p and p.dead or false end
UnitCanAttack = function(_, u) local p = byUnit(u); return p and not p.friend end
UnitName = function(u) local p = (u == "target") and byUnit(target) or byUnit(u); return p and p.name end
UnitGUID = function(u) local p = byUnit(u); return p and p.guid end
UnitIsUnit = function(a, b) if b == "target" then return a == target end return a == b end

local btn = { attrs = {} }
function btn:SetAttribute(k, v) self.attrs[k] = v end
btn.wfNames = { ["Tarantula"] = true, ["Chatter"] = true }
btn.wfOrder = { ["Chatter"] = 1, ["Tarantula"] = 2 }
btn.wfChunks = { "/cleartarget\n/targetexact Chatter\n/targetexact [noexists][dead]Tarantula\n/cleartarget [dead]" }
btn.wfChunk = 1

local function click()
	clock = clock + 1
	UI.targetPreClick(btn, "LeftButton", true)
	UI.targetPreClick(btn, "LeftButton", false)          -- the up half of the same click must not pick again
	local m = btn.attrs.macrotext
	local unit = m and m:match("^/target (nameplate%d+)$")
	if unit then target = unit end                         -- what the secure macro would do
	return m, unit
end

plates = {
	{ unit = "nameplate1", name = "Tarantula", guid = "Creature-0-1-0-1-505-01" },
	{ unit = "nameplate2", name = "Redridge Mongrel", guid = "Creature-0-1-0-1-423-02" },   -- not listed
	{ unit = "nameplate3", name = "Tarantula", guid = "Creature-0-1-0-1-505-03" },
	{ unit = "nameplate4", name = "Chatter", guid = "Creature-0-1-0-1-14273-04" },
	{ unit = "nameplate5", name = "Tarantula", guid = "Creature-0-1-0-1-505-05", dead = true },
	{ unit = "nameplate6", name = "Tarantula", guid = "Creature-0-1-0-1-505-06", friend = true },
}
local seen = {}
local _, u1 = click(); seen[#seen + 1] = u1
check(u1 == "nameplate4", "the rare (first in the list) comes first, got " .. tostring(u1))
local _, u2 = click(); seen[#seen + 1] = u2
local _, u3 = click(); seen[#seen + 1] = u3
check(u2 == "nameplate1" and u3 == "nameplate3", "then every Tarantula in view, got " .. tostring(u2) .. ", " .. tostring(u3))
local _, u4 = click()
check(u4 == "nameplate4", "after all three a new round starts, got " .. tostring(u4))
for _, u in ipairs(seen) do check(u ~= "nameplate2" and u ~= "nameplate5" and u ~= "nameplate6", "unlisted, dead or friendly units are never picked: " .. u) end

-- a new mob walking in joins the round
plates[#plates + 1] = { unit = "nameplate7", name = "Tarantula", guid = "Creature-0-1-0-1-505-07" }
local _, u5 = click(); local _, u6 = click(); local _, u7 = click()
local got = { [u5 or ""] = true, [u6 or ""] = true, [u7 or ""] = true }
check(got.nameplate7, "a newly visible match is reached within the round")

-- PostClick puts the name macro back, so a click in combat still does something sensible
local post
for k, v in pairs(UI) do if k == "targetPostClick" then post = v end end
-- in combat: the pre-click leaves the macro alone
combat = true
btn.attrs.macrotext = btn.wfChunks[1]
clock = clock + 1
UI.targetPreClick(btn, "LeftButton", true)
check(btn.attrs.macrotext == btn.wfChunks[1], "in combat the name macro is kept")
combat = false

-- no nameplates (enemy plates hidden): the name macro is kept too
plates = {}; target = nil
btn.attrs.macrotext = btn.wfChunks[1]
clock = clock + 1
UI.targetPreClick(btn, "LeftButton", true)
check(btn.attrs.macrotext == btn.wfChunks[1], "no plates in view: name macro kept")

-- names hidden by the game (secret values): no pick, no error
issecretvalue = function(v) return v == "SECRETNAME" end
plates = { { unit = "nameplate1", name = "SECRETNAME", guid = "x" } }
clock = clock + 1
local ok = pcall(UI.targetPreClick, btn, "LeftButton", true)
check(ok and btn.attrs.macrotext == btn.wfChunks[1], "secret names: name macro kept, no error")
issecretvalue = nil

-- nearest first (16 Nov: a Tarantula stood next to him but Target any picked a Great Goretusk, which is higher on the list)
issecretvalue = nil
local yards = {}
CheckInteractDistance = function(u, idx) local y = yards[u]; if not y then return nil end; if idx == 3 then return y <= 8 end; if idx == 4 then return y <= 28 end; return nil end
C_Spell = { IsSpellInRange = function(spell, u) local y = yards[u]; if not y then return nil end; if spell == 5782 then return y <= 20 end; if spell == 686 then return y <= 30 end; return nil end }
local b2 = { attrs = {} }
function b2:SetAttribute(k, v) self.attrs[k] = v end
b2.wfNames = { ["Great Goretusk"] = true, ["Tarantula"] = true }
b2.wfOrder = { ["Great Goretusk"] = 1, ["Tarantula"] = 2 }
b2.wfChunks = { "/cleartarget\n/targetexact Great Goretusk\n/targetexact [noexists][dead]Tarantula\n/cleartarget [dead]" }
b2.wfChunk = 1
plates = {
	{ unit = "nameplate1", name = "Great Goretusk", guid = "Creature-0-1-0-1-547-01" },
	{ unit = "nameplate2", name = "Tarantula", guid = "Creature-0-1-0-1-505-02" },
	{ unit = "nameplate3", name = "Great Goretusk", guid = "Creature-0-1-0-1-547-03" },
}
yards = { nameplate1 = 25, nameplate2 = 5, nameplate3 = 29 }
target = nil
local function click2() clock = clock + 1; UI.targetPreClick(b2, "LeftButton", true); local u = (b2.attrs.macrotext or ""):match("^/target (nameplate%d+)$"); if u then target = u end; return u end
check(click2() == "nameplate2", "the Tarantula 5 yards away comes before a Goretusk 25 yards away")
check(click2() == "nameplate1", "then the next nearest (25 yards)")
check(click2() == "nameplate3", "then the farthest (29 yards)")
-- same distance band: the list order decides
yards = { nameplate1 = 15, nameplate2 = 15, nameplate3 = 40 }
b2.wfSeen = {}; target = nil
check(click2() == "nameplate1", "same band: list order (Goretusk first)")

-- enemy nameplates switched on once, out of combat, never in combat
local cv = { nameplateShowEnemies = "0" }
GetCVar = function(k) return cv[k] end
SetCVar = function(k, v) cv[k] = tostring(v); return true end
local S = WoWFeedUI.state
S.platesChecked = nil; combat = true
check(UI.ensureEnemyPlates() == false and cv.nameplateShowEnemies == "0", "not touched in combat")
combat = false
check(UI.ensureEnemyPlates() == true and cv.nameplateShowEnemies == "1", "turned on out of combat")
cv.nameplateShowEnemies = "0"
check(UI.ensureEnemyPlates() == false and cv.nameplateShowEnemies == "0", "only once per session: if he hides them again (V), the choice stands")

if fails > 0 then error(fails .. " failure(s)", 0) end
io.write("target cycle ok\n")
