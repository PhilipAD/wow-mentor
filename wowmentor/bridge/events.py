"""Moves events from the bridge to the server without ever blocking the screen-capture loop.

`EventPipe.add()` (called from the capture thread and the presence thread) only appends to a bounded in-memory queue. A worker
thread posts batches to the server (POST /ui/<secret>/events/push). The server assigns the global `eid`; the stored envelopes it
returns are handed to `on_stored` (the webhook sender, the EVENTS_TAIL ring) so everything downstream sees eids. If the server is
unreachable the batch stays queued and is retried with a growing delay; the queue keeps the newest CAP events.

Every event gets a source id `src` ("<process id>:<counter>") before it is queued; the server drops a second copy of the same src, so a
push that is retried after a lost reply never duplicates events. Separately, an event that arrives with its OWN `boot` and `seq` from the
addon is dropped when that pair was already queued, so the addon may repeat unacknowledged events. A boot that the bridge only guessed
from the KV line is never used for this (it can lag an addon reload and would swallow the first events of the new load).
"""
import collections, json, threading, time, uuid

CAP = 500
BATCH = 100
BACKOFF = (1, 2, 5, 10, 30)
TAIL = 50
SEEN = 4000
MAX_EVENTS_PER_MESSAGE = 100


def parse_evj(text):
    """Decode an `evj` strip payload (a JSON array of event objects). Returns a clean list; malformed input gives []."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return []
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        data = data["events"]
    if not isinstance(data, list):
        return []
    out = []
    for e in data[:MAX_EVENTS_PER_MESSAGE]:
        if isinstance(e, dict) and isinstance(e.get("type"), str) and e["type"]:
            out.append(e)
    return out


class EventPipe(object):
    def __init__(self, post=None, on_stored=None, clock=time.time, log=print):
        """post(route, obj) -> (http status, parsed json or None); raise or return a falsy status on network failure."""
        self.post, self.on_stored, self.clock, self.log = post, on_stored, clock, log
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = []
        self.confirmed = collections.deque(maxlen=TAIL)
        self.seen = collections.OrderedDict()
        self.fails = 0
        self.retry_at = 0.0
        self.run_id = uuid.uuid4().hex[:12]
        self.counter = 0
        self.stats = {"queued": 0, "pushed": 0, "dropped": 0, "rejected": 0, "failures": 0}

    def add(self, events, boot=None, received_at=None):
        """Queue events. `boot` is the default for events that do not carry their own. Never blocks, never raises."""
        now = self.clock()
        try:
            with self.lock:
                for e in events:
                    if not isinstance(e, dict) or not isinstance(e.get("type"), str):
                        continue
                    ev = dict(e)
                    b, s = ev.get("boot"), ev.get("seq")
                    if b is not None and isinstance(s, int) and not isinstance(s, bool):       # the addon's own boot: safe to dedupe on
                        if (b, s) in self.seen:
                            continue
                        self.seen[(b, s)] = True
                        while len(self.seen) > SEEN:
                            self.seen.popitem(last=False)
                    if ev.get("boot") is None and boot is not None:
                        ev["boot"] = boot
                    if ev.get("received_at") is None:
                        ev["received_at"] = received_at if received_at is not None else now
                    self.counter += 1
                    ev["src"] = "%s:%d" % (self.run_id, self.counter)
                    self.pending.append(ev)
                    self.stats["queued"] += 1
                if len(self.pending) > CAP:
                    self.stats["dropped"] += len(self.pending) - CAP
                    self.pending = self.pending[-CAP:]
        except Exception as e:
            self.log("events: could not queue (%s)" % type(e).__name__)
        self.wake.set()

    def _remove(self, batch):
        ids = set(id(e) for e in batch)          # by identity: add() may have trimmed the front of the queue meanwhile
        self.pending = [e for e in self.pending if id(e) not in ids]

    def recent(self, n=20):
        """Newest n events, confirmed ones (with eid) first, then those still waiting for the server (eid None)."""
        with self.lock:
            rows = list(self.confirmed) + [dict(e, eid=None) for e in self.pending]
        return rows[-n:]

    def step(self, now=None):
        """Push one batch if there is one and the backoff allows. Returns True if a batch was delivered (or discarded as invalid)."""
        now = self.clock() if now is None else now
        with self.lock:
            if self.retry_at > now + max(BACKOFF) + 1:
                self.retry_at = 0.0                  # the wall clock was set back: do not wait hours for the old retry time
            if not self.pending or now < self.retry_at or self.post is None:
                return False
            batch = self.pending[:BATCH]
        try:
            code, res = self.post("events/push", {"events": batch})
        except Exception as e:
            code, res = None, None
            err = type(e).__name__
        else:
            err = "HTTP %s" % code
        if code == 200 and isinstance(res, dict) and res.get("ok"):
            stored = [e for e in res.get("stored", []) if isinstance(e, dict)]
            rejected = res.get("rejected") or []
            with self.lock:
                self._remove(batch)
                self.confirmed.extend(stored)
                self.fails, self.retry_at = 0, 0.0
                self.stats["pushed"] += len(stored)
                self.stats["rejected"] += len(rejected)
            if rejected:
                self.log("events: server rejected %d event(s): %s" % (len(rejected), json.dumps(rejected[:2])[:200]))
            if self.on_stored and stored:
                try:
                    self.on_stored(stored)
                except Exception as e:
                    self.log("events: consumer failed (%s)" % type(e).__name__)
            return True
        if code in (400, 413, 422):
            # the server will never accept this batch (malformed or too large): drop it rather than block everything behind it.
            # Other failures (401/403/404 from a wrong secret or an older server, 5xx, timeouts) are retried: they are fixable.
            with self.lock:
                self._remove(batch)
                self.stats["dropped"] += len(batch)
            self.log("events: dropped %d event(s), server answered HTTP %s" % (len(batch), code))
            return True
        with self.lock:
            self.fails += 1
            self.stats["failures"] += 1
            self.retry_at = now + BACKOFF[min(self.fails - 1, len(BACKOFF) - 1)]
        if self.fails == 1 or self.fails % 20 == 0:
            self.log("events: server not reachable (%s), %d event(s) waiting" % (err, len(self.pending)))
        return False

    def run(self, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                while self.step():
                    pass
            except Exception as e:
                self.log("events: worker error (%s)" % type(e).__name__)
            with self.lock:
                wait = 5.0 if not self.pending else max(0.05, min(5.0, self.retry_at - self.clock()))
            self.wake.wait(wait)
            self.wake.clear()
