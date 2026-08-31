from __future__ import annotations

from pathlib import Path


DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_installs_full_chromium_only():
    # The chromium-headless-shell package pulls a different Chromium build that
    # fails to bind the CDP debug port during the on-demand refresh flow
    # ("[Errno 99] Cannot assign requested address"). Match the known-good v8
    # setup: install full Chromium only.
    assert "chromium-headless-shell" not in DOCKERFILE
    assert "chromium \\" in DOCKERFILE
    assert "chromium-common" in DOCKERFILE


def test_dockerfile_uses_tini_as_pid1_reaper():
    # Without an init, orphaned Chromium children become permanent zombies under
    # `uv` as PID 1. tini reaps them as a safety net on top of process-group kill.
    assert "tini" in DOCKERFILE
    assert 'ENTRYPOINT ["tini", "--", "/entrypoint.sh"]' in DOCKERFILE
