-- WoWFeed: personal-use addon. Addons cannot open sockets or write files in realtime and this
-- client has no CombatLogAdd, so state is drawn as a pixel strip (Codec.lua) that a Mac bridge screen-captures.
-- Idea (addon -> external process over a side channel) inspired by chelinho139/wow-ai (MIT).
local ADDON = ...
-- Tolerate API functions this client lacks or that raise: Get*/Unit*/Is* globals that are missing become no-ops, and the ones
-- that exist are called under pcall (an error returns nothing, which every caller treats as "unknown"). Works on Lua 5.1 (WoW,
-- setfenv) and on newer Lua (the test harness, _ENV).
local unpack = unpack or table.unpack
local G = _G
local safeFns = {}
local function results(ok, ...) if ok then return ... end end
local ENV = setmetatable({}, {
	__index = function(_, k)
		local v = G[k]
		if type(k) == "string" and k ~= "GetTime" and (k:find("^Get") or k:find("^Unit") or k:find("^Is")) then
			if v == nil then return function() end end
			if type(v) == "function" then
				local c = safeFns[k]
				if c and c[1] == v then return c[2] end
				local fn = function(...) return results(pcall(v, ...)) end
				safeFns[k] = { v, fn }
				return fn
			end
		end
		return v
	end,
	__newindex = function(_, k, v) G[k] = v end,
})
if setfenv then setfenv(1, ENV) end
local _ENV = ENV
WoWFeedDB = WoWFeedDB or {}
local F = CreateFrame("Frame")
local seq, last = 0, {}
local tconcat = table.concat
local _tostring = tostring
-- tostring that never raises on a secret value (the game hides those: they print as <restricted>)
local function tostring(...)
	if select("#", ...) == 0 then return "nil" end
	local v = ...
	if issecretvalue and issecretvalue(v) then return "<restricted>" end
	return _tostring(v)
end
-- values can be "secret" (restricted) in combat: format defensively instead of erroring the whole section
local function strfmt(f, ...)
	local ok, r = pcall(string.format, f, ...)
	if ok then return r end
	local a, parts = { ... }, {}
	for i = 1, select("#", ...) do local o, v = pcall(tostring, a[i]); parts[i] = o and v or "<restricted>" end
	return "(restricted) " .. tconcat(parts, " ")
end

local function try(f, ...)
	if type(f) ~= "function" then return nil end
	local r = { pcall(f, ...) }
	if not r[1] then return nil end
	return unpack(r, 2)
end
local function num(v) if v == nil or (issecretvalue and issecretvalue(v)) then return nil end return tonumber(v) end
local function nz(v, d) return num(v) or d end
local function str(v) if type(v) ~= "string" or (issecretvalue and issecretvalue(v)) then return nil end return v end
-- truth of a value that may be a secret boolean (a boolean test of one raises): secret counts as false
local function tb(v)
	if v == nil or (issecretvalue and issecretvalue(v)) then return false end
	return v and true or false
end
local function clean(s)
	if s == nil then return "" end
	if issecretvalue and issecretvalue(s) then return "<restricted>" end
	s = tostring(s)
	if issecretvalue and issecretvalue(s) then return "<restricted>" end
	s = s:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|H(.-)|h%[?(.-)%]?|h", "%2 <%1>")
	s = s:gsub("|T.-|t", ""):gsub("|", "/"):gsub("[\r\n]+", " ")
	return s
end
local GII = (C_Item and C_Item.GetItemInfo) or GetItemInfo or function() end
local GDILI = (C_Item and C_Item.GetDetailedItemLevelInfo) or GetDetailedItemLevelInfo or function() end
local function link(l)
	if not l then return "" end
	local id = tostring(l):match("item:(%d+)") or tostring(l):match("spell:(%d+)") or tostring(l):match("quest:(%d+)")
	local name = tostring(l):match("%[(.-)%]") or tostring(l)
	return clean(name) .. (id and (" #" .. id) or "")
end
local function pct(c, m) local ok, r = pcall(function() if not m or m == 0 then return "?" end return strfmt("%d%%", c / m * 100 + 0.5) end); return ok and r or "?" end

---------------------------------------------------------------- pixel transport
-- Messages are drawn as a hairline strip of coloured cells along the very top edge of the game window
-- (Codec.lua); the Mac bridge reads it with a tiny ScreenCaptureKit crop and decodes it.
-- Pixel-perfect: one strip unit is one physical pixel; cells are CW x CH pixels.
-- Row 1: [white anchor][data cells...]   Row 2: three status bars (secret-value workaround, see updateBars).
local CW, CH = 3, 2
local BARW, BARGAP = 120, 6
local physW, physH = 1728, 1117
do
	local okp, w, h = pcall(GetPhysicalScreenSize)
	if okp and type(w) == "number" and type(h) == "number" and w > 0 and h > 0 then physW, physH = w, h end
end
local MAXCELLS = math.max(64, math.floor(physW / CW) - 6)
local MAXPAY = math.max(80, math.floor((MAXCELLS - 1) * 3 / 8) - 8 - 48)   -- 48 = section/id/part prefix added by nextMessage
local strip = CreateFrame("Frame", "WoWFeedStrip", UIParent)
strip:SetFrameStrata("TOOLTIP"); strip:SetFrameLevel(100)
strip:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
strip:EnableMouse(false)
local cells, cellState = {}, {}
local function layoutStrip()
	local es = UIParent:GetEffectiveScale()
	strip:SetScale((768 / physH) / es * (WoWFeedDB.pxScale or 1))
	strip:SetSize((MAXCELLS + 1) * CW, CH * 2)
end
local anchor = strip:CreateTexture(nil, "ARTWORK")
anchor:SetColorTexture(1, 1, 1, 1)
anchor:SetPoint("TOPLEFT", strip, "TOPLEFT", 0, 0)
anchor:SetSize(CW, CH)
-- Secret-value workaround: StatusBar:SetValue accepts secret numbers, so the game draws the fill for us and
-- the bridge measures the filled width in the capture. Bars: player health, player power, target health.
local bars = {}
do
	local specs = { { "health", 1, 1, 1 }, { "power", 1, 1, 0 }, { "target", 1, 0, 0 } }
	for i, sp in ipairs(specs) do
		local b = CreateFrame("StatusBar", nil, strip)
		b:SetSize(BARW, CH)
		b:SetPoint("TOPLEFT", strip, "TOPLEFT", (i - 1) * (BARW + BARGAP), -CH)
		b:SetStatusBarTexture("Interface\\Buttons\\WHITE8X8")
		b:SetStatusBarColor(sp[2], sp[3], sp[4])
		b:SetMinMaxValues(0, 1); b:SetValue(0)
		b:SetFrameLevel(strip:GetFrameLevel() + 2)
		local back = b:CreateTexture(nil, "BACKGROUND"); back:SetAllPoints(); back:SetColorTexture(0, 0, 0, 1)
		bars[sp[1]] = b
	end
end
local function updateBars()
	pcall(function()
		local hm = UnitHealthMax("player"); bars.health:SetMinMaxValues(0, hm); bars.health:SetValue(UnitHealth("player"))
		local pm = UnitPowerMax("player"); bars.power:SetMinMaxValues(0, pm); bars.power:SetValue(UnitPower("player"))
		if UnitExists("target") then
			local tm = UnitHealthMax("target"); bars.target:SetMinMaxValues(0, tm); bars.target:SetValue(UnitHealth("target"))
		else bars.target:SetMinMaxValues(0, 1); bars.target:SetValue(0) end
	end)
end
local function drawCells(vals)
	local n = math.min(#vals, MAXCELLS)
	local prev = cellState.n or 0
	for i = 1, math.max(n, prev) do
		if i <= n then
			local v = vals[i]
			if cellState[i] ~= v then
				cellState[i] = v
				local tex = cells[i]
				if not tex then
					tex = strip:CreateTexture(nil, "ARTWORK")
					tex:SetSize(CW, CH)
					tex:SetPoint("TOPLEFT", strip, "TOPLEFT", i * CW, 0)
					cells[i] = tex
				end
				local r, g, bl = WoWAI_Codec.CellColor(v)
				tex:SetColorTexture(r, g, bl, 1)
				tex:Show()
			end
		elseif cellState[i] ~= nil then
			cellState[i] = nil
			cells[i]:Hide()
		end
	end
	cellState.n = n
end
local pending, order, queued = {}, {}, {}
local txstat = {}      -- section -> { pushes, parts, restarts }: why a big section may be slow to arrive
local evq = {}
local msgId = 0
-- Structured events (Coach.lua) ride in their own message type "evj" (JSON array). The provider is registered through
-- WoWFeed_API.setEvjProvider and returns payload, id, part, parts (or nil when nothing is queued).
local evjProvider, lastWasEvj = nil, false
-- Cut a line into pieces of at most `limit` bytes. Prefers a space in the second half of the piece, never cuts inside a UTF-8 sequence.
local function splitLine(l, limit)
	if #l <= limit then return { l } end
	local out = {}
	while #l > limit do
		local cut = limit
		local sp = l:sub(1, limit + 1):match("^.*() ")
		if sp and sp > limit / 2 then cut = sp - 1 end
		while cut > 1 and l:byte(cut + 1) and l:byte(cut + 1) >= 0x80 and l:byte(cut + 1) < 0xC0 do cut = cut - 1 end
		out[#out + 1] = l:sub(1, cut)
		l = l:sub(cut + 1):gsub("^ ", "")
	end
	if #l > 0 then out[#out + 1] = l end
	return out
end
local function emit(section, lines)
	-- split into <= MAXPAY byte parts; latest content for a section replaces the older one. A single line longer than
	-- MAXPAY is split into several lines (never silently cut).
	local parts, cur = {}, {}
	local size = 0
	for _, l0 in ipairs(lines) do
		for _, l in ipairs(splitLine(l0, MAXPAY)) do
			if size + #l + 1 > MAXPAY and #cur > 0 then parts[#parts+1] = tconcat(cur, "\n"); cur, size = {}, 0 end
			cur[#cur+1] = l; size = size + #l + 1
		end
	end
	parts[#parts+1] = tconcat(cur, "\n")
	local st = txstat[section] or { pushes = 0, parts = 0, held = 0 }
	txstat[section] = st
	st.pushes, st.parts = st.pushes + 1, #parts
	local cur0 = pending[section]
	if cur0 and cur0.i > 0 and cur0.i < #cur0.parts then
		-- a multi-part section is half sent: finish it, then send the newest version (restarting it on every change could starve it forever)
		cur0.nextParts = parts; st.held = st.held + 1
		return
	end
	seq = seq + 1
	pending[section] = { parts = parts, i = 0, sid = seq }
	if not queued[section] then queued[section] = true; order[#order+1] = section end
end
local FASTSEC = "fast"
local turn = 0
local stickySec
local function nextMessage()
	turn = turn + 1
	if #evq > 0 then
		local out, size = {}, 0
		while #evq > 0 and size + #evq[1] < MAXPAY - 40 do local e = table.remove(evq, 1); out[#out+1] = e; size = size + #e + 1 end
		if #out == 0 then out[1] = table.remove(evq, 1):sub(1, MAXPAY - 40) end
		lastWasEvj = false
		return strfmt("ev\31%d\0311\0311\31%s", seq, tconcat(out, "\n"))
	end
	-- structured events: at most one evj message per tick, and never two in a row while a section is waiting
	-- (so a burst of events cannot starve the state sections)
	if evjProvider then
		local waiting = false
		for _, p in pairs(pending) do if p.i < #p.parts then waiting = true; break end end
		if not (lastWasEvj and waiting) then
			-- spare: no section part is waiting, so a redundant repeat of an already sent event costs the strip nothing
			local ok, payload, id, part, parts = pcall(evjProvider, MAXPAY - 32, not waiting)
			if ok and type(payload) == "string" and payload ~= "" then
				lastWasEvj = true
				return strfmt("evj\31%d\31%d\31%d\31%s", id or seq, part or 1, parts or 1, payload)
			end
		end
	end
	lastWasEvj = false
	for _, p in pairs(pending) do
		if p.nextParts and p.i >= #p.parts then
			seq = seq + 1
			p.parts, p.nextParts, p.i, p.sid = p.nextParts, nil, 0, seq
		end
	end
	local sec
	if turn % 2 == 0 and pending[FASTSEC] and pending[FASTSEC].i < #pending[FASTSEC].parts then sec = FASTSEC
	else
		local st = pending[stickySec or ""]
		if st and st.i < #st.parts then sec = stickySec      -- finish a multi-part section back to back: the bridge drops one whose parts are far apart
		else
			for _ = 1, #order do
				local s = table.remove(order, 1); order[#order+1] = s
				local p = pending[s]
				if p and p.i < #p.parts then sec = s; break end
			end
		end
		if not sec and pending[FASTSEC] and pending[FASTSEC].i < #pending[FASTSEC].parts then sec = FASTSEC end
	end
	if not sec then return nil end
	local p = pending[sec]; p.i = p.i + 1
	stickySec = (sec ~= FASTSEC and p.i < #p.parts) and sec or nil
	return strfmt("%s\31%d\31%d\31%d\31%s", sec, p.sid, p.i, #p.parts, p.parts[p.i])
end
local lastSend, heartbeat = 0, 0
local function sendTick()
	local m = nextMessage()
	if not m then
		heartbeat = heartbeat + 1
		m = strfmt("hb\31%d\0311\0311\31%s", seq, date("%H:%M:%S"))
	end
	msgId = (msgId % 65535) + 1
	local cellsV = WoWAI_Codec.Encode(msgId, m)
	drawCells(cellsV)
	updateBars()
	if #cellsV > MAXCELLS - 1 and not WoWFeed_warned then WoWFeed_warned = true; print("WoWFeed: message too large (" .. #cellsV .. " cells, max " .. (MAXCELLS - 1) .. ")") end
end
WoWFeed_SendTick = sendTick

-- send only when content changed, or when forced / older than maxAge
local function push(section, lines, maxAge)
	local clean2 = {}
	for i = 1, #lines do
		local v = lines[i]
		if (issecretvalue and issecretvalue(v)) or type(v) ~= "string" then v = "(restricted value hidden by the game)" end
		clean2[i] = v
	end
	lines = clean2
	local blob = tconcat(lines, "\n")
	local now = GetTime()
	local l = last[section]
	if l and l.blob == blob and (now - l.t) < (maxAge or 60) then return end
	last[section] = { blob = blob, t = now }
	emit(section, lines)
end

---------------------------------------------------------------- collectors
local function unitLine(u)
	if not UnitExists(u) then return nil end
	local hp, hpm = num(UnitHealth(u)), num(UnitHealthMax(u))
	local pm = num(UnitPowerMax(u)); local p = num(UnitPower(u))
	local cls = select(2, UnitClass(u))
	local flags = {}
	if tb(UnitIsDead(u)) then flags[#flags+1] = "dead" end
	if tb(UnitIsGhost(u)) then flags[#flags+1] = "ghost" end
	if tb(UnitAffectingCombat(u)) then flags[#flags+1] = "combat" end
	if tb(UnitIsPlayer(u)) then flags[#flags+1] = "player" end
	if tb(UnitIsEnemy("player", u)) then flags[#flags+1] = "enemy" elseif tb(UnitIsFriend("player", u)) then flags[#flags+1] = "friend" end
	local cast = try(UnitCastingInfo, u); local chan = try(UnitChannelInfo, u)
	local hptxt = (hp and hpm) and strfmt("HP %d/%d (%s)", hp, hpm, pct(hp, hpm)) or ("HP max " .. tostring(hpm) .. " (current secret; target bar in VITALS)")
	local pwtxt = (p and pm) and strfmt("power %d/%d", p, pm) or ("power max " .. tostring(pm))
	return strfmt("%s | L%s %s %s %s | %s | %s | %s%s%s",
		clean(UnitName(u)), tostring(num(UnitLevel(u))), clean(UnitRace(u) or UnitCreatureType(u) or ""), clean(UnitClass(u) or ""), clean(UnitClassification(u) or ""),
		hptxt, pwtxt, tconcat(flags, ","),
		cast and (" | casting " .. clean(cast)) or "", chan and (" | channeling " .. clean(chan)) or "")
end

-- Map coordinates are not exposed by C_Map in this client, but Questie/TomTom/minimap draw them as text: read that text.
local coordRegion
local function coordText()
	local function ok(r)
		if not r then return nil end
		local o, t = pcall(r.GetText, r)
		if not o or type(t) ~= "string" or (issecretvalue and issecretvalue(t)) then return nil end
		local a, b = t:match("^%s*(%d+%.?%d*)%s*[,/]%s*(%d+%.?%d*)%s*$")
		if a then return tonumber(a), tonumber(b) end
	end
	local x, y = ok(coordRegion)
	if x then return x, y end
	coordRegion = nil
	if not EnumerateFrames then return end
	local f = EnumerateFrames()
	local n = 0
	while f and n < 20000 do
		n = n + 1
		local okr, regs = pcall(function() return { f:GetRegions() } end)
		if okr then
			for _, r in ipairs(regs) do
				if r.GetObjectType and r:GetObjectType() == "FontString" then
					local cx, cy = ok(r)
					if cx and r:IsVisible() then coordRegion = r; return cx, cy end
				end
			end
		end
		f = EnumerateFrames(f)
	end
end
-- Player position: returns mapID, x, y (fractions 0..1, nil when unknown) and the map info table.
-- Tries C_Map, then the world position converted to the map, then text another addon draws (coordText).
local function playerMapPos()
	local mid = C_Map and try(C_Map.GetBestMapForUnit, "player")
	local x, y
	if mid and (issecretvalue and issecretvalue(mid)) then mid = nil end
	if mid then
		local pos = try(C_Map.GetPlayerMapPosition, mid, "player")
		if pos then
			local okx, px, py = pcall(pos.GetXY, pos)
			if okx then x, y = num(px), num(py) end      -- coordinates can be secret: then they are unknown
		end
	end
	local mi = mid and try(C_Map.GetMapInfo, mid)
	if not x and mid and C_Map.GetMapPosFromWorldPos and CreateVector2D then
		local ux0, uy0, _, inst0 = try(UnitPosition, "player")
		ux0, uy0 = num(ux0), num(uy0)
		if ux0 and uy0 then for _, pr in ipairs({ { ux0, uy0 }, { uy0, ux0 } }) do local v = try(C_Map.GetMapPosFromWorldPos, inst0, CreateVector2D(pr[1], pr[2]), mid); local vx, vy = v and num(v.x), v and num(v.y); if vx and vy and vx > 0 and vx < 1 then x, y = vx, vy; break end end end
	end
	if not x and mid and C_Map.GetWorldPosFromMapPos and CreateVector2D then
		local ux0, uy0, _, inst0 = try(UnitPosition, "player")
		ux0, uy0 = num(ux0), num(uy0)
		local _, p0 = try(C_Map.GetWorldPosFromMapPos, mid, CreateVector2D(0, 0))
		local _, p1 = try(C_Map.GetWorldPosFromMapPos, mid, CreateVector2D(1, 1))
		local p0x, p0y, p1x, p1y = p0 and num(p0.x), p0 and num(p0.y), p1 and num(p1.x), p1 and num(p1.y)
		if ux0 and uy0 and p0x and p0y and p1x and p1y and p1x ~= p0x and p1y ~= p0y then
			for _, pr in ipairs({ { ux0, uy0 }, { uy0, ux0 } }) do
				local fx = (pr[1] - p0x) / (p1x - p0x); local fy = (pr[2] - p0y) / (p1y - p0y)
				if fx > 0 and fx < 1 and fy > 0 and fy < 1 then x, y = fx, fy; break end
			end
		end
	end
	if not x then
		local cxt, cyt = coordText()
		if cxt then x, y = cxt / 100, cyt / 100 end
	end
	return mid, x, y, mi
end
local function sPlayerFast()
	local L = {}
	local mid, x, y, mi = playerMapPos()
	local ux, uy, uz, uinst = try(UnitPosition, "player")
	L[#L+1] = strfmt("Position: map=%s (%s) x=%s y=%s", tostring(mid), mi and mi.name or "?", x and strfmt("%.2f", x * 100) or "unavailable", y and strfmt("%.2f", y * 100) or "unavailable")
	L[#L+1] = strfmt("World position (yards): x=%s y=%s instanceID=%s facing=%.1f deg", tostring(num(ux)), tostring(num(uy)), tostring(num(uinst)), math.deg(nz(GetPlayerFacing(), 0)))
	L[#L+1] = strfmt("Zone: %s | Subzone: %s | Minimap zone: %s | PvP: %s", clean(GetRealZoneText()), clean(GetSubZoneText()), clean(GetMinimapZoneText()), clean((GetZonePVPInfo())))
	local hp, hpm = num(UnitHealth("player")), num(UnitHealthMax("player"))
	local pw, pwm = num(UnitPower("player")), num(UnitPowerMax("player"))
	L[#L+1] = "Health: " .. (hp and hpm and strfmt("%d/%d (%s)", hp, hpm, pct(hp, hpm)) or ("max " .. tostring(hpm) .. ", current is secret in this client: see VITALS (measured from a status bar)")) .. " | Power(" .. tostring(num(UnitPowerType("player"))) .. "): " .. (pw and pwm and strfmt("%d/%d", pw, pwm) or ("max " .. tostring(pwm) .. ", current secret: see VITALS"))
	local flags = {}
	local function f(c, n) if tb(c) then flags[#flags+1] = n end end
	f(tb(UnitAffectingCombat("player")), "in-combat"); f(tb(UnitIsDeadOrGhost("player")), "dead"); f(IsResting(), "resting")
	f(IsMounted(), "mounted"); f(IsSwimming(), "swimming"); f(IsFlying(), "flying"); f(IsIndoors(), "indoors"); f(IsStealthed(), "stealthed")
	f(tb(UnitIsAFK("player")), "AFK"); f(tb(UnitIsDND("player")), "DND"); f(tb(UnitOnTaxi("player")), "on-taxi"); f(IsFalling(), "falling")
	f(tb(UnitInVehicle("player")), "in-vehicle"); f(IsInInstance(), "in-instance")
	L[#L+1] = "State: " .. (#flags > 0 and tconcat(flags, ", ") or "idle")
	L[#L+1] = strfmt("Speed (yd/s): %.1f", nz(select(1, GetUnitSpeed("player")), 0))
	local mny = nz(GetMoney(), 0); L[#L+1] = "Money: " .. strfmt("%dg %ds %dc", math.floor(mny / 10000), math.floor(mny / 100) % 100, mny % 100)
	L[#L+1] = strfmt("XP: %s/%s (rested %s)", tostring(num(UnitXP("player"))), tostring(num(UnitXPMax("player"))), tostring(nz(GetXPExhaustion(), 0)))
	for _, u in ipairs({ "target", "targettarget", "focus", "pet", "mouseover" }) do
		local ul = unitLine(u); if ul then L[#L+1] = u .. ": " .. ul end
	end
	local combo = num(try(GetComboPoints, "player", "target")); if combo and combo > 0 then L[#L+1] = "Combo points: " .. combo end
	return L
end

local function aurasOf(unit, filter)
	local out = {}
	for i = 1, 60 do
		local name, icon, count, dtype, dur, exp, src, _, _, spellId
		if C_UnitAuras and C_UnitAuras.GetAuraDataByIndex then
			local a = try(C_UnitAuras.GetAuraDataByIndex, unit, i, filter)
			if not a then break end
			local okf = pcall(function() name, count, dtype, dur, exp, spellId, src = a.name, a.applications, a.dispelName, a.duration, a.expirationTime, a.spellId, a.sourceUnit end)
			if not okf then name, count, dtype, dur, exp, spellId, src = nil end
		elseif UnitAura then
			name, icon, count, dtype, dur, exp, src, _, _, spellId = UnitAura(unit, i, filter)
			if not name then break end
		else
			break
		end
		-- remaining seconds, total duration and who cast it; values the game hides are written as unknown
		local e, d = num(exp), num(dur)
		local rem = ""
		if (issecretvalue and issecretvalue(exp)) or (exp ~= nil and not e) then rem = " unknown s left"
		elseif e and e > 0 then
			rem = strfmt(" %.0fs left", e - GetTime())
			if d and d > 0 then rem = rem .. strfmt(" of %.0fs", d) end
		elseif e == 0 then rem = " no expiry" end
		local from = ""
		if src then local sv = (issecretvalue and issecretvalue(src)) and "unknown" or tostring(src); from = " by " .. clean(sv) end
		local cnt = num(count)
		out[#out+1] = strfmt("%s #%s%s%s%s%s", clean(name), tostring(num(spellId) or "?"), (cnt and cnt > 1) and (" x" .. cnt) or "", dtype and (" [" .. clean(dtype) .. "]") or "", rem, from)
	end
	return out
end
local function sAuras()
	local L = { "BUFFS:" }
	for _, a in ipairs(aurasOf("player", "HELPFUL")) do L[#L+1] = "  " .. a end
	L[#L+1] = "DEBUFFS:"
	for _, a in ipairs(aurasOf("player", "HARMFUL")) do L[#L+1] = "  " .. a end
	if UnitExists("target") then
		L[#L+1] = "TARGET BUFFS:"; for _, a in ipairs(aurasOf("target", "HELPFUL")) do L[#L+1] = "  " .. a end
		L[#L+1] = "TARGET DEBUFFS:"; for _, a in ipairs(aurasOf("target", "HARMFUL")) do L[#L+1] = "  " .. a end
	end
	return L
end

local function sGroup()
	local L = {}
	local inRaid, n = tb(IsInRaid()), nz(GetNumGroupMembers(), 0)
	L[#L+1] = strfmt("Group: %s, members=%d, leader=%s", inRaid and "raid" or (n > 0 and "party" or "solo"), n, tostring(tb(UnitIsGroupLeader("player"))))
	if n > 0 then
		for i = 1, (inRaid and 40 or 4) do
			local u = (inRaid and "raid" or "party") .. i
			local ul = unitLine(u); if ul then L[#L+1] = u .. ": " .. ul end
		end
	end
	if UnitExists("pet") then L[#L+1] = "pet: " .. (unitLine("pet") or "") end
	local g, grank = GetGuildInfo("player"); L[#L+1] = "Guild: " .. clean(g or "none") .. " / rank " .. clean(grank or "")
	return L
end

local function sNearby()
	local L = {}
	local plates = C_NamePlate and try(C_NamePlate.GetNamePlates)
		if type(plates) ~= "table" then plates = {} end
	L[#L+1] = "Visible nameplates: " .. #plates
	for i, p in ipairs(plates) do
		if i > 40 then break end
		local u = p.namePlateUnitToken or (p.UnitFrame and p.UnitFrame.unit)
		if u then local ul = unitLine(u); if ul then L[#L+1] = "  " .. ul end end
	end
	return L
end

local SLOTS = { "Head","Neck","Shoulder","Shirt","Chest","Waist","Legs","Feet","Wrist","Hands","Finger1","Finger2","Trinket1","Trinket2","Back","MainHand","SecondaryHand","Ranged","Tabard" }
local function sStats()
	local L = {}
	L[#L+1] = strfmt("Name: %s-%s | Level %d | %s %s | Faction %s | Sex %s", clean(UnitName("player")), clean(GetRealmName()), UnitLevel("player"), clean(UnitRace("player")), clean(UnitClass("player")), clean(UnitFactionGroup("player")), tostring(UnitSex("player")))
	local names = { "Strength", "Agility", "Stamina", "Intellect", "Spirit" }
	for i = 1, 5 do local s, e, p, n = UnitStat("player", i); L[#L+1] = strfmt("%s: %s (base %s)", names[i], tostring(e), tostring(s)) end
	local b, e = UnitArmor("player"); L[#L+1] = "Armor: " .. tostring(e)
	L[#L+1] = "Attack power: " .. tostring(select(1, UnitAttackPower("player")) or "?") .. " (+" .. tostring(select(2, UnitAttackPower("player")) or 0) .. ")"
	L[#L+1] = "Ranged attack power: " .. tostring(select(1, UnitRangedAttackPower("player")) or "?")
	L[#L+1] = strfmt("Crit: melee %.2f%% ranged %.2f%% spell %.2f%%", try(GetCritChance) or 0, try(GetRangedCritChance) or 0, try(GetSpellCritChance, 2) or 0)
	L[#L+1] = strfmt("Dodge %.2f%% Parry %.2f%% Block %.2f%% Haste(melee) %.2f%%", try(GetDodgeChance) or 0, try(GetParryChance) or 0, try(GetBlockChance) or 0, try(GetMeleeHaste) or 0)
	L[#L+1] = strfmt("Spell power: %s | Mana regen (cast/oos): %s/%s", tostring(try(GetSpellBonusDamage, 2)), tostring(select(2, GetManaRegen())), tostring(select(1, GetManaRegen())))
	for s = 1, 6 do local r = try(UnitResistance, "player", s); if r then L[#L+1] = "Resistance school " .. s .. ": " .. tostring(select(2, UnitResistance("player", s))) end end
	L[#L+1] = strfmt("Defense: %s | Bonus: %s", tostring(select(1, UnitDefense("player"))), tostring(select(2, UnitDefense("player"))))
	return L
end

local function sEquipment()
	local L = {}
	local totalIlvl, cnt, missing = 0, 0, {}
	for slot = 1, 19 do
		local l = GetInventoryItemLink("player", slot)
		if l then
			local name, _, q, ilvl, minl, itype, isub, _, equipLoc = GII(l)
			ilvl = try(GDILI, l) or ilvl
			local cur, max = num((GetInventoryItemDurability(slot))), num(select(2, GetInventoryItemDurability(slot)))
			cnt = cnt + 1; totalIlvl = totalIlvl + (num(ilvl) or 0)
			L[#L+1] = strfmt("[%02d] %s: %s | quality %s | ilvl %s | req level %s | %s %s | durability %s", slot, SLOTS[slot] or "?", link(l), tostring(q), tostring(ilvl), tostring(minl), tostring(itype), tostring(isub), cur and (cur .. "/" .. max) or "n/a")
		else
			L[#L+1] = strfmt("[%02d] %s: (empty)", slot, SLOTS[slot] or "?")
			if slot ~= 4 and slot ~= 19 then missing[#missing+1] = SLOTS[slot] end
		end
	end
	L[#L+1] = strfmt("Equipped items: %d | avg ilvl %.1f | EMPTY SLOTS: %s", cnt, cnt > 0 and totalIlvl / cnt or 0, #missing > 0 and tconcat(missing, ", ") or "none")
	return L
end

local function bagCount(b)
	if C_Container and C_Container.GetContainerNumSlots then return num(try(C_Container.GetContainerNumSlots, b)) end
	return num(GetContainerNumSlots(b))
end
local function bagItem(b, s)
	if C_Container and C_Container.GetContainerItemInfo then
		local i = try(C_Container.GetContainerItemInfo, b, s); if type(i) ~= "table" then return nil end
		return i.hyperlink, i.stackCount, i.quality
	end
	local _, c, _, q, _, _, l = GetContainerItemInfo(b, s); return l, c, q
end
local function sBags()
	local L = {}
	local used, total = 0, 0
	for b = 0, 5 do
		local n = bagCount(b) or 0
		if n > 0 then
			total = total + n
			for s = 1, n do
				local l, c, q = bagItem(b, s)
				if l then used = used + 1; L[#L+1] = strfmt("bag %d slot %d: %s x%s (q%s)", b, s, link(l), tostring(c), tostring(q)) end
			end
		end
	end
	table.insert(L, 1, strfmt("Bag slots used %d/%d", used, total))
	return L
end

local function sTalents()
	local L = {}
	local nt = try(GetNumTalentTabs)
	if nt then
		for t = 1, nt do
			local name, _, spent = GetTalentTabInfo(t)
			L[#L+1] = strfmt("Tree %d: %s (%s points)", t, clean(name), tostring(spent))
			for i = 1, (GetNumTalents(t) or 0) do
				local n, _, tier, col, rank, maxRank = GetTalentInfo(t, i)
				if rank and rank > 0 then L[#L+1] = strfmt("  %s %d/%d (tier %s col %s)", clean(n), rank, maxRank, tostring(tier), tostring(col)) end
			end
		end
	end
	L[#L+1] = "Unspent talent points: " .. tostring(try(UnitCharacterPoints, "player") or try(GetUnspentTalentPoints) or try(GetNumUnspentTalents) or (C_SpecializationInfo and try(C_SpecializationInfo.GetNumUnspentTalents)) or "unknown")
	local spec = try(GetSpecialization); if spec then L[#L+1] = "Specialization index: " .. tostring(spec) end
	return L
end

-- Spellbook enumeration shared by sSkills and the coach (action bar rank checks, spell_known): returns
-- { { name = tab, items = { { name, id, sub, rank, passive } } } }. Not-yet-learned entries are skipped.
local function spellbook()
	local tabs = {}
	if C_SpellBook and C_SpellBook.GetNumSpellBookSkillLines and Enum and Enum.SpellBookSpellBank then
		local FUTURE = Enum.SpellBookItemType and Enum.SpellBookItemType.FutureSpell
		for tab = 1, nz(try(C_SpellBook.GetNumSpellBookSkillLines), 0) do
			local info = try(C_SpellBook.GetSpellBookSkillLineInfo, tab)
			local off, cnt = type(info) == "table" and num(info.itemIndexOffset), type(info) == "table" and num(info.numSpellBookItems)
			if off and cnt then
				local t = { name = info.name, items = {} }
				for i = off + 1, off + cnt do
					local it = try(C_SpellBook.GetSpellBookItemInfo, i, Enum.SpellBookSpellBank.Player)
					if type(it) == "table" and str(it.name) and not (FUTURE and num(it.itemType) == FUTURE) and not tb(it.isOffSpec) then
						local sub = str(it.subName)
						t.items[#t.items+1] = { name = it.name, id = num(it.spellID), sub = sub, rank = tonumber(tostring(sub or ""):match("(%d+)")), passive = tb(it.isPassive) }
					end
				end
				tabs[#tabs+1] = t
			end
		end
	elseif GetNumSpellTabs then
		for tab = 1, nz(GetNumSpellTabs(), 0) do
				local name, _, off, count = GetSpellTabInfo(tab)
				local t = { name = name, items = {} }
				off, count = nz(off, 0), nz(count, 0)
				for i = off + 1, off + count do
				local sn, sub = GetSpellBookItemName(i, "spell")
				if sn then
					local _, sid = try(GetSpellBookItemInfo, i, "spell")
					t.items[#t.items+1] = { name = sn, id = type(sid) == "number" and sid or nil, sub = sub, rank = tonumber(tostring(sub or ""):match("(%d+)")) }
				end
			end
			tabs[#tabs+1] = t
		end
	end
	return tabs
end

local function sSkills()
	local L = {}
	for i = 1, nz(try(GetNumSkillLines), 0) do
		local name, hdr, _, rank, _, _, maxRank = GetSkillLineInfo(i)
		if tb(hdr) then L[#L+1] = "== " .. clean(name) else L[#L+1] = strfmt("  %s %s/%s", clean(name), tostring(rank), tostring(maxRank)) end
	end
	for _, tab in ipairs(spellbook()) do
		L[#L+1] = "Spellbook tab: " .. clean(tab.name)
		for _, it in ipairs(tab.items) do
			L[#L+1] = "  " .. clean(it.name) .. (it.id and (" #" .. tostring(it.id)) or "") .. (it.sub and it.sub ~= "" and (" (" .. clean(it.sub) .. ")") or "")
		end
	end
	return L
end

local function sQuests()
	local L = {}
	local cnt = 0
	if C_QuestLog and C_QuestLog.GetNumQuestLogEntries and C_QuestLog.GetInfo then
		for i = 1, nz(try(C_QuestLog.GetNumQuestLogEntries), 0) do
			local q = try(C_QuestLog.GetInfo, i)
			if type(q) == "table" then
				if tb(q.isHeader) then L[#L+1] = "== " .. clean(q.title)
				else
					cnt = cnt + 1
					local done = C_QuestLog.IsComplete and tb(try(C_QuestLog.IsComplete, q.questID))
					L[#L+1] = strfmt("[%s] %s (L%s) #%s", done and "COMPLETE" or "in progress", clean(q.title), tostring(q.level), tostring(q.questID))
					local objs = try(C_QuestLog.GetQuestObjectives, q.questID)
					for _, o in ipairs(type(objs) == "table" and objs or {}) do
						L[#L+1] = strfmt("    - %s%s", clean(o.text), tb(o.finished) and " [done]" or "")
					end
				end
			end
		end
	elseif GetNumQuestLogEntries then
		for i = 1, nz(GetNumQuestLogEntries(), 0) do
			local title, level, _, hdr, _, complete, _, qid = GetQuestLogTitle(i)
			if tb(hdr) then L[#L+1] = "== " .. clean(title) else cnt = cnt + 1; L[#L+1] = strfmt("[%s] %s (L%s) #%s", num(complete) == 1 and "COMPLETE" or "in progress", clean(title), tostring(level), tostring(qid)) end
		end
	end
	table.insert(L, 1, "Quests in log: " .. cnt)
	return L
end

local function sRep()
	local L = {}
	for i = 1, nz(try(GetNumFactions), 0) do
		local name, desc, standing, bmin, bmax, val, _, _, hdr, _, hasRep = GetFactionInfo(i)
		if str(name) then
			bmin, bmax, val = num(bmin), num(bmax), num(val)
			if tb(hdr) and not tb(hasRep) then L[#L+1] = "== " .. clean(name)
			else L[#L+1] = strfmt("  %s: standing %s, %s/%s", clean(name), tostring(num(standing)), tostring(val and bmin and (val - bmin)), tostring(bmax and bmin and (bmax - bmin))) end
		end
	end
	return L
end

local function sCurrency()
	local L = {}
	if C_CurrencyInfo and C_CurrencyInfo.GetCurrencyListSize then
		for i = 1, nz(try(C_CurrencyInfo.GetCurrencyListSize), 0) do
			local ci = try(C_CurrencyInfo.GetCurrencyListInfo, i)
			if type(ci) == "table" then
				if tb(ci.isHeader) then L[#L+1] = "== " .. clean(ci.name) else L[#L+1] = strfmt("  %s: %s", clean(ci.name), tostring(ci.quantity)) end
			end
		end
	end
	return L
end

local function sSystem()
	local L = {}
	local v, b, d, toc = GetBuildInfo()
	L[#L+1] = strfmt("Client: %s build %s (%s) interface %s | Locale %s", v, b, d, tostring(toc), GetLocale())
	local _, _, home, world = GetNetStats()
	L[#L+1] = strfmt("FPS %.0f | latency home %s ms world %s ms", GetFramerate(), tostring(home), tostring(world))
	L[#L+1] = "Game time: " .. strfmt("%02d:%02d", GetGameTime()) .. " | Local time: " .. date("%Y-%m-%d %H:%M:%S")
	local name, itype, diff, dname, maxp = GetInstanceInfo()
	L[#L+1] = strfmt("Instance: %s (%s) difficulty=%s max=%s", clean(name), tostring(itype), clean(dname), tostring(maxp))
	L[#L+1] = "Bind location: " .. clean(GetBindLocation())
	L[#L+1] = "Loaded addons: "
	local a = {}
	local nAddons = nz(try((C_AddOns and C_AddOns.GetNumAddOns) or GetNumAddOns), 0)
	for i = 1, math.min(nAddons, 300) do
		local nm = str(try((C_AddOns and C_AddOns.GetAddOnInfo) or GetAddOnInfo, i))
		if nm and tb(try((C_AddOns and C_AddOns.IsAddOnLoaded) or IsAddOnLoaded, i)) then a[#a+1] = nm end
	end
	L[#L+1] = "  " .. tconcat(a, ", ")
	L[#L+1] = "Logging combat: " .. tostring(LoggingCombat and LoggingCombat())
	return L
end

local function sCooldowns()
	local L = {}
	local now = GetTime()
	local n = 0
	-- cooldown numbers are secret while the game restricts cooldowns: such a spell is simply skipped
	local getCd = (C_Spell and C_Spell.GetSpellCooldown) or nil
	if getCd and C_SpellBook and C_SpellBook.GetNumSpellBookSkillLines and Enum and Enum.SpellBookSpellBank then
		for tab = 1, nz(try(C_SpellBook.GetNumSpellBookSkillLines), 0) do
			local info = try(C_SpellBook.GetSpellBookSkillLineInfo, tab)
			local off, cnt = info and num(info.itemIndexOffset), info and num(info.numSpellBookItems)
			if off and cnt then
				for i = off + 1, off + cnt do
					local it = try(C_SpellBook.GetSpellBookItemInfo, i, Enum.SpellBookSpellBank.Player)
					local sid = type(it) == "table" and num(it.spellID) or nil
					if sid then
						local cd = try(getCd, sid)
						local dur, st = type(cd) == "table" and num(cd.duration), type(cd) == "table" and num(cd.startTime)
						if dur and st and dur > 1.5 and st > 0 then
							L[#L+1] = strfmt("%s: %.0fs left", clean(it.name), st + dur - now); n = n + 1
						end
					end
				end
			end
		end
	end
	if n == 0 then L[#L+1] = "(none on cooldown)" end
	return L
end


---------------------------------------------------------------- mentor-oriented collectors

local sessionStart = GetTime()
local function sSummary()
	-- machine-readable key=value line used by the bridge to derive rates; plus a human line
	local lvl = nz(UnitLevel("player"), 0)
	local xp, xpm = nz(UnitXP("player"), 0), nz(UnitXPMax("player"), 1)
	local money = nz(GetMoney(), 0)
	local hk = nz(select(1, try(GetPVPLifetimeStats) or 0), 0)
	local _, cls = UnitClass("player")
	local L = {}
	local afk, loading = 0, 0
	if WoWFeedCoach and WoWFeedCoach.flags then local okf, a, l = pcall(WoWFeedCoach.flags); if okf then afk, loading = a, l end end
	-- The bridge reads ONE line starting "KV " as space separated k=v tokens, and a line longer than MAXPAY would not fit one strip
	-- message. Tokens are packed in priority order: what the bridge must always see (afk/loading/uirev/uiboot/chat/combat, then the
	-- fields derive() needs), then the rest. Whatever does not fit goes to following "KV2 ..." lines (the bridge ignores them, the
	-- coach still reads them), so nothing is cut mid-token at any strip width.
	local function tok(v) return (clean(v):gsub("%s+", "_")) end
	local ui = { crit = {}, extra = {} }
	if WoWFeedUI and WoWFeedUI.kvTokens then local okk, c, e = pcall(WoWFeedUI.kvTokens); if okk and type(c) == "table" then ui.crit, ui.extra = c, e or {} end end
	local toks = { strfmt("afk=%d", afk), strfmt("loading=%d", loading) }
	for _, t in ipairs(ui.crit) do toks[#toks+1] = t end
	toks[#toks+1] = strfmt("level=%d", lvl); toks[#toks+1] = strfmt("xp=%d", xp); toks[#toks+1] = strfmt("xpmax=%d", xpm)
	toks[#toks+1] = strfmt("money_copper=%d", money); toks[#toks+1] = strfmt("honorable_kills=%d", hk)
	toks[#toks+1] = "tgt=" .. (tb(UnitExists("target")) and 1 or 0)
	toks[#toks+1] = "hpmax=" .. tostring(num(UnitHealthMax("player"))); toks[#toks+1] = "pwmax=" .. tostring(num(UnitPowerMax("player")))
	toks[#toks+1] = "pwtype=" .. tostring(num(UnitPowerType("player")))
	for _, t in ipairs(ui.extra) do toks[#toks+1] = t end
	toks[#toks+1] = strfmt("rested=%d", nz(GetXPExhaustion(), 0)); toks[#toks+1] = strfmt("uptime_s=%d", math.floor(GetTime() - sessionStart))
	toks[#toks+1] = "class=" .. tok(cls); toks[#toks+1] = "race=" .. tok(UnitRace("player")); toks[#toks+1] = "realm=" .. tok(GetRealmName())
	local line, prefix, nlines = "KV", "KV", 1
	for _, t in ipairs(toks) do
		if #t > MAXPAY - 6 then t = t:sub(1, MAXPAY - 6) end
		if #line + 1 + #t > MAXPAY and line ~= prefix then
			L[#L+1] = line
			nlines = nlines + 1
			prefix = "KV" .. nlines; line = prefix
		end
		line = line .. " " .. t
	end
	L[#L+1] = line
	L[#L+1] = strfmt("Level %d %s %s | XP %d/%d (%.1f%% of level, %d to go) | Gold %dg %ds %dc",
		lvl, clean(UnitRace("player")), clean(UnitClass("player")), xp, xpm, xp / math.max(xpm, 1) * 100, xpm - xp,
		math.floor(money / 10000), math.floor(money / 100) % 100, money % 100)
	-- level/xp stay in this KV line for the coach; they are never drawn on the overlay
	return L
end

local function sPvP()
	local L = {}
	local hk, hr = try(GetPVPLifetimeStats)
	L[#L+1] = "Lifetime honorable kills: " .. tostring(hk) .. " | highest rank id: " .. tostring(hr)
	local rank = try(UnitPVPRank, "player")
	if rank then L[#L+1] = "Current PvP rank id: " .. tostring(rank) .. " (" .. tostring((try(GetPVPRankInfo, rank, "player"))) .. ")" end
	local prog = try(GetPVPRankProgress); if prog then L[#L+1] = strfmt("Rank progress: %.1f%%", prog * 100) end
	local a, b, c, d = try(GetPVPThisWeekStats); if a then L[#L+1] = strfmt("This week: HK %s, contribution %s", tostring(a), tostring(b)) end
	a, b, c, d = try(GetPVPLastWeekStats); if a then L[#L+1] = strfmt("Last week: HK %s, contribution %s, standing %s, rating %s", tostring(a), tostring(b), tostring(c), tostring(d)) end
	local honor = try(UnitHonor, "player"); if honor then L[#L+1] = strfmt("Honor %s/%s level %s", tostring(honor), tostring(try(UnitHonorMax, "player")), tostring(try(UnitHonorLevel, "player"))) end
	local hc = try(GetHonorCurrency); if hc then L[#L+1] = "Honor currency: " .. tostring(hc) end
	L[#L+1] = "PvP flagged: " .. tostring(try(UnitIsPVP, "player")) .. " | FFA: " .. tostring(try(UnitIsPVPFreeForAll, "player")) .. " | War mode: " .. tostring(try(C_PvP and C_PvP.IsWarModeActive))
	for i = 1, (try(GetMaxBattlefieldID) or 3) do
		local status, map, inst = try(GetBattlefieldStatus, i)
		if status and status ~= "none" then L[#L+1] = strfmt("Battleground slot %d: %s %s", i, tostring(status), clean(map)) end
	end
	if C_CurrencyInfo and C_CurrencyInfo.GetCurrencyListSize then
		for i = 1, C_CurrencyInfo.GetCurrencyListSize() do
			local ci = C_CurrencyInfo.GetCurrencyListInfo(i)
			if ci and not ci.isHeader and ci.name and (ci.name:find("Honor") or ci.name:find("Mark") or ci.name:find("Arena") or ci.name:find("Conquest")) then L[#L+1] = strfmt("Currency %s: %s", clean(ci.name), tostring(ci.quantity)) end
		end
	end
	return L
end

local PROFS = {
	{ 201, "One-Handed Swords" }, { 202, "Two-Handed Swords" }, { 196, "One-Handed Axes" }, { 197, "Two-Handed Axes" }, { 198, "One-Handed Maces" },
	{ 199, "Two-Handed Maces" }, { 227, "Staves" }, { 1180, "Daggers" }, { 200, "Polearms" }, { 264, "Bows" }, { 266, "Guns" }, { 5011, "Crossbows" },
	{ 2567, "Thrown" }, { 15590, "Fist Weapons" }, { 5009, "Wands" }, { 9077, "Leather" }, { 8737, "Mail" }, { 750, "Plate" }, { 9116, "Shield" },
	{ 674, "Dual Wield" }, { 3127, "Parry" },
}
local function sProfessions()
	local L = {}
	local n = (C_SkillInfo and try(C_SkillInfo.GetNumSkillLines)) or try(GetNumSkillLines) or 0
	L[#L+1] = "Skill lines reported by game: " .. tostring(n)
	for i = 1, n do
		local info = C_SkillInfo and try(C_SkillInfo.GetSkillLineInfo, i)
		if type(info) == "table" then
			if info.isHeader then L[#L+1] = "== " .. clean(info.name)
			else L[#L+1] = strfmt("  %s: %s/%s%s%s", clean(info.name), tostring(info.rank), tostring(info.maxRank), (info.modifier and info.modifier ~= 0) and (" (+" .. tostring(info.modifier) .. ")") or "", (info.maxRank == 1) and " [proficiency]" or "") end
		elseif GetSkillLineInfo then
			local name, hdr, _, rank, _, mod, maxRank = GetSkillLineInfo(i)
			L[#L+1] = hdr and ("== " .. clean(name)) or strfmt("  %s: %s/%s", clean(name), tostring(rank), tostring(maxRank))
		end
	end
	if GetProfessions then
		local p1, p2, arch, fish, cook, fa = GetProfessions()
		L[#L+1] = strfmt("Profession slots: primary1=%s primary2=%s fishing=%s cooking=%s firstaid=%s", tostring(p1), tostring(p2), tostring(fish), tostring(cook), tostring(fa))
		for _, idx in ipairs({ p1, p2, fish, cook, fa }) do
			if idx then
				local name, _, rank, maxRank, _, _, skillLine = GetProfessionInfo(idx)
				L[#L+1] = strfmt("  PROFESSION %s: %s/%s (skillLine %s)", clean(name), tostring(rank), tostring(maxRank), tostring(skillLine))
			end
		end
	end
	local known, unknown = {}, {}
	for _, p in ipairs(PROFS) do
		local ok = (IsPlayerSpell and tb(try(IsPlayerSpell, p[1]))) or (C_SpellBook and C_SpellBook.IsSpellKnown and tb(try(C_SpellBook.IsSpellKnown, p[1])))
		if ok then known[#known+1] = p[2] else unknown[#unknown+1] = p[2] end
	end
	L[#L+1] = "Weapon/armor proficiencies KNOWN: " .. (#known > 0 and tconcat(known, ", ") or "none")
	L[#L+1] = "Not yet learned (see a weapon master/trainer): " .. tconcat(unknown, ", ")
	return L
end

local recipes = {}
local function captureTradeSkill()
	local L = {}
	local line = try(GetTradeSkillLine) or "?"
	L[#L+1] = "Open trade skill: " .. clean(line)
	for i = 1, (try(GetNumTradeSkills) or 0) do
		local name, kind, avail = GetTradeSkillInfo(i)
		if name and kind ~= "header" then
			local reag = {}
			for r = 1, (try(GetTradeSkillNumReagents, i) or 0) do
				local rn, _, rc = GetTradeSkillReagentInfo(i, r); reag[#reag+1] = clean(rn) .. " x" .. tostring(rc)
			end
			L[#L+1] = strfmt("  [%s] %s (can make %s) needs: %s", tostring(kind), clean(name), tostring(avail), tconcat(reag, ", "))
		elseif name then L[#L+1] = "== " .. clean(name) end
	end
	for i = 1, (try(GetNumCrafts) or 0) do
		local name, sub, kind, avail = GetCraftInfo(i)
		if name then L[#L+1] = strfmt("  [craft %s] %s %s", tostring(kind), clean(name), clean(sub or "")) end
	end
	recipes[clean(line)] = L
end
local function sRecipes()
	local L = {}
	local any = false
	for k, v in pairs(recipes) do any = true; for _, l in ipairs(v) do L[#L+1] = l end; L[#L+1] = "" end
	if not any then L[1] = "(open each profession window once, recipes are captured when the window is shown)" end
	return L
end

local function sItemsValue()
	local L = {}
	local total, junk, byq = 0, 0, {}
	for b = 0, 5 do
		for s = 1, (bagCount(b) or 0) do
			local l, c, q = bagItem(b, s)
			if l then
				local price = select(11, GII(l))
				price = num(price) or 0
				total = total + price * (num(c) or 1)
				if num(q) == 0 then junk = junk + price * (num(c) or 1) end
				byq[tostring(q)] = (byq[tostring(q)] or 0) + 1
			end
		end
	end
	L[#L+1] = strfmt("Vendor value of bags: %dg %ds %dc | grey junk value: %dg %ds %dc", math.floor(total / 10000), math.floor(total / 100) % 100, total % 100, math.floor(junk / 10000), math.floor(junk / 100) % 100, junk % 100)
	local parts = {}
	for q, n in pairs(byq) do parts[#parts+1] = "quality " .. q .. ": " .. n end
	L[#L+1] = "Items by quality (0 grey,1 white,2 green,3 blue,4 epic): " .. tconcat(parts, ", ")
	return L
end


---------------------------------------------------------------- diagnostics, talents (C_Traits), social, misc
local function sSecrets()
	local L = {}
	if C_RestrictedActions and Enum and Enum.AddOnRestrictionType then
		for name, id in pairs(Enum.AddOnRestrictionType) do
			L[#L+1] = "Restriction " .. name .. ": state " .. tostring(try(C_RestrictedActions.GetAddOnRestrictionState, id))
		end
	end
	if C_Secrets then
		local names = {}
		for k, v in pairs(C_Secrets) do if type(v) == "function" then names[#names+1] = k end end
		table.sort(names)
		for _, k in ipairs(names) do
			local r1 = try(C_Secrets[k], "player")
			local r0 = r1
			if r0 == nil then r0 = try(C_Secrets[k]) end
			L[#L+1] = "C_Secrets." .. k .. " = " .. tostring(r0)
		end
	end
	local function probe(label, v) L[#L+1] = strfmt("probe %s: type=%s secret=%s", label, type(v), tostring(issecretvalue and issecretvalue(v))) end
	probe("UnitHealth", UnitHealth("player")); probe("UnitHealthMax", UnitHealthMax("player"))
	probe("UnitPower", UnitPower("player")); probe("UnitPowerMax", UnitPowerMax("player"))
	probe("GetMoney", GetMoney()); probe("UnitXP", UnitXP("player")); probe("UnitLevel", UnitLevel("player"))
	probe("UnitName", UnitName("player")); probe("UnitPosition.x", (UnitPosition("player")))
	local mid = C_Map and try(C_Map.GetBestMapForUnit, "player"); probe("mapID", mid)
		local pos = mid and try(C_Map.GetPlayerMapPosition, mid, "player"); L[#L+1] = "GetPlayerMapPosition -> " .. tostring(pos)
		if mid and C_Map and CreateVector2D then
		local ux, uy, uz, ui = try(UnitPosition, "player")
		local c0, p0 = try(C_Map.GetWorldPosFromMapPos, mid, CreateVector2D(0, 0))
		local c1, p1 = try(C_Map.GetWorldPosFromMapPos, mid, CreateVector2D(1, 1))
		L[#L+1] = strfmt("posdiag unit=(%s,%s) inst=%s | corner0 cont=%s (%s,%s) | corner1 cont=%s (%s,%s)", tostring(ux), tostring(uy), tostring(ui), tostring(c0), tostring(p0 and p0.x), tostring(p0 and p0.y), tostring(c1), tostring(p1 and p1.x), tostring(p1 and p1.y))
	end
	if CurveConstants and UnitHealthPercent then probe("UnitHealthPercent", try(UnitHealthPercent, "player", false, CurveConstants.ScaleTo100)) end
	L[#L+1] = strfmt("strip geometry: physical %dx%d | maxCells %d | maxPayload %d | scale %.4f | px per unit %.3f", physW, physH, MAXCELLS, MAXPAY, strip:GetScale(), strip:GetEffectiveScale() * physH / 768)
	local lp = {}
	lp[#lp+1] = "C_Log=" .. tostring(C_Log ~= nil) .. " LogMessage=" .. tostring(C_Log and C_Log.LogMessage ~= nil)
	for _, cv in ipairs({ "developerLog", "developerLogWriteToFile" }) do lp[#lp+1] = cv .. "=" .. tostring(C_CVar and try(C_CVar.GetCVar, cv)) end
	if C_Log and C_Log.LogMessage then lp[#lp+1] = "logcall=" .. tostring(select(2, pcall(C_Log.LogMessage, "WOWFEED_PROBE " .. date("%H:%M:%S")))) end
	L[#L+1] = "log probe: " .. tconcat(lp, " | ")
	return L
end

local function sTalents2()
	local L = {}
	if not (C_Traits and C_ClassTalents) then return { "C_Traits unavailable" } end
	local cfg = try(C_ClassTalents.GetActiveConfigID)
	L[#L+1] = "Active talent config id: " .. tostring(cfg)
	if not cfg then
		local names = {}
		for k, v in pairs(C_Traits) do if type(v) == "function" then names[#names+1] = k end end
		table.sort(names); L[#L+1] = "C_Traits functions: " .. tconcat(names, ", ")
		return L
	end
	local info = try(C_Traits.GetConfigInfo, cfg)
	local trees = info and info.treeIDs or {}
	L[#L+1] = "Trees: " .. #trees
	for _, tid in ipairs(trees) do
		L[#L+1] = "== Tree " .. tostring(tid)
		local cur = try(C_Traits.GetTreeCurrencyInfo, cfg, tid, false)
		if type(cur) == "table" then for _, c in ipairs(cur) do L[#L+1] = strfmt("  currency: quantity=%s spent=%s max=%s", tostring(c.quantity), tostring(c.spent), tostring(c.maxQuantity)) end end
		for _, nid in ipairs(try(C_Traits.GetTreeNodes, tid) or {}) do
			local ni = try(C_Traits.GetNodeInfo, cfg, nid)
			if ni and (ni.currentRank or 0) > 0 then
				local nm, sp = "?", nil
				local eid = ni.activeEntry and ni.activeEntry.entryID
				local ei = eid and try(C_Traits.GetEntryInfo, cfg, eid)
				local di = ei and ei.definitionID and try(C_Traits.GetDefinitionInfo, ei.definitionID)
				if di and di.spellID then sp = di.spellID; nm = (C_Spell and C_Spell.GetSpellName and C_Spell.GetSpellName(sp)) or tostring(sp) end
				L[#L+1] = strfmt("  %s #%s rank %s/%s", clean(nm), tostring(sp), tostring(ni.currentRank), tostring(ni.maxRanks))
			end
		end
	end
	L[#L+1] = "HasUnspentTalentPoints: " .. tostring(select(1, try(C_ClassTalents.HasUnspentTalentPoints) or false))
	return L
end

local function sSocial()
	local L = {}
	local nf = (C_FriendList and try(C_FriendList.GetNumFriends)) or 0
	L[#L+1] = "Friends: " .. tostring(nf)
	for i = 1, math.min(nf, 40) do
		local f = try(C_FriendList.GetFriendInfoByIndex, i)
		if f then L[#L+1] = strfmt("  %s L%s %s %s%s", clean(f.name), tostring(f.level), clean(f.className), clean(f.area), f.connected and " [online]" or "") end
	end
	local inGuild = try(IsInGuild)
	L[#L+1] = "In guild: " .. tostring(inGuild)
	if inGuild then
		local n, online = try(GetNumGuildMembers); L[#L+1] = strfmt("Guild members: %s (%s online) | MOTD: %s", tostring(n), tostring(online), clean(try(GetGuildRosterMOTD)))
	end
	for i = 1, (try(GetNumSavedInstances) or 0) do
		local nm, _, reset, diff, locked, _, _, isRaid, maxp = GetSavedInstanceInfo(i)
		L[#L+1] = strfmt("Saved instance: %s reset in %ss locked=%s", clean(nm), tostring(reset), tostring(locked))
	end
	return L
end

local function sMisc()
	local L = {}
	local hs = C_Container and C_Container.GetItemCooldown and try(C_Container.GetItemCooldown, 6948)
	local st, du = try(C_Container.GetItemCooldown, 6948)
	if st and du then L[#L+1] = strfmt("Hearthstone cooldown: %s", (du > 0) and strfmt("%.0fs left", st + du - GetTime()) or "ready") end
	local riding = { [33388] = "Apprentice Riding", [33391] = "Journeyman Riding", [34090] = "Expert Riding", [34091] = "Artisan Riding" }
	for id, nm in pairs(riding) do
		if (IsPlayerSpell and tb(try(IsPlayerSpell, id))) or (C_SpellBook and C_SpellBook.IsSpellKnown and tb(try(C_SpellBook.IsSpellKnown, id))) then L[#L+1] = "Riding skill known: " .. nm end
	end
	if C_MountJournal and C_MountJournal.GetNumMounts then L[#L+1] = "Mounts known: " .. tostring(try(C_MountJournal.GetNumMounts)) end
	L[#L+1] = "Mail unread: " .. tostring(try(HasNewMail)) .. " | Inbox: " .. tostring(try(GetInboxNumItems))
	L[#L+1] = "Experience rate preset: " .. tostring(C_GameRules and try(C_GameRules.GetForeverExperiencePreset))
	L[#L+1] = "Time played requested via /played (see events)"
	return L
end

---------------------------------------------------------------- event log
local evbuf = {}
local function ev(kind, text)
	if #evq > 200 then table.remove(evq, 1) end
	evq[#evq+1] = strfmt("%s | %s | %s", date("%H:%M:%S"), kind, clean(text))
end
local EVENTS = {
	"PLAYER_ENTERING_WORLD", "PLAYER_LEVEL_UP", "PLAYER_DEAD", "PLAYER_ALIVE", "PLAYER_UNGHOST", "PLAYER_REGEN_DISABLED", "PLAYER_REGEN_ENABLED",
	"ZONE_CHANGED", "ZONE_CHANGED_NEW_AREA", "ZONE_CHANGED_INDOORS", "PLAYER_TARGET_CHANGED", "QUEST_ACCEPTED", "QUEST_TURNED_IN", "QUEST_REMOVED", "QUEST_LOG_UPDATE",
	"CHAT_MSG_SAY", "CHAT_MSG_YELL", "CHAT_MSG_WHISPER", "CHAT_MSG_WHISPER_INFORM", "CHAT_MSG_PARTY", "CHAT_MSG_PARTY_LEADER", "CHAT_MSG_RAID", "CHAT_MSG_GUILD", "CHAT_MSG_OFFICER", "CHAT_MSG_CHANNEL", "CHAT_MSG_EMOTE", "CHAT_MSG_SYSTEM",
	"CHAT_MSG_LOOT", "CHAT_MSG_MONEY", "CHAT_MSG_SKILL", "CHAT_MSG_COMBAT_XP_GAIN", "CHAT_MSG_COMBAT_FACTION_CHANGE", "CHAT_MSG_COMBAT_HONOR_GAIN",
	"UNIT_SPELLCAST_SUCCEEDED", "PLAYER_MONEY", "BAG_UPDATE_DELAYED", "PLAYER_EQUIPMENT_CHANGED", "UNIT_AURA", "MERCHANT_SHOW", "TAXIMAP_OPENED", "GROUP_ROSTER_UPDATE",
	"READY_CHECK", "DUEL_REQUESTED", "PARTY_INVITE_REQUEST", "PLAYER_LOGOUT", "SKILL_LINES_CHANGED", "CHARACTER_POINTS_CHANGED", "UPDATE_FACTION", "TRADE_SKILL_SHOW", "TRADE_SKILL_UPDATE", "CRAFT_SHOW", "CRAFT_UPDATE", "HONOR_XP_UPDATE", "PLAYER_PVP_KILLS_CHANGED", "UPDATE_BATTLEFIELD_STATUS",
}
local dirty = { slow = true }
local noisy = { QUEST_LOG_UPDATE = true, UNIT_AURA = true, BAG_UPDATE_DELAYED = true, PLAYER_TARGET_CHANGED = true, UPDATE_FACTION = true, UNIT_SPELLCAST_SUCCEEDED = true }
for _, e in ipairs(EVENTS) do pcall(F.RegisterEvent, F, e) end
local function onEventBody(e, a1, a2, a3, a4)
	if e == "UNIT_SPELLCAST_SUCCEEDED" then
		if not (issecretvalue and issecretvalue(a1)) and a1 == "player" then
			local nm = (C_Spell and C_Spell.GetSpellName) and try(C_Spell.GetSpellName, a3) or nil
			ev("cast", clean(nm or a3) .. " #" .. clean(a3))
		end
	elseif e == "UNIT_AURA" then
		-- covered by the aura section; just mark dirty
	elseif e:find("^CHAT_MSG") then
		ev((e:gsub("CHAT_MSG_", "chat:")), strfmt("%s: %s", clean(a2), clean(a1)))
	elseif e == "PLAYER_ENTERING_WORLD" then
		dirty.slow = true; ev("world", "entered world")
	elseif e == "PLAYER_LOGOUT" then
		ev("logout", "player logout")
	elseif not noisy[e] then
		ev(e, clean(a1))
	end
	if e == "TRADE_SKILL_SHOW" or e == "TRADE_SKILL_UPDATE" or e == "CRAFT_SHOW" or e == "CRAFT_UPDATE" then C_Timer.After(0.3, function() pcall(captureTradeSkill); last.recipes = nil end) end
	if e ~= "PLAYER_TARGET_CHANGED" and e ~= "UNIT_SPELLCAST_SUCCEEDED" then dirty.slow = true end
end
F:SetScript("OnEvent", function(_, e, a1, a2, a3, a4)
	local ok, err = pcall(onEventBody, e, a1, a2, a3, a4)      -- a handler that raises must not spam the player's error frame
	if not ok then WoWFeed_lastError = tostring(err):sub(1, 140) end
end)

---------------------------------------------------------------- tick loops
local function safe(fn) local ok, r = pcall(fn); if ok then return r end return { "ERROR: " .. tostring(r) } end
local tickN = 0
local function tickBody()
	tickN = tickN + 1
	push("summary", safe(sSummary), 5); push("fast", safe(sPlayerFast), 5)
	if tickN % 3 == 0 then push("auras", safe(sAuras), 20); push("group", safe(sGroup), 30); push("nearby", safe(sNearby), 30) end
	if tickN % 15 == 0 or dirty.slow then
		dirty.slow = false
		push("stats", safe(sStats), 120); push("equipment", safe(sEquipment), 300); push("bags", safe(sBags), 300)
		push("quests", safe(sQuests), 300); push("pvp", safe(sPvP), 120); push("professions", safe(sProfessions), 120); push("secrets_diagnostic", safe(sSecrets), 60); push("talents_traits", safe(sTalents2), 300); push("social", safe(sSocial), 300); push("misc", safe(sMisc), 120); push("bag_value", safe(sItemsValue), 120); push("recipes", safe(sRecipes), 600); push("cooldowns", safe(sCooldowns), 30); push("system", safe(sSystem), 60)
	end
	if tickN % 60 == 1 then
		push("talents", safe(sTalents), 600); push("skills", safe(sSkills), 600); push("reputation", safe(sRep), 600); push("currency", safe(sCurrency), 600)
	end
end
WoWFeed_API = {
	push = push, try = try, clean = clean, num = num, coords = coordText,
	position = playerMapPos,          -- mapID, x, y (fractions 0..1 or nil), mapInfo
	spellbook = spellbook,            -- { { name, items = { { name, id, sub, rank, passive } } } }
	maxpay = function() return MAXPAY end,
	txstat = txstat,
	nextMessage = nextMessage,        -- exposed for tests and for the coach
	setEvjProvider = function(fn) evjProvider = fn end,
}
local function tick()
	local ok, err = xpcall(tickBody, function(e) return tostring(e) .. " @ " .. (debugstack and debugstack(2, 3, 0) or "") end)
	if not ok then emit("wowfeed_error", { tostring(err) }) end
	C_Timer.After(1, tick)
end
local boot = CreateFrame("Frame")
boot:RegisterEvent("PLAYER_LOGIN")
boot:SetScript("OnEvent", function()
	C_Timer.After(3, function()
			layoutStrip(); C_Timer.NewTicker(0.5, sendTick); print("|cff00ff00WoWFeed|r active (hairline strip, top edge)")
		tick()
	end)
end)
SLASH_WOWFEED1 = "/wowfeed"
SlashCmdList.WOWFEED = function()
	dirty.slow = true; tickN = 0
	layoutStrip(); print("WoWFeed: seq=" .. seq)
end
