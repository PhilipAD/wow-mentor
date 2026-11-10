import json, os, sys, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowmentor.bridge import webhook as wh

SECRET = "s3cret-value-123"


class Receiver(object):
    """A local HTTP server that records every POST and answers with a scripted status."""

    def __init__(self, statuses=None, location=None):
        self.requests, self.statuses, self.location = [], list(statuses or []), location
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                code = outer.statuses.pop(0) if outer.statuses else 200
                self.send_response(code)
                if outer.location and 300 <= code < 400:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.url = "http://127.0.0.1:%d/hooks/abc-private-path?token=zzz" % self.port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def ev(eid, typ="zone_changed", t=1794300000, **data):
    return {"eid": eid, "t": t + eid, "type": typ, "data": data, "boot": 1, "received_at": t + eid}


class FakeNet(object):
    """Injected post(): records calls and answers from a script (an int status or an exception instance)."""

    def __init__(self, script=None):
        self.script, self.calls = list(script or []), []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        r = self.script.pop(0) if self.script else 200
        if isinstance(r, Exception):
            raise r
        return r


class Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def sender(net, clock=None, secret=None, log=None, session=None):
    clock = clock or Clock()
    return wh.WebhookSender("https://hooks.example.test/in/private-token", secret, session_fn=session or (lambda: {"state": "in_world"}),
                            clock=clock, post=net, log=log or (lambda m: None)), clock


class FormatTests(unittest.TestCase):
    def test_signature_and_body(self):
        body = wh.build_body([ev(1, "death", zone="Z")], {"state": "in_world", "foreground": True}, 1794300123.7)
        data = json.loads(body)
        self.assertEqual((data["v"], data["sent_at"], data["next"]), (1, 1794300123, 1))
        self.assertEqual(data["events"][0], {"eid": 1, "t": 1794300001, "type": "death", "data": {"zone": "Z"}})
        self.assertEqual(data["session"]["state"], "in_world")
        sig = wh.sign(SECRET, body)
        self.assertTrue(sig.startswith("sha256="))
        self.assertTrue(wh.verify(SECRET, body, sig))
        self.assertFalse(wh.verify(SECRET, body + b" ", sig))
        self.assertFalse(wh.verify("other", body, sig))
        self.assertFalse(wh.verify(SECRET, body, None))
        self.assertFalse(wh.verify(SECRET, body, "sha256=é"))
        import hashlib, hmac
        self.assertEqual(sig, "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest())

    def test_url_helpers(self):
        for ok in ("http://127.0.0.1:9/x", "https://example.com", "https://h.example.com:8443/a/b?c=d"):
            self.assertTrue(wh.valid_url(ok), ok)
        for bad in ("", None, "ftp://x.com/a", "file:///etc/passwd", "javascript:alert(1)", "https://", "https://exa mple.com", "http://" + "a" * 3000, 5):
            self.assertFalse(wh.valid_url(bad), bad)
        self.assertEqual(wh.mask_url("https://user:pw@hooks.example.com:8443/secret/path?token=1"), "https://hooks.example.com:8443/...")
        self.assertEqual(wh.mask_url("http://[::1]:80/x"), "http://[::1]:80/...")
        self.assertNotIn("secret", wh.mask_url("https://hooks.example.com/secret"))


class SchedulingTests(unittest.TestCase):
    def test_debounce_batches_everything_and_waits_up_to_twenty_seconds(self):
        net = FakeNet()
        s, clock = sender(net)
        s.enqueue([ev(1)])
        self.assertEqual(s.next_due(), 1020.0)
        clock.t = 1005
        s.enqueue([ev(2)])
        clock.t = 1010
        s.enqueue([ev(3, "level_up")])
        self.assertEqual(s.next_due(), 1020.0, "later events do not push the first one back")
        self.assertFalse(s.pump(1019.9))
        self.assertEqual(net.calls, [])
        self.assertTrue(s.pump(1020.0))
        self.assertEqual(len(net.calls), 1)
        sent = json.loads(net.calls[0]["body"])
        self.assertEqual([e["eid"] for e in sent["events"]], [1, 2, 3])
        self.assertEqual(sent["next"], 3)
        self.assertEqual(net.calls[0]["headers"][wh.HEADER_COUNT], "3")
        self.assertIsNone(s.next_due())
        self.assertFalse(s.pump(2000))

    def test_at_most_one_post_per_window_for_a_steady_trickle(self):
        net = FakeNet()
        s, clock = sender(net)
        t = 1000.0
        sends = []
        while t < 1100:
            clock.t = t
            s.enqueue([ev(int(t))])
            if s.pump(t):
                sends.append(t)
            t += 1
        self.assertTrue(all(b - a >= wh.DEBOUNCE_S for a, b in zip(sends, sends[1:])), sends)
        self.assertGreaterEqual(len(sends), 4)

    def test_urgent_types_shorten_the_wait_to_three_seconds(self):
        for typ in sorted(wh.URGENT_TYPES):
            net = FakeNet()
            s, clock = sender(net)
            s.enqueue([ev(1, typ)])
            self.assertEqual(s.next_due(), 1003.0, typ)
        net = FakeNet()
        s, clock = sender(net)
        s.enqueue([ev(1)])
        clock.t = 1005
        s.enqueue([ev(2, "death")])
        self.assertEqual(s.next_due(), 1008.0, "an urgent event pulls an already scheduled batch forward")
        self.assertFalse(s.pump(1007.9))
        self.assertTrue(s.pump(1008.0))
        self.assertEqual([e["eid"] for e in json.loads(net.calls[0]["body"])["events"]], [1, 2])

    def test_urgent_events_are_never_closer_than_three_seconds_apart(self):
        net = FakeNet()
        s, clock = sender(net)
        s.enqueue([ev(1, "death")])
        self.assertTrue(s.pump(1003))
        clock.t = 1003.5
        s.enqueue([ev(2, "death")])
        self.assertEqual(s.next_due(), 1006.5)
        clock.t = 1004
        s.enqueue([ev(3, "death")])
        self.assertEqual(s.next_due(), 1006.5, "a second urgent event does not move it earlier or later than the floor")

    def test_retry_backoff_5_20_60_then_every_60(self):
        net = FakeNet([500, OSError("boom http://x/secret"), 503, 502, 200])
        s, clock = sender(net)
        s.enqueue([ev(1, "death")])
        due = [1003]
        self.assertTrue(s.pump(1003))
        self.assertEqual(s.next_due(), 1008.0)                       # +5
        self.assertFalse(s.pump(1007.9))
        s.enqueue([ev(2)])                                           # new events join the retry, they do not jump the backoff
        self.assertEqual(s.next_due(), 1008.0)
        self.assertTrue(s.pump(1008))
        self.assertEqual(s.next_due(), 1028.0)                       # +20
        self.assertTrue(s.pump(1028))
        self.assertEqual(s.next_due(), 1088.0)                       # +60
        self.assertTrue(s.pump(1088))
        self.assertEqual(s.next_due(), 1148.0)                       # stays at 60
        self.assertTrue(s.pump(1148))
        self.assertIsNone(s.next_due())
        self.assertEqual(len(net.calls), 5)
        self.assertEqual([e["eid"] for e in json.loads(net.calls[-1]["body"])["events"]], [1, 2], "nothing was lost in between")
        st = s.status()
        self.assertEqual((st["queued"], st["failures"], st["last_status"], st["failing"], st["sent_events"]), (0, 4, 200, False, 2))
        self.assertEqual(st["last_ok_at"], 1148)

    def test_failure_is_recorded_without_leaking_url_or_secret(self):
        logs = []
        net = FakeNet([OSError("cannot reach https://hooks.example.test/in/private-token with %s" % SECRET), 404])
        s, clock = sender(net, secret=SECRET, log=logs.append)
        s.enqueue([ev(1, "death")])
        s.pump(1003)
        s.pump(1008)
        st = s.status()
        self.assertEqual(st["last_status"], 404)
        self.assertEqual(st["last_error"], "HTTP 404")
        blob = json.dumps(st) + " ".join(logs)
        for leak in (SECRET, "private-token", "/in/"):
            self.assertNotIn(leak, blob)
        self.assertIn("hooks.example.test", blob)
        self.assertEqual(st["host"], "https://hooks.example.test/...")
        s2, _ = sender(FakeNet([OSError("x")]), secret=SECRET)
        s2.enqueue([ev(1, "death")])
        s2.pump(1003)
        self.assertEqual(s2.status()["last_error"], "OSError")

    def test_signature_header_only_with_a_secret(self):
        net = FakeNet()
        s, clock = sender(net, secret=SECRET)
        s.enqueue([ev(1, "death")])
        s.pump(1003)
        h = net.calls[0]["headers"]
        self.assertTrue(wh.verify(SECRET, net.calls[0]["body"], h[wh.HEADER_SIG]))
        self.assertEqual(h["Content-Type"], "application/json")
        net2 = FakeNet()
        s2, _ = sender(net2)
        s2.enqueue([ev(1, "death")])
        s2.pump(1003)
        self.assertNotIn(wh.HEADER_SIG, net2.calls[0]["headers"])

    def test_queue_is_capped_at_500_newest_and_drained_in_batches(self):
        net = FakeNet()
        s, clock = sender(net)
        s.enqueue([ev(i) for i in range(1, 601)])
        st = s.status()
        self.assertEqual((st["queued"], st["dropped"]), (500, 100))
        t = 1020.0
        sizes = []
        while s.next_due() is not None:
            self.assertTrue(s.pump(max(t, s.next_due())))
            t = s.next_due() or t
            sizes.append(int(net.calls[-1]["headers"][wh.HEADER_COUNT]))
        self.assertEqual(sizes, [100] * 5)
        first = json.loads(net.calls[0]["body"])
        self.assertEqual(first["events"][0]["eid"], 101, "the oldest were the ones dropped")

    def test_configure_validates_and_resets(self):
        net = FakeNet([500])
        s, clock = sender(net)
        s.enqueue([ev(1, "death")])
        s.pump(1003)
        self.assertTrue(s.status()["failing"])
        s.configure("https://other.example.test/new", None)
        self.assertFalse(s.status()["failing"], "a new URL starts with a clean retry state")
        clock.t = 1010
        s.enqueue([ev(2, "death")])
        self.assertEqual(s.next_due(), 1013.0)
        s.configure("not a url", "x")
        self.assertFalse(s.status()["configured"])
        s.enqueue([ev(3)])
        self.assertEqual(s.status()["queued"], 0)
        self.assertIsNone(s.next_due())
        s.configure(None, None)
        self.assertEqual(s.status()["host"], None)

    def test_session_failure_does_not_break_a_delivery(self):
        def broken():
            raise RuntimeError("no session")
        net = FakeNet()
        s, clock = sender(net, session=broken)
        s.enqueue([ev(1, "death")])
        self.assertTrue(s.pump(1003))
        self.assertEqual(json.loads(net.calls[0]["body"])["session"], {})

    def test_enqueue_ignores_junk_and_never_raises(self):
        s, clock = sender(FakeNet())
        s.enqueue(None)
        s.enqueue([])
        s.enqueue(["x", 5, {"no": "type"}])
        self.assertEqual(s.status()["queued"], 0)


class NonBlockingTests(unittest.TestCase):
    def test_enqueue_returns_immediately_while_a_post_is_in_flight(self):
        gate, started = threading.Event(), threading.Event()

        def slow_post(url, body, headers, timeout):
            started.set()
            gate.wait(5)
            return 200
        s = wh.WebhookSender("https://hooks.example.test/x", None, clock=time.time, post=slow_post, log=lambda m: None)
        s.enqueue([ev(1, "death")])
        s.due = time.time() - 1
        t = threading.Thread(target=s.pump)
        t.start()
        self.assertTrue(started.wait(2))
        t0 = time.time()
        s.enqueue([ev(2, "death")])
        self.assertLess(time.time() - t0, 0.5)
        gate.set()
        t.join(5)
        self.assertEqual([e["eid"] for e in s.q], [2], "only the delivered batch left the queue")


class RealHttpTests(unittest.TestCase):
    def test_delivery_over_http_with_signature(self):
        r = Receiver()
        try:
            s = wh.WebhookSender(r.url, SECRET, session_fn=lambda: {"state": "afk", "foreground": False}, log=lambda m: None)
            s.enqueue([ev(7, "death", zone="Elwynn Forest")])
            s.due = time.time() - 1
            self.assertTrue(s.pump())
            self.assertEqual(len(r.requests), 1)
            req = r.requests[0]
            self.assertEqual(req["path"], "/hooks/abc-private-path?token=zzz")
            self.assertTrue(wh.verify(SECRET, req["body"], req["headers"][wh.HEADER_SIG.lower()]))
            body = json.loads(req["body"])
            self.assertEqual(body["events"][0]["data"]["zone"], "Elwynn Forest")
            self.assertEqual(body["session"], {"state": "afk", "foreground": False})
            self.assertEqual(req["headers"][wh.HEADER_COUNT.lower()], "1")
            self.assertEqual(s.status()["last_status"], 200)
        finally:
            r.close()

    def test_error_statuses_and_unreachable(self):
        r = Receiver([500, 404])
        try:
            s = wh.WebhookSender(r.url, None, log=lambda m: None)
            s.enqueue([ev(1, "death")])
            s.due = 0
            s.pump(time.time())
            self.assertEqual((s.status()["last_status"], s.status()["failing"], s.status()["queued"]), (500, True, 1))
        finally:
            r.close()
        status, err = wh.send_once("http://127.0.0.1:1/nothing", None, [ev(1)], {}, 1.0, timeout=2)
        self.assertEqual((status, err), (None, "URLError"))

    def test_redirects_are_not_followed(self):
        target = Receiver()
        r = Receiver([302], location="http://127.0.0.1:%d/stolen" % target.port)
        try:
            status, err = wh.send_once(r.url, SECRET, [ev(1)], {}, 1.0)
            self.assertEqual((status, err), (302, None))
            self.assertEqual(target.requests, [], "the body and signature never reach the redirect target")
            s = wh.WebhookSender(r.url, SECRET, log=lambda m: None)
            r.statuses.append(302)
            s.enqueue([ev(1, "death")])
            s.pump(time.time() + 10)
            self.assertTrue(s.status()["failing"], "a redirect answer is not a successful delivery")
        finally:
            r.close()
            target.close()

    def test_send_once_reports_the_response_code(self):
        r = Receiver([204, 401])
        try:
            self.assertEqual(wh.send_once(r.url, SECRET, [ev(0, "webhook_test")], {"state": "unknown"}), (204, None))
            self.assertEqual(wh.send_once(r.url, None, [ev(0, "webhook_test")], {}), (401, None))
        finally:
            r.close()

    def test_background_thread_delivers_urgent_events(self):
        r = Receiver()
        old = wh.URGENT_S
        wh.URGENT_S = 0.2
        stop = threading.Event()
        try:
            s = wh.WebhookSender(r.url, None, log=lambda m: None)
            t = threading.Thread(target=s.run, args=(stop,), daemon=True)
            t.start()
            s.enqueue([ev(1, "death")])
            deadline = time.time() + 5
            while not r.requests and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(r.requests), 1)
            stop.set()
            s.wake.set()
            t.join(3)
        finally:
            wh.URGENT_S = old
            r.close()


if __name__ == "__main__":
    unittest.main()
