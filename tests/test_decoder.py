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


def dim_top_row(frame, oy, u, factor, black_rows=0):
    """What macOS 26's translucent title bar does to a windowed game: the strip's first row darker, its top line(s) black."""
    buf = bytearray(frame.raw)
    for y in range(oy, oy + CH * u):
        row = y * frame.stride
        for x in range(frame.width):
            o = row + x * 4
            if y < oy + black_rows:
                buf[o:o + 3] = b"\x00\x00\x00"
            else:
                for k in range(3):
                    buf[o + k] = int(buf[o + k] * factor + 7)
    return bridge.Frame(bytes(buf), frame.width, frame.height, frame.stride)


class DimmedStripTests(unittest.TestCase):
    """15 Nov 2026: in a windowed game under macOS 26 the title bar darkened the strip to ~67% (white read 171) and the bridge, which
    wanted a white anchor of 200+, decoded nothing for hours: the server thought he was logged out and the overlay went stale."""
    text = "summary\x1f7\x1f1\x1f1\x1fKV level=22 zone=Redridge Mountains"

    def tearDown(self):
        from wowmentor.bridge import strip
        strip.TH = 128

    def test_title_bar_dimmed_strip_decodes(self):
        for factor in (0.67, 0.55):
            frame = dim_top_row(make_frame(encode(3771, self.text.encode("utf-8")), u=1, oy=56), 56, 1, factor, black_rows=1)
            hint = bridge.find_hint(frame, *frame.size)
            self.assertIsNotNone(hint, factor)
            self.assertEqual(bridge.decode(frame, *frame.size, hint), (3771, self.text))

    def test_full_brightness_keeps_the_classic_threshold(self):
        from wowmentor.bridge import strip
        frame = make_frame(encode(9, self.text.encode("utf-8")), u=2, oy=60)
        hint = bridge.find_hint(frame, *frame.size)
        self.assertEqual(bridge.decode(frame, *frame.size, hint), (9, self.text))
        self.assertEqual(strip.TH, 127)

    def test_threshold_follows_the_anchor(self):
        from wowmentor.bridge import strip
        self.assertEqual(strip.threshold_for(255), 127)
        self.assertEqual(strip.threshold_for(171), 85)
        self.assertEqual(strip.threshold_for(100), 72)

    def test_coloured_pixel_is_not_an_anchor(self):
        self.assertFalse(bridge.white((255, 200, 60)))
        self.assertTrue(bridge.white((171, 171, 171)))
        self.assertFalse(bridge.white((110, 110, 110)))


class BlindWindowTests(unittest.TestCase):
    """A bridge that cannot see a running game's window exits, so launchd or the Windows supervisor starts a fresh one."""

    def run_frames(self, running, minutes=3):
        import types
        from wowmentor import host as hostmod
        cap = types.SimpleNamespace(find_game=lambda: None, game_running=lambda: running)
        orig = hostmod.get
        hostmod.get = lambda: types.SimpleNamespace(capture=cap)
        t = [1000.0]; exits = []

        class Done(Exception):
            pass

        def sleep(s):                       # the loop never yields without a window: stop it from inside once time is up
            t[0] += s
            if t[0] > 1000 + minutes * 60: raise Done()
        try:
            gen = bridge.frames(exit_fn=lambda: exits.append(t[0]), clock=lambda: t[0], sleep=sleep)
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                try: next(gen)
                except (StopIteration, Done): pass
        finally:
            hostmod.get = orig
        return exits

    def test_running_game_with_no_window_exits_after_the_grace_period(self):
        exits = self.run_frames(True)
        self.assertEqual(len(exits), 1)
        self.assertGreaterEqual(exits[0] - 1000, bridge.BLIND_EXIT_S)

    def test_closed_game_never_exits(self):
        self.assertEqual(self.run_frames(False), [])
