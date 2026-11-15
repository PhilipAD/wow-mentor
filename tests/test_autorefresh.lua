-- The addon reloads the UI itself when the overlay slot pool is 85% used, but only at a quiet moment and not twice in 10 minutes
package.path = "tests/?.lua;" .. package.path
local W = require("wowstub")
W.install()
local reloads = 0
time = function() return W.epoch + W.clock end
ReloadUI = function() reloads = reloads + 1 end
W.combat = false
UnitAffectingCombat = function() return W.combat end
InCombatLockdown = function() return W.combat end
UnitIsDeadOrGhost = function() return W.dead == true end
UnitOnTaxi = function() return W.taxi == true end
UnitCastingInfo = function() return W.casting end
UnitChannelInfo = function() return nil end
ChatEdit_GetActiveWindow = function() return W.typing end
dofile("wowmentor/addon/WoWFeed/JSON.lua")
WoWFeed_API = { push = function() end, coords = function() return 47.9, 42.7 end }
dofile("wowmentor/addon/WoWFeed/UI.lua")
local S = WoWFeedUI.state
local doc = '{"v":1,"rev":1,"theme":{},"panels":[{"title":"x","id":"p","children":[{"type":"text","text":"hi"}]}],"waypoints":[],"toasts":[],"actions":[],"builtin":{},"rules":[],"inbox":[]}'
W.slots["WFSlot000"] = { rev = 1, json = doc }
W.meta = 1
for _, f in ipairs(W.frames) do if f.events.PLAYER_LOGIN and f.scripts.OnEvent then f.scripts.OnEvent(f, "PLAYER_LOGIN") end end
W.runTimers()
local function secs(n) for _ = 1, n do W.advance(1); W.runTickers(); W.runTimers() end end
secs(5)
assert(S.poolMax == 2000, "the pool is 2000, got " .. tostring(S.poolMax))

-- polling is slow when idle: after the 40 s hot window that follows an update, 60 s burn 2 slots (the old idle poll was 15 s: 4)
secs(60)
local before = S.slot
secs(60)
assert(S.slot - before <= 3, "idle polling burned " .. (S.slot - before) .. " slots in 60 s")

-- below 85%: nothing is wanted
assert(not S.refreshWanted, "no refresh wanted at the start")
assert(reloads == 0, "no reload at the start")

-- at 85%: wanted, but not while he fights, is dead, flies, casts or types
for n = 1699, 1705 do W.slots[string.format("WFSlot%03d", n)] = { rev = 1, json = doc } end
W.combat = true                                    -- he is fighting when the pool reaches 85%
S.slot = 1699
secs(40)
assert(reloads == 0, "never in combat")
W.combat = false; W.dead = true; secs(40)
assert(S.refreshWanted, "refresh wanted at 85%")
assert(reloads == 0, "never while dead")
W.dead = false; W.taxi = true; secs(40); assert(reloads == 0, "never on a flight")
W.taxi = false; W.casting = "Drain Soul"; secs(40); assert(reloads == 0, "never mid cast")
W.casting = nil; W.typing = {}; secs(40); assert(reloads == 0, "never while typing in chat")
W.typing = nil
-- combat ended less than 15 s ago: still waits
W.combat = true; secs(2); W.combat = false; secs(8)
assert(reloads == 0, "waits 15 s after combat")
secs(10)

-- quiet: the toast, then the reload 5 s later
secs(40)
assert(reloads == 1, "reloaded once when quiet, got " .. reloads)
assert(WoWFeedDB.ui.lastAutoReload and WoWFeedDB.ui.lastAutoReload > 0, "the time is saved so the reload cannot loop")

-- the pool is refilled by the reload (modelled: slot back to 0): and a still-wanted flag cannot reload again within 10 minutes
S.slot = 1750; S.refreshWanted = true
secs(120); assert(reloads == 1, "no second reload within 10 minutes, got " .. reloads)
assert(#S.errors == 0, table.concat(S.errors, " | "))
io.stdout:write("autorefresh ok\n")

-- a client that only knows 1000 folders: the pool is sized to what it knows at boot, so the 85% mark (850) triggers the refresh
-- long before slot 1001 would be missing; and a MISSING slot (older client info) also asks for a refresh
S.poolMax = 2000
S.slot = 1000; S.refreshWanted = false
-- the next slot is unknown to the client: LoadAddOn reports MISSING
C_AddOns.LoadAddOn = function(name) return false, "MISSING" end
W.advance(40); W.runTickers()
assert(S.poolMax == 1000, "pool shrinks to the known folders, " .. tostring(S.poolMax))
assert(S.refreshWanted, "a missing slot asks for a refresh")
io.stdout:write("autorefresh known-pool ok\n")
