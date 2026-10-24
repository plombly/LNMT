"""Hosting on the node(s): the edge's side (runs as root inside lnmt-admin).

The edge talks to a node's agent over SSH with its own key
(/etc/lnmt/node_ed25519); the node runs only /usr/local/sbin/lnmt-node
for that key. Templates turn a choice in the dashboard into an image, a
port, volumes and settings; a WordPress app also gets a private database.
"""

import json
import re
import secrets
import subprocess
from pathlib import Path

NODES_FILE = Path("/etc/lnmt/nodes.json")
NODE_KEY = Path("/etc/lnmt/node_ed25519")


class HostingError(RuntimeError):
    pass


def nodes():
    try:
        return json.loads(NODES_FILE.read_text()).get("nodes", [])
    except (OSError, ValueError):
        return []


def node(node_id=None):
    for item in nodes():
        if node_id in (None, item["id"]):
            return item
    raise HostingError("No hosting node is configured.")


def call(action, /, node_id=None, **args):
    target = node(node_id)
    try:
        result = subprocess.run(
            ["ssh", "-i", str(NODE_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=yes", f"root@{target['ip']}"],
            input=json.dumps({"action": action, "args": args}) + "\n",
            capture_output=True, text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        raise HostingError("The hosting node did not answer in time.")
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise HostingError("The hosting node is not reachable.")
    if not answer.get("ok"):
        raise HostingError(answer.get("error") or "The hosting node refused.")
    return answer["result"]


def customer_name(username):
    """Dashboard user -> node customer (a-z, 0-9, -)."""
    name = re.sub(r"[^a-z0-9-]", "-", username.lower()).strip("-")
    if not name or not name[0].isalpha():
        name = "u-" + name
    return name[:31]


PHP_VERSIONS = ("8.1", "8.2", "8.3", "8.4")
TEMPLATES = {
    "static": {"label": "Static website (nginx)", "image": "docker.io/library/nginx:alpine", "port": 80,
               "volumes": [{"name": "site", "path": "/usr/share/nginx/html"}], "memory_mb": 128},
    "php": {"label": "PHP + Apache", "image": "docker.io/library/php:{version}-apache", "port": 80,
            "versions": PHP_VERSIONS, "volumes": [{"name": "site", "path": "/var/www/html"}], "memory_mb": 256},
    "wordpress": {"label": "WordPress (with its own database)", "image": "docker.io/library/wordpress:php8.3-apache",
                  "port": 80, "volumes": [{"name": "wordpress", "path": "/var/www/html"}], "memory_mb": 512},
    "node": {"label": "Node.js", "image": "docker.io/library/node:{version}-alpine", "port": 3000,
             "versions": ("20", "22", "24"), "volumes": [{"name": "app", "path": "/app"}], "memory_mb": 512,
             "command": "sh -c \"cd /app && (test -f package.json && npm install --omit=dev; npm start)\""},
    "python": {"label": "Python", "image": "docker.io/library/python:{version}-slim", "port": 8000,
               "versions": ("3.11", "3.12", "3.13"), "volumes": [{"name": "app", "path": "/app"}], "memory_mb": 512,
               "command": "sh -c \"cd /app && (test -f requirements.txt && pip install -r requirements.txt); python app.py\""},
    "mariadb": {"label": "MariaDB (private database)", "image": "docker.io/library/mariadb:11", "port": 0,
                "volumes": [{"name": "data", "path": "/var/lib/mysql"}], "memory_mb": 512, "private": True},
    "postgres": {"label": "PostgreSQL (private database)", "image": "docker.io/library/postgres:{version}-alpine",
                 "port": 0, "versions": ("16", "17", "18"), "volumes": [{"name": "data", "path": "/var/lib/postgresql/data"}],
                 "memory_mb": 512, "private": True},
    "redis": {"label": "Redis (private cache)", "image": "docker.io/library/redis:7-alpine", "port": 0,
              "volumes": [{"name": "data", "path": "/data"}], "memory_mb": 128, "private": True},
    "adminer": {"label": "Adminer (web admin for your databases)", "image": "docker.io/library/adminer:latest",
                "port": 8080, "volumes": [], "memory_mb": 128},
    "image": {"label": "Any container image", "image": "", "port": 0, "volumes": [], "memory_mb": 512},
    "wordpress-theme": {"label": "WordPress site built by LAIka (look, pages and content)", "image": "docker.io/library/wordpress:php8.3-apache",
                        "port": 80, "volumes": [{"name": "wordpress", "path": "/var/www/html"}], "memory_mb": 512,
                        "build": True},
    "dockerfile": {"label": "Your own Dockerfile (git push or uploaded files)", "image": "", "port": 0,
                   "volumes": [{"name": "data", "path": "/data"}], "memory_mb": 512, "build": True},
}


STARTER = {
    "static": {"volume": "site", "files": {"index.html": (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>It works</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:40em;margin:15vh auto;padding:0 1em;color:#222}"
        "code{background:#eee;padding:2px 5px;border-radius:4px}</style></head><body><h1>It works</h1>"
        "<p>This site is served from <code>data/site</code>. Replace this page with your own files in the "
        "dashboard's Files tab, or upload them with SFTP.</p></body></html>\n")}},
    "php": {"volume": "site", "files": {"index.php": (
        "<?php\n// Replace this file with your site: dashboard Files tab (data/site), or SFTP.\n?>"
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>It works</title></head>"
        "<body style=\"font-family:system-ui,sans-serif;max-width:40em;margin:15vh auto;padding:0 1em\">"
        "<h1>It works</h1><p>PHP <?= htmlspecialchars(PHP_VERSION) ?> is running. Put your files in "
        "<code>data/site</code>.</p></body></html>\n")}},
}


def templates():
    return {"templates": {k: {"label": v["label"], "versions": list(v.get("versions", ())),
                              "port": v["port"], "private": bool(v.get("private"))} for k, v in TEMPLATES.items()},
            "nodes": [{"id": n["id"], "ip": n["ip"]} for n in nodes()]}


def create_app(customer, name, template, version="", image="", port=0, env=None, memory_mb=0, cpus=1.0,
               volumes=None, command=""):
    spec = TEMPLATES.get(template)
    if not spec:
        raise HostingError("Unknown template.")
    env = dict(env or {})
    if template in ("image", "dockerfile"):
        if template == "image" and not image:
            raise HostingError("Give the image to run, e.g. ghcr.io/you/app:latest.")
        chosen_image, publish = (image or "localhost/build"), bool(int(port or 0))
        chosen_port = int(port or 0)
        vols = volumes or spec["volumes"]
    else:
        versions = spec.get("versions", ())
        if versions:
            version = version or versions[-1]
            if version not in versions:
                raise HostingError(f"Versions: {', '.join(versions)}")
        chosen_image = spec["image"].format(version=version)
        chosen_port, publish, vols = spec["port"], not spec.get("private"), spec["volumes"]
    created = []
    if template in ("wordpress", "wordpress-theme"):
        password = secrets.token_urlsafe(24)
        created.append(call("app_create", customer=customer, name=f"{name}-db", image="docker.io/library/mariadb:11",
                            publish=False, memory_mb=512, cpus=1.0,
                            volumes=[{"name": "data", "path": "/var/lib/mysql"}],
                            env={"MARIADB_DATABASE": "wordpress", "MARIADB_USER": "wordpress",
                                 "MARIADB_PASSWORD": password, "MARIADB_RANDOM_ROOT_PASSWORD": "1"}))
        env.update({"WORDPRESS_DB_HOST": f"{name}-db", "WORDPRESS_DB_USER": "wordpress",
                    "WORDPRESS_DB_PASSWORD": password, "WORDPRESS_DB_NAME": "wordpress"})
    shown_once = {}
    if template == "mariadb":
        env.setdefault("MARIADB_RANDOM_ROOT_PASSWORD", "1")
        env.setdefault("MARIADB_DATABASE", "app")
        env.setdefault("MARIADB_USER", "app")
        if "MARIADB_PASSWORD" not in env:
            env["MARIADB_PASSWORD"] = shown_once["MARIADB_PASSWORD"] = secrets.token_urlsafe(24)
        shown_once.update(host=name, database=env["MARIADB_DATABASE"], user=env["MARIADB_USER"])
    if template == "postgres":
        env.setdefault("POSTGRES_USER", "app")
        env.setdefault("POSTGRES_DB", "app")
        if "POSTGRES_PASSWORD" not in env:
            env["POSTGRES_PASSWORD"] = shown_once["POSTGRES_PASSWORD"] = secrets.token_urlsafe(24)
        shown_once.update(host=name, database=env["POSTGRES_DB"], user=env["POSTGRES_USER"])
    app = call("app_create", customer=customer, name=name, image=chosen_image, port=chosen_port, env=env,
               volumes=vols, command=command or spec.get("command", ""),
               memory_mb=int(memory_mb or spec["memory_mb"]), cpus=float(cpus or 1.0), publish=publish,
               build=bool(spec.get("build")), starter=STARTER.get(template),
               auto_update=template not in ("image", "dockerfile"))
    return {"app": app, "also_created": [a["name"] for a in created], "shown_once": shown_once}
