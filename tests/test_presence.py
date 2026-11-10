import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowmentor.bridge import presence as pr
from wowmentor.bridge.presence import Presence


def kinds(events):
    return [e["type"] for e in events]


class PresenceStateTests(unittest.TestCase):
    """Every state of the design doc, with an injected clock (no sleeping)."""

    def live(self, p, now=1000.0, kv=None, fg=True):
        return p.update(now, 1.0, True, kv or {}, fg)

    def test_game_closed_without_a_window(self):
        p = Presence()
        self.assertEqual(p.update(1000, None, False, None, None), [], "the first observation emits nothing")
        self.assertEqual(p.state, pr.GAME_CLOSED)
        self.assertEqual(p.update(1001, 5, False), [])

    def test_in_world_and_afk_follow_the_kv_flag(self):
        p = Presence()
        self.live(p)
        self.assertEqual(p.state, pr.IN_WORLD)
        ev = self.live(p, 1001, {"afk": "1"})
        self.assertEqual(p.state, pr.AFK)
        self.assertEqual(ev, [{"type": "state_changed", "data": {"from": "in_world", "to": "afk"}}])
        self.live(p, 1002, {"afk": "0"})
        self.assertEqual(p.state, pr.IN_WORLD)

    def test_stale_boundaries_without_logout(self):
        p = Presence()
        self.live(p)
        cases = [(14.9, pr.IN_WORLD), (15, pr.LOADING), (30, pr.LOADING), (59.9, pr.LOADING), (60, pr.LOADING), (89.9, pr.LOADING), (90, pr.LOGGED_OUT), (500, pr.LOGGED_OUT)]
        for age, want in cases:
            p2 = Presence()
            p2.update(0, 1.0, True, {}, True)            # window up for a long time: the age is the strip's
            p2.window_since = -10000
            p2.update(1000, age, True, {}, True)
            self.assertEqual(p2.state, want, "strip age %s" % age)

    def test_afk_needs_a_live_strip(self):
        p = Presence()
        p.window, p.window_since = True, -1000
        p.update(10, 40, True, {"afk": "1"}, True)
        self.assertEqual(p.state, pr.LOADING, "a stale strip is never reported as afk")

    def test_logout_event_then_silence(self):
        p = Presence()
        self.live(p, 1000)
        p.note_event("logout", 1000, t=1000)
        self.assertEqual(p.last_logout_at, 1000)
        for age, want in ((0.5, pr.IN_WORLD), (14.9, pr.IN_WORLD), (15, pr.IN_WORLD), (19.9, pr.IN_WORLD), (20, pr.LOGGED_OUT), (45, pr.LOGGED_OUT)):
            p.update(1000 + age, age, True, {}, True)
            self.assertEqual(p.state, want, "age %s after a logout event" % age)

    def test_logout_wins_over_the_longer_loading_window(self):
        p = Presence()
        self.live(p)
        p.note_event("logout", 1000)
        ev = p.update(1030, 30, True)
        self.assertEqual(p.state, pr.LOGGED_OUT)
        self.assertIn({"type": "state_changed", "data": {"from": "in_world", "to": "logged_out"}}, ev)

    def test_logout_hold_does_not_flap_before_the_grace_period(self):
        p = Presence()
        self.live(p)
        p.note_event("logout", 1000)
        out = []
        for age in (16, 17, 18, 19):
            out += p.update(1000 + age, age, True)
        self.assertEqual(out, [], "no state change events while holding")

    def test_strip_returning_after_a_loading_screen_forgets_the_logout(self):
        p = Presence()
        self.live(p)
        p.note_event("logout", 1000)                     # PLAYER_LEAVING_WORLD for a loading screen
        p.update(1018, 18, True)                         # dark for a while
        self.assertTrue(p.logout_pending)
        p.update(1019, 0.5, True)                        # the addon draws again, no login event
        self.assertEqual(p.state, pr.IN_WORLD)
        self.assertFalse(p.logout_pending)
        p.update(1050, 31, True)
        self.assertEqual(p.state, pr.LOADING, "a later silence is judged without the old logout")

    def test_login_event_clears_a_logout(self):
        p = Presence()
        self.live(p)
        p.note_event("logout", 1000)
        p.update(1025, 25, True)
        self.assertEqual(p.state, pr.LOGGED_OUT)
        p.note_event("login", 1030, t=1029)
        self.assertEqual(p.last_login_at, 1029)
        p.update(1031, 0.2, True)
        self.assertEqual(p.state, pr.IN_WORLD)
        self.assertEqual(p.last_login_at, 1029, "an explicit login event is not overwritten by the estimate")

    def test_login_time_is_estimated_when_the_event_is_missed(self):
        p = Presence()
        p.update(1000, None, False)
        p.update(1010, None, True)                       # game started
        self.assertEqual(p.state, pr.LOADING)
        p.update(1020, 0.5, True)
        self.assertEqual(p.state, pr.IN_WORLD)
        self.assertEqual(p.last_login_at, 1020)

    def test_login_estimate_is_not_invented_for_a_bridge_started_mid_session(self):
        p = Presence()
        p.update(1000, 0.5, True)
        self.assertEqual((p.state, p.last_login_at), (pr.IN_WORLD, None))

    def test_note_event_ignores_implausible_timestamps_and_other_types(self):
        p = Presence()
        now = 1794300000
        p.note_event("login", now, t=5)
        self.assertEqual(p.last_login_at, now)
        p.note_event("death", now + 1)
        p.note_event("login", now + 2, t="x")
        self.assertEqual(p.last_login_at, now + 2)
        p.note_event("login", now + 3, t=now + 1)
        self.assertEqual(p.last_login_at, now + 1)

    def test_game_close_and_restart_cycle(self):
        p = Presence()
        self.live(p, 1000)
        ev = p.update(1010, 10, False, None, None)
        self.assertEqual(kinds(ev), ["game_closed", "state_changed"])
        self.assertEqual(ev[1]["data"], {"from": "in_world", "to": "game_closed"})
        self.assertEqual(p.last_logout_at, 1010, "closing the game counts as the logout when no logout event arrived")
        # the game is started again; the bridge's strip age still counts from the previous session
        ev = p.update(5000, 3990, True, {}, True)
        self.assertEqual(kinds(ev), ["game_started", "state_changed"])
        self.assertEqual(p.state, pr.LOADING, "the old strip age must not make a fresh launch logged_out")
        p.update(5061, 4051, True)
        self.assertEqual(p.state, pr.LOADING)
        p.update(5095, 4085, True)
        self.assertEqual(p.state, pr.LOGGED_OUT, "no strip for 90 s after the window appeared: sitting at the login screen")
        p.update(5100, 0.4, True)
        self.assertEqual(p.state, pr.IN_WORLD)
        self.assertEqual(p.last_login_at, 5100)

    def test_close_after_logout_event_keeps_that_logout_time(self):
        p = Presence()
        self.live(p, 1000)
        p.note_event("logout", 1002, t=1002)
        p.update(1050, 48, False)
        self.assertEqual(p.last_logout_at, 1002)
        self.assertFalse(p.logout_pending)

    def test_foreground_events(self):
        p = Presence()
        self.assertEqual(kinds(p.update(1, 0.5, True, {}, None)), [])
        self.assertEqual(p.foreground, "unknown")
        self.assertEqual(kinds(p.update(2, 0.5, True, {}, True)), [], "unknown -> true is not a change worth an event")
        ev = p.update(3, 0.5, True, {}, False)
        self.assertEqual(ev, [{"type": "foreground_changed", "data": {"foreground": False, "previous": True}}])
        self.assertEqual(kinds(p.update(4, 0.5, True, {}, False)), [])
        self.assertEqual(kinds(p.update(5, 0.5, True, {}, None)), [])
        self.assertEqual(p.foreground, "unknown")
        p.update(6, 0.5, True, {}, True)
        p.update(7, 8, False, {}, True)
        self.assertEqual(p.foreground, False, "no window means WoW is not in front")

    def test_snapshot_and_helpers(self):
        p = Presence()
        self.assertEqual(p.snapshot(10)["state"], "unknown")
        self.live(p, 1000)
        p.note_event("login", 1000, t=990)
        snap = p.snapshot(1005, 5.04, uploader_alive_at=1005)
        for k in ("state", "state_since", "foreground", "window_present", "last_login_at", "last_logout_at", "uploader_alive_at", "seconds_since_last_game_update"):
            self.assertIn(k, snap)
        self.assertEqual((snap["state"], snap["seconds_since_last_game_update"], snap["foreground"]), ("in_world", 5.0, True))
        self.assertEqual(sorted(pr.webhook_session(snap)), ["foreground", "last_login_at", "last_logout_at", "state", "uploader_alive_at"])
        lines = pr.session_lines(snap, lambda t: "T")
        self.assertIn("state=in_world", lines)
        self.assertIn("foreground=true", lines)
        self.assertIn("last_logout_at=none", lines)
        self.assertTrue(any(l.startswith("last_login_at=990 (T)") for l in lines))
        self.assertIn("seconds_since_last_game_update=unknown", pr.session_lines(Presence().snapshot(1), str))


if __name__ == "__main__":
    unittest.main()
