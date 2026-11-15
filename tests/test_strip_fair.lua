-- Strip fairness: with many sections and one that changes on every push, QUESTS still goes out completely within a bounded
-- number of messages (the section whose last complete send is oldest goes next; QUESTS, GUIDES and PROFESSIONS win ties).
package.path = "tests/?.lua;" .. package.path
local W = dofile("tests/wowstub.lua")
W.install()
W.installCoach()
W.loadAddon()
local API = WoWFeed_API
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local function lines(prefix, n) local L = {}; for i = 1, n do L[i] = prefix .. " line " .. i .. string.rep("x", 60) end return L end
local function drain(n) for _ = 1, n do API.nextMessage() end end
drain(400)                                                             -- whatever the addon queued at load

-- thirty quiet 3-part sections and one noisy 6-part section that changes before every message
for k = 1, 30 do API.push("extra" .. k, lines("extra" .. k, 6), 1e9) end
local version = 0
local gotQuests, sent = false, 0
local done = {}
for step = 1, 400 do
	version = version + 1
	API.push("noisy", lines("noisy v" .. version, 12), 1e9)
	if step == 20 then API.push("quests", lines("quest", 10), 1e9) end
	local m = API.nextMessage()
	if m then
		sent = sent + 1
		local sec, _, part, parts = m:match("^([^\31]*)\31([^\31]*)\31([^\31]*)\31([^\31]*)\31")
		if tonumber(part) == tonumber(parts) then done[sec] = true end
		if sec == "quests" and tonumber(part) == tonumber(parts) then gotQuests = step; break end
	end
end
check(gotQuests and gotQuests - 20 <= 60, "QUESTS fully sent within 60 messages of its push, got " .. tostring(gotQuests and gotQuests - 20))

-- the noisy section does not monopolise the strip: every quiet section completes too (in either loop)
for _ = 1, 600 do
	version = version + 1
	API.push("noisy", lines("noisy v" .. version, 12), 1e9)
	local m = API.nextMessage()
	local sec, _, part, parts = (m or ""):match("^([^\31]*)\31([^\31]*)\31([^\31]*)\31([^\31]*)\31")
	if sec and tonumber(part) == tonumber(parts) then done[sec] = true end
end
local quiet = 0
for k = 1, 30 do if done["extra" .. k] then quiet = quiet + 1 end end
check(quiet == 30, "every quiet section was sent, got " .. quiet .. " of 30")
if fails > 0 then error(fails .. " failure(s)", 0) end
io.write("strip fairness ok\n")
