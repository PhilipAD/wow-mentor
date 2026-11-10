"""HTTP helpers for the bridge's calls to the WoW Mentor server.

A server on this Mac (127.0.0.1) must be reached directly: macOS system proxy settings (a VPN, a debugging proxy) would otherwise route
`http://127.0.0.1/...` through the proxy, which then sees the secret in the URL and usually cannot reach loopback anyway. Redirects are not
followed for loopback either, so a misconfigured port cannot bounce a request (and its secret) elsewhere. A remote `server_url` keeps the
default behaviour (system proxy, redirects), because that is a deliberate configuration.
"""
import urllib.parse, urllib.request

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def urlopen(req, timeout=10):
    """urllib.request.urlopen replacement: direct and redirect-free for loopback, the default opener for anything else."""
    url = req if isinstance(req, str) else req.full_url
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        host = None
    if host in LOOPBACK:
        return _direct.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
