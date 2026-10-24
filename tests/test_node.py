"""The hosting node agent: what it writes, never what it runs (no podman here)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "node"))
from lnmt_node import agent  # noqa: E402


def test_the_container_unit_is_limited_and_published_only_on_the_node(monkeypatch):
    monkeypatch.setattr(agent, "config", lambda: {"node_ip": "192.0.2.121"})
    unit = agent._unit({"name": "shop", "image": "docker.io/library/php:8.3-apache", "port": 80, "host_port": 20001,
                        "volumes": [{"name": "site", "path": "/var/www/html"}], "command": "", "memory_mb": 256, "cpus": 0.5})
    assert "PublishPort=192.0.2.121:20001:80" in unit and "NoNewPrivileges=true" in unit
    assert "Volume=%h/apps/shop/data/site:/var/www/html:Z" in unit
    assert "--memory=256m --cpus=0.5 --pids-limit=1024" in unit and "Privileged" not in unit
    private = agent._unit({"name": "db", "image": "mariadb:11", "port": 0, "host_port": 0, "volumes": [],
                           "command": "", "memory_mb": 512, "cpus": 1})
    assert "PublishPort" not in private


def test_environment_is_validated_and_written_literally():
    assert agent._env_file(agent._clean_env({"A": 'x "y" \\z'})) == 'A=x "y" \\z\n'
    for bad in ({"1A": "x"}, {"A": "line\nbreak"}, {"A B": "x"}):
        with pytest.raises(agent.AgentError):
            agent._clean_env(bad)


def test_names_and_requests_are_checked():
    with pytest.raises(agent.AgentError):
        agent.customer_user("../root")
    assert agent.customer_user("sam") == "c-sam"
    assert agent.handle('{"action": "rm", "args": {}}') == {"ok": False, "error": "unknown action"}
    assert agent.handle('{"action": "info", "args": {"x": 1}}') == {"ok": False, "error": "unknown argument"}


def test_customer_shell_allows_only_git_on_their_repos_and_sftp(tmp_path, monkeypatch):
    from lnmt_node import shell
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "git" / "shop.git").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(shell.os, "execvp", lambda prog, argv: calls.append(argv) or 0)
    monkeypatch.setattr(shell.os, "execv", lambda prog, argv: calls.append(argv) or 0)
    shell.main(["-c", "git-receive-pack '/shop.git'"])
    assert calls[-1] == ["git-shell", "-c", f"git-receive-pack '{tmp_path}/git/shop.git'"]
    shell.main(["-c", "/usr/lib/openssh/sftp-server"])
    assert calls[-1][0] == "sftp-server" and calls[-1][-1] == f"{tmp_path}/apps"
    for refused in (["-c", "bash"], ["-c", "git-receive-pack '../../etc'"], [], ["-c", "git-receive-pack 'other.git'"]):
        assert shell.main(refused) == 1
    assert len(calls) == 2


def test_file_operations_stay_in_the_app_folder(tmp_path):
    from lnmt_node import files
    root = tmp_path / "app"
    (root / "data" / "site").mkdir(parents=True)
    (tmp_path / "secret").write_text("x")
    (root / "data" / "escape").symlink_to(tmp_path / "secret")
    import base64
    files.run(str(root), "write", {"path": "data/site/index.html", "content": base64.b64encode(b"<h1>hi</h1>").decode()})
    assert files.run(str(root), "list", {"path": "data/site"})["items"][0]["name"] == "index.html"
    assert base64.b64decode(files.run(str(root), "read", {"path": "data/site/index.html"})["content"]) == b"<h1>hi</h1>"
    for bad in ("../secret", "data/../../secret", "data/escape"):
        with pytest.raises(ValueError):
            files.run(str(root), "read", {"path": bad})
    with pytest.raises(ValueError):
        files.run(str(root), "delete", {"path": "data"})


def test_scheduled_tasks_run_their_command_from_a_file(tmp_path, monkeypatch):
    written = {}
    monkeypatch.setattr(agent, "_write_as", lambda user, path, text, mode=0o600: written.__setitem__(path.name, text))
    monkeypatch.setattr(agent, "HOMES", tmp_path)
    agent._task_unit("c-sam", "shop", {"name": "nightly", "schedule": "*-*-* 03:00", "command": "echo 'it''s' \"$X\""})
    assert written["nightly.cmd"] == "echo 'it''s' \"$X\"\n"           # the command is never quoted into the unit
    service = written["lnmt-task-shop-nightly.service"]
    assert "podman exec -i shop sh -s <%h/apps/shop/tasks/nightly.cmd" in service and "echo" not in service
    assert "OnCalendar=*-*-* 03:00" in written["lnmt-task-shop-nightly.timer"]
    monkeypatch.setattr(agent, "_load_app", lambda c, n: ("c-sam", {"name": n, "tasks": []}))
    for bad in ([{"name": "Bad", "schedule": "daily", "command": "x"}],
                [{"name": "ok", "schedule": "daily", "command": "a\nb"}],
                [{"name": "ok", "schedule": "daily", "command": "x"}, {"name": "ok", "schedule": "daily", "command": "y"}]):
        monkeypatch.setattr(agent, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
        with pytest.raises(agent.AgentError):
            agent.app_tasks_set("sam", "shop", bad)


def test_stats_sizes_and_database_kinds():
    assert agent._bytes("20.5MB") == 20500000 and agent._bytes("1.5GiB") == 1610612736 and agent._bytes("") == 0
    assert agent._db_kind({"image": "docker.io/library/mariadb:11"}) == "mariadb"
    assert agent._db_kind({"image": "docker.io/library/postgres:17-alpine"}) == "postgres"
    with pytest.raises(agent.AgentError):
        agent._db_kind({"image": "docker.io/library/redis:7-alpine"})


def test_deploy_keys_reach_one_app_only(tmp_path, monkeypatch):
    from lnmt_node import shell
    monkeypatch.setenv("HOME", str(tmp_path))
    for app in ("shop", "blog"):
        (tmp_path / "git" / f"{app}.git").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(shell.os, "execvp", lambda prog, argv: calls.append(argv) or 0)
    monkeypatch.setattr(shell.os, "execv", lambda prog, argv: calls.append(argv) or 0)
    forced = ["-c", "/usr/local/sbin/lnmt-customer-shell --deploy shop"]
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "git-receive-pack '/shop.git'")
    shell.main(forced)
    assert calls[-1] == ["git-shell", "-c", f"git-receive-pack '{tmp_path}/git/shop.git'"]
    for other in ("git-receive-pack '/blog.git'", "internal-sftp", "bash", ""):
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", other)
        assert shell.main(forced) == 1
    assert len(calls) == 1


def test_authorized_keys_hold_customer_and_deploy_keys(tmp_path, monkeypatch):
    written = {}
    monkeypatch.setattr(agent, "KEYS", tmp_path / "keys")
    monkeypatch.setattr(agent, "_write_as", lambda user, path, text, mode=0o600: written.__setitem__(path.name, text))
    monkeypatch.setattr(agent, "_load_app", lambda c, n: (f"c-{c}", {"name": n}))
    monkeypatch.setattr(agent, "config", lambda: {"node_ip": "192.0.2.121"})
    monkeypatch.setattr(agent, "HOMES", tmp_path / "homes")
    key = "ssh-ed25519 " + "A" * 68
    result = agent.app_deploy_key("sam", "shop", key + " laika@build-host", label="laika")
    assert result["git_url"] == "ssh://c-sam@192.0.2.121/shop.git"
    assert written["authorized_keys"] == f'restrict,command="/usr/local/sbin/lnmt-customer-shell --deploy shop" {key}\n'
    monkeypatch.setattr(agent, "_exists", lambda user: True)
    agent.customer_set_keys("sam", ["ssh-ed25519 " + "B" * 68 + " me@laptop"])
    assert written["authorized_keys"].splitlines()[0] == "restrict ssh-ed25519 " + "B" * 68
    assert "--deploy shop" in written["authorized_keys"]                     # kept when the customer's keys change
    agent.app_deploy_key("sam", "shop", label="laika", remove=True)
    assert "--deploy" not in written["authorized_keys"]


def test_the_hook_deploys_main_and_previews_other_branches():
    hook = agent.HOOK.replace("{app}", "shop")
    assert "refs/heads/main|refs/heads/master)" in hook and "build.py shop" in hook
    assert "refs/heads/lnmt-preview/*)" in hook and 'preview.py shop "${ref#refs/heads/lnmt-preview/}" "$new"' in hook


def test_a_wordpress_preview_never_uses_the_live_database(tmp_path, monkeypatch):
    from lnmt_node import preview
    home = tmp_path
    (home / "git" / "shop.git").mkdir(parents=True)
    (home / "apps" / "shop").mkdir(parents=True)
    (home / "apps" / "shop" / "env").write_text("WORDPRESS_DB_HOST=shop-db\nWORDPRESS_DB_PASSWORD=live-secret\n")
    monkeypatch.setenv("HOME", str(home))
    commands = []

    def fake_run(argv, **kw):
        commands.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    class Archive:
        stdout = None

        def wait(self):
            return 0
    monkeypatch.setattr(preview, "run", fake_run)
    monkeypatch.setattr(preview.subprocess, "run", lambda argv, **kw: (argv[0] == "tar" and (home / "apps/shop/preview/Dockerfile").write_text("FROM x\n")) or fake_run(argv))
    monkeypatch.setattr(preview.subprocess, "Popen", lambda *a, **k: Archive())
    monkeypatch.setattr(preview, "node_ip", lambda: "192.0.2.121")
    monkeypatch.setattr(preview, "free_port", lambda ip: 30007)
    monkeypatch.setattr(preview, "wait_http", lambda app, host, seconds=120: True)
    installed = []
    monkeypatch.setattr(preview, "install_wordpress", lambda app, host: installed.append(host))
    monkeypatch.setattr(preview.time, "sleep", lambda s: None)
    preview.main(["shop", "job1", "a" * 40])
    web = next(c for c in commands if c[:4] == ["podman", "run", "-d", "--name"] and c[4] == "shop-preview")
    assert "WORDPRESS_DB_HOST=shop-preview-db" in web and not any("live-secret" in part for part in web)
    assert "192.0.2.121:30007:80" in web and installed == ["192.0.2.121:30007"]
    assert any(c[4] == "shop-preview-db" and "--tmpfs" in c for c in commands if c[:4] == ["podman", "run", "-d", "--name"])
