"""Minimal parser for WoW SavedVariables (Lua table literals). Returns Python dicts/lists."""
import os, re
TOK = re.compile(r'\s*(?:(--[^\n]*)|("(?:\\.|[^"\\])*")|(-?\d+\.?\d*(?:[eE][-+]?\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(.))', re.S)
def _tokens(s):
    for m in TOK.finditer(s):
        if m.group(1): continue
        if m.group(2) is not None: yield ("s", bytes(m.group(2)[1:-1], "utf-8").decode("unicode_escape", "ignore").encode("latin-1", "ignore").decode("utf-8", "ignore") if "\\" in m.group(2) else m.group(2)[1:-1])
        elif m.group(3) is not None:
            t = m.group(3); yield ("n", float(t) if ("." in t or "e" in t.lower()) else int(t))
        elif m.group(4) is not None: yield ("w", m.group(4))
        elif m.group(5) is not None: yield ("p", m.group(5))
def parse_value(toks, i):
    t, v = toks[i]
    if t == "s" or t == "n": return v, i + 1
    if t == "w": return {"true": True, "false": False, "nil": None}.get(v, v), i + 1
    if t == "p" and v == "{":
        i += 1; out = {}; arr = 1
        while toks[i] != ("p", "}"):
            if toks[i] == ("p", ","): i += 1; continue
            if toks[i] == ("p", "["):
                k, i = parse_value(toks, i + 1); i += 1  # ]
                i += 1  # =
                val, i = parse_value(toks, i); out[k] = val
            elif toks[i][0] == "w" and toks[i + 1] == ("p", "="):
                k = toks[i][1]; i += 2; val, i = parse_value(toks, i); out[k] = val
            else:
                val, i = parse_value(toks, i); out[arr] = val; arr += 1
        return out, i + 1
    raise ValueError("unexpected %r" % (toks[i],))
MAX_BYTES = 24_000_000      # SavedVariables of other addons can be huge; refuse to tokenise more than this into memory
def parse_file(path):
    if os.path.getsize(path) > MAX_BYTES: raise ValueError("file is larger than %d bytes" % MAX_BYTES)
    with open(path, encoding="utf-8", errors="replace") as f: text = f.read()
    toks = list(_tokens(text))
    res, i = {}, 0
    while i < len(toks):
        if toks[i][0] == "w" and i + 1 < len(toks) and toks[i + 1] == ("p", "="):
            name = toks[i][1]; val, i = parse_value(toks, i + 2); res[name] = val
        else: i += 1
    return res
