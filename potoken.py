"""PO-token provider lifecycle for Sourcer.

YouTube now withholds media streams unless yt-dlp presents a *PO token* (proof of
origin). Cookies establish identity but no longer release the streams on their
own — without a token you only get storyboard thumbnails.

We solve this the way the yt-dlp project recommends: run the bgutil provider as a
local Docker container. The bgutil yt-dlp plugin (installed in the venv) then
fetches fresh, video-bound tokens from it automatically at
http://127.0.0.1:4416 — no per-download work, nothing that goes stale.

This module keeps that container alive so the user never has to think about it:
on startup it ensures the container is running (starting Docker's engine first if
needed) and exposes a health check the Settings UI can display.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

CONTAINER = "sourcer-pot-provider"
IMAGE = "brainicism/bgutil-ytdlp-pot-provider"
PORT = 4416
PING_URL = f"http://127.0.0.1:{PORT}/ping"

# Hide the transient console windows the docker CLI would otherwise flash on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_ensure_lock = threading.Lock()
_status: dict = {"state": "unknown", "detail": "not checked yet", "version": None}


def _run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 1, "", str(e)


def ping(timeout: float = 4.0) -> dict | None:
    """Return the provider's /ping payload if it's reachable, else None."""
    try:
        with urllib.request.urlopen(PING_URL, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    rc, _, _ = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)
    return rc == 0


def _find_docker_desktop() -> str | None:
    for p in (
        os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
        os.path.expandvars(r"%ProgramW6432%\Docker\Docker\Docker Desktop.exe"),
        os.path.expandvars(r"%LocalAppData%\Docker\Docker Desktop.exe"),
    ):
        if p and os.path.isfile(p):
            return p
    return None


def _start_docker_engine(wait_s: int = 90) -> bool:
    """Best-effort: launch Docker Desktop and wait for the daemon. Non-fatal."""
    exe = _find_docker_desktop()
    if not exe:
        return False
    try:
        subprocess.Popen([exe], creationflags=_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _docker_ok():
            return True
        time.sleep(3)
    return False


def _container_state() -> str:
    """'running' | 'stopped' | 'absent' (assumes the daemon is reachable)."""
    rc, out, _ = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], timeout=15
    )
    if rc != 0:
        return "absent"
    return "running" if out.strip() == "true" else "stopped"


def _set(state: str, detail: str, version=None) -> dict:
    _status.update(state=state, detail=detail, version=version)
    return dict(_status)


def ensure() -> dict:
    """Make the provider healthy if possible; return a status dict.

    States: healthy | starting | docker_down | no_docker | error.
    Safe to call repeatedly; serialized so concurrent callers don't race Docker.
    """
    with _ensure_lock:
        info = ping()
        if info:
            return _set("healthy", "PO-token provider is running.",
                        info.get("version"))

        if not _docker_ok():
            if not shutil.which("docker"):
                return _set("no_docker",
                            "Docker isn't installed — the PO-token provider can't run. "
                            "Install Docker Desktop, then restart Sourcer.")
            if not _start_docker_engine():
                return _set("docker_down",
                            "Docker Desktop isn't running and couldn't be started "
                            "automatically. Start Docker Desktop, then restart Sourcer.")

        state = _container_state()
        if state == "running":
            # Container is up but not answering yet — give it a moment to bind.
            for _ in range(10):
                time.sleep(2)
                info = ping()
                if info:
                    return _set("healthy", "PO-token provider is running.",
                                info.get("version"))
            return _set("starting", "Provider container is up but not responding yet.")

        if state == "stopped":
            _run(["docker", "start", CONTAINER], timeout=30)
        else:  # absent — create it (pulls the image on first run)
            _run([
                "docker", "run", "-d", "--name", CONTAINER,
                "--restart", "unless-stopped", "-p", f"{PORT}:{PORT}",
                "--init", IMAGE,
            ], timeout=300)

        for _ in range(15):
            info = ping()
            if info:
                return _set("healthy", "PO-token provider is running.",
                            info.get("version"))
            time.sleep(2)
        return _set("starting",
                    "Provider is starting (first run pulls the image). "
                    "Refresh in a moment.")


def ensure_async() -> None:
    """Kick off ensure() in the background so app startup never blocks on Docker."""
    threading.Thread(target=ensure, name="pot-ensure", daemon=True).start()


def status() -> dict:
    """Cheap status for the UI: a live ping refreshes 'healthy' without touching Docker."""
    info = ping(timeout=2.0)
    if info:
        return _set("healthy", "PO-token provider is running.", info.get("version"))
    return dict(_status)
