-- Mentor overlay: renders a validated document (see server/uidoc.py) that the bridge delivers.
--   inbound : bridge rewrites pre-created load-on-demand slot addons (WFSlot###) and raises a silent-sound
--             "doorbell" (sig/###.wav); we load the next slot and read WoWFeedInbox. No input is injected.
--   outbound: UI.kv() and the "ui_state" section ride the existing pixel strip back to the agent.
-- Everything is non-secure, click-through by default, and dims in combat. Lua 5.1 only (WoW runtime).
local floor, max, min, abs = math.floor, math.max, math.min, math.abs
local strfmt, tconcat = string.format, table.concat
local pcall, type, pairs, ipairs, tostring, tonumber, select = pcall, type, pairs, ipairs, tostring, tonumber, select

-- SavedVariables are applied AFTER this file runs and replace the global table, so DB is (re)bound at PLAYER_LOGIN.
local DB = { user = {}, locked = false, alpha = 1, scale = 1 }
local function bindDB()
	WoWFeedDB = WoWFeedDB or {}
	WoWFeedDB.ui = WoWFeedDB.ui or {}
	DB = WoWFeedDB.ui
	DB.user = DB.user or {}
	if DB.lockedVer ~= 2 then DB.locked = false; DB.lockedVer = 2 end
	DB.alpha = DB.alpha or 1
	DB.scale = DB.scale or 1
end

local API = WoWFeed_API or {}
local JSON = WoWFeedJSON
local UI = {}
WoWFeedUI = UI

local PANEL_CAP = 60   -- panel frames ever created (they are never freed)
local POOL = 1000   -- folders exist up to this; the client only knows the ones present at launch (S.poolMax shrinks to match)
local SIG_PATH = "Interface\\AddOns\\WoWFeed\\sig\\%s.wav"
local SLOT_FMT = "WFSlot%03d"

local S = {
	rev = 0, theme = {}, panels = {}, order = {}, toastId = 0, actionId = 0, wps = {}, errors = {}, tickSeq = 0, ticks = {},
	slot = 0, door = 0, doorMode = "?", hidden = false, boot = true, docTime = 0, selftest = "", lastLoad = "", docs = 0,
	bootId = (time and time() or 0), builtin = {}, autoY = 0, dirtyState = true, backoff = 5, nextPoll = 0, poolMax = 1000, meta = nil, metaMode = "unknown", metaChanges = 0, lastApply = -999, lastMetaLoad = -999,
	wpDismissed = {}, wpOrder = {}, wpNote = {}, override = {}, rules = {}, inbox = {}, docNow = 0,
}
UI.state = S

---------------------------------------------------------------- helpers
local function now() return GetTime and GetTime() or 0 end
local function lerr(msg) S.errors[#S.errors + 1] = tostring(msg):sub(1, 160); if #S.errors > 5 then table.remove(S.errors, 1) end end

-- call an API that may be missing or raise: its first result, or nil
local function call(f, ...)
	if type(f) ~= "function" then return nil end
	local ok, a = pcall(f, ...)
	if ok then return a end
end
-- numbers / strings the game may hide: nil when secret or of another type
local function plainNum(v) if type(v) ~= "number" or (issecretvalue and issecretvalue(v)) then return nil end return v end
local function plainStr(v) if type(v) ~= "string" or (issecretvalue and issecretvalue(v)) then return nil end return v end
-- a value that may be a secret boolean (boolean-testing one raises): secret counts as false
local function truthy(v)
	if v == nil or (issecretvalue and issecretvalue(v)) then return false end
	return v and true or false
end

local DEFAULT_THEME = {
	font = "default", size = 12, accent = "#f5c518", bg = "#0b0d12", bgAlpha = 0.55, border = "#ffffff", borderAlpha = 0.10,
	text = "#e9ecf2", muted = "#9aa3b2", good = "#5bd37a", warn = "#ffb020", bad = "#ff5c5c", padding = 8, gap = 4,
	shadow = true, outline = false, cornerAccent = true,
}
local FONT_PATHS = { friz = "Fonts\\FRIZQT__.TTF", arial = "Fonts\\ARIALN.TTF", morpheus = "Fonts\\MORPHEUS.TTF", skurri = "Fonts\\SKURRI.TTF" }

local function hex(c)
	if type(c) ~= "string" then return 1, 1, 1, 1 end
	local h = c:gsub("^#", "")
	local r, g, b = tonumber(h:sub(1, 2), 16), tonumber(h:sub(3, 4), 16), tonumber(h:sub(5, 6), 16)
	local a = tonumber(h:sub(7, 8), 16)
	return (r or 255) / 255, (g or 255) / 255, (b or 255) / 255, a and a / 255 or 1
end
local function hexcode(c)
	local r, g, b = hex(c)
	return strfmt("%02x%02x%02x", floor(r * 255 + 0.5), floor(g * 255 + 0.5), floor(b * 255 + 0.5))
end

local function resolveTheme(pd)
	local t = {}
	for k, v in pairs(DEFAULT_THEME) do t[k] = v end
	for k, v in pairs(S.theme or {}) do t[k] = v end
	for _, k in ipairs({ "font", "bg", "bgAlpha", "border", "borderAlpha", "accent", "padding", "gap" }) do
		if pd[k] ~= nil then t[k] = pd[k] end
	end
	if pd.fontSize then t.size = pd.fontSize end
	return t
end

---------------------------------------------------------------- live variables and text markup
local V, Vt = {}, -10
local function refreshVars()
	local t = now()
	if t - Vt < 0.5 then return V end
	Vt = t
	pcall(function()
		V.player = plainStr(UnitName("player")) or ""; V.class = plainStr((UnitClass("player"))) or ""; V.race = plainStr((UnitRace("player"))) or ""
		-- level and XP are never shown on the overlay: these placeholders expand to nothing (the coach still gets them in the feed)
		V.level, V.xp, V.xpmax, V.xppct = "", "", "", ""
		V.zone = plainStr(GetZoneText()) or ""; V.subzone = plainStr(GetSubZoneText()) or ""
		local x, y
		if API.coords then x, y = API.coords() end
		V.x = x and strfmt("%.1f", x) or "?"; V.y = y and strfmt("%.1f", y) or "?"
		local g = plainNum(GetMoney()) or 0; V.gold = strfmt("%dg %ds", floor(g / 10000), floor(g / 100) % 100)
		local total, free = 0, 0
		for b = 0, 4 do
			local n = plainNum(C_Container.GetContainerNumSlots(b)) or 0
					total = total + n; free = free + (plainNum((C_Container.GetContainerNumFreeSlots(b))) or 0)
		end
		V.bags = strfmt("%d/%d", total - free, total)
		V.bagsUsed, V.bagsTotal = total - free, total
	end)
	return V
end

local function expand(s)
	if s == nil or s == "" then return "" end
	s = tostring(s):gsub("|", "||")
	local v = refreshVars()
	s = s:gsub("%[c=(#?%x+)%](.-)%[/c%]", function(col, inner) return "|cff" .. hexcode(col) .. inner .. "|r" end)
	s = s:gsub("{item:(%d+)}", function(id)
		local ok, link = pcall(function() return select(2, (C_Item and C_Item.GetItemInfo or GetItemInfo)(tonumber(id))) end)
		return (ok and link) or ("item " .. id)
	end)
	s = s:gsub("{spell:(%d+)}", function(id)
		local ok, link = pcall(function() return C_Spell.GetSpellLink(tonumber(id)) end)
		return (ok and link) or ("spell " .. id)
	end)
	s = s:gsub("{quest:(%d+):([^}]*)}", function(id, name) return strfmt("|cffffff00|Hquest:%d:0|h[%s]|h|r", tonumber(id), name) end)
	s = s:gsub("{(%a+)}", function(k) local x = v[k]; if x ~= nil then return tostring(x) end end)
	return s
end
local function hasVars(s) return type(s) == "string" and (s:find("{%a+}") ~= nil) end

local function sigOf(t)
	if type(t) ~= "table" then return tostring(t) end
	local entries = {}
	for k, v in pairs(t) do entries[#entries + 1] = { tostring(k), v } end
	table.sort(entries, function(x, y) return x[1] < y[1] end)
	local out = {}
	for _, e in ipairs(entries) do out[#out + 1] = e[1] .. "=" .. sigOf(e[2]) end
	return "{" .. tconcat(out, ",") .. "}"
end

---------------------------------------------------------------- per-player overrides
local function userFor(pid)
	local u = DB.user[pid]
	if not u then u = { done = {}, seen = {} }; DB.user[pid] = u end
	u.done = u.done or {}; u.seen = u.seen or {}
	return u
end

local function itemDone(pid, it)
	local u = DB.user[pid]
	if u and u.done and u.done[it.id] ~= nil then return u.done[it.id] end
	return it.done == true
end

local function syncVersions(pd)
	for _, c in ipairs(pd.children or {}) do
		if c.type == "checklist" then
			local u = userFor(pd.id)
			for _, it in ipairs(c.items or {}) do
				if it.ver and it.ver > (u.seen[it.id] or 0) then u.seen[it.id] = it.ver; u.done[it.id] = nil; S.ticks[pd.id .. "/" .. it.id] = nil end
			end
		end
	end
end

---------------------------------------------------------------- panel frames
local render = {}
local layout

local function setFont(fs, P, size)
	local t = P.t
	local flags = t.outline and "OUTLINE" or ""
	local ok, res = pcall(fs.SetFont, fs, FONT_PATHS[t.font] or STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", size, flags)
	if not ok or res == false then pcall(fs.SetFont, fs, STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", size, flags) end
	if t.shadow then fs:SetShadowColor(0, 0, 0, 0.9); fs:SetShadowOffset(1, -1) else fs:SetShadowOffset(0, 0) end
end

local function nextFS(P)
	P.n.fs = P.n.fs + 1
	local fs = P.fs[P.n.fs]
	if not fs then fs = P.body:CreateFontString(nil, "OVERLAY"); P.fs[P.n.fs] = fs end
	fs:Show(); fs:ClearAllPoints(); fs:SetWordWrap(true); fs:SetJustifyH("LEFT"); fs:SetJustifyV("TOP")
	fs:SetTextColor(1, 1, 1, 1); fs:SetAlpha(1)
	return fs
end
local function nextTex(P, layer)
	P.n.tex = P.n.tex + 1
	local tx = P.tex[P.n.tex]
	if not tx then tx = P.body:CreateTexture(nil, layer or "ARTWORK"); P.tex[P.n.tex] = tx end
	tx:Show(); tx:ClearAllPoints(); tx:SetTexCoord(0, 1, 0, 1); tx:SetColorTexture(1, 1, 1, 1); tx:SetAlpha(1); tx:SetDrawLayer(layer or "ARTWORK")
	return tx
end
local function nextRow(P)
	P.n.rows = P.n.rows + 1
	local r = P.rows[P.n.rows]
	if not r then r = CreateFrame("Button", nil, P.body); P.rows[P.n.rows] = r end
	r:Show(); r:ClearAllPoints(); r:EnableMouse(false); r:SetScript("OnClick", nil); r:SetScript("OnEnter", nil); r:SetScript("OnLeave", nil)
	return r
end
local function nextBar(P)
	P.n.bars = P.n.bars + 1
	local b = P.bars[P.n.bars]
	if not b then
		b = CreateFrame("StatusBar", nil, P.body)
		b:SetStatusBarTexture("Interface\\Buttons\\WHITE8X8")
		b.back = b:CreateTexture(nil, "BACKGROUND"); b.back:SetAllPoints()
		P.bars[P.n.bars] = b
	end
	b:Show(); b:ClearAllPoints(); b:EnableMouse(false)
	return b
end
local function endPool(P)
	for i = P.n.fs + 1, #P.fs do P.fs[i]:Hide() end
	for i = P.n.tex + 1, #P.tex do P.tex[i]:Hide() end
	for i = P.n.rows + 1, #P.rows do P.rows[i]:Hide(); P.rows[i]:EnableMouse(false) end
	for i = P.n.bars + 1, #P.bars do P.bars[i]:Hide() end
end

local function measure(fs, w, text, size)
	fs:SetWidth(w); fs:SetText(text)
	local h = fs:GetStringHeight()
	if not h or h <= 0 then
		local _, n = text:gsub("\n", "")
		h = size * 1.25 * (n + 1)
	end
	return h
end

local function col(P, spec, fallbackKey)
	if spec then return hex(spec) end
	return hex(P.t[fallbackKey or "text"])
end

local function dyn(P, d) P.dyn[#P.dyn + 1] = d end

local function placeText(P, c, x, w, y, template, size, colorSpec)
	local fs = nextFS(P)
	setFont(fs, P, size)
	local r, g, b = hex(colorSpec)
	fs:SetTextColor(r, g, b, 1)
	if c.align then fs:SetJustifyH(c.align:upper()) end
	fs:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -y)
	local txt = expand(template)
	local h = measure(fs, w, txt, size)
	if hasVars(template) then dyn(P, { kind = "text", fs = fs, src = template, w = w, size = size, h = h, last = txt }) end
	return h, fs
end

render.heading = function(P, c, x, w, y)
	local size = (c.size or P.t.size) + 2
	local h = placeText(P, c, x, w, y, (c.icon and (c.icon .. " ") or "") .. c.text, size, c.color or P.t.accent)
	return h
end

render.text = function(P, c, x, w, y)
	local size = c.size or P.t.size
	local colorSpec = c.color or (c.muted and P.t.muted or P.t.text)
	return (placeText(P, c, x, w, y, c.text, size, colorSpec))
end

local function toggleItem(P, it)
	local u = userFor(P.id)
	local val = not itemDone(P.id, it)
	u.done[it.id] = val
	S.tickSeq = S.tickSeq + 1
	S.ticks[P.id .. "/" .. it.id] = { val = val, seq = S.tickSeq }
	P.dirty = true; S.dirtyState = true; P.lastActivity = now()
end

-- Item lookup for the coach (auto-complete, waypoint tasks, rule "check" actions). Ticks go through toggleItem, so
-- an automatic tick is recorded and reported exactly like a player click.
local function findItem(pid, iid)
	local P = S.panels[pid]
	if not P or not P.data or P.removed then return nil end
	local function scan(list)
		for _, c in ipairs(list or {}) do
			if c.type == "checklist" then
				for _, it in ipairs(c.items or {}) do if it.id == iid then return it end end
			elseif c.type == "row" then
				local it = scan(c.children)
				if it then return it end
			end
		end
	end
	local it = scan(P.data.children)
	if it then return P, it end
end
function UI.itemDone(pid, iid)
	local P, it = findItem(pid, iid)
	if not P then return nil end
	return itemDone(pid, it)
end
function UI.setItem(pid, iid, val)
	local P, it = findItem(pid, iid)
	if not P then return false end
	if itemDone(pid, it) == (val and true or false) then return false end
	toggleItem(P, it)
	return true
end
-- calls fn(panelId, item) for every checklist item that carries an "auto" rule
function UI.eachAutoItem(fn)
	local function scan(P, list)
		for _, c in ipairs(list or {}) do
			if c.type == "checklist" then
				for _, it in ipairs(c.items or {}) do if type(it.auto) == "table" then fn(P.id, it, itemDone(P.id, it)) end end
			elseif c.type == "row" then scan(P, c.children) end
		end
	end
	for id, P in pairs(S.panels) do
		if P.data and not P.removed then scan(P, P.data.children) end
	end
end

render.checklist = function(P, c, x, w, y)
	local size = c.size or P.t.size
	local cur = y
	if c.title then
		local h = placeText(P, c, x, w, cur, c.title, size - 1, P.t.muted)
		cur = cur + h + 2
	end
	local interactive = P.data.interactive ~= false
	local box = max(10, size)
	for idx, it in ipairs(c.items or {}) do
		local done = itemDone(P.id, it)
		local tx = x + box + 6
		local text = (c.numbered and (idx .. ". ") or "") .. it.text
		local fs = nextFS(P); setFont(fs, P, size)
		local r, g, b = hex(done and P.t.muted or (c.color or P.t.text)); fs:SetTextColor(r, g, b, done and 0.75 or 1)
		fs:SetPoint("TOPLEFT", P.body, "TOPLEFT", tx, -cur)
		local th = measure(fs, w - (tx - x), expand(text), size)
		local total = max(th, box)
		if it.note and it.note ~= "" then
			local nf = nextFS(P); setFont(nf, P, size - 1)
			local nr, ng, nb = hex(P.t.muted); nf:SetTextColor(nr, ng, nb, 0.9)
			nf:SetPoint("TOPLEFT", P.body, "TOPLEFT", tx, -(cur + th + 1))
			local nh = measure(nf, w - (tx - x), expand(it.note), size - 1)
			total = max(th + 1 + nh, box)
		end
		-- checkbox: outline in the priority colour, dark inside, filled when done
		local pc = it.priority == "high" and P.t.bad or it.priority == "med" and P.t.warn or P.t.muted
		local o = nextTex(P, "ARTWORK"); o:SetColorTexture(hex(pc)); o:SetSize(box, box); o:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -cur - 1)
		local inn = nextTex(P, "ARTWORK"); inn:SetColorTexture(0.04, 0.05, 0.07, 0.95); inn:SetSize(box - 2, box - 2); inn:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + 1, -cur - 2)
		inn:SetDrawLayer("ARTWORK", 1)
		if done then
			local f = nextTex(P, "ARTWORK"); f:SetColorTexture(hex(P.t.good)); f:SetSize(box - 6, box - 6); f:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + 3, -cur - 4)
			f:SetDrawLayer("ARTWORK", 2)
		end
		if interactive then
			local row = nextRow(P)
			row:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -cur); row:SetSize(w, total + 2); row:EnableMouse(true)
			row:SetScript("OnClick", function() toggleItem(P, it) end)
		end
		cur = cur + total + 3
	end
	return cur - y
end

local function dura(slot)
	if not GetInventoryItemDurability then return nil end
	local ok, a, b = pcall(GetInventoryItemDurability, slot)
	a, b = plainNum(a), plainNum(b)
	if ok and a and b then return a, b end
end

local function resolveProgress(c)
	local v, m = c.value or 0, c.max or 100
	if c.bind == "bags" then local r = refreshVars(); v, m = r.bagsUsed or 0, r.bagsTotal or 1
	elseif c.bind == "durability" then
		local cur, mx = 0, 0
		for slot = 1, 19 do local a, b = dura(slot); if a and b and b > 0 then cur = cur + a; mx = mx + b end end
		v, m = cur, (mx > 0 and mx or 1)
	end
	return v, (m and m > 0) and m or 1
end

local function hiddenBind(c) return c.bind == "xp" or c.bind == "xpmax" or c.bind == "xppct" or c.bind == "level" end

render.progress = function(P, c, x, w, y)
	if hiddenBind(c) then return 0 end   -- XP and level are never drawn
	local size = (c.size or P.t.size) - 1
	local cur = y
	local left, right
	if c.label or c.showText ~= false then
		left = nextFS(P); setFont(left, P, size)
		local r, g, b = hex(P.t.muted); left:SetTextColor(r, g, b, 1)
		left:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -cur); left:SetWidth(w * 0.6); left:SetText(expand(c.label or ""))
		right = nextFS(P); setFont(right, P, size)
		right:SetTextColor(hex(P.t.text)); right:SetJustifyH("RIGHT")
		right:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + w * 0.4, -cur); right:SetWidth(w * 0.6)
		cur = cur + size + 3
	end
	local bar = nextBar(P)
	local h = c.height or 8
	bar:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -cur); bar:SetSize(w, h)
	bar.back:SetColorTexture(0, 0, 0, 0.45)
	bar:SetStatusBarColor(hex(c.fill or c.color or P.t.accent))
	local d = { kind = "progress", c = c, bar = bar, right = right }
	local function update()
		local v, m = resolveProgress(c)
		bar:SetMinMaxValues(0, m); bar:SetValue(min(v, m))
		if right and c.showText ~= false then right:SetText(strfmt("%d / %d  (%d%%)", floor(v), floor(m), floor(v / m * 100 + 0.5))) end
	end
	d.update = update; update(); dyn(P, d)
	return (cur + h) - y
end

render.kv = function(P, c, x, w, y)
	local size = c.size or P.t.size
	local kw = floor(w * 0.38)
	local cur = y
	for _, row in ipairs(c.rows or {}) do
		local k = nextFS(P); setFont(k, P, size); k:SetTextColor(hex(c.keyColor or P.t.muted)); k:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -cur)
		local kh = measure(k, kw - 4, expand(row[1]), size)
		local v = nextFS(P); setFont(v, P, size); v:SetTextColor(hex(c.color or P.t.text)); v:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + kw, -cur)
		local vtxt = expand(row[2])
		local vh = measure(v, w - kw, vtxt, size)
		if hasVars(row[2]) then dyn(P, { kind = "text", fs = v, src = row[2], w = w - kw, size = size, h = vh, last = vtxt }) end
		cur = cur + max(kh, vh) + 2
	end
	return cur - y
end

local LEVEL_COLOR = { info = "accent", tip = "accent", good = "good", warn = "warn", bad = "bad" }
render.callout = function(P, c, x, w, y)
	local size = c.size or P.t.size
	local key = LEVEL_COLOR[c.level or "info"] or "accent"
	local pad = 6
	local bgt = nextTex(P, "BACKGROUND"); local r, g, b = hex(P.t[key]); bgt:SetColorTexture(r, g, b, 0.12)
	local bar = nextTex(P, "ARTWORK"); bar:SetColorTexture(r, g, b, 0.9)
	local cur = y + pad
	if c.title then
		local fs = nextFS(P); setFont(fs, P, size); fs:SetTextColor(r, g, b, 1)
		fs:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + pad + 4, -cur)
		cur = cur + measure(fs, w - pad * 2 - 4, expand(c.title), size) + 1
	end
	local fs = nextFS(P); setFont(fs, P, size)
	fs:SetTextColor(hex(c.color or P.t.text)); fs:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + pad + 4, -cur)
	local txt = expand(c.text)
	local th = measure(fs, w - pad * 2 - 4, txt, size)
	if hasVars(c.text) then dyn(P, { kind = "text", fs = fs, src = c.text, w = w - pad * 2 - 4, size = size, h = th, last = txt }) end
	cur = cur + th + pad
	bgt:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -y); bgt:SetSize(w, cur - y)
	bar:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -y); bar:SetSize(3, cur - y)
	return cur - y
end

local function fmtTime(sec, mode)
	sec = max(0, floor(sec))
	if mode == "words" then
		if sec >= 3600 then return strfmt("%dh %02dm", floor(sec / 3600), floor(sec / 60) % 60) end
		if sec >= 60 then return strfmt("%dm %02ds", floor(sec / 60), sec % 60) end
		return strfmt("%ds", sec)
	end
	return strfmt("%d:%02d", floor(sec / 60), sec % 60)
end
render.timer = function(P, c, x, w, y)
	local size = c.size or P.t.size
	local fs = nextFS(P); setFont(fs, P, size); fs:SetTextColor(hex(c.color or P.t.text)); fs:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -y)
	local doc0 = S.docTime
	local function text()
		local remain = c["until"] and (c["until"] - (time and time() or 0)) or ((c.seconds or 0) - (now() - doc0))
		local label = c.text and expand(c.text) .. "  " or ""
		return label .. (remain <= 0 and ("|cff" .. hexcode(P.t.good) .. "now|r") or fmtTime(remain, c.format))
	end
	local h = measure(fs, w, text(), size)
	dyn(P, { kind = "timer", fs = fs, text = text, w = w, size = size, h = h })
	return h
end

render.icon = function(P, c, x, w, y)
	local size = c.iconSize or 24
	local tex = nextTex(P, "ARTWORK")
	local path
	pcall(function()
		if c.spell then path = (C_Spell and C_Spell.GetSpellTexture and C_Spell.GetSpellTexture(c.spell)) or GetSpellTexture(c.spell)
		elseif c.item then path = (C_Item and C_Item.GetItemIconByID and C_Item.GetItemIconByID(c.item)) or GetItemIcon(c.item)
		elseif c.texture then
			local tname = c.texture
			path = tonumber(tname) or (tname:find("\\") and tname or ("Interface\\Icons\\" .. tname))
		end
	end)
	tex:SetTexture(path or "Interface\\Icons\\INV_Misc_QuestionMark")
	tex:SetSize(size, size); tex:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -y)
	local th = 0
	if c.text then
		local fsz = c.size or P.t.size
		local fs = nextFS(P); setFont(fs, P, fsz); fs:SetTextColor(hex(c.color or P.t.text)); fs:SetPoint("TOPLEFT", P.body, "TOPLEFT", x + size + 6, -y)
		th = measure(fs, w - size - 6, expand(c.text), fsz)
	end
	return max(size, th)
end

render.divider = function(P, c, x, w, y)
	local t = nextTex(P, "ARTWORK"); t:SetColorTexture(hex(P.t.muted)); t:SetAlpha(0.35)
	local th = c.thickness or 1
	t:SetPoint("TOPLEFT", P.body, "TOPLEFT", x, -(y + 2)); t:SetSize(w, th)
	return th + 4
end

render.spacer = function(P, c, x, w, y) return c.height or 6 end

-- Rules can replace the text of a component (action panel_text). Position, size and alpha are never touched.
local function withOverride(P, c)
	local o = c.id and S.override[P.id .. "/" .. c.id]
	if o == nil then return c end
	local copy = {}
	for k, v in pairs(c) do copy[k] = v end
	copy.text = o
	return copy
end
function UI.panelText(pid, cid, text)
	local P = S.panels[pid]
	if not P or not P.data or P.removed then return false end
	local found = false
	local function scan(list)
		for _, c in ipairs(list or {}) do
			if c.id == cid and c.type ~= "checklist" then found = true elseif c.type == "row" then scan(c.children) end
		end
	end
	scan(P.data.children)
	if not found then return false end
	S.override[pid .. "/" .. cid] = tostring(text)
	P.dirty = true
	return true
end

render.row = function(P, c, x, w, y)
	local kids = c.children or {}
	local n = #kids
	if n == 0 then return 0 end
	local gap = c.gap or 8
	local cw = floor((w - gap * (n - 1)) / n)
	local hmax = 0
	for i, k0 in ipairs(kids) do
		local k = withOverride(P, k0)
		local f = render[k.type]
		if f then
			local ok, h = pcall(f, P, k, x + (i - 1) * (cw + gap), cw, y)
			if ok then hmax = max(hmax, h or 0) else lerr("row child: " .. tostring(h)) end
		end
	end
	return hmax
end

local function componentVisible(c)
	if c.type == "progress" and hiddenBind(c) then return false end
	if c.when == "combat" then return truthy(call(UnitAffectingCombat, "player")) end
	if c.when == "outofcombat" then return not truthy(call(UnitAffectingCombat, "player")) end
	return true
end

layout = function(P)
	local d, t = P.data, P.t
	P.n = { fs = 0, tex = 0, rows = 0, bars = 0 }
	P.dyn = {}
	local pad = t.padding
	local w = d.w or 300
	local u = DB.user[P.id]
	local collapsed = (u and u.collapsed ~= nil) and u.collapsed or (d.collapsed == true)
	local headerH = 0
	local showHeader = d.header ~= false and (d.title ~= nil or d.collapsible ~= false)
	local hs = t.size + 1
	if showHeader then
		P.title:Show(); setFont(P.title, P, hs)
		P.title:SetTextColor(hex(t.accent))
		P.title:SetText(expand(d.title or "") .. (d.subtitle and ("  |cff" .. hexcode(t.muted) .. expand(d.subtitle) .. "|r") or ""))
		P.title:ClearAllPoints(); P.title:SetPoint("TOPLEFT", P.frame, "TOPLEFT", pad, -pad); P.title:SetWidth(w - pad * 2 - 14)
		P.chev:Show(); setFont(P.chev, P, hs); P.chev:SetTextColor(hex(t.muted)); P.chev:SetText(collapsed and "+" or "-")
		P.chev:ClearAllPoints(); P.chev:SetPoint("TOPRIGHT", P.frame, "TOPRIGHT", -pad, -pad)
		headerH = hs + 4
		P.header:ClearAllPoints(); P.header:SetPoint("TOPLEFT", P.frame, "TOPLEFT", 0, 0); P.header:SetSize(w, pad + headerH)
		P.header:EnableMouse(true); P.header:Show()
	else
		P.title:Hide(); P.chev:Hide(); P.header:EnableMouse(false); P.header:Hide()
	end
	P.body:ClearAllPoints(); P.body:SetPoint("TOPLEFT", P.frame, "TOPLEFT", pad, -(pad + headerH))
	P.body:SetWidth(w - pad * 2)
	local y = 0
	if collapsed then
		P.body:Hide()
	else
		P.body:Show()
		local innerW = w - pad * 2
		for _, c0 in ipairs(d.children or {}) do
			local c = withOverride(P, c0)
			local f = render[c.type]
			if f and componentVisible(c) then
				local ok, h = pcall(f, P, c, c.indent or 0, innerW - (c.indent or 0), y)
				if ok then y = y + (h or 0) + t.gap else lerr(c.type .. ": " .. tostring(h)) end
			end
		end
	end
	endPool(P)
	local total = pad * 2 + headerH + max(0, y - (collapsed and 0 or t.gap))
	if d.maxH and total > d.maxH then total = d.maxH end
	P.body:SetHeight(max(1, total - pad * 2 - headerH))
	pcall(P.body.SetClipsChildren, P.body, true)
	P.frame:SetSize(w, total)
	P.frame.bg:SetColorTexture(hex(t.bg)); P.frame.bg:SetAlpha(t.bgAlpha)
	local br, bg_, bb = hex(t.border)
	for _, e in ipairs(P.frame.edges) do e:SetColorTexture(br, bg_, bb, t.borderAlpha) end
	P.frame.accent:SetColorTexture(hex(t.accent)); P.frame.accent:SetAlpha(t.cornerAccent and 0.9 or 0)
	P.height = total
	P.dirty = false
end

local editMode, manualEdit = false, false      -- Blizzard Edit Mode open / /wfui edit
local function startMove(P)
	if P.data and P.data.draggable ~= false and (editMode or manualEdit or not DB.locked) then P.frame:StartMoving(); P.moving = true end
end
local function stopMove(P)
	P.frame:StopMovingOrSizing()
	if P.moving then
		P.moving = false
		local point, _, rel, px, py = P.frame:GetPoint()
		userFor(P.id).pos = { point, rel, px, py }
		P.auto = nil; P.lastActivity = now(); S.dirtyState = true
	end
end

local function newPanel(id)
	local f = CreateFrame("Frame", "WoWFeedPanel_" .. id, UIParent)
	f:SetFrameStrata("MEDIUM"); f:SetClampedToScreen(true); f:EnableMouse(false); f:SetMovable(true)
	f.bg = f:CreateTexture(nil, "BACKGROUND"); f.bg:SetAllPoints()
	f.edges = {}
	for i = 1, 4 do f.edges[i] = f:CreateTexture(nil, "BORDER") end
	f.edges[1]:SetPoint("TOPLEFT"); f.edges[1]:SetPoint("TOPRIGHT"); f.edges[1]:SetHeight(1)
	f.edges[2]:SetPoint("BOTTOMLEFT"); f.edges[2]:SetPoint("BOTTOMRIGHT"); f.edges[2]:SetHeight(1)
	f.edges[3]:SetPoint("TOPLEFT"); f.edges[3]:SetPoint("BOTTOMLEFT"); f.edges[3]:SetWidth(1)
	f.edges[4]:SetPoint("TOPRIGHT"); f.edges[4]:SetPoint("BOTTOMRIGHT"); f.edges[4]:SetWidth(1)
	f.hl = f:CreateTexture(nil, "OVERLAY"); f.hl:SetAllPoints(); f.hl:SetColorTexture(1, 1, 1, 0.12); f.hl:Hide()
	f.accent = f:CreateTexture(nil, "BORDER", nil, 1)
	f.accent:SetPoint("TOPLEFT"); f.accent:SetPoint("TOPRIGHT"); f.accent:SetHeight(2)
	local P = { id = id, frame = f, n = { fs = 0, tex = 0, rows = 0, bars = 0 }, fs = {}, tex = {}, rows = {}, bars = {}, dyn = {}, dirty = true, alpha = 0, lastActivity = now() }
	P.body = CreateFrame("Frame", nil, f); P.body:EnableMouse(false)
	P.title = f:CreateFontString(nil, "OVERLAY"); P.title:SetJustifyH("LEFT"); P.title:SetWordWrap(false)
	P.chev = f:CreateFontString(nil, "OVERLAY")
	P.header = CreateFrame("Button", nil, f); P.header:EnableMouse(true)
	P.header:RegisterForDrag("LeftButton")
	P.header:SetScript("OnClick", function()
		if P.data and P.data.collapsible ~= false then
			local u = userFor(P.id)
			local cur = (u.collapsed ~= nil) and u.collapsed or (P.data.collapsed == true)
			u.collapsed = not cur; P.dirty = true; S.dirtyState = true
		end
	end)
	P.header:SetScript("OnDragStart", function() startMove(P) end)
	P.header:SetScript("OnDragStop", function() stopMove(P) end)
	-- hold Shift (or open Edit Mode / /wfui edit) and the whole panel can be grabbed; see the driver below
	f:RegisterForDrag("LeftButton")
	f:SetScript("OnDragStart", function() startMove(P) end)
	f:SetScript("OnDragStop", function() stopMove(P) end)
	return P
end

local function placePanel(P)
	local d = P.data
	local f = P.frame
	f:SetScale(max(0.3, (d.scale or 1) * DB.scale))
	local u = DB.user[P.id]
	P.auto = nil
	f:ClearAllPoints()
	if u and u.pos then
		f:SetPoint(u.pos[1], UIParent, u.pos[2], u.pos[3], u.pos[4])
	elseif d.anchor or d.x or d.y then
		local a = d.anchor or "TOPRIGHT"
		f:SetPoint(a, UIParent, a, d.x or 0, d.y or 0)
	else
		P.auto = true
	end
end

local function autoStack()
	local y = -250
	for _, id in ipairs(S.order) do
		local P = S.panels[id]
		if P and P.auto and P.shown and not (DB.user[id] and DB.user[id].pos) then
			P.frame:ClearAllPoints()
			P.frame:SetPoint("TOPRIGHT", UIParent, "TOPRIGHT", -20, y)
			y = y - (P.height or 100) * (P.frame:GetScale() or 1) - 10
		end
	end
end

---------------------------------------------------------------- visibility driver
local function computeTarget(P)
	if S.hidden or P.removed then return 0 end
	local d = P.data
	if editMode or manualEdit or P.moving then return max(0.9, (d.alpha or 1) * DB.alpha) end
	local v = d.visibility or {}
	local lvl = plainNum(call(UnitLevel, "player")) or 1
	if (v.minLevel and lvl < v.minLevel) or (v.maxLevel and lvl > v.maxLevel) then return 0 end
	if v.hideWhenMounted and truthy(call(IsMounted)) then return 0 end
	if v.hideInInstance and truthy(call(IsInInstance)) then return 0 end
	if v.ctx then local cx, okc = UI.context(), false; for _, x in ipairs(v.ctx) do if x == cx then okc = true end end; if not okc then return 0 end end
	local full = (d.alpha or 1) * DB.alpha
	local a = full
	local inCombat = truthy(call(UnitAffectingCombat, "player")) or truthy(call(InCombatLockdown))
	if inCombat and not d.pin then
		local mode = v.combat or "dim"
		if mode == "hide" then return 0 elseif mode == "dim" then a = a * (v.dim or 0.3) end
	end
	local hover = false
	if v.mouseover ~= false and MouseIsOver then
		local ok, res = pcall(MouseIsOver, P.frame)
		hover = ok and res
	end
	if hover then P.lastActivity = now(); return max(a, full) end
	if v.idleFade and v.idleFade > 0 and (now() - P.lastActivity) > v.idleFade then a = a * (v.idleAlpha or 0.35) end
	return a
end

local driver = CreateFrame("Frame")
local acc = 0
driver:SetScript("OnUpdate", function(_, dt)
	acc = acc + dt
	if acc < 0.1 then return end
	local step = acc; acc = 0
	local edit = manualEdit
	if EditModeManagerFrame and EditModeManagerFrame.IsShown then
		local okE, shownE = pcall(EditModeManagerFrame.IsShown, EditModeManagerFrame)
		if okE and shownE then edit = true end
	end
	editMode = edit
	local grab = edit or (IsShiftKeyDown and IsShiftKeyDown() and not DB.locked) or false
	for id, P in pairs(S.panels) do
		if P.data and P.grab ~= grab then P.grab = grab; P.frame:EnableMouse(grab); if grab then P.frame.hl:Show() else P.frame.hl:Hide() end end
		if P.data then
			if P.dirty and not P.removed then
				local ok, err = pcall(layout, P)
				if not ok then lerr("layout " .. id .. ": " .. tostring(err)); P.dirty = false end
				autoStack()
			end
			local okT, target = pcall(computeTarget, P)
			if not okT then target = P.alpha > 0.01 and P.alpha or 1; lerr("visibility " .. id .. ": " .. tostring(target)); target = 1 end
			local a = P.alpha + (target - P.alpha) * min(1, step * 8)
			if abs(a - target) < 0.01 then a = target end
			P.alpha = a
			if a ~= P.setAlpha then P.setAlpha = a; P.frame:SetAlpha(a) end
			local show = a > 0.01
			if show ~= P.shown then P.shown = show; if show then P.frame:Show() else P.frame:Hide() end; autoStack() end
		end
	end
end)

---------------------------------------------------------------- toasts
local toastFrames = {}
local SOUND_IDS = { ping = 3175, warning = 8959, quest = 878, levelup = 888, ready = 8960 }
local function playSound(name)
	local id = SOUND_IDS[name]
	if id and PlaySound then pcall(PlaySound, id, "Master") end
end

local TOAST_POOL = 6     -- frames are never freed in WoW: at most this many exist, the oldest one is reused when all are busy
local function showToast(t)
	if type(t) ~= "table" or type(t.text) ~= "string" then return false end
	local theme = S.theme
	local pt = { t = {} }
	for k, v in pairs(DEFAULT_THEME) do pt.t[k] = v end
	for k, v in pairs(theme or {}) do pt.t[k] = v end
	local key = LEVEL_COLOR[t.level or "info"] or "accent"
	local f
	for _, cand in ipairs(toastFrames) do if not cand.busy then f = cand; break end end
	if not f and #toastFrames >= TOAST_POOL then
		f = toastFrames[1]                                  -- reuse the longest-shown toast
		table.remove(toastFrames, 1); toastFrames[#toastFrames + 1] = f
	end
	if not f then
		f = CreateFrame("Frame", nil, UIParent); f:SetFrameStrata("HIGH"); f:EnableMouse(false)
		f.bg = f:CreateTexture(nil, "BACKGROUND"); f.bg:SetAllPoints()
		f.bar = f:CreateTexture(nil, "ARTWORK"); f.bar:SetPoint("TOPLEFT"); f.bar:SetPoint("BOTTOMLEFT"); f.bar:SetWidth(4)
		f.title = f:CreateFontString(nil, "OVERLAY"); f.text = f:CreateFontString(nil, "OVERLAY")
		toastFrames[#toastFrames + 1] = f
	end
	f.busy = true
	local size = pt.t.size + 1
	local pcall_ = function(fs, sz) pcall(fs.SetFont, fs, STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF", sz, pt.t.outline and "OUTLINE" or "") fs:SetShadowColor(0, 0, 0, 0.9); fs:SetShadowOffset(1, -1) end
	pcall_(f.title, size); pcall_(f.text, size)
	local r, g, b = hex(pt.t[key])
	f.bar:SetColorTexture(r, g, b, 1); f.bg:SetColorTexture(0.04, 0.05, 0.08, 0.82)
	local w = 360
	f.title:ClearAllPoints(); f.text:ClearAllPoints()
	f.title:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -8); f.title:SetWidth(w - 24); f.title:SetTextColor(r, g, b); f.title:SetJustifyH("LEFT")
	f.text:SetWordWrap(true); f.text:SetJustifyH("LEFT"); f.text:SetWidth(w - 24); f.text:SetTextColor(hex(pt.t.text))
	local y = 8
	if t.title then f.title:SetText(expand(t.title)); f.title:Show(); y = y + (f.title:GetStringHeight() or 14) + 2 else f.title:Hide() end
	f.text:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -y); f.text:SetText(expand(t.text))
	y = y + (f.text:GetStringHeight() or 14) + 8
	f:SetSize(w, y)
	local idx = 0
	for i, cand in ipairs(toastFrames) do if cand == f then idx = i end end
	f:ClearAllPoints(); f:SetPoint("TOP", UIParent, "TOP", 0, -110 - (idx - 1) * 6)
	f:SetAlpha(0); f:Show()
	local start, ttl = now(), t.ttl or 8
	local tacc = 0
	f:SetScript("OnUpdate", function(self, dt)
		tacc = tacc + (dt or 0.05)
		if tacc < 0.04 then return end          -- ~25 fps is plenty for a fade
		tacc = 0
		local e = now() - start
		local a = 1
		if e < 0.25 then a = e / 0.25 elseif e > ttl then a = max(0, 1 - (e - ttl) / 0.6) end
		self:SetAlpha(a)
		if e > ttl + 0.6 then self:Hide(); self:SetScript("OnUpdate", nil); self.busy = false end
	end)
	playSound(t.sound)
	return true
end

---------------------------------------------------------------- waypoints and actions
local function clearWaypoint(id)
	local w = S.wps[id]
	if not w then return end
	if w.via == "TomTom" and TomTom and TomTom.RemoveWaypoint and w.uid then pcall(TomTom.RemoveWaypoint, TomTom, w.uid)
	elseif w.via == "native" and C_Map and C_Map.ClearUserWaypoint then pcall(C_Map.ClearUserWaypoint) end
	S.wps[id] = nil
end

-- A waypoint the coach HUD cleared (reached, or its task is done) stays cleared until the agent changes it:
-- the document still lists it, so the same signature must not bring it back on the next document revision.
local function dismissWaypoint(id, reason)
	local w = S.wps[id]
	if not w then return end
	S.wpDismissed[id] = w.sig
	S.wpNote[id] = reason or "cleared"
	clearWaypoint(id)
	S.dirtyState = true
end

-- Focus a quest the way the player would: Questie's tracker focus (fades the other pins, supertracks the quest) when Questie
-- is loaded, the Blizzard watch and supertrack otherwise. Advice-level helper: it only changes what is highlighted.
local function focusQuest(id)
	id = tonumber(id)
	if not id then return false end
	if QuestieLoader and QuestieLoader.ImportModule then
		local ok, done = pcall(function()
			local TU = QuestieLoader:ImportModule("TrackerUtils")
			return TU and TU.FocusQuest and TU:FocusQuest(id)
		end)
		if ok and done then return true end
	end
	pcall(function() C_QuestLog.AddQuestWatch(id) end)
	return (pcall(function() C_SuperTrack.SetSuperTrackedQuestID(id) end))
end

-- Guide edits without /reload. The bridge writes the edited guide file to disk AND sends its text here; we register it with the running
-- Guidelime (Guidelime.registerGuide is its public API) and then run the same refresh Guidelime runs when a player toggles a map option
-- (re-parse the current guide, re-match ticks by step text, redraw). Everything is guarded: if Guidelime changes shape the edit stays on
-- disk, nothing is acknowledged, and the server falls back to its "type /reload" prompt.
local function limeOptionsFrame()
	if S.limeContent and S.limeContent.options then return S.limeContent end
	if not EnumerateFrames then return nil end
	local f = EnumerateFrames()
	while f do
		local ok, o = pcall(function() return f.options end)
		if ok and type(o) == "table" and o.showMapMarkersGOTO then S.limeContent = f; return f end
		f = EnumerateFrames(f)
	end
	return nil
end
local function refreshLime()
	local c = limeOptionsFrame()
	local cb = c and c.options and c.options.showMapMarkersGOTO
	local h = cb and cb.GetScript and cb:GetScript("OnClick")
	if not h then return false end
	h(cb)           -- Guidelime's handler stores the checkbox's current value again, then loadCurrentGuide(false), updateFromQuestLog, updateMainFrame
	return true
end
function UI.applyGuideLive(list)
	if not (Guidelime and Guidelime.registerGuide) then return end
	S.gliveTried = S.gliveTried or {}
	local pending = {}
	for _, g in ipairs(list) do
		if type(g) == "table" and type(g.id) == "number" and g.id > (DB.glive or 0) and not S.gliveTried[g.id] and type(g.text) == "string" and type(g.group) == "string" then
			pending[#pending + 1] = g
		end
	end
	if #pending == 0 then return end
	table.sort(pending, function(a, b) return a.id < b.id end)
	local maxid, registered = 0, 0
	for _, g in ipairs(pending) do
		S.gliveTried[g.id] = true
		local ok, err = pcall(Guidelime.registerGuide, g.text, g.group)
		if ok then registered = registered + 1; maxid = g.id
		else S.gliveErr = tostring(err):sub(1, 90); lerr("guide live: " .. tostring(err)) end
	end
	if registered == 0 then return end
	local ok, refreshed = pcall(refreshLime)
	if ok and refreshed then
		DB.glive = maxid; S.glive = maxid; S.gliveErr = nil; S.gliveAt = now()
	else
		S.gliveErr = "registered " .. registered .. " guide(s) but could not refresh Guidelime's current guide"
	end
end

-- Where the player is, in the words panels use: town, wild, contested, instance, battleground, city_pvp. Cached for half a second.
function UI.context()
	local t = now()
	if S.ctxAt and t - S.ctxAt < 0.5 then return S.ctx end
	local ok, inInst, itype = pcall(IsInInstance)
	local cx = "wild"
	if ok and inInst then
		cx = (itype == "pvp" or itype == "arena") and "battleground" or "instance"
	else
		local okz, zt = pcall(GetZonePVPInfo)
		local resting = truthy(call(IsResting))
		if resting then cx = (okz and (zt == "hostile" or zt == "contested")) and "city_pvp" or "town"
		elseif okz and (zt == "contested" or zt == "hostile" or zt == "combat") then cx = "contested"
		elseif okz and zt == "sanctuary" then cx = "town" end
	end
	S.ctx, S.ctxAt = cx, t
	return cx
end

-- Spoken advice (opt-in): the game's own text to speech through C_VoiceChat.SpeakText. The agent turns it on with `wowui builtin --voice on`;
-- the player can always override with /wfui voice on|off. Throttled, short, never in a loop.
local function voiceOn()
	if DB.voiceMode == "off" then return false end
	if DB.voiceMode == "on" then return true end
	return S.voice == true
end
function UI.speak(text)
	if not voiceOn() or type(text) ~= "string" or text == "" then return false end
	local t = now()
	if t - (S.speakAt or 0) < 4 then return false end
	if not (C_VoiceChat and C_VoiceChat.SpeakText) then return false end
	local voice = 0
	pcall(function()
		if C_TTSSettings and C_TTSSettings.GetVoiceOptionID then voice = C_TTSSettings.GetVoiceOptionID((Enum and Enum.TtsVoiceType and Enum.TtsVoiceType.Standard) or 0) or 0 end
	end)
	text = expand(text):sub(1, 220)
	-- 12.x: (voiceID, text, rate, volume, overlap); older clients take a destination before the rate
	local ok = pcall(C_VoiceChat.SpeakText, voice, text, 0, 100, false)
	if not ok then ok = pcall(C_VoiceChat.SpeakText, voice, text, (Enum and Enum.VoiceTtsDestination and Enum.VoiceTtsDestination.LocalPlayback) or 1, 0, 100) end
	if ok then S.speakAt = t end
	return ok
end

-- Mentor verdicts in item tooltips (doc.verdicts: itemId -> short text).
local function hookTooltips()
	if S.tooltipHooked then return end
	if not (TooltipDataProcessor and TooltipDataProcessor.AddTooltipPostCall and Enum and Enum.TooltipDataType) then return end
	local ok = pcall(TooltipDataProcessor.AddTooltipPostCall, Enum.TooltipDataType.Item, function(tt, data)
		local id = type(data) == "table" and data.id or nil
		if id == nil or (issecretvalue and issecretvalue(id)) then return end
		local v = S.verdicts and S.verdicts[tostring(id)]
		if v and tt and tt.AddLine then tt:AddLine("|cff33ccffMentor:|r " .. v, 1, 1, 1, true) end
	end)
	if ok then S.tooltipHooked = true end
end
function UI.watchSpells() return S.watchSpells or {} end

-- owner "rule" marks a waypoint created by a rule action: a new document revision does not sweep it away.
local function setWaypoint(w, owner)
	local sig = strfmt("%s:%s:%s:%s:%s:%s:%s", w.map, w.x, w.y, w.title or "", tostring(w.clear_yards or ""), tostring(w.task or ""), tostring(w.quest or ""))
	if S.wpDismissed[w.id] == sig then return end
	S.wpDismissed[w.id] = nil; S.wpNote[w.id] = nil
	local cur = S.wps[w.id]
	if cur and cur.sig == sig then return end
	if cur then clearWaypoint(w.id) end
	local x, y = w.x / 100, w.y / 100
	local rec = { sig = sig, data = w, owner = owner, t = now() }
	if w.quest then focusQuest(w.quest) end
	if TomTom and TomTom.AddWaypoint then
		local ok, uid = pcall(TomTom.AddWaypoint, TomTom, w.map, x, y, { title = w.title or "Mentor", persistent = false, minimap = true, world = true, crazy = w.track and true or false, silent = true, from = "WoWFeed" })
		if ok and uid then rec.uid = uid; rec.via = "TomTom"; S.wps[w.id] = rec; return end
	end
	if C_Map and C_Map.SetUserWaypoint and UiMapPoint then
		local ok = pcall(function()
			if C_Map.CanSetUserWaypointOnMap and not C_Map.CanSetUserWaypointOnMap(w.map) then error("map not allowed") end
			C_Map.SetUserWaypoint(UiMapPoint.CreateFromCoordinates(w.map, x, y))
			if w.track and C_SuperTrack and C_SuperTrack.SetSuperTrackedUserWaypoint then C_SuperTrack.SetSuperTrackedUserWaypoint(true) end
		end)
		if ok then rec.via = "native"; S.wps[w.id] = rec; return end
		lerr("waypoint " .. w.id .. " failed")
	end
	rec.via = "hud"      -- no map pin possible: the coach HUD (distance and arrow) still guides
	S.wps[w.id] = rec
end

local function runAction(a)
	if a.name == "flash" then if FlashClientIcon then pcall(FlashClientIcon) end
	elseif a.name == "sound" then playSound(a.sound or "ping")
	elseif a.name == "chat" then
		if DEFAULT_CHAT_FRAME then DEFAULT_CHAT_FRAME:AddMessage("|cff33ccffMentor|r: " .. expand(a.text or "")) end
		playSound(a.sound)
	elseif a.name == "track_quest" and a.quest then
		focusQuest(a.quest)
	elseif a.name == "speak" then UI.speak(a.text)
	elseif a.name == "tomtom_poi" then
		-- TomTom's own "closest quest objective" arrow follows the super-tracked quest and fights Guidelime's arrow; on/off here
		pcall(function()
			local tt = rawget(_G, "TomTom")
			if tt and tt.db and tt.db.profile then
				tt.db.profile.poi = tt.db.profile.poi or {}
				tt.db.profile.poi.setClosest = (a.on == true)
				if tt.ReloadOptions then tt:ReloadOptions() end
				-- ReloadOptions does not re-evaluate TomTom's modules: ask the quest-objectives module itself to start or stop, which
				-- also clears the arrow it set (TomTom shares its internals as TomTom.private)
				local pv = tt.private
				local mod = pv and pv.GetModule and pv.GetModule("QuestObjectives")
				if mod and pv.UpdateModule then pv.UpdateModule(mod) end
				-- disabling the module leaves its last arrow behind; its own setting-changed hook removes that waypoint
				if mod and a.on ~= true and mod.enabled then mod.enabled = false; pcall(mod.OnDisable, mod) end      -- still running: stop it by hand
				if mod and a.on ~= true and mod.OnSettingChanged then pcall(mod.OnSettingChanged, mod, "target") end
			end
		end)
	elseif a.name == "unfocus_quest" then
		pcall(function()
			local TU = QuestieLoader and QuestieLoader:ImportModule("TrackerUtils")
			if TU and TU.UnFocus then TU:UnFocus() end
			if C_SuperTrack and C_SuperTrack.SetSuperTrackedQuestID then C_SuperTrack.SetSuperTrackedQuestID(0) end
		end)
	elseif a.name == "guidelime" then
		local gc, gd = rawget(_G, "GuidelimeDataChar"), rawget(_G, "GuidelimeData")
		local c = a.cmd
		if c == "skip" or c == "complete" then
			if SlashCmdList and SlashCmdList.Guidelime then pcall(SlashCmdList.Guidelime, c) end
		elseif c == "toggle_window" then
			if Guidelime and Guidelime.toggleMainFrame then pcall(Guidelime.toggleMainFrame) end
		elseif c == "arrow_tomtom" or c == "arrow_builtin" then
			-- Guidelime reads this flag on every arrow update, so it takes effect at once (no /reload)
			if gd then gd.useTomTomArrow = (c == "arrow_tomtom") end
		elseif c == "arrow_show" or c == "arrow_hide" then
			if gc then gc.showArrow = (c == "arrow_show") end
		elseif c == "skip_to" and a.step and gc then
			-- skip the unresolved steps before a step number with Guidelime's own skip (current steps), stopping at the target
			local target = tonumber(a.step)
			local function hiBelow()
				local prog = gc.progress and gc.progress[gc.currentGuide]
				local hi = 0
				if type(prog) == "table" then
					for i, st in pairs(prog) do
						if type(i) == "number" and i < target and type(st) == "table" and (st.completed or st.skip) and i > hi then hi = i end
					end
				end
				return hi
			end
			local n = 0
			while SlashCmdList and SlashCmdList.Guidelime and n < 60 and hiBelow() < target - 1 do
				local before = hiBelow()
				pcall(SlashCmdList.Guidelime, "skip")
				n = n + 1
				if hiBelow() == before then break end
			end
			S.skipToResult = strfmt("skip_to %s: %d skip(s), steps up to %d resolved", tostring(target), n, hiBelow())
		elseif c == "open_guide" and gc and a.guide then
			local name = a.guide
			if not name:find("^TUGs ") then name = "TUGs The Ultimate (Leveling) Guides " .. name:gsub("^%a+/", "") end
			gc.currentGuide = name
			-- Guidelime's refresh (loadCurrentGuide + updateFromQuestLog) loads the new guide and ticks the steps the quest log proves done
			local okr, did = pcall(refreshLime)
			if okr and did then showToast({ text = "Guide switched to " .. name, level = "good", ttl = 6 })
			else showToast({ text = "Guide set to " .. name .. ": type /reload to load it.", level = "info", ttl = 12 }) end
		elseif c == "unskip" and a.step and gc then
			local prog = gc.progress and gc.progress[gc.currentGuide]
			local st = type(prog) == "table" and prog[tonumber(a.step)]
			if type(st) == "table" and st.skip then st.skip = nil; pcall(refreshLime) end
		elseif c == "refresh" then
			pcall(refreshLime)     -- re-load the current guide and tick what the quest log shows is done
		end
	end
end

---------------------------------------------------------------- unit marks (doc.marks): an icon over the units the guide is about
-- The agent lists the units a guide step is about (quest giver, turn-in NPC, kill target) by npc id and/or name. When one of them gets a
-- nameplate (comes into view) it gets an icon above it. Names and ids that the game hides from addons ("secret" values) are skipped,
-- never guessed. Friendly NPCs only get nameplates when the nameplateShowFriendlyNPCs setting is on, so it is switched on while marks
-- with a quest, turn-in or talk icon exist, and put back when they are gone.
local MARK_TEX = { quest = "Interface\\GossipFrame\\AvailableQuestIcon", turnin = "Interface\\GossipFrame\\ActiveQuestIcon", talk = "Interface\\GossipFrame\\ChatBubbleGossipIcon" }
local RAID = { star = 1, circle = 2, diamond = 3, triangle = 4, moon = 5, square = 6, cross = 7, skull = 8 }
S.markFrames, S.markUnits, S.markSeen = S.markFrames or {}, S.markUnits or {}, S.markSeen or {}

local function readable(v) return v ~= nil and not (issecretvalue and issecretvalue(v)) end

local function npcIdOf(unit)
	local ok, g = pcall(UnitGUID, unit)
	if not ok or type(g) ~= "string" or not readable(g) then return nil end
	local kind, id = g:match("^(%a+)%-[^-]*%-[^-]*%-[^-]*%-[^-]*%-(%d+)")
	if kind == "Creature" or kind == "Vehicle" then return tonumber(id) end
	return nil
end

local function matchMark(unit)
	if type(S.marks) ~= "table" or #S.marks == 0 then return nil end
	local okp, isPlayer = pcall(UnitIsPlayer, unit)
	if okp and readable(isPlayer) and isPlayer then return nil end
	local okd, dead = pcall(UnitIsDead, unit)
	if okd and readable(dead) and dead then return nil end
	local id = npcIdOf(unit)
	local okn, nm = pcall(UnitName, unit)
	nm = (okn and type(nm) == "string" and readable(nm)) and nm:lower() or nil
	for _, m in ipairs(S.marks) do
		if (m.npc and id and m.npc == id) or (m.name and nm and m.name:lower() == nm) then return m end
	end
	return nil
end

local function markFrameFor(plate)
	local f = S.markFrames[plate]
	if f then return f end
	local ok, fr = pcall(CreateFrame, "Frame", nil, plate)
	if ok and fr then S.markParent = "plate"
	else ok, fr = pcall(CreateFrame, "Frame", nil, UIParent); S.markParent = "UIParent (the nameplate refused a child frame)" end
	f = fr
	f:SetSize(28, 28)
	f:SetFrameStrata("HIGH")
	f.tex = f:CreateTexture(nil, "OVERLAY")
	f.tex:SetAllPoints()
	f.txt = f:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
	f.txt:SetPoint("BOTTOM", f, "TOP", 0, 1)
	S.markFrames[plate] = f
	return f
end

S.markDiag = S.markDiag or { scanned = 0, matched = 0, shown = 0 }
local function applyMarkInner(unit)
	if not (C_NamePlate and C_NamePlate.GetNamePlateForUnit) then S.markDiag.err = "no C_NamePlate.GetNamePlateForUnit"; return end
	local okp, plate = pcall(C_NamePlate.GetNamePlateForUnit, unit)
	S.markDiag.scanned = S.markDiag.scanned + 1
	if not okp or not plate then S.markDiag.err = "no nameplate frame for " .. tostring(unit); return end
	S.markUnits[unit] = plate
	local m = matchMark(unit)
	local f = S.markFrames[plate]
	if not m then if f then f:Hide() end; return end
	S.markDiag.matched = S.markDiag.matched + 1
	f = markFrameFor(plate)
	local tex = MARK_TEX[m.icon]
	if tex then
		f.tex:SetTexture(tex); f.tex:SetTexCoord(0, 1, 0, 1)
	else
		local i = RAID[m.icon] or 1
		f.tex:SetTexture("Interface\\TargetingFrame\\UI-RaidTargetingIcons")
		local col, row = (i - 1) % 4, floor((i - 1) / 4)
		f.tex:SetTexCoord(col * 0.25, (col + 1) * 0.25, row * 0.25, (row + 1) * 0.25)
	end
	f.txt:SetText(m.text or "")
	f:ClearAllPoints()
	local oka, erra = pcall(f.SetPoint, f, "BOTTOM", plate, "TOP", 0, 14)
	if not oka then S.markDiag.err = "anchor to nameplate failed: " .. tostring(erra):sub(1, 80); pcall(f.SetPoint, f, "CENTER", UIParent, "CENTER", 0, 0) end
	f:Show()
	S.markDiag.shown = S.markDiag.shown + 1
	S.markSeen[m.id] = (S.docNow or 0)
end

local function applyMark(unit)
	local ok, err = pcall(applyMarkInner, unit)
	if not ok then S.markDiag.err = tostring(err):sub(1, 120) end
end

local function refreshMarks()
	if not (C_NamePlate and C_NamePlate.GetNamePlates) then return end
	local okl, plates = pcall(C_NamePlate.GetNamePlates)
	if not okl or type(plates) ~= "table" then return end
	for _, pl in ipairs(plates) do
		local unit = pl.namePlateUnitToken or (pl.UnitFrame and pl.UnitFrame.unit)
		if unit then applyMark(unit) end
	end
end

local function markCvars(wantFriendly, wantEnemies)
	if not (GetCVar and SetCVar) then return end
	if truthy(call(UnitAffectingCombat, "player")) or truthy(call(InCombatLockdown)) then return end
	S.markCvarOrig = S.markCvarOrig or {}
	local function want(name, on)
		local cur = GetCVar(name)
		if on then
			if cur ~= "1" then if S.markCvarOrig[name] == nil then S.markCvarOrig[name] = cur end; pcall(SetCVar, name, "1") end
		elseif S.markCvarOrig[name] ~= nil then
			pcall(SetCVar, name, S.markCvarOrig[name]); S.markCvarOrig[name] = nil
		end
	end
	want("nameplateShowFriendlyNPCs", wantFriendly)
	want("nameplateShowEnemies", wantEnemies)
end

function UI.applyMarks(list)
	S.marks = type(list) == "table" and list or {}
	local friendly = false
	local enemies = false
	for _, m in ipairs(S.marks) do
		if m.icon == "quest" or m.icon == "turnin" or m.icon == "talk" then friendly = true else enemies = true end
	end
	pcall(markCvars, friendly, enemies)
	for _, f in pairs(S.markFrames) do f:Hide() end
	if #S.marks > 0 then
		if not S.markEv then
			S.markEv = CreateFrame("Frame")
			pcall(S.markEv.RegisterEvent, S.markEv, "NAME_PLATE_UNIT_ADDED")
			pcall(S.markEv.RegisterEvent, S.markEv, "NAME_PLATE_UNIT_REMOVED")
			S.markEv:SetScript("OnEvent", function(_, event, unit)
				if event == "NAME_PLATE_UNIT_ADDED" then pcall(applyMark, unit)
				else local pl = S.markUnits[unit]; local f = pl and S.markFrames[pl]; if f then f:Hide() end; S.markUnits[unit] = nil end
			end)
			if C_Timer and C_Timer.NewTicker then C_Timer.NewTicker(2, function() if type(S.marks) == "table" and #S.marks > 0 then pcall(refreshMarks) end end) end
		end
		pcall(refreshMarks)
	end
end

function UI.markLines()
	local L = {}
	local n = type(S.marks) == "table" and #S.marks or 0
	local vis = {}
	for plate, f in pairs(S.markFrames) do if f:IsShown() and f.txt then vis[#vis + 1] = (f.txt:GetText() or "?") end end
	L[#L + 1] = strfmt("marks loaded: %d | icons visible now: %d%s", n, #vis, #vis > 0 and (" (" .. tconcat(vis, ", ") .. ")") or "")
	local d = S.markDiag or {}
	L[#L + 1] = strfmt("marks debug: plates scanned=%d matched=%d shown=%d parent=%s%s", d.scanned or 0, d.matched or 0, d.shown or 0, tostring(S.markParent or "-"), d.err and (" | last error: " .. d.err) or "")
	for _, m in ipairs(S.marks or {}) do L[#L + 1] = strfmt("  %s %s%s%s", m.id, m.icon, m.name and (" " .. m.name) or "", m.npc and (" #" .. m.npc) or "") end
	return L
end

-- item ids the agent asked to have reported (ITEM_WATCH in WoWFeed.lua)
function UI.watchList() return S.watch or {} end

-- what Coach.lua needs from this file
UI.showToast = showToast
UI.runAction = runAction
UI.playSound = playSound
UI.expand = expand
UI.setWaypoint = setWaypoint
UI.clearWaypoint = clearWaypoint
UI.dismissWaypoint = dismissWaypoint
UI.toggleItem = toggleItem
function UI.panelVisible(id)
	local P = S.panels[id]
	if not P or not P.data or P.removed or S.hidden then return false end
	return (P.shown and P.alpha >= 0.2) and true or false
end

---------------------------------------------------------------- built-in character panel (no agent needed)
-- Professions and skills as bars, weapon/armour skills, empty gear slots, durability, talents, XP, gold. Written by the
-- addon from live game data and drawn by the same engine; the agent can hide it ({"op":"builtin","me":false}) and /wfui me toggles it.
local EQUIP = { { 1, "Head" }, { 2, "Neck" }, { 3, "Shoulder" }, { 5, "Chest" }, { 6, "Waist" }, { 7, "Legs" }, { 8, "Feet" }, { 9, "Wrist" }, { 10, "Hands" },
	{ 11, "Ring 1" }, { 12, "Ring 2" }, { 13, "Trinket 1" }, { 14, "Trinket 2" }, { 15, "Back" }, { 16, "Main hand" }, { 17, "Off hand" }, { 18, "Wand/ranged" } }

local function collectSkills()
	local profs, secondary, weapons, armor = {}, {}, {}, {}
	local n = (C_SkillInfo and C_SkillInfo.GetNumSkillLines and C_SkillInfo.GetNumSkillLines()) or 0
	local group, seen = "", {}
	for i = 1, n do
		local info = C_SkillInfo.GetSkillLineInfo(i)
		if type(info) == "table" then
			if info.isHeader then group = (info.name or ""):lower()
			elseif seen[group .. "/" .. tostring(info.name)] then -- the client sometimes lists a skill twice
			else
				seen[group .. "/" .. tostring(info.name)] = true
				local it = { name = info.name or "?", rank = info.rank or 0, max = info.maxRank or 0 }
				if group:find("secondary") then secondary[#secondary + 1] = it
				elseif group:find("profession") then profs[#profs + 1] = it
				elseif group:find("weapon") then weapons[#weapons + 1] = it
				elseif group:find("armor") then armor[#armor + 1] = it end
			end
		end
	end
	return profs, secondary, weapons, armor
end

local function buildMe()
	local profs, secondary, weapons, armor = collectSkills()
	local kids = {}
	local function add(c) kids[#kids + 1] = c end
	add({ type = "heading", text = "Professions", size = 10 })
	if #profs == 0 then add({ type = "text", text = "No primary profession yet", muted = true, size = 11 }) end
	for _, p in ipairs(profs) do add({ type = "progress", label = p.name, value = p.rank, max = max(p.max, 1), height = 6, size = 11 }) end
	for _, p in ipairs(secondary) do add({ type = "progress", label = p.name, value = p.rank, max = max(p.max, 1), height = 6, size = 11, fill = "#8fa3bf" }) end
	local function list(items)
		local out = {}
		for _, it in ipairs(items) do out[#out + 1] = (it.max > 1) and strfmt("%s %d/%d", it.name, it.rank, it.max) or it.name end
		return tconcat(out, ", ")
	end
	if #weapons > 0 then add({ type = "text", text = "Weapons: " .. list(weapons), muted = true, size = 11 }) end
	if #armor > 0 then add({ type = "text", text = "Armor: " .. list(armor), muted = true, size = 11 }) end
	add({ type = "divider" })
	local empty, cur, mx = {}, 0, 0
	for _, e in ipairs(EQUIP) do
		local id = GetInventoryItemID and GetInventoryItemID("player", e[1])
		if not id then empty[#empty + 1] = e[2] end
	end
	for slot = 1, 19 do local a, b = dura(slot); if a and b and b > 0 then cur = cur + a; mx = mx + b end end
	add({ type = "text", size = 11, color = (#empty > 0) and "#ffb020" or nil,
		text = (#empty > 0) and ("Empty slots: " .. tconcat(empty, ", ")) or "All gear slots filled" })
	if mx > 0 then add({ type = "text", size = 11, muted = true, text = strfmt("Durability %d%%", floor(cur / mx * 100 + 0.5)) }) end
	local okp, pts = pcall(function() return UnitCharacterPoints and UnitCharacterPoints("player") end)
	pts = plainNum(pts)
		if okp and pts and pts > 0 then add({ type = "text", size = 11, color = "#5bd37a", text = strfmt("%d unspent talent point%s", pts, pts == 1 and "" or "s") }) end
	add({ type = "text", size = 11, muted = true, text = "Gold {gold}   Bags {bags}" })
	return { id = "sys-me", title = "Character", anchor = "TOPRIGHT", x = -20, y = -330, w = 250, fontSize = 11, bgAlpha = 0.45,
		visibility = { combat = "dim", idleFade = 60, idleAlpha = 0.35 }, children = kids }
end

-- Built-in panels (no agent needed): { id, key (doc.builtin flag), hide (DB flag), build = function() -> panel data or nil }.
-- Coach.lua registers "sys-inbox". A builder that returns nil means "nothing to show right now".
local BUILTINS = { { id = "sys-me", key = "me", hide = "hideMe", build = buildMe } }
function UI.registerBuiltin(spec) BUILTINS[#BUILTINS + 1] = spec end

local function refreshOne(spec)
	local want = (S.builtin[spec.key] ~= false) and not DB[spec.hide]
	local P = S.panels[spec.id]
	local data
	if want then
		local okb, res = pcall(spec.build)
		if not okb then lerr(spec.id .. ": " .. tostring(res)); return end
		data = res
	end
	if not data then
		if P then P.removed = true end
		return
	end
	local sig = sigOf(data) .. sigOf(S.theme)
	if not P then P = newPanel(spec.id); P.builtin = true; S.panels[spec.id] = P end
	if P.removed or P.sig ~= sig then
		P.removed = false; P.sig = sig; P.data = data; P.t = resolveTheme(data); P.dirty = true; P.lastActivity = P.lastActivity or now()
		placePanel(P)
	end
end

local function refreshBuiltin()
	for _, spec in ipairs(BUILTINS) do
		local ok, err = pcall(refreshOne, spec)
		if not ok then lerr("builtin " .. spec.id .. ": " .. tostring(err)) end
	end
end
UI.refreshBuiltin = refreshBuiltin

---------------------------------------------------------------- applying a document
local function applyDoc(doc)
	local first = S.boot
	S.rev = doc.rev; S.theme = doc.theme or {}; S.docTime = now(); S.lastApply = now(); S.docs = S.docs + 1
	local nowEpoch = doc.now or (time and time() or 0)
	local seen, order = {}, {}
	for _, pd in ipairs(doc.panels or {}) do
		if type(pd) == "table" and pd.id then
			seen[pd.id] = true; order[#order + 1] = pd.id
			local P = S.panels[pd.id]
			if not P then
				-- frames are never freed in WoW, so a document that keeps inventing panel ids must not grow them without bound
				local count = 0
				for _ in pairs(S.panels) do count = count + 1 end
				if count >= PANEL_CAP then lerr("too many panels (" .. count .. "): ignoring " .. tostring(pd.id)) else P = newPanel(pd.id); S.panels[pd.id] = P end
			end
			if P then
			local sig = sigOf(pd) .. sigOf(S.theme)
			P.removed = false
			if P.sig ~= sig then
				for k in pairs(S.override) do if k:sub(1, #pd.id + 1) == pd.id .. "/" then S.override[k] = nil end end
				P.sig = sig; P.data = pd; P.t = resolveTheme(pd); P.dirty = true; P.lastActivity = now()
				P.expires = pd.ttl and (now() + pd.ttl) or nil
				syncVersions(pd); placePanel(P)
			end
			end
		end
	end
	for id, P in pairs(S.panels) do
		if not seen[id] and not P.builtin then P.removed = true; P.dirty = false end
	end
	S.builtin = doc.builtin or {}
	pcall(refreshBuiltin)
	S.order = order
	-- waypoints
	local wseen = {}
	S.wpOrder = {}
	for _, w in ipairs(doc.waypoints or {}) do wseen[w.id] = true; S.wpOrder[#S.wpOrder + 1] = w.id; setWaypoint(w) end
	for id, rec in pairs(S.wps) do if not wseen[id] and rec.owner ~= "rule" then clearWaypoint(id) end end
	for id in pairs(S.wpDismissed) do if not wseen[id] then S.wpDismissed[id] = nil; S.wpNote[id] = nil end end
	-- toasts and actions: only new ones, and on first load only fresh ones
	for _, t in ipairs(doc.toasts or {}) do
		if t.id > S.toastId then
			S.toastId = t.id
			if not first or (nowEpoch - (t.t or 0)) < 30 then
				local okt, shown = pcall(showToast, t)
				if okt and shown and WoWFeedCoach and WoWFeedCoach.onToastShown then pcall(WoWFeedCoach.onToastShown, t) end
			end
		end
	end
	for _, a in ipairs(doc.actions or {}) do
		if a.id > S.actionId then
			S.actionId = a.id
			if not first or (nowEpoch - (a.t or 0)) < 30 then pcall(runAction, a) end
		end
	end
	S.rules = type(doc.rules) == "table" and doc.rules or {}
	S.watch = type(doc.watch) == "table" and doc.watch or {}
	S.watchSpells = type(doc.watch_spells) == "table" and doc.watch_spells or {}
	S.verdicts = type(doc.verdicts) == "table" and doc.verdicts or {}
	do      -- unit marks: only touch the nameplates when the list actually changed
		local msig = (WoWFeedJSON and WoWFeedJSON.encode and WoWFeedJSON.encode(doc.marks or {})) or tostring(#(doc.marks or {}))
		if msig ~= S.marksSig then S.marksSig = msig; pcall(UI.applyMarks, doc.marks) end
	end
	S.voice = type(doc.builtin) == "table" and doc.builtin.voice == true
	if next(S.verdicts) ~= nil then pcall(hookTooltips) end
	if type(doc.guide_live) == "table" and #doc.guide_live > 0 then local okg, eg = pcall(UI.applyGuideLive, doc.guide_live); if not okg then lerr("guide live: " .. tostring(eg)) end end
	S.inbox = type(doc.inbox) == "table" and doc.inbox or {}
	S.docNow = nowEpoch
	local coach = WoWFeedCoach
	if coach and coach.onDoc then
		local okc, ec = pcall(coach.onDoc, doc, first, nowEpoch)
		if not okc then lerr("coach: " .. tostring(ec)) end
	end
	S.boot = false; S.dirtyState = true
end

function UI.apply(json)
	local doc, err = JSON.decode(json, 14, 120000)
	if type(doc) ~= "table" then lerr("decode: " .. tostring(err)); return false, err end
	if doc.v ~= 1 or type(doc.rev) ~= "number" then lerr("bad document header"); return false, "bad header" end
	if doc.rev <= S.rev and not S.boot then return false, "stale" end
	local ok, e = pcall(applyDoc, doc)
	if not ok then lerr("apply: " .. tostring(e)); return false, e end
	return true
end

---------------------------------------------------------------- inbound: slots and doorbell
local function sigPlayable(name)
	if not PlaySoundFile then return false end
	local ok, will = pcall(PlaySoundFile, strfmt(SIG_PATH, name), "Master")
	return ok and will and true or false
end

local function loadSlot(n)
	local name = strfmt(SLOT_FMT, n)
	_G.WoWFeedInbox = nil
	local loader = (C_AddOns and C_AddOns.LoadAddOn) or LoadAddOn
	local ok, loaded, reason = pcall(loader, name)
	if not ok then S.lastLoad = name .. ": " .. tostring(loaded):sub(1, 60); return false end
	if not loaded and (reason == "MISSING" or reason == "DISABLED" or reason == "INTERFACE_VERSION") then
		S.poolMax = min(S.poolMax, n - 1)       -- this slot (and later ones) are unknown to the client: restart WoW to discover more
		S.lastLoad = name .. ": " .. tostring(reason)
		return false
	end
	local inbox = _G.WoWFeedInbox
	_G.WoWFeedInbox = nil
	if type(inbox) ~= "table" or type(inbox.json) ~= "string" then
		S.lastLoad = name .. ": nothing (" .. tostring(reason) .. ")"
		return false
	end
	S.lastLoad = name .. ": rev " .. tostring(inbox.rev)
	return UI.apply(inbox.json)
end

local function loadNext()
	if S.slot >= S.poolMax then lerr("slot pool used up (" .. S.slot .. "): /reload refreshes slots, a restart discovers more"); return end
	S.slot = S.slot + 1
	if S.slot == floor(S.poolMax * 0.85) then
		pcall(showToast, { text = "Overlay inbox is 85% used. Type /reload when convenient to refill it.", level = "warn", ttl = 12 })
	end
	loadSlot(S.slot)
end

-- The bridge advertises the newest revision in WFSlot000.toc as "## X-Door: <rev>". If the client re-reads TOC metadata
-- at runtime that is a free doorbell (no slot is burned unless there is news). Until a change is observed we poll slowly.
local function metaDoor()
	local ok, v = pcall(function() return ((C_AddOns and C_AddOns.GetAddOnMetadata) or GetAddOnMetadata)("WFSlot000", "X-Door") end)
	return ok and tonumber(v) or nil
end

local function pollTick()
	local t = now()
	local m = metaDoor()
	if m ~= nil then
		if S.meta == nil then S.meta = m
		elseif m ~= S.meta then S.meta = m; S.metaChanges = S.metaChanges + 1; S.metaMode = "live" end
	end
	if S.metaMode == "live" then
		if m and m > S.rev and t - S.lastMetaLoad >= 2 then S.lastMetaLoad = t; loadNext() end
		return
	end
	if S.hidden or truthy(call(UnitAffectingCombat, "player")) then return end
	-- fallback: poll slots, quickly right after activity, slowly otherwise (each poll burns one slot)
	if t >= S.nextPoll then
		local hot = (t - S.lastApply) < 30
		S.nextPoll = t + (hot and 6 or 30)
		loadNext()
	end
end

local function bootInbound()
	-- silent-sound doorbell self-test (kept for diagnostics: on this client empty files report as playable, so it is off)
	local okOk, missing, zero = sigPlayable("ok"), sigPlayable("missing"), sigPlayable("zero")
	S.selftest = strfmt("valid=%s missing=%s zero=%s", tostring(okOk), tostring(missing), tostring(zero))
	S.doorMode = "meta"
	S.meta = metaDoor()
	-- boot slot: always the latest document, re-read on every UI load
	local loader = (C_AddOns and C_AddOns.LoadAddOn) or LoadAddOn
	_G.WoWFeedInbox = nil
	local ok = pcall(loader, "WFSlot000")
	local inbox = _G.WoWFeedInbox; _G.WoWFeedInbox = nil
	if ok and type(inbox) == "table" and type(inbox.json) == "string" then UI.apply(inbox.json); S.lastLoad = "boot: rev " .. tostring(inbox.rev) else S.lastLoad = "boot: nothing" end
end

---------------------------------------------------------------- feedback: kv + ui_state
-- Tokens for the SUMMARY "KV " line, in two groups: `crit` is what the bridge must always see (kept short, so it fits the
-- narrowest strip), `extra` is nice to have. WoWFeed.lua packs them; UI.kv() is the single-string form.
function UI.kvTokens()
	local chat = 0
	pcall(function() if ChatEdit_GetActiveWindow and ChatEdit_GetActiveWindow() then chat = 1 end end)
	local combat = truthy(call(UnitAffectingCombat, "player")) and 1 or 0
	local crit = { strfmt("uirev=%d", S.rev), strfmt("uiboot=%d", S.bootId), strfmt("chat=%d", chat), strfmt("combat=%d", combat), strfmt("glive=%d", S.glive or DB.glive or 0) }
	local extra = { strfmt("uislot=%d", S.slot), strfmt("uidoor=%d", S.door), "uidb=" .. tostring(S.doorMode), "uimeta=" .. tostring(S.metaMode) }
	return crit, extra
end
function UI.kv()
	local crit, extra = UI.kvTokens()
	return " " .. tconcat(crit, " ") .. " " .. tconcat(extra, " ")
end

local function stateLines()
	local L = {}
	L[#L + 1] = strfmt("rev=%d docs_applied=%d slot=%d/%d mode=%s meta=%s (%d changes) last_load=%s", S.rev, S.docs, S.slot, S.poolMax, S.metaMode, tostring(S.meta), S.metaChanges, S.lastLoad)
	L[#L + 1] = "doorbell selftest: " .. S.selftest
	L[#L + 1] = strfmt("guide live updates (no /reload): last applied edit %s%s", tostring(S.glive or DB.glive or 0), S.gliveErr and (" | PROBLEM: " .. S.gliveErr) or "")
	L[#L + 1] = strfmt("overlay: hidden=%s locked=%s edit=%s alpha=%.2f scale=%.2f", tostring(S.hidden), tostring(DB.locked), tostring(editMode or manualEdit), DB.alpha, DB.scale)
	for _, spec in ipairs(BUILTINS) do
		local bp = S.panels[spec.id]
		L[#L + 1] = strfmt("builtin %s panel: %s", spec.key == "me" and "character" or spec.key, (bp and bp.data and not bp.removed) and ("on, shown=" .. tostring(bp.shown and true or false)) or "off")
	end
	for _, id in ipairs(S.order) do
		local P = S.panels[id]
		if P and P.data then
			local u = DB.user[id]
			local pos = (u and u.pos) and strfmt("moved to %s %.0f,%.0f", u.pos[1], u.pos[3], u.pos[4]) or "agent position"
			local collapsed = (u and u.collapsed ~= nil) and u.collapsed or (P.data.collapsed == true)
			L[#L + 1] = strfmt("panel %s: shown=%s alpha=%.2f collapsed=%s %s", id, tostring(P.shown and true or false), P.alpha, tostring(collapsed), pos)
			for _, c in ipairs(P.data.children or {}) do
				if c.type == "checklist" then
					local parts = {}
					for _, it in ipairs(c.items or {}) do parts[#parts + 1] = it.id .. "=" .. (itemDone(id, it) and "done" or "open") end
					L[#L + 1] = "  checklist " .. id .. ": " .. tconcat(parts, " ")
				end
			end
		end
	end
	for key, tk in pairs(S.ticks) do L[#L + 1] = strfmt("tick seq=%d %s=%s", tk.seq, key, tk.val and "1" or "0") end
	for id, w in pairs(S.wps) do L[#L + 1] = strfmt("waypoint %s: set via %s", id, w.via) end
	for id, why in pairs(S.wpNote) do L[#L + 1] = strfmt("waypoint %s: cleared (%s)", id, why) end
	for _, e in ipairs(S.errors) do L[#L + 1] = "error: " .. e end
	return L
end

---------------------------------------------------------------- slash commands
local DEMO = {
	v = 1, rev = 0, now = 0, theme = {}, waypoints = {}, toasts = {}, actions = {},
	panels = { {
		id = "demo", title = "Mentor overlay", anchor = "TOPRIGHT", x = -24, y = -280, w = 300,
		children = {
			{ type = "text", text = "{class} in {zone}. This is a local demo.", muted = true },
			{ type = "progress", bind = "bags", label = "Bags" },
			{ type = "checklist", items = { { id = "a", text = "Click a box to tick it", priority = "high" }, { id = "b", text = "Drag the title after /wfui unlock", note = "positions are remembered" } } },
			{ type = "callout", level = "tip", text = "The overlay dims in combat and fades when idle." },
		},
	} },
}

SLASH_WFUI1 = "/wfui"
SlashCmdList.WFUI = function(msg)
	local cmd, arg = (msg or ""):match("^(%S*)%s*(.-)$")
	cmd = cmd:lower()
	local function say(t) print("|cff00ff00WFUI|r " .. t) end
	if cmd == "hide" then S.hidden = true; say("hidden")
	elseif cmd == "show" then S.hidden = false; say("shown")
	elseif cmd == "toggle" then S.hidden = not S.hidden; say(S.hidden and "hidden" or "shown")
	elseif cmd == "lock" then DB.locked = true; say("locked")
	elseif cmd == "unlock" then DB.locked = false; say("unlocked: drag a panel by its title, or hold Shift and drag anywhere on it")
	elseif cmd == "me" then DB.hideMe = not DB.hideMe; pcall(refreshBuiltin); say(DB.hideMe and "character panel hidden" or "character panel shown")
	elseif cmd == "inbox" then DB.hideInbox = not DB.hideInbox; pcall(refreshBuiltin); say(DB.hideInbox and "coach inbox hidden" or "coach inbox shown")
	elseif cmd == "voice" then local m = arg:lower(); if m == "on" or m == "off" then DB.voiceMode = m elseif m == "auto" then DB.voiceMode = nil end; say("spoken advice: " .. (DB.voiceMode or (S.voice and "on (the mentor turned it on)" or "off")) .. " (/wfui voice on|off|auto)")
	elseif cmd == "edit" then manualEdit = not manualEdit; say(manualEdit and "edit mode on: drag any panel, /wfui edit again to finish" or "edit mode off")
	elseif cmd == "alpha" then DB.alpha = max(0.1, min(1, tonumber(arg) or 1)); say("alpha " .. DB.alpha)
	elseif cmd == "scale" then DB.scale = max(0.5, min(2, tonumber(arg) or 1)); for _, P in pairs(S.panels) do if P.data then placePanel(P) end end; say("scale " .. DB.scale)
	elseif cmd == "reset" then DB.user = {}; for _, P in pairs(S.panels) do if P.data then P.dirty = true; placePanel(P) end end; say("positions, collapsed panels and ticks reset")
	elseif cmd == "demo" then S.boot = true; pcall(applyDoc, DEMO); say("demo shown; the mentor replaces it on the next update")
	elseif cmd == "dump" then for _, l in ipairs(stateLines()) do print(l) end
	else say("/wfui show|hide|toggle|lock|unlock|edit|me|inbox|voice on|off|auto|alpha <0.1-1>|scale <0.5-2>|reset|demo|dump. Drag by the title, or hold Shift and drag anywhere on a panel; Blizzard Edit Mode also unlocks them.") end
	S.dirtyState = true
end

---------------------------------------------------------------- boot
local clock = 0
local ticker = CreateFrame("Frame")
ticker:RegisterEvent("PLAYER_LOGIN")
ticker:SetScript("OnEvent", function()
	bindDB()
	C_Timer.After(2.5, function()
		local ok, err = pcall(bootInbound)
		if not ok then lerr("boot: " .. tostring(err)) end
		C_Timer.NewTicker(1, function()
			local ok2, e2 = pcall(pollTick)
			if not ok2 then lerr("poll: " .. tostring(e2)) end
			for id, P in pairs(S.panels) do
				if P.data and P.shown and not P.removed then
					for _, d in ipairs(P.dyn or {}) do
						pcall(function()
							if d.kind == "progress" then d.update()
							elseif d.kind == "text" then
								local txt = expand(d.src)
								if txt ~= d.last then d.last = txt; d.fs:SetText(txt); local h = d.fs:GetStringHeight(); if h and abs(h - d.h) > 1 then P.dirty = true end end
							elseif d.kind == "timer" then d.fs:SetText(d.text()) end
						end)
					end
					if P.expires and now() > P.expires then P.removed = true; S.dirtyState = true end
				end
			end
			clock = clock + 1
			if clock % 3 == 0 then pcall(refreshBuiltin) end
			if (S.dirtyState or clock % 10 == 0) and API.push then
				S.dirtyState = false
				pcall(API.push, "ui_state", stateLines(), 30)
			end
		end)
	end)
end)
