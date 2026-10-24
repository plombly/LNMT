"""lnmt-node: the root agent of a hosting node.

Reached only from the edge proxy, over SSH with a forced command
(authorized_keys: from="<edge address>",restrict,command="/usr/local/sbin/lnmt-node").
One JSON request on stdin, one JSON answer on stdout (like lnmt-admin):

    {"action": "app_create", "args": {...}}  ->  {"ok": true, "result": {...}}

Customers
  One Linux user per customer (c-<name>, uid 20000-29999), rootless Podman,
  a home on its own fixed-size disk image (the disk quota: files, images and
  containers all count), lingering systemd user manager. Their outbound
  traffic may not reach private networks (nftables, by uid), so a customer
  can reach neither the LAN nor another customer's app.

Apps
  A Quadlet container unit per app in the customer's
  ~/.config/containers/systemd/, on the customer's private network (apps
  of one customer find each other by name), published on NODE_IP:<port>
  (20000-29999, reachable from the edge only) when it serves the web.
  Never privileged, no new privileges, memory / CPU / process limits.
  App records (image, ports, limits) are root's: /var/lib/lnmt-node/apps/<user>/<app>.json,
  so a customer cannot change them through their files. Persistent data in
  ~/apps/<app>/data, build source in ~/apps/<app>/src.
  Each customer's processes together are capped by their systemd user slice
  (memory, CPU, tasks), set by root: editing their own unit files cannot get
  past it.
"""

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path("/srv/lnmt-hosting")
DISKS = BASE / "disks"
HOMES = BASE / "home"
CONFIG = Path("/etc/lnmt-node/node.json")
UID_RANGE = (20000, 29999)
PORT_RANGE = (20000, 29999)
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{1,250}$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CONTAINER_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")
REGISTRY = Path("/var/lib/lnmt-node/apps")
MAX_REQUEST = 16 * 1024 * 1024  # file uploads arrive base64-encoded
SHELL = "/usr/local/sbin/lnmt-customer-shell"
KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|sk-ssh-ed25519@openssh.com) [A-Za-z0-9+/=]{40,1000}( [^\n]{0,100})?$")


class AgentError(Exception):
    pass


def config():
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        data = {}
    return {"node_ip": data.get("node_ip", "127.0.0.1"), **data}


def run(argv, check=True, timeout=600, input_text=None, user=None):
    if user:
        uid = pwd.getpwnam(user).pw_uid
        argv = ["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", f"HOME={HOMES / user}", *argv]
    program = os.path.basename(argv[8] if user else argv[0])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text, cwd="/")
    if check and result.returncode != 0:
        raise AgentError(f"{program} failed: {(result.stderr or result.stdout).strip()[-600:]}")
    return result


def user_systemctl(user, *args, check=True):
    return run(["systemctl", "--user", f"--machine={user}@", *args], check=check, timeout=300)


def customer_user(name):
    if not NAME.match(name or ""):
        raise AgentError("Customer names: 2-31 of a-z, 0-9, - (starting with a letter).")
    return f"c-{name}"


def _exists(user):
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def _free_uid():
    used = {p.pw_uid for p in pwd.getpwall()}
    return next(u for u in range(*UID_RANGE) if u not in used)


# --- customers -------------------------------------------------------------------------

def customer_ensure(name, disk_gb=10, memory_gb=4, cpus=2, tasks=4096):
    """Create (or check) a customer: user, disk image, mounted home, linger,
    and the limits on all their processes together."""
    user = customer_user(name)
    disk_gb = int(disk_gb)
    grown = False
    if not 1 <= disk_gb <= 500:
        raise AgentError("Disk size must be 1-500 GB.")
    DISKS.mkdir(parents=True, exist_ok=True)
    HOMES.mkdir(parents=True, exist_ok=True)
    os.chmod(DISKS, 0o700)
    os.chmod(BASE, 0o711)
    os.chmod(HOMES, 0o711)
    image, home = DISKS / f"{user}.img", HOMES / user
    if not image.exists():
        free = shutil.disk_usage(BASE).free // 2 ** 30
        if disk_gb > free:
            raise AgentError(f"Only {free} GB free on the node.")
        run(["truncate", "-s", f"{disk_gb}G", str(image)])
        run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", user[:16], str(image)])
        os.chmod(image, 0o600)
    elif image.stat().st_size < disk_gb * 2 ** 30:
        grow = disk_gb - image.stat().st_size // 2 ** 30
        if grow > shutil.disk_usage(BASE).free // 2 ** 30:
            raise AgentError(f"Not enough free space on the node to grow the disk by {grow} GB.")
        run(["truncate", "-s", f"{disk_gb}G", str(image)])
        grown = True
    if not _exists(user):
        run(["useradd", "--uid", str(_free_uid()), "--home-dir", str(home), "--no-create-home",
             "--shell", SHELL, "--comment", f"LNMT hosting customer {name}", user])
    else:
        run(["usermod", "--shell", SHELL, user])  # git push and SFTP only (lnmt_node/shell.py)
    home.mkdir(exist_ok=True)
    line = f"{image} {home} ext4 loop,nodev,nosuid,noatime 0 2\n"
    fstab = Path("/etc/fstab").read_text()
    if str(image) not in fstab:
        with open("/etc/fstab", "a") as handle:
            handle.write(line)
    if run(["mountpoint", "-q", str(home)], check=False).returncode != 0:
        run(["mount", str(home)])
    if grown:
        # Online grow: tell the loop device about the bigger file, then the filesystem.
        device = run(["findmnt", "-n", "-o", "SOURCE", str(home)]).stdout.strip()
        run(["losetup", "-c", device])
        run(["resize2fs", device])
    entry = pwd.getpwnam(user)
    os.chown(home, entry.pw_uid, entry.pw_gid)
    os.chmod(home, 0o700)
    run(["loginctl", "enable-linger", user])
    _limit(entry.pw_uid, memory_gb, cpus, tasks)
    for _ in range(20):  # the user manager starts
        if Path(f"/run/user/{entry.pw_uid}").exists():
            break
        time.sleep(0.5)
    # Apps with "keep updated" pull newer images daily; podman rolls an app
    # back to its previous image when it does not come back up.
    user_systemctl(user, "enable", "--now", "podman-auto-update.timer", check=False)
    _write_as(user, home / ".config/containers/systemd/lnmt.network",
              "[Network]\nNetworkName=lnmt\n# The customer's private network: apps find each other by name.\n")
    for folder in ("apps", "git", ".ssh"):
        (home / folder).exists() or _mkdir_as(user, home / folder)
    os.chmod(home / ".ssh", 0o700)
    return customer_info(name)


def _limit(uid, memory_gb, cpus, tasks):
    memory_gb, cpus, tasks = float(memory_gb), float(cpus), int(tasks)
    if not (0.25 <= memory_gb <= 64 and 0.1 <= cpus <= 32 and 64 <= tasks <= 65536):
        raise AgentError("Customer limits: 0.25-64 GB memory, 0.1-32 CPUs, 64-65536 processes.")
    run(["systemctl", "set-property", f"user-{uid}.slice", f"MemoryMax={int(memory_gb * 1024)}M",
         f"MemoryHigh={int(memory_gb * 1024 * 0.9)}M", f"CPUQuota={int(cpus * 100)}%", f"TasksMax={tasks}"])


KEYS = Path("/var/lib/lnmt-node/keys")


def _keys_path(user):
    return KEYS / f"{user}.json"


def _load_keys(user):
    try:
        data = json.loads(_keys_path(user).read_text())
    except (OSError, ValueError):
        # Before the list existed: the customer's keys were only in authorized_keys.
        try:
            text = (HOMES / user / ".ssh" / "authorized_keys").read_text()
        except OSError:
            text = ""
        data = {"customer": [line[len("restrict "):] for line in text.splitlines() if line.startswith("restrict ")]}
    return {"customer": data.get("customer", []), "deploy": data.get("deploy", {})}


def _write_keys(user, keys):
    """authorized_keys from the root-kept list: the customer's own keys (git
    push and SFTP on all their apps) and deploy keys, each forced to git push
    and fetch of one app (e.g. a LAIka project deploying it)."""
    KEYS.mkdir(mode=0o700, parents=True, exist_ok=True)
    _keys_path(user).write_text(json.dumps(keys, indent=1))
    os.chmod(_keys_path(user), 0o600)
    lines = [f"restrict {key}\n" for key in keys["customer"]]
    for app, entries in sorted(keys["deploy"].items()):
        for entry in entries:
            lines.append(f'restrict,command="{SHELL} --deploy {app}" {entry["key"]}\n')
    _write_as(user, HOMES / user / ".ssh" / "authorized_keys", "".join(lines))
    return len(lines)


def _clean_key(key):
    key = " ".join(str(key).split()[:2])  # type and key; the comment is ours to choose
    if not KEY.match(key):
        raise AgentError("That is not an SSH public key (ssh-ed25519 AAAA… or ssh-rsa AAAA…).")
    return key


def customer_set_keys(name, keys):
    """The customer's SSH keys for git push and SFTP (each line "restrict"ed)."""
    user = customer_user(name)
    if not _exists(user):
        raise AgentError("No such customer.")
    stored = _load_keys(user)
    stored["customer"] = [_clean_key(k) for k in keys or [] if str(k).strip()]
    _write_keys(user, stored)
    return {"keys": len(stored["customer"])}


def app_deploy_key(customer, name, key="", label="", remove=False):
    """Add (or with remove, drop) a deploy key that may only push and fetch this
    app's repository: for a build service such as LAIka. One key per label."""
    user, app = _load_app(customer, name)
    label = str(label or "deploy")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", label):
        raise AgentError("Bad label.")
    stored = _load_keys(user)
    entries = [e for e in stored["deploy"].get(name, []) if e["label"] != label]
    if not remove:
        entries.append({"label": label, "key": _clean_key(key)})
    stored["deploy"][name] = entries
    if not entries:
        stored["deploy"].pop(name, None)
    _write_keys(user, stored)
    return {"app": name, "deploy_keys": [e["label"] for e in entries],
            "git_url": f"ssh://{user}@{config()['node_ip']}/{name}.git"}


def _mkdir_as(user, path):
    run(["mkdir", "-p", str(path)], user=user)


def _write_as(user, path, text, mode=0o600):
    _mkdir_as(user, path.parent)
    run(["sh", "-c", f'umask 077 && cat > "$1"', "sh", str(path)], user=user, input_text=text)
    os.chmod(path, mode)


def customer_info(name):
    user = customer_user(name)
    if not _exists(user):
        raise AgentError("No such customer.")
    home = HOMES / user
    usage = shutil.disk_usage(home)
    limits = {}
    shown = run(["systemctl", "show", f"user-{pwd.getpwnam(user).pw_uid}.slice",
                 "--property=MemoryMax,CPUQuotaPerSecUSec,TasksMax"], check=False).stdout
    for line in shown.splitlines():
        key, _, value = line.partition("=")
        if key == "MemoryMax" and value.isdigit():
            limits["memory_gb"] = round(int(value) / 2 ** 30, 2)
        elif key == "CPUQuotaPerSecUSec" and value.endswith("s") and value != "infinity":
            limits["cpus"] = round(float(value.rstrip("ms")) / (1000 if value.endswith("ms") else 1), 2)
        elif key == "TasksMax" and value.isdigit():
            limits["tasks"] = int(value)
    return {"name": name, "user": user, "disk_total": usage.total, "disk_used": usage.used,
            "apps": [a["name"] for a in _apps(user)], "limits": limits}


def customer_remove(name, confirm=""):
    """Remove a customer completely: their apps, data, disk and user."""
    user = customer_user(name)
    if confirm != name:
        raise AgentError("Confirm with the customer's name.")
    if not _exists(user):
        raise AgentError("No such customer.")
    home, image = HOMES / user, DISKS / f"{user}.img"
    run(["loginctl", "disable-linger", user], check=False)
    run(["loginctl", "terminate-user", user], check=False)
    for _ in range(20):
        if run(["pgrep", "-u", user], check=False).returncode != 0:
            break
        run(["pkill", "-KILL", "-u", user], check=False)
        time.sleep(0.5)
    if run(["mountpoint", "-q", str(home)], check=False).returncode == 0:
        run(["umount", str(home)])
    lines = Path("/etc/fstab").read_text().splitlines(keepends=True)
    Path("/etc/fstab").write_text("".join(line for line in lines if not line.startswith(f"{image} ")))
    run(["userdel", user])
    image.unlink(missing_ok=True)
    shutil.rmtree(REGISTRY / user, ignore_errors=True)
    if home.exists():
        home.rmdir()
    return {"removed": name}


def customers():
    names = [p.pw_name[2:] for p in pwd.getpwall() if p.pw_name.startswith("c-") and UID_RANGE[0] <= p.pw_uid <= UID_RANGE[1]]
    return {"customers": [customer_info(n) for n in sorted(names)]}


# --- apps --------------------------------------------------------------------------------

def _app_dir(user, app):
    return HOMES / user / "apps" / app


def _record(user, app):
    return REGISTRY / user / f"{app}.json"


def _save_record(user, app):
    folder = REGISTRY / user
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = folder / f".{app['name']}.json"
    tmp.write_text(json.dumps(app, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, _record(user, app["name"]))


def _apps(user):
    apps = []
    for meta in sorted((REGISTRY / user).glob("*.json")):
        try:
            apps.append(json.loads(meta.read_text()))
        except (OSError, ValueError):
            continue
    return apps


def _used_ports():
    ports = set()
    for user_dir in REGISTRY.iterdir() if REGISTRY.exists() else []:
        for meta in user_dir.glob("*.json"):
            try:
                port = json.loads(meta.read_text()).get("host_port")
            except (OSError, ValueError, PermissionError):
                continue
            if port:
                ports.add(int(port))
    return ports


def _free_port():
    used = _used_ports()
    return next(p for p in range(*PORT_RANGE) if p not in used)


def _unit(app):
    lines = ["[Unit]", f"Description=LNMT app {app['name']}", "",
             "[Container]", f"ContainerName={app['name']}", f"Image={app['image']}", "Network=lnmt.network",
             "NoNewPrivileges=true", "AutoUpdate=registry" if app.get("auto_update") else "#"]
    if app.get("host_port") and app.get("port"):
        lines.append(f"PublishPort={config()['node_ip']}:{app['host_port']}:{app['port']}")
    for volume in app.get("volumes", []):
        lines.append(f"Volume=%h/apps/{app['name']}/data/{volume['name']}:{volume['path']}:Z")
    lines.append(f"EnvironmentFile=%h/apps/{app['name']}/env")
    if app.get("command"):
        lines.append(f"Exec={app['command']}")
    lines.append(f"PodmanArgs=--memory={int(app['memory_mb'])}m --cpus={float(app['cpus'])} --pids-limit=1024")
    lines += ["", "[Service]", "Restart=always", "TimeoutStartSec=900", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(line for line in lines if line != "#")


def _clean_env(env):
    if not isinstance(env, dict):
        raise AgentError("Environment must be name -> value.")
    clean = {}
    for key, value in env.items():
        if not ENV_KEY.match(str(key)):
            raise AgentError(f"Bad environment name: {str(key)[:40]}")
        value = str(value)
        if "\n" in value or "\0" in value or len(value) > 4000:
            raise AgentError(f"Bad value for {key}")
        clean[key] = value
    return clean


def _env_file(env):
    # Podman's env-file format: NAME=value to the end of the line, taken
    # literally (quotes would become part of the value). Values never hold a
    # newline (_clean_env).
    return "".join(f"{k}={v}\n" for k, v in sorted(env.items()))


HOOK = """#!/bin/sh
# Pushing to main (or master) checks the code out and builds and redeploys the app.
# Pushing to lnmt-preview/<name> runs a throwaway preview next to it (preview.py).
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
while read old new ref; do
  case "$ref" in
    refs/heads/main|refs/heads/master)
      git --git-dir="$HOME/git/{app}.git" --work-tree="$HOME/apps/{app}/src" checkout -f "$new" -- . || exit 1
      python3 /usr/local/lib/lnmt_node/build.py {app} "$new" ;;
    refs/heads/lnmt-preview/*)
      [ "$new" = 0000000000000000000000000000000000000000 ] || \
        python3 /usr/local/lib/lnmt_node/preview.py {app} "${ref#refs/heads/lnmt-preview/}" "$new" ;;
  esac
done
"""


def refresh_hooks():
    """Give every built app the current hook (after an update)."""
    for entry in pwd.getpwall():
        if not entry.pw_name.startswith("c-") or not UID_RANGE[0] <= entry.pw_uid <= UID_RANGE[1]:
            continue
        for app in _apps(entry.pw_name):
            repo = HOMES / entry.pw_name / "git" / f"{app['name']}.git"
            if app.get("build") and repo.is_dir():
                _write_as(entry.pw_name, repo / "hooks" / "post-receive", HOOK.replace("{app}", app["name"]), mode=0o700)


def app_create(customer, name, image, port=0, env=None, volumes=None, command="", memory_mb=512, cpus=1.0,
               publish=True, build=False, starter=None, auto_update=False):
    user = customer_user(customer)
    if not _exists(user):
        raise AgentError("No such customer.")
    if not NAME.match(name or ""):
        raise AgentError("App names: 2-31 of a-z, 0-9, - (starting with a letter).")
    # Built apps run localhost/<name>:current (their own Dockerfile, git push or
    # uploaded files). A base image (e.g. WordPress) runs under that name until
    # the first build, so the app works from the start.
    base = image if build and IMAGE.match(image or "") and not str(image).startswith("localhost/") else ""
    if build:
        image = f"localhost/{name}:current"
    if not IMAGE.match(image or ""):
        raise AgentError("That is not an image name.")
    if _app_dir(user, name).exists():
        raise AgentError("An app with that name exists.")
    port = int(port or 0)
    if publish and not 1 <= port <= 65535:
        raise AgentError("A web app needs the port it listens on inside the container.")
    memory_mb, cpus = int(memory_mb), float(cpus)
    if not (32 <= memory_mb <= 16384 and 0.1 <= cpus <= 8):
        raise AgentError("Limits: 32-16384 MB memory, 0.1-8 CPUs.")
    vols = []
    for volume in volumes or []:
        vname, path = str(volume.get("name", "")), str(volume.get("path", ""))
        if not NAME.match(vname) or not CONTAINER_PATH.match(path) or ".." in path:
            raise AgentError("Volumes need a name and an absolute path inside the container.")
        vols.append({"name": vname, "path": path})
    if command and ("\n" in command or len(command) > 1000):
        raise AgentError("Bad command.")
    app = {"name": name, "customer": customer, "image": image, "build": bool(build),
           "auto_update": bool(auto_update) and not build, "tasks": [], "port": port if publish else 0,
           "host_port": _free_port() if publish else 0, "volumes": vols, "command": command,
           "memory_mb": memory_mb, "cpus": cpus, "created": time.time()}
    folder = _app_dir(user, name)
    for volume in vols:
        _mkdir_as(user, folder / "data" / volume["name"])
    # A starting point so a new site shows something (index.html, index.php…).
    for rel, text in ((starter or {}).get("files") or {}).items():
        volume = (starter or {}).get("volume", "")
        if not any(v["name"] == volume for v in vols) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rel):
            raise AgentError("Bad starter file.")
        _write_as(user, folder / "data" / volume / rel, str(text)[:100000], mode=0o644)
    _write_as(user, folder / "env", _env_file(_clean_env(env or {})))
    _save_record(user, app)
    _write_unit(user, app)
    user_systemctl(user, "daemon-reload")
    if build:
        _mkdir_as(user, folder / "src")
        repo = HOMES / user / "git" / f"{name}.git"
        run(["git", "init", "-q", "--bare", "-b", "main", str(repo)], user=user)
        _write_as(user, repo / "hooks" / "post-receive", HOOK.replace("{app}", name), mode=0o700)
        if not base:
            return app_status(customer, name)  # starts after its first build
        run(["podman", "pull", "-q", base], user=user, timeout=900)
        run(["podman", "tag", base, image], user=user)
        user_systemctl(user, "start", f"{name}.service")
        return app_status(customer, name)
    run(["podman", "pull", "-q", image], user=user, timeout=900)
    user_systemctl(user, "start", f"{name}.service")
    return app_status(customer, name)


def app_build(customer, name):
    user, app = _load_app(customer, name)
    if not app.get("build"):
        raise AgentError("This app runs a ready-made image; change its image instead.")
    result = run(["python3", "/usr/local/lib/lnmt_node/build.py", name], user=user, check=False, timeout=1800)
    return {**app_build_status(customer, name), "output": (result.stdout + result.stderr)[-4000:]}


def app_build_status(customer, name):
    user, _ = _load_app(customer, name)
    folder = _app_dir(user, name)
    try:
        status = json.loads((folder / "build.json").read_text())
    except (OSError, ValueError):
        status = {"state": "never built"}
    try:
        log = (folder / "build.log").read_text(errors="replace")[-20000:]
    except OSError:
        log = ""
    return {"build": status, "log": log}


def app_files(customer, name, op, args=None):
    """List / read / write / mkdir / delete / rename inside the app's folder
    (data volumes and the build source), as the customer, in their user namespace."""
    user, _ = _load_app(customer, name)
    if op not in ("list", "read", "write", "mkdir", "delete", "rename"):
        raise AgentError("Unknown file operation.")
    result = run(["podman", "unshare", "python3", "/usr/local/lib/lnmt_node/files.py", str(_app_dir(user, name)), op],
                 user=user, check=False, input_text=json.dumps(args or {}), timeout=300)
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AgentError(f"File operation failed: {(result.stderr or '').strip()[-300:]}")
    if not answer.get("ok"):
        raise AgentError(answer.get("error") or "File operation failed.")
    return answer["result"]


def _write_unit(user, app):
    _write_as(user, HOMES / user / ".config/containers/systemd" / f"{app['name']}.container", _unit(app))


def _load_app(customer, name):
    user = customer_user(customer)
    meta = _record(user, name)
    if not NAME.match(name or "") or not meta.exists():
        raise AgentError("No such app.")
    return user, json.loads(meta.read_text())


def app_update(customer, name, image=None, env=None, command=None, memory_mb=None, cpus=None, auto_update=None):
    user, app = _load_app(customer, name)
    if auto_update is not None:
        app["auto_update"] = bool(auto_update) and not app.get("build")
    if image is not None:
        if not IMAGE.match(image):
            raise AgentError("That is not an image name.")
        app["image"] = image
    if command is not None:
        app["command"] = command
    if memory_mb is not None:
        app["memory_mb"] = int(memory_mb)
    if cpus is not None:
        app["cpus"] = float(cpus)
    if not (32 <= app["memory_mb"] <= 16384 and 0.1 <= app["cpus"] <= 8):
        raise AgentError("Limits: 32-16384 MB memory, 0.1-8 CPUs.")
    folder = _app_dir(user, name)
    if env is not None:
        _write_as(user, folder / "env", _env_file(_clean_env(env)))
    _save_record(user, app)
    _write_unit(user, app)
    user_systemctl(user, "daemon-reload")
    if image is not None:
        run(["podman", "pull", "-q", app["image"]], user=user, timeout=900)
    user_systemctl(user, "restart", f"{name}.service")
    return app_status(customer, name)


def app_env_names(customer, name):
    """Variable names only (values are never sent back)."""
    user, _ = _load_app(customer, name)
    text = (_app_dir(user, name) / "env").read_text()
    return {"names": [line.split("=", 1)[0] for line in text.splitlines() if "=" in line]}


def app_action(customer, name, action):
    user, _ = _load_app(customer, name)
    if action not in ("start", "stop", "restart"):
        raise AgentError("Unknown action.")
    user_systemctl(user, action, f"{name}.service")
    return app_status(customer, name)


def app_remove(customer, name, keep_data=False):
    user, app = _load_app(customer, name)
    if app.get("tasks"):
        app_tasks_set(customer, name, [])
    keys = _load_keys(user)
    if keys["deploy"].pop(name, None) is not None:
        _write_keys(user, keys)
    user_systemctl(user, "stop", f"{name}.service", check=False)
    for leftover in (f"{name}-preview", f"{name}-preview-db"):
        run(["podman", "rm", "-f", leftover], user=user, check=False)
    (HOMES / user / ".config/containers/systemd" / f"{name}.container").unlink(missing_ok=True)
    user_systemctl(user, "daemon-reload")
    folder = _app_dir(user, name)
    if keep_data:
        _record(user, name).rename(_record(user, name).with_suffix(".removed"))
    else:
        # Data written by the container belongs to its sub-UIDs: remove it
        # inside the customer's user namespace.
        run(["podman", "unshare", "rm", "-rf", "--one-file-system", str(folder)], user=user)
        _record(user, name).unlink(missing_ok=True)
    run(["podman", "image", "prune", "-f"], user=user, check=False)
    return {"removed": name, "kept_data": bool(keep_data)}


def app_status(customer, name):
    user, app = _load_app(customer, name)
    state = user_systemctl(user, "is-active", f"{name}.service", check=False).stdout.strip()
    stats = run(["podman", "stats", "--no-stream", "--format", "json", name], user=user, check=False)
    usage = {}
    try:
        first = (json.loads(stats.stdout or "[]") or [{}])[0]
        usage = {"cpu": first.get("cpu_percent"), "memory": first.get("mem_usage")}
    except (ValueError, IndexError, AttributeError):
        pass
    return {**app, "state": state, "usage": usage}


def apps(customer=None):
    users = [customer_user(customer)] if customer else [p.pw_name for p in pwd.getpwall() if p.pw_name.startswith("c-")]
    result = []
    for user in users:
        if not _exists(user):
            continue
        for app in _apps(user):
            state = user_systemctl(user, "is-active", f"{app['name']}.service", check=False).stdout.strip()
            result.append({**app, "state": state})
    return {"apps": result}


def app_logs(customer, name, lines=200):
    user, _ = _load_app(customer, name)
    lines = max(10, min(int(lines), 2000))
    out = run(["podman", "logs", "--tail", str(lines), "--timestamps", name], user=user, check=False)
    if out.returncode != 0 and "no such container" in out.stderr:
        building = app_build_status(customer, name)["build"].get("state") == "never built"
        return {"logs": "Not running yet: it starts after its first build (a git push, LAIka's first approved change, or Build now)."
                if building else "Not running: start it, or see the build log."}
    return {"logs": (out.stdout + out.stderr)[-200000:]}


# --- databases ---------------------------------------------------------------------------

def _db_kind(app):
    image = app["image"].split("/")[-1]
    if image.startswith(("mariadb", "mysql")):
        return "mariadb"
    if image.startswith("postgres"):
        return "postgres"
    raise AgentError("Export and import work for MariaDB/MySQL and PostgreSQL apps.")


DUMP = {"mariadb": 'exec mariadb-dump --single-transaction -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE"',
        "postgres": 'exec pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'}
LOAD = {"mariadb": 'exec mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE"',
        "postgres": 'exec psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"'}


def app_db_export(customer, name):
    """Dump the app's database into ~/apps/<app>/backups/<stamp>.sql (download it from Files)."""
    user, app = _load_app(customer, name)
    kind = _db_kind(app)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = _app_dir(user, name) / "backups"
    _mkdir_as(user, folder)
    target = folder / f"{name}-{stamp}.sql"
    script = f'podman exec {name} sh -c \'{DUMP[kind]}\' > "$1"'
    result = run(["sh", "-c", script, "sh", str(target)], user=user, check=False, timeout=360)
    if result.returncode != 0:
        run(["rm", "-f", str(target)], user=user, check=False)
        if "connect" in result.stderr.lower():
            raise AgentError("The database is not answering yet (still starting, or stopped); try again in a minute.")
        raise AgentError(f"Export failed: {result.stderr.strip()[-300:]}")
    return {"file": f"backups/{target.name}", "bytes": target.stat().st_size}


def app_db_import(customer, name, path):
    """Load an .sql file from the app's folder (upload it with Files or SFTP) into its database."""
    user, app = _load_app(customer, name)
    kind = _db_kind(app)
    rel = str(path or "").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}\.sql", rel) or ".." in rel:
        raise AgentError("Choose an .sql file inside the app's folder.")
    source = _app_dir(user, name) / rel
    script = f'podman exec -i {name} sh -c \'{LOAD[kind]}\' < "$1"'
    result = run(["sh", "-c", script, "sh", str(source)], user=user, check=False, timeout=360)
    if result.returncode != 0:
        raise AgentError(f"Import failed: {(result.stderr or result.stdout).strip()[-400:]}")
    return {"imported": rel}


# --- scheduled tasks --------------------------------------------------------------------

TASK = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def _task_unit(user, app, task):
    """A user timer + service that runs the task's command inside the app's
    container, appending its output to ~/apps/<app>/tasks/<task>.log. The
    command sits in <task>.cmd (fed to the container's sh), so no quoting."""
    unit = f"lnmt-task-{app}-{task['name']}"
    base = f"%h/apps/{app}/tasks/{task['name']}"
    _write_as(user, _app_dir(user, app) / "tasks" / f"{task['name']}.cmd", task["command"] + "\n")
    service = (f"[Unit]\nDescription=LNMT task {task['name']} of {app}\n\n[Service]\nType=oneshot\n"
               f"ExecStart=/bin/sh -c \"exec >>{base}.log 2>&1; date -Is; podman exec -i {app} sh -s <{base}.cmd\"\n"
               f"ExecStopPost=/bin/sh -c \"tail -n 2000 {base}.log > {base}.log.tmp; mv {base}.log.tmp {base}.log\"\n"
               "TimeoutStartSec=3600\n")
    timer = (f"[Unit]\nDescription=LNMT task {task['name']} of {app}\n\n[Timer]\nOnCalendar={task['schedule']}\n"
             "Persistent=true\n\n[Install]\nWantedBy=timers.target\n")
    folder = HOMES / user / ".config" / "systemd" / "user"
    _write_as(user, folder / f"{unit}.service", service)
    _write_as(user, folder / f"{unit}.timer", timer)
    return unit


def app_tasks_set(customer, name, tasks):
    """Replace the app's scheduled tasks: [{name, schedule (systemd calendar, e.g.
    "hourly", "*-*-* 03:00"), command (run inside the app's container)}]."""
    user, app = _load_app(customer, name)
    clean = []
    for task in tasks or []:
        tname, schedule, command = str(task.get("name", "")), str(task.get("schedule", "")).strip(), str(task.get("command", "")).strip()
        if not TASK.match(tname) or not command or "\n" in command or len(command) > 500:
            raise AgentError("Each task needs a name (a-z, 0-9, -) and a one-line command.")
        if not schedule or "\n" in schedule or run(["systemd-analyze", "calendar", schedule], check=False).returncode != 0:
            raise AgentError(f"Not a schedule systemd understands: {schedule[:60]} (try hourly, daily, *-*-* 03:00).")
        clean.append({"name": tname, "schedule": schedule, "command": command})
    if len({t["name"] for t in clean}) != len(clean) or len(clean) > 20:
        raise AgentError("Task names must differ (at most 20 tasks).")
    for old in app.get("tasks", []):
        unit = f"lnmt-task-{name}-{old['name']}"
        user_systemctl(user, "disable", "--now", f"{unit}.timer", check=False)
        for suffix in ("service", "timer"):
            (HOMES / user / ".config" / "systemd" / "user" / f"{unit}.{suffix}").unlink(missing_ok=True)
    _mkdir_as(user, _app_dir(user, name) / "tasks")
    for task in clean:
        _task_unit(user, name, task)
    user_systemctl(user, "daemon-reload")
    for task in clean:
        user_systemctl(user, "enable", "--now", f"lnmt-task-{name}-{task['name']}.timer")
    app["tasks"] = clean
    _save_record(user, app)
    return {"tasks": clean}


def app_task_run(customer, name, task):
    user, app = _load_app(customer, name)
    if task not in [t["name"] for t in app.get("tasks", [])]:
        raise AgentError("No such task.")
    user_systemctl(user, "start", "--no-block", f"lnmt-task-{name}-{task}.service")
    return {"started": task}


# --- stats (the edge's monitor collects them every few minutes) ------------------------------

def _bytes(text):
    match = re.match(r"([\d.]+)\s*([kKMGT]?i?B)", str(text or "").strip())
    if not match:
        return 0
    units = {"B": 1, "kB": 1e3, "KB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 2 ** 20, "GB": 1e9, "GiB": 2 ** 30, "TB": 1e12}
    return int(float(match.group(1)) * units.get(match.group(2), 1))


def stats():
    """Per customer: disk; per app: state, CPU %, memory bytes, last build."""
    out = []
    for entry in pwd.getpwall():
        if not entry.pw_name.startswith("c-") or not UID_RANGE[0] <= entry.pw_uid <= UID_RANGE[1]:
            continue
        user, home = entry.pw_name, HOMES / entry.pw_name
        try:
            usage = shutil.disk_usage(home)
        except OSError:
            continue
        live = {}
        result = run(["podman", "stats", "--no-stream", "--format", "json"], user=user, check=False, timeout=60)
        try:
            for item in json.loads(result.stdout or "[]") or []:
                live[item.get("name")] = {"cpu": float(str(item.get("cpu_percent", "0")).rstrip("%") or 0),
                                          "memory": _bytes(str(item.get("mem_usage", "")).split("/")[0])}
        except (ValueError, TypeError):
            pass
        apps_out = []
        for app in _apps(user):
            state = user_systemctl(user, "is-active", f"{app['name']}.service", check=False).stdout.strip()
            build = {}
            try:
                build = json.loads((_app_dir(user, app["name"]) / "build.json").read_text())
            except (OSError, ValueError):
                pass
            apps_out.append({"name": app["name"], "state": state, **live.get(app["name"], {"cpu": 0.0, "memory": 0}),
                             "build_state": build.get("state"), "build_finished": build.get("finished")})
        out.append({"customer": user[2:], "disk_used": usage.used, "disk_total": usage.total, "apps": apps_out})
    return {"at": time.time(), "customers": out}


def info():
    usage = shutil.disk_usage(BASE if BASE.exists() else "/")
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        mem[key] = int(value.split()[0]) * 1024 if value.split() else 0
    podman = run(["podman", "--version"], check=False).stdout.strip()
    return {"hostname": os.uname().nodename, "cpus": os.cpu_count(), "memory_total": mem.get("MemTotal"),
            "memory_available": mem.get("MemAvailable"), "disk_total": usage.total, "disk_free": usage.free,
            "podman": podman, "node_ip": config()["node_ip"], "load": os.getloadavg()}


ACTIONS = {
    "info": info,
    "customers": customers,
    "customer_ensure": customer_ensure,
    "customer_info": customer_info,
    "customer_remove": customer_remove,
    "customer_set_keys": customer_set_keys,
    "app_build": app_build,
    "app_build_status": app_build_status,
    "app_files": app_files,
    "app_deploy_key": app_deploy_key,
    "app_db_export": app_db_export,
    "app_db_import": app_db_import,
    "app_tasks_set": app_tasks_set,
    "app_task_run": app_task_run,
    "stats": stats,
    "apps": apps,
    "app_create": app_create,
    "app_update": app_update,
    "app_env_names": app_env_names,
    "app_action": app_action,
    "app_remove": app_remove,
    "app_status": app_status,
    "app_logs": app_logs,
}


def handle(raw):
    import inspect
    try:
        request = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "bad request"}
    if not isinstance(request, dict) or request.get("action") not in ACTIONS:
        return {"ok": False, "error": "unknown action"}
    function = ACTIONS[request["action"]]
    args = request.get("args") or {}
    if not isinstance(args, dict) or set(args) - set(inspect.signature(function).parameters):
        return {"ok": False, "error": "unknown argument"}
    try:
        return {"ok": True, "result": function(**args)}
    except AgentError as exc:
        return {"ok": False, "error": str(exc)}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        print(f"lnmt-node {request['action']}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"ok": False, "error": f"{request['action']} failed: {type(exc).__name__}"}


def main():
    if os.geteuid() != 0:
        sys.exit("run as root")
    raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        answer = {"ok": False, "error": "request too large"}
    else:
        answer = handle(raw.decode("utf-8", "replace"))
    sys.stdout.write(json.dumps(answer, default=str) + "\n")
    return 0
