#!/usr/bin/env python3
"""Publish a thin context-usage bar to Herdr's Agent sidebar.

The daemon subscribes to Herdr pane events, reads local agent session artifacts,
and reports exactly one of three metadata tokens per supported pane.  Herdr owns
the colors; see CONFIG_SNIPPET below.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import date
import fcntl
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, SupportsFloat, SupportsIndex, TextIO, TypeGuard

import opencode_v2 as opencode


SOURCE = "herdr-ctx-bar"
SUPPORTED_AGENTS = {"codex", "claude", "pi", "hermes", "opencode"}
TOKEN_NAMES = ("ctx_green", "ctx_yellow", "ctx_red")
ROW_MARGIN = 5
DEFAULT_SIDEBAR_WIDTH = 26
MIN_BAR_WIDTH = 8
MAX_BAR_WIDTH = 80  # Herdr's metadata-token value limit.
WIDTH_POLL_SECONDS = 2.0
USAGE_POLL_SECONDS = 15.0
EVENT_SETTLE_SECONDS = 0.75
OPENCODE_SAMPLE_SECONDS = 20.0
OPENCODE_MIN_HERDR_VERSION = (0, 8, 2)
TAIL_BYTES = 1024 * 1024
DEMO_SECONDS = 5 * 60
DEMO_LEVELS = {
    "demo-green": 25.0,
    "demo-yellow": 65.0,
    "demo-red": 95.0,
}

SIDEBAR_SECTION = """[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent"],
  [
    { token = "$ctx_green", fg = "#3fb950" },
    { token = "$ctx_yellow", fg = "#d29922" },
    { token = "$ctx_red", fg = "#f85149" },
  ],
]
"""

CONFIG_SNIPPET = f"""# Merge this section into ~/.config/herdr/config.toml, then run:
#   herdr server reload-config
{SIDEBAR_SECTION}"""


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def usage_percent(used: int | float, window: int | float) -> float | None:
    if not finite_positive(window) or not math.isfinite(float(used)) or used < 0:
        return None
    return clamp_percent(float(used) * 100.0 / float(window))


def color_for_percent(percent: float) -> str:
    if percent >= 80:
        return "ctx_red"
    if percent >= 50:
        return "ctx_yellow"
    return "ctx_green"


def render_bar(percent: float, width: int) -> str:
    """Use a heavy used segment and a thin remaining track.

    The glyphs stay within one terminal row and visually form a roughly
    three-pixel-high line in common terminal fonts.
    """
    width = max(MIN_BAR_WIDTH, min(MAX_BAR_WIDTH, int(width)))
    filled = max(0, min(width, round(width * clamp_percent(percent) / 100.0)))
    return "━" * filled + "─" * (width - filled)


def finite_positive(
    value: Any,
) -> TypeGuard[str | bytes | bytearray | SupportsFloat | SupportsIndex]:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def read_tail_lines(path: str | Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
        if size > max_bytes:
            data = data.split(b"\n", 1)[-1]
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def json_lines_reverse(path: str | Path) -> Iterable[dict[str, Any]]:
    for raw in reversed(read_tail_lines(path)):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def session_info(pane: dict[str, Any]) -> tuple[str, str, str]:
    session = pane.get("agent_session")
    if not isinstance(session, dict):
        session = {}
    agent = str(pane.get("agent") or session.get("agent") or "").lower()
    return agent, str(session.get("kind") or ""), str(session.get("value") or "")


def newest(paths: Iterable[str]) -> str | None:
    candidates = []
    for path in paths:
        with suppress(OSError):
            candidates.append((os.path.getmtime(path), path))
    return max(candidates, default=(0, None))[1]


def mtime_or_zero(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def read_first_json(path: str | Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.readline(1024 * 1024)
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def resolve_codex_path(session_id: str, cwd: str) -> str | None:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if session_id:
        direct = newest(
            glob.iglob(
                str(root / "**" / f"rollout-*-{session_id}.jsonl"), recursive=True
            )
        )
        if direct:
            return direct
    # Safe fallback: inspect newest rollouts and require an exact cwd match.
    paths = sorted(
        glob.iglob(str(root / "**" / "rollout-*.jsonl"), recursive=True),
        key=mtime_or_zero,
        reverse=True,
    )[:100]
    for path in paths:
        meta = read_first_json(path)
        if not meta:
            continue
        payload = meta.get("payload") or {}
        if session_id and session_id in (payload.get("id"), payload.get("session_id")):
            return path
        if cwd and os.path.normpath(str(payload.get("cwd") or "")) == os.path.normpath(
            cwd
        ):
            return path
    return None


def collect_codex(pane: dict[str, Any]) -> tuple[int, int] | None:
    _, _, session_id = session_info(pane)
    path = resolve_codex_path(
        session_id, str(pane.get("foreground_cwd") or pane.get("cwd") or "")
    )
    if not path:
        return None
    for entry in json_lines_reverse(path):
        payload = entry.get("payload") or {}
        if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        usage = info.get("last_token_usage") or {}
        used = usage.get("total_tokens", usage.get("input_tokens"))
        window = info.get("model_context_window")
        if finite_positive(used) and finite_positive(window):
            return int(float(used)), int(float(window))
    return None


CLAUDE_WINDOWS = (
    ("claude-sonnet-5", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-fable-5", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-sonnet-4-5", 1_000_000),
    ("claude-sonnet-4", 1_000_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-opus-4-5", 200_000),
    ("claude-opus-4-1", 200_000),
)


def claude_window(model: str) -> int | None:
    normalized = model.lower().removesuffix("[1m]")
    for fragment, window in sorted(
        CLAUDE_WINDOWS, key=lambda row: len(row[0]), reverse=True
    ):
        if fragment in normalized:
            return window
    return None


def resolve_claude_path(session_id: str) -> str | None:
    if not session_id:
        return None
    root = Path(
        os.environ.get("CLAUDE_PROJECTS_ROOT", Path.home() / ".claude" / "projects")
    )
    return newest(glob.iglob(str(root / "*" / f"{session_id}.jsonl")))


def collect_claude(pane: dict[str, Any]) -> tuple[int, int] | None:
    _, _, session_id = session_info(pane)
    path = resolve_claude_path(session_id)
    if not path:
        return None
    compact_post_tokens = None
    for entry in json_lines_reverse(path):
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            value = (entry.get("compactMetadata") or {}).get("postTokens")
            if finite_positive(value):
                compact_post_tokens = int(float(value))
            continue
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        used = sum(
            int(float(usage.get(key) or 0))
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        window = claude_window(str(message.get("model") or ""))
        if compact_post_tokens and window:
            return compact_post_tokens, window
        if used > 0 and window:
            return used, window
    return None


def pi_model_windows(session_path: str) -> dict[str, int]:
    path = Path(session_path).expanduser()
    roots: list[Path] = []
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        roots.append(Path(configured).expanduser())
    try:
        sessions = path.parent.parent
        if sessions.name == "sessions":
            roots.append(sessions.parent)
    except IndexError:
        pass
    roots.append(Path.home() / ".pi" / "agent")
    windows: dict[str, int] = {}
    for root in reversed(list(dict.fromkeys(roots))):
        try:
            stored = json.loads((root / "models-store.json").read_text())
            for provider, catalog in stored.items():
                for model in (catalog or {}).get("models", []):
                    set_model_window(
                        windows, provider, model.get("id"), model.get("contextWindow")
                    )
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
        try:
            configured_models = json.loads((root / "models.json").read_text())
            for provider, definition in (
                configured_models.get("providers") or {}
            ).items():
                for model in (definition or {}).get("models", []):
                    set_model_window(
                        windows,
                        provider,
                        model.get("id"),
                        model.get("contextWindow", 128_000),
                    )
                for model_id, override in (
                    (definition or {}).get("modelOverrides") or {}
                ).items():
                    set_model_window(
                        windows,
                        provider,
                        model_id,
                        (override or {}).get("contextWindow"),
                    )
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    return windows


def set_model_window(
    windows: dict[str, int], provider: Any, model: Any, window: Any
) -> None:
    if not model or not finite_positive(window):
        return
    windows[str(model)] = int(float(window))
    if provider:
        windows[f"{provider}/{model}"] = int(float(window))


def resolve_pi_path(pane: dict[str, Any]) -> str | None:
    _, kind, value = session_info(pane)
    if (
        kind == "path"
        and value.endswith(".jsonl")
        and Path(value).expanduser().is_file()
    ):
        return str(Path(value).expanduser())
    cwd = str(pane.get("foreground_cwd") or pane.get("cwd") or "")
    root = (
        Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
        / "sessions"
    )
    candidates = sorted(
        glob.iglob(str(root / "**" / "*.jsonl"), recursive=True),
        key=mtime_or_zero,
        reverse=True,
    )[:100]
    if not cwd:
        return None
    for path in candidates:
        for entry in read_tail_lines(path, 32 * 1024)[:3]:
            try:
                obj = json.loads(entry)
            except json.JSONDecodeError:
                continue
            if os.path.normpath(str(obj.get("cwd") or "")) == os.path.normpath(cwd):
                return path
    return None


def collect_pi(pane: dict[str, Any]) -> tuple[int, int] | None:
    path = resolve_pi_path(pane)
    if not path:
        return None
    windows = pi_model_windows(path)
    for entry in json_lines_reverse(path):
        if entry.get("type") == "compaction":
            return None  # wait for the first real post-compaction response
        message = entry.get("message") or {}
        if message.get("role") != "assistant" or message.get("stopReason") in (
            "aborted",
            "error",
        ):
            continue
        snapshot = message.get("contextSnapshot") or {}
        usage = message.get("usage") or {}
        used = snapshot.get("promptTokens") or usage.get("totalTokens")
        if not finite_positive(used):
            used = sum(
                float(usage.get(key) or 0)
                for key in ("input", "output", "cacheRead", "cacheWrite")
            )
        provider, model = (
            str(message.get("provider") or ""),
            str(message.get("model") or ""),
        )
        window = windows.get(f"{provider}/{model}") or windows.get(model)
        if finite_positive(used) and finite_positive(window):
            return int(float(used)), int(float(window))
    return None


def hermes_db_paths() -> Iterable[Path]:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    yield root / "state.db"
    yield from sorted((root / "profiles").glob("*/state.db"))


def rough_tokens(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return max(0, math.ceil(len(value) / 4))


def hermes_context_window(model: str, model_config: Any) -> int | None:
    try:
        config = (
            json.loads(model_config or "{}")
            if isinstance(model_config, str)
            else (model_config or {})
        )
    except json.JSONDecodeError:
        config = {}
    for key in ("context_length", "contextWindow", "context_window"):
        if finite_positive(config.get(key)):
            return int(float(config[key]))
    # Hermes' own config uses model.context_length. Parse just that scalar to
    # keep this plugin dependency-free; unknown windows fail closed.
    config_path = (
        Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
    )
    try:
        in_model = False
        for raw in config_path.read_text().splitlines():
            stripped = raw.strip()
            if not raw.startswith((" ", "\t")):
                in_model = stripped == "model:"
            elif in_model and stripped.startswith("context_length:"):
                value = (
                    stripped.split(":", 1)[1].split("#", 1)[0].strip().replace("_", "")
                )
                if finite_positive(value):
                    return int(float(value))
    except OSError:
        pass
    # Match Hermes' own dependency-free last resort for models whose provider
    # metadata is unavailable. Hermes runs the same session at this 256K
    # fallback, so the meter and its compression guard stay aligned.
    return claude_window(model) or 256_000


def collect_hermes(pane: dict[str, Any]) -> tuple[int, int] | None:
    _, _, session_id = session_info(pane)
    if not session_id:
        return None
    for db_path in hermes_db_paths():
        if not db_path.is_file():
            continue
        connection = None
        try:
            uri = f"file:{db_path}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.1)
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT model, model_config, COALESCE(system_prompt, "
                "(SELECT prompt FROM system_prompts WHERE hash = sessions.system_prompt_hash)) "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                continue
            model, model_config, system_prompt = row
            messages = connection.execute(
                "SELECT token_count, content, tool_calls, reasoning, reasoning_content "
                "FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
            used = rough_tokens(system_prompt)
            for (
                token_count,
                content,
                tool_calls,
                reasoning,
                reasoning_content,
            ) in messages:
                if finite_positive(token_count) and isinstance(
                    token_count, (int, float, str, bytes)
                ):
                    used += int(token_count)
                else:
                    used += sum(
                        rough_tokens(value)
                        for value in (content, tool_calls, reasoning, reasoning_content)
                    )
            window = hermes_context_window(str(model or ""), model_config)
            if used > 0 and window:
                return used, window
        except (sqlite3.Error, OSError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return None


COLLECTORS = {
    "codex": collect_codex,
    "claude": collect_claude,
    "pi": collect_pi,
    "hermes": collect_hermes,
}


def collect_percent(pane: dict[str, Any]) -> float | None:
    agent, _, _ = session_info(pane)
    collector = COLLECTORS.get(agent)
    if not collector:
        return None
    usage = collector(pane)
    return usage_percent(*usage) if usage else None


def state_dir() -> Path:
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    path = (
        Path(value)
        if value
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / SOURCE
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def demo_overrides_path() -> Path:
    return state_dir() / "demo-overrides.json"


def read_demo_overrides(now: float | None = None) -> dict[str, float]:
    current_time = time.time() if now is None else now
    try:
        stored = json.loads(demo_overrides_path().read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    overrides: dict[str, float] = {}
    for pane_id, entry in stored.items():
        if not isinstance(pane_id, str) or not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        expires_at = entry.get("expires_at")
        if percent is None or expires_at is None:
            continue
        try:
            percent_value = float(percent)
            expires_value = float(expires_at)
        except (TypeError, ValueError):
            continue
        if math.isfinite(percent_value) and expires_value > current_time:
            overrides[pane_id] = clamp_percent(percent_value)
    return overrides


def update_demo_override(
    pane_id: str, percent: float | None, now: float | None = None
) -> None:
    """Atomically add or clear one pane's temporary preview."""
    current_time = time.time() if now is None else now
    lock = (state_dir() / "demo-overrides.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = demo_overrides_path()
        try:
            stored = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        # Discard expired and malformed entries while the file is locked.
        live = read_demo_overrides(current_time)
        cleaned = {
            key: {
                "percent": value,
                "expires_at": float((stored.get(key) or {}).get("expires_at", 0)),
            }
            for key, value in live.items()
        }
        if percent is None:
            cleaned.pop(pane_id, None)
        else:
            cleaned[pane_id] = {
                "percent": clamp_percent(percent),
                "expires_at": current_time + DEMO_SECONDS,
            }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".demo-overrides.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(cleaned, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
    finally:
        lock.close()


def config_path() -> Path:
    configured = os.environ.get("HERDR_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "herdr" / "config.toml"


def install_sidebar_config(path: Path) -> tuple[str, Path | None]:
    """Install sidebar rows only when no custom Agent layout would be lost."""
    try:
        original = path.read_text()
    except FileNotFoundError:
        original = ""
    if all(f'"${name}"' in original for name in TOKEN_NAMES):
        return "already-installed", None
    if any(line.strip() == "[ui.sidebar.agents]" for line in original.splitlines()):
        return "custom-layout", None

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = time.time_ns() % 1_000_000
        backup = path.with_name(
            f"{path.name}.before-herdr-ctx-bar-{timestamp}-{suffix:06d}"
        )
        shutil.copy2(path, backup)

    separator = (
        ""
        if not original or original.endswith("\n\n")
        else "\n"
        if original.endswith("\n")
        else "\n\n"
    )
    updated = original + separator + SIDEBAR_SECTION
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    return "installed", backup


def setup_sidebar() -> int:
    path = config_path()
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    before = subprocess.run(
        [herdr, "config", "check"], capture_output=True, text=True, check=False
    )
    if before.returncode != 0 and path.exists():
        print(
            "Context Bar did not change the config because it is already invalid:",
            file=sys.stderr,
        )
        print(before.stderr or before.stdout, file=sys.stderr, end="")
        return 1

    status, backup = install_sidebar_config(path)
    if status == "custom-layout":
        print(f"Context Bar found custom Agent rows in {path} and left them unchanged.")
        print(CONFIG_SNIPPET, end="")
        return 2
    if status == "already-installed":
        print(f"Context Bar sidebar rows are already configured in {path}.")
    else:
        print(f"Context Bar added its sidebar row to {path}.")
        if backup:
            print(f"Backup: {backup}")

    checked = subprocess.run(
        [herdr, "config", "check"], capture_output=True, text=True, check=False
    )
    if checked.returncode != 0:
        if backup:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
        print(
            "The updated config failed validation and was rolled back:",
            file=sys.stderr,
        )
        print(checked.stderr or checked.stdout, file=sys.stderr, end="")
        return 1

    reloaded = subprocess.run(
        [herdr, "server", "reload-config"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reloaded.returncode != 0:
        print("Config is valid, but Herdr could not reload it:", file=sys.stderr)
        print(reloaded.stderr or reloaded.stdout, file=sys.stderr, end="")
        return 1
    print("Herdr configuration reloaded.")
    return 0


def lock_path(socket_path: str) -> Path:
    digest = hashlib.sha256(socket_path.encode()).hexdigest()[:12]
    return state_dir() / f"daemon-{digest}.lock"


def running_pid(socket_path: str) -> int | None:
    path = lock_path(socket_path)
    try:
        with path.open() as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
    except FileNotFoundError:
        return None
    except OSError:
        pass
    try:
        return int(path.read_text().strip()) or None
    except (OSError, ValueError):
        return None


def acquire_daemon_lock(socket_path: str) -> TextIO | None:
    """Acquire the daemon lock, then publish this process's PID.

    Opening in append mode avoids truncating the active daemon's PID before
    we know that this process owns the lock.
    """
    lock_file = lock_path(socket_path).open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


class HerdrClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(3)
            sock.connect(self.socket_path)
            request = {
                "id": f"{SOURCE}:{time.time_ns()}",
                "method": method,
                "params": params,
            }
            sock.sendall((json.dumps(request) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                data += chunk
            return json.loads(data.split(b"\n", 1)[0])
        except (OSError, ValueError):
            return None
        finally:
            sock.close()


@dataclass(frozen=True)
class HerdrSupport:
    version: str | None
    reason: opencode.Reason | None

    @classmethod
    def from_response(cls, response: object) -> HerdrSupport:
        unavailable = cls(None, opencode.Reason.HERDR_UNAVAILABLE)
        if not isinstance(response, dict) or "error" in response:
            return unavailable
        result = response.get("result")
        if not isinstance(result, dict) or result.get("type") != "pong":
            return unavailable
        version = result.get("version")
        if not isinstance(version, str) or len(version) > 128:
            return unavailable
        match = re.fullmatch(
            r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
            version,
        )
        if match is None:
            return unavailable
        release = tuple(int(part) for part in match.groups()[:3])
        prerelease = match[4]
        compatible = release > OPENCODE_MIN_HERDR_VERSION or (
            release == OPENCODE_MIN_HERDR_VERSION and prerelease is None
        )
        preview = re.fullmatch(
            r"preview\.(\d{4}-\d{2}-\d{2})-[0-9a-f]{12}", prerelease or ""
        )
        if preview is not None:
            try:
                built = date.fromisoformat(preview[1])
            except ValueError:
                return unavailable
            compatible = release >= OPENCODE_MIN_HERDR_VERSION or (
                release == (0, 8, 0) and built >= date(2026, 8, 17)
            )
        return cls(version, None if compatible else opencode.Reason.HERDR_COMPATIBILITY)

    def print_problem(self) -> None:
        if self.reason is None:
            return
        detail = (
            f"running Herdr {self.version}"
            if self.version is not None
            else "cannot verify the running Herdr version"
        )
        required = ".".join(str(part) for part in OPENCODE_MIN_HERDR_VERSION)
        print(
            f"OpenCode unavailable: {detail}; requires server support from {required} or a compatible preview."
        )
        print("Other agent integrations remain enabled.")


def opencode_status_path(socket_path: str) -> Path:
    return lock_path(socket_path).with_suffix(".opencode.json")


def print_opencode_status(
    socket_path: str,
    *,
    current_pid: int | None = None,
    support: HerdrSupport | None = None,
) -> None:
    if support is not None:
        support.print_problem()
    try:
        with opencode_status_path(socket_path).open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Diagnostic state exceeds the supported size")
        state = json.loads(raw)
        if (
            not isinstance(state, dict)
            or state.get("schema") != 1
            or not isinstance(state.get("panes"), dict)
        ):
            raise ValueError("Invalid diagnostic state")
        saved_support = state.get("herdr")
        if support is None and isinstance(saved_support, dict):
            HerdrSupport.from_response(
                {"result": {"type": "pong", "version": saved_support.get("version")}}
            ).print_problem()
        for pane_id, entry in state["panes"].items():
            if not isinstance(pane_id, str) or not isinstance(entry, dict):
                continue
            try:
                reason = opencode.Reason(entry.get("reason"))
                checked = entry.get("checked_at")
                if (
                    isinstance(checked, bool)
                    or not isinstance(checked, (int, float))
                    or not math.isfinite(checked)
                ):
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            age = max(0, time.time() - checked)
            pane_label = "".join(
                char
                for char in pane_id
                if char.isascii() and char.isalnum() or char in ":_-"
            )[:80]
            stale = (
                ", stale"
                if age > OPENCODE_SAMPLE_SECONDS or state.get("pid") != current_pid
                else ""
            )
            print(
                f"OpenCode {pane_label}: {reason.value} (checked {age:.0f}s ago{stale})"
            )
    except (OSError, ValueError, TypeError):
        print("OpenCode diagnostics: unavailable.")


class OpenCodeJob:
    def __init__(
        self, future: Future, transport: opencode.Transport, subscribers: dict[str, int]
    ):
        self.future = future
        self.transport = transport
        self.subscribers = subscribers
        self.started_at = transport.started_at
        self.abandoned = False

    def cancel(self) -> None:
        self.abandoned = True
        self.transport.cancel()
        self.future.cancel()


class Watcher:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.client = HerdrClient(socket_path)
        self.subscription: socket.socket | None = None
        self.panes: dict[str, dict[str, Any]] = {}
        self.percentages: dict[str, float] = {}
        self.bar_width = DEFAULT_SIDEBAR_WIDTH - ROW_MARGIN
        self.pending_refresh_at = 0.0
        self.next_usage_poll = 0.0
        self.opencode_daemon = False
        self.opencode_executor: ThreadPoolExecutor | None = None
        self.opencode_jobs: dict[tuple[int, str], OpenCodeJob] = {}
        self.opencode_pending: dict[str, tuple[float, dict[str, int]]] = {}
        self.opencode_identities: dict[str, tuple[str, int]] = {}
        self.opencode_identity_seq = 0
        self.opencode_due: dict[str, float] = {}
        self.opencode_samples: dict[str, tuple[opencode.Result, float]] = {}
        self.opencode_states: dict[str, dict[str, Any]] = {}
        self.opencode_registration: opencode.Registration | None = None
        self.opencode_generation = 0
        self.opencode_cycle = opencode.Cycle()
        self.opencode_cycle_at = 0.0
        self.opencode_config_loaded = False
        self.opencode_file: Path | None = None
        self.opencode_config_error: opencode.Reason | None = None
        self.opencode_action_deadline: float | None = None
        self.herdr_support: HerdrSupport | None = None
        self.next_herdr_probe = 0.0
        self.opencode_dirty = False

    def pane_list(self) -> list[dict[str, Any]]:
        response = self.client.request("pane.list", {}) or {}
        return (response.get("result") or {}).get("panes") or []

    def refresh_panes(self) -> None:
        self.panes = {p["pane_id"]: p for p in self.pane_list() if p.get("pane_id")}

    def refresh_width(self) -> bool:
        pane_id = next(iter(self.panes), None)
        if not pane_id:
            return False
        response = self.client.request("pane.layout", {"pane_id": pane_id}) or {}
        try:
            sidebar_width = int(response["result"]["layout"]["area"]["x"])
        except (KeyError, TypeError, ValueError):
            return False
        if sidebar_width < 8:  # collapsed, hidden, or mobile
            return False
        width = max(MIN_BAR_WIDTH, min(MAX_BAR_WIDTH, sidebar_width - ROW_MARGIN))
        if width == self.bar_width:
            return False
        self.bar_width = width
        return True

    def publish(
        self, pane_id: str, percent: float | None, *, ttl_ms: int | None = None
    ) -> bool:
        tokens: dict[str, str | None] = dict.fromkeys(TOKEN_NAMES)
        if percent is not None:
            tokens[color_for_percent(percent)] = render_bar(percent, self.bar_width)
        params: dict[str, Any] = {
            "pane_id": pane_id,
            "source": SOURCE,
            "seq": time.time_ns(),
            "tokens": tokens,
        }
        if ttl_ms is not None:
            params["ttl_ms"] = ttl_ms
        response = self.client.request("pane.report_metadata", params)
        return (
            isinstance(response, dict)
            and "error" not in response
            and isinstance(response.get("result"), dict)
        )

    def refresh_usage(self) -> None:
        self.refresh_panes()
        self.refresh_width()
        demo_overrides = read_demo_overrides()
        self.refresh_opencode(demo_overrides)
        live_supported: set[str] = set()
        for pane_id, pane in self.panes.items():
            agent, _, _ = session_info(pane)
            if agent not in SUPPORTED_AGENTS:
                continue
            live_supported.add(pane_id)
            percent = demo_overrides.get(pane_id)
            if agent == "opencode" and percent is None:
                sample = self.opencode_samples.get(pane_id)
                self.publish_opencode(
                    pane_id, sample[0] if sample else opencode.Result(), demo_overrides
                )
                continue
            if percent is None:
                percent = collect_percent(pane)
            self.publish(pane_id, percent)
            if percent is None:
                self.percentages.pop(pane_id, None)
            else:
                self.percentages[pane_id] = percent
        for pane_id in set(self.percentages) - live_supported:
            self.publish(pane_id, None)
            self.percentages.pop(pane_id, None)
        self.next_usage_poll = time.monotonic() + USAGE_POLL_SECONDS
        self.pending_refresh_at = 0.0

    def record_opencode(
        self,
        pane_id: str,
        identifier: str,
        result: opencode.Result,
        *,
        sampled: bool = False,
    ) -> None:
        previous = self.opencode_states.get(pane_id, {})
        used, window = result.usage or (None, None)
        values = {
            "session_id": identifier,
            "reason": result.reason.value,
            "used": used,
            "context_window": window,
        }
        changed = any(previous.get(key) != value for key, value in values.items())
        if not changed and not sampled:
            return
        now = time.time()
        reason_changed = (
            previous.get("reason") != result.reason.value
            or previous.get("session_id") != identifier
        )
        self.opencode_states[pane_id] = {
            **values,
            "checked_at": now,
            "changed_at": now if reason_changed else previous.get("changed_at", now),
        }
        self.opencode_dirty = True

    def save_opencode_status(self) -> None:
        if not self.opencode_daemon or not self.opencode_dirty:
            return
        temporary = None
        try:
            path = opencode_status_path(self.socket_path)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".opencode-", dir=path.parent
            )
            with os.fdopen(descriptor, "w") as handle:
                json.dump(
                    {
                        "schema": 1,
                        "pid": os.getpid(),
                        "herdr": asdict(self.herdr_support)
                        if self.herdr_support
                        else None,
                        "panes": self.opencode_states,
                    },
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self.opencode_dirty = False
        except OSError:
            pass
        finally:
            if temporary is not None:
                with suppress(OSError):
                    Path(temporary).unlink(missing_ok=True)

    def publish_opencode(
        self, pane_id: str, result: opencode.Result, demos: dict[str, float]
    ) -> None:
        if pane_id in demos:
            return
        percent = usage_percent(*result.usage) if result.usage else None
        ttl_ms = None
        if percent is not None:
            sample = self.opencode_samples.get(pane_id)
            age = time.monotonic() - sample[1] if sample else 0
            ttl_ms = max(1, int(max(0, OPENCODE_SAMPLE_SECONDS - age) * 1000))
        if not self.publish(pane_id, percent, ttl_ms=ttl_ms):
            self.opencode_samples.pop(pane_id, None)
            self.percentages.pop(pane_id, None)
            identifier = self.opencode_identities.get(pane_id, ("", 0))[0]
            self.record_opencode(
                pane_id, identifier, opencode.Result(reason=opencode.Reason.PUBLICATION)
            )
            return
        if percent is None:
            self.percentages.pop(pane_id, None)
            return
        self.percentages[pane_id] = percent

    def check_herdr_support(self, now: float, *, live: bool) -> None:
        if self.herdr_support is not None and (not live or now < self.next_herdr_probe):
            return
        support = HerdrSupport.from_response(self.client.request("ping", {}))
        self.next_herdr_probe = now + USAGE_POLL_SECONDS
        if support == self.herdr_support:
            return
        previous = self.herdr_support
        self.herdr_support = support
        self.opencode_dirty = True
        if previous is None or previous.reason == support.reason:
            return
        self.opencode_generation += 1
        self.opencode_samples.clear()
        self.opencode_pending.clear()
        self.opencode_due.clear()
        for job in self.opencode_jobs.values():
            job.cancel()

    def refresh_opencode(self, demos: dict[str, float]) -> None:
        now = time.monotonic()
        live = {}
        for pane_id, pane in self.panes.items():
            agent, kind, identifier = session_info(pane)
            if agent != "opencode":
                continue
            reference = pane.get("agent_session")
            raw_id = reference.get("value") if isinstance(reference, dict) else None
            identifier = raw_id if kind == "id" and isinstance(raw_id, str) else ""
            previous = self.opencode_identities.get(pane_id)
            if previous is None or previous[0] != identifier:
                self.opencode_identity_seq += 1
                self.opencode_identities[pane_id] = (
                    identifier,
                    self.opencode_identity_seq,
                )
                self.opencode_due.pop(pane_id, None)
                self.opencode_samples.pop(pane_id, None)
                self.publish_opencode(pane_id, opencode.Result(), demos)
                self.record_opencode(
                    pane_id,
                    identifier,
                    opencode.Result(reason=opencode.Reason.COLLECTING),
                )
            live[pane_id] = self.opencode_identities[pane_id]
        for pane_id in set(self.opencode_identities) - live.keys():
            self.opencode_identities.pop(pane_id)
            self.opencode_due.pop(pane_id, None)
            self.opencode_samples.pop(pane_id, None)
            self.opencode_states.pop(pane_id, None)
            self.publish_opencode(pane_id, opencode.Result(), demos)
            self.opencode_dirty = True
        self.check_herdr_support(now, live=bool(live))
        if self.herdr_support is not None and self.herdr_support.reason is not None:
            for key, job in list(self.opencode_jobs.items()):
                job.cancel()
                if job.future.done():
                    del self.opencode_jobs[key]
            self.opencode_pending.clear()
            for pane_id, (identifier, _) in live.items():
                result = opencode.Result(reason=self.herdr_support.reason)
                self.opencode_samples.pop(pane_id, None)
                self.record_opencode(pane_id, identifier, result)
                self.publish_opencode(pane_id, result, demos)
            self.save_opencode_status()
            return
        if not live and not self.opencode_jobs:
            self.opencode_pending.clear()
            self.save_opencode_status()
            return

        registration = None
        failure = None
        if any(identifier for identifier, _ in live.values()):
            if not self.opencode_config_loaded:
                try:
                    self.opencode_file = opencode.service_file()
                except opencode.Unavailable as error:
                    self.opencode_config_error = error.reason
                self.opencode_config_loaded = True
            failure = self.opencode_config_error
            if failure is None and self.opencode_file is not None:
                try:
                    registration = opencode.Registration.read(self.opencode_file)
                except opencode.Unavailable as error:
                    failure = error.reason
        if registration != self.opencode_registration:
            self.opencode_registration = registration
            self.opencode_generation += 1
            self.opencode_cycle_at = 0
            self.opencode_pending.clear()
            self.opencode_samples.clear()
            self.opencode_due.clear()
            for job in self.opencode_jobs.values():
                job.cancel()
            for pane_id in live:
                self.publish_opencode(pane_id, opencode.Result(), demos)
        if now >= self.opencode_cycle_at:
            self.opencode_cycle = opencode.Cycle()
            self.opencode_cycle_at = now + USAGE_POLL_SECONDS

        for key, job in list(self.opencode_jobs.items()):
            generation, identifier = key
            interested = any(
                live.get(pane_id) == (identifier, revision) and pane_id not in demos
                for pane_id, revision in job.subscribers.items()
            )
            if generation != self.opencode_generation or not interested:
                job.cancel()
            if not job.future.done():
                continue
            del self.opencode_jobs[key]
            if job.abandoned:
                continue
            try:
                result = job.future.result()
            except Exception:
                result = opencode.Result(reason=opencode.Reason.INVALID_RESPONSE)
            if now - job.started_at >= OPENCODE_SAMPLE_SECONDS:
                result = opencode.Result(reason=opencode.Reason.EXPIRED)
            for pane_id, revision in job.subscribers.items():
                if live.get(pane_id) != (identifier, revision):
                    continue
                self.opencode_samples[pane_id] = result, job.started_at
                self.record_opencode(pane_id, identifier, result, sampled=True)
                self.publish_opencode(pane_id, result, demos)

        for identifier, (queued, subscribers) in list(self.opencode_pending.items()):
            subscribers = {
                pane_id: revision
                for pane_id, revision in subscribers.items()
                if live.get(pane_id) == (identifier, revision) and pane_id not in demos
            }
            if not subscribers or now - queued >= USAGE_POLL_SECONDS:
                del self.opencode_pending[identifier]
                for pane_id in subscribers:
                    result = opencode.Result(reason=opencode.Reason.BUDGET)
                    self.opencode_due[pane_id] = now + USAGE_POLL_SECONDS
                    self.opencode_samples.pop(pane_id, None)
                    self.record_opencode(pane_id, identifier, result, sampled=True)
                    self.publish_opencode(pane_id, result, demos)
                continue
            self.opencode_pending[identifier] = queued, subscribers

        for pane_id, (identifier, revision) in live.items():
            sample = self.opencode_samples.get(pane_id)
            if sample is not None and now - sample[1] >= OPENCODE_SAMPLE_SECONDS:
                self.opencode_samples.pop(pane_id)
                expired = opencode.Result(reason=opencode.Reason.EXPIRED)
                self.record_opencode(pane_id, identifier, expired)
                self.publish_opencode(pane_id, expired, demos)
            if not identifier or failure is not None:
                unavailable = opencode.Result(
                    reason=opencode.Reason.SESSION
                    if not identifier
                    else (failure or opencode.Reason.DISCOVERY)
                )
                self.opencode_samples.pop(pane_id, None)
                self.record_opencode(pane_id, identifier, unavailable)
                self.publish_opencode(pane_id, unavailable, demos)
                continue
            if pane_id in demos or now < self.opencode_due.get(pane_id, 0):
                continue
            key = self.opencode_generation, identifier
            job = self.opencode_jobs.get(key)
            if job is not None:
                if not job.abandoned and pane_id not in job.subscribers:
                    job.subscribers[pane_id] = revision
                    self.opencode_due[pane_id] = now + USAGE_POLL_SECONDS
                continue
            queued, subscribers = self.opencode_pending.setdefault(
                identifier, (now, {})
            )
            subscribers[pane_id] = revision

        while (
            registration is not None
            and self.opencode_pending
            and len(self.opencode_jobs) < 2
        ):
            identifier = next(iter(self.opencode_pending))
            timeout = opencode.COLLECTION_SECONDS
            if self.opencode_action_deadline is not None:
                timeout = min(
                    timeout, max(0, self.opencode_action_deadline - time.monotonic())
                )
            if timeout <= 0:
                break
            _, subscribers = self.opencode_pending.pop(identifier)
            if self.opencode_executor is None:
                self.opencode_executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="ctx-opencode"
                )
            transport = opencode.Transport(timeout=timeout)
            future = self.opencode_executor.submit(
                opencode.collect,
                registration,
                identifier,
                cycle=self.opencode_cycle,
                transport=transport,
            )
            self.opencode_jobs[self.opencode_generation, identifier] = OpenCodeJob(
                future, transport, subscribers
            )
            for pane_id in subscribers:
                self.opencode_due[pane_id] = now + USAGE_POLL_SECONDS
        self.save_opencode_status()

    def close_opencode(self) -> None:
        for job in self.opencode_jobs.values():
            job.cancel()
        self.opencode_jobs.clear()
        self.opencode_pending.clear()
        if self.opencode_executor is not None:
            self.opencode_executor.shutdown(wait=True, cancel_futures=True)
            self.opencode_executor = None

    def refresh_once(self, timeout: float = opencode.COLLECTION_SECONDS) -> bool:
        self.opencode_action_deadline = time.monotonic() + timeout
        try:
            self.refresh_usage()
            while (
                self.opencode_jobs or self.opencode_pending
            ) and time.monotonic() < self.opencode_action_deadline:
                futures = [job.future for job in self.opencode_jobs.values()]
                if futures:
                    finished, _ = wait(
                        futures,
                        timeout=min(
                            0.05,
                            max(0, self.opencode_action_deadline - time.monotonic()),
                        ),
                        return_when=FIRST_COMPLETED,
                    )
                    if not finished:
                        continue
                self.refresh_panes()
                self.refresh_opencode(read_demo_overrides())
            incomplete = bool(self.opencode_jobs or self.opencode_pending)
            if incomplete:
                demos = read_demo_overrides()
                unfinished = {
                    pane_id
                    for job in self.opencode_jobs.values()
                    for pane_id in job.subscribers
                }
                unfinished.update(
                    pane_id
                    for _, subscribers in self.opencode_pending.values()
                    for pane_id in subscribers
                )
                for pane_id in unfinished:
                    identity = self.opencode_identities.get(pane_id)
                    if pane_id in demos or identity is None:
                        continue
                    identifier, _ = identity
                    result = opencode.Result(reason=opencode.Reason.BUDGET)
                    self.opencode_samples.pop(pane_id, None)
                    self.record_opencode(pane_id, identifier, result, sampled=True)
                    self.publish_opencode(pane_id, result, demos)
            return not incomplete
        finally:
            self.close_opencode()

    def rerender(self) -> None:
        demos = read_demo_overrides() if self.opencode_identities else {}
        for pane_id, percent in list(self.percentages.items()):
            if pane_id in self.opencode_identities and pane_id not in demos:
                sample = self.opencode_samples.get(pane_id)
                self.publish_opencode(
                    pane_id, sample[0] if sample else opencode.Result(), demos
                )
                continue
            self.publish(pane_id, percent)

    def schedule_refresh(self) -> None:
        deadline = time.monotonic() + EVENT_SETTLE_SECONDS
        if not self.pending_refresh_at or deadline < self.pending_refresh_at:
            self.pending_refresh_at = deadline

    def run(self) -> None:
        self.opencode_daemon = True
        try:
            self.run_subscription()
        finally:
            self.close_opencode()
            if self.subscription is not None:
                self.subscription.close()
                self.subscription = None

    def run_subscription(self) -> None:
        self.refresh_usage()
        subscription = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.subscription = subscription
        subscription.connect(self.socket_path)
        subscription.settimeout(WIDTH_POLL_SECONDS)
        subscriptions = [
            {"type": event}
            for event in (
                "pane.created",
                "pane.closed",
                "pane.focused",
                "pane.agent_detected",
                "pane.updated",
                "layout.updated",
            )
        ]
        request = {
            "id": f"{SOURCE}:subscribe",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        subscription.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while True:
            try:
                chunk = subscription.recv(65536)
                if not chunk:
                    subscription.close()
                    self.subscription = None
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    data = message.get("data") or {}
                    kind = str(data.get("type") or message.get("event") or "")
                    if kind in ("layout_updated", "layout.updated"):
                        self.refresh_panes()
                        if self.refresh_width():
                            self.rerender()
                    elif kind:
                        self.schedule_refresh()
            except TimeoutError:
                pass
            now = time.monotonic()
            if (
                self.pending_refresh_at and now >= self.pending_refresh_at
            ) or now >= self.next_usage_poll:
                self.refresh_usage()
            else:
                self.refresh_panes()
                if self.refresh_width():
                    self.rerender()
            self.refresh_opencode(read_demo_overrides())


def print_opencode_refresh(watcher: Watcher) -> bool:
    demos = read_demo_overrides()
    states = {
        pane_id: entry
        for pane_id, entry in watcher.opencode_states.items()
        if pane_id not in demos
    }
    support = watcher.herdr_support
    if states and support is not None and support.reason is not None:
        support.print_problem()
        return False
    success = True
    for pane_id, entry in states.items():
        reason = opencode.Reason(entry["reason"])
        if reason == opencode.Reason.AVAILABLE:
            continue
        print(f"OpenCode {pane_id}: usage unavailable ({reason.value}).")
        if reason not in {
            opencode.Reason.NO_USAGE,
            opencode.Reason.SESSION,
            opencode.Reason.MISSING_LIMIT,
        }:
            success = False
    return success


def socket_path() -> str | None:
    value = os.environ.get("HERDR_SOCKET_PATH", "").strip()
    return value if value and os.path.exists(value) else None


def run_daemon(path: str) -> int:
    devnull = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(devnull, descriptor)
    lock_file = acquire_daemon_lock(path)
    if lock_file is None:
        return 0
    with suppress(OSError, KeyboardInterrupt):
        Watcher(path).run()
    return 0


def ensure(path: str, quiet: bool = False) -> int:
    if running_pid(path):
        if not quiet:
            print("Context Bar daemon is already running.")
        return 0
    devnull = os.open(os.devnull, os.O_RDWR)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        stdin=devnull,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,
    )
    os.close(devnull)
    if not quiet:
        print("Context Bar daemon started.")
    return 0


def stop(path: str) -> None:
    pid = running_pid(path)
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and running_pid(path):
        time.sleep(0.05)


def run_demo(path: str, command: str) -> int:
    pane_id = os.environ.get("HERDR_PANE_ID", "").strip()
    if not pane_id:
        print(
            "Context Bar preview needs a focused Herdr pane. Focus one and invoke the action again.",
            file=sys.stderr,
        )
        return 1
    percent = DEMO_LEVELS.get(command)
    if command != "demo-clear" and percent is None:
        return 2
    update_demo_override(pane_id, percent)
    watcher = Watcher(path)
    complete = watcher.refresh_once()
    ensure(path, quiet=True)
    if percent is None:
        print(
            f"Context Bar preview cleared for {pane_id}; live refresh {'completed' if complete else 'incomplete'}."
        )
    else:
        print(
            f"Context Bar preview set to {percent:g}% for {pane_id}; "
            f"it expires in {DEMO_SECONDS // 60} minutes."
        )
    print_opencode_refresh(watcher)
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "ensure"
    if command == "setup":
        result = setup_sidebar()
        path = socket_path()
        if result == 0 and path:
            ensure(path, quiet=True)
        return result
    path = socket_path()
    if not path:
        if command == "status":
            print("Context Bar daemon is not running (no Herdr socket found).")
            return 1
        print("Context Bar must be run inside a Herdr plugin context.", file=sys.stderr)
        return 1
    if command == "daemon":
        return run_daemon(path)
    if command == "ensure":
        return ensure(path)
    if command == "status":
        pid = running_pid(path)
        print(
            f"Context Bar daemon: {'running (pid ' + str(pid) + ')' if pid else 'not running'}"
        )
        print(f"Supported agents: {', '.join(sorted(SUPPORTED_AGENTS))}")
        support = HerdrSupport.from_response(HerdrClient(path).request("ping", {}))
        print_opencode_status(path, current_pid=pid, support=support)
        return 0 if pid else 1
    if command == "restart":
        stop(path)
        return ensure(path)
    if command == "refresh":
        watcher = Watcher(path)
        complete = watcher.refresh_once()
        print(
            "Context bars refreshed."
            if complete
            else "Context bars refreshed; OpenCode collection incomplete."
        )
        successful = print_opencode_refresh(watcher)
        return 0 if complete and successful else 1
    if command in (*DEMO_LEVELS, "demo-clear"):
        return run_demo(path, command)
    print(f"Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
