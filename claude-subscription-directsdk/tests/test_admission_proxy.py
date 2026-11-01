"""The relay's upstream hop honors HTTPS_PROXY/NO_PROXY like native does, via an HTTP CONNECT tunnel."""
import base64
import http.client
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admission import Admission


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith('_proxy') or name == 'REQUEST_METHOD':
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _proxy(upstream):
    gate = Admission(upstream, 1)
    try:
        return gate.proxy
    finally:
        gate.close()


@pytest.mark.parametrize(('host', 'no_proxy', 'expected'), [
    ('api.example.com', '', True),
    ('api.example.com', '.example.com', False),
    ('example.com', 'example.com', False),
    ('api.example.com', 'example.com:443', False),
    ('api.example.com', 'other.com, .example.com', False),
    ('api.example.com', 'other.com .example.com', False),
    ('api.example.com', '*', False),
    ('notexample.com', 'example.com', True),
])
def test_proxy_selection_by_no_proxy(clean_proxy_env, host, no_proxy, expected):
    clean_proxy_env.setenv('HTTPS_PROXY', 'http://proxy.test:8080')
    clean_proxy_env.setenv('NO_PROXY', no_proxy)
    assert (_proxy(f'https://{host}') is not None) is expected


def test_no_proxy_port_must_match_upstream_port(clean_proxy_env):
    clean_proxy_env.setenv('HTTPS_PROXY', 'http://proxy.test:8080')
    clean_proxy_env.setenv('NO_PROXY', 'api.example.com:443')
    assert _proxy('https://api.example.com:8443') is not None
    clean_proxy_env.setenv('NO_PROXY', 'api.example.com:8443')
    assert _proxy('https://api.example.com:8443') is None


def test_proxy_variables_follow_native_order(clean_proxy_env):
    assert _proxy('https://api.example.com') is None
    clean_proxy_env.setenv('ALL_PROXY', 'http://all.test:3128')  # native ignores ALL_PROXY
    assert _proxy('https://api.example.com') is None
    clean_proxy_env.setenv('HTTP_PROXY', 'http://plain.test:3128')
    assert _proxy('https://api.example.com') == ('plain.test', 3128, None)
    clean_proxy_env.setenv('HTTPS_PROXY', 'http://upper.test')
    assert _proxy('https://api.example.com') == ('upper.test', 80, None)
    if os.name != 'nt':  # Windows environment names are case-insensitive
        clean_proxy_env.setenv('https_proxy', 'http://lower.test:3128')
        assert _proxy('https://api.example.com')[0] == 'lower.test'


def test_dedicated_proxy_variable_precedes_generic_https_proxy(clean_proxy_env):
    clean_proxy_env.setenv('HTTPS_PROXY', 'http://generic.test:8080')
    clean_proxy_env.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY', 'http://dedicated.test:9090')
    assert _proxy('https://api.example.com') == ('dedicated.test', 9090, None)


def test_loopback_http_fixture_never_uses_the_proxy(clean_proxy_env):
    clean_proxy_env.setenv('HTTPS_PROXY', 'http://proxy.test:8080')
    clean_proxy_env.setenv('HTTP_PROXY', 'http://proxy.test:8080')
    assert _proxy('http://127.0.0.1:9') is None


@pytest.mark.parametrize('url', ['socks5://user:hunter2@proxy.test:1080', 'https://user:hunter2@proxy.test:8443',
                                 'http://user:hunter2@proxy.test:notaport'])
def test_unsupported_proxy_connects_directly_without_leaking_credentials(clean_proxy_env, url, caplog):
    """An unusable proxy keeps the pre-proxy behavior (direct connection) instead of breaking the relay."""
    clean_proxy_env.setenv('HTTPS_PROXY', url)
    with caplog.at_level('WARNING'):
        assert Admission('https://api.example.com', 1).proxy is None
    assert 'CONNECT proxy' in caplog.text and 'hunter2' not in caplog.text


def test_authenticated_connect_tunnels_to_the_upstream_host(clean_proxy_env):
    seen = {}
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_CONNECT(self):
            seen['target'] = self.path
            seen['headers'] = {k.lower(): v for k, v in self.headers.items()}
            # No upstream behind it: the TLS handshake that follows fails and the relay records it.
            self.send_response(200, 'Connection Established')
            self.end_headers()
            self.close_connection = True
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    clean_proxy_env.setenv('HTTPS_PROXY', f'http://user:p%40ss@127.0.0.1:{proxy.server_port}')
    gate = Admission('https://api.example.invalid/base', 5)
    try:
        port = int(gate.url.split(':')[2].split('/')[0])
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        conn.request('POST', gate.prefix + '/v1/messages', b'{}',
                     {'Content-Type': 'application/json', 'Authorization': 'Bearer subscription-secret'})
        try:
            conn.getresponse().read()
        except (OSError, http.client.HTTPException):
            pass
        conn.close()
        assert seen['target'] == 'api.example.invalid:443'
        assert seen['headers']['proxy-authorization'] == 'Basic ' + base64.b64encode(b'user:p@ss').decode()
        # The subscription bearer travels only inside TLS to the upstream host, never to the proxy.
        assert 'authorization' not in seen['headers']
        assert gate.failure  # TLS to api.example.invalid could not complete through the fake tunnel
    finally:
        gate.close()
        proxy.shutdown(); thread.join(); proxy.server_close()
