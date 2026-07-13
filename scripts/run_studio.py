from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


STUDIO_HOST = "127.0.0.1"
STUDIO_PORT = 8765
SIDECAR_HOST = "127.0.0.1"
MINIMUM_NODE_MAJOR = 24
DEFAULT_HEALTH_TIMEOUT = 20.0
DEFAULT_POLL_INTERVAL = 0.1
DEFAULT_TERMINATE_TIMEOUT = 5.0

_NODE_BOOTSTRAP = """
import { createServer } from './rednote_sidecar/src/server.mjs';
import { allocateOutputPaths } from './rednote_sidecar/src/files/output-paths.mjs';

const port = Number.parseInt(process.env.REDNOTE_SIDECAR_PORT ?? '', 10);
const outputRoot = process.env.REDNOTE_OUTPUT_ROOT;
if (!Number.isInteger(port) || port < 1 || port > 65535 || !outputRoot) {
  throw new Error('Invalid sidecar launcher configuration.');
}
const server = createServer({
  allocateOutputPaths: (noteId) => allocateOutputPaths(noteId, { root: outputRoot }),
});
server.listen(port, '127.0.0.1');
""".strip()


class LauncherError(RuntimeError):
    """A stable launcher error that is safe to print."""


def _choose_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((SIDECAR_HOST, 0))
        return int(listener.getsockname()[1])


def _choose_studio_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((STUDIO_HOST, STUDIO_PORT))
        except OSError:
            return _choose_loopback_port()
    return STUDIO_PORT


def _sidecar_is_healthy(url: str) -> bool:
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=0.5) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _command_version(
    executable: str,
    command_runner: Callable[..., Any],
    label: str,
) -> str:
    try:
        result = command_runner(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        raise LauncherError(f"{label}를 실행할 수 없습니다.") from exc
    if getattr(result, "returncode", 1) != 0:
        raise LauncherError(f"{label}를 실행할 수 없습니다.")
    return str(getattr(result, "stdout", "")).strip()


def _verify_prerequisites(
    *,
    which: Callable[[str], str | None],
    command_runner: Callable[..., Any],
) -> tuple[str, str]:
    codex = which("codex")
    if not codex:
        raise LauncherError("Codex CLI가 필요합니다. 설치 후 `codex login`을 실행하세요.")

    node_candidates = [which("node"), str(Path(codex).with_name("node"))]
    node = ""
    seen_candidates: set[str] = set()
    for candidate in node_candidates:
        if not candidate or candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)
        try:
            node_version = _command_version(candidate, command_runner, "Node.js")
        except LauncherError:
            continue
        match = re.match(r"^v?(\d+)(?:\.|$)", node_version)
        if match and int(match.group(1)) >= MINIMUM_NODE_MAJOR:
            node = candidate
            break
    if not node:
        raise LauncherError("Node.js 24 이상이 필요합니다.")

    _command_version(codex, command_runner, "Codex CLI")
    return node, codex


def _wait_for_sidecar(
    process: Any,
    health_url: str,
    *,
    health_check: Callable[[str], bool],
    should_stop: Callable[[], bool],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float,
    poll_interval: float,
) -> bool:
    deadline = clock() + timeout
    while True:
        if should_stop():
            return False
        returncode = process.poll()
        if returncode is not None:
            raise LauncherError(f"RedNote sidecar가 준비 전에 종료되었습니다 (code {returncode}).")
        is_healthy = health_check(health_url)
        if should_stop():
            return False
        if is_healthy:
            return True
        if clock() >= deadline:
            raise LauncherError("RedNote sidecar health check timed out.")
        sleep(poll_interval)


def _reap_processes(processes: list[Any], terminate_timeout: float) -> None:
    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            continue

    for process in processes:
        try:
            process.wait(timeout=terminate_timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=terminate_timeout)
            except Exception:
                pass
        except Exception:
            pass


def _restore_signal_handlers(
    signal_setter: Callable[[int, Any], Any] | None,
    previous_handlers: dict[int, Any],
) -> None:
    if signal_setter is None:
        return
    for signum, previous in previous_handlers.items():
        try:
            signal_setter(signum, previous)
        except Exception:
            pass


def run_studio(
    *,
    project_root: str | Path | None = None,
    output_root: str | Path | None = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
    command_runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    port_factory: Callable[[], int] = _choose_loopback_port,
    studio_port_factory: Callable[[], int] = _choose_studio_port,
    health_check: Callable[[str], bool] = _sidecar_is_healthy,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    key_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    environ: Mapping[str, str] | None = None,
    output: Callable[[str], None] = print,
    signal_setter: Callable[[int, Any], Any] | None = signal.signal,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    terminate_timeout: float = DEFAULT_TERMINATE_TIMEOUT,
) -> int:
    """Run the private RedNote sidecar and local FastAPI studio together."""

    processes: list[Any] = []
    previous_handlers: dict[int, Any] = {}
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    try:
        if (
            health_timeout <= 0
            or poll_interval <= 0
            or terminate_timeout <= 0
        ):
            raise LauncherError("런처 시간 설정이 올바르지 않습니다.")

        node, _codex = _verify_prerequisites(which=which, command_runner=command_runner)
        root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        sidecar_server = root / "rednote_sidecar" / "src" / "server.mjs"
        if not sidecar_server.is_file():
            raise LauncherError("RedNote sidecar 설치를 찾을 수 없습니다.")

        base_environment = dict(os.environ if environ is None else environ)
        configured_output = output_root or base_environment.get("REDNOTE_OUTPUT_ROOT")
        media_root = Path(configured_output or (Path.home() / "Downloads" / "rednote")).resolve()
        sidecar_port = int(port_factory())
        if sidecar_port < 1 or sidecar_port > 65_535:
            raise LauncherError("RedNote sidecar 포트를 선택하지 못했습니다.")
        studio_port = int(studio_port_factory())
        if studio_port < 1 or studio_port > 65_535 or studio_port == sidecar_port:
            raise LauncherError("Studio 포트를 선택하지 못했습니다.")
        sidecar_key = key_factory()
        if not isinstance(sidecar_key, str) or len(sidecar_key) < 16:
            raise LauncherError("RedNote sidecar 인증 키를 만들지 못했습니다.")

        sidecar_url = f"http://{SIDECAR_HOST}:{sidecar_port}"
        shared_environment = {
            **base_environment,
            "REDNOTE_SIDECAR_URL": sidecar_url,
            "REDNOTE_SIDECAR_KEY": sidecar_key,
            "REDNOTE_OUTPUT_ROOT": str(media_root),
        }
        sidecar_environment = {
            **shared_environment,
            "REDNOTE_SIDECAR_PORT": str(sidecar_port),
        }
        studio_environment = dict(shared_environment)

        if signal_setter is not None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal_setter(signum, request_stop)

        sidecar = process_factory(
            [node, "--input-type=module", "--eval", _NODE_BOOTSTRAP],
            cwd=str(root),
            env=sidecar_environment,
        )
        processes.append(sidecar)
        sidecar_ready = _wait_for_sidecar(
            sidecar,
            f"{sidecar_url}/api/health",
            health_check=health_check,
            should_stop=lambda: stop_requested,
            clock=clock,
            sleep=sleep,
            timeout=health_timeout,
            poll_interval=poll_interval,
        )
        if not sidecar_ready:
            return 0

        studio = process_factory(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "codex_coupang_workbench.main:app",
                "--host",
                STUDIO_HOST,
                "--port",
                str(studio_port),
            ],
            cwd=str(root),
            env=studio_environment,
        )
        processes.append(studio)
        output(f"Studio: http://{STUDIO_HOST}:{studio_port}")
        output(f"RedNote sidecar health: {sidecar_url}/api/health")

        while not stop_requested:
            for name, process in (("RedNote sidecar", sidecar), ("Uvicorn", studio)):
                returncode = process.poll()
                if returncode is not None:
                    output(f"{name} exited early (code {returncode}).")
                    return returncode if returncode != 0 else 1
            sleep(poll_interval)
        return 0
    except LauncherError as exc:
        output(str(exc))
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        output("Studio launcher failed.")
        return 1
    finally:
        _reap_processes(processes, terminate_timeout)
        _restore_signal_handlers(signal_setter, previous_handlers)


if __name__ == "__main__":
    raise SystemExit(run_studio())
