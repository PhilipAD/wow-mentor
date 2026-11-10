"""`wowmentor`: set up, check and run WoW Mentor. Standard library only, Python 3.9+. OS specifics live in wowmentor.host."""
import argparse, collections, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request

from . import REPO, __version__, config as cfgmod, doctor, host as hostmod, install
from .host.base import HostUnsupported, KINDS
from .host.common import ensure_utf8_stdio
from .bridge import localnet, webhook as whmod


def say(msg=""):
    print(msg, flush=True)


def ask_yes(question, default=True, assume=False):
    if assume or not sys.stdin.isatty():
        return default
    reply = input("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
    return default if not reply else reply.startswith("y")


def paste_prompt(cfg):
    public = bool(cfg.get("public_url"))
    base = cfgmod.ui_base(cfg, public=public)
    me = hostmod.get().text["this_machine"]
    where = "" if public else "\n(These links only work on %s, which is right for AI tools running on %s such as Claude Code or Codex. Run `wowmentor share on` to get links that work from anywhere.)" % (me, me)
    return (
        "You are my live World of Warcraft mentor. Read your operating skill first, then follow it:\n"
        '  curl -s "%s/skill?t=$RANDOM"\n'
        "Install the wowui command as the skill says. You can see my game (live text feed), draw plans and checklists on my screen, "
        "and edit my leveling guide. After any context reset run `wowui brief` first. Start by reading my game feed and putting a short plan on my screen.%s"
    ) % (base, where)


# ---------------------------------------------------------------- commands
def cmd_setup(a):
    h = hostmod.get()
    ok, why = h.capture.available()
    if not ok:
        say("WoW Mentor cannot run on this system yet. %s See docs/FAQ.md." % why)
        return 1
    if sys.version_info < (3, 9):
        say("Python 3.9 or newer is needed. %s" % h.text["python_help"])
        return 1
    say("WoW Mentor %s setup\n" % __version__)
    cfg = cfgmod.load()
    wow = a.wow_dir or cfg.get("wow_dir")
    if not wow or not os.path.isdir(os.path.join(wow, "Interface", "AddOns")):
        found = install.find_wow_dirs()
        if not found:
            say("I could not find World of Warcraft. Install it, start it once, then run this again.\n"
                "If it lives somewhere unusual: wowmentor setup --wow-dir \"/path/to/World of Warcraft/_forever_\"")
            return 1
        wow = found[0]
        if len(found) > 1:
            say("Several WoW folders found; using %s (choose another with --wow-dir):\n  %s" % (wow, "\n  ".join(found)))
    if "forever" not in wow.lower():
        say("Note: WoW Mentor is built and tested for the Forever client. %s may not work." % wow)
    cfg["wow_dir"] = wow
    cfgmod.ensure_secrets(cfg)
    cfgmod.save(cfg)
    if not os.path.exists(cfgmod.path("profile.md")):
        with open(cfgmod.path("profile.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(cfgmod.PROFILE_TEMPLATE)
    say("1/5  Found WoW at %s" % wow)

    install.install_addon(wow)
    new = install.create_slots(wow)
    say("2/5  Installed the addon (%d overlay slots %s)" % (install.POOL + 1, "created" if new else "already there"))

    helpers = None
    if not a.no_build:
        helpers = h.services.build_helpers(cfg)
        say("3/5  Built the helpers: %s" % helpers["summary"] if helpers.get("summary") else "3/5  No helpers to build on this system")
    else:
        say("3/5  Skipped building helpers (--no-build)")

    remote = cfgmod.server_url(cfg)
    if a.no_services:
        say("4/5  Skipped starting services (--no-services)")
    elif remote:
        h.services.install("bridge", cfg, helpers)
        ok2 = h.services.start("bridge")
        say("4/5  Started the bridge (%s). No local server installed: server_url points at %s" % ("running" if ok2 else "check `wowmentor doctor`", whmod.mask_url(remote)))
    else:
        h.services.install("server", cfg, helpers)
        h.services.install("bridge", cfg, helpers)
        ok1, ok2 = h.services.start("server"), h.services.start("bridge")
        say("4/5  Started the server and the bridge (%s)" % ("running" if ok1 and ok2 else "check `wowmentor doctor`"))

    app_name = ((helpers or {}).get("launcher") and os.path.basename(helpers["launcher"])) or "WoW Mentor Bridge"
    if h.text["permission_lines"]:
        for line in h.text["permission_lines"]:
            say(line % {"app": app_name})
        if not a.no_services:
            h.shell.open_privacy_settings()
    else:
        say("5/5  No extra permission is needed on %s." % h.text["os_name"])
    say("\nAlmost done:")
    say("  - Fully quit World of Warcraft and start it again (the game only discovers the new addon files at launch).")
    say("  - Then run:  wowmentor doctor     (it checks everything and tells you what to fix)")
    say("  - And:       wowmentor links      (your private links and the text to paste to your AI)")
    say("  - Optional:  wowmentor profile    (tell the mentor about your goals)")
    return 0


def cmd_links(a):
    cfg = cfgmod.load()
    if not cfg.get("ui_secret"):
        say("Run `wowmentor setup` first.")
        return 1
    say("Your private links (anyone with a link can read your game feed or control your overlay: do not post them).\n")
    if cfgmod.server_url(cfg):
        say("Server (server_url):     %s   (runs elsewhere; the bridge on %s pushes to it)" % (whmod.mask_url(cfgmod.server_url(cfg)), hostmod.get().text["this_machine"]))
    say("Game feed (read only):   %s?t=1" % cfgmod.feed_url(cfg, public=bool(cfg.get("public_url"))))
    say("Overlay and guide API:   %s" % cfgmod.ui_base(cfg, public=bool(cfg.get("public_url"))))
    say("Agent briefing page:     %s/skill" % cfgmod.ui_base(cfg, public=bool(cfg.get("public_url"))))
    say("Events (read only):      %s/events?since=0" % cfgmod.feed_url(cfg, public=bool(cfg.get("public_url"))))
    code, _ = doctor._get(cfgmod.server_root(cfg) + "/healthz", 2)
    if code != 200:
        say("\nHeads up: the server is not answering yet, so these links will not work until it does. Run: wowmentor start   (then: wowmentor doctor)")
    if not cfg.get("public_url") and not cfgmod.server_url(cfg):
        say("\nThese work on %s only. Using an AI that runs somewhere else (a website or another computer)? Run:  wowmentor share on" % hostmod.get().text["this_machine"])
    say("\nPaste this to your AI agent:\n")
    say("----------------------------------------------------------------")
    p = paste_prompt(cfg)
    say(p)
    say("----------------------------------------------------------------")
    if a.copy:
        if hostmod.get().shell.copy_to_clipboard(p):
            say("(copied to your clipboard)")
    return 0


def cmd_prompt(a):
    a.copy = True
    return cmd_links(a)


def cmd_doctor(a):
    return doctor.run(cfgmod.load(), verbose=a.verbose)


def _status_line(cfg):
    from urllib.error import URLError
    try:
        localnet.urlopen(cfgmod.server_root(cfg) + "/healthz", timeout=2).read()
        server = "running"
    except (URLError, OSError):
        server = "not running"
    return server


def cmd_status(a):
    cfg = cfgmod.load()
    where = (" (remote %s)" % whmod.mask_url(cfgmod.server_url(cfg))) if cfgmod.server_url(cfg) else ""
    say("server: %s%s   bridge: %s   share: %s" % (_status_line(cfg), where, "running" if hostmod.get().services.is_running("bridge") else "not running",
                                                cfg.get("public_url") or "off"))
    return 0


def cmd_start(a):
    sv = hostmod.get().services
    for k in (("bridge",) if cfgmod.server_url(cfgmod.load()) else ("server", "bridge")):      # a remote server is not ours to start
        sv.start(k)
    if sv.is_installed("tunnel"):
        sv.start("tunnel")
    say("started")
    return 0


def cmd_stop(a):
    for k in ("bridge", "server", "tunnel"):
        hostmod.get().services.stop(k)
    say("stopped (they start again at your next login, or run `wowmentor start`)")
    return 0


def cmd_restart(a):
    cmd_stop(a)
    time.sleep(1)
    return cmd_start(a)


def cmd_logs(a):
    d = cfgmod.log_dir()
    sv = hostmod.get().services
    if not any(os.path.exists(sv.log_path(k)) for k in KINDS):
        say("No log files yet in %s. They appear once the services have started (run `wowmentor setup`, or `wowmentor start`)." % d)
        return 0
    for k in KINDS:
        p = sv.log_path(k)
        if os.path.exists(p):
            say("== %s ==" % p)
            with open(p, encoding="utf-8", errors="replace") as f:
                say("".join(collections.deque(f, maxlen=max(1, a.lines))))          # the tail only: logs can be large
    return 0


def cmd_profile(a):
    p = cfgmod.path("profile.md")
    if not os.path.exists(p):
        os.makedirs(cfgmod.home(), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(cfgmod.PROFILE_TEMPLATE)
    say("Your profile file: %s\nEdit it, save, and your mentor uses it the next time it fetches its skill page." % p)
    if not a.no_open:
        hostmod.get().shell.open_text_file(p)
    return 0


def cmd_rotate(a):
    cfg = cfgmod.load()
    cfgmod.ensure_secrets(cfg, rotate=True)
    cfgmod.save(cfg)
    cmd_restart(a)
    say("New secrets created and the services restarted. Old links no longer work: run `wowmentor links`.")
    return 0


def find_cloudflared(cfg):
    extra = [os.path.join(d, "cloudflared") for d in hostmod.get().locations.extra_bin_dirs()]
    for c in [cfg.get("cloudflared"), shutil.which("cloudflared")] + extra:
        if c and os.path.exists(c):
            return c
    return None


def cmd_share(a):
    cfg = cfgmod.load()
    if a.state == "status":
        say("share is %s" % (cfg.get("public_url") or "off"))
        return 0
    if a.state == "off":
        hostmod.get().services.remove("tunnel")
        cfg["public_url"] = None
        cfgmod.save(cfg)
        say("Sharing is off. Your links work on %s only." % hostmod.get().text["this_machine"])
        return 0
    if cfgmod.server_url(cfg):
        say("server_url is set, so the server does not run on %s and there is nothing here to share. Use the links of that server (`wowmentor links`)." % hostmod.get().text["this_machine"])
        return 1
    cf = find_cloudflared(cfg)
    if not cf:
        pm = hostmod.get().text["package_manager"]
        if pm == "brew" and shutil.which("brew") and ask_yes("cloudflared (Cloudflare's free tunnel tool) is needed. Install it with Homebrew now?", True, a.yes):
            subprocess.run(["brew", "install", "cloudflared"])
            cf = find_cloudflared(cfg)
        if not cf:
            say(hostmod.get().text["cloudflared_hint"])
            return 1
    cfg["cloudflared"] = cf
    cfgmod.save(cfg)
    hostmod.get().services.install("tunnel", cfg)
    hostmod.get().services.start("tunnel")
    say("Starting the tunnel...")
    for _ in range(40):
        time.sleep(1)
        if cfgmod.load().get("public_url"):
            break
    cfg = cfgmod.load()
    if not cfg.get("public_url"):
        say("The tunnel did not report a URL yet. Check `wowmentor logs`.")
        return 1
    say("Sharing is on: %s\n(This free address changes whenever the tunnel restarts; run `wowmentor links` for the current one.)\n" % cfg["public_url"])
    return cmd_links(a)


def _session(cfg):
    """The server's session object (with the bridge's webhook stats), or None if unreachable."""
    return doctor.fetch_session(cfg)


def cmd_webhook(a):
    cfg = cfgmod.load()
    if a.action == "set":
        if not whmod.valid_url(a.url):
            say("That is not a usable URL. Use http:// or https:// with a host, for example https://example.com/hooks/wow")
            return 1
        cfg["webhook_url"] = a.url
        secret = a.secret
        if secret == "-":
            secret = sys.stdin.readline().strip()
        elif secret is None and os.environ.get("WOWMENTOR_WEBHOOK_SECRET"):
            secret = os.environ["WOWMENTOR_WEBHOOK_SECRET"]
        if a.no_secret:
            cfg["webhook_secret"] = None
        elif secret is not None:
            cfg["webhook_secret"] = secret or None
        token = a.bearer
        if token == "-":
            token = sys.stdin.readline().strip()
        elif token is None and os.environ.get("WOWMENTOR_WEBHOOK_BEARER"):
            token = os.environ["WOWMENTOR_WEBHOOK_BEARER"]
        auth = whmod.bearer(token) if token else a.auth
        if auth and not whmod.valid_auth(auth):
            say("That key or Authorization value has characters that cannot go in an HTTP header (use printable ASCII, no line breaks).")
            return 1
        if a.no_auth:
            cfg["webhook_auth"] = None
        elif auth:
            cfg["webhook_auth"] = auth
        cfgmod.save(cfg)
        say("Webhook set: %s  (%s%s)" % (whmod.mask_url(a.url), "signed with your secret" if cfg.get("webhook_secret") else "not signed: add --secret to sign requests",
                                         ", Authorization header sent" if cfg.get("webhook_auth") else ""))
        if a.url.lower().startswith("http://") and (urllib.parse.urlsplit(a.url).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
            say("Note: http:// is not encrypted; use https:// if the events should stay private.")
        say("The running bridge picks this up within a few seconds (no restart). Check it with: wowmentor webhook test")
        return 0
    if a.action == "off":
        cfg["webhook_url"] = cfg["webhook_secret"] = cfg["webhook_auth"] = None
        cfgmod.save(cfg)
        say("Webhook is off.")
        return 0
    url = cfg.get("webhook_url")
    if a.action == "status":
        if not url:
            say("Webhook: off  (set one with: wowmentor webhook set <url> [--secret S])")
            return 0
        say("Webhook: %s  (%s%s)" % (whmod.mask_url(url), "signed" if cfg.get("webhook_secret") else "not signed", ", Authorization header" if cfg.get("webhook_auth") else ""))
        s = _session(cfg)
        w = (s or {}).get("webhook") or {}
        if not s or not s.get("known"):
            say("Bridge: no report yet (is it running? `wowmentor status`)")
        else:
            say("Delivered: %s event(s) in %s post(s); queued: %s; failures: %s%s" % (w.get("sent_events", 0), w.get("posts", 0), w.get("queued", 0), w.get("failures", 0),
                                                                                     "; last result: HTTP %s" % w["last_status"] if w.get("last_status") else ""))
            if w.get("failing"):
                say("Currently failing (%s); the bridge keeps retrying every minute." % w.get("last_error"))
        return 0
    # test
    if not url:
        say("No webhook is set. Use: wowmentor webhook set <url> [--secret S]")
        return 1
    now = time.time()
    ev = {"eid": 0, "t": int(now), "type": "webhook_test", "data": {"message": "Test event from `wowmentor webhook test`"}}
    sess = whmod_session(_session(cfg), now)
    status, err = whmod.send_once(url, cfg.get("webhook_secret"), [ev], sess, now, auth=cfg.get("webhook_auth"))
    if status is None:
        say("Could not reach %s (%s)." % (whmod.mask_url(url), err))
        return 1
    say("%s answered HTTP %d%s." % (whmod.mask_url(url), status, " (signed)" if cfg.get("webhook_secret") else ""))
    if not 200 <= status < 300:
        say("Your endpoint must answer 2xx for the bridge to treat a delivery as done.")
        return 1
    return 0


def whmod_session(s, now):
    keys = ("state", "foreground", "last_login_at", "last_logout_at", "uploader_alive_at")
    base = {"state": "unknown", "foreground": "unknown", "last_login_at": None, "last_logout_at": None, "uploader_alive_at": int(now)}
    if s and s.get("known"):
        base.update({k: s.get(k) for k in keys if k in s})
    return base


def cmd_server_url(a):
    cfg = cfgmod.load()
    if a.action == "status":
        say("server_url: %s" % (whmod.mask_url(cfgmod.server_url(cfg)) if cfgmod.server_url(cfg) else "not set (the server runs on %s, port %s)" % (hostmod.get().text["this_machine"], cfg["port"])))
        return 0
    if a.action == "off":
        cfg["server_url"] = None
        cfgmod.save(cfg)
        say("server_url cleared. Run `wowmentor setup` to install the local server again, then `wowmentor restart`.")
        return 0
    if not cfgmod.valid_server_url(a.url):
        say("Use the server's root, for example https://wow.example.com or http://192.168.1.20:8095 (no query, no credentials).")
        return 1
    cfg["server_url"] = a.url.strip().rstrip("/")
    cfgmod.save(cfg)
    host = (urllib.parse.urlparse(cfg["server_url"]).hostname or "")
    if cfg["server_url"].startswith("http://") and not (host in ("localhost", "127.0.0.1", "::1") or host.startswith(("10.", "192.168.", "172."))):
        say("Warning: plain http:// sends your secrets unencrypted across the internet. Use https:// (or a private network such as a VPN or an ssh tunnel).")
    say("server_url set to %s. The bridge now pushes there; `wowmentor setup` will not install a local server. Run `wowmentor restart` to apply." % whmod.mask_url(cfg["server_url"]))
    say("The remote server must run the same secrets (read_token, write_secret, ui_secret) as %s's config.json." % hostmod.get().text["this_machine"])
    return 0


PURGE_ENTRIES = ("config.json", "config.json.bak", "config.json.tmp", "profile.md", "data", "bin", "logs")


def purge_home():
    """Delete what WoW Mentor created in its config folder, and the folder itself if that leaves it empty. $WOWMENTOR_HOME can point
    anywhere (even at a folder with other files in it), so the folder is never removed recursively as a whole."""
    home = cfgmod.home()
    for name in PURGE_ENTRIES:
        p = os.path.join(home, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        os.rmdir(home)
    except OSError:
        pass


def cmd_uninstall(a):
    cfg = cfgmod.load()
    if not ask_yes("Remove WoW Mentor (services, helper app, and the addon with its slots from your AddOns folder)?", True, a.yes):
        return 1
    for k in ("bridge", "server", "tunnel"):
        hostmod.get().services.remove(k)
    hostmod.get().services.remove_helpers()
    if cfg.get("wow_dir"):
        say("Removed %d addon folders." % install.remove_addon(cfg["wow_dir"]))
    if a.purge:
        purge_home()
        say("Removed your config, secrets and saved data.")
    else:
        say("Kept your config and data in %s (add --purge to delete it)." % cfgmod.home())
    say("Your guide files were never deleted; backups of anything the mentor edited are in %s." % cfgmod.path("data", "guide-backups"))
    return 0


def cmd_update(a):
    root = install.PKG_ROOT
    if os.path.isdir(os.path.join(root, ".git")):
        r = subprocess.run(["git", "-C", root, "pull", "--ff-only"])
        if r.returncode != 0:
            return r.returncode
    else:
        say("Run the installer again to update:\n  curl -fsSL https://raw.githubusercontent.com/%s/main/install.sh | bash" % REPO)
        return 0
    return cmd_setup(argparse.Namespace(wow_dir=None, no_build=False, no_services=False))


def cmd_server(a):
    from .server import app
    app.serve(cfgmod.load())
    return 0


def cmd_bridge(a):
    from .bridge import app
    app.run(cfgmod.load())
    return 0


def cmd_tunnel(a):
    """Internal: run cloudflared and keep config.json's public_url current."""
    cfg = cfgmod.load()
    cf = find_cloudflared(cfg)
    if not cf:
        say("cloudflared not found")
        return 1
    proc = subprocess.Popen([cf, "tunnel", "--url", cfgmod.local_url(cfg), "--no-autoupdate"], stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    try:
        for line in proc.stderr:
            m = pat.search(line)
            if m:
                c = cfgmod.load()
                c["public_url"] = m.group(0)
                cfgmod.save(c)
                say("tunnel: %s" % m.group(0))
    finally:
        c = cfgmod.load()
        c["public_url"] = None
        cfgmod.save(c)
    return proc.wait()


def main(argv=None):
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(prog="wowmentor", description="Set up and run WoW Mentor: your AI agent sees your game and mentors you with an on-screen overlay.")
    ap.add_argument("--version", action="version", version="wowmentor " + __version__)
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_):
        if help_ is argparse.SUPPRESS:        # internal commands: callable, but not listed
            p = sub.add_parser(name)
        else:
            p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("setup", cmd_setup, "install everything (safe to run again)")
    p.add_argument("--wow-dir", help='e.g. "%s"' % hostmod.get().text["example_wow_path"])
    p.add_argument("--no-build", action="store_true", help="skip compiling the helper app (testing)")
    p.add_argument("--no-services", "--no-launchd", dest="no_services", action="store_true", help="do not start background services (testing; --no-launchd is the old name)")
    p = add("doctor", cmd_doctor, "check everything and say what to fix"); p.add_argument("-v", "--verbose", action="store_true")
    add("status", cmd_status, "one-line state of the services")
    p = add("links", cmd_links, "show your private links and the text to paste to your AI"); p.add_argument("--copy", action="store_true", help="copy the paste text to the clipboard")
    add("prompt", cmd_prompt, "copy the text to paste to your AI to the clipboard")
    p = add("share", cmd_share, "make your links work from anywhere with a free Cloudflare tunnel"); p.add_argument("state", choices=["on", "off", "status"]); p.add_argument("--yes", action="store_true"); p.add_argument("--copy", action="store_true")
    wp = sub.add_parser("webhook", help="send game events (death, bags full, ...) to your own URL", description="The bridge batches the game events and POSTs them to your URL. set | off | status | test")
    wsub = wp.add_subparsers(dest="action", required=True, metavar="action")
    q = wsub.add_parser("set", help="set the URL (and optionally a signing secret)"); q.set_defaults(fn=cmd_webhook); q.add_argument("url")
    q.add_argument("--secret", help="HMAC-SHA256 signing secret ('-' reads it from stdin; or set $WOWMENTOR_WEBHOOK_SECRET); kept if you omit it")
    q.add_argument("--no-secret", action="store_true", help="remove the secret")
    q.add_argument("--bearer", help="send 'Authorization: Bearer <KEY>' with every POST ('-' reads it from stdin; or set $WOWMENTOR_WEBHOOK_BEARER); kept if you omit it")
    q.add_argument("--auth", help="send this exact Authorization header value instead (for example 'Basic abc123')")
    q.add_argument("--no-auth", action="store_true", help="stop sending an Authorization header")
    for nm, hp in (("off", "turn the webhook off"), ("status", "show where events go and whether deliveries work"), ("test", "send a sample event and report the response code")):
        q = wsub.add_parser(nm, help=hp); q.set_defaults(fn=cmd_webhook)
    sp = sub.add_parser("server-url", help="use a server that runs elsewhere (set <url> | off | status)", description="Point the bridge at a server running on another machine; setup then installs no local server.")
    ssub = sp.add_subparsers(dest="action", required=True, metavar="action")
    q = ssub.add_parser("set", help="the server root, e.g. https://wow.example.com"); q.set_defaults(fn=cmd_server_url); q.add_argument("url")
    for nm in ("off", "status"):
        q = ssub.add_parser(nm); q.set_defaults(fn=cmd_server_url)
    p = add("profile", cmd_profile, "tell the mentor about your goals (opens a text file)"); p.add_argument("--no-open", action="store_true")
    add("start", cmd_start, "start the background services")
    add("stop", cmd_stop, "stop the background services")
    add("restart", cmd_restart, "restart the background services")
    p = add("logs", cmd_logs, "show recent log lines"); p.add_argument("-n", "--lines", type=int, default=30)
    add("rotate-secrets", cmd_rotate, "make new private links (old ones stop working)")
    add("update", cmd_update, "update to the latest version")
    p = add("uninstall", cmd_uninstall, "remove services and the addon"); p.add_argument("--yes", action="store_true"); p.add_argument("--purge", action="store_true", help="also delete config and saved data")
    add("server", cmd_server, argparse.SUPPRESS)
    add("bridge", cmd_bridge, argparse.SUPPRESS)
    add("tunnel", cmd_tunnel, argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    try:
        return a.fn(a) or 0
    except HostUnsupported as e:
        say(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
