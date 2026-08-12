"""swarmctl's forge transport, exercised against a real HTTP server on localhost.

WHY A SERVER AND NOT A MOCK. This transport was rewritten from `urllib.request` to `http.client`
while nobody could reach the real Gitea, and a mock of the layer under test would have agreed with
whatever it was rewritten into. A loopback server disagrees: it sees the actual request line, the
actual headers and the actual body, so "the path is assembled correctly" is measured rather than
asserted about a fake.

WHAT THE REWRITE WAS FOR, and why it is not merely a lint fix: `urllib.request` dispatches on the
URL's SCHEME and honours `file:`. A `--base-url` of `file:///etc` would have turned every API call
into a local read that still looked like a forge answering. `http.client` has no scheme handler to
reach, so the failure mode is absent rather than guarded -- and `test_a_non_http_base_url_is_refused`
pins the guard that remains.

THE SHAPE CHECKS ARE THE POINT OF THE TYPED HELPERS. Gitea answers some errors with a bare string
and some endpoints with an array; a cast would let that reach a subscript far away and surface as a
TypeError naming a local variable instead of an endpoint.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_swarm import swarmctl as _swarmctl


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


class _Recorder:
    """What the server saw, and what it should answer with."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        self.status = 200
        self.payload: object = {}
        self.netloc = ''


@pytest.fixture
def forge():
    """A loopback stand-in for the forge. Bound to port 0 so concurrent tests cannot collide."""
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            """Silence: the default handler writes every request to stderr."""

        def _respond(self) -> None:
            length = int(self.headers.get('Content-Length') or 0)
            recorder.seen.append(
                {
                    'method': self.command,
                    'path': self.path,
                    'auth': self.headers.get('Authorization'),
                    'content_type': self.headers.get('Content-Type'),
                    'body': json.loads(self.rfile.read(length)) if length else None,
                }
            )
            body = b'' if recorder.payload is None else json.dumps(recorder.payload).encode()
            self.send_response(recorder.status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _respond

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    recorder.netloc = f'127.0.0.1:{server.server_port}'
    yield recorder
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def provider(swarmctl, forge, monkeypatch):
    made = swarmctl.GiteaProvider(f'http://{forge.netloc}', 'TestOrg', None, None)
    monkeypatch.setattr(made, 'token', lambda: 'test-token')
    return made


# --------------------------------------------------------------------------- the request


def test_the_request_carries_method_path_auth_and_json(provider, forge):
    forge.payload = {'ok': True}
    provider.api_obj('POST', '/orgs/TestOrg/teams', {'name': 'Agents'})
    seen = forge.seen[-1]
    assert seen['method'] == 'POST'
    assert seen['path'] == '/api/v1/orgs/TestOrg/teams'
    assert seen['auth'] == 'token test-token'
    assert seen['content_type'] == 'application/json'
    assert seen['body'] == {'name': 'Agents'}


def test_a_get_sends_no_body(provider, forge):
    forge.payload = []
    provider.api_list('GET', '/orgs/TestOrg/teams')
    assert forge.seen[-1]['body'] is None


def test_a_base_url_with_a_path_prefix_keeps_it(swarmctl, forge, monkeypatch):
    """Gitea behind a sub-path (`http://host/gitea`) is a normal deployment. Dropping the prefix
    fails ONLY against those installs, which is the kind of bug that surfaces on someone else's
    server months later.
    """
    made = swarmctl.GiteaProvider(f'http://{forge.netloc}/gitea', 'TestOrg', None, None)
    monkeypatch.setattr(made, 'token', lambda: 't')
    forge.payload = []
    made.api_list('GET', '/orgs/TestOrg/teams')
    assert forge.seen[-1]['path'] == '/gitea/api/v1/orgs/TestOrg/teams'


# --------------------------------------------------------------------------- the response


def test_an_error_status_raises_and_names_the_endpoint(provider, forge, swarmctl):
    forge.status = 403
    forge.payload = {'message': 'nope'}
    with pytest.raises(swarmctl.Fail) as caught:
        provider.api_obj('GET', '/orgs/TestOrg/teams')
    assert '403' in str(caught.value)
    assert '/orgs/TestOrg/teams' in str(caught.value)


def test_an_allowed_status_is_not_an_error(provider, forge):
    """`destroy` deletes teams that may already be gone; a 404 there is the desired end state."""
    forge.status = 404
    forge.payload = {'message': 'gone'}
    provider.api('DELETE', '/teams/7', allow=(404,))  # must not raise


def test_a_status_not_in_allow_still_raises(provider, forge, swarmctl):
    forge.status = 500
    forge.payload = {'message': 'boom'}
    with pytest.raises(swarmctl.Fail):
        provider.api('DELETE', '/teams/7', allow=(404,))


def test_an_empty_body_reads_as_nothing_rather_than_a_crash(provider, forge):
    forge.payload = None
    assert provider.api_list('GET', '/teams/1/members') == []


def test_a_listing_helper_refuses_an_object(provider, forge, swarmctl):
    forge.payload = {'message': 'this is not a list'}
    with pytest.raises(swarmctl.Fail, match='expected a JSON array'):
        provider.api_list('GET', '/orgs/TestOrg/teams')


def test_an_object_helper_refuses_a_list(provider, forge, swarmctl):
    forge.payload = [{'id': 1}]
    with pytest.raises(swarmctl.Fail, match='expected a JSON object'):
        provider.api_obj('POST', '/orgs/TestOrg/teams', {'name': 'Agents'})


def test_an_unreachable_server_says_unreachable_not_a_traceback(swarmctl, monkeypatch):
    """Port 1 on loopback refuses instantly, so this measures the error path rather than a timeout."""
    made = swarmctl.GiteaProvider('http://127.0.0.1:1', 'TestOrg', None, None)
    monkeypatch.setattr(made, 'token', lambda: 't')
    with pytest.raises(swarmctl.Fail, match='unreachable'):
        made.api_list('GET', '/orgs/TestOrg/teams')


# --------------------------------------------------------------------------- the scheme guard


@pytest.mark.parametrize('bad', ['file:///etc/passwd', 'ftp://host/x', 'gopher://host', 'host:9000', 'http://', ''])
def test_a_non_http_base_url_is_refused_where_the_operator_typed_it(swarmctl, bad):
    with pytest.raises(swarmctl.Fail, match='--base-url'):
        swarmctl.GiteaProvider(bad, 'TestOrg', None, None)


@pytest.mark.parametrize('good', ['http://host:9000', 'https://forge.example.com', 'https://host/gitea'])
def test_http_and_https_are_accepted(swarmctl, good):
    assert swarmctl.GiteaProvider(good, 'TestOrg', None, None).scheme in {'http', 'https'}
