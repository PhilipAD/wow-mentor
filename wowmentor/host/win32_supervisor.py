"""Windows service supervisor: keeps `wowmentor server|bridge|tunnel` alive without admin rights.

Why: launchd restarts a dying service on macOS; a Windows Run-key entry only starts it once at login. So the Run key (and
`wowmentor start`) launches this small program, which runs the real service as a child, restarts it with a growing pause, and
writes a pid file with a heartbeat that `wowmentor status` reads. Standard library only; importing this module touches no native
binding (ctypes is bound inside `pid_alive`).

Command lines (always started by `Win32Services`, or by the Run key through pythonw.exe, which has no console, so stdout and
stderr are None until `redirect_output` points them at the log file):

    pythonw.exe win32_supervisor.py <kind> [--home DIR]     supervise one service (server, bridge or tunnel)
    pythonw.exe win32_supervisor.py --autostart [--home DIR]  at login: start a supervisor for every installed service, then exit

It is started as a script (not `-m`) because the Run key's working directory is System32, where the `wowmentor` package cannot be
found; the script puts the package root on sys.path itself. Stopping: `Win32Services.stop` creates `<kind>.stop`; this loop sees it
within a second, ends the child (and its process tree) and exits. `taskkill /T /F` is the fallback.
"""
import io, json, os, subprocess, sys, time

if __name__ == "__main__":                       # run as a script: make `import wowmentor` work and drop wowmentor/host from sys.path
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))

KINDS = ("server", "bridge", "tunnel")
BACKOFF = (1, 2, 5, 15, 30)         # seconds to wait before restart number 1, 2, 3, ...; the last value repeats
RESET_AFTER = 60                    # a child that ran this long (seconds) counts as healthy: the next failure starts the backoff again
TICK = 1.0                          # how often the loop looks at the child and the stop file
HEARTBEAT_EVERY = 5.0               # pid file rewrite interval
STALE_AFTER = 20.0                  # a pid file whose heartbeat is older than this does not describe a live supervisor
LOG_ROTATE_BYTES = 5 * 1024 * 1024

# https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5

SCRIPT = os.path.abspath(__file__)
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT)))


# ---------------------------------------------------------------- pure helpers
def backoff_delay(failures):
    """Pause before restart number `failures` (1 = first restart): 1, 2, 5, 15, 30, 30, ..."""
    return BACKOFF[min(max(failures, 1), len(BACKOFF)) - 1]


def next_failures(failures, uptime):
    """Failure count after a child ended: a child that stayed up RESET_AFTER seconds starts the count over at 1."""
    return 1 if uptime >= RESET_AFTER else failures + 1


def gui_python(exe=None, exists=os.path.exists):
    """pythonw.exe next to `exe` (no console window at all), else `exe` itself (callers then add CREATE_NO_WINDOW)."""
    exe = exe or sys.executable
    d, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        return exe
    w = os.path.join(d, "pythonw.exe")
    return w if exists(w) else exe


def console_python(exe=None, exists=os.path.exists):
    """python.exe next to `exe`: children need real standard handles (the log file), which pythonw.exe does not provide."""
    exe = exe or sys.executable
    d, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        c = os.path.join(d, "python.exe")
        return c if exists(c) else exe
    return exe


def child_env(base_env=None, home=None):
    """Environment for a service: the package on PYTHONPATH (as the macOS plist does), UTF-8 mode, unbuffered output."""
    env = dict(os.environ if base_env is None else base_env)
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = PKG_ROOT + (os.pathsep + old if old else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if home:
        env["WOWMENTOR_HOME"] = home
    return env


def child_argv(kind, exe=None):
    return [console_python(exe), "-m", "wowmentor", kind]


def supervisor_argv(kind, home=None, exe=None):
    """Command line that starts the supervisor of `kind` (or the autostart pass when kind is None)."""
    a = [gui_python(exe), SCRIPT, kind if kind else "--autostart"]
    if home:
        a += ["--home", home]
    return a


def run_dir():
    from wowmentor import config
    return config.path("run")


def pidfile_path(kind):
    return os.path.join(run_dir(), kind + ".pid")


def stopfile_path(kind):
    return os.path.join(run_dir(), kind + ".stop")


def installed_marker(kind):
    return os.path.join(run_dir(), kind + ".installed")


def installed_kinds():
    return [k for k in KINDS if os.path.exists(installed_marker(k))]


def read_pidfile(path):
    """The pid file as a dict with an int `pid` and a float `heartbeat`, or None when missing, unreadable or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and int(d["pid"]) > 0:
            d["pid"] = int(d["pid"])
            d["heartbeat"] = float(d.get("heartbeat", 0))
            return d
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_pidfile(path, data, replace=os.replace, sleep=time.sleep):
    """Atomic small write, retried while another process (the status command, antivirus) holds the old file open."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f)
    try:
        for attempt in range(8):
            try:
                replace(tmp, path)
                return
            except PermissionError as e:
                if getattr(e, "winerror", None) not in (5, 32, 33) or attempt == 7:
                    raise
                sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def is_fresh(info, now=None, stale_after=STALE_AFTER):
    return bool(info) and (time.time() if now is None else now) - info["heartbeat"] <= stale_after


# ---------------------------------------------------------------- native: is a pid alive
_k32 = None


def _kernel32():
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import c_void_p, c_int, POINTER
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, c_int, wintypes.DWORD]
        k.OpenProcess.restype = c_void_p
        k.GetExitCodeProcess.argtypes = [c_void_p, POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = c_int
        k.CloseHandle.argtypes = [c_void_p]
        k.CloseHandle.restype = c_int
        _k32 = k
    return _k32


def pid_alive(pid):
    """True if a process with this pid is running. Windows: OpenProcess + GetExitCodeProcess (os.kill(pid, 0) would TERMINATE the
    process there, and wmic is gone from current Windows 11). An exit code of 259 (STILL_ACTIVE) can also be a real exit code, which is
    why callers combine this with a fresh heartbeat.
    https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess"""
    if not pid or pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
    import ctypes
    from ctypes import wintypes
    k = _kernel32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED         # exists, but belongs to someone we cannot query
    try:
        code = wintypes.DWORD(0)
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def supervisor_running(kind, now=None):
    info = read_pidfile(pidfile_path(kind))
    return bool(info) and is_fresh(info, now) and pid_alive(info["pid"])


def kill_tree(pid):
    """End `pid` and everything it started (cloudflared under `wowmentor tunnel`). No shell; best effort, never raises."""
    try:
        r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def launch_detached(kind, home=None, exe=None, popen=None):
    """Start the supervisor of `kind` so it survives this process: new process group, no console, no inherited handles.
    Returns the new pid. Clears an old stop request first."""
    try:
        os.remove(stopfile_path(kind))
    except OSError:
        pass
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW        # the same flags are harmless where they are undefined
    p = (popen or subprocess.Popen)(supervisor_argv(kind, home, exe), creationflags=flags if sys.platform == "win32" else 0,
                                    close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=child_env(home=home), cwd=PKG_ROOT)
    try:
        write_pidfile(pidfile_path(kind), {"pid": p.pid, "kind": kind, "created": time.time(), "heartbeat": time.time()})
    except OSError:
        pass
    return p.pid


# ---------------------------------------------------------------- the loop
class Supervisor(object):
    """Runs `argv` until `stop_requested()`; restarts it with backoff. Time, sleeping and process creation are injected so the policy
    is unit-tested without Windows."""

    def __init__(self, kind, argv, env=None, log=None, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
                 stop_requested=lambda: False, heartbeat=lambda child_pid: None, kill=kill_tree, say=lambda m: None, flags=0):
        self.kind, self.argv, self.env, self.log = kind, argv, env, log
        self.popen, self.clock, self.sleep, self.stop_requested = popen, clock, sleep, stop_requested
        self.heartbeat, self.kill, self.say, self.flags = heartbeat, kill, say, flags
        self.restarts = 0
        self._last_beat = None

    def _beat(self, child_pid, force=False):
        now = self.clock()
        if force or self._last_beat is None or now - self._last_beat >= HEARTBEAT_EVERY:
            self._last_beat = now
            try:
                self.heartbeat(child_pid)
            except Exception as e:              # a locked pid file must not take the service down
                self.say("pid file not written (%s)" % type(e).__name__)

    def _wait(self, seconds, child_pid=None):
        """Sleep in TICK steps so a stop request and the heartbeat are served; True if a stop was requested."""
        end = self.clock() + seconds
        while True:
            if self.stop_requested():
                return True
            self._beat(child_pid)
            left = end - self.clock()
            if left <= 0:
                return False
            self.sleep(min(TICK, left))

    def run(self):
        failures = 0
        while not self.stop_requested():
            started = self.clock()
            try:
                child = self.popen(self.argv, env=self.env, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                   close_fds=True, creationflags=self.flags)
            except OSError as e:
                self.say("could not start %s: %s" % (self.kind, e))
                child = None
            if child is not None:
                self.say("started %s (pid %s)" % (self.kind, child.pid))
                self._beat(child.pid, force=True)
                while child.poll() is None:
                    if self._wait(TICK, child.pid):
                        self.say("stopping %s" % self.kind)
                        self.kill(child.pid)
                        try:
                            child.wait(timeout=5)
                        except Exception:
                            pass
                        return 0
                self.say("%s exited with code %s" % (self.kind, child.returncode))
            failures = next_failures(failures, self.clock() - started)
            delay = backoff_delay(failures)
            self.restarts += 1
            self.say("restarting %s in %ds" % (self.kind, delay))
            if self._wait(delay):
                return 0
        return 0


# ---------------------------------------------------------------- process entry
def redirect_output(path):
    """pythonw.exe starts with sys.stdout and sys.stderr set to None, so the first print would crash. Point them at the log file (one
    shared, unbuffered, appending handle that is also handed to the child, so both write at the same file position). Returns the raw
    file object, or None when the log cannot be opened (output then goes to the null device)."""
    raw = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LOG_ROTATE_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        raw = open(path, "ab", buffering=0)
        out = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    except OSError:
        out = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = out
    return raw


def say(msg):
    print("%s supervisor: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def autostart(home=None):
    """Login entry point: one supervisor per installed service that is not already running."""
    for kind in installed_kinds():
        if not supervisor_running(kind):
            launch_detached(kind, home)
    return 0


def supervise(kind, home=None):
    from wowmentor import config
    raw = redirect_output(os.path.join(config.log_dir(), kind + ".log"))
    try:
        cfg = config.load()
        if kind == "server" and config.server_url(cfg):
            say("server_url is set: the server runs elsewhere, nothing to supervise here")
            return 0
        pidpath = pidfile_path(kind)
        created = time.time()

        def beat(child_pid):
            write_pidfile(pidpath, {"pid": os.getpid(), "kind": kind, "child": child_pid, "created": created, "heartbeat": time.time()})

        beat(None)
        stop = stopfile_path(kind)
        sup = Supervisor(kind, child_argv(kind), env=child_env(home=home), log=raw, stop_requested=lambda: os.path.exists(stop),
                         heartbeat=beat, say=say, flags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        say("supervising %s" % kind)
        try:
            return sup.run()
        finally:
            for p in (pidpath, stop):
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:                       # last resort: say why in the log instead of vanishing silently under pythonw
        say("fatal: %s: %s" % (type(e).__name__, e))
        return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    home = None
    if "--home" in argv:
        i = argv.index("--home")
        home = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
        if home:
            os.environ["WOWMENTOR_HOME"] = home
    if argv == ["--autostart"]:
        return autostart(home)
    if len(argv) == 1 and argv[0] in KINDS:
        return supervise(argv[0], home)
    print("usage: win32_supervisor.py (server|bridge|tunnel|--autostart) [--home DIR]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
