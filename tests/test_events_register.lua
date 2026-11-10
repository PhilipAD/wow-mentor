-- A client that rejects event names (RegisterEvent raises on an unknown one) must not stop the addon, and the polled fallbacks
-- must keep durability, spell learning and combat numbers working.
package.path = "tests/?.lua;" .. package.path
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end
local function eq(a, b, msg) check(a == b, (msg or "") .. " expected " .. tostring(b) .. " got " .. tostring(a)) end

local W = dofile("tests/wowstub.lua")
W.install()
local w = W.installCoach()
local unknown = { LEARNED_SPELL_IN_TAB = true, LEARNED_SPELL_IN_SKILL_LINE = true, UPDATE_INVENTORY_DURABILITY = true, CRAFT_SHOW = true, CRAFT_UPDATE = true,
	CHARACTER_POINTS_CHANGED = true, UNIT_COMBAT = true, PLAYER_PVP_KILLS_CHANGED = true, HONOR_XP_UPDATE = true, UNIT_PET = true }
local rejected = 0
local baseCreate = _G.CreateFrame
_G.CreateFrame = function(...)
	local f = baseCreate(...)
	local function reg(self, e) if unknown[e] then rejected = rejected + 1; error("Frame:RegisterEvent(): Unknown event '" .. tostring(e) .. "'", 2) end self.events[e] = true end
	f.RegisterEvent = reg
	f.RegisterUnitEvent = function(self, e) return reg(self, e) end
	return f
end
W.loadAddon()
local C, API = WoWFeedCoach, WoWFeed_API
check(rejected > 0, "the stub rejected some registrations (" .. rejected .. ")")
eq(C.registered.LEARNED_SPELL_IN_TAB, nil, "a rejected event is not marked registered")
eq(C.registered.PLAYER_DEAD, true, "an accepted event is marked registered")
W.fire("PLAYER_LOGIN"); W.runTimers()
W.fire("PLAYER_ENTERING_WORLD", true, false)

-- durability falls back to the 5 s poll
w.dura[1] = { 10, 100 }
for _ = 1, 8 do W.advance(1); C.tick() end
local seen = {}
for _ = 1, 600 do
	local m = API.nextMessage()
	if not m then break end
	local payload = m:match("^evj\31[^\31]*\31[^\31]*\31[^\31]*\31(.*)$")
	if payload then for t in payload:gmatch('"type":"([%w_]+)"') do seen[t] = true end end
end
check(seen.durability_critical, "durability_critical still arrives through the poll")

-- spell_learned falls back to a spellbook diff
w.book = { { name = "General", items = { { id = 100, name = "Shadow Bolt", sub = "Rank 1" } } } }
W.fire("SPELLS_CHANGED")
for _ = 1, 3 do W.advance(2); C.tick() end            -- first scan: records what is known
w.book[1].items[2] = { id = 101, name = "Corruption", sub = "Rank 1" }
W.fire("SPELLS_CHANGED")
for _ = 1, 3 do W.advance(2); C.tick() end
local learned
for _ = 1, 600 do
	local m = API.nextMessage()
	if not m then break end
	local payload = m:match("^evj\31[^\31]*\31[^\31]*\31[^\31]*\31(.*)$")
	if payload and payload:find('"type":"spell_learned"', 1, true) and payload:find("Corruption", 1, true) then learned = true end
end
check(learned, "spell_learned is derived from the spellbook when the client has no learned-spell event")
eq(#C.S.errors, 0, "no addon errors: " .. table.concat(C.S.errors, " | "))

if fails > 0 then error(fails .. " registration test(s) failed", 0) end
io.write("registration ok\n")
