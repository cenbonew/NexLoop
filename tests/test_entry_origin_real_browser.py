"""S81 R67 in a real browser engine (headless Chromium via Playwright): the real enterWithTicket (apps/web Entries.tsx, bundled by
apps/web/test/browser/entry-origin.browser.mjs) posts the ticket form from a page served with the gateway's own Referrer-Policy
(deploy/stage/config/Caddyfile.template). Under no-referrer the browser sends Origin: null on that navigating POST, so the exact
Origin comparison refused every redemption on stage while the TestClient tests (which set Origin themselves) passed. The headers the
browser really sends must pass entry_tickets.same_origin_navigation; Origin null alone, or from another site, must not.

Opt-in lane like tests/test_sse_real_browser.py: marked `real_browser`, deselected unless `-m real_browser`. Needs Node 24 and the
pinned Playwright of apps/web with its Chromium headless shell. No database, no credentials; the ticket is a fixed synthetic value."""
import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexloop_eios import entry_tickets as E
from test_sse_real_browser import ROOT

pytestmark = pytest.mark.real_browser
SCRIPT = ROOT / 'apps/web/test/browser/entry-origin.browser.mjs'
TICKET = 'A' * 43


def gateway_referrer_policy():
    """The Referrer-Policy the stage gateway puts on every response (the workbench document included)."""
    found = re.findall(r'^\s*Referrer-Policy\s+(\S+)\s*$', (ROOT / 'deploy/stage/config/Caddyfile.template').read_text(), re.M)
    assert found == ['no-referrer'], found
    return found[0]


def node(*args):
    pnpm = shutil.which('pnpm')
    assert pnpm, 'pnpm (Node 24) is required for the real browser test'
    run = subprocess.run([pnpm, '--filter', '@nexloop/web', 'exec', 'node', str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]


def test_the_real_ticket_form_post_passes_the_origin_check(tmp_path):
    bundle = tmp_path / 'bundle'
    node('build', str(bundle))
    policy = gateway_referrer_policy()
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, kind, **extra):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Referrer-Policy', policy)
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/workbench/entries':
                self.send(200, b'<!doctype html><button id="enter" onclick="enter()">enter</button><script src="/entry.js"></script>', 'text/html')
            elif self.path == '/entry.js':
                self.send(200, (bundle / 'entry.js').read_bytes(), 'text/javascript')
            elif self.path == '/done':
                self.send(200, b'done', 'text/plain')
            else:
                self.send(404, b'', 'text/plain')

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if self.path == '/api/v1/workbench/auth/csrf':
                self.send(200, json.dumps({'csrf_token': 'synthetic'}).encode(), 'application/json')
            elif self.path == '/api/v1/workbench/entry-tickets':
                self.send(200, json.dumps({'target': 'webchat', 'action': '/api/v1/auth/entry', 'ticket': TICKET}).encode(), 'application/json')
            elif self.path == '/api/v1/auth/entry':
                posts.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send(303, b'', 'text/plain', Location='/done')
            else:
                self.send(404, b'', 'text/plain')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        node('drive', origin + '/workbench/entries')
    finally:
        server.shutdown()
    assert len(posts) == 1, posts
    headers, body = posts[0]
    assert body == b'ticket=' + TICKET.encode() and headers['content-type'] == 'application/x-www-form-urlencoded'
    # What R67 met: the browser hides the origin under the gateway's policy, but says itself that the navigation is same-origin.
    assert headers['origin'] == 'null' and headers['sec-fetch-site'] == 'same-origin' and headers['sec-fetch-mode'] == 'navigate'
    assert E.same_origin_navigation(headers, origin)
    assert not E.same_origin_navigation({**headers, 'sec-fetch-site': 'cross-site'}, origin)
    assert not E.same_origin_navigation({**headers, 'sec-fetch-site': 'same-site'}, origin)
    assert not E.same_origin_navigation({k: v for k, v in headers.items() if k != 'sec-fetch-site'}, origin)
    assert not E.same_origin_navigation({**headers, 'host': 'elsewhere.example.invalid'}, origin)
