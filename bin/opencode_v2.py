"""Read context snapshots from an existing loopback OpenCode 2 service."""

from __future__ import annotations

import base64
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from typing import Any, Callable, TypeGuard
from urllib.parse import quote, urlencode, urlsplit


REQUEST_SECONDS = 2.0
COLLECTION_SECONDS = 5.0
MAX_PAGES = 10
MAX_BYTES = 8 * 1024 * 1024
REGISTRATION_BYTES = 64 * 1024


class Reason(str, Enum):
    AVAILABLE = "available"
    COLLECTING = "collecting"
    CONFIGURATION = "configuration"
    DISCOVERY = "service_unavailable"
    COMPATIBILITY = "incompatible_service"
    HERDR_COMPATIBILITY = "incompatible_herdr"
    HERDR_UNAVAILABLE = "herdr_version_unavailable"
    PUBLICATION = "publication_failed"
    ENDPOINT = "unsafe_endpoint"
    AUTHENTICATION = "authentication_failed"
    TRANSPORT = "transport_failed"
    INVALID_RESPONSE = "invalid_response"
    SESSION = "session_unavailable"
    NO_USAGE = "no_eligible_usage"
    MISSING_LIMIT = "missing_model_limit"
    BUDGET = "collection_budget"
    CHANGED = "state_changed"
    EXPIRED = "sample_expired"


class Unavailable(Exception):
    def __init__(self, reason: Reason):
        super().__init__(reason.value)
        self.reason = reason


def _integer(raw: str) -> int:
    if len(raw) > 64:
        raise ValueError("JSON number exceeds the supported size")
    try:
        return int(raw)
    except ValueError as error:
        raise Unavailable(Reason.INVALID_RESPONSE) from error


def _float(raw: str) -> float:
    if len(raw) > 64:
        raise ValueError("JSON number exceeds the supported size")
    try:
        value = float(raw)
    except ValueError as error:
        raise Unavailable(Reason.INVALID_RESPONSE) from error
    if not math.isfinite(value):
        raise ValueError("JSON number is not finite")
    return value


def _constant(raw: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw, parse_int=_integer, parse_float=_float, parse_constant=_constant
        )
    except (ValueError, RecursionError) as error:
        raise Unavailable(Reason.INVALID_RESPONSE) from error


def _read_json(path: Path, reason: Reason) -> Any:
    try:
        with path.open("rb") as handle:
            raw = handle.read(REGISTRATION_BYTES + 1)
        if len(raw) > REGISTRATION_BYTES:
            raise ValueError("Registration exceeds the supported size")
        return _json(raw)
    except (OSError, ValueError, RecursionError, Unavailable) as error:
        raise Unavailable(reason) from error


def service_file() -> Path:
    root = Path(
        os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "herdr-ctx-bar"
    )
    config = root / "config.json"
    try:
        exists = config.exists()
    except OSError as error:
        raise Unavailable(Reason.CONFIGURATION) from error
    if exists:
        value = _read_json(config, Reason.CONFIGURATION)
        if not isinstance(value, dict):
            raise Unavailable(Reason.CONFIGURATION)
        options = value.get("opencode", {})
        if not isinstance(options, dict):
            raise Unavailable(Reason.CONFIGURATION)
        if "service_file" in options:
            override = options["service_file"]
            if not isinstance(override, str) or not Path(override).is_absolute():
                raise Unavailable(Reason.CONFIGURATION)
            return Path(override)
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "opencode"
        / "service.json"
    )


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    tls: bool
    addresses: tuple[str, ...]

    @classmethod
    def parse(cls, url: str) -> Endpoint:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (
                parsed.scheme not in ("http", "https")
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(ord(char) <= 32 for char in url)
            ):
                raise ValueError("Unsupported endpoint")
            if host == "localhost":
                addresses = ("127.0.0.1", "::1")
            else:
                if "%" in host or not ipaddress.ip_address(host).is_loopback:
                    raise ValueError("Endpoint is not loopback")
                addresses = (host,)
            tls = parsed.scheme == "https"
            port = parsed.port if parsed.port is not None else (443 if tls else 80)
            if port == 0:
                raise ValueError("Endpoint port is zero")
            return cls(host, port, tls, addresses)
        except (TypeError, ValueError) as error:
            raise Unavailable(Reason.ENDPOINT) from error


@dataclass(frozen=True)
class Registration:
    file: Path
    url: str = field(repr=False)
    pid: int
    endpoint: Endpoint
    instance: str | None = None
    version: str | None = None
    password: str | None = field(default=None, repr=False)

    @classmethod
    def read(cls, path: Path) -> Registration:
        value = _read_json(path, Reason.DISCOVERY)
        if not isinstance(value, dict):
            raise Unavailable(Reason.DISCOVERY)
        url, pid = value.get("url"), value.get("pid")
        if not isinstance(url, str) or type(pid) is not int or pid < 0:
            raise Unavailable(Reason.DISCOVERY)
        for key in ("id", "version", "password"):
            if key in value and not isinstance(value[key], str):
                raise Unavailable(Reason.DISCOVERY)
        return cls(
            path,
            url,
            pid,
            Endpoint.parse(url),
            value.get("id"),
            value.get("version"),
            value.get("password"),
        )


@dataclass(frozen=True)
class Result:
    usage: tuple[float, float] | None = None
    reason: Reason = Reason.NO_USAGE
    message_id: str | None = None


class Transport:
    def __init__(
        self,
        timeout: float = COLLECTION_SECONDS,
        request_timeout: float = REQUEST_SECONDS,
        max_bytes: int = MAX_BYTES,
    ):
        self.started_at = time.monotonic()
        self.deadline = self.started_at + timeout
        self.request_timeout = request_timeout
        self.remaining_bytes = max_bytes
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._active: tuple[object, socket.socket] | None = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise Unavailable(Reason.BUDGET)
        return remaining

    def _abort(self, token: object | None = None) -> None:
        with self._lock:
            if self._active is None or (
                token is not None and self._active[0] is not token
            ):
                return
            _, active = self._active
            # close() alone can leave HTTP makefile readers blocked.
            with suppress(OSError):
                active.shutdown(socket.SHUT_RDWR)
            active.close()

    def cancel(self) -> None:
        self.cancelled.set()
        self._abort()

    def get(self, registration: Registration, path: str) -> Any:
        limit = min(self.remaining(), self.request_timeout)
        deadline = time.monotonic() + limit
        expired = threading.Event()
        token = object()
        connection = http.client.HTTPConnection(
            registration.endpoint.host, registration.endpoint.port
        )
        response: http.client.HTTPResponse | None = None

        def expire() -> None:
            expired.set()
            self._abort(token)

        timer = threading.Timer(limit, expire)
        timer.daemon = True
        timer.start()
        try:
            endpoint = registration.endpoint
            connected = False
            stream: socket.socket | None = None
            for address in endpoint.addresses:
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                stream = socket.socket(family, socket.SOCK_STREAM)
                with self._lock:
                    if self.cancelled.is_set() or expired.is_set():
                        stream.close()
                        raise Unavailable(Reason.BUDGET)
                    self._active = token, stream
                try:
                    stream.settimeout(max(0.001, deadline - time.monotonic()))
                    stream.connect((address, endpoint.port))
                    connected = True
                    break
                except OSError as error:
                    stream.close()
                    if expired.is_set() or time.monotonic() >= deadline:
                        raise Unavailable(Reason.BUDGET) from error
            if not connected or stream is None:
                raise Unavailable(Reason.TRANSPORT)
            if endpoint.tls:
                context = ssl.create_default_context()
                with self._lock:
                    stream = context.wrap_socket(
                        stream,
                        server_hostname=endpoint.host,
                        do_handshake_on_connect=False,
                    )
                    self._active = token, stream
                stream.do_handshake()
            connection.sock = stream
            headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
            if registration.password is not None:
                credentials = base64.b64encode(
                    f"opencode:{registration.password}".encode()
                ).decode("ascii")
                headers["Authorization"] = "Basic " + credentials
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                raise Unavailable(Reason.AUTHENTICATION)
            if response.status == 404:
                raise Unavailable(
                    Reason.SESSION
                    if path.startswith("/api/session/")
                    else Reason.COMPATIBILITY
                )
            if 300 <= response.status < 400:
                raise Unavailable(Reason.TRANSPORT)
            if not 200 <= response.status < 300:
                raise Unavailable(
                    Reason.DISCOVERY if path == "/api/info" else Reason.TRANSPORT
                )
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise Unavailable(Reason.INVALID_RESPONSE)
            chunks = []
            while True:
                self.remaining()
                chunk = response.read1(min(65536, self.remaining_bytes + 1))
                if expired.is_set():
                    raise Unavailable(Reason.BUDGET)
                self.remaining_bytes -= len(chunk)
                if self.remaining_bytes < 0:
                    raise Unavailable(Reason.BUDGET)
                if not chunk:
                    if response.length not in (None, 0):
                        raise Unavailable(Reason.INVALID_RESPONSE)
                    break
                chunks.append(chunk)
            if expired.is_set() or time.monotonic() >= deadline:
                raise Unavailable(Reason.BUDGET)
            value = _json(b"".join(chunks))
            self.remaining()
            return value
        except Unavailable:
            raise
        except (OSError, http.client.HTTPException) as error:
            reason = (
                Reason.BUDGET
                if expired.is_set()
                or self.cancelled.is_set()
                or time.monotonic() >= deadline
                else Reason.TRANSPORT
            )
            raise Unavailable(reason) from error
        except (ValueError, RecursionError) as error:
            raise Unavailable(Reason.INVALID_RESPONSE) from error
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            with self._lock:
                if self._active is not None and self._active[0] is token:
                    self._active[1].close()
                    self._active = None
            connection.close()


class Cycle:
    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[object, Future] = {}

    def shared(
        self, key: object, transport: Transport, fetch: Callable[[], Any]
    ) -> Any:
        with self._lock:
            future = self._requests.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._requests[key] = future
        if owner:
            try:
                future.set_result(fetch())
            except Unavailable as error:
                future.set_exception(error)
        while True:
            try:
                return future.result(timeout=min(0.05, transport.remaining()))
            except FutureTimeout:
                continue


def _probe(registration: Registration, transport: Transport) -> None:
    info = transport.get(registration, "/api/info")
    if not isinstance(info, dict):
        raise Unavailable(Reason.INVALID_RESPONSE)
    version = info.get("version")
    match = (
        re.fullmatch(r"2\.(\d{1,6})\.(\d{1,6})", version)
        if isinstance(version, str)
        else None
    )
    if match is None or tuple(map(int, match.groups())) < (0, 32):
        raise Unavailable(Reason.COMPATIBILITY)
    if type(info.get("pid")) is not int or info["pid"] != registration.pid:
        raise Unavailable(Reason.DISCOVERY)
    if registration.version is not None and version != registration.version:
        raise Unavailable(Reason.DISCOVERY)


def _session(
    registration: Registration, identifier: str, transport: Transport
) -> dict[str, Any]:
    response = transport.get(registration, f"/api/session/{quote(identifier, safe='')}")
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or data.get("id") != identifier:
        raise Unavailable(Reason.INVALID_RESPONSE)
    location = data.get("location")
    if (
        not isinstance(location, dict)
        or not isinstance(location.get("directory"), str)
        or not Path(location["directory"]).is_absolute()
    ):
        raise Unavailable(Reason.INVALID_RESPONSE)
    revert = data.get("revert")
    if revert is not None and (
        not isinstance(revert, dict)
        or not isinstance(revert.get("messageID"), str)
        or not revert["messageID"]
    ):
        raise Unavailable(Reason.INVALID_RESPONSE)
    return data


def _boundary(session: dict[str, Any]) -> tuple[str, str | None]:
    return session["location"]["directory"], (session.get("revert") or {}).get(
        "messageID"
    )


def _assistant(
    registration: Registration,
    identifier: str,
    boundary: str | None,
    transport: Transport,
    max_pages: int,
) -> dict[str, Any]:
    seeking = boundary is not None
    cursor = None
    visited: set[str] = set()
    for _ in range(max_pages):
        query = {"limit": "50"}
        if cursor is None:
            query["order"] = "desc"
        else:
            query["cursor"] = cursor
        response = transport.get(
            registration,
            f"/api/session/{quote(identifier, safe='')}/message?{urlencode(query)}",
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("data"), list)
            or not isinstance(response.get("cursor"), dict)
        ):
            raise Unavailable(Reason.INVALID_RESPONSE)
        messages = response["data"]
        for message in messages:
            if not isinstance(message, dict):
                raise Unavailable(Reason.INVALID_RESPONSE)
            if seeking:
                if message.get("id") == boundary:
                    seeking = False
                continue
            if (
                message.get("type") == "compaction"
                and message.get("status") == "completed"
            ):
                raise Unavailable(Reason.NO_USAGE)
            if message.get("type") == "assistant" and "tokens" in message:
                return message
        cursor = response["cursor"].get("next")
        if not messages or cursor is None:
            raise Unavailable(Reason.NO_USAGE)
        if not isinstance(cursor, str) or not cursor or cursor in visited:
            raise Unavailable(Reason.INVALID_RESPONSE)
        visited.add(cursor)
    raise Unavailable(Reason.BUDGET)


def _number(value: Any) -> TypeGuard[float]:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _usage(message: dict[str, Any]) -> tuple[float, str, str]:
    tokens = message.get("tokens")
    model = message.get("model")
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get("cache"), dict)
        or not isinstance(model, dict)
    ):
        raise Unavailable(Reason.INVALID_RESPONSE)
    raw_values = [tokens.get(key) for key in ("input", "output", "reasoning")]
    raw_values.extend(tokens["cache"].get(key) for key in ("read", "write"))
    values: list[float] = []
    for value in raw_values:
        if not _number(value):
            raise Unavailable(Reason.INVALID_RESPONSE)
        values.append(value)
    used = sum(values)
    if not _number(used) or not isinstance(message.get("id"), str):
        raise Unavailable(Reason.INVALID_RESPONSE)
    if used == 0:
        raise Unavailable(Reason.NO_USAGE)
    if not all(
        isinstance(model.get(key), str) and model[key] for key in ("providerID", "id")
    ):
        raise Unavailable(Reason.INVALID_RESPONSE)
    return used, model["providerID"], model["id"]


def _models(
    registration: Registration, directory: str, transport: Transport
) -> dict[tuple[str, str], float]:
    response = transport.get(
        registration, "/api/model?" + urlencode({"location[directory]": directory})
    )
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("data"), list)
        or not isinstance(response.get("location"), dict)
        or response["location"].get("directory") != directory
    ):
        raise Unavailable(Reason.INVALID_RESPONSE)
    models = {}
    for model in response["data"]:
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("providerID"), str)
            or not isinstance(model.get("id"), str)
        ):
            raise Unavailable(Reason.INVALID_RESPONSE)
        limit = model.get("limit")
        window = limit.get("context") if isinstance(limit, dict) else None
        if _number(window) and window > 0:
            models[model["providerID"], model["id"]] = window
    return models


def collect(
    registration: Registration,
    identifier: str,
    *,
    cycle: Cycle | None = None,
    transport: Transport | None = None,
    timeout: float = COLLECTION_SECONDS,
    request_timeout: float = REQUEST_SECONDS,
    max_pages: int = MAX_PAGES,
    max_bytes: int = MAX_BYTES,
) -> Result:
    transport = transport or Transport(timeout, request_timeout, max_bytes)
    cycle = cycle or Cycle()
    try:
        cycle.shared(
            (registration, "health"), transport, lambda: _probe(registration, transport)
        )
        session = _session(registration, identifier, transport)
        directory, boundary = _boundary(session)
        message = _assistant(registration, identifier, boundary, transport, max_pages)
        used, provider, model = _usage(message)
        models = cycle.shared(
            (registration, directory),
            transport,
            lambda: _models(registration, directory, transport),
        )
        window = models.get((provider, model))
        if window is None:
            raise Unavailable(Reason.MISSING_LIMIT)
        latest = _session(registration, identifier, transport)
        if (
            _boundary(latest) != (directory, boundary)
            or Registration.read(registration.file) != registration
        ):
            raise Unavailable(Reason.CHANGED)
        transport.remaining()
        return Result((used, window), Reason.AVAILABLE, message["id"])
    except Unavailable as error:
        return Result(reason=error.reason)
    finally:
        transport.cancel()
