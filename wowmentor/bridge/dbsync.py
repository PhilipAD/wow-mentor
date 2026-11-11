"""Keeps the server's game database (quests, NPCs, items, objects) in step with the QuestieDB data that ships with Forever.

Every minute it compares the source file's fingerprint with what the server last received. When they differ it rebuilds the sqlite index
(about 6 s, in this thread), gzips it and uploads it in parts. Read-only on the game folder. Quiet when the server is down.
"""
import base64, gzip, hashlib, json, os, time, urllib.error, urllib.request

from . import localnet
from ..data import build

PART = 120_000          # raw bytes per request (base64 makes it 160 KB; the server accepts 300 KB bodies)


class DbSync(object):
    def __init__(self, addons, local_url, secret, data_dir, log=print):
        self.addons, self.base, self.dir, self.log = addons, "%s/ui/%s/db" % (local_url, secret), data_dir, log
        self.sent_fp = None

    def _call(self, route, body=None, timeout=30):
        try:
            req = urllib.request.Request("%s/%s" % (self.base, route), json.dumps(body).encode() if body is not None else None,
                                         {"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
            with localnet.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return None
        except Exception:
            return None

    def once(self):
        if not os.path.exists(build.toc_path(self.addons)):
            return
        fp = build.fingerprint(self.addons)
        if fp == self.sent_fp:
            return
        st = self._call("status")
        if st is None:
            return                                   # server unreachable: try again next minute
        if st.get("present") and (st.get("meta") or {}).get("fingerprint") == fp:
            self.sent_fp = fp
            return
        os.makedirs(self.dir, exist_ok=True)
        out = os.path.join(self.dir, "forever.sqlite")
        info = build.build(self.addons, out)
        with open(out, "rb") as f:
            blob = gzip.compress(f.read(), 6)
        sha = hashlib.sha256(blob).hexdigest()
        n = (len(blob) + PART - 1) // PART
        self.log("game database: uploading %d KB (%d parts), built in %ss" % (len(blob) // 1024, n, info["seconds"]))
        for i in range(n):
            r = self._call("upload", {"sha": sha, "part": i, "of": n, "data": base64.b64encode(blob[i * PART:(i + 1) * PART]).decode()}, timeout=60)
            if not r or not r.get("ok"):
                self.log("game database upload failed at part %d: %s" % (i, (r or {}).get("errors")))
                return
        self.sent_fp = fp
        self.log("game database uploaded")

    def run(self):
        while True:
            try:
                self.once()
            except Exception as e:
                self.log("dbsync error: %r" % (e,))
            time.sleep(60)
