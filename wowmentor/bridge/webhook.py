"""Outgoing webhook: batches the game events and POSTs them to a URL the player configured.

`wowmentor webhook set <url> [--secret S]` stores webhook_url / webhook_secret in config.json. The bridge feeds every stored
event into a WebhookSender, which sends JSON (see docs/DESIGN-coach-events.md, "Webhook"):

  {"v":1,"sent_at":<epoch>,"session":{state,foreground,last_login_at,last_logout_at,uploader_alive_at},
   "events":[{"eid","t","type","data"}...],"next":<last eid in this batch>}

Headers: Content-Type: application/json, X-WoWMentor-Event-Count: <n>, and, when a secret is set,
X-WoWMentor-Signature: sha256=<hex HMAC-SHA256 of the exact request body>. For receivers that want a plain key instead (or as well), `--bearer KEY`
adds `Authorization: Bearer KEY` to every POST (`--auth "Scheme value"` sends any other Authorization value).

Timing: at most one POST per DEBOUNCE_S (20 s) carrying everything queued since the last one. The first event after a quiet
period is sent after at most DEBOUNCE_S; an urgent event (death, logout, ...) shortens the wait to URGENT_S (3 s), and no two POSTs are
ever closer than URGENT_S. A failed POST is retried after 5, 20, then every 60 seconds, and the events stay queued (the queue holds the
newest QUEUE_CAP events). Nothing here ever blocks the capture loop: enqueue() only appends under a lock, and the HTTP call runs on
the sender's own thread.

Safety: the secret, the Authorization value and the full URL (a webhook URL is often itself a secret) are never logged or printed; only scheme://host is.
Redirects are not followed, so the configured host cannot bounce the request (and the body) to another address.
"""
import hashlib, hmac, json, threading, time, urllib.error, urllib.parse, urllib.request

URGENT_TYPES = frozenset(["death", "logout", "game_closed", "bags_full", "durability_critical", "coach_question"])
DEBOUNCE_S = 20
URGENT_S = 3
RETRY_S = (5, 20, 60)
QUEUE_CAP = 500
BATCH_MAX = 100
TIMEOUT_S = 10
MAX_URL = 2048
HEADER_SIG = "X-WoWMentor-Signature"
HEADER_COUNT = "X-WoWMentor-Event-Count"


def valid_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in url):
        return False                       # whitespace, control characters (CR/LF would split a header) and non-ASCII are refused
    try:
        p = urllib.parse.urlsplit(url)
        p.port                             # raises ValueError for a malformed or out-of-range port
        return p.scheme in ("http", "https") and bool(p.hostname) and "@" not in p.netloc
    except ValueError:
        return False


def mask_url(url):
    """`https://host:port/...` : enough to recognise the endpoint, nothing that could be a secret (path, query, userinfo)."""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or "?"
        if ":" in host:
            host = "[%s]" % host
        port = ":%d" % p.port if p.port else ""
        return "%s://%s%s/..." % (p.scheme, host, port)
    except ValueError:
        return "(invalid url)"


def valid_auth(v):
    """An Authorization header value: printable ASCII only (CR/LF would split the header), no leading/trailing space."""
    return isinstance(v, str) and 0 < len(v) <= 1024 and v == v.strip() and all(32 <= ord(c) <= 126 for c in v)


def bearer(token):
    return "Bearer " + token


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(secret, body, header):
    """For receivers (and tests): constant-time check of an X-WoWMentor-Signature header."""
    return isinstance(header, str) and hmac.compare_digest(sign(secret, body).encode(), header.encode("utf-8", "replace"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_post(url, body, headers, timeout=TIMEOUT_S):
    """POST and return the HTTP status (3xx included, never followed). Network problems raise OSError."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as r:
            r.read(1024)
            return r.status
    except urllib.error.HTTPError as e:
        try:
            e.read(1024)
        finally:
            e.close()
        return e.code


def build_body(events, session, now):
    """The exact bytes that are sent (and signed)."""
    payload = {"v": 1, "sent_at": int(now), "session": session,
               "events": [{"eid": e.get("eid"), "t": e.get("t"), "type": e.get("type"), "data": e.get("data", {})} for e in events],
               "next": max([e.get("eid") or 0 for e in events] + [0])}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def send_once(url, secret, events, session, now=None, post=http_post, timeout=TIMEOUT_S, auth=None):
    """One synchronous delivery (used by `wowmentor webhook test`). Returns (status or None, error class name or None)."""
    body = build_body(events, session, time.time() if now is None else now)
    headers = {"Content-Type": "application/json", HEADER_COUNT: str(len(events)), "User-Agent": "wowmentor-webhook/1"}
    if secret:
        headers[HEADER_SIG] = sign(secret, body)
    if valid_auth(auth):
        headers["Authorization"] = auth
    try:
        return post(url, body, headers, timeout), None
    except Exception as e:                    # never include str(e): urllib messages can carry the URL
        return None, type(e).__name__


class WebhookSender(object):
    def __init__(self, url=None, secret=None, session_fn=None, clock=time.time, post=http_post, log=print, auth=None):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.clock, self.post, self.log = clock, post, log
        self.session_fn = session_fn or (lambda: {})
        self.url, self.secret, self.auth = None, None, None
        self.q = []
        self.due = None                 # earliest time the next POST may go out
        self.block_until = 0.0          # backoff after a failure
        self.last_send = 0.0
        self.fails = 0
        self.stats = {"sent_events": 0, "posts": 0, "failures": 0, "dropped": 0, "last_status": None, "last_error": None,
                      "last_ok_at": None, "last_attempt_at": None}
        self.configure(url, secret, auth)

    # ---------------------------------------------------------------- configuration and input
    def configure(self, url, secret, auth=None):
        """Change the target live (the bridge calls this when config.json changes). A different URL starts empty with a clean retry
        state: events queued for the old endpoint are dropped, not replayed to the new one."""
        url = url if valid_url(url) else None
        with self.lock:
            if url != self.url:
                self.fails, self.block_until = 0, 0.0
                self.q, self.due = [], None
            self.url, self.secret, self.auth = url, (secret or None), (auth if valid_auth(auth) else None)
        self.wake.set()

    def enqueue(self, events):
        """Queue stored events (dicts with eid, t, type, data). Never blocks beyond a short lock, never raises."""
        if not events:
            return
        now = self.clock()
        with self.lock:
            if not self.url:
                return
            self._unjump(now)
            urgent = False
            for e in events:
                if isinstance(e, dict) and isinstance(e.get("type"), str):
                    self.q.append(e)
                    urgent = urgent or e["type"] in URGENT_TYPES
            if len(self.q) > QUEUE_CAP:
                self.stats["dropped"] += len(self.q) - QUEUE_CAP
                self.q = self.q[-QUEUE_CAP:]
            wait = URGENT_S if urgent else DEBOUNCE_S
            floor = max(self.block_until, self.last_send + URGENT_S)
            want = max(now + wait, floor)
            self.due = want if self.due is None else max(min(self.due, want), floor)
        self.wake.set()

    # ---------------------------------------------------------------- sending
    def _unjump(self, now):
        """Caller holds the lock. If the wall clock was set back, a `due` hours in the future would stall delivery until it catches up:
        nothing is ever scheduled further ahead than the longest retry delay, so anything beyond that is pulled back."""
        horizon = now + max(RETRY_S) + DEBOUNCE_S
        if self.due is not None and self.due > horizon:
            self.due = now + URGENT_S
        if self.block_until > horizon:
            self.block_until = now
        if self.last_send > now:
            self.last_send = now

    def next_due(self):
        with self.lock:
            return self.due if (self.q and self.url) else None

    def pump(self, now=None):
        """Send if a POST is due. Returns True when a delivery was attempted. Safe to call from tests with an injected clock."""
        now = self.clock() if now is None else now
        with self.lock:
            self._unjump(now)
            if not (self.q and self.url) or self.due is None or now < self.due:
                return False
            batch = self.q[:BATCH_MAX]
            url, secret, auth = self.url, self.secret, self.auth
        try:
            session = self.session_fn() or {}
        except Exception:
            session = {}
        body = build_body(batch, session, now)
        headers = {"Content-Type": "application/json", HEADER_COUNT: str(len(batch)), "User-Agent": "wowmentor-webhook/1"}
        if secret:
            headers[HEADER_SIG] = sign(secret, body)
        if auth:
            headers["Authorization"] = auth
        status, err = None, None
        try:
            status = self.post(url, body, headers, TIMEOUT_S)
        except Exception as e:
            err = type(e).__name__
        ok = status is not None and 200 <= status < 300
        with self.lock:
            self.stats["posts"] += 1
            self.stats["last_status"], self.stats["last_error"] = status, (None if ok else (err or "HTTP %s" % status))
            self.stats["last_attempt_at"] = int(now)
            self.last_send = now
            if self.url != url:
                return True                     # reconfigured while the request was in flight: the new target starts fresh
            if ok:
                ids = set(id(e) for e in batch)
                self.q = [e for e in self.q if id(e) not in ids]
                self.stats["sent_events"] += len(batch)
                self.stats["last_ok_at"] = int(now)
                if self.fails:
                    self.log("webhook: delivery to %s works again" % mask_url(url))
                self.fails, self.block_until = 0, 0.0
                if self.q:
                    self.due = now + (URGENT_S if len(self.q) >= BATCH_MAX or any(e["type"] in URGENT_TYPES for e in self.q) else DEBOUNCE_S)
                else:
                    self.due = None
            else:
                self.fails += 1
                self.stats["failures"] += 1
                delay = RETRY_S[min(self.fails - 1, len(RETRY_S) - 1)]
                self.block_until = self.due = now + delay
                if self.fails == 1 or self.fails % 10 == 0:
                    self.log("webhook: delivery to %s failed (%s); retrying in %ds, %d event(s) queued" % (mask_url(url), self.stats["last_error"], delay, len(self.q)))
        return True

    def run(self, stop=None):
        """Thread body: sleep until the next POST is due (or something changes), then pump."""
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.pump()
            except Exception as e:
                self.log("webhook error: %s" % type(e).__name__)
            due = self.next_due()
            wait = 5.0 if due is None else max(0.05, min(5.0, due - self.clock()))
            self.wake.wait(wait)
            self.wake.clear()

    def status(self):
        """For `wowmentor webhook status` / doctor / the session object: never contains the URL path or the secret."""
        with self.lock:
            s = dict(self.stats)
            s.update(configured=bool(self.url), host=mask_url(self.url) if self.url else None, signed=bool(self.secret), authorized=bool(self.auth), queued=len(self.q),
                     failing=self.fails > 0, next_attempt_at=int(self.due) if (self.due and self.q) else None)
            return s
