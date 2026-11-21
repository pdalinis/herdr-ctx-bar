import base64
from collections.abc import Callable
from contextlib import contextmanager, suppress
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


MODULE_PATH = Path(__file__).parents[1] / "bin" / "opencode_v2.py"
with mock.patch.object(sys, "path", [str(MODULE_PATH.parent), *sys.path]):
    opencode = importlib.import_module("opencode_v2")


TOKENS = {
    "input": 1000,
    "output": 200,
    "reasoning": 50,
    "cache": {"read": 8000, "write": 500},
}


def assistant(identifier="a1", tokens=None, model="coding"):
    return {
        "id": identifier,
        "type": "assistant",
        "model": {"providerID": "custom", "id": model},
        "tokens": TOKENS if tokens is None else tokens,
    }


class Fixture:
    def __init__(self):
        self.session: dict[str, Any] = {
            "id": "s1",
            "location": {"directory": "/project one"},
        }
        self.pages: dict[str | None, dict[str, Any]] = {
            None: {"data": [assistant()], "cursor": {}}
        }
        self.models = [
            {"providerID": "custom", "id": "coding", "limit": {"context": 200000}}
        ]
        self.requests = []
        self.override: Callable[[str, Any], tuple[int, object] | None] | None = None
        self.password = uuid4().hex
        self.drip: str | None = None

    def response(self, path, headers):
        self.requests.append((path, headers.get("Authorization")))
        if self.override:
            value = self.override(path, headers)
            if value is not None:
                return value
        expected = (
            "Basic " + base64.b64encode(f"opencode:{self.password}".encode()).decode()
        )
        if headers.get("Authorization") != expected:
            return 401, {}
        url = urlsplit(path)
        if url.path == "/api/info":
            return 200, {"pid": 123, "version": "2.0.32"}
        if url.path == "/api/session/s1":
            return 200, {"data": self.session}
        if url.path == "/api/session/s1/message":
            query = parse_qs(url.query)
            cursor = query.get("cursor", [None])[0]
            if cursor is not None and "order" in query:
                return 400, {}
            return 200, self.pages[cursor]
        if url.path == "/api/model":
            return 200, {"location": self.session["location"], "data": self.models}
        return 404, {}


@contextmanager
def service(fixture, *, tls=None, host="127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, data = fixture.response(self.path, self.headers)
            raw = json.dumps(data).encode()
            if fixture.drip == "headers":
                with suppress(BrokenPipeError, ConnectionResetError):
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n":
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.02)
                return
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                if fixture.drip == "body":
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.02)
                    return
                self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "service.json"
        path.write_text(
            json.dumps(
                {
                    "url": f"{'https' if tls is not None else 'http'}://{host}:{server.server_port}",
                    "id": "instance-one",
                    "pid": 123,
                    "version": "2.0.32",
                    "password": fixture.password,
                }
            )
        )
        try:
            yield path
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CollectorTests(unittest.TestCase):
    def collect(self, fixture, **limits):
        with service(fixture) as path:
            registration = opencode.Registration.read(path)
            return opencode.collect(registration, "s1", **limits)

    def test_https_verifies_certificate_trust_and_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / "cert.pem", root / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            for host, trusted, expected in [
                (
                    "localhost",
                    True,
                    opencode.Result((9750, 200000), opencode.Reason.AVAILABLE, "a1"),
                ),
                ("localhost", False, opencode.Result(reason=opencode.Reason.TRANSPORT)),
                ("127.0.0.1", True, opencode.Result(reason=opencode.Reason.TRANSPORT)),
            ]:
                with self.subTest(host=host, trusted=trusted):
                    fixture = Fixture()
                    context = ssl.create_default_context(
                        cafile=str(cert) if trusted else None
                    )
                    with (
                        mock.patch.object(
                            ssl, "create_default_context", return_value=context
                        ),
                        service(fixture, tls=tls, host=host) as path,
                    ):
                        result = opencode.collect(
                            opencode.Registration.read(path), "s1"
                        )
                    self.assertEqual(result, expected)
                    if expected.reason != opencode.Reason.AVAILABLE:
                        self.assertEqual(fixture.requests, [])

    def test_effective_limit_and_all_token_buckets(self):
        fixture = Fixture()
        fixture.session["model"] = {"providerID": "other", "id": "new-model"}
        fixture.models.insert(
            0, {"providerID": "other", "id": "coding", "limit": {"context": 1}}
        )
        result = self.collect(fixture)
        self.assertEqual(
            result, opencode.Result((9750, 200000), opencode.Reason.AVAILABLE, "a1")
        )
        model_path = next(p for p, _ in fixture.requests if p.startswith("/api/model"))
        self.assertEqual(
            parse_qs(urlsplit(model_path).query),
            {"location[directory]": ["/project one"]},
        )

    def test_selection_boundaries_and_zero_usage(self):
        zero = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        input_only = dict(zero, input=100)
        for messages, revert, expected in [
            ([assistant("zero", zero), assistant()], None, None),
            ([assistant("input", input_only)], None, (100, 200000)),
            ([assistant("reasoning", dict(zero, reasoning=100))], None, (100, 200000)),
            (
                [assistant("cache", dict(zero, cache={"read": 100, "write": 0}))],
                None,
                (100, 200000),
            ),
            (
                [{"type": "assistant", "id": "pending"}, assistant()],
                None,
                (9750, 200000),
            ),
            (
                [{"id": "c", "type": "compaction", "status": "completed"}, assistant()],
                None,
                None,
            ),
            (
                [{"id": "c", "type": "compaction", "status": "running"}, assistant()],
                None,
                (9750, 200000),
            ),
            (
                [{"id": "c", "type": "compaction", "status": "failed"}, assistant()],
                None,
                (9750, 200000),
            ),
            (
                [assistant("new"), {"id": "boundary", "type": "user"}, assistant()],
                "boundary",
                (9750, 200000),
            ),
            ([assistant()], "missing", None),
        ]:
            with self.subTest(messages=messages, revert=revert):
                fixture = Fixture()
                fixture.pages[None]["data"] = messages
                if revert:
                    fixture.session["revert"] = {"messageID": revert}
                expected_result = (
                    opencode.Result(
                        expected, opencode.Reason.AVAILABLE, messages[-1]["id"]
                    )
                    if expected
                    else opencode.Result()
                )
                self.assertEqual(self.collect(fixture), expected_result)

    def test_pagination_preserves_revert_and_compaction_state(self):
        fixture = Fixture()
        fixture.session["revert"] = {"messageID": "boundary"}
        fixture.pages = {
            None: {"data": [assistant("excluded")], "cursor": {"next": "page two +/="}},
            "page two +/=": {
                "data": [{"id": "boundary", "type": "user"}],
                "cursor": {"next": "three"},
            },
            "three": {"data": [assistant()], "cursor": {"next": "last"}},
        }
        self.assertEqual(
            self.collect(fixture),
            opencode.Result((9750, 200000), opencode.Reason.AVAILABLE, "a1"),
        )
        self.assertFalse(any("last" in p for p, _ in fixture.requests))
        fixture.pages["three"]["data"] = [{"type": "compaction", "status": "completed"}]
        self.assertEqual(self.collect(fixture).reason, opencode.Reason.NO_USAGE)

    def test_invalid_usage_and_missing_models_fail_closed(self):
        for field, value in [
            ("input", -1),
            ("input", True),
            ("input", "100"),
            ("cache", {}),
        ]:
            with self.subTest(field=field, value=value):
                fixture = Fixture()
                fixture.pages[None]["data"][0]["tokens"] = dict(
                    TOKENS, **{field: value}
                )
                self.assertEqual(
                    self.collect(fixture).reason, opencode.Reason.INVALID_RESPONSE
                )
        fixture = Fixture()
        fixture.models = []
        self.assertEqual(self.collect(fixture).reason, opencode.Reason.MISSING_LIMIT)

    def test_http_and_identity_errors_are_sanitised(self):
        cases = [
            (
                "/api/info",
                200,
                {"pid": 999, "version": "2.0.32"},
                opencode.Reason.DISCOVERY,
            ),
            ("/api/info", 503, {}, opencode.Reason.DISCOVERY),
            (
                "/api/info",
                200,
                {"pid": 123, "version": "3.0.0"},
                opencode.Reason.COMPATIBILITY,
            ),
            ("/api/info", 401, {}, opencode.Reason.AUTHENTICATION),
            ("/api/info", 302, {}, opencode.Reason.TRANSPORT),
            ("/api/session/s1", 404, {}, opencode.Reason.SESSION),
            (
                "/api/session/s1",
                200,
                {"data": {"id": "s2"}},
                opencode.Reason.INVALID_RESPONSE,
            ),
        ]
        for path, status, body, reason in cases:
            with self.subTest(path=path, status=status, reason=reason):
                fixture = Fixture()
                fixture.override = lambda p, h, path=path, status=status, body=body: (
                    (status, body) if p == path else None
                )
                result = self.collect(fixture)
                self.assertEqual(result, opencode.Result(reason=reason))
                self.assertNotIn(fixture.password, repr(result))

    def test_limits_and_registration_replacement(self):
        fixture = Fixture()
        fixture.pages = {
            None: {"data": [{"type": "user"}], "cursor": {"next": "repeat"}},
            "repeat": {"data": [{"type": "user"}], "cursor": {"next": "repeat"}},
        }
        self.assertEqual(
            self.collect(fixture, max_pages=1).reason, opencode.Reason.BUDGET
        )
        self.assertEqual(self.collect(fixture).reason, opencode.Reason.INVALID_RESPONSE)
        self.assertEqual(
            self.collect(Fixture(), max_bytes=10).reason, opencode.Reason.BUDGET
        )
        fixture = Fixture()
        with service(fixture) as path:
            registration = opencode.Registration.read(path)

            def replace(p, headers):
                if p.startswith("/api/model"):
                    value = json.loads(path.read_text())
                    value["id"] = "replacement"
                    path.write_text(json.dumps(value))

            fixture.override = replace
            self.assertEqual(
                opencode.collect(registration, "s1").reason, opencode.Reason.CHANGED
            )

    def test_cold_or_stalled_service_respects_deadline(self):
        fixture = Fixture()

        def delay(path, headers):
            time.sleep(0.3)

        fixture.override = delay
        with service(fixture) as path:
            start = time.monotonic()
            result = opencode.collect(
                opencode.Registration.read(path), "s1", timeout=0.12
            )
            elapsed = time.monotonic() - start
        self.assertEqual(result.reason, opencode.Reason.BUDGET)
        self.assertLess(elapsed, 0.6)

    def test_slow_drip_headers_and_body_have_wall_clock_deadlines(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                fixture = Fixture()
                fixture.drip = phase
                with service(fixture) as path:
                    start = time.monotonic()
                    result = opencode.collect(
                        opencode.Registration.read(path), "s1", request_timeout=0.12
                    )
                    elapsed = time.monotonic() - start
                self.assertEqual(result.reason, opencode.Reason.BUDGET)
                self.assertLess(elapsed, 0.6)

    def test_cycle_shares_health_and_effective_models(self):
        fixture = Fixture()
        with service(fixture) as path:
            registration = opencode.Registration.read(path)
            cycle = opencode.Cycle()
            with mock.patch.dict(
                "os.environ",
                {
                    "HTTP_PROXY": "http://192.0.2.1:1",
                    "HTTPS_PROXY": "http://192.0.2.1:1",
                },
            ):
                results = [
                    opencode.collect(registration, "s1", cycle=cycle) for _ in range(2)
                ]
        self.assertEqual([result.usage for result in results], [(9750, 200000)] * 2)
        self.assertEqual(sum(p == "/api/info" for p, _ in fixture.requests), 1)
        self.assertEqual(
            sum(p.startswith("/api/model") for p, _ in fixture.requests), 1
        )

    def test_session_revalidation_catches_location_and_revert_changes(self):
        for change in (
            {"location": {"directory": "/changed"}},
            {"revert": {"messageID": "a1"}},
        ):
            with self.subTest(change=change):
                fixture = Fixture()
                original = fixture.session.copy()
                calls = 0

                def update(path, headers, original=original, change=change):
                    nonlocal calls
                    if path == "/api/session/s1":
                        calls += 1
                        return 200, {
                            "data": original if calls == 1 else dict(original, **change)
                        }

                fixture.override = update
                self.assertEqual(self.collect(fixture).reason, opencode.Reason.CHANGED)

    def test_empty_password_and_optional_registration_fields(self):
        fixture = Fixture()
        fixture.password = ""
        with service(fixture) as path:
            registration = json.loads(path.read_text())
            del registration["id"]
            del registration["version"]
            path.write_text(json.dumps(registration))
            self.assertEqual(
                opencode.collect(opencode.Registration.read(path), "s1").usage,
                (9750, 200000),
            )


class HTTPWatcherTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(sys, "path", [str(MODULE_PATH.parent), *sys.path]):
            self.ctx = importlib.import_module("ctx_bar")

    def watcher(self):
        watcher = self.ctx.Watcher("synthetic-herdr-socket")
        watcher.client.request = mock.Mock(
            return_value={"result": {"type": "pong", "version": "0.9.5"}}
        )
        self.addCleanup(watcher.close_opencode)
        watcher.refresh_width = mock.Mock(return_value=False)
        watcher.publish = mock.Mock()
        return watcher

    def test_two_sessions_and_location_scoped_shared_models(self):
        for same_location in (True, False):
            with self.subTest(same_location=same_location):
                fixture = Fixture()
                directory = "/project one" if same_location else "/project two"

                def routes(path, headers, directory=directory):
                    url = urlsplit(path)
                    if url.path == "/api/session/s2":
                        return 200, {
                            "data": {"id": "s2", "location": {"directory": directory}}
                        }
                    if url.path == "/api/session/s2/message":
                        return 200, {"data": [assistant()], "cursor": {}}
                    if url.path == "/api/model":
                        target = parse_qs(url.query)["location[directory]"][0]
                        return 200, {
                            "location": {"directory": target},
                            "data": [
                                {
                                    "providerID": "custom",
                                    "id": "coding",
                                    "limit": {
                                        "context": 200000
                                        if target == "/project one"
                                        else 100000
                                    },
                                }
                            ],
                        }

                fixture.override = routes
                watcher = self.watcher()
                watcher.pane_list = mock.Mock(
                    return_value=[
                        {
                            "pane_id": f"w1:p{i}",
                            "agent": "opencode",
                            "agent_session": {"kind": "id", "value": f"s{i}"},
                        }
                        for i in (1, 2)
                    ]
                )
                with (
                    service(fixture) as path,
                    mock.patch.object(
                        self.ctx.opencode, "service_file", return_value=path
                    ),
                    mock.patch.object(self.ctx, "read_demo_overrides", return_value={}),
                ):
                    self.assertTrue(watcher.refresh_once())
                self.assertEqual(
                    watcher.percentages,
                    {"w1:p1": 4.875, "w1:p2": 4.875 if same_location else 9.75},
                )
                model_requests = [
                    path
                    for path, _ in fixture.requests
                    if path.startswith("/api/model")
                ]
                self.assertEqual(len(model_requests), 1 if same_location else 2)

    def test_real_stalled_io_does_not_block_other_agents_or_shutdown(self):
        fixture = Fixture()
        entered = threading.Event()
        release = threading.Event()

        def stalled(path, headers):
            entered.set()
            release.wait(3)

        fixture.override = stalled
        watcher = self.watcher()
        watcher.pane_list = mock.Mock(
            return_value=[
                {
                    "pane_id": "w1:p1",
                    "agent": "opencode",
                    "agent_session": {"kind": "id", "value": "s1"},
                },
                {"pane_id": "w1:p2", "agent": "codex"},
            ]
        )
        with (
            service(fixture) as path,
            mock.patch.object(self.ctx.opencode, "service_file", return_value=path),
            mock.patch.object(self.ctx, "read_demo_overrides", return_value={}),
            mock.patch.object(self.ctx, "collect_percent", return_value=25),
        ):
            try:
                watcher.refresh_usage()
                self.assertTrue(entered.wait(1))
                self.assertTrue(
                    any(not job.future.done() for job in watcher.opencode_jobs.values())
                )
                self.assertEqual(watcher.percentages, {"w1:p2": 25})
                start = time.monotonic()
                watcher.close_opencode()
                self.assertLess(time.monotonic() - start, 1)
            finally:
                release.set()


class DiscoveryTests(unittest.TestCase):
    def test_endpoint_restrictions(self):
        for url in [
            "http://example.com",
            "http://127.0.0.1.evil.test",
            "file:///tmp/test",
            "http://user:secret@127.0.0.1",
            "http://127.0.0.1/?secret=x",
            "http://127.0.0.1/#fragment",
            "http://127.0.0.1:0",
            "http://[::ffff:192.0.2.1]",
        ]:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "service.json"
                path.write_text(json.dumps({"url": url, "pid": 123}))
                with self.assertRaises(opencode.Unavailable) as caught:
                    opencode.Registration.read(path)
                self.assertEqual(caught.exception.reason, opencode.Reason.ENDPOINT)
        for url in ["http://127.0.0.1", "http://[::1]", "http://localhost:1234"]:
            with self.subTest(url=url):
                self.assertTrue(opencode.Endpoint.parse(url).addresses)

    def test_inaccessible_config_disables_only_opencode(self):
        with (
            mock.patch.object(Path, "exists", side_effect=PermissionError),
            self.assertRaises(opencode.Unavailable) as caught,
        ):
            opencode.service_file()
        self.assertEqual(caught.exception.reason, opencode.Reason.CONFIGURATION)

    def test_config_and_registration_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(
                "os.environ",
                {"XDG_STATE_HOME": directory, "HERDR_PLUGIN_CONFIG_DIR": directory},
            ):
                self.assertEqual(
                    opencode.service_file(), root / "opencode" / "service.json"
                )
                config = root / "config.json"
                override = root / "custom-service.json"
                config.write_text(
                    json.dumps({"opencode": {"service_file": str(override)}})
                )
                self.assertEqual(opencode.service_file(), override)
                config.write_text('{"opencode":{"service_file":"relative.json"}}')
                with self.assertRaises(opencode.Unavailable) as caught:
                    opencode.service_file()
                self.assertEqual(caught.exception.reason, opencode.Reason.CONFIGURATION)


if __name__ == "__main__":
    unittest.main()
