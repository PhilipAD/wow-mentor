"""Helpers shared by the host implementations. Standard library only; safe to import on every OS."""
import os, sys, time

from .base import Fs


def ensure_utf8_stdio():
    """Never crash on output: a console or pipe that cannot encode a character (emoji in `wowmentor doctor` on a legacy Windows
    code page, or redirected output) gets a replacement character instead of a UnicodeEncodeError. Keeps the stream's encoding."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is not None:
            try:
                reconf(errors="replace")
            except (ValueError, OSError):
                pass


def scan_wow_roots(roots):
    """Client folders under each root that have an Interface/AddOns folder, Forever first, then by path. Missing roots are skipped."""
    found = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if os.path.isdir(os.path.join(p, "Interface", "AddOns")):
                found.append(p)
    return sorted(found, key=lambda p: (0 if "forever" in p.lower() else 1, p))


def replace_with_retry(src, dst, tries=6, delay=0.05, sleep=time.sleep):
    """os.replace, retried a few times when another process holds the destination open (Windows PermissionError winerror 5 or 32).
    On POSIX a PermissionError is real, so it is raised at once."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            if getattr(e, "winerror", None) not in (5, 32) or attempt == tries - 1:
                raise
            sleep(delay * (attempt + 1))


def write_private_file(path, data=None, mode=0o600, replace=replace_with_retry, writer=None):
    """Atomic private write (see base.Fs.write_private): temp file created with `mode`, fsync, replace. Give bytes `data`, or a
    `writer(f)` that writes text (UTF-8, "\\n" line ends) to the open temp file."""
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
        with (os.fdopen(fd, "wb") if writer is None else os.fdopen(fd, "w", encoding="utf-8", newline="\n")) as f:
            if writer is None:
                f.write(data)
            else:
                writer(f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class CommonFs(Fs):
    """The real file operations; every host uses this unless it needs something special."""

    def atomic_replace(self, src, dst):
        replace_with_retry(src, dst)

    def write_private(self, path, data, mode=0o600):
        write_private_file(path, data, mode, self.atomic_replace)
