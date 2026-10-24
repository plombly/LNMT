import ipaddress
import getpass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .common import normalize_hostname
from .state import load_json
API="https://api.cloudflare.com/client/v4"
CREDS_DIR=Path("/etc/lnmt/credentials")

def cf_api(token, method, path, data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "LNMT/1.0",
    }

    body = None

    if data is not None:
        body = json.dumps(data).encode()

    req = urllib.request.Request(
        API + path,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

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


def credential_files():
    return sorted(CREDS_DIR.glob("*.json"))


def load_credential(zone):
    path = CREDS_DIR / f"{zone}.json"

    if not path.exists():
        return None

    return load_json(path)


def find_zone_for_hostname(hostname):
    candidates = []

    for path in credential_files():
        zone = path.stem

        if hostname == zone or hostname.endswith("." + zone):
            candidates.append(zone)

    if not candidates:
        return None

    # Longest match wins.
    return max(candidates, key=len)


def cf_get_record(cred, hostname, record_type=None, record_id=None):
    """The record for hostname: of record_type if given (any type
    otherwise), preferring the one with record_id (the record this manager
    created)."""
    params = {"name": hostname}
    if record_type:
        params["type"] = record_type
    query = urllib.parse.urlencode(params)

    result = cf_api(
        cred["token"],
        "GET",
        f"/zones/{cred['zone_id']}/dns_records?{query}",
    )

    records = result.get("result", [])
    if record_id:
        for record in records:
            if record.get("id") == record_id:
                return record

    return records[0] if records else None


def get_public_ip():
    req = urllib.request.Request(
        "https://api.ipify.org",
        headers={"User-Agent": "LNMT/1.0"},
    )

    with urllib.request.urlopen(req, timeout=10) as r:
        text = r.read().decode().strip()
    ip = ipaddress.ip_address(text)  # refuses anything that is not an address
    if ip.version != 4 or not ip.is_global:
        raise ValueError(f"unexpected public IP from ipify: {text[:40]}")
    return str(ip)


def cf_create_record(cred, hostname, public_ip):
    payload = {
        "type": "A",
        "name": hostname,
        "content": public_ip,
        "ttl": 1,
        "proxied": True,
        "comment": "Managed by LAIka NGINx Management Terminal",
    }

    return cf_api(
        cred["token"],
        "POST",
        f"/zones/{cred['zone_id']}/dns_records",
        payload,
    )


def cf_delete_record(cred, record_id):
    return cf_api(
        cred["token"],
        "DELETE",
        f"/zones/{cred['zone_id']}/dns_records/{record_id}",
    )


def add_credential():
    print("\nAdd Cloudflare Credential")
    print("-------------------------")

    zone = normalize_hostname(
        input("Cloudflare zone: ")
    )

    print()
    print("Recommended token permissions:")
    print("  Zone / DNS / Edit")
    print("  Zone / Zone / Read")
    print("  Zone / SSL and Certificates / Edit")
    print()
    print(f"Restrict token to: {zone}")
    print()

    token = getpass.getpass(
        "API token (hidden): "
    ).strip()

    if not token:
        print("Cancelled.")
        return False

    query = urllib.parse.urlencode({
        "name": zone,
    })

    try:
        result = cf_api(
            token,
            "GET",
            f"/zones?{query}",
        )

        zones = result.get("result", [])

        if not zones:
            raise RuntimeError(
                "Token cannot access this zone."
            )

        zone_id = zones[0]["id"]

        # Verify DNS API access.
        cf_api(
            token,
            "GET",
            f"/zones/{zone_id}/dns_records?per_page=1",
        )

    except Exception as e:
        print(f"\nFAILED: {e}")
        return False

    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDS_DIR, 0o700)

    path = CREDS_DIR / f"{zone}.json"

    payload = {
        "zone": zone,
        "zone_id": zone_id,
        "token": token,
    }

    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )

    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    os.chmod(path, 0o600)

    print(f"\nCredential stored for {zone}.")
    return True
