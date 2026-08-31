from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path
from .runtime_flags import ulog

_LOGGED_CHROMIUM_PATH: str | None = None


def _chromium_path() -> str:
    """Locate a Chromium/Edge binary and log the resolved path once per change."""
    resolved = _resolve_chromium_path()
    global _LOGGED_CHROMIUM_PATH
    if resolved != _LOGGED_CHROMIUM_PATH:
        _LOGGED_CHROMIUM_PATH = resolved
        ulog(f"Chromium binary resolved to: {resolved}")
    return resolved


def _resolve_chromium_path() -> str:
    """Locate a Chromium/Edge binary for the current platform."""
    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return shutil.which("chromium") or shutil.which("chrome") or "chromium"
    if platform.system() == "Darwin":
        return "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    configured = os.environ.get("CHROME_BIN")
    if configured and shutil.which(configured):
        return configured
    # Linux (container default): prefer full Chromium. The headless-shell build
    # cannot complete the Microsoft SSO redirect chain (it lands on
    # login.microsoftonline.com and fails to capture a fresh substrate token),
    # so it must never be preferred for the refresh flow.
    return (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("microsoft-edge")
        or shutil.which("microsoft-edge-stable")
        or "chromium"
    )


def _popen_kwargs_for_chromium() -> dict:
    """Kwargs so Chromium is launched in its own process group (POSIX).

    Chromium forks crashpad/zygote/utility children. If we only wait()/kill the
    Popen parent, orphans get reparented to container PID 1. When PID 1 is `uv`
    (not an init/reaper), those children become permanent zombies. A dedicated
    session/process group lets close/cleanup signal the whole tree.
    """
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() != "Windows":
        # start_new_session=True => setsid(); child pid becomes the process-group leader.
        kwargs["start_new_session"] = True
    return kwargs


def _launch_chromium(args: list[str]) -> subprocess.Popen:
    """Launch Chromium with process-group isolation (see _popen_kwargs_for_chromium)."""
    return subprocess.Popen(args, **_popen_kwargs_for_chromium())


def _signal_chromium_tree(proc: subprocess.Popen, sig: signal.Signals) -> None:
    """Deliver *sig* to the Chromium process group when possible, else the proc itself."""
    if proc.poll() is not None:
        return
    if platform.system() != "Windows":
        try:
            # When launched with start_new_session=True, pgid == pid.
            os.killpg(proc.pid, sig)
            return
        except ProcessLookupError:
            return
        except Exception:
            pass
    try:
        proc.send_signal(sig)
    except Exception:
        pass


def _reap_chromium_tree(proc: subprocess.Popen, timeout: float) -> None:
    """Best-effort wait for the main Chromium process to exit."""
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass


async def _close_chromium_gracefully(cdp_port: int, proc: subprocess.Popen | None) -> None:
    """Shut down a Chromium instance started by _launch_chromium / Popen.

    Order: Browser.close via CDP → wait → SIGTERM process group → wait → SIGKILL group.
    Always tries to reap the Popen handle so we do not leave zombies of the leader.
    """
    if proc is None:
        return
    if proc.poll() is not None:
        # Leader already exited; still try a non-blocking wait to reap if needed.
        try:
            proc.wait(timeout=0)
        except Exception:
            pass
        return

    # 1) Ask Chromium to exit cleanly over CDP (closes renderers cooperatively).
    try:
        import httpx
        import websockets
        async with httpx.AsyncClient(timeout=2) as client:
            info = (await client.get(f"http://localhost:{cdp_port}/json/version")).json()
        ws_url = info.get("webSocketDebuggerUrl")
        if ws_url:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        await asyncio.to_thread(_reap_chromium_tree, proc, 10.0)
    except Exception:
        pass

    if proc.poll() is not None:
        try:
            proc.wait(timeout=0)
        except Exception:
            pass
        return

    # 2) SIGTERM the whole process group (parent + crashpad + zygote + utilities).
    try:
        await asyncio.to_thread(_signal_chromium_tree, proc, signal.SIGTERM)
        await asyncio.to_thread(_reap_chromium_tree, proc, 8.0)
    except Exception:
        pass

    if proc.poll() is not None:
        try:
            proc.wait(timeout=0)
        except Exception:
            pass
        return

    # 3) Last resort: SIGKILL the group, then reap the leader.
    try:
        await asyncio.to_thread(_signal_chromium_tree, proc, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.to_thread(_reap_chromium_tree, proc, 3.0)
    except Exception:
        pass


def _iter_pids_matching_profile(profile_dir: Path) -> list[int]:
    """PIDs whose cmdline references this profile's --user-data-dir (Linux /proc)."""
    if platform.system() == "Windows":
        return []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    profile = str(profile_dir.resolve())
    profile_arg = str(profile_dir)
    self_pid = os.getpid()
    hits: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes().decode("utf-8", "ignore")
        except Exception:
            continue
        if "--user-data-dir=" in raw and (profile in raw or profile_arg in raw):
            hits.append(pid)
    return hits


def _pgids_for_pids(pids: list[int]) -> set[int]:
    """Resolve process-group IDs for the given PIDs (best-effort)."""
    pgids: set[int] = set()
    for pid in pids:
        try:
            pgids.add(os.getpgid(pid))
        except Exception:
            pgids.add(pid)
    return pgids


def _cleanup_profile_locks(profile_dir: Path) -> None:
    """Stop stale Chromium processes for this profile and remove Singleton locks.

    Prefer signalling whole process groups (so crashpad/zygote die with the
    browser). Fall back to per-PID kill for anything still matching the profile.
    """
    if platform.system() != "Windows":
        pids = _iter_pids_matching_profile(profile_dir)
        if pids:
            for pgid in _pgids_for_pids(pids):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pgid, signal.SIGTERM)
                    except Exception:
                        pass
            time.sleep(0.3)
            # Second pass: anything still alive matching the profile → SIGKILL group/pid.
            pids = _iter_pids_matching_profile(profile_dir)
            for pgid in _pgids_for_pids(pids):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        os.kill(pgid, signal.SIGKILL)
                    except Exception:
                        pass
            # Brief moment for the kernel / PID1 to reap.
            time.sleep(0.1)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except Exception:
            pass
