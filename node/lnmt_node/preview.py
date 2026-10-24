"""lnmt-app-preview APP NAME REV: run a throwaway preview of a pushed commit.

Runs as the customer, from the git post-receive hook, when someone pushes to
refs/heads/lnmt-preview/<NAME> (LAIka does, for every change before it is
reviewed, to look at the rendered result). The commit is built like a
deploy, but runs next to the live app, not instead of it:

  - its own image tag (localhost/<app>:preview), container <app>-preview and
    network, published on the node in the preview range (30000-30999, open
    to the LAN and VPN only);
  - a WordPress app gets its own empty database (in memory) and is set up
    with a random administrator nobody is told, so the site's content from
    the project is imported exactly as on a deploy, and the live site's
    database is never touched; other apps run without the live app's
    environment (it may hold credentials for live data);
  - one preview per app (a new one replaces it), removed after PREVIEW_HOURS.

The last line it prints is "LNMT preview: http://<node>:<port>/" (the pusher
reads it from git's "remote:" output), or "LNMT preview failed: <why>".
"""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

PORTS = range(30000, 31000)
PREVIEW_HOURS = 6
WORDPRESS_ENV = "WORDPRESS_DB_HOST"


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def node_ip():
    try:
        return json.loads(Path("/etc/lnmt-node/node.json").read_text())["node_ip"]
    except (OSError, ValueError, KeyError):
        return "127.0.0.1"


def free_port(ip):
    for port in PORTS:
        with socket.socket() as probe:
            try:
                probe.bind((ip, port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free preview port")


def remove(app):
    for name in (f"{app}-preview", f"{app}-preview-db"):
        run(["podman", "rm", "-f", "-t", "2", name])


# The customer's own processes may not open connections on the LAN (the node
# firewall), so requests to the preview are made from inside its container,
# with the Host header visitors will use (WordPress records it at setup).
PHP_GET = (r'$o = ["http" => ["method" => $argv[1], "header" => "Host: " . $argv[3] . "\r\n"'
           r' . "Content-Type: application/x-www-form-urlencoded\r\n", "content" => $argv[4] ?? "",'
           r' "ignore_errors" => true, "timeout" => 60, "follow_location" => 0]];'
           r' @file_get_contents("http://127.0.0.1" . $argv[2], false, stream_context_create($o));'
           r' echo $http_response_header[0] ?? "no answer";')


def http(app, method, path, host, body=""):
    """(status line) of a request made inside the preview container."""
    answer = run(["podman", "exec", f"{app}-preview", "php", "-r", PHP_GET, "--", method, path, host, body], timeout=90)
    return answer.stdout.strip()


def wait_http(app, host, seconds=180):
    """Until the app answers without a server error (WordPress: its database is up)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = http(app, "GET", "/", host)
        if status.startswith("HTTP/") and " 5" not in status[8:10]:
            return True
        time.sleep(3)
    return False


def install_wordpress(app, host):
    """Run WordPress's setup with a random administrator (never shown)."""
    password = secrets.token_urlsafe(24)
    data = urllib.parse.urlencode({
        "weblog_title": "Preview", "user_name": "preview-" + secrets.token_hex(3),
        "admin_password": password, "admin_password2": password, "pw_weak": "1",
        "admin_email": "preview@example.invalid", "blog_public": "0", "language": ""})
    http(app, "POST", "/wp-admin/install.php?step=2", host, data)
    for _ in range(2):  # the first request imports the site's content; the second sees it
        http(app, "GET", "/", host)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    app, name, rev = argv[0], argv[1], argv[2]
    home = Path(os.path.expanduser("~"))
    repo = home / "git" / f"{app}.git"
    work = home / "apps" / app / "preview"
    try:
        subprocess.run(["rm", "-rf", str(work)], check=True)
        work.mkdir(parents=True)
        archive = subprocess.Popen(["git", "--git-dir", str(repo), "archive", rev], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(work)], stdin=archive.stdout, check=True)
        archive.wait()
        if not (work / "Dockerfile").exists() and not (work / "Containerfile").exists():
            raise RuntimeError("no Dockerfile")
        tag = f"localhost/{app}:preview"
        print(f"Building a preview of {rev[:12]} …", flush=True)
        built = run(["podman", "build", "--memory", "2g", "--pull=newer", "-t", tag, str(work)])
        if built.returncode:
            raise RuntimeError("build failed: " + built.stdout[-400:].replace("\n", " "))
        remove(app)
        env_file = home / "apps" / app / "env"
        env = env_file.read_text() if env_file.exists() else ""
        ip = node_ip()
        port = free_port(ip)
        network = f"{app}-preview"
        run(["podman", "network", "create", network])
        container_port = "80"
        args = ["podman", "run", "-d", "--name", f"{app}-preview", "--network", network, "--memory", "512m",
                "--cpus", "1", "--pids-limit", "512", "--security-opt", "no-new-privileges",
                "-p", f"{ip}:{port}:{container_port}"]
        wordpress = WORDPRESS_ENV in env
        if wordpress:
            password = secrets.token_urlsafe(18)
            started = run(["podman", "run", "-d", "--name", f"{app}-preview-db", "--network", network,
                           "--memory", "512m", "--tmpfs", "/var/lib/mysql",
                           "-e", "MARIADB_DATABASE=wordpress", "-e", "MARIADB_USER=wordpress",
                           "-e", f"MARIADB_PASSWORD={password}", "-e", "MARIADB_RANDOM_ROOT_PASSWORD=1",
                           "docker.io/library/mariadb:11"])
            if started.returncode:
                raise RuntimeError("database did not start: " + started.stderr[-300:])
            args += ["-e", f"WORDPRESS_DB_HOST={app}-preview-db", "-e", "WORDPRESS_DB_USER=wordpress",
                     "-e", f"WORDPRESS_DB_PASSWORD={password}", "-e", "WORDPRESS_DB_NAME=wordpress"]
        else:
            match = re.search(r"^EXPOSE\s+(\d+)", (work / "Dockerfile").read_text() if (work / "Dockerfile").exists() else "", re.M)
            container_port = match.group(1) if match else "8080"
            args[-1] = f"{ip}:{port}:{container_port}"
        started = run(args + [tag])
        if started.returncode:
            raise RuntimeError("preview did not start: " + started.stderr[-300:])
        base, host = f"http://{ip}:{port}/", f"{ip}:{port}"
        if wordpress:
            if not wait_http(f"{app}", host):
                raise RuntimeError("the preview does not answer")
            install_wordpress(app, host)
        else:
            time.sleep(5)
            state = run(["podman", "inspect", "-f", "{{.State.Running}}", f"{app}-preview"]).stdout.strip()
            if state != "true":
                raise RuntimeError("the preview stopped right after starting")
        # Gone after a while, whatever happens to the pusher.
        run(["systemd-run", "--user", "--quiet", "--collect", f"--on-active={PREVIEW_HOURS}h",
             f"--unit=lnmt-preview-clean-{app}-{int(time.time())}", "podman", "rm", "-f",
             f"{app}-preview", f"{app}-preview-db"])
        (home / "apps" / app / "preview.json").write_text(json.dumps({"url": base, "rev": rev, "name": name,
                                                                         "at": time.time()}))
        print(f"LNMT preview: {base}", flush=True)
        return 0
    except Exception as exc:
        remove(app)
        print(f"LNMT preview failed: {exc}"[:500], flush=True)
        return 0  # never refuse the push itself
    finally:
        subprocess.run(["rm", "-rf", str(work)])


if __name__ == "__main__":
    sys.exit(main())
