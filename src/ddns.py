
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .state import state_lock

API = "https://api.cloudflare.com/client/v4"

STATE_FILE = Path("/var/lib/lnmt/state.json")
CREDS_DIR = Path("/etc/lnmt/credentials")

IP_SERVICES = [
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    with path.open() as f:
        return json.load(f)


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, tmpname = tempfile.mkstemp(
        prefix=".state-",
        dir=str(STATE_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.chmod(tmpname, 0o600)
        os.replace(tmpname, STATE_FILE)

    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def api(token, method, path, data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "LNMT-DDNS/1.0",
    }

    body = None

    if data is not None:
        body = json.dumps(data).encode()

    request = urllib.request.Request(
        API + path,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
        except Exception:
            detail = {}

        messages = [
            x.get("message", str(x))
            for x in detail.get("errors", [])
        ]

        raise RuntimeError(
            "; ".join(messages) or f"HTTP {e.code}"
        )


def get_public_ip():
    import ipaddress

    last_error = None

    for service in IP_SERVICES:
        try:
            request = urllib.request.Request(
                service,
                headers={"User-Agent": "LNMT-DDNS/1.0"},
            )

            with urllib.request.urlopen(request, timeout=10) as response:
                value = response.read().decode().strip()

            ip = ipaddress.ip_address(value)

            if ip.version != 4 or not ip.is_global:
                raise ValueError("Not a public IPv4 address")

            return str(ip)

        except Exception as e:
            last_error = e

    raise RuntimeError(
        f"Could not determine public IPv4: {last_error}"
    )


def load_credential(zone):
    path = CREDS_DIR / f"{zone}.json"

    if not path.exists():
        raise RuntimeError(
            f"No Cloudflare credential for {zone}"
        )

    data = load_json(path)

    if not data.get("token"):
        raise RuntimeError(
            f"Credential for {zone} contains no token"
        )

    if not data.get("zone_id"):
        raise RuntimeError(
            f"Credential for {zone} contains no zone ID"
        )

    return data


def get_record(token, zone_id, hostname):
    query = urllib.parse.urlencode({
        "type": "A",
        "name": hostname,
    })

    result = api(
        token,
        "GET",
        f"/zones/{zone_id}/dns_records?{query}",
    )

    records = result.get("result", [])

    if not records:
        return None

    return records[0]


def update_record(token, zone_id, record, ip):
    payload = {
        "type": "A",
        "name": record["name"],
        "content": ip,
        "ttl": 1,
        "proxied": True,
    }

    return api(
        token,
        "PATCH",
        f"/zones/{zone_id}/dns_records/{record['id']}",
        payload,
    )


def create_record(token, zone_id, hostname, ip):
    payload = {
        "type": "A",
        "name": hostname,
        "content": ip,
        "ttl": 1,
        "proxied": True,
        "comment": "Managed by LNMT DDNS",
    }

    return api(
        token,
        "POST",
        f"/zones/{zone_id}/dns_records",
        payload,
    )


def ensure_structures(state):
    state.setdefault("version", 1)
    state.setdefault("zones", {})
    state.setdefault("proxies", {})
    state.setdefault("ddns", {})

    state["ddns"].setdefault("records", {})


def set_health(record_state, status, message):
    record_state["status"] = status
    record_state["message"] = message
    record_state["last_check"] = now()


def process_record(hostname, config, public_ip):
    zone = config["zone"]

    cred = load_credential(zone)

    token = cred["token"]
    zone_id = cred["zone_id"]

    record = get_record(
        token,
        zone_id,
        hostname,
    )

    if record is None:
        if config.get("repair_missing", True):
            create_record(
                token,
                zone_id,
                hostname,
                public_ip,
            )

            return (
                "HEALTHY",
                f"created A record -> {public_ip}",
            )

        return (
            "DEGRADED",
            "DNS record is missing",
        )

    needs_update = (
        record.get("content") != public_ip
        or record.get("proxied") is not True
    )

    if needs_update:
        old_ip = record.get("content")

        update_record(
            token,
            zone_id,
            record,
            public_ip,
        )

        return (
            "HEALTHY",
            f"updated {old_ip} -> {public_ip}",
        )

    return (
        "HEALTHY",
        f"already correct at {public_ip}",
    )


def main():
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    # One run at a time, and never during a menu change of the same state
    # (it would overwrite it, or re-create a record that is being removed).
    with state_lock():
        return _run()


def _run():

    if not STATE_FILE.exists():
        sys.exit(
            f"State file does not exist: {STATE_FILE}"
        )

    state = load_json(STATE_FILE)
    ensure_structures(state)

    try:
        public_ip = get_public_ip()

    except Exception as e:
        state["ddns"]["status"] = "DEGRADED"
        state["ddns"]["message"] = str(e)
        state["ddns"]["last_check"] = now()

        save_state(state)

        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"WAN IPv4: {public_ip}")

    state["ddns"]["public_ip"] = public_ip
    state["ddns"]["last_check"] = now()

    failures = 0

    records = state["ddns"]["records"]

    if not records:
        print("No DDNS records registered.")

    for hostname, config in sorted(records.items()):

        if not config.get("enabled", True):
            print(f"SKIP  {hostname} (disabled)")
            continue

        print(f"CHECK {hostname}")

        record_state = config.setdefault(
            "health",
            {},
        )

        try:
            status, message = process_record(
                hostname,
                config,
                public_ip,
            )

            set_health(
                record_state,
                status,
                message,
            )

            print(f"  {status}: {message}")

            if status != "HEALTHY":
                failures += 1

        except Exception as e:
            message = str(e)

            # API/DDNS problems NEVER disable the proxy.
            set_health(
                record_state,
                "DEGRADED",
                message,
            )

            print(
                f"  DEGRADED: {message}",
                file=sys.stderr,
            )

            failures += 1

    if failures:
        state["ddns"]["status"] = "DEGRADED"
        state["ddns"]["message"] = (
            f"{failures} managed record(s) require attention"
        )
    else:
        state["ddns"]["status"] = "HEALTHY"
        state["ddns"]["message"] = "All managed records healthy"

    save_state(state)

    return 1 if failures else 0


