"""Configuration and secrets, stored as plain JSON in ~/.config/wowmentor (override with $WOWMENTOR_HOME).

Standard library only, Python 3.9+. Secrets are random and live only in this file; nothing is sent anywhere until you
run `wowmentor share` (or deploy the server yourself).
"""
import json, os, secrets, sys

DEFAULTS = {
    "port": 8095,
    "wow_dir": None,             # e.g. /Applications/World of Warcraft/_forever_
    "public_url": None,          # set by `wowmentor share`, e.g. https://xyz.trycloudflare.com
    "include_chat": False,       # chat text from other players stays out of the feed unless you turn this on
    "read_token": None, "write_secret": None, "ui_secret": None,
    "server_url": None,          # optional: a server running elsewhere (http(s)://host[:port]); the bridge pushes there and no local server is installed
    "webhook_url": None,         # `wowmentor webhook set <url>`: the bridge POSTs batched game events here
    "webhook_secret": None,      # optional: signs each webhook body (X-WoWMentor-Signature: sha256=<hmac>)
}


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("WOWMENTOR_HOME", "~/.config/wowmentor")))


def path(*parts):
    return os.path.join(home(), *parts)


def data_dir():
    d = path("data")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def read_json(p):
    """Parsed JSON file, or None when it is missing, unreadable or corrupt. A corrupt file is moved to `<p>.bak` (never deleted)
    so the caller can start from defaults without destroying what the player might want to recover."""
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        set_aside(p)
        return None


def set_aside(p):
    """Keep a file the program cannot use as `<p>.bak` (replacing an older .bak) so it can start from defaults without destroying it."""
    try:
        os.replace(p, p + ".bak")
    except OSError:
        pass


def write_json_atomic(p, obj, mode=0o600, **dump_kw):
    """Write JSON via a private temp file (created with `mode`, never world readable), fsync, then rename. Raises OSError on
    failure after removing the temp file, so a full disk leaves the old file intact."""
    tmp = p + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, **dump_kw)
            if dump_kw.get("indent"):
                f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except (OSError, ValueError, TypeError, RecursionError) as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise e if isinstance(e, OSError) else OSError("could not serialize %s: %s" % (os.path.basename(p), type(e).__name__))


def log_dir():
    if os.environ.get("WOWMENTOR_LOGS"):
        d = os.environ["WOWMENTOR_LOGS"]
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Logs/WoWMentor")
    else:
        d = path("logs")
    os.makedirs(d, exist_ok=True)
    return d


def load():
    cfg = dict(DEFAULTS)
    d = read_json(path("config.json"))       # a corrupt file is kept as config.json.bak and defaults are used
    if isinstance(d, dict):
        cfg.update(d)
    return cfg


def peek(strict=False):
    """config.json as a dict (defaults filled in) without touching the disk: a long-running server reads it often and must never
    move a file the player is in the middle of editing aside the way load() does for a corrupt one. strict=True returns None
    instead of defaults when the file cannot be read, so a watcher can keep its current settings through a bad edit."""
    cfg = dict(DEFAULTS)
    try:
        with open(path("config.json"), "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not an object")
        cfg.update(d)
    except (OSError, ValueError, RecursionError):
        if strict:
            return None
    return cfg


def save(cfg):
    """Atomic and private: the temp file is created 0600 (it holds the secrets), so there is no moment it is readable by others."""
    os.makedirs(home(), mode=0o700, exist_ok=True)
    write_json_atomic(path("config.json"), cfg, 0o600, indent=2, sort_keys=True)


def ensure_secrets(cfg, rotate=False):
    for k, n in (("read_token", 24), ("write_secret", 24), ("ui_secret", 24)):
        if rotate or not cfg.get(k):
            cfg[k] = secrets.token_hex(n)
    return cfg


def local_url(cfg):
    return "http://127.0.0.1:%d" % int(cfg["port"])


def server_url(cfg):
    """The configured remote server root (no trailing slash), or None when the server runs on this Mac."""
    u = cfg.get("server_url")
    return u.strip().rstrip("/") if isinstance(u, str) and u.strip() else None


def server_root(cfg):
    """Where the bridge and the CLIs reach the server: server_url if set, else http://127.0.0.1:<port>."""
    return server_url(cfg) or local_url(cfg)


def valid_server_url(u):
    """http(s)://host[:port][/prefix] with no query, fragment or userinfo."""
    import urllib.parse
    if not isinstance(u, str) or not u or any(c.isspace() for c in u):
        return False
    try:
        p = urllib.parse.urlsplit(u)
        return p.scheme in ("http", "https") and bool(p.hostname) and not p.query and not p.fragment and "@" not in p.netloc
    except ValueError:
        return False


def ui_base(cfg, public=False):
    root = (cfg.get("public_url") or "").rstrip("/") if public else ""
    return "%s/ui/%s" % (root or server_root(cfg), cfg["ui_secret"])


def feed_url(cfg, public=False):
    root = (cfg.get("public_url") or "").rstrip("/") if public else ""
    return "%s/%s" % (root or server_root(cfg), cfg["read_token"])


PROFILE_TEMPLATE = """# About me and how I want to be mentored
# This file is read by your AI mentor (it is added to its instructions). Edit it in any text editor, save, and the
# mentor picks it up the next time it fetches its skill page. Delete the lines that do not apply.

Character: <name>, <race> <class>, <faction>, <realm / game version>.
Goal: <for example: level fast and safely, top PvP rank, make gold, complete every quest zone>.
Playstyle: <for example: damage focused, no kiting; solo; group; casual 1 hour a night>.

Professions: <for example: primary Engineering + Alchemy, secondary Cooking + First Aid. Buy materials, never gather.>
Spells and talents: <for example: Affliction while leveling, respec at 40>.
Gear: <for example: cloth only, fill empty slots first, a wand before another dagger>.

Guide editing rules (if you use Guidelime): <for example: keep the quest order, only change profession steps and reward picks>.
Things the mentor must never do or say: <for example: never tell me to learn gathering professions>.
"""


def profile_text():
    try:
        with open(path("profile.md")) as f:
            txt = f.read().strip()
        # drop comment lines that start with '#'
        return "\n".join(l for l in txt.splitlines() if not l.startswith("#")).strip()
    except OSError:
        return ""
