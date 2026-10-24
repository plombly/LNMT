"""Tests run against src/ as the package lnmt, with every system path
pointed into a temporary folder and no real nginx, openssl or Cloudflare."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_pkg_parent = Path(tempfile.mkdtemp(prefix="lnmt-pkg-"))
(_pkg_parent / "lnmt").symlink_to(ROOT / "src")
sys.path.insert(0, str(_pkg_parent))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from lnmt import nginx, state, access
    avail, enabled = tmp_path / "sites-available", tmp_path / "sites-enabled"
    avail.mkdir()
    enabled.mkdir()
    monkeypatch.setattr(nginx, "NGINX_AVAILABLE", avail)
    monkeypatch.setattr(nginx, "NGINX_ENABLED", enabled)
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state, "LOCK_FILE", tmp_path / "lnmt.lock")
    calls = {"test": [], "reload": []}
    results = {"test": True, "reload": True}

    def fake_test():
        calls["test"].append(1)
        return results["test"], "nginx -t output"

    def fake_reload():
        calls["reload"].append(1)
        return results["reload"], "reload output"

    for module in (nginx,):
        monkeypatch.setattr(module, "nginx_test", fake_test)
        monkeypatch.setattr(module, "nginx_reload", fake_reload)
    return {"avail": avail, "enabled": enabled, "calls": calls, "results": results, "tmp": tmp_path}


def proxy(**extra):
    return {"hostname": "app.example.com", "zone": "example.com", "backend_ip": "192.0.2.5",
            "backend_port": 3000, "backend_protocol": "http",
            "certificate": "/etc/lnmt/certs/example.com/origin.pem",
            "certificate_key": "/etc/lnmt/certs/example.com/origin.key", "enabled": True, **extra}
