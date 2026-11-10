-- Strip plumbing end to end in the stub, at the NARROWEST supported width (MAXPAY = 157, a 1728 px wide window):
--   * every event envelope carries the addon's boot id,
--   * each event is sent once more on a later tick (>= 2 s), only with spare strip capacity, never starving sections, bounded,
--   * the queue cap (200) and MAXPAY limits hold,
--   * the SUMMARY KV line keeps afk/loading/uirev/uiboot/chat/combat (and what the bridge needs) intact, long lines are split not cut.
package.path = "tests/?.lua;" .. package.path
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end
local function eq(a, b, msg) check(a == b, (msg or "") .. " expected " .. tostring(b) .. " got " .. tostring(a)) end

local function newWorld(width, height)
	local W = dofile("tests/wowstub.lua")
	W.install()
	local w = W.installCoach()
	_G.GetPhysicalScreenSize = function() return width or 1728, height or 1117 end
	W.loadAddon()
	return W, w
end

local function parse(m)
	local sec, id, part, parts, payload = m:match("^([^\31]*)\31([^\31]*)\31([^\31]*)\31([^\31]*)\31(.*)$")
	return sec, id, tonumber(part), tonumber(parts), payload
end

---------------------------------------------------------------- MAXPAY at the narrow width
do
	local W, w = newWorld()
	local API, C, UI, J = WoWFeed_API, WoWFeedCoach, WoWFeedUI, WoWFeedJSON
	local S = C.S
	eq(API.maxpay(), 157, "MAXPAY at 1728 px")

	-- decode everything the strip sends: evj batches (reassembled), sections (last text per section)
	local chunks, sent = {}, {}
	local function pump(limit)
		local evs = {}
		for _ = 1, (limit or 400) do
			local m = API.nextMessage()
			if not m then break end
			local sec, id, part, parts, payload = parse(m)
			check(#payload <= API.maxpay(), sec .. " payload " .. #payload .. " exceeds MAXPAY")
			check(#WoWAI_Codec.Encode(1, m) <= 569, "message of " .. sec .. " fits the strip's cells")
			if sec == "evj" then
				chunks[id] = chunks[id] or {}
				chunks[id][part] = payload
				local complete = true
				for i = 1, parts do if not chunks[id][i] then complete = false end end
				if complete then
					local arr = assert(J.decode(table.concat(chunks[id])))
					chunks[id] = nil
					for _, e in ipairs(arr) do evs[#evs + 1] = e end
				end
			end
			sent[#sent + 1] = sec
		end
		return evs
	end

	---------------------------------------------------------------- boot id in every envelope
	W.fire("PLAYER_LOGIN"); pump()           -- nothing pending: PLAYER_LOGIN only starts the tickers
	S.q, S.rs = {}, {}
	C.emit("level_up", { level = 7 })
	local first = S.q[1].s
	check(first:find('"boot":' .. UI.state.bootId .. ',', 1, true), "envelope carries the boot id: " .. first)
	local ev1 = pump()
	eq(#ev1, 1, "one event sent")
	eq(ev1[1].boot, UI.state.bootId, "decoded boot id equals the uiboot of the KV line")
	eq(ev1[1].seq, S.seq, "seq")

	---------------------------------------------------------------- one redundant resend, >= 2 s later, only when spare
	eq(#pump(), 0, "nothing is repeated immediately")
	W.advance(1.9); eq(#pump(), 0, "nor after 1.9 s")
	W.advance(0.7)                                   -- 2.6 s after the first send
	local again = pump()
	eq(#again, 1, "the event is repeated once after >= 2 s")
	eq(again[1].seq, ev1[1].seq, "same seq (the bridge dedupes on boot+seq)"); eq(again[1].boot, ev1[1].boot, "same boot")
	W.advance(100); eq(#pump(), 0, "never a third time")
	eq(#S.rs, 0, "nothing left waiting")

	---------------------------------------------------------------- not while a section waits (never starve sections)
	C.emit("level_up", { level = 8 })
	eq(#pump(), 1, "fresh event goes out at once")
	local lines = {}
	for i = 1, 30 do lines[i] = "line number " .. i .. " of a long section that needs several strip messages" end
	API.push("testsec", lines, 60)                   -- many parts
	W.advance(3)
	local order = {}
	for _ = 1, 60 do
		local m = API.nextMessage()
		if not m then break end
		local sec = parse(m)
		order[#order + 1] = sec
	end
	local lastSec, firstEvj
	for i, sec in ipairs(order) do
		if sec == "testsec" then lastSec = i elseif sec == "evj" and not firstEvj then firstEvj = i end
	end
	check(lastSec ~= nil, "the section was sent")
	check(firstEvj == nil or firstEvj > lastSec, "the repeat waits until the section has been sent completely (evj at " .. tostring(firstEvj) .. ", section ends at " .. tostring(lastSec) .. ")")
	check(firstEvj ~= nil, "once the section is out, the repeat still goes out")

	-- a burst of fresh events does not starve a waiting section either
	API.push("testsec2", lines, 60)
	local got, ticks = 0, 0
	repeat
		C.emit("level_up", { level = 9 })
		local m = API.nextMessage(); ticks = ticks + 1
		if m and parse(m) == "testsec2" then got = got + 1 end
	until ticks >= 40
	check(got >= 10, "sections keep flowing during an event burst, got " .. got .. " section messages in 40 ticks")
	pump(2000); W.advance(40); pump(2000)

	---------------------------------------------------------------- an unsent repeat expires (bounded), and never goes out without spare capacity
	S.q, S.rs, S.resendDropped = {}, {}, 0
	C.emit("level_up", { level = 10 })
	local p1 = C.nextEvj(API.maxpay() - 32, true)
	check(p1 ~= nil, "first send")
	eq(#S.rs, 1, "queued for a repeat")
	W.advance(5)
	check(C.nextEvj(API.maxpay() - 32, false) == nil, "no repeat without spare capacity")
	W.advance(40)
	check(C.nextEvj(API.maxpay() - 32, true) == nil, "an expired repeat is dropped, not sent late")
	eq(#S.rs, 0, "expired entry removed"); eq(S.resendDropped, 1, "and counted")

	---------------------------------------------------------------- caps
	S.q, S.rs, S.dropped = {}, {}, 0
	for i = 1, 250 do C.emit("level_up", { level = i }) end
	eq(#S.q, 200, "queue capped at 200"); eq(S.dropped, 50, "oldest 50 dropped")
	local maxRs = 0
	for _ = 1, 2000 do
		local p = C.nextEvj(API.maxpay() - 32, false)      -- sending only: no repeats yet
		if not p then break end
		maxRs = math.max(maxRs, #S.rs)
		check(#p <= API.maxpay() - 32 + 2, "batch fits")
	end
	check(maxRs <= 200, "repeat list capped at 200, was " .. maxRs)
	for i = 1, 250 do C.emit("level_up", { level = i }) end
	check(#S.q <= 200 and #S.rs <= 200, "both lists stay bounded")

	---------------------------------------------------------------- a big event is split over parts and repeated as a whole
	S.q, S.rs = {}, {}
	W.advance(60); pump(3000)
	S.q, S.rs = {}, {}
	local big = {}
	for i = 1, 40 do big[i] = { name = "Attacker with a rather long name number " .. i, level = i, classification = "elite" } end
	C.emit("death", { attackers = 40, attackers_list = J.array(big), killer = { name = "x" } })
	local maxParts = 0
	local function pumpParts()
		local evs = {}
		for _ = 1, 200 do
			local m = API.nextMessage()
			if not m then break end
			local sec, id, part, parts, payload = parse(m)
			if sec == "evj" then
				maxParts = math.max(maxParts, parts)
				chunks[id] = chunks[id] or {}; chunks[id][part] = payload
				local ok = true
				for i = 1, parts do if not chunks[id][i] then ok = false end end
				if ok then
					local arr = assert(J.decode(table.concat(chunks[id]))); chunks[id] = nil
					for _, e in ipairs(arr) do evs[#evs + 1] = e end
				end
			end
		end
		return evs
	end
	local d1 = pumpParts()
	eq(#d1, 1, "big event delivered whole"); check(maxParts >= 2, "split over several messages, parts " .. maxParts)
	W.advance(3)
	local d2 = pumpParts()
	eq(#d2, 1, "big event repeated whole"); eq(d2[1] and d2[1].seq, d1[1] and d1[1].seq, "same seq")
end

---------------------------------------------------------------- the KV line at MAXPAY = 157
do
	local W, w = newWorld()
	local API, C, UI = WoWFeed_API, WoWFeedCoach, WoWFeedUI
	-- the largest realistic values
	w.units.player.level = 60
	_G.UnitLevel = function() return 60 end
	_G.UnitXP = function() return 1234567 end
	_G.UnitXPMax = function() return 2345678 end
	_G.GetMoney = function() return 1234567890 end
	_G.UnitRace = function() return "Night Elf" end
	_G.GetRealmName = function() return "Argent Dawn Ultra Long Realm Name" end
	_G.GetPVPLifetimeStats = function() return 123456, 0 end
	UI.state.rev = 999999; UI.state.slot = 999; UI.state.door = 999
	W.fire("PLAYER_LOGIN"); W.runTimers()
	local lines, sums = {}, {}
	for _ = 1, 600 do
		local m = API.nextMessage()
		if not m then break end
		local sec, id, part, parts, payload = parse(m)
		check(#payload <= 157, sec .. " payload over MAXPAY: " .. #payload)
		if sec == "summary" then sums[#sums + 1] = payload end
	end
	local text = table.concat(sums, "\n")
	local kv = {}
	for l in text:gmatch("[^\n]+") do
		check(#l <= 157, "summary line over MAXPAY (" .. #l .. "): " .. l)
		local tag = l:match("^(KV%d*) ")
		if tag then kv[#kv + 1] = l end
	end
	check(#kv >= 1, "summary has a KV line")
	local first = kv[1] or ""
	eq(first:sub(1, 3), "KV ", "first line is the plain KV line the bridge parses")
	local tokens = {}
	for _, l in ipairs(kv) do for t in l:gmatch("%S+") do if t:find("=", 1, true) then tokens[#tokens + 1] = t end end end
	local have = {}
	for _, t in ipairs(tokens) do local k, v = t:match("^([^=]+)=(.*)$"); have[k] = v; check(v and v ~= "", "token " .. t .. " has a value") end
	-- the six the task names must be on the FIRST line, intact
	for _, k in ipairs({ "afk", "loading", "uirev", "uiboot", "chat", "combat" }) do check(first:find(" " .. k .. "=%d+"), "first KV line has " .. k .. "=<number>: " .. first) end
	eq(have.uirev, "999999"); eq(have.uiboot, tostring(UI.state.bootId))
	-- what derive() in the bridge needs, also intact and on the first line
	for _, k in ipairs({ "level", "xp", "xpmax", "money_copper", "honorable_kills" }) do check(first:find(" " .. k .. "=%d+"), "first KV line has " .. k .. ": " .. first) end
	-- nothing was lost: every other token is present in full somewhere in the KV lines
	eq(have.xp, "1234567"); eq(have.xpmax, "2345678"); eq(have.money_copper, "1234567890"); eq(have.race, "Night_Elf"); eq(have.realm, "Argent_Dawn_Ultra_Long_Realm_Name")
	check(have.hpmax and have.pwmax and have.pwtype and have.tgt and have.uptime_s and have.uislot and have.uidoor and have.uidb and have.uimeta, "optional tokens survive in KV2 lines")

	-- emit() splits long single lines instead of cutting them
	local long = {}
	for i = 1, 60 do long[i] = "word" .. i end
	local line = table.concat(long, " ")
	API.push("longline", { line, "tail line" }, 60)
	local got = {}
	for _ = 1, 50 do
		local m = API.nextMessage()
		if not m then break end
		local sec, _, _, _, payload = parse(m)
		check(#payload <= 157, "long line piece within MAXPAY")
		if sec == "longline" then got[#got + 1] = payload end
	end
	local joined = table.concat(got, "\n")
	check(joined:find("word1 ", 1, true) and joined:find("word60", 1, true) and joined:find("tail line", 1, true), "no text lost from a long line")
	local words = {}
	for t in joined:gmatch("word%d+") do words[#words + 1] = t end
	eq(#words, 60, "every word survives intact (no mid-word cut)")
	-- a line with no space at all and UTF-8 text is still cut safely
	local utf = string.rep("\195\169", 200)           -- 400 bytes of "e acute"
	API.push("utfline", { utf }, 60)
	local out = {}
	for _ = 1, 20 do
		local m = API.nextMessage()
		if not m then break end
		local sec, _, _, _, payload = parse(m)
		if sec == "utfline" then out[#out + 1] = payload end
	end
	local all = table.concat(out, "")
	all = all:gsub("\n", "")
	eq(#all, 400, "all bytes of the unbroken line arrive")
	for p in table.concat(out, "\n"):gmatch("[^\n]+") do check(#p % 2 == 0, "UTF-8 pieces are cut on character boundaries") end
end

if fails > 0 then error(fails .. " strip test(s) failed", 0) end
io.write("strip ok\n")
