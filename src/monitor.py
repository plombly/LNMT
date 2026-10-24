"""lnmt-monitor (root, every 2 minutes): uptime checks and alerts.

Each run:
  - checks every enabled site's backend (the same TCP + HTTP test as the
    menu) and keeps 7 days of results (uptime on the dashboard);
  - looks at DDNS health and the public IP, certificate expiry, the last
    backup, and failed dashboard sign-ins;
  - sends an alert when something changes state (a site goes down after two
    failed checks in a row, comes back, DDNS degrades or recovers, the public
    IP changes, a certificate has under 21 days left, a backup fails or is
    over 2 days old, 5+ failed sign-ins in 15 minutes), once per change.

Alert targets live in /etc/lnmt/notify.json (root only, 0600):
  {"discord_webhook": "...", "ntfy_url": "...", "events": {"site_down": true, ...}}
They are set from the dashboard (System -> Alerts) and never shown again.

Everyone else can have their own targets (/etc/lnmt/notify-people/<user>.json,
set under Account): they hear about the sites they have access to and their
own hosted apps (down / back, a failed build, a nearly full disk).

Each run also records usage for graphs (/var/lib/lnmt/stats.json): CPU
and memory of every hosted app (from the node) and requests per minute of
every site (from the per-site count logs nginx writes). Two-minute points are
kept for a day, hourly averages for a week.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .backend import backend_test
from .state import load_state

MONITOR_FILE = Path("/var/lib/lnmt/monitor.json")
NOTIFY_FILE = Path("/etc/lnmt/notify.json")
AUDIT_FILE = Path("/var/lib/lnmt-web/audit.jsonl")
BACKUP_DIR = Path("/var/backups/lnmt")
HISTORY_SECONDS = 7 * 86400
STATS_FILE = Path("/var/lib/lnmt/stats.json")
PEOPLE_DIR = Path("/etc/lnmt/notify-people")
USERS_FILE = Path("/var/lib/lnmt-web/users.json")
SITE_LOGS = Path("/var/log/nginx/lnmt-sites")
PERSON = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
EVENTS = {
    "site_down": "A site's backend stops answering, or comes back",
    "app_down": "A hosted app stops running, or comes back",
    "build": "A hosted app's build fails",
    "disk": "A hosting customer's disk is over 90% full",
    "ddns": "Dynamic DNS degrades or recovers",
    "public_ip": "This network's public IP changes",
    "certificate": "A certificate has under 21 days left",
    "backup": "A backup fails or is more than 2 days old",
    "logins": "5 or more failed dashboard sign-ins within 15 minutes",
}


def _write_json(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# --- alert targets -------------------------------------------------------------------------

def notify_config():
    return _load(NOTIFY_FILE, {})


PERSON_EVENTS = {
    "site_down": "A site you have access to stops answering, or comes back",
    "app_down": "One of your apps stops running, or comes back",
    "build": "A build of one of your apps fails",
    "disk": "Your hosting disk is over 90% full",
}
OFF_BY_DEFAULT = {"build"}  # for administrators: customers' builds are theirs


def _person_path(username):
    if not PERSON.match(str(username)):
        raise ValueError("Bad user name.")
    return PEOPLE_DIR / f"{username}.json"


def person_config(username):
    return _load(_person_path(username), {})


def public_person_config(username):
    config = person_config(username)
    return {"discord": bool(config.get("discord_webhook")), "ntfy": bool(config.get("ntfy_url")),
            "events": {name: bool(config.get("events", {}).get(name, True)) for name in PERSON_EVENTS},
            "labels": PERSON_EVENTS}


def _merge_targets(config, discord_webhook, ntfy_url):
    for key, value in (("discord_webhook", discord_webhook), ("ntfy_url", ntfy_url)):
        if value is None:
            continue
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("Alert addresses must start with https://")
        if value:
            config[key] = value[:500]
        else:
            config.pop(key, None)


def save_person_config(username, discord_webhook=None, ntfy_url=None, events=None):
    path = _person_path(username)
    config = person_config(username)
    _merge_targets(config, discord_webhook, ntfy_url)
    if isinstance(events, dict):
        config["events"] = {name: bool(events.get(name, True)) for name in PERSON_EVENTS}
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(PEOPLE_DIR, 0o700)
    _write_json(path, config)
    return public_person_config(username)


def public_notify_config():
    config = notify_config()
    events = {name: bool(config.get("events", {}).get(name, name not in OFF_BY_DEFAULT)) for name in EVENTS}
    return {"discord": bool(config.get("discord_webhook")), "ntfy": bool(config.get("ntfy_url")),
            "events": events, "labels": EVENTS}


def save_notify_config(discord_webhook=None, ntfy_url=None, events=None):
    """None keeps a value; "" removes it."""
    config = notify_config()
    _merge_targets(config, discord_webhook, ntfy_url)
    if isinstance(events, dict):
        config["events"] = {name: bool(events.get(name, name not in OFF_BY_DEFAULT)) for name in EVENTS}
    _write_json(NOTIFY_FILE, config)
    return public_notify_config()


def send(title, message, config=None, opener=urllib.request.urlopen):
    """Send to every configured target; returns how many accepted it."""
    config = notify_config() if config is None else config
    sent = 0
    if config.get("discord_webhook"):
        body = json.dumps({"content": f"**{title}**\n{message}"[:1900]}).encode()
        request = urllib.request.Request(config["discord_webhook"], data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "lnmt-monitor"})
        try:
            opener(request, timeout=10).close()
            sent += 1
        except Exception as exc:
            print(f"discord: {type(exc).__name__}", file=sys.stderr)
    if config.get("ntfy_url"):
        request = urllib.request.Request(config["ntfy_url"], data=message.encode()[:3000], method="POST",
                                         headers={"Title": title.encode("ascii", "replace").decode(),
                                                  "User-Agent": "lnmt-monitor"})
        try:
            opener(request, timeout=10).close()
            sent += 1
        except Exception as exc:
            print(f"ntfy: {type(exc).__name__}", file=sys.stderr)
    return sent


# --- checks --------------------------------------------------------------------------------

def _cert_days(path):
    result = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
                            capture_output=True, text=True)
    if result.returncode != 0 or "=" not in result.stdout:
        return None
    import datetime
    when = datetime.datetime.strptime(result.stdout.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z")
    return int((when.replace(tzinfo=datetime.timezone.utc).timestamp() - time.time()) // 86400)


def _backup_problem():
    result = subprocess.run(["systemctl", "show", "lnmt-backup.service", "--property=Result", "--value"],
                            capture_output=True, text=True)
    if result.stdout.strip() not in ("success", ""):
        return f"the last backup run ended with: {result.stdout.strip()}"
    newest = max((p.stat().st_mtime for p in BACKUP_DIR.glob("lnmt-backup-*.tar.gz")), default=0)
    if newest and time.time() - newest > 2 * 86400:
        return f"the newest backup is {int((time.time() - newest) // 3600)} hours old"
    return ""


def _failed_logins(memory, now):
    """Failed sign-ins in the last 15 minutes (reads only new audit lines)."""
    try:
        size = AUDIT_FILE.stat().st_size
    except OSError:
        return 0
    offset = memory.get("audit_offset", 0)
    if offset > size:
        offset = 0  # the log was replaced
    recent = [t for t in memory.get("login_failures", []) if now - t < 900]
    with AUDIT_FILE.open() as handle:
        handle.seek(offset)
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("action") == "login" and not entry.get("ok"):
                recent.append(float(entry.get("at", now)))
        memory["audit_offset"] = handle.tell()
    memory["login_failures"] = [t for t in recent if now - t < 900]
    return len(memory["login_failures"])


# --- usage for graphs ---------------------------------------------------------------------

DAY = 86400


def _compact(points, now):
    """Keep two-minute points for a day; older ones become hourly averages, for a week."""
    recent = [p for p in points if now - p[0] < DAY]
    old = {}
    for point in points:
        if DAY <= now - point[0] < HISTORY_SECONDS:
            old.setdefault(int(point[0] // 3600), []).append(point)
    hourly = []
    for hour, group in sorted(old.items()):
        if len(group) == 1:
            hourly.append(group[0])
            continue
        values = [round(sum(p[i] for p in group) / len(group), 2) for i in range(1, len(group[0]))]
        hourly.append([hour * 3600 + 1800, *values])
    return hourly + recent


def _count_requests(hostname, memory):
    """Requests and 5xx answers since the last run, from nginx's per-site count log."""
    path = SITE_LOGS / f"{hostname}.log"
    try:
        info = path.stat()
    except OSError:
        return None
    offsets = memory.setdefault("log_offsets", {})
    inode, offset = offsets.get(hostname, [info.st_ino, info.st_size])  # a new log counts from now
    if inode != info.st_ino or offset > info.st_size:
        offset = 0  # rotated
    total = errors = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        for line in handle:
            total += 1
            parts = line.split()
            if len(parts) == 2 and parts[1][:1] == b"5":
                errors += 1
        offsets[hostname] = [info.st_ino, handle.tell()]
    return total, errors


def record_usage(stats, memory, state, now, node_stats):
    """Append this run's points: requests per minute per site, CPU and memory per app."""
    last = memory.get("usage_last") or now - 120
    minutes = max((now - last) / 60, 0.5)
    memory["usage_last"] = now
    sites = stats.setdefault("sites", {})
    for hostname in state["proxies"]:
        counted = _count_requests(hostname, memory)
        if counted is None:
            continue
        total, errors = counted
        sites[hostname] = _compact(sites.get(hostname, []) + [[now, round(total / minutes, 2), round(errors / minutes, 2)]], now)
    for gone in [h for h in sites if h not in state["proxies"]]:
        sites.pop(gone)
    if node_stats is None:
        return
    apps = stats.setdefault("apps", {})
    seen = set()
    for customer in node_stats.get("customers", []):
        stats.setdefault("disks", {})[customer["customer"]] = [customer["disk_used"], customer["disk_total"]]
        for app in customer["apps"]:
            key = f"{customer['customer']}/{app['name']}"
            seen.add(key)
            apps[key] = _compact(apps.get(key, []) + [[now, round(app.get("cpu", 0), 2), app.get("memory", 0)]], now)
    for gone in [k for k in apps if k not in seen]:
        apps.pop(gone)


def _node_stats():
    try:
        from . import hosting
        if not hosting.nodes():
            return None
        return hosting.call("stats")
    except Exception as exc:  # the node being away must not stop the site checks
        print(f"node stats: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def traffic(hostname):
    return {"points": _load(STATS_FILE, {}).get("sites", {}).get(hostname, []),
            "columns": ["time", "requests per minute", "errors per minute"]}


def app_usage(customer, name):
    return {"points": _load(STATS_FILE, {}).get("apps", {}).get(f"{customer}/{name}", []),
            "columns": ["time", "cpu percent", "memory bytes"]}


def _people():
    """username -> {"customer", "sites", "admin"} from the dashboard's accounts."""
    users = _load(USERS_FILE, {}).get("users", {})
    return {name: {"customer": (u.get("hosting") or {}).get("customer"), "sites": list((u.get("access") or {}).keys()),
                   "admin": u.get("role") == "admin"}
            for name, u in users.items() if not u.get("disabled")}


def _hosting_alerts(nodes, memory, alert):
    conditions = memory.setdefault("conditions", {})
    fails = memory.setdefault("app_fails", {})
    for customer in (nodes or {}).get("customers", []):
        cname = customer["customer"]
        for app in customer["apps"]:
            key = f"app:{cname}/{app['name']}"
            # "inactive" = stopped on purpose; failed / restarting twice in a row = down.
            if app["state"] == "active":
                fails[key] = 0
                alert("app_down", key, "up", f"App {app['name']} is running again", f"{cname}'s app is back.", customer=cname)
            elif app["state"] != "inactive":
                fails[key] = fails.get(key, 0) + 1
                if fails[key] >= 2:
                    alert("app_down", key, "down", f"App {app['name']} is down",
                          f"{cname}'s app is {app['state']}. Its logs are on the app's page.", customer=cname)
            finished = app.get("build_finished") or 0
            build_key = f"build:{cname}/{app['name']}"
            if app.get("build_state") != "failed" or not finished:
                conditions[build_key] = f"ok {finished}"  # each failed build is a new condition
            else:
                alert("build", build_key, f"failed {finished}", f"Build of {app['name']} failed",
                      f"{cname}'s push did not build; the running version stays. The build log is on the app's page.",
                      customer=cname)
        if customer.get("disk_total"):
            share = customer["disk_used"] / customer["disk_total"]
            full = share >= 0.9 or (conditions.get(f"disk:{cname}") == "full" and share >= 0.85)
            percent = round(share * 100)
            alert("disk", f"disk:{cname}", "full" if full else "ok",
                  f"Hosting disk of {cname} is {percent}% full" if full else f"Hosting disk of {cname} has room again",
                  "Remove old files or backups, or ask for a bigger disk." if full else f"{percent}% used.",
                  customer=cname)


def run(now=None, tester=backend_test, sender=send, cert_days=_cert_days, backup_problem=_backup_problem,
        node_stats=_node_stats, people=_people):
    now = time.time() if now is None else now
    memory = _load(MONITOR_FILE, {})
    state = load_state()
    config = notify_config()
    wanted = config.get("events", {})
    alerts = []
    who = people()

    def alert(kind, key, condition, title, message, site=None, customer=None):
        """Remember each condition; alert when it changes. Administrators hear
        it when the event is on for them; people with access to the site, or
        whose app it is, when they turned it on for themselves."""
        last = memory.setdefault("conditions", {}).get(key)
        memory["conditions"][key] = condition
        # The first sighting of a condition is only learned, never announced.
        if last is None or last == condition:
            return
        persons = [name for name, p in who.items() if not p["admin"]
                   and ((site and site in p["sites"]) or (customer and customer == p["customer"]))]
        alerts.append((kind, title, message, persons))

    sites = memory.setdefault("sites", {})
    for hostname, proxy in sorted(state["proxies"].items()):
        record = sites.setdefault(hostname, {"history": [], "fails": 0, "up": True})
        if not proxy.get("enabled", True):
            continue
        ok, message = tester(proxy)
        memory.setdefault("conditions", {}).setdefault(f"site:{hostname}", "up")  # a new site counts as up
        record["history"] = [h for h in record["history"] if now - h[0] < HISTORY_SECONDS] + [[now, ok]]
        record["fails"] = 0 if ok else record.get("fails", 0) + 1
        record["message"] = message
        if ok and not record.get("up", True):
            record["up"] = True
            alert("site_down", f"site:{hostname}", "up", f"{hostname} is back", f"Its backend answers again ({message}).", site=hostname)
        elif record["fails"] >= 2 and record.get("up", True):
            record["up"] = False
            alert("site_down", f"site:{hostname}", "down", f"{hostname} is down",
                  f"Its backend has not answered twice in a row: {message}", site=hostname)
    for gone in [h for h in sites if h not in state["proxies"]]:
        sites.pop(gone)

    ddns = state.get("ddns", {})
    if ddns.get("status"):
        healthy = ddns["status"] == "HEALTHY"
        alert("ddns", "ddns", "ok" if healthy else "bad",
              "Dynamic DNS is healthy again" if healthy else "Dynamic DNS has problems",
              ddns.get("message", ""))
    if ddns.get("public_ip"):
        previous = memory.get("public_ip")
        memory["public_ip"] = ddns["public_ip"]
        if previous and previous != ddns["public_ip"]:
            alerts.append(("public_ip", "Public IP changed", f"{previous} → {ddns['public_ip']}; DNS records follow it.", []))

    for zone in sorted({p.get("zone") for p in state["proxies"].values()}):
        days = cert_days(Path("/etc/lnmt/certs") / zone / "origin.pem")
        if days is not None:
            alert("certificate", f"cert:{zone}", "low" if days < 21 else "ok",
                  f"Certificate for {zone} expires soon" if days < 21 else f"Certificate for {zone} renewed",
                  f"{days} days left." if days < 21 else f"{days} days left.")
    problem = backup_problem()
    alert("backup", "backup", "bad" if problem else "ok",
          "Backup problem" if problem else "Backups are working again", problem or "The last backup succeeded.")
    failures = _failed_logins(memory, now)
    alert("logins", "logins", "many" if failures >= 5 else "few",
          "Many failed dashboard sign-ins" if failures >= 5 else "Failed sign-ins calmed down",
          f"{failures} failed sign-ins in the last 15 minutes.")

    # Hosting: usage for graphs, and apps that stop, builds that fail, full disks.
    stats = _load(STATS_FILE, {})
    nodes = node_stats()
    record_usage(stats, memory, state, now, nodes)
    _write_json(STATS_FILE, stats, mode=0o600)
    _hosting_alerts(nodes, memory, alert)

    # First run: learn the current conditions without announcing them.
    if not memory.get("initialized"):
        alerts = []
        memory["initialized"] = True
    sent = 0
    for kind, title, message, persons in alerts:
        if wanted.get(kind, kind not in OFF_BY_DEFAULT):
            sent += sender(title, message, config)
        for person in persons:
            mine = person_config(person)
            if (mine.get("discord_webhook") or mine.get("ntfy_url")) and mine.get("events", {}).get(kind, True):
                sent += sender(title, message, mine)
    memory["last_run"] = now
    _write_json(MONITOR_FILE, memory, mode=0o600)
    return {"alerts": [a[1] for a in alerts], "sent": sent}


def uptime(hostname, memory=None, now=None):
    """{"up": bool|None, "day": %, "week": %, "message"} from the monitor's history."""
    memory = _load(MONITOR_FILE, {}) if memory is None else memory
    now = time.time() if now is None else now
    record = (memory.get("sites") or {}).get(hostname)
    if not record or not record.get("history"):
        return {"up": None, "day": None, "week": None, "message": ""}

    def percent(seconds):
        points = [ok for t, ok in record["history"] if now - t < seconds]
        return round(100 * sum(1 for ok in points if ok) / len(points), 2) if points else None

    return {"up": record.get("up", True), "day": percent(86400), "week": percent(7 * 86400),
            "message": record.get("message", ""), "last": record["history"][-1][0]}


def main():
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    result = run()
    print(json.dumps(result))
    return 0
