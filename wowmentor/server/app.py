"""The WoW Mentor server: a tiny, dependency-free HTTP server.

It receives the text snapshot the bridge pushes, serves it back at a private URL, and exposes the overlay, guide
editing and memory APIs under /ui/<secret>/. Everything private is a long random path segment; the server binds to
127.0.0.1 only (use `wowmentor share` to reach it from another machine).
"""
import base64, collections, hashlib, hmac, json, os, re, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import config as cfgmod
from . import agentmem, events as eventsmod, tugapi, uidoc

# Filled in by serve(); module level so the request handler can reach them.
READ = WRITE = UI = ""
state = {"body": b"(no data yet)\n", "t": 0.0}
ui_lock = threading.Lock()
doc = uidoc.Doc()
game = {"t": 0.0}      # last report from the bridge: what the addon applied and what the player did
tug = mem = store = None
UI_FILE = ""
CFG = {}

_idem = collections.OrderedDict()      # opId -> (status, response, body digest): a retried request is answered, not re-applied
MAX_BODY = 300_000
MAX_PUSH_BODY = 2_600_000              # events/push: up to MAX_PUSH events of MAX_EVENT_BYTES each
MAX_FEED_BODY = 4_000_000
MAX_OPID = 100
MAX_OPS = uidoc.MAX_OPS                  # ops per request (more is an error, never silently cut)
MAX_JSON_DEPTH = 40                    # a request body nested deeper than this is refused (parser and validator recursion bombs)
SOCKET_TIMEOUT_S = 20                  # per socket operation: a client that stalls mid-request (slowloris) is dropped
MAX_CONNECTIONS = 64                   # simultaneous connections served; the rest are closed at once instead of spawning threads
_CL = re.compile(r"[0-9]{1,12}")       # Content-Length: ASCII digits only (no sign, spaces, underscores or unicode digits)

LANDING = b"WoW Mentor server is running.\nRun `wowmentor links` in a terminal to get your private links.\n"


def save_doc():
    """Persist the overlay document. A full disk or a vanished data dir must not fail the request that already changed memory."""
    try:
        cfgmod.write_json_atomic(UI_FILE, doc.to_dict())
        return True
    except OSError as e:
        print("ui_state: could not save (%s)" % type(e).__name__, file=sys.stderr, flush=True)
        return False


def _reject_constant(name):
    raise ValueError("%s is not valid JSON" % name)


def parse_json(raw):
    """json.loads for request bodies: NaN/Infinity are refused (they would make every later response invalid JSON) and so is
    nesting deeper than MAX_JSON_DEPTH. Raises ValueError."""
    try:
        obj = json.loads(raw, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("JSON is nested too deeply")
    if eventsmod.too_deep(obj, MAX_JSON_DEPTH):
        raise ValueError("JSON is nested deeper than %d levels" % MAX_JSON_DEPTH)
    return obj


def _eq(a, b):
    """Constant-time string compare that never raises (hmac.compare_digest rejects non-ASCII str)."""
    return hmac.compare_digest(str(a).encode("utf-8", "replace"), str(b).encode("utf-8", "replace"))


def _ops_list(body):
    ops = body.get("ops") if isinstance(body, dict) and "ops" in body else body
    if isinstance(ops, dict):
        ops = [ops]
    return ops if isinstance(ops, list) else None


def _translate_answers(body):
    """`coach_answer` is sugar: {"op":"coach_answer","id":<question id>,"text":..} becomes a plain `coach` op and the question
    is closed once the batch applied. Returns (body for uidoc, {op index: question id}, {op index: error message})."""
    ops = _ops_list(body)
    if ops is None or not any(isinstance(o, dict) and o.get("op") == "coach_answer" for o in ops):
        return body, {}, {}
    new, answers, bad = [], {}, {}
    for i, op in enumerate(ops):
        if not (isinstance(op, dict) and op.get("op") == "coach_answer"):
            new.append(op)
            continue
        ident = op.get("id")
        qid = store.find_open_question(ident) if isinstance(ident, (str, int)) and not isinstance(ident, bool) else None
        if qid is None or qid in answers.values():
            open_ids = [q["id"] for q in store.open_questions()]
            bad[i] = "coach_answer.id: no open question %r (open: %s)" % (str(ident)[:40], ", ".join(open_ids) or "none")
            new.append(op)                      # uidoc does not know this op, so it is rejected with the index kept in place
            continue
        c = {k: v for k, v in op.items() if k not in ("op", "id")}
        c["op"] = "coach"
        c["answer_to"] = str(qid)[:100]          # the addon closes that question in COACH_QUESTIONS when it shows the answer
        new.append(c)
        answers[i] = qid
    return (dict(body, ops=new) if isinstance(body, dict) and "ops" in body else new), answers, bad


def _overlay(ops, panel_exists=None):
    """Apply overlay ops on behalf of the guide editor (caller holds ui_lock)."""
    if panel_exists and not any(p["id"] == panel_exists for p in doc.panels):
        return False
    try:
        uidoc.apply_ops(doc, ops)
    except uidoc.OpError:
        return False
    save_doc()
    return True


def _bases():
    """(ui base URL, feed URL) as the agent should use them: the public tunnel if one is set, else localhost."""
    cfg = cfgmod.peek()
    cfg.update({k: CFG[k] for k in ("read_token", "ui_secret", "port") if k in CFG})
    return cfgmod.ui_base(cfg, public=True), cfgmod.feed_url(cfg, public=True) + "?t=<random>"


class Handler(BaseHTTPRequestHandler):
    server_version = "wowmentor"
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S          # StreamRequestHandler applies it to the socket: reads and writes cannot hang forever

    def log_message(self, *a):          # request paths contain secrets: never log them
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, (json.dumps(obj, indent=2) + "\n").encode(), "application/json; charset=utf-8")

    def _read_body(self, limit):
        """The request body (bytes), or None after answering with an error. Bounded: nothing larger than `limit` is read."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json(411, {"ok": False, "errors": ["send a Content-Length (chunked bodies are not supported)"]})
            return None
        cl = (self.headers.get("Content-Length") or "0").strip()
        n = int(cl) if _CL.fullmatch(cl) else -1
        if n < 0 or len(self.headers.get_all("Content-Length") or []) > 1:
            self.close_connection = True
            self._json(400, {"ok": False, "errors": ["bad Content-Length"]})
            return None
        if n > limit:
            self.close_connection = True          # the oversized body is not read, so the connection cannot be reused
            self._json(413, {"ok": False, "errors": ["body too large (limit %d bytes)" % limit]})
            return None
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:                          # the client hung up mid-body
            self.close_connection = True
            return None
        return raw

    def _run_ops(self, body):
        """Apply overlay ops. Optional: opId (body or query) makes a retry idempotent; partial (body or ?partial=1)
        applies the valid ops and lists the rejected ones with reasons. Caller holds ui_lock."""
        q = self._query()
        opid = body.get("opId") if isinstance(body, dict) else None
        if opid is None:
            opid = (q.get("opId") or [None])[0]
        if opid == "":
            opid = None
        if opid is not None and (not isinstance(opid, (str, int)) or isinstance(opid, bool) or len(str(opid)) > MAX_OPID):
            return self._json(400, {"ok": False, "errors": ["opId must be a string of at most %d characters" % MAX_OPID]})
        partial = bool(body.get("partial")) if isinstance(body, dict) else False
        partial = partial or (q.get("partial") or ["0"])[0] in ("1", "true")
        every = _ops_list(body)
        if every is not None and len(every) > MAX_OPS:
            return self._json(400, {"ok": False, "errors": ["at most %d ops per request (got %d); split the batch" % (MAX_OPS, len(every))], "rev": doc.rev, "rejected": []})
        digest = hashlib.sha256(json.dumps([body, partial], sort_keys=True, default=str).encode()).hexdigest()
        if opid is not None and str(opid) in _idem:
            code, resp, old = _idem[str(opid)]
            if old != digest:
                return self._json(409, {"ok": False, "errors": ["opId %r was already used for different ops; use a fresh opId" % str(opid)[:40]]})
            return self._json(code, dict(resp, replayed=True))
        body2, answers, bad = _translate_answers(body)
        if bad and not partial:
            return self._json(400, {"ok": False, "errors": ["op %d: %s" % (i, m) for i, m in sorted(bad.items())], "rev": doc.rev, "rejected": []})
        try:
            if partial:
                warns, rejected, n = uidoc.apply_ops_partial(doc, body2)
            else:
                warns, rejected, n = uidoc.apply_ops(doc, body2), [], None
        except uidoc.OpError as e:
            errs = list(bad.values()) if (bad and every is not None and len(bad) == len(every)) else (list(bad.values()) + e.errors if bad else e.errors)
            return self._json(400, {"ok": False, "errors": errs, "rev": doc.rev, "rejected": []})
        for r in rejected:
            if r["index"] in bad:
                r["errors"] = [bad[r["index"]]]
        saved = save_doc()
        failed = set(r["index"] for r in rejected)
        answered = []
        for i, qid in sorted(answers.items()):
            if i not in failed:
                ops = _ops_list(body) or []
                text = ops[i].get("text") if i < len(ops) and isinstance(ops[i], dict) else None
                if store.close_question(qid, text if isinstance(text, str) else None):
                    answered.append(qid)
        resp = {"ok": True, "rev": doc.rev, "applied_rev": doc.rev, "bytes": len(doc.wire()), "warnings": warns, "rejected": rejected}
        if n is not None:
            resp["applied"] = n
        if answered:
            resp["answered"] = answered
        if not saved:
            resp["warnings"] = list(warns) + ["the server could not write ui_state.json (disk full?); the change is live but will be lost on restart"]
        if opid is not None:
            _idem[str(opid)] = (200, resp, digest)
            while len(_idem) > 200:
                _idem.popitem(last=False)
        return self._json(200, resp)

    def _ui_route(self):
        parts = self.path.split("?")[0].split("/")
        if len(parts) >= 4 and parts[1] == "ui" and UI and _eq(parts[2], UI):
            return "/".join(parts[3:])
        return None

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    def _guarded(self, fn):
        """Run a route; an unexpected error answers 500 (no details: they could echo input) instead of killing the connection mid-air."""
        try:
            fn()
        except (socket.timeout, ConnectionError):
            self.close_connection = True
        except Exception as e:
            print("server: internal error in a request handler (%s)" % type(e).__name__, file=sys.stderr, flush=True)
            self.close_connection = True
            try:
                self._json(500, {"ok": False, "errors": ["internal error"]})
            except Exception:
                pass

    def _get(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._send(200, b"ok\n")
        if path == "/":
            return self._send(200, LANDING)
        route = self._ui_route()
        if route is not None:
            hit = store.handle("GET", route, self._query(), None)      # events, npcs, session, coach/questions (own lock)
            if hit is not None:
                return self._json(*hit)
            with ui_lock:
                if route.startswith("tug/"):
                    code, obj = tug.handle("GET", route, self._query(), None)
                    return self._json(code, obj)
                if route == "memory" or route.startswith("memory/"):
                    code, obj = mem.handle("GET", route, self._query(), None)
                    return self._json(code, obj)
                if route == "brief":
                    st = tug.handle("GET", "tug/status", {}, None)[1]
                    age = (time.time() - state["t"]) if state["t"] else None
                    return self._send(200, agentmem.build_brief(mem, doc, st, state["body"], _bases()[0], age, session=store.get_session(),
                                                                events=store.query(None, None, 10)["events"], questions=store.open_questions()).encode())
                if route == "doc":
                    return self._send(200, doc.wire().encode(), "application/json")
                if route == "state":
                    return self._json(200, {"doc": doc.to_dict(), "game": game, "game_age_s": round(time.time() - game["t"], 1) if game["t"] else None})
                if route == "schema":
                    return self._json(200, schema())
                if route == "guide":
                    return self._send(200, uidoc.guide().encode())
                if route == "cli":
                    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent", "wowui.py")
                    try:
                        with open(here, "rb") as f:
                            return self._send(200, f.read(), "text/x-python; charset=utf-8")
                    except OSError:
                        return self._send(404, b"cli not found\n")
                if route == "skill":
                    base, feed = _bases()
                    return self._send(200, uidoc.skill(base, feed, cfgmod.profile_text()).encode())
                if route == "ops":
                    # GET writes exist because many tunnels and proxies block POST bodies: ?b=<base64url(JSON)>
                    try:
                        raw = self._query()["b"][0]
                        body = parse_json(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                    except Exception as e:
                        return self._json(400, {"ok": False, "errors": ["GET /ops needs ?b=<base64url JSON>: %s" % type(e).__name__]})
                    return self._run_ops(body)
            return self._send(404, b"not found\n")
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "" and parts[2] == "events" and READ and _eq(parts[1], READ):
            # read-only twin of GET /ui/<secret>/events for agents that only hold the feed link; secrets are scrubbed
            return self._json(*store.read_events(self._query(), _secrets()))
        if READ and _eq(path, "/" + READ):
            age = time.time() - state["t"] if state["t"] else -1
            return self._send(200, ("# server_age_seconds: %.1f\n" % age).encode() + state["body"])
        self._send(404, b"not found\n")

    def _post(self):
        route = self._ui_route()
        if route is not None:
            raw = self._read_body(MAX_PUSH_BODY if route == "events/push" else MAX_BODY)
            if raw is None:
                return
            try:
                body = parse_json(raw or b"null")
            except ValueError as e:
                return self._json(400, {"ok": False, "errors": ["body is not valid JSON: %s" % e]})
            hit = store.handle("POST", route, self._query(), body)
            if hit is not None:
                return self._json(*hit)
            with ui_lock:
                if route.startswith("tug/"):
                    code, obj = tug.handle("POST", route, self._query(), body if isinstance(body, dict) else {})
                    return self._json(code, obj)
                if route.startswith("memory/"):
                    code, obj = mem.handle("POST", route, self._query(), body)
                    return self._json(code, obj)
                if route == "ops":
                    return self._run_ops(body)
                if route == "report":
                    if isinstance(body, dict):
                        game.clear()
                        game.update(body)
                        game["t"] = time.time()
                    return self._json(200, {"ok": True})
            return self._send(404, b"not found\n")
        if not (WRITE and _eq(self.path, "/push/" + WRITE)):
            return self._send(404, b"not found\n")
        raw = self._read_body(MAX_FEED_BODY)
        if raw is None:
            return
        state["body"] = raw
        state["t"] = time.time()
        self._send(200, b"ok\n")


def _secrets():
    """Everything a read-only caller must never see echoed back: the three tokens and the webhook URL and secret (read live, since
    `wowmentor webhook set` changes config.json while the server runs)."""
    live = cfgmod.peek()
    return (UI, WRITE, READ, CFG.get("webhook_secret"), CFG.get("webhook_url"), live.get("webhook_secret"), live.get("webhook_url"))


def schema():
    """GET schema: the uidoc catalog plus what this server adds on top (coach_answer, opId/partial, events API)."""
    c = uidoc.catalog()
    c["ops"]["coach_answer"] = {"id": "id of an open coach question (see `wowui questions`)", "text": "the answer", "chat": "default true",
                                "toast": "default false", "level": list(uidoc.LEVELS), "sound": list(uidoc.SOUNDS),
                                "note": "same as `coach`, and also closes the question"}
    c["request"] = {"body": "an op, a list of ops or {\"ops\": [...], \"opId\": \"<unique per request>\", \"partial\": true}",
                    "opId": "makes a retry idempotent: the same opId with the same ops is answered from memory, never applied twice",
                    "partial": "apply the valid ops and return rejected:[{index, op, errors}] instead of failing the whole batch",
                    "response": "{ok, rev, applied_rev, rejected, warnings, applied?, answered?, replayed?}"}
    c["events_api"] = {
        "events": "GET events?since=<eid>&types=a,b&limit=100 -> {events:[{eid,t,received_at,boot,type,data}], next, last_eid, oldest_eid, gap?, reset?}; no since = newest events",
        "read_only_events": "GET /<read-token>/events?since=<eid> (same shape; for agents that only hold the feed link)",
        "npcs": "GET npcs[?name=x] -> seen NPC cache {name,id,map,x,y,kind,count,last_seen}",
        "session": "GET session -> {state, foreground, last_login_at, last_logout_at, uploader_alive_at, ...}",
        "questions": "GET coach/questions[?all=1] -> open questions the player asked with /coach <text>",
    }
    return c


def configure(cfg, data_path=None):
    """Load secrets and persistent state. Returns nothing; call serve() or use make_server() afterwards."""
    global READ, WRITE, UI, doc, tug, mem, store, UI_FILE, CFG
    CFG = dict(cfg)
    READ, WRITE, UI = cfg["read_token"], cfg["write_secret"], cfg["ui_secret"]
    d = data_path or cfgmod.data_dir()
    os.makedirs(d, exist_ok=True)
    UI_FILE = os.path.join(d, "ui_state.json")
    doc = uidoc.Doc()
    saved = cfgmod.read_json(UI_FILE)          # a corrupt file is kept as ui_state.json.bak and the server starts with an empty overlay
    if saved is not None:
        try:
            doc = uidoc.Doc(saved)
            json.dumps(doc.wire())
        except Exception:
            cfgmod.set_aside(UI_FILE)
            doc = uidoc.Doc()
    tug = tugapi.TugAPI(os.path.join(d, "tug_state.json"), _overlay)
    mem = agentmem.AgentMemory(os.path.join(d, "agent_memory.json"))
    store = eventsmod.EventStore(os.path.join(d, "events.json"))
    _idem.clear()


class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on simultaneous connections: past MAX_CONNECTIONS a new connection is closed immediately, so a
    flood of idle sockets cannot grow the thread count without limit."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        ThreadingHTTPServer.__init__(self, *a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):       # the default prints a traceback with the client address; keep logs terse
        print("server: connection error (%s)" % type(sys.exc_info()[1]).__name__, file=sys.stderr, flush=True)


def make_server(cfg, host="127.0.0.1", port=None, data_path=None):
    configure(cfg, data_path)
    return BoundedServer((host, port if port is not None else int(cfg["port"])), Handler)


def serve(cfg):
    srv = make_server(cfg)
    print("WoW Mentor server listening on http://127.0.0.1:%d" % srv.server_address[1], flush=True)
    srv.serve_forever()
