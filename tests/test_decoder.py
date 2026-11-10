import os, random, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowmentor.bridge import app as bridge

CW, CH, BARW, BARGAP = bridge.CW, bridge.CH, bridge.BARW, bridge.BARGAP


def fletcher(b):
    s1 = s2 = 0
    for x in b:
        s1 = (s1 + x) % 255
        s2 = (s2 + s1) % 255
    return s1, s2


def encode(msg_id, payload):
    body = [msg_id >> 8 & 255, msg_id & 255, len(payload) >> 8 & 255, len(payload) & 255] + list(payload)
    s1, s2 = fletcher(body)
    raw = [0xC7, 0x1A] + body + [s1, s2]
    bits = "".join("{:08b}".format(x) for x in raw)
    bits += "0" * (-len(bits) % 3)
    return [int(bits[i:i + 3], 2) for i in range(0, len(bits), 3)]


def make_frame(cells, u=1, ox=0, oy=63, width=None, height=140, bars=(0.6, 1.0, 0.0), seed=1):
    """Draw a strip exactly as the addon does onto a noisy 'game scene', return a bridge.Frame."""
    rnd = random.Random(seed)
    width = width or max(1800, ox + (len(cells) + 3) * CW * u, ox + 3 * (BARW + BARGAP) * u + 20)
    stride = width * 4
    buf = bytearray(stride * height)
    for y in range(height):          # noisy background so detection cannot rely on blank space
        for x in range(0, width, 7):
            c = rnd.randrange(0, 256)
            o = y * stride + x * 4
            buf[o:o + 4] = bytes((c, c // 2, c // 3, 255))

    def rect(x0, y0, w, h, rgb):
        r, g, b = rgb
        for y in range(y0, y0 + h):
            row = y * stride
            for x in range(x0, x0 + w):
                buf[row + x * 4: row + x * 4 + 4] = bytes((b, g, r, 255))

    rect(ox, oy, CW * u, CH * u, (255, 255, 255))                         # anchor
    for i, v in enumerate(cells):
        rect(ox + (i + 1) * CW * u, oy, CW * u, CH * u, (255 if v & 4 else 0, 255 if v & 2 else 0, 255 if v & 1 else 0))
    colors = ((255, 255, 255), (255, 255, 0), (255, 0, 0))
    for i, frac in enumerate(bars):                                       # status bars on row 2
        x0 = ox + i * (BARW + BARGAP) * u
        rect(x0, oy + CH * u, BARW * u, CH * u, (0, 0, 0))
        rect(x0, oy + CH * u, int(BARW * u * frac), CH * u, colors[i])
    return bridge.Frame(bytes(buf), width, height, stride)


class DecoderTests(unittest.TestCase):
    def test_round_trip_at_each_pixel_scale(self):
        text = "summary\x1f7\x1f1\x1f1\x1fKV level=6 xp=40 money_copper=229\nLevel 6 Human Warlock | café"
        for u in (1, 2, 3):
            frame = make_frame(encode(4242, text.encode("utf-8")), u=u, ox=2 if u == 2 else 0, oy=63 + u)
            hint = bridge.find_hint(frame, *frame.size)
            self.assertIsNotNone(hint, "strip found at scale %d" % u)
            self.assertEqual(hint[0], u)
            msg_id, got = bridge.decode(frame, frame.width, frame.height, hint)
            self.assertEqual((msg_id, got), (4242, text))

    def test_corruption_is_rejected_not_misread(self):
        cells = encode(1, b"hello world, this is a message")
        cells[20] ^= 5
        frame = make_frame(cells)
        hint = bridge.find_hint(frame, *frame.size)
        self.assertIsNotNone(hint)
        self.assertIsNone(bridge.decode(frame, frame.width, frame.height, hint))

    def test_no_strip_means_no_hint(self):
        frame = make_frame([], u=1)
        # an anchor alone has no valid header
        self.assertIsNone(bridge.find_hint(frame, *frame.size))

    def test_bars_are_measured(self):
        frame = make_frame(encode(2, b"x"), bars=(0.6, 1.0, 0.0), u=2, oy=64)
        hint = bridge.find_hint(frame, *frame.size)
        bridge.measure_bars(frame, frame.width, frame.height, hint)
        self.assertAlmostEqual(bridge.bars["health"], 0.6, delta=0.03)
        self.assertEqual(bridge.bars["power"], 1.0)
        self.assertEqual(bridge.bars["target"], 0.0)

    def test_chat_from_other_players_is_filtered_by_default(self):
        bridge.CFG.clear()
        lines = ["06:30 | chat:SAY | Bob: hi", "06:31 | chat:WHISPER | Eve: secret", "06:32 | chat:SYSTEM | : You are now AFK", "06:33 | loot: Linen Cloth"]
        self.assertEqual(bridge._filter_events(lines), lines[2:])
        bridge.CFG["include_chat"] = True
        self.assertEqual(bridge._filter_events(lines), lines)
        bridge.CFG.clear()


if __name__ == "__main__":
    unittest.main()
