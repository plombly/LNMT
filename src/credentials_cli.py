
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
CREDS = Path("/etc/lnmt/credentials")

def api(token, method, path, data=None):
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
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
        except Exception:
            detail = {"errors": [{"message": str(e)}]}

        raise RuntimeError(
            "; ".join(
                x.get("message", str(x))
                for x in detail.get("errors", [])
            ) or str(e)
        )

def safe_zone(zone):
    zone = zone.strip().lower().rstrip(".")

    if not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
        zone,
    ):
        raise ValueError("Invalid DNS zone.")

    return zone

def credential_path(zone):
    return CREDS / f"{zone}.json"

def load_credential(zone):
    path = credential_path(zone)

    if not path.exists():
        return None

    return json.loads(path.read_text())

def save_credential(zone, token, zone_id):
    path = credential_path(zone)

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

def find_zone(token, zone):
    q = urllib.parse.urlencode({"name": zone})

    result = api(token, "GET", f"/zones?{q}")

    zones = result.get("result", [])

    if not zones:
        raise RuntimeError(
            f"Token cannot access Cloudflare zone {zone}."
        )

    return zones[0]["id"]

def test_dns(token, zone_id):
    # Read is enough for this preflight; deployment will test Write
    # when it actually creates the managed record.
    result = api(
        token,
        "GET",
        f"/zones/{zone_id}/dns_records?per_page=1",
    )

    if not result.get("success"):
        raise RuntimeError("DNS API test failed.")

def verify(zone, quiet=False):
    cred = load_credential(zone)

    if not cred:
        if not quiet:
            print(f"No credential stored for {zone}.")
        return False

    try:
        zone_id = find_zone(cred["token"], zone)

        if zone_id != cred["zone_id"]:
            cred["zone_id"] = zone_id
            save_credential(
                zone,
                cred["token"],
                zone_id,
            )

        test_dns(cred["token"], zone_id)

        if not quiet:
            print(f"{zone}: API access OK")

        return True

    except Exception as e:
        if not quiet:
            print(f"{zone}: API ERROR: {e}")
        return False

def add():
    print()
    zone = safe_zone(input("Cloudflare zone: "))

    print()
    print("Recommended token permissions:")
    print("  Zone / DNS / Edit")
    print("  Zone / Zone / Read")
    print("  Zone / SSL and Certificates / Edit")
    print()
    print(f"Restrict the token to: {zone}")
    print()

    token = getpass.getpass("API token (hidden): ").strip()

    if not token:
        print("Cancelled.")
        return

    print()
    print("Testing zone access...")

    try:
        zone_id = find_zone(token, zone)
        test_dns(token, zone_id)
    except Exception as e:
        print(f"FAILED: {e}")
        return

    print(f"Zone found: {zone}")
    print(f"Zone ID:    {zone_id}")
    print("DNS API:    OK")
    print()

    answer = input("Store this credential? [Y/n]: ").strip().lower()

    if answer not in ("", "y", "yes"):
        print("Cancelled.")
        return

    save_credential(zone, token, zone_id)

    print()
    print(f"Credential stored for {zone}.")
    print("The token was not printed or logged.")

def remove():
    print()

    zones = sorted(
        p.stem for p in CREDS.glob("*.json")
    )

    if not zones:
        print("No credentials stored.")
        return

    for i, zone in enumerate(zones, 1):
        print(f"{i}) {zone}")

    print()
    raw = input("Select credential: ").strip()

    try:
        n = int(raw)
        zone = zones[n - 1]
    except Exception:
        print("Invalid selection.")
        return

    print()
    answer = input(
        f"Remove API credential for {zone}? [y/N]: "
    ).strip().lower()

    if answer not in ("y", "yes"):
        print("Cancelled.")
        return

    credential_path(zone).unlink()

    print(f"Credential removed for {zone}.")
    print("Existing sites were NOT disabled.")

def list_credentials():
    print()
    zones = sorted(
        p.stem for p in CREDS.glob("*.json")
    )

    if not zones:
        print("No Cloudflare credentials stored.")
        return

    print("Cloudflare credentials")
    print("----------------------")

    for zone in zones:
        status = "OK" if verify(zone, quiet=True) else "ERROR"
        print(f"{zone:<35} {status}")

def test():
    print()
    zone = safe_zone(input("Zone to test: "))

    print()
    verify(zone)

def menu():
    while True:
        print()
        print("========================================")
        print("       LNMT Cloudflare Credentials")
        print("========================================")
        print()
        print("1) Add credential")
        print("2) Remove credential")
        print("3) List / health check")
        print("4) Test credential")
        print("5) Exit")
        print()

        choice = input("Selection: ").strip()

        if choice == "1":
            add()
        elif choice == "2":
            remove()
        elif choice == "3":
            list_credentials()
        elif choice == "4":
            test()
        elif choice == "5":
            return
        else:
            print("Invalid selection.")

def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo.")

    CREDS.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDS, 0o700)

    menu()

