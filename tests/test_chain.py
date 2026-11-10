"""The whole instant-coaching chain with real components and nothing faked in between:

    synthetic pixel frames (the addon's codec, drawn by tests/test_decoder.py's encoder)
      -> bridge.tail() / find_hint / decode / handle / ingest_evj          (real bridge decode path)
      -> EventPipe -> POST events/push -> the real server module            (real HTTP on a loopback port)
      -> GET events (owner route and read-token route)
      -> WebhookSender -> http_post -> a local receiver                     (real HTTP, real HMAC)

Plus a performance guard and an equivalence check for the optimised frame accessors.
"""
import json, os, sys, threading, time, unittest
import http.server as httpsrv
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, http, jhttp
from test_decoder import encode, make_frame
from wowmentor.bridge import app as bridge, events as evmod, presence as presmod, webhook as whmod


class Receiver(object):
    """A local webhook endpoint that records every request."""

    def __init__(self, status=200):
        self.hits, outer = [], self

        class H(httpsrv.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.hits.append((self.path, {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(n)))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = httpsrv.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/hooks/wow" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def strip_message(sec, sid, part, parts, text):
    return ("%s\x1f%s\x1f%d\x1f%d\x1f%s" % (sec, sid, part, parts, text)).encode("utf-8")


class WholeChainTests(unittest.TestCase):
    def test_strip_frames_to_events_api_to_signed_webhook(self):
        server = RunningServer()
        recv = Receiver()
        secret = "chain-test-secret"
        offset = [0.0]
        clock = lambda: time.time() + offset[0]
        patches = [mock.patch.object(bridge, "presence", presmod.Presence()), mock.patch.object(bridge, "boots", bridge.BootTracker())]
        saved = (dict(bridge.sections), dict(bridge.partial), dict(bridge.meta), list(bridge.events))
        bridge.sections.clear(); bridge.partial.clear(); bridge.events.clear()
        bridge.meta["last_line"] = 0
        for k in bridge.evj_stats:
            bridge.evj_stats[k] = 0
        try:
            api = bridge.Api(server.ui)
            sender = whmod.WebhookSender(recv.url, secret, clock=clock, log=lambda m: None)         # real http_post to the real receiver
            pipe = evmod.EventPipe(post=api.post, on_stored=sender.enqueue, log=lambda m: None)
            patches += [mock.patch.object(bridge, "pipe", pipe), mock.patch.object(bridge, "sender", sender)]
            for p in patches:
                p.start()
            sender.session_fn = lambda: presmod.webhook_session(bridge.session_snapshot())

            now = int(time.time())
            death = {"seq": 1, "t": now, "type": "death", "data": {"zone": "Elwynn Forest", "subzone": "Fargodeep Mine", "attackers": 2,
                                                                    "killer": {"name": "Kobold Miner", "level": 6, "elite": False}, "deaths_in_subzone_10m": 2}}
            npc = {"seq": 2, "t": now, "type": "npc_seen", "data": {"name": "Innkeeper Farley", "id": 295, "map": 1429, "x": 43.1, "y": 65.7, "kind": "gossip"}}
            big = {"seq": 3, "t": now, "type": "quest_complete", "data": {"quest": 33, "title": "Kobold " + "Candles " * 40}}
            evj_one = json.dumps([death, npc], separators=(",", ":"))
            evj_big = json.dumps([big], separators=(",", ":"))
            half = len(evj_big) // 2
            kv = "KV level=5 xp=120 xpmax=900 rested=0 money_copper=229 honorable_kills=0 class=MAGE race=Human uiboot=4242 afk=0"
            messages = [
                (101, strip_message("summary", 7, 1, 1, kv + "\nLevel 5 Human Mage")),
                (102, strip_message("evj", 8, 1, 1, evj_one)),
                (103, strip_message("evj", 9, 2, 2, evj_big[half:])),          # a two-part event whose parts arrive in the wrong order
                (104, strip_message("evj", 9, 1, 2, evj_big[:half])),
                (105, strip_message("hb", 1, 1, 1, "12:00:00")),
            ]
            frames = [make_frame(encode(mid, payload), u=1, ox=0, oy=62, width=3456, height=220, seed=mid) for mid, payload in messages]
            frames.insert(2, frames[1])                        # the capture sees the same frame twice: it must not be processed twice
            frames.insert(1, make_frame([], u=1, width=3456, height=220))        # a frame without any strip
            with mock.patch.object(bridge, "frames", lambda: iter(frames)):
                bridge.tail()

            self.assertEqual(bridge.evj_stats["events"], 3)
            self.assertEqual(bridge.sections["summary"][1][0], kv)
            self.assertEqual(pipe.stats["queued"], 3, "the repeated frame was not queued twice")
            while pipe.step():
                pass

            # --- the server: owner route and read-only route agree and carry everything the addon said
            code, res = jhttp(server.ui + "/events?since=0")
            self.assertEqual(code, 200)
            self.assertEqual([e["type"] for e in res["events"]], ["death", "npc_seen", "quest_complete"])
            self.assertEqual([e["eid"] for e in res["events"]], [1, 2, 3])
            self.assertEqual(res["events"][0]["boot"], 4242)
            self.assertEqual(res["events"][0]["data"]["killer"]["name"], "Kobold Miner")
            self.assertTrue(res["events"][2]["data"]["title"].startswith("Kobold Candles"))
            self.assertEqual(res["next"], 3)
            code, ro = jhttp("%s/%s/events?since=1" % (server.base, server.cfg["read_token"]))
            self.assertEqual([e["eid"] for e in ro["events"]], [2, 3])
            self.assertEqual(jhttp(server.ui + "/npcs?name=farley")[1]["npcs"][0]["id"], 295)

            # --- the presence object and the feed, as the agent reads them
            snap, _ = bridge.presence_tick(time.time(), api, window_fn=lambda: 1, foreground_fn=lambda: True)
            self.assertEqual(snap["state"], "in_world")
            self.assertEqual(jhttp(server.ui + "/session")[1]["state"], "in_world")
            feed = bridge.render()
            self.assertEqual(http("%s/push/%s" % (server.base, server.cfg["write_secret"]), feed.encode())[0], 200)
            code, text = http("%s/%s?t=1" % (server.base, server.cfg["read_token"]))
            text = text.decode()
            self.assertIn("state=in_world", text)
            self.assertRegex(text, r"#1 \d\d:\d\d:\d\d death ")

            # --- the webhook: urgent (death) means it goes out after URGENT_S, signed, carrying the stored envelopes
            self.assertFalse(sender.pump(), "nothing is due in the first second")
            offset[0] += whmod.URGENT_S + 1
            self.assertTrue(sender.pump())
            self.assertEqual(len(recv.hits), 1)
            path, headers, body = recv.hits[0]
            self.assertEqual(path, "/hooks/wow")
            self.assertTrue(whmod.verify(secret, body, headers["x-wowmentor-signature"]), "valid HMAC over the exact body")
            self.assertFalse(whmod.verify("wrong", body, headers["x-wowmentor-signature"]))
            self.assertEqual(headers["x-wowmentor-event-count"], "3")
            payload = json.loads(body)
            self.assertEqual(payload["v"], 1)
            self.assertEqual([e["eid"] for e in payload["events"]], [1, 2, 3])
            self.assertEqual(payload["events"][0]["data"]["subzone"], "Fargodeep Mine")
            self.assertEqual(payload["next"], 3)
            self.assertEqual(payload["session"]["state"], "in_world")
            self.assertEqual(sorted(payload["session"]), ["foreground", "last_login_at", "last_logout_at", "state", "uploader_alive_at"])
            self.assertEqual(sender.status()["sent_events"], 3)
            self.assertNotIn(secret, json.dumps(sender.status()))
        finally:
            for p in reversed(patches):
                try:
                    p.stop()
                except RuntimeError:
                    pass
            bridge.sections.clear(); bridge.sections.update(saved[0])
            bridge.partial.clear(); bridge.partial.update(saved[1])
            bridge.meta.update(saved[2])
            bridge.events.clear(); bridge.events.extend(saved[3])
            recv.close()
            server.close()

    def test_a_reload_in_the_middle_keeps_events_apart_and_in_order(self):
        server = RunningServer()
        patches = [mock.patch.object(bridge, "presence", presmod.Presence()), mock.patch.object(bridge, "boots", bridge.BootTracker())]
        saved = (dict(bridge.sections), dict(bridge.partial), dict(bridge.meta))
        bridge.sections.clear(); bridge.partial.clear()
        try:
            api = bridge.Api(server.ui)
            pipe = evmod.EventPipe(post=api.post, log=lambda m: None)
            patches.append(mock.patch.object(bridge, "pipe", pipe))
            for p in patches:
                p.start()
            summary = lambda boot: bridge.sections.__setitem__("summary", (time.time(), ["KV level=5 xp=1 xpmax=9 money_copper=1 honorable_kills=0 uiboot=%d" % boot]))
            summary(1000)
            bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "type": "coach_question", "data": {"id": "q1", "text": "first load?"}}, {"seq": 2, "type": "death", "data": {}}]))
            # /reload: the addon restarts its counter; the KV (a section sent every few seconds) still shows the old boot for a moment
            bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "type": "coach_question", "data": {"id": "q1", "text": "after reload?"}}]))
            summary(2000)
            bridge.handle("evj", "2", 1, 1, json.dumps([{"seq": 2, "type": "death", "data": {"zone": "B"}}]))
            while pipe.step():
                pass
            evs = jhttp(server.ui + "/events?since=0")[1]["events"]
            self.assertEqual([e["type"] for e in evs], ["coach_question", "death", "coach_question", "death"], "the second load's seq 1 and 2 were not swallowed as duplicates")
            self.assertEqual(len(set(e["boot"] for e in evs)), 3, "three distinct labels: first load, reload with the lagging KV, reload once the KV caught up")
            qs = jhttp(server.ui + "/coach/questions")[1]["questions"]
            self.assertEqual(sorted(q["text"] for q in qs), ["after reload?", "first load?"], "both questions stay open; the reload did not overwrite the first")
            ids = [q["id"] for q in qs]
            self.assertEqual(len(set(ids)), 2)
            # answering the second one by its full id closes exactly that question
            after = [q for q in qs if q["text"] == "after reload?"][0]["id"]
            code, r = jhttp(server.ui + "/ops", {"op": "coach_answer", "id": after, "text": "yes"})
            self.assertEqual((code, r.get("answered")), (200, [after]))
            self.assertEqual([q["text"] for q in jhttp(server.ui + "/coach/questions")[1]["questions"]], ["first load?"])
        finally:
            for p in reversed(patches):
                try:
                    p.stop()
                except RuntimeError:
                    pass
            bridge.sections.clear(); bridge.sections.update(saved[0])
            bridge.partial.clear(); bridge.partial.update(saved[1])
            bridge.meta.update(saved[2])
            server.close()


# ---------------------------------------------------------------------------------------------- decoding speed and equivalence
def ref_cellv(px, x, y):
    r, g, b = px[x, y][:3]
    return (4 if r >= 128 else 0) + (2 if g >= 128 else 0) + (1 if b >= 128 else 0)


def ref_decode(px, W, H, h):
    """The decoder as it was before optimisation (tuple per pixel), as an oracle."""
    acc = nb = 0; out = bytearray(); need = 6; c = 1
    while True:
        x, y = bridge.cell_xy(h, c); c += 1
        if x >= W: return None
        acc = (acc << 3) | ref_cellv(px, x, y); nb += 3
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


def ref_find_hint(px, W, H):
    for u in (1, 2, 3):
        for oy in range(0, min(H - bridge.CH * u - 1, bridge.CROP - 10)):
            for ox in range(0, 12):
                x, y = ox + (bridge.CW * u) // 2, oy + (bridge.CH * u) // 2
                if not bridge.white(px[x, y]): continue
                h = (u, ox, oy)
                if bridge.magic(px, W, H, h): return h
    return None


class FrameSpeedTests(unittest.TestCase):
    def frames(self):
        payload = ("summary\x1f7\x1f1\x1f1\x1fKV " + "level=6 xp=40 " * 100).encode()
        for u, n, ox, oy in ((1, 380, 0, 62), (2, 190, 2, 61), (3, 120, 5, 40)):
            yield u, make_frame(encode(777, payload[:n]), u=u, ox=ox, oy=oy, width=3456, height=220, seed=u)

    def test_optimised_accessors_give_the_same_results_as_the_plain_ones(self):
        for u, f in self.frames():
            W, H = f.size
            h = bridge.find_hint(f, W, H)
            self.assertEqual(h, ref_find_hint(f, W, H))
            self.assertIsNotNone(h)
            self.assertEqual(bridge.decode(f, W, H, h), ref_decode(f, W, H, h))
            self.assertEqual(bridge.decode(f, W, H, h)[0], 777)
            damaged = bytearray(f.raw)
            for k in range(0, 40):                              # flip pixels inside the data cells
                o = (h[2] + 1) * f.stride + (h[1] + (10 + k) * bridge.CW * u + 1) * 4
                damaged[o:o + 3] = bytes(255 - c for c in damaged[o:o + 3])
            g = bridge.Frame(bytes(damaged), W, H, f.stride)
            self.assertEqual(bridge.decode(g, W, H, h), ref_decode(g, W, H, h))
            empty = make_frame([], u=u, width=3456, height=220)
            self.assertEqual(bridge.find_hint(empty, W, H), ref_find_hint(empty, W, H))

    def test_a_cached_strip_costs_a_few_milliseconds_per_frame(self):
        u, f = next(iter(self.frames()))
        W, H = f.size
        hint = bridge.find_hint(f, W, H)
        for _ in range(3):
            bridge.decode(f, W, H, hint)
        n = 60
        t0 = time.perf_counter()
        for _ in range(n):
            self.assertTrue(bridge.magic(f, W, H, hint))
            self.assertIsNotNone(bridge.decode(f, W, H, hint))
            bridge.measure_bars(f, W, H, hint)
        per_frame_ms = (time.perf_counter() - t0) / n * 1000
        self.assertLess(per_frame_ms, 25, "cached strip: %.1f ms per frame (about 2 ms on an idle Mac; the bound only catches regressions)" % per_frame_ms)

    def test_searching_a_frame_with_no_strip_stays_cheap(self):
        f = make_frame([], u=1, width=3456, height=220)
        W, H = f.size
        n = 15
        t0 = time.perf_counter()
        for _ in range(n):
            self.assertIsNone(bridge.find_hint(f, W, H))
        self.assertLess((time.perf_counter() - t0) / n * 1000, 60)


if __name__ == "__main__":
    unittest.main()
