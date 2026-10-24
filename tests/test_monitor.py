"""Uptime checks and alerts: alert once per change, never on the first run."""
import json

import pytest


@pytest.fixture
def mon(tmp_path, monkeypatch):
    from lnmt import monitor, state
    monkeypatch.setattr(monitor, "MONITOR_FILE", tmp_path / "monitor.json")
    monkeypatch.setattr(monitor, "NOTIFY_FILE", tmp_path / "notify.json")
    monkeypatch.setattr(monitor, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(monitor, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(monitor, "PEOPLE_DIR", tmp_path / "people")
    monkeypatch.setattr(monitor, "SITE_LOGS", tmp_path / "logs")
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(state, "LOCK_FILE", tmp_path / "lock")
    state.save_state({"version": 1, "zones": {}, "ddns": {"records": {}, "status": "HEALTHY", "public_ip": "1.1.1.1"},
                      "proxies": {"a.example.com": {"hostname": "a.example.com", "zone": "example.com", "enabled": True,
                                                    "backend_ip": "192.0.2.5", "backend_port": 80, "backend_protocol": "http"}}})
    world = {"ok": True, "days": 400, "backup": "", "sent": [], "to": [], "node": None, "people": {}}

    def sender(title, message, config):
        world["sent"].append(title)
        world["to"].append((config.get("ntfy_url"), title))
        return 1

    def run(now):
        return monitor.run(now=now, tester=lambda p: (world["ok"], "HTTP 200" if world["ok"] else "TCP connection failed"),
                           sender=sender, cert_days=lambda p: world["days"], backup_problem=lambda: world["backup"],
                           node_stats=lambda: world["node"], people=lambda: world["people"])
    return monitor, state, world, run, tmp_path


def test_a_site_alerts_after_two_failures_and_once_when_back(mon):
    monitor, state, world, run, _ = mon
    run(1000)
    world["ok"] = False
    run(1120)
    assert world["sent"] == []                                  # one failure is not an outage
    run(1240)
    run(1360)
    assert world["sent"] == ["a.example.com is down"]           # once
    world["ok"] = True
    run(1480)
    assert world["sent"] == ["a.example.com is down", "a.example.com is back"]
    up = monitor.uptime("a.example.com", now=1500)
    assert up["up"] is True and up["day"] == 40.0


def test_first_run_learns_and_later_changes_alert(mon):
    monitor, state, world, run, tmp_path = mon
    world["days"], world["backup"] = 10, "the newest backup is 60 hours old"
    run(1000)
    assert world["sent"] == []                                  # existing problems: learned, not announced
    world["backup"] = ""
    data = state.load_state()
    data["ddns"].update(status="DEGRADED", public_ip="2.2.2.2", message="API failed")
    state.save_state(data)
    run(1120)
    assert sorted(world["sent"]) == ["Backups are working again", "Dynamic DNS has problems", "Public IP changed"]


def test_failed_sign_ins_and_switched_off_events(mon):
    monitor, state, world, run, tmp_path = mon
    run(1000)
    audit = tmp_path / "audit.jsonl"
    audit.write_text("".join(json.dumps({"at": 1050 + i, "action": "login", "ok": False}) + "\n" for i in range(5)))
    monitor.save_notify_config(events={"logins": False})
    run(1100)
    assert world["sent"] == []
    monitor.save_notify_config(events={"logins": True})
    with audit.open("a") as handle:
        handle.write(json.dumps({"at": 1200, "action": "login", "ok": False}) + "\n")
    run(1300)
    assert world["sent"] == []                                  # the condition was already "many" (learned while off)


def test_targets_are_https_only_and_never_shown(mon, monkeypatch):
    monitor, _, _, _, tmp_path = mon
    with pytest.raises(ValueError):
        monitor.save_notify_config(discord_webhook="http://example.com/hook")
    shown = monitor.save_notify_config(discord_webhook="https://discord.com/api/webhooks/1/secret", ntfy_url="")
    assert shown["discord"] is True and "secret" not in json.dumps(shown)
    assert oct((tmp_path / "notify.json").stat().st_mode)[-3:] == "600"
    sent = []

    class Reply:
        def close(self):
            pass

    assert monitor.send("Title", "Body", opener=lambda req, timeout: sent.append(req.full_url) or Reply()) == 1
    assert sent == ["https://discord.com/api/webhooks/1/secret"]


def _node(state="active", build=("deployed", 0), used=10):
    return {"customers": [{"customer": "sam", "disk_used": used, "disk_total": 100,
                           "apps": [{"name": "shop", "state": state, "cpu": 3.5, "memory": 2 ** 20,
                                     "build_state": build[0], "build_finished": build[1]}]}]}


def test_app_owner_hears_about_their_app_and_sites(mon):
    monitor, state, world, run, tmp_path = mon
    world["people"] = {"sam": {"customer": "sam", "sites": ["a.example.com"], "admin": False},
                       "kim": {"customer": "kim", "sites": [], "admin": False}}
    monitor.save_person_config("sam", ntfy_url="https://ntfy.sh/sam")
    monitor.save_person_config("kim", ntfy_url="https://ntfy.sh/kim")
    monitor.save_notify_config(ntfy_url="https://ntfy.sh/admin")
    world["node"] = _node()
    run(1000)
    world["node"] = _node(state="failed")
    run(1120)
    assert world["sent"] == []                                  # one bad look is not an outage
    run(1240)
    assert sorted(world["to"]) == [("https://ntfy.sh/admin", "App shop is down"), ("https://ntfy.sh/sam", "App shop is down")]
    world["to"].clear()
    world["node"] = _node(state="inactive")                     # stopped on purpose: nothing new
    run(1360)
    world["node"] = _node(build=("failed", 1300))
    run(1480)
    # back up; the failed build goes to its owner only (off by default for administrators)
    assert sorted(world["to"]) == [("https://ntfy.sh/admin", "App shop is running again"),
                                   ("https://ntfy.sh/sam", "App shop is running again"),
                                   ("https://ntfy.sh/sam", "Build of shop failed")]
    world["to"].clear()
    run(1600)
    assert world["to"] == []                                    # the same failed build: once
    world["node"], world["ok"] = _node(used=95), False
    run(1720)
    run(1840)
    assert ("https://ntfy.sh/sam", "Hosting disk of sam is 95% full") in world["to"]
    assert ("https://ntfy.sh/sam", "a.example.com is down") in world["to"]
    assert not [t for t in world["to"] if t[0] == "https://ntfy.sh/kim"]
    monitor.save_person_config("sam", events={"disk": False, "site_down": True, "app_down": True, "build": True})
    assert monitor.public_person_config("sam")["events"]["disk"] is False
    assert "ntfy.sh" not in json.dumps(monitor.public_person_config("sam"))


def test_usage_points_for_graphs(mon):
    monitor, state, world, run, tmp_path = mon
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "a.example.com.log"
    log.write_text("1.0 200\n" * 7)                            # before the first look: not counted
    world["node"] = _node()
    run(1000)
    with log.open("a") as handle:
        handle.write("2.0 200\n" * 6 + "3.0 502\n" * 2)
    run(1120)
    points = monitor.traffic("a.example.com")["points"]
    assert points[-1][1:] == [4.0, 1.0]                         # 8 requests and 2 errors in 2 minutes
    log.write_text("4.0 200\n" * 2)                            # rotated
    run(1240)
    assert monitor.traffic("a.example.com")["points"][-1][1] == 1.0
    assert monitor.app_usage("sam", "shop")["points"][-1][1:] == [3.5, 2 ** 20]
    old = [[t, 2.0, 4.0] for t in range(0, 3600, 120)]
    compacted = monitor._compact(old + [[200000, 1.0, 1.0]], 200000)
    assert compacted == [[1800, 2.0, 4.0], [200000, 1.0, 1.0]]
