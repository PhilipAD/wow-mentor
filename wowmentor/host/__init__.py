"""The host layer (see wowmentor/host/base.py and docs/DESIGN-host.md).

    from wowmentor import host
    host.get().capture.find_game()
    with host.using(host.fake.FakeHost()): ...      # tests

`get()` picks the host from sys.platform ('darwin' -> darwin, 'win32' -> win32 when that module exists, anything else -> unsupported).
The environment variable WOWMENTOR_HOST=fake|darwin|win32|unsupported overrides the choice. Importing this package imports no native binding.
"""
import contextlib, importlib, importlib.util, os, sys

from .base import Host, HostUnsupported, KINDS, TEXT_DEFAULTS  # noqa: F401

NAMES = ("fake", "darwin", "win32", "unsupported")
_override = []              # stack of hosts installed by using()
_made = {}                  # name -> Host, built on first use


def selected_name():
    env = os.environ.get("WOWMENTOR_HOST", "").strip().lower()
    if env:
        if env not in NAMES:
            raise HostUnsupported("WOWMENTOR_HOST=%r is not one of %s" % (env, ", ".join(NAMES)))
        name = env
    elif sys.platform == "darwin":
        name = "darwin"
    elif sys.platform == "win32":
        name = "win32"
    else:
        name = "unsupported"
    if name == "win32" and importlib.util.find_spec(__name__ + ".win32") is None:
        name = "unsupported"            # the Windows host is not shipped yet
    return name


def get():
    """The current host. Cheap: the module import and the object are cached per name."""
    if _override:
        return _override[-1]
    name = selected_name()
    h = _made.get(name)
    if h is None:
        h = _made[name] = importlib.import_module("%s.%s" % (__name__, name)).make()
    return h


@contextlib.contextmanager
def using(h):
    """Make `h` the current host inside the block (tests)."""
    _override.append(h)
    try:
        yield h
    finally:
        _override.pop()
