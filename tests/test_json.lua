dofile("wowmentor/addon/WoWFeed/JSON.lua")
local J = WoWFeedJSON
local function eq(a, b, msg) if a ~= b then error((msg or "") .. " expected " .. tostring(b) .. " got " .. tostring(a), 2) end end

local v = J.decode('{"a":1,"b":[1,2,{"c":"x\\u00e9\\n"}],"d":true,"e":null,"f":-1.5e2,"g":"q\\"uote\\\\"}')
eq(v.a, 1); eq(#v.b, 3); eq(v.b[3].c, "x\195\169\n"); eq(v.d, true); eq(v.e, nil); eq(v.f, -150); eq(v.g, 'q"uote\\')
eq(J.decode("[]")[1], nil); eq(type(J.decode("{}")), "table")
eq(J.decode('"\\ud83d\\ude00"'), "\240\159\152\128", "surrogate pair")
local bad = { '{"a":}', '[1,2', '{"a" 1}', '"abc', '{"a":1} x', 'nul', '{"a":"\\x"}', '', '[1,]' }
for _, s in ipairs(bad) do
	local r, e = J.decode(s)
	assert(r == nil and e, "should fail: " .. s)
end
local deep = string.rep("[", 30) .. string.rep("]", 30)
local r, e = J.decode(deep); assert(r == nil and e:find("deep"), "depth limit")
local r2, e2 = J.decode(string.rep(" ", 300000)); assert(r2 == nil and e2 == "too large")

-- encoder
local enc = J.encode
eq(enc({ b = 1, a = "x" }), '{"a":"x","b":1}', "keys are sorted")
eq(enc({ 1, 2, 3 }), "[1,2,3]"); eq(enc({}), "{}", "empty table is an object"); eq(enc(J.array({})), "[]", "marked empty table is an array")
eq(enc({ list = J.array({}) }), '{"list":[]}')
eq(enc("a\"b\\c\n\t\r\b\f"), '"a\\"b\\\\c\\n\\t\\r\\b\\f"', "short escapes")
eq(enc("\1\31\127"), '"\\u0001\\u001f\\u007f"', "control characters, including the strip's 0x1F separator, are escaped")
eq(enc("caf\195\169"), '"caf\\u00e9"', "non-ASCII becomes \\u escapes (the output is pure ASCII)")
eq(enc("\240\159\152\128"), '"\\ud83d\\ude00"', "astral characters become surrogate pairs")
eq(enc("a\255b"), '"a\\ufffdb"', "invalid UTF-8 byte")
eq(enc("\192\128"), '"\\ufffd\\ufffd"', "overlong encoding is invalid")
eq(enc("\237\160\128"), '"\\ufffd\\ufffd\\ufffd"', "encoded surrogate is invalid")
eq(enc("\195"), '"\\ufffd"', "truncated sequence")
eq(enc(5), "5"); eq(enc(5.0), "5"); eq(enc(-3), "-3"); eq(enc(1.5), "1.5"); eq(enc(0.1), "0.1"); eq(enc(1e20), "1e+20")
eq(enc(0 / 0), "null", "NaN"); eq(enc(math.huge), "null", "inf"); eq(enc(true), "true"); eq(enc(false), "false"); eq(enc(nil), "null")
eq(enc({ f = function() end, a = 1 }), '{"a":1,"f":null}', "functions encode as null")
eq(enc({ [1] = "a", [3] = "c" }), '{"1":"a","3":"c"}', "sparse arrays are objects")
local nested = { a = { b = { c = { d = 1 } } } }
eq(enc(nested), '{"a":{"b":{"c":{"d":1}}}}')
local r, err = enc(nested, 2); assert(r == nil and err:find("deep"), "depth limit")
local cyc = {}; cyc.self = cyc
r, err = enc(cyc); assert(r == nil and err:find("deep"), "cycles hit the depth limit instead of looping")
-- round trip through the decoder
local sample = { s = "q\"uote\\ \1 \195\169 \240\159\152\128", n = { 1, 2.5, -7 }, t = true, o = { x = { y = "z" } }, e = J.array({}) }
local back = J.decode(enc(sample))
eq(back.s, sample.s); eq(back.n[2], 2.5); eq(back.t, true); eq(back.o.x.y, "z"); eq(#back.e, 0)
-- output is ASCII only and never contains the strip separator
local allbytes = {}
for i = 0, 255 do allbytes[#allbytes + 1] = string.char(i) end
local out = enc(table.concat(allbytes))
assert(not out:find("[\128-\255]") and not out:find("\31") and not out:find("\n"), "encoded text is plain ASCII")
-- secret values (restricted game data) never leak into the output
_G.issecretvalue = function(v) return type(v) == "table" and v.__s == true end
eq(enc({ a = { __s = true }, b = 1 }), '{"a":null,"b":1}', "a secret value encodes as null")
_G.issecretvalue = nil
print("json ok")
