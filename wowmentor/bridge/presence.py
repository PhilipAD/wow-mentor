"""Presence: is the player in the world, loading, away, logged out, or has the game closed?

Pure logic with no I/O and no clock of its own: every method takes `now`, so each state and transition is unit testable.
The bridge calls update() about once a second with what it can see from the Mac side:

  strip_age       seconds since the last pixel-strip message was decoded (None: none since the bridge started)
  window_present  a WoW window exists
  kv              the addon's KV line as a dict (only `afk` is used)
  foreground      True / False, or None when macOS could not tell us (non-Mac, CoreGraphics error)

States (docs/DESIGN-coach-events.md, "Presence"):

  game_closed  no WoW window
  in_world     the strip was decoded within LIVE_S seconds and the addon does not report afk
  afk          same, and the addon reports afk=1
  loading      window present, strip stale, the last lifecycle event was not a logout, and the strip has been stale for
               less than LOGGED_OUT_S (the design says "< 60 s"; between 60 and 90 s nothing else applies, so it stays loading)
  logged_out   window present and either: the last lifecycle event was a logout and the strip has been stale for
               LOGOUT_GRACE_S or more, or the strip has been stale for LOGGED_OUT_S or more

Two gaps in the written rules are filled like this, both so that the state does not flap:
  * after a `logout` event and before LOGOUT_GRACE_S of silence (strip stale between LIVE_S and LOGOUT_GRACE_S) the previous
    state is held;
  * a `logout` event is also emitted for loading screens (PLAYER_LEAVING_WORLD). If the strip goes dark after a logout event and
    later comes back without a `login` event in between, the logout is forgotten and the player is in_world again;
  * /reload sends `logout` too (PLAYER_LOGOUT) and the new addon load sends `login`. The strip is dark for a few seconds in between,
    far less than LOGOUT_GRACE_S, so the state stays in_world and never visits logged_out. If the `login` event is lost, a strip that is
    drawn again LOGOUT_REVIVE_S after the logout cancels it as well.

"Live" means the real strip age is under LIVE_S. The staleness used for the other rules is min(strip_age, seconds since the WoW window
appeared), so when the game starts, the old strip age from the previous session does not make the state jump straight to logged_out.
"""
import threading

IN_WORLD, LOADING, AFK, LOGGED_OUT, GAME_CLOSED = "in_world", "loading", "afk", "logged_out", "game_closed"
STATES = (IN_WORLD, LOADING, AFK, LOGGED_OUT, GAME_CLOSED)
LIVE_S = 15            # a strip message this fresh means the addon is drawing
LOADING_S = 60         # loading is the answer for staleness below this (and, see above, up to LOGGED_OUT_S)
LOGOUT_GRACE_S = 20    # after a logout event: silence this long means logged out
LOGGED_OUT_S = 90      # silence this long with the window present means logged out
LOGOUT_REVIVE_S = 8    # the strip still being drawn this long after a `logout` event means it was a /reload, not a logout
UNKNOWN = "unknown"


def _plausible(t, now):
    return isinstance(t, (int, float)) and not isinstance(t, bool) and abs(now - t) < 86400 * 2


class Presence(object):
    def __init__(self):
        self.lock = threading.RLock()
        self.state = None               # None until the first update
        self.state_since = None
        self.window = None              # None until the first update, then True/False
        self.window_since = None
        self.foreground = UNKNOWN       # True / False / "unknown"
        self.last_login_at = None
        self.last_logout_at = None
        self.logout_pending = False     # the last lifecycle event was a logout
        self._dark_since_logout = False
        self._awaiting_login = False    # entered logged_out / game_closed and have not seen the player come back yet
        self._login_seen = False        # a `login` event arrived since then
        self._logout_seen_at = None     # when the last `logout` event arrived (our clock)

    # ---------------------------------------------------------------- inputs
    def note_event(self, typ, now, t=None):
        """Feed lifecycle events seen on the strip (`login`, `logout`). Other types are ignored."""
        with self.lock:
            when = t if _plausible(t, now) else now
            if typ == "login":
                self.last_login_at, self.logout_pending, self._dark_since_logout = when, False, False
                self._login_seen = True
            elif typ == "logout":
                self.last_logout_at, self.logout_pending, self._dark_since_logout = when, True, False
                self._logout_seen_at = now

    def update(self, now, strip_age, window_present, kv=None, foreground=None):
        """Recompute the state. Returns the bridge events to emit: a list of {"type": ..., "data": {...}}."""
        out = []
        with self.lock:
            prev_window, prev_state, prev_fg = self.window, self.state, self.foreground
            self.window = bool(window_present)
            if self.window and not prev_window:
                self.window_since = now
                if prev_window is False:
                    out.append({"type": "game_started", "data": {}})
            if not self.window:
                self.window_since = None
            live = strip_age is not None and strip_age < LIVE_S
            last_strip_at = None if strip_age is None else now - max(0.0, strip_age)
            new = self._classify(live, self._age(now, strip_age), kv or {}, last_strip_at)
            if not self.window and prev_window:
                out.append({"type": "game_closed", "data": {}})
                if prev_state in (IN_WORLD, AFK, LOADING) and not self.logout_pending:
                    self.last_logout_at = now          # the game went away without a logout event reaching the strip
                self.logout_pending, self._dark_since_logout = False, False
            if new != prev_state:
                if prev_state is not None:
                    out.append({"type": "state_changed", "data": {"from": prev_state, "to": new}})
                if new in (LOGGED_OUT, GAME_CLOSED) and prev_state not in (LOGGED_OUT, GAME_CLOSED):
                    self._awaiting_login = True
                elif new in (IN_WORLD, AFK) and self._awaiting_login:
                    self._awaiting_login = False
                    if prev_state is not None and not self._login_seen:
                        self.last_login_at = now          # no `login` event reached us: the first live strip is the best estimate
                if new in (LOGGED_OUT, GAME_CLOSED):
                    self._login_seen = False
                self.state, self.state_since = new, now
            if not self.window:
                fg = False if foreground is not None else UNKNOWN
            else:
                fg = UNKNOWN if foreground is None else bool(foreground)
            if fg != prev_fg and fg != UNKNOWN and prev_fg != UNKNOWN:
                out.append({"type": "foreground_changed", "data": {"foreground": fg, "previous": prev_fg}})
            self.foreground = fg
        return out

    # ---------------------------------------------------------------- derivation
    def _age(self, now, strip_age):
        if not self.window:
            return strip_age
        since = max(0.0, now - self.window_since)
        return since if strip_age is None else min(max(0.0, strip_age), since)

    def _classify(self, live, age, kv, last_strip_at=None):
        if not self.window:
            return GAME_CLOSED
        if live:
            if self.logout_pending and (self._dark_since_logout or (
                    last_strip_at is not None and self._logout_seen_at is not None and last_strip_at > self._logout_seen_at + LOGOUT_REVIVE_S)):
                # the strip came back, or kept being drawn well after the logout event (a /reload whose `login` event was missed)
                self.logout_pending, self._dark_since_logout = False, False
            return AFK if str(kv.get("afk")) == "1" else IN_WORLD
        if self.logout_pending:
            self._dark_since_logout = True
            if age >= LOGOUT_GRACE_S:
                return LOGGED_OUT
            return self.state if self.state in (IN_WORLD, AFK, LOADING) else LOGGED_OUT
        if age >= LOGGED_OUT_S:
            return LOGGED_OUT
        return LOADING

    def snapshot(self, now, strip_age=None, uploader_alive_at=None):
        """The session object served at GET /ui/<s>/session and pushed by the bridge."""
        with self.lock:
            return {
                "state": self.state or UNKNOWN,
                "state_since": self.state_since,
                "foreground": self.foreground,
                "window_present": self.window,
                "last_login_at": self.last_login_at,
                "last_logout_at": self.last_logout_at,
                "uploader_alive_at": now if uploader_alive_at is None else uploader_alive_at,
                "seconds_since_last_game_update": None if strip_age is None else round(strip_age, 1),
            }


def webhook_session(snap):
    """The five session keys of the webhook payload (see the design doc)."""
    return {k: snap.get(k) for k in ("state", "foreground", "last_login_at", "last_logout_at", "uploader_alive_at")}


def session_lines(snap, fmt_time):
    """The SESSION_STATE feed section as key=value lines. Values never seen are `none`; the game's hidden ones are `unknown`."""
    def t(v):
        return "none" if v is None else "%d (%s)" % (v, fmt_time(v))
    age = snap.get("seconds_since_last_game_update")
    return [
        "state=%s" % snap.get("state", UNKNOWN),
        "state_since=%s" % t(snap.get("state_since")),
        "foreground=%s" % ("true" if snap.get("foreground") is True else "false" if snap.get("foreground") is False else UNKNOWN),
        "window_present=%s" % ("true" if snap.get("window_present") else "false" if snap.get("window_present") is False else UNKNOWN),
        "last_login_at=%s" % t(snap.get("last_login_at")),
        "last_logout_at=%s" % t(snap.get("last_logout_at")),
        "uploader_alive_at=%s" % t(snap.get("uploader_alive_at")),
        "seconds_since_last_game_update=%s" % ("unknown" if age is None else "%.0f" % age),
    ]
