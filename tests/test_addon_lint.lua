-- Static checks on the addon source (display and advice only; no protected calls; guarded registrations; secret booleans).
--   1. nothing in the addon may equip, preview, use, move or buy an item, cast, target, or send chat to other players
--   2. every RegisterEvent / RegisterUnitEvent is protected with pcall (an unknown event name raises), except PLAYER_LOGIN
--   3. functions that return booleans which the game may hide (secret) are never boolean-tested directly
--   4. OnUpdate scripts are few, known and throttled
--   5. no user-visible literal in the overlay files mentions level or XP
package.path = "tests/?.lua;" .. package.path
local fails = 0
local function check(cond, msg) if not cond then fails = fails + 1; io.stderr:write("FAIL: " .. msg .. "\n") end end

local DIR = "wowmentor/addon/WoWFeed/"
local FILES = { "Codec", "JSON", "WoWFeed", "UI", "Coach", "CoachState", "CoachRules" }
local src, lines = {}, {}
for _, n in ipairs(FILES) do
	local f = assert(io.open(DIR .. n .. ".lua", "rb"))
	src[n] = f:read("*a"); f:close()
	lines[n] = {}
	for l in (src[n] .. "\n"):gmatch("(.-)\r?\n") do lines[n][#lines[n] + 1] = l end
end
-- a line without its comment (naive: a "--" inside a string literal is not a case in this addon)
local function code(l)
	local s = l:gsub("%-%-%[=*%[.*", "")
	s = s:gsub("%-%-.*$", "")
	return s
end

---------------------------------------------------------------- 0. Lua 5.1 only (WoW's runtime): newer syntax makes a whole file fail to load, and the strip stops drawing
for _, n in ipairs(FILES) do
	for i, l in ipairs(lines[n]) do
		local c = code(l):gsub('"[^"]*"', '""'):gsub("'[^']*'", "''")
		check(not c:find("[%w%)%]]%s*//%s*[%w%(]"), n .. ".lua:" .. i .. " uses // (integer division is Lua 5.3+; use math.floor(a / b))")
		check(not c:find("%f[%w]goto%f[%W]") and not c:find("::[%a_][%w_]*::"), n .. ".lua:" .. i .. " uses goto (Lua 5.2+)")
		check(not c:find("[%w%)%]]%s*[&|~]%s*[%w%(]") and not c:find("<<") and not c:find(">>"), n .. ".lua:" .. i .. " uses a bit operator (Lua 5.3+; use the bit library)")
	end
	if jit then check(loadfile(DIR .. n .. ".lua") ~= nil, n .. ".lua does not compile under LuaJIT (Lua 5.1 syntax)") end
end

---------------------------------------------------------------- 1. forbidden calls
local FORBIDDEN = {
	"EquipItemByName", "EquipCursorItem", "AutoEquipCursorItem", "EquipPendingItem", "UseContainerItem", "PickupContainerItem", "PickupInventoryItem", "PickupItem",
	"UseInventoryItem", "UseItemByName", "UseItem", "SplitContainerItem", "SellCursorItem", "BuyMerchantItem", "BuybackItem", "DressUpItemLink", "DressUpBattlePet",
	"DressUpVisual", "DressUpTransmogLink", "DressUpMountLink", "TryOn", "SendChatMessage", "SendAddonMessage", "SendAddonMessageLogged", "BNSendWhisper", "ReplyTell",
	"CastSpellByName", "CastSpellByID", "CastSpell", "TargetUnit", "TargetByName", "AssistUnit", "AcceptQuest", "CompleteQuest", "GetQuestReward", "AcceptTrade", "InviteUnit",
	"JoinChannelByName", "RunMacro", "RunMacroText", "SecureCmdOptionParse", "AutoLootMapItem", "LootSlot", "ConfirmBindOnUse", "EquipItem", "UseEquipmentSet",
	"C_Item.EquipItem", "C_Item.UseItem", "C_Container.UseContainerItem", "C_Container.PickupContainerItem", "C_Container.SplitContainerItem", "C_ChatInfo.SendChatMessage",
	"C_ChatInfo.SendAddonMessage", "C_PartyInfo.InviteUnit", "C_TransmogCollection.PreviewItem", "C_EquipmentSet.UseEquipmentSet", "DEFAULT_CHAT_FRAME.editBox",
	"ChatFrame_OpenChat", "ChatEdit_SendText", "ChatEdit_ParseText", "EditBox:Insert",
}
for _, n in ipairs(FILES) do
	for i, l in ipairs(lines[n]) do
		local c = code(l)
		for _, name in ipairs(FORBIDDEN) do
			local pat = "%f[%w_%.]" .. name:gsub("%.", "%%.") .. "%f[^%w_]"
			if c:find(pat) then check(false, n .. ".lua:" .. i .. " uses forbidden " .. name .. ": " .. l:sub(1, 100)) end
		end
	end
end
-- the same names must not be assembled from strings either (_G["Equip".."ItemByName"])
for _, n in ipairs(FILES) do
	for i, l in ipairs(lines[n]) do
		local c = code(l)
		if c:find("_G%s*%[") or c:find("getglobal") or c:find("loadstring") or c:find("setfenv%(%s*[^1]") then
			check(n == "Coach" or n == "CoachState" or n == "CoachRules" or n == "WoWFeed" or n == "UI", "dynamic global access in " .. n)
			-- allowed only for reading numbers/strings: the whitelist below names every use
			local ok = c:find('_G%.GENERAL') or c:find("_G%[name%]") or c:find('_G%[g%]') or c:find("_G%.WoWFeedInbox") or c:find('_G%[k%]')
			check(ok or not c:find("_G%s*%["), n .. ".lua:" .. i .. " dynamic global access: " .. l:sub(1, 100))
			check(not c:find("getglobal") and not c:find("loadstring"), n .. ".lua:" .. i .. " getglobal/loadstring: " .. l:sub(1, 100))
		end
	end
end
-- the one place that reads a global by name: a string for a pet action token, never a function that is then called
for i, l in ipairs(lines.CoachState) do
	if code(l):find("_G%[name%]") then check(code(l):find("isToken and _G%[name%]"), "CoachState.lua:" .. i .. ": _G[name] must only read a label") end
end

---------------------------------------------------------------- 2. event registration
for _, n in ipairs(FILES) do
	for i, l in ipairs(lines[n]) do
		local c = code(l)
		if c:find("RegisterEvent") or c:find("RegisterUnitEvent") or c:find("RegisterAllEvents") then
			local guarded = c:find("pcall%s*%(%s*[%w_%.]*Register") or c:find("pcall%s*%(%s*F%.Register") or c:find("pcall%s*%(%s*[%w_]+%.Register")
			local login = c:find('RegisterEvent%("PLAYER_LOGIN"%)')
			check(guarded or login, n .. ".lua:" .. i .. " registers an event without pcall: " .. l:sub(1, 110))
			check(not c:find("RegisterAllEvents"), n .. ".lua:" .. i .. " registers every event")
		end
	end
end

---------------------------------------------------------------- 3. secret booleans are never boolean-tested directly
-- A call such as `if UnitIsDead(u) then` raises when the game returns a secret boolean. Boolean-returning APIs must be wrapped:
-- tb(...), U.bool(...), truthy(...), or passed as an argument to try(...) / call(...) / pcall(...). A line may carry `-- secret-ok`
-- when the result goes through such a wrapper some other way.
local BOOLEAN_APIS = { "UnitIsDead", "UnitIsDeadOrGhost", "UnitIsGhost", "UnitCanAttack", "UnitIsUnit", "UnitAffectingCombat", "UnitIsPlayer", "UnitIsEnemy",
	"UnitIsFriend", "UnitIsAFK", "UnitIsDND", "UnitInVehicle", "UnitOnTaxi", "UnitIsPVP", "UnitIsPVPFreeForAll", "UnitIsTapDenied", "UnitIsConnected", "UnitIsVisible",
	"UnitIsGroupLeader", "UnitPlayerControlled", "UnitIsOtherPlayersPet", "UnitInRange", "InCombatLockdown", "IsPlayerSpell", "IsSpellKnown" }
local WRAPPERS = { "tb%(%s*$", "bool%(%s*$", "truthy%(%s*$", "tryResult%(%s*$" }
for _, n in ipairs(FILES) do
	for i, l in ipairs(lines[n]) do
		local c = code(l)
		if not l:find("secret%-ok") then
			for _, name in ipairs(BOOLEAN_APIS) do
				local from = 1
				while true do
					local s, e = c:find("%f[%w_]" .. name .. "%s*%(", from)
					if not s then break end
					local before = c:sub(1, s - 1)
					local ok = false
					for _, w in ipairs(WRAPPERS) do if before:find(w) then ok = true end end
					-- the definition of a global in a stub or `local function name(` is not a call
					if before:find("function%s*$") then ok = true end
					check(ok, n .. ".lua:" .. i .. " calls " .. name .. " without a secret-safe wrapper: " .. l:sub(1, 110))
					from = e + 1
				end
			end
		end
	end
end

---------------------------------------------------------------- 4. OnUpdate scripts: known, and throttled
local onupdate = {}
for _, n in ipairs(FILES) do
	for i, l in ipairs(lines[n]) do
		if code(l):find('SetScript%("OnUpdate"') and not code(l):find("OnUpdate\"%s*,%s*nil") then onupdate[#onupdate + 1] = { n, i } end
	end
end
check(#onupdate == 3, "expected exactly 3 OnUpdate scripts (panel driver, toast fade, waypoint HUD), found " .. #onupdate)
for _, o in ipairs(onupdate) do
	local n, i = o[1], o[2]
	local body = table.concat(lines[n], "\n", i, math.min(#lines[n], i + 8))
	check(body:find("acc%s*<%s*0%.%d+") or body:find("tacc%s*<%s*0%.%d+"), n .. ".lua:" .. i .. ": OnUpdate must return early until a time threshold has passed")
end

---------------------------------------------------------------- 5. level / XP never in a user-visible literal of the overlay files
-- (UI.lua and CoachRules.lua draw everything the player sees; the feed sections of CoachState / WoWFeed are for the coach only)
local ALLOWED = {
	['c.bind == "xp" or c.bind == "level"'] = true,     -- the progress components that are hidden on purpose
	['"Overlay inbox is 85% used'] = false,
}
for _, n in ipairs({ "UI", "CoachRules" }) do
	for i, l in ipairs(lines[n]) do
		local c = code(l)
		for lit in c:gmatch('"([^"]*)"') do
			local low = lit:lower()
			local bad = low:find("%f[%a]xp%f[%A]") or low:find("experience") or low:find("%f[%a]level%f[%A]") and not (lit == "level" or lit == "level_up" or lit == "levelup")
			if bad then
				local allowed = false
				for k, v in pairs(ALLOWED) do if v and c:find(k, 1, true) then allowed = true end end
				check(allowed or lit == "xp" or lit == "level", n .. ".lua:" .. i .. " user-visible literal mentions level/xp: \"" .. lit .. "\"")
			end
		end
	end
end

if fails > 0 then error(fails .. " lint failure(s)", 0) end
io.write("addon lint ok\n")
