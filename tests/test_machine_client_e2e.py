"""Machine SDK and stdio against the governed application, loopback TLS and disposable PG."""
import dataclasses
import json
import logging
import os
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import uvicorn

from nexloop_eios.http_api import create_app
from test_machine_lifecycle_pg import desk  # noqa: F401
from test_machine_reads_pg import grant
from test_object_notes_pg import CAP, limits, notes, rows  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

SDK = Path(__file__).resolve().parents[1] / "packages/machine-client/src"
with patch.object(sys, "path", [str(SDK), *sys.path]):
    from nexloop_machine import Client, MachineError


@pytest.fixture
def endpoint(notes, tmp_path, monkeypatch):
    assert grant(notes, "object.get", ["Commitment"]).status_code == 200
    limits(notes["admin"])
    config = notes["config"]
    pepper = notes["peppers_file"]
    app = create_app(dataclasses.replace(config, machine_key_peppers_file=pepper,
                     workbench=dataclasses.replace(config.workbench, machine_key_peppers_file=pepper)))
    cert, private = tmp_path / "cert.pem", tmp_path / "tls.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(private), "-out", str(cert)], check=True, capture_output=True)
    key = tmp_path / "machine.key"
    with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(notes["key"])
    paths = []
    dropped = []

    async def application(scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            paths.append((scope["method"], path))
            if path == "/machine/v1/commit" and not dropped:
                # Run the entire real handler first. Drop its successful response at the
                # H11 transport before any response headers reach the SDK.
                messages = []

                async def collect(message):
                    messages.append(message)

                await app(scope, receive, collect)
                assert messages[0]["status"] == 200
                assert not messages[-1].get("more_body", False)
                body = b"".join(m["body"] for m in messages if m["type"] == "http.response.body")
                outcome_id = json.loads(body)["outcome_id"]
                receive.__self__.transport.close()
                dropped.append(outcome_id)
                return
        await app(scope, receive, send)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"https://127.0.0.1:{listener.getsockname()[1]}"
        # This server shares the pytest worker. Keep its transport logs private
        # without replacing the worker's handlers, levels or propagation settings.
        for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
            monkeypatch.setattr(logging.getLogger(name), "disabled", True)
        server = uvicorn.Server(uvicorn.Config(application, http="h11", ssl_certfile=cert,
                                               ssl_keyfile=private, log_config=None))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while thread.is_alive() and not server.started and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started
            yield url, cert, key, paths, dropped
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def test_machine_client_end_to_end(notes, endpoint, capfd):
    url, cert, key, paths, dropped = endpoint
    stdio = [sys.executable, "-B", "-m", "nexloop_machine.stdio", "--base-url", url, "--ca-file", str(cert)]
    env = {"PYTHONPATH": str(SDK), "NEXLOOP_MACHINE_KEY_FILE": str(key)}
    key.chmod(0o644)
    with pytest.raises(MachineError, match="Invalid machine key file"):
        Client(url, key, cert)
    help_result = subprocess.run([*stdio, "--help"], env=env, capture_output=True, text=True, timeout=10)
    assert help_result.returncode == 0
    refused = subprocess.run(stdio, env=env, input="", capture_output=True, text=True, timeout=10)
    assert refused.returncode == 2 and not paths
    key.chmod(0o600)
    client = Client(url, key, cert)
    caps = client.capabilities()["capabilities"]
    target = {"type_name": "Commitment", "object_id": notes["object"]}
    assert client.read("object.get", **target)["object_id"] == notes["object"]
    intent = "sdk-lost-response"
    params = dict(target, text="synthetic SDK note")
    prepared = client.prepare(CAP, intent, **params)
    paths.clear()
    committed = client.commit(CAP, intent, prepared["prepare_digest"])
    assert paths == [("POST", "/machine/v1/commit"), ("GET", "/machine/v1/intents/" + intent)]
    assert committed["intent_id"] == intent and committed["phase"] == "committed"
    assert dropped == [committed["outcome_id"]]
    assert [row[2] for row in rows(notes["admin"])] == [params["text"]]

    replies = []
    with subprocess.Popen(stdio, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        def rpc(method, args):
            ident = len(replies) + 1
            proc.stdin.write(json.dumps(dict(jsonrpc="2.0", id=ident, method=method, params=args)) + "\n")
            proc.stdin.flush()
            assert select.select([proc.stdout], [], [], 10)[0], "stdio response timed out"
            raw = proc.stdout.readline()
            replies.append(raw)
            response = json.loads(raw)
            assert response["id"] == ident
            return response

        def call(name, **arguments):
            response = rpc("tools/call", dict(name=name, arguments=arguments))
            return json.loads(response["result"]["content"][0]["text"])

        try:
            assert "tools" in rpc("initialize", {})["result"]["capabilities"]
            tools = rpc("tools/list", {})["result"]["tools"]
            expected = {name for cap in caps for name in (
                [cap["capability"]] if cap["kind"] == "read"
                else [cap["capability"] + ".prepare", cap["capability"] + ".commit"])}
            assert len(tools) == len(expected) and {tool["name"] for tool in tools} == expected
            assert call("object.get", params=target)["object_id"] == notes["object"]
            other = "stdio-confirm-note"
            p = call(CAP + ".prepare", intent_id=other, params=dict(target, text="synthetic stdio note"))
            assert len(rows(notes["admin"])) == 1
            done = call(CAP + ".commit", intent_id=other, prepare_digest=p["prepare_digest"])
            assert done["phase"] == "committed" and done["intent_id"] == other
            assert len(rows(notes["admin"])) == 2
            assert "error" in rpc("tools/call", dict(name="not.a.tool", arguments={}))
        finally:
            proc.stdin.close()
            if proc.poll() is None:
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        stderr = proc.stderr.read()
        assert proc.returncode == 0
    captured = capfd.readouterr()
    outputs = [help_result.stdout, help_result.stderr, refused.stdout, refused.stderr, *replies, stderr, captured.out, captured.err]
    if any(notes["key"] in output for output in outputs):
        pytest.fail("machine bearer appeared in process output", pytrace=False)
