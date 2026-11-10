"""Pure decoding of the data strip the in-game addon draws (no operating-system calls, standard library only).

`Frame` is a read-only view of captured BGRA pixels; `find_hint`, `magic`, `decode` and `measure_bars` rebuild the addon's message from it.
Every host's capture (wowmentor.host) hands back a Frame, so this module is the same on every OS.
The strip format is the wow-ai codec (MIT, chelinho139): 3 bits per cell, each of R/G/B fully on or off.
"""
import time

# Geometry (must match the addon): one strip unit = u captured pixels (u found at runtime).
# Row 1: white anchor cell, then data cells of CW x CH units. Row 2: three status bars BARW units wide.
CW, CH, BARW, BARGAP = 3, 2, 120, 6
CROP = 220   # rows of the captured window to look at (the strip sits at the very top of the game area)

class Frame(object):
    """Read-only view of captured BGRA pixels: frame[x, y] -> (r, g, b, a). No imaging library needed.

    The decoder uses `row()` and `cell()` instead of `frame[x, y]`: they read the bytes directly and never build tuples, which is what
    keeps a 3456-pixel-wide capture cheap enough to decode five times a second."""
    def __init__(self, raw, width, height, stride):
        self.raw, self.width, self.height, self.stride = raw, width, height, stride
        self.size = (width, height)

    def __getitem__(self, xy):
        x, y = xy
        o = y * self.stride + x * 4
        d = self.raw
        return (d[o + 2], d[o + 1], d[o], d[o + 3])

    def row(self, y):
        """The BGRA bytes of one pixel row (index x * 4 + 0/1/2 = b/g/r)."""
        o = y * self.stride
        return self.raw[o:o + self.width * 4]

    def cell(self, x, y):
        """The 3-bit cell value at (x, y): bit 2 = red on, bit 1 = green on, bit 0 = blue on (a channel >= 128 counts as on)."""
        o = y * self.stride + x * 4
        d = self.raw
        return (4 if d[o + 2] >= 128 else 0) + (2 if d[o + 1] >= 128 else 0) + (1 if d[o] >= 128 else 0)


def cellv(px, x, y):
    c = getattr(px, "cell", None)
    if c is not None: return c(x, y)
    r, g, b = px[x, y][:3]
    return (4 if r >= 128 else 0) + (2 if g >= 128 else 0) + (1 if b >= 128 else 0)

def white(p): return p[0] >= 200 and p[1] >= 200 and p[2] >= 200

def cell_xy(h, c):
    u, ox, oy = h
    return ox + c * CW * u + (CW * u) // 2, oy + (CH * u) // 2

def magic(px, W, H, h):
    u, ox, oy = h
    if oy + CH * u >= H or ox + CW * u * 8 >= W: return False
    x, y = cell_xy(h, 0)
    if not white(px[x, y]): return False
    acc = 0
    for c in range(1, 7):
        x, y = cell_xy(h, c); acc = (acc << 3) | cellv(px, x, y)
    return (acc >> 2) == 0xC71A

def decode(px, W, H, h):
    u, ox, oy = h
    step = CW * u
    x = ox + step + step // 2                       # centre of data cell 1
    y = oy + (CH * u) // 2
    row = px.row(y) if hasattr(px, "row") else None  # fast path: index the row's bytes directly
    acc = nb = 0; out = bytearray(); need = 6
    while True:
        if x >= W: return None
        if row is not None:
            o = x * 4
            v = (4 if row[o + 2] >= 128 else 0) + (2 if row[o + 1] >= 128 else 0) + (1 if row[o] >= 128 else 0)
        else:
            v = cellv(px, x, y)
        x += step
        acc = (acc << 3) | v; nb += 3
        while nb >= 8:
            out.append((acc >> (nb - 8)) & 255); nb -= 8; acc &= (1 << nb) - 1
            if len(out) == 2 and (out[0] != 0xC7 or out[1] != 0x1A): return None
            if len(out) == 6:
                need = 8 + out[4] * 256 + out[5]
                if need > 4000: return None
            if len(out) >= need: break
        if len(out) >= need: break
    ln = out[4] * 256 + out[5]; s1 = s2 = 0
    for k in range(2, 6 + ln):
        s1 = (s1 + out[k]) % 255; s2 = (s2 + s1) % 255
    if out[6 + ln] != s1 or out[7 + ln] != s2: return None
    return out[2] * 256 + out[3], bytes(out[6:6 + ln]).decode("utf-8", "replace")

def find_hint(px, W, H):
    """Look for the white anchor followed by the 0xC71A header near the top-left of the capture."""
    raw, stride = getattr(px, "raw", None), getattr(px, "stride", None)
    for u in (1, 2, 3):
        for oy in range(0, min(H - CH * u - 1, CROP - 10)):
            y = oy + (CH * u) // 2
            for ox in range(0, 12):
                x = ox + (CW * u) // 2
                if raw is not None:                       # fast path: look at the bytes, build no tuples
                    o = y * stride + x * 4
                    if raw[o] < 200 or raw[o + 1] < 200 or raw[o + 2] < 200: continue
                elif not white(px[x, y]): continue
                h = (u, ox, oy)
                if magic(px, W, H, h): return h
    return None

bars = {"t": 0, "health": None, "power": None, "target": None}
BAR_TESTS = (
    ("health", lambda p: min(p[:3]) >= 150),
    ("power", lambda p: p[0] >= 150 and p[1] >= 150 and p[2] < 150),
    ("target", lambda p: p[0] >= 150 and p[1] < 150 and p[2] < 150 and (p[0] - max(p[1], p[2])) > 80),
)
def measure_bars(px, W, H, h):
    u, ox, oy = h
    y = oy + CH * u + (CH * u) // 2
    if y >= H: return
    for i, (name, test) in enumerate(BAR_TESTS):
        x0 = ox + i * (BARW + BARGAP) * u; width = BARW * u
        if x0 + width >= W: return
        f = sum(1 for x in range(x0, x0 + width) if test(px[x, y])) / width
        bars[name] = 1.0 if f > 0.97 else (0.0 if f < 0.02 else f)
    bars["t"] = time.time()
