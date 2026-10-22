"""SSRF policy: URL import must never reach the deployment's private network."""
from __future__ import annotations

import socket
import urllib.request

import pytest
from app.core.ssrf import (
    SafeRedirectHandler,
    SSRFBlocked,
    assert_public_url,
    is_public_ip,
    resolve_host,
)

PUBLIC_IPS = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"]
BLOCKED_IPS = [
    "127.0.0.1",
    "127.1.2.3",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.10",
    "169.254.169.254",  # cloud metadata
    "169.254.1.1",
    "100.64.0.1",  # carrier-grade NAT / shared space
    "0.0.0.0",
    "224.0.0.1",
    "240.0.0.1",
    "::1",
    "fe80::1",
    "fd00::1",
    "::ffff:127.0.0.1",
]


@pytest.mark.parametrize("address", PUBLIC_IPS)
def test_public_addresses_are_allowed(address):
    assert is_public_ip(address) is True


@pytest.mark.parametrize("address", BLOCKED_IPS)
def test_private_loopback_link_local_and_metadata_are_blocked(address):
    assert is_public_ip(address) is False


def _resolver(*answers):
    def fake(host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))
            for address in answers
        ]

    return fake


def test_url_to_metadata_address_is_blocked_before_any_request():
    with pytest.raises(SSRFBlocked):
        assert_public_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(SSRFBlocked):
        assert_public_url("http://[::1]:80/")
    with pytest.raises(SSRFBlocked):
        assert_public_url("http://127.0.0.1/")


def test_non_http_schemes_and_credentials_are_blocked():
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/video.mp4",
        "gopher://example.com/",
        "https://user:secret@youtube.com/watch?v=abc",
    ):
        with pytest.raises(SSRFBlocked):
            assert_public_url(url, resolve=False)


def test_non_standard_ports_are_blocked():
    with pytest.raises(SSRFBlocked):
        assert_public_url("https://youtube.com:8443/watch?v=abc", resolve=False)
    # The default port is implicit and allowed.
    assert_public_url("https://youtube.com/watch?v=abc", resolve=False)


def test_allowlist_does_not_accept_suffix_confusion():
    with pytest.raises(SSRFBlocked):
        assert_public_url(
            "https://youtube.com.evil.example/watch?v=abc",
            allowed_hosts=("youtube.com",),
            resolve=False,
        )
    # A real subdomain of an allowed host is fine.
    assert_public_url(
        "https://www.youtube.com/watch?v=abc",
        allowed_hosts=("youtube.com",),
        resolve=False,
    )


def test_hostname_resolving_to_a_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("10.1.2.3"))
    with pytest.raises(SSRFBlocked):
        assert_public_url("https://video.example.com/watch")


def test_mixed_public_and_private_answers_are_blocked(monkeypatch):
    """A rebinding answer can contain both; refuse the host entirely."""
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34", "127.0.0.1"))
    with pytest.raises(SSRFBlocked):
        assert_public_url("https://video.example.com/watch")


def test_public_hostname_passes_and_resolution_failure_is_a_refusal(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    assert assert_public_url("https://video.example.com/watch") == "https://video.example.com/watch"

    def broken(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    with pytest.raises(SSRFBlocked):
        assert_public_url("https://video.example.com/watch")
    with pytest.raises(SSRFBlocked):
        resolve_host("video.example.com")


def test_redirect_handler_blocks_a_hop_to_private_space(monkeypatch):
    handler = SafeRedirectHandler()
    request = urllib.request.Request("https://video.example.com/watch")
    with pytest.raises(SSRFBlocked):
        handler.redirect_request(request, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/")

    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://cdn.example.com/video.mp4"
    )
    assert redirected is not None
    assert redirected.full_url == "https://cdn.example.com/video.mp4"


def test_guarded_ytdlp_uses_the_ssrf_handler():
    """The downloader must not fall back to a stock urllib redirect handler."""
    yt_dlp = pytest.importorskip("yt_dlp")
    from app.services.remote_video import _guarded_youtube_dl

    guarded = _guarded_youtube_dl(yt_dlp)
    assert issubclass(guarded, yt_dlp.YoutubeDL)
    if guarded is yt_dlp.YoutubeDL:  # pragma: no cover - internal API changed
        pytest.skip("yt-dlp internals changed; preflight still applies")
    with guarded({"quiet": True, "no_warnings": True}) as downloader:
        director = downloader._request_director
        urllib_handler = director.handlers["Urllib"]
        assert any(
            isinstance(handler, SafeRedirectHandler)
            for handler in urllib_handler._get_instance(
                proxies={}, cookiejar=downloader.cookiejar
            ).handlers
        )
