"""Webhook delivery and DNS rebinding.

``_deliver_webhook`` checks the URL with ``assert_public_url`` and then POSTs.
If the POST resolves the host again on a thread where ``ssrf_guard`` is not
active, a DNS answer that changes between the two lookups (public at check
time, private at connect time) reaches an internal address. The delivery must
connect only to an address that passed the public-IP check.
"""
import asyncio
import http.server
import socket
import threading

import pytest

import app
import ssrf_guard

PUBLIC_IP = "93.184.216.34"


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):  # noqa: N802
        type(self).hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def internal_server():
    _Recorder.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def _rebinding_resolver(monkeypatch, private_ip):
    """First lookup of rebind.test answers a public IP, every later one a private IP."""
    ssrf_guard.install()
    real = ssrf_guard._original_getaddrinfo
    calls = {"n": 0}

    def fake(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if name != "rebind.test":
            return real(host, port, *args, **kwargs)
        calls["n"] += 1
        ip = PUBLIC_IP if calls["n"] == 1 else private_ip
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, int(port or 80), 0, 0) if family == socket.AF_INET6 else (ip, int(port or 80))
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr(ssrf_guard, "_original_getaddrinfo", fake)
    monkeypatch.setattr(app, "WEBHOOK_RETRY_DELAYS", (0,))
    return calls


def _deliver(url):
    asyncio.run(app._deliver_webhook(url, b"{}", None))


def test_rebinding_to_a_private_ipv4_is_not_delivered(monkeypatch, internal_server, capsys):
    calls = _rebinding_resolver(monkeypatch, "127.0.0.1")
    _deliver(f"http://rebind.test:{internal_server}/hook")
    assert _Recorder.hits == [], "webhook reached an internal address after a DNS rebind"
    assert calls["n"] >= 2, "the connect-time lookup did not happen"
    assert "no longer safe" in capsys.readouterr().out


@pytest.mark.parametrize("private_ip", ["::1", "fd00::1", "169.254.169.254"])
def test_rebinding_to_other_private_addresses_is_dropped(monkeypatch, internal_server, capsys, private_ip):
    _rebinding_resolver(monkeypatch, private_ip)
    _deliver(f"http://rebind.test:{internal_server}/hook")
    assert _Recorder.hits == []
    assert "no longer safe" in capsys.readouterr().out


def test_guarded_post_still_delivers_to_an_allowed_host(monkeypatch, internal_server):
    # Hosts the deployment allows on purpose keep working through the guarded client.
    ssrf_guard.install()
    real = ssrf_guard._original_getaddrinfo

    def fake(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if name == "hooks.internal.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", int(port)))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(ssrf_guard, "_original_getaddrinfo", fake)
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", "hooks.internal.test")
    resp = app._post_webhook_guarded(f"http://hooks.internal.test:{internal_server}/hook", b"{}", {})
    assert resp.status_code == 200
    assert _Recorder.hits == ["/hook"]
