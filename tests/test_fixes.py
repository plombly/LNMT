import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import proxy


def test_a_failing_nginx_test_restores_the_previous_site_exactly(sandbox):
    from lnmt import nginx
    p = proxy()
    nginx.install_nginx_config(p)
    site = sandbox["avail"] / "app.example.com"
    before = site.read_bytes()
    sandbox["results"]["test"] = False
    with pytest.raises(RuntimeError, match="validation failed"):
        nginx.install_nginx_config(proxy(backend_port=4000))
    assert site.read_bytes() == before
    assert os.readlink(sandbox["enabled"] / "app.example.com") == str(site)


def test_a_new_site_that_fails_leaves_nothing_behind(sandbox):
    from lnmt import nginx
    sandbox["results"]["test"] = False
    with pytest.raises(RuntimeError):
        nginx.install_nginx_config(proxy())
    assert list(sandbox["avail"].iterdir()) == [] and list(sandbox["enabled"].iterdir()) == []


def test_the_new_file_itself_is_what_gets_tested(sandbox, monkeypatch):
    from lnmt import nginx
    seen = []
    monkeypatch.setattr(nginx, "nginx_test", lambda: (seen.append(
        (sandbox["avail"] / "app.example.com").read_text()) or True, ""))
    nginx.install_nginx_config(proxy(backend_port=4321))
    assert "proxy_pass http://192.0.2.5:4321;" in seen[0]   # no dot-named candidate nginx would skip


def test_a_disabled_proxy_stays_disabled(sandbox):
    from lnmt import nginx
    nginx.install_nginx_config(proxy(enabled=False))
    assert (sandbox["avail"] / "app.example.com").exists()
    assert not (sandbox["enabled"] / "app.example.com").is_symlink()


def test_a_failed_reload_restores_and_reloads_the_old_config(sandbox):
    from lnmt import nginx
    nginx.install_nginx_config(proxy())
    before = (sandbox["avail"] / "app.example.com").read_bytes()
    sandbox["results"]["reload"] = False
    with pytest.raises(RuntimeError, match="reload failed"):
        nginx.install_nginx_config(proxy(backend_port=5000))
    assert (sandbox["avail"] / "app.example.com").read_bytes() == before
    assert len(sandbox["calls"]["reload"]) == 3      # ok, failed, then the restored config


def test_the_state_lock_is_reentrant_and_excludes_other_processes(sandbox):
    from lnmt import state
    lock = state.LOCK_FILE
    code = ("import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR); "
            "t=time.time(); fcntl.flock(fd,fcntl.LOCK_EX); print(round(time.time()-t,1))")
    with state.state_lock():
        with state.state_lock():
            pass
        other = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True)
        time.sleep(0.6)
    waited = float(other.communicate(timeout=10)[0])
    assert waited >= 0.5


def test_ddns_runs_while_holding_the_lock(sandbox, monkeypatch):
    from lnmt import ddns, state
    held = []
    monkeypatch.setattr(ddns.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ddns, "_run", lambda: held.append(state._held) or 0)
    assert ddns.main() == 0 and held == [1]


def test_records_are_found_by_type_and_the_stored_id(monkeypatch):
    from lnmt import cloudflare
    asked = []
    records = [{"id": "txt1", "type": "A"}, {"id": "mine", "type": "A"}]
    monkeypatch.setattr(cloudflare, "cf_api", lambda token, method, path, data=None: asked.append(path) or {"result": records})
    cred = {"token": "t", "zone_id": "z"}
    assert cloudflare.cf_get_record(cred, "app.example.com", record_type="A", record_id="mine")["id"] == "mine"
    assert "type=A" in asked[0]
    assert "type=" not in cloudflare.cf_get_record(cred, "app.example.com") and "type=" not in asked[1]


def test_public_ip_must_be_a_public_ipv4(monkeypatch):
    from lnmt import cloudflare

    class Reply:
        def __init__(self, text): self.text = text
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.text.encode()

    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", lambda req, timeout: Reply("73.1.2.3\n"))
    assert cloudflare.get_public_ip() == "73.1.2.3"
    for bad in ("<html>error</html>", "192.0.2.1", "::1"):
        monkeypatch.setattr(cloudflare.urllib.request, "urlopen", lambda req, timeout, b=bad: Reply(b))
        with pytest.raises(ValueError):
            cloudflare.get_public_ip()


def test_a_failed_basic_auth_change_leaves_state_unchanged(sandbox, monkeypatch):
    from lnmt import access, nginx, state
    p = proxy(enabled=False, access_protection={"enabled": False, "realm": "Old", "auth_file": "/x"})
    state.save_state({"version": 1, "zones": {}, "proxies": {"app.example.com": p}, "ddns": {"records": {}}})
    sandbox["results"]["test"] = False

    def change(proxy_record):
        proxy_record["access_protection"]["realm"] = "New"

    with pytest.raises(RuntimeError):
        access._commit("app.example.com", change)
    saved = json.loads(state.STATE_FILE.read_text())["proxies"]["app.example.com"]
    assert saved["access_protection"]["realm"] == "Old"
    assert not (sandbox["enabled"] / "app.example.com").is_symlink()   # still disabled


def test_the_realm_cannot_inject_nginx_syntax():
    from lnmt import nginx
    text = nginx.render_nginx(proxy(access_protection={"enabled": True, "realm": 'a"$x\\y', "auth_file": "/f"}))
    assert 'auth_basic "ax y";' not in text and 'auth_basic "axy";' in text


def test_tls_failure_never_touches_the_live_key(tmp_path, monkeypatch):
    from lnmt import tls
    zone_dir = tmp_path / "example.com"
    zone_dir.mkdir()
    (zone_dir / "origin.key").write_text("LIVE KEY")
    (zone_dir / "origin.pem").write_text("LIVE CERT")
    monkeypatch.setattr(tls, "cert_paths", lambda zone: (zone_dir, zone_dir / "origin.pem", zone_dir / "origin.key", zone_dir / "origin.json"))
    monkeypatch.setattr(tls, "load_credential", lambda zone: {"token": "t", "zone_id": "z"})
    monkeypatch.setattr(tls, "cert_valid", lambda cert: False)

    def fake_csr(zone, directory):
        key, csr = directory / "origin.key.new", directory / "origin.csr"
        key.write_text("NEW KEY")
        csr.write_text("CSR")
        return key, csr

    monkeypatch.setattr(tls, "generate_key_and_csr", fake_csr)
    monkeypatch.setattr(tls, "cf_api", lambda *a, **k: {"success": False})
    with pytest.raises(RuntimeError):
        tls.issue("example.com")
    assert (zone_dir / "origin.key").read_text() == "LIVE KEY"
    assert sorted(p.name for p in zone_dir.iterdir()) == ["origin.key", "origin.pem"]


def test_backup_pruning_keeps_the_newest_and_never_other_files(tmp_path):
    import importlib.machinery
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "bin/lnmt-backup"
    loader = importlib.machinery.SourceFileLoader("lnmt_backup", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("lnmt_backup", loader))
    loader.exec_module(module)
    for stamp in ("20261001-010000", "20261002-010000", "20261003-010000"):
        (tmp_path / f"lnmt-backup-{stamp}.tar.gz").write_text("x")
        (tmp_path / f"lnmt-backup-{stamp}.tar.gz.sha256").write_text("x")
    (tmp_path / "lnmt-v2.1-final.tar.gz").write_text("release")
    module.prune(tmp_path, 2)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "lnmt-backup-20261002-010000.tar.gz", "lnmt-backup-20261002-010000.tar.gz.sha256",
        "lnmt-backup-20261003-010000.tar.gz", "lnmt-backup-20261003-010000.tar.gz.sha256",
        "lnmt-v2.1-final.tar.gz"]


def test_saved_firewall_ranges_are_rewritten_in_place():
    from lnmt import edge
    conf = """table inet filter {
    set cloudflare_ipv4 {
        type ipv4_addr
        flags interval
        elements = {
            1.0.0.0/8,
            2.0.0.0/8
        }
    }
    set cloudflare_ipv6 {
        type ipv6_addr
        flags interval
        elements = { 2400:cb00::/32 }
    }
    chain input {
        ip saddr 192.0.2.0/24 tcp dport 22 accept
    }
}
"""
    out = edge.with_ranges(conf, {"ipv4_cidrs": ["3.0.0.0/8", "4.0.0.0/8"], "ipv6_cidrs": ["2c0f:f248::/32"]})
    assert "3.0.0.0/8,\n            4.0.0.0/8" in out and "1.0.0.0/8" not in out and "2c0f:f248::/32" in out
    assert "tcp dport 22 accept" in out and out.count("elements") == 2
    assert edge.with_ranges("table inet filter {}", {"ipv4_cidrs": [], "ipv6_cidrs": []}) is None


def test_site_options_render_into_the_site(tmp_path, monkeypatch):
    from lnmt import nginx
    proxy = {"hostname": "a.example.com", "backend_ip": "192.0.2.5", "backend_port": 80, "backend_protocol": "http",
             "certificate": "/c.pem", "certificate_key": "/c.key"}
    monkeypatch.setattr(nginx, "COMMON_CONF", tmp_path / "common.conf")
    plain = nginx.render_nginx(proxy)
    assert "access_log" not in plain and "limit_req" not in plain       # before the shared file: as before
    (tmp_path / "common.conf").write_text(nginx.COMMON)
    full = nginx.render_nginx(dict(proxy, options={
        "rate_limit": 20, "security_headers": True, "friendly_errors": True,
        "redirects": [{"from": "/old", "to": "/new", "code": 301}]}))
    assert "limit_req zone=lnmt_rl_20 burst=40 nodelay;" in full and "Strict-Transport-Security" in full
    assert "location = /old { return 301 /new; }" in full and "error_page 502 504 /lnmt-error.html;" in full
    assert "access_log /var/log/nginx/access.log;" in full and "lnmt_count" in full
    down = nginx.render_nginx(dict(proxy, options={"maintenance": True}))
    assert "return 503" in down and "proxy_pass" not in down
    assert nginx.render_nginx(dict(proxy, options={"rate_limit": 7})).count("limit_req ") == 0   # unknown rates ignored


def test_node_calls_may_carry_an_argument_named_action(monkeypatch):
    """App Start/Stop/Restart send action=…; it must not clash with the call's own name."""
    from lnmt import hosting
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, '{"ok": true, "result": {"done": 1}}', ""
    monkeypatch.setattr(hosting, "node", lambda node_id=None: {"id": "n", "ip": "192.0.2.121"})
    monkeypatch.setattr(hosting.subprocess, "run", lambda argv, **kw: seen.update(input=kw.get("input")) or Done())
    hosting.call("app_action", customer="sam", name="shop", action="stop")
    assert '"action": "app_action"' in seen["input"] and '"action": "stop"' in seen["input"]


def test_dns_records_are_checked_and_sites_records_stay_with_the_site(monkeypatch):
    from lnmt import service, cloudflare
    calls = []
    monkeypatch.setattr(cloudflare, "load_credential", lambda zone: {"token": "t", "zone_id": "z", "owner": "sam"} if zone == "example.com" else None)
    monkeypatch.setattr(service, "load_state", lambda: {"proxies": {"shop.example.com": {"zone": "example.com"}}, "ddns": {}})

    def fake_api(token, method, path, data=None):
        calls.append((method, path, data))
        if method == "GET":
            return {"result": {"id": "a" * 32, "name": "shop.example.com", "type": "A"}}
        return {"result": {"id": "b" * 32, "name": data["name"] if data else "", "type": data["type"] if data else ""}}
    monkeypatch.setattr(cloudflare, "cf_api", fake_api)
    made = service.dns_record_set("example.com", rtype="MX", name="@", content="Mail.Example.com.", priority=5, owner="sam", by="sam")
    assert made["name"] == "example.com" and calls[-1][2]["content"] == "mail.example.com" and calls[-1][2]["priority"] == 5
    service.dns_record_set("example.com", rtype="A", name="www", content="203.0.113.7", proxied=True, owner="sam")
    assert calls[-1][2] == {"type": "A", "name": "www.example.com", "content": "203.0.113.7", "ttl": 1, "proxied": True,
                            "comment": "Added in LNMT"}
    service.dns_record_set("example.com", rtype="TXT", name="shop", content="v=spf1 -all", owner="sam")   # a site's TXT is free
    assert calls[-1][2]["name"] == "shop.example.com"
    for bad in ({"rtype": "A", "name": "x", "content": "::1"}, {"rtype": "CNAME", "name": "x", "content": "not a host"},
                {"rtype": "NS", "name": "x", "content": "ns.example.net"}, {"rtype": "A", "name": "shop", "content": "1.2.3.4"},
                {"rtype": "TXT", "name": "bad name!", "content": "x"}):
        with pytest.raises(service.ServiceError):
            service.dns_record_set("example.com", owner="sam", **bad)
    with pytest.raises(service.ServiceError):                    # someone else's domain
        service.dns_record_set("example.com", rtype="A", name="x", content="1.2.3.4", owner="kim")
    with pytest.raises(service.ServiceError):                    # the site's own record: only through the site
        service.dns_record_delete("example.com", "a" * 32, owner="sam")
    assert not [c for c in calls if c[0] == "DELETE"]


def test_technical_records_are_sent_as_checked_parts(monkeypatch):
    from lnmt import service, cloudflare
    sent = []
    monkeypatch.setattr(cloudflare, "load_credential", lambda zone: {"token": "t", "zone_id": "z", "owner": ""})
    monkeypatch.setattr(service, "load_state", lambda: {"proxies": {"shop.example.com": {"zone": "example.com"}}, "ddns": {}})
    monkeypatch.setattr(cloudflare, "cf_api", lambda t, m, p, data=None: sent.append(data) or {"result": {"id": "c" * 32, "name": data["name"], "type": data["type"]}})
    service.dns_record_set("example.com", rtype="SRV", name="_minecraft._tcp",
                           data={"priority": "0", "weight": "5", "port": "25565", "target": "Play.Example.com."})
    assert sent[-1]["name"] == "_minecraft._tcp.example.com"
    assert sent[-1]["data"] == {"priority": 0, "weight": 5, "port": 25565, "target": "play.example.com"}
    service.dns_record_set("example.com", rtype="CAA", name="@", data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"})
    service.dns_record_set("example.com", rtype="NS", name="lab", data={"content": "ns1.other.net"})
    assert sent[-1]["content"] == "ns1.other.net" and "data" not in sent[-1]
    service.dns_record_set("example.com", rtype="URI", name="_ftp._tcp", data={"priority": 10, "weight": 1, "target": "ftp://x.example.com/"})
    assert sent[-1]["priority"] == 10 and sent[-1]["data"] == {"weight": 1, "target": "ftp://x.example.com/"}
    for bad in ({"rtype": "NS", "name": "@", "data": {"content": "ns1.other.net"}},        # would take the domain away
                {"rtype": "NS", "name": "shop", "data": {"content": "ns1.other.net"}},     # would take a site away
                {"rtype": "DS", "name": "@", "data": {"key_tag": 1, "algorithm": 13, "digest_type": 2, "digest": "ab"}},
                {"rtype": "SRV", "name": "_x._tcp", "data": {"priority": 0, "weight": 0, "port": 70000, "target": "a.b"}},
                {"rtype": "CAA", "name": "@", "data": {"flags": 0, "tag": "bogus", "value": "x"}},
                {"rtype": "TLSA", "name": "_443._tcp", "data": {"usage": 3, "selector": 1, "matching_type": 1, "certificate": "zz"}},
                {"rtype": "HINFO", "name": "x", "data": {}}):
        with pytest.raises(service.ServiceError):
            service.dns_record_set("example.com", **bad)
    detail_types = service.TECH_TYPES.keys()
    assert {"SRV", "CAA", "NS", "TLSA", "SSHFP", "HTTPS", "SVCB", "LOC", "NAPTR", "URI", "PTR", "DS", "DNSKEY", "CERT",
            "SMIMEA", "OPENPGPKEY"} <= set(detail_types)
