
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"

CREDS_DIR = Path("/etc/lnmt/credentials")
CERT_DIR = Path("/etc/lnmt/certs")


def cf_api(token, method, path, data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "LNMT-TLS/1.0",
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
        with urllib.request.urlopen(req, timeout=20) as r:
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


def load_credential(zone):
    path = CREDS_DIR / f"{zone}.json"

    if not path.exists():
        raise RuntimeError(
            f"No Cloudflare credential for {zone}"
        )

    with path.open() as f:
        return json.load(f)


def cert_paths(zone):
    directory = CERT_DIR / zone

    return (
        directory,
        directory / "origin.pem",
        directory / "origin.key",
        directory / "origin.json",
    )


def cert_valid(cert):
    if not cert.exists():
        return False

    result = subprocess.run(
        [
            "openssl",
            "x509",
            "-checkend",
            str(30 * 24 * 60 * 60),
            "-noout",
            "-in",
            str(cert),
        ],
        capture_output=True,
        text=True,
    )

    return result.returncode == 0


def cert_covers(cert, hostname):
    result = subprocess.run(
        [
            "openssl",
            "x509",
            "-in",
            str(cert),
            "-noout",
            "-checkhost",
            hostname,
        ],
        capture_output=True,
        text=True,
    )

    return result.returncode == 0


def generate_key_and_csr(zone, directory):
    # A new key goes next to the live one, never over it: the live key stays
    # until Cloudflare has issued a certificate for the new one.
    key = directory / "origin.key.new"
    csr = directory / "origin.csr"

    result = subprocess.run(
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={zone}",
            "-addext",
            f"subjectAltName=DNS:{zone},DNS:*.{zone}",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "OpenSSL CSR generation failed:\n"
            + result.stderr
        )

    os.chmod(key, 0o600)
    os.chmod(csr, 0o600)

    return key, csr


def issue(zone):
    cred = load_credential(zone)

    directory, cert, key, metadata = cert_paths(zone)

    directory.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    os.chmod(directory, 0o700)

    # Reuse healthy certificate.
    if cert_valid(cert):
        if (
            cert_covers(cert, zone)
            and cert_covers(cert, f"test.{zone}")
        ):
            print(
                f"Existing Origin CA certificate "
                f"for {zone} is healthy."
            )

            print(cert)
            print(key)
            return 0

    # Generate fresh key/CSR.
    temp_key = None
    temp_csr = None

    try:
        temp_key, temp_csr = generate_key_and_csr(
            zone,
            directory,
        )

        csr_text = temp_csr.read_text()

        payload = {
            "csr": csr_text,
            "hostnames": [
                zone,
                f"*.{zone}",
            ],
            "request_type": "origin-rsa",

            # 15 years.
            "requested_validity": 5475,
        }

        print(
            f"Requesting Cloudflare Origin CA "
            f"certificate for:"
        )
        print(f"  {zone}")
        print(f"  *.{zone}")

        result = cf_api(
            cred["token"],
            "POST",
            "/certificates",
            payload,
        )

        if not result.get("success"):
            raise RuntimeError(
                "Cloudflare rejected certificate request."
            )

        issued = result.get("result", {})

        certificate = issued.get("certificate")

        if not certificate:
            raise RuntimeError(
                "Cloudflare returned no certificate."
            )

        new_cert = cert.with_name(cert.name + ".new")
        new_cert.write_text(
            certificate.rstrip() + "\n"
        )
        os.chmod(new_cert, 0o644)

        # Verify the returned cert before it replaces anything.
        if not cert_covers(new_cert, zone) or not cert_covers(new_cert, f"test.{zone}"):
            raise RuntimeError(
                "Issued certificate does not cover "
                f"{zone} and *.{zone}"
            )

        # Key and certificate change together (nginx reads them at reload).
        os.replace(temp_key, key)
        os.chmod(key, 0o600)
        os.replace(new_cert, cert)

        meta = {
            "zone": zone,
            "certificate_id": issued.get("id"),
            "expires_on": issued.get("expires_on"),
            "hostnames": issued.get(
                "hostnames",
                [zone, f"*.{zone}"],
            ),
            "managed_by": "lnmt-tls",
        }

        metadata.write_text(
            json.dumps(meta, indent=2) + "\n"
        )

        os.chmod(metadata, 0o600)

        temp_csr.unlink(missing_ok=True)

        # Verify the actual returned cert.
        if not cert_covers(cert, zone):
            raise RuntimeError(
                "Issued certificate does not cover "
                f"{zone}"
            )

        if not cert_covers(
            cert,
            f"test.{zone}",
        ):
            raise RuntimeError(
                "Issued certificate wildcard check failed."
            )

        print()
        print("Origin CA certificate issued.")
        print(f"Certificate: {cert}")
        print(f"Private key: {key}")
        print(
            f"Expires:     "
            f"{issued.get('expires_on', 'unknown')}"
        )

        return 0

    except Exception:
        # Never leave a newly generated private key around if issuance
        # failed; the live key and certificate were not touched.
        if temp_csr:
            temp_csr.unlink(missing_ok=True)
        if temp_key:
            temp_key.unlink(missing_ok=True)
        cert.with_name(cert.name + ".new").unlink(missing_ok=True)

        raise


def status(zone):
    directory, cert, key, metadata = cert_paths(zone)

    print(f"Zone:        {zone}")
    print(
        f"Certificate: "
        f"{'FOUND' if cert.exists() else 'MISSING'}"
    )
    print(
        f"Private key: "
        f"{'FOUND' if key.exists() else 'MISSING'}"
    )

    if cert.exists():
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-in",
                str(cert),
                "-noout",
                "-subject",
                "-issuer",
                "-dates",
            ],
            capture_output=True,
            text=True,
        )

        print()
        print(result.stdout.strip())

    if metadata.exists():
        print()
        print("Metadata:")
        print(metadata.read_text().strip())


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo/root.")

    if len(sys.argv) < 3:
        print(
            "Usage:\n"
            "  lnmt-tls issue <zone>\n"
            "  lnmt-tls status <zone>"
        )
        return 1

    command = sys.argv[1]
    zone = sys.argv[2].strip().lower().rstrip(".")

    CERT_DIR.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    os.chmod(CERT_DIR, 0o700)

    try:
        if command == "issue":
            return issue(zone)

        if command == "status":
            status(zone)
            return 0

        print("Unknown command.")
        return 1

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


