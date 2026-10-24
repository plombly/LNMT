import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
CREDS_DIR=Path("/etc/lnmt/credentials")

def pause():
    input("\nPress Enter to continue...")


def require_root():
    if os.geteuid() != 0:
        sys.exit("Run with sudo/root.")








def run(cmd):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
    )


def normalize_hostname(value):
    value = value.strip().lower().rstrip(".")

    if len(value) > 253:
        raise ValueError("Hostname too long.")

    labels = value.split(".")

    if len(labels) < 2:
        raise ValueError("A full hostname is required.")

    label_re = re.compile(
        r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
    )

    for label in labels:
        if not label_re.fullmatch(label):
            raise ValueError(
                f"Invalid hostname label: {label}"
            )

    return value


def validate_ip(value):
    ip = ipaddress.ip_address(value.strip())

    if ip.version != 4:
        raise ValueError("Backend must currently be IPv4.")

    if not (ip.is_private or ip.is_loopback):
        raise ValueError(
            "Backend must be a private or loopback IPv4 address."
        )

    return str(ip)


def validate_port(value):
    port = int(value)

    if not 1 <= port <= 65535:
        raise ValueError("Port must be 1-65535.")

    return port
