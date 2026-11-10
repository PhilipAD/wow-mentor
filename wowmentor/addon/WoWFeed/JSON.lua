-- Strict JSON decoder for data the bridge drops into the addon (data only: nothing is ever executed; depth and size are
-- bounded), and a JSON encoder for the events the addon sends back (see the end of this file).
-- decode returns value, or nil + error message.
WoWFeedJSON = {}

local strbyte, strsub, strfind, strchar = string.byte, string.sub, string.find, string.char
local tonumber, floor = tonumber, math.floor
local strfmt = string.format

local function utf8char(cp)
	if cp < 0x80 then return strchar(cp)
	elseif cp < 0x800 then return strchar(0xC0 + floor(cp / 64), 0x80 + cp % 64)
	elseif cp < 0x10000 then return strchar(0xE0 + floor(cp / 4096), 0x80 + floor(cp / 64) % 64, 0x80 + cp % 64)
	end
	return strchar(0xF0 + floor(cp / 262144), 0x80 + floor(cp / 4096) % 64, 0x80 + floor(cp / 64) % 64, 0x80 + cp % 64)
end

local ESC = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }

function WoWFeedJSON.decode(s, maxDepth, maxBytes)
	if type(s) ~= "string" then return nil, "not a string" end
	if #s > (maxBytes or 200000) then return nil, "too large" end
	maxDepth = maxDepth or 12
	local pos = 1
	local parseValue

	local function fail(msg) error({ msg = msg .. " at " .. pos }, 0) end

	local function skip()
		local _, e = strfind(s, "^[ \t\r\n]*", pos)
		pos = e + 1
	end

	local function parseString()
		pos = pos + 1 -- opening quote
		local parts, n = {}, 0
		while true do
			local e = strfind(s, '["\\]', pos)
			if not e then fail("unterminated string") end
			if e > pos then n = n + 1; parts[n] = strsub(s, pos, e - 1) end
			if strbyte(s, e) == 34 then pos = e + 1; break end
			local c = strsub(s, e + 1, e + 1)
			if c == "u" then
				local hex = strsub(s, e + 2, e + 5)
				if not strfind(hex, "^%x%x%x%x$") then fail("bad \\u escape") end
				local cp = tonumber(hex, 16)
				pos = e + 6
				if cp >= 0xD800 and cp < 0xDC00 and strsub(s, pos, pos + 1) == "\\u" then
					local lo = tonumber(strsub(s, pos + 2, pos + 5), 16)
					if lo and lo >= 0xDC00 and lo < 0xE000 then
						cp = 0x10000 + (cp - 0xD800) * 1024 + (lo - 0xDC00)
						pos = pos + 6
					end
				end
				n = n + 1; parts[n] = utf8char(cp)
			else
				local r = ESC[c]
				if not r then fail("bad escape") end
				n = n + 1; parts[n] = r
				pos = e + 2
			end
		end
		return table.concat(parts)
	end

	function parseValue(depth)
		if depth > maxDepth then fail("too deep") end
		skip()
		local c = strsub(s, pos, pos)
		if c == "{" then
			pos = pos + 1
			local obj = {}
			skip()
			if strsub(s, pos, pos) == "}" then pos = pos + 1; return obj end
			while true do
				skip()
				if strsub(s, pos, pos) ~= '"' then fail("expected string key") end
				local k = parseString()
				skip()
				if strsub(s, pos, pos) ~= ":" then fail("expected ':'") end
				pos = pos + 1
				obj[k] = parseValue(depth + 1)
				skip()
				local d = strsub(s, pos, pos)
				pos = pos + 1
				if d == "}" then return obj end
				if d ~= "," then fail("expected ',' or '}'") end
			end
		elseif c == "[" then
			pos = pos + 1
			local arr, n = {}, 0
			skip()
			if strsub(s, pos, pos) == "]" then pos = pos + 1; return arr end
			while true do
				n = n + 1
				arr[n] = parseValue(depth + 1)
				skip()
				local d = strsub(s, pos, pos)
				pos = pos + 1
				if d == "]" then return arr end
				if d ~= "," then fail("expected ',' or ']'") end
			end
		elseif c == '"' then
			return parseString()
		elseif c == "t" and strsub(s, pos, pos + 3) == "true" then pos = pos + 4; return true
		elseif c == "f" and strsub(s, pos, pos + 4) == "false" then pos = pos + 5; return false
		elseif c == "n" and strsub(s, pos, pos + 3) == "null" then pos = pos + 4; return nil
		end
		local _, e = strfind(s, "^-?%d+%.?%d*[eE]?[+-]?%d*", pos)
		if not e then fail("unexpected character") end
		local num = tonumber(strsub(s, pos, e))
		if not num then fail("bad number") end
		pos = e + 1
		return num
	end

	local ok, res = pcall(function()
		local v = parseValue(0)
		skip()
		if pos <= #s then fail("trailing data") end
		return v
	end)
	if ok then return res end
	return nil, type(res) == "table" and res.msg or tostring(res)
end

---------------------------------------------------------------- encoder
-- Strict JSON encoder for the events the addon sends to the bridge. Output is pure ASCII: every non-ASCII character is
-- written as a \uXXXX escape (surrogate pairs above U+FFFF), invalid UTF-8 becomes U+FFFD, control characters are escaped.
-- That keeps the text free of the strip's 0x1F field separator and lets a long message be cut at any byte.
-- Empty tables encode as {} unless marked with WoWFeedJSON.array(t). Object keys are sorted. nil/NaN/inf/functions become null.
local ARR = { __wowfeed_array = true }
function WoWFeedJSON.array(t) return setmetatable(t or {}, ARR) end

local SHORT = { [8] = "\\b", [9] = "\\t", [10] = "\\n", [12] = "\\f", [13] = "\\r", [34] = '\\"', [92] = "\\\\" }

local function encodeString(s)
	local out, n, i, len = {}, 0, 1, #s
	local function put(x) n = n + 1; out[n] = x end
	local function esc(cp)
		if cp >= 0x10000 then
			cp = cp - 0x10000
			put(strfmt("\\u%04x\\u%04x", 0xD800 + floor(cp / 1024), 0xDC00 + cp % 1024))
		else put(strfmt("\\u%04x", cp)) end
	end
	while i <= len do
		local b = strbyte(s, i)
		if b < 0x80 then
			local j = i
			-- copy a run of plain printable ASCII at once
			while j <= len do
				local c = strbyte(s, j)
				if c < 32 or c == 34 or c == 92 or c >= 0x7F then break end
				j = j + 1
			end
			if j > i then put(strsub(s, i, j - 1)); i = j
			else
				put(SHORT[b] or strfmt("\\u%04x", b)); i = i + 1
			end
		else
			local need, cp, min
			if b >= 0xC2 and b <= 0xDF then need, cp, min = 1, b - 0xC0, 0x80
			elseif b >= 0xE0 and b <= 0xEF then need, cp, min = 2, b - 0xE0, 0x800
			elseif b >= 0xF0 and b <= 0xF4 then need, cp, min = 3, b - 0xF0, 0x10000 end
			local ok = need ~= nil
			if ok then
				for k = 1, need do
					local c = strbyte(s, i + k)
					if not c or c < 0x80 or c > 0xBF then ok = false; break end
					cp = cp * 64 + (c - 0x80)
				end
			end
			if ok and (cp < min or cp > 0x10FFFF or (cp >= 0xD800 and cp < 0xE000)) then ok = false end
			if ok then esc(cp); i = i + need + 1 else esc(0xFFFD); i = i + 1 end
		end
	end
	return '"' .. table.concat(out) .. '"'
end
WoWFeedJSON.encodeString = encodeString

local function isSecret(v) return issecretvalue ~= nil and issecretvalue(v) end

local function encodeValue(v, depth, maxDepth)
	local t = type(v)
	if t ~= "nil" and isSecret(v) then return "null" end
	if t == "string" then
		return encodeString(v)
	elseif t == "number" then
		if v ~= v or v == math.huge or v == -math.huge then return "null" end
		if v == floor(v) and v > -1e15 and v < 1e15 then return strfmt("%d", v) end
		return strfmt("%.14g", v)
	elseif t == "boolean" then
		return v and "true" or "false"
	elseif t == "table" then
		if depth >= maxDepth then error("too deep", 0) end
		local n, cnt = #v, 0
		for _ in pairs(v) do cnt = cnt + 1 end
		local out = {}
		if getmetatable(v) == ARR or (n > 0 and cnt == n) then
			for i = 1, n do out[i] = encodeValue(v[i], depth + 1, maxDepth) end
			return "[" .. table.concat(out, ",") .. "]"
		end
		local keys = {}
		for k in pairs(v) do
			local kt = type(k)
			if kt == "string" or kt == "number" then keys[#keys + 1] = k end
		end
		table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
		for i, k in ipairs(keys) do
			local ks = type(k) == "string" and k or tostring(k)
			if not isSecret(ks) then out[#out + 1] = encodeString(ks) .. ":" .. encodeValue(v[k], depth + 1, maxDepth) end
		end
		return "{" .. table.concat(out, ",") .. "}"
	end
	return "null"
end

-- Returns the JSON text, or nil + error message (e.g. nesting deeper than maxDepth, default 10).
function WoWFeedJSON.encode(v, maxDepth)
	local ok, res = pcall(encodeValue, v, 0, maxDepth or 10)
	if ok then return res end
	return nil, tostring(res)
end
