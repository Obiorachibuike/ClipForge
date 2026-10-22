"""Server-side request forgery (SSRF) guards.

The deployment can reach a private network: PostgreSQL, Redis, an S3/MinIO
endpoint, cloud metadata (`169.254.169.254`) and, on some platforms, internal
admin services. Anything that fetches a user-supplied URL must therefore refuse
to connect to a non-public address — even when the hostname looks acceptable.

This module is deliberately dependency-free (standard library only) so both the
upload URL importer and any future fetcher can share exactly one policy.

What is enforced
----------------
* only ``http`` and ``https`` URLs, on the default ports;
* no embedded credentials;
* every A/AAAA record must be globally routable (private, loopback, link-local,
  multicast, reserved, unspecified and shared address space are refused);
* redirects are re-validated hop by hop when used with :class:`SafeRedirectHandler`
  (a redirect is exactly how an allow-listed URL becomes an internal request).

DNS rebinding note: validating a hostname and then letting a lower layer resolve
it again leaves a small time-of-check/time-of-use window. The guarded yt-dlp
handler in ``services/remote_video.py`` validates *every* request (initial and
redirected), which closes the practical path. Pinning the resolved IP into the
connection would require a custom socket layer; that is not implemented.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.request
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})


class SSRFBlocked(ValueError):
    """A URL or hostname was refused because it could reach a private network."""

    def __init__(self, message: str, *, host: str = "") -> None:
        super().__init__(message)
        self.host = host


def is_public_ip(value: str) -> bool:
    """True only for a globally routable unicast address."""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global) and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def resolve_host(host: str, port: int = 443) -> list[str]:
    """Resolve every address for ``host``, or raise :class:`SSRFBlocked`.

    A resolution failure is a refusal rather than a pass: the caller must not
    continue with an unchecked host just because DNS was unavailable.
    """
    host = (host or "").strip().rstrip(".")
    if not host:
        raise SSRFBlocked("The URL has no host.", host=host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise SSRFBlocked(f"Could not resolve '{host}'.", host=host) from exc
    addresses = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SSRFBlocked(f"Could not resolve '{host}'.", host=host)
    return addresses


def _host_in_allowlist(host: str, allowed_hosts: tuple[str, ...] | list[str] | None) -> bool:
    if not allowed_hosts:
        return True
    lowered = host.lower().rstrip(".")
    for suffix in allowed_hosts:
        suffix = suffix.lower().lstrip(".").rstrip(".")
        if lowered == suffix or lowered.endswith(f".{suffix}"):
            return True
    return False


def assert_public_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] | None = None,
    resolve: bool = True,
) -> str:
    """Validate an outbound URL and return it unchanged.

    Raises :class:`SSRFBlocked` with a human-safe message on refusal. Passing
    ``allowed_hosts`` additionally restricts the initial hostname (redirects
    should not use that restriction: media CDNs legitimately live elsewhere).
    """
    raw = str(url or "").strip()
    if not raw:
        raise SSRFBlocked("The URL is empty.")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise SSRFBlocked("The URL is malformed.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlocked("Only HTTP and HTTPS URLs are allowed.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise SSRFBlocked("The URL has no host.")
    if parsed.username or parsed.password:
        raise SSRFBlocked("URLs with embedded credentials are not allowed.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFBlocked("The URL has an invalid port.", host=host) from exc
    effective_port = port or (443 if scheme == "https" else 80)
    if effective_port not in ALLOWED_PORTS:
        raise SSRFBlocked("Only the standard HTTP/HTTPS ports are allowed.", host=host)

    if not _host_in_allowlist(host, allowed_hosts):
        raise SSRFBlocked("That host is not supported.", host=host)

    # An IP literal must itself be public; this catches http://[::1]/ and
    # decimal/hex spellings before any socket is opened.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_ip(str(literal)):
        raise SSRFBlocked("That address is not a public internet address.", host=host)

    if resolve:
        for address in resolve_host(host, effective_port):
            if not is_public_ip(address):
                raise SSRFBlocked(
                    "That host resolves to a private or reserved address.", host=host
                )
    return raw


def safe_host_for_logs(url: str) -> str:
    """Host only — never log credentials or query strings from user URLs."""
    try:
        return (urlsplit(url).hostname or "")[:255]
    except ValueError:
        return ""


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that rejects redirects to private addresses.

    urllib follows 30x redirects inside ``opener.open`` with no application hook,
    so without this an allow-listed URL could redirect to ``127.0.0.1``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)
