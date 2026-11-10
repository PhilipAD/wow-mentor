"""Shared test helpers: a real server on an ephemeral port, plus tiny HTTP and frame-drawing utilities."""
import json, os, shutil, sys, tempfile, threading, urllib.error, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowmentor.server import app as server_app


class RunningServer(object):
    def __init__(self, extra=None, data_path=None, config=None):
        """A real server on an ephemeral port. WOWMENTOR_HOME points into its temp dir, so nothing reads or writes the real config;
        `config` is written there as config.json (webhook settings, for example)."""
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-test-")
        self._home = os.environ.get("WOWMENTOR_HOME")
        os.environ["WOWMENTOR_HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["WOWMENTOR_HOME"])
        if config is not None:
            with open(os.path.join(os.environ["WOWMENTOR_HOME"], "config.json"), "w") as f:
                json.dump(config, f)
        self.cfg = {"read_token": "rt" * 12, "write_secret": "ws" * 12, "ui_secret": "us" * 12, "port": 0}
        self.cfg.update(extra or {})
        self.data_path = data_path or os.path.join(self.tmp, "data")
        self.srv = server_app.make_server(self.cfg, port=0, data_path=self.data_path)
        self.port = self.srv.server_address[1]
        self.cfg["port"] = self.port
        server_app.CFG["port"] = self.port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.port
        self.ui = "%s/ui/%s" % (self.base, self.cfg["ui_secret"])

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self._home is None:
            os.environ.pop("WOWMENTOR_HOME", None)
        else:
            os.environ["WOWMENTOR_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)


def http(url, body=None, method=None):
    data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jhttp(url, body=None):
    code, raw = http(url, body)
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw.decode("utf-8", "replace")


def raw_http(port, data, timeout=5, read_all=True):
    """Send raw bytes to the server and return everything it answers until it closes the connection (b'' if it just closes)."""
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        out = b""
        while read_all:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
            if b"\r\n\r\n" in out and out.split(b"\r\n\r\n", 1)[1]:
                break
        return out
    finally:
        s.close()


def status_of(raw):
    """HTTP status code of a raw response, or None if there was none."""
    try:
        return int(raw.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return None
