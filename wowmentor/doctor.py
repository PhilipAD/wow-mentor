"""`wowmentor doctor`: check the whole chain and say, in plain words, what to fix."""
import json, os, re, sys, time, urllib.request

from . import __version__, config as cfgmod, host as hostmod, install
from .bridge import localnet, webhook as whmod

OK, WARN, BAD = "ok", "warn", "bad"
ICON = {OK: "✅", WARN: "⚠️ ", BAD: "❌"}


def _get(url, timeout=3):
    try:
        with localnet.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, type(e).__name__          # never str(e): urllib messages can quote the URL, which holds a secret


def fetch_session(cfg):
    """GET /ui/<secret>/session from the server (local or server_url) as a dict, or None."""
    if not cfg.get("ui_secret"):
        return None
    code, body = _get("%s/session?t=%d" % (cfgmod.ui_base(cfg), time.time() * 1000))
    if code != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _ago(t, now):
    return "never" if not isinstance(t, (int, float)) else "%ds ago" % max(0, now - t)


def session_checks(cfg, now=None):
    """Session state and webhook health as doctor rows (status, title, fix). Needs only the server, so it runs on any system."""
    now = time.time() if now is None else now
    out = []
    add = lambda status, title, fix="": out.append((status, title, fix))
    url = cfg.get("webhook_url")
    s = fetch_session(cfg)
    if s is None:
        add(WARN, "Could not read the session state from the server", "Run: wowmentor restart")
    elif not s.get("known"):
        add(WARN, "The bridge has not reported the game session yet", "Run: wowmentor start (the bridge reports every few seconds), then check again")
    elif s.get("uploader_stale"):
        add(WARN, "The bridge stopped reporting (%s)" % _ago(s.get("uploader_alive_at"), now), "Run: wowmentor start")
    else:
        fg = {True: "WoW is in front", False: "another app is in front"}.get(s.get("foreground"), "front window unknown")
        st = s.get("state")
        title = "Game session: %s (%s)" % (st, fg)
        if st in ("logged_out", "game_closed"):
            add(WARN, title, "Start WoW and log in to a character" if st == "game_closed" else "Log in to a character")
        else:
            add(OK, title)
    if url:
        if not whmod.valid_url(url):
            add(BAD, "Webhook URL in config.json is not a valid http(s) URL", "Run: wowmentor webhook set <url>")
        else:
            w = (s or {}).get("webhook") or {}
            name = "Webhook %s%s" % (whmod.mask_url(url), "" if cfg.get("webhook_secret") else " (not signed)")
            if w.get("failing"):
                add(WARN, "%s is failing (%s), %s event(s) queued" % (name, w.get("last_error"), w.get("queued", 0)), "Run: wowmentor webhook test")
            elif w.get("last_ok_at"):
                add(OK, "%s delivers (last success %s)" % (name, _ago(w["last_ok_at"], now)))
            elif w.get("configured") is False:
                add(WARN, "%s is not active in the running bridge yet" % name, "Wait a few seconds, or run: wowmentor restart")
            else:
                add(OK, "%s configured, nothing delivered yet" % name)
    return out


def checks(cfg):
    out = []
    add = lambda status, title, fix="": out.append((status, title, fix))

    h = hostmod.get()
    ok, why = h.capture.available()
    if not ok:
        add(BAD, h.text["not_supported_title"], why)
        return out
    add(OK if sys.version_info >= (3, 9) else BAD, "Python %d.%d" % sys.version_info[:2], h.text["python_fix"])

    if not cfg.get("ui_secret"):
        add(BAD, "Not set up yet", "Run: wowmentor setup")
        return out
    add(OK, "Config found (%s)" % cfgmod.home())

    wow = cfg.get("wow_dir")
    if not wow or not os.path.isdir(install.addons_dir(wow)):
        add(BAD, "WoW folder not found (%s)" % wow, 'Run: wowmentor setup --wow-dir "%s"' % h.text["example_wow_path"])
        return out
    add(OK, "WoW folder: %s" % wow)

    ver = install.addon_installed_version(wow)
    pkg_ver = None
    try:
        with open(os.path.join(install.PKG, "addon", install.ADDON, install.ADDON + ".toc"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("## Version:"):
                    pkg_ver = line.split(":", 1)[1].strip()
    except OSError:
        pass
    if ver is None:
        add(BAD, "Addon is not installed", "Run: wowmentor setup")
    elif pkg_ver and ver != pkg_ver:
        add(WARN, "Addon is out of date (%s, newest %s)" % (ver, pkg_ver), "Run: wowmentor setup, then type /reload in the game")
    else:
        add(OK, "Addon installed (version %s)" % ver)

    n = install.slots_present(wow)
    add(OK if n >= install.POOL + 1 else WARN, "Overlay slots: %d of %d" % (n, install.POOL + 1), "Run: wowmentor setup, then fully quit and restart WoW")

    local = cfgmod.server_root(cfg)
    code, _ = _get(local + "/healthz")
    server_ok = code == 200
    if cfgmod.server_url(cfg):
        add(OK if code == 200 else BAD, "Remote server %s is %s" % (whmod.mask_url(local), "reachable" if code == 200 else "not reachable"),
            "Check that the server is running and server_url is right: wowmentor server-url status")
    else:
        add(OK if code == 200 else BAD, "Server %s" % ("is running" if code == 200 else "is not running"), "Run: wowmentor start   (see logs: wowmentor logs)")
    bridge_up = h.services.is_running("bridge")
    add(OK if bridge_up else BAD, "Bridge %s" % ("is running" if bridge_up else "is not running"), "Run: wowmentor start")

    running = h.capture.game_running()
    add(OK if running else WARN, "WoW is %s" % ("running" if running else "not running"), "Start the game and log in to a character")

    if running:
        try:
            from .bridge import strip
            wid = h.capture.find_game()
            frame = h.capture.grab_top(wid, 120) if wid else None
            if frame is not None:
                hint = strip.find_hint(frame, *frame.size)
                add(OK if hint else WARN, "Screen capture works%s" % ("" if hint else ", but the data strip is not visible"),
                    "Log in to a character (not the login screen), then /reload. If it persists: wowmentor logs")
            else:
                add(BAD, "Cannot capture the game window", h.text["capture_fix"] % {"app": "WoW Mentor Bridge"})
        except Exception as e:
            add(WARN, "Could not test screen capture (%s)" % e)

    code, body = _get("%s?t=1" % cfgmod.feed_url(cfg))
    if code == 200:
        m = re.search(r"seconds_since_last_game_update: (-?\d+)", body)
        age = int(m.group(1)) if m else None
        if age is not None and 0 <= age <= 15:
            add(OK, "Live feed is flowing")
        else:
            add(WARN, "Feed is stale (the game is closed, minimised or on a login screen)", "Log in to a character and keep WoW visible once")
        sm = re.search(r"uislot=(\d+)", body)
        if sm:
            add(OK, "Overlay is connected (slot %s of %d used this session)" % (sm.group(1), install.POOL))
        elif age is not None and age >= 0:
            add(WARN, "Overlay slots not detected in the game", "Fully quit and restart WoW once, then /reload")
    else:
        add(BAD, "Feed not readable", "Run: wowmentor restart")

    code, body = _get("%s/state?t=1" % cfgmod.ui_base(cfg))
    if code == 200:
        try:
            st = json.loads(body)
            applied, rev = st.get("game", {}).get("rev_applied"), st["doc"]["rev"]
            if rev == 0:
                add(OK, "Overlay is empty (your agent has not drawn anything yet)")
            elif applied == rev:
                add(OK, "Overlay is in sync (revision %d)" % rev)
            else:
                add(WARN, "Overlay update is on its way (game shows %s, newest %s)" % (applied, rev), "It can take up to 30 seconds; if it stays stuck: /reload in the game")
        except Exception:
            pass

    if server_ok:
        out.extend(session_checks(cfg))

    if os.path.isdir(install.addons_dir(wow)):
        packs = [d for d in os.listdir(install.addons_dir(wow)) if d.lower().startswith("guidelime_")]
        add(OK if packs else WARN, "Guidelime guide packs: %s" % (", ".join(packs) if packs else "none found (guide editing will be unavailable)"),
            "Optional: install Guidelime and a guide pack if you want the mentor to edit your leveling guide")
    if cfg.get("public_url"):
        code, _ = _get(cfg["public_url"].rstrip("/") + "/healthz", 6)
        add(OK if code == 200 else WARN, "Public link %s" % ("reachable" if code == 200 else "not reachable"), "Run: wowmentor share on")
    return out


def run(cfg, verbose=False):
    results = checks(cfg)
    bad = 0
    print("WoW Mentor %s\n" % __version__)
    for status, title, fix in results:
        print("%s %s" % (ICON[status], title))
        if status != OK and fix:
            print("     -> %s" % fix)
        bad += status == BAD
    print("\n%s" % ("Everything needed is working." if not bad and all(s == OK for s, _, _ in results) else
                    ("Fix the ❌ items first." if bad else "Mostly fine; see the ⚠️ notes.")))
    return 1 if bad else 0
