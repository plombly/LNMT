"""Non-interactive operations for the web dashboard (run as root by
lnmt-admin). Each returns plain data (JSON-able) or raises ServiceError with
a message fit for the page. Secrets (tokens, password hashes, keys) never
appear in a return value.

The terminal menu keeps its own interactive flows; these reuse the same
building blocks (state lock, nginx transaction, Cloudflare client).
"""

import contextlib
import datetime
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import access, cloudflare, credentials_cli, monitor, nginx
from .backend import backend_test
from .common import normalize_hostname, validate_ip, validate_port
from .state import load_state, save_state, state_lock

BACKUP_DIR = Path("/var/backups/lnmt")
CERTS_DIR = Path("/etc/lnmt/certs")
RANGES_FILE = Path("/var/lib/lnmt/cloudflare-ranges.json")
UNITS = ("lnmt-ddns.timer", "lnmt-edge.timer", "lnmt-backup.timer")


class ServiceError(RuntimeError):
    pass


@contextlib.contextmanager
def captured():
    """Collect what the reused terminal code prints (it never goes to the
    helper's stdout, which carries the JSON answer)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def _tail(text, lines=12):
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _run(argv, timeout=120):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def _proxy(state, hostname):
    proxy = state["proxies"].get(hostname)
    if proxy is None:
        raise ServiceError(f"{hostname} is not managed here")
    return proxy


# --- reading ------------------------------------------------------------------------

def cert_expiry(path):
    code, out = _run(["openssl", "x509", "-in", str(path), "-noout", "-enddate"], timeout=10)
    if code != 0 or "=" not in out:
        return None
    try:
        when = datetime.datetime.strptime(out.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return when.replace(tzinfo=datetime.timezone.utc).timestamp()


def _site_summary(hostname, proxy, ddns):
    record = ddns.get("records", {}).get(hostname, {})
    ap = proxy.get("access_protection") or {}
    return {
        "hostname": hostname,
        "zone": proxy.get("zone"),
        "enabled": proxy.get("enabled", True),
        "backend": f"{proxy['backend_protocol']}://{proxy['backend_ip']}:{proxy['backend_port']}",
        "backend_ip": proxy["backend_ip"],
        "backend_port": proxy["backend_port"],
        "backend_protocol": proxy["backend_protocol"],
        "backend_tls_verify": bool(proxy.get("backend_tls_verify", False)),
        "backend_tls_sni": proxy.get("backend_tls_sni", ""),
        "auth_enabled": bool(ap.get("enabled")),
        "auth_realm": ap.get("realm", "Restricted Service"),
        "ddns": {"enabled": record.get("enabled", False), **(record.get("health") or {})},
        "cert_expires": cert_expiry(proxy["certificate"]) if proxy.get("certificate") else None,
        "uptime": monitor.uptime(hostname),
        "options": site_options_public(proxy),
    }


def list_sites():
    state = load_state()
    ddns = state.get("ddns", {})
    return {"sites": [_site_summary(h, p, ddns) for h, p in sorted(state["proxies"].items())],
            "ddns": {k: ddns.get(k) for k in ("public_ip", "status", "message", "last_check")}}


def site_detail(hostname):
    state = load_state()
    proxy = _proxy(state, hostname)
    summary = _site_summary(hostname, proxy, state.get("ddns", {}))
    path = nginx.nginx_path(hostname)
    summary.update(
        config=path.read_text() if path.exists() else "",
        linked=nginx.nginx_enabled_path(hostname).is_symlink(),
        auth_users=access._read_users(access._auth_path(hostname)),
        dns_record_id=proxy.get("cloudflare_record_id", ""),
    )
    return summary


def test_backend(hostname):
    proxy = _proxy(load_state(), hostname)
    ok, message = backend_test(proxy)
    return {"ok": ok, "message": message}


def site_health(hostname):
    """The terminal's full health check, as data: backend, nginx files, DNS
    at Cloudflare, DDNS, certificate."""
    state = load_state()
    proxy = _proxy(state, hostname)
    checks = []

    def add(name, level, detail):
        checks.append({"name": name, "level": level, "detail": detail})

    enabled = proxy.get("enabled", True)
    ok, message = backend_test(proxy)
    add("backend", "ok" if ok else "fail", message)
    path, link = nginx.nginx_path(hostname), nginx.nginx_enabled_path(hostname)
    add("nginx file", "ok" if path.exists() else "fail", str(path))
    if enabled:
        add("enabled link", "ok" if link.is_symlink() else "fail", "present" if link.is_symlink() else "missing")
    else:
        add("enabled link", "warn" if link.is_symlink() else "ok", "present while disabled" if link.is_symlink() else "absent (disabled)")
    ddns = state.get("ddns", {})
    health = (ddns.get("records", {}).get(hostname) or {}).get("health") or {}
    add("ddns", "ok" if health.get("status") == "HEALTHY" else "warn", health.get("message") or "no check yet")
    cred = cloudflare.load_credential(proxy["zone"])
    if not cred:
        add("cloudflare", "warn", f"no credential for {proxy['zone']}")
    else:
        try:
            record = cloudflare.cf_get_record(cred, hostname, record_type="A",
                                              record_id=proxy.get("cloudflare_record_id"))
            if not record:
                add("dns record", "warn", "no A record at Cloudflare")
            else:
                right = record.get("content") == ddns.get("public_ip") and record.get("proxied")
                add("dns record", "ok" if right else "warn",
                    f"A {record.get('content')}, {'proxied' if record.get('proxied') else 'NOT proxied'}")
        except Exception as exc:
            add("cloudflare", "warn", f"API: {exc}")
    expires = cert_expiry(proxy["certificate"])
    if expires is None:
        add("certificate", "fail", "missing or unreadable")
    else:
        days = int((expires - datetime.datetime.now(datetime.timezone.utc).timestamp()) // 86400)
        add("certificate", "ok" if days > 30 else ("warn" if days > 0 else "fail"), f"{days} days left")
    return {"hostname": hostname, "checks": checks}


def system_status():
    checks = []

    def add(name, level, detail):
        checks.append({"name": name, "level": level, "detail": detail})

    ok, out = nginx.nginx_test()
    add("nginx config", "ok" if ok else "fail", _tail(out, 3))
    code, out = _run(["systemctl", "is-active", "nginx"], timeout=10)
    add("nginx service", "ok" if out.strip() == "active" else "fail", out.strip())
    for unit in UNITS:
        code, active = _run(["systemctl", "is-active", unit], timeout=10)
        service = unit.replace(".timer", ".service")
        _, result = _run(["systemctl", "show", service, "--property=Result", "--value"], timeout=10)
        level = "ok" if active.strip() == "active" and result.strip() in ("success", "") else "warn"
        add(unit.replace(".timer", ""), level, f"timer {active.strip()}, last run {result.strip() or 'none yet'}")
    state = load_state()
    ddns = state.get("ddns", {})
    add("ddns", "ok" if ddns.get("status") == "HEALTHY" else "warn", ddns.get("message") or "no run yet")
    code, out = _run(["nft", "list", "set", "inet", "filter", "cloudflare_ipv4"], timeout=10)
    count = len(re.findall(r"\d+\.\d+\.\d+\.\d+/\d+", out)) if code == 0 else 0
    add("cloudflare firewall", "ok" if count >= 10 else "fail", f"{count} IPv4 ranges allowed on 443")
    return {"checks": checks, "public_ip": ddns.get("public_ip"), "last_check": ddns.get("last_check")}


# --- sites --------------------------------------------------------------------------

def add_site(hostname, backend_ip, backend_port, backend_protocol="http", force=False):
    from . import proxy as proxy_mod
    from .tls import cert_covers
    hostname = normalize_hostname(hostname)
    backend_ip = validate_ip(str(backend_ip))
    backend_port = validate_port(str(backend_port))
    if backend_protocol not in ("http", "https"):
        raise ServiceError("protocol must be http or https")
    state = load_state()
    if hostname in state["proxies"]:
        raise ServiceError("That hostname is already managed.")
    if nginx.nginx_path(hostname).exists():
        raise ServiceError("An nginx configuration already exists for this hostname.")
    zone = cloudflare.find_zone_for_hostname(hostname)
    if not zone:
        raise ServiceError(f"No Cloudflare credential manages {hostname}: add one for its zone first.")
    cred = cloudflare.load_credential(zone)
    try:
        if cloudflare.cf_get_record(cred, hostname):
            raise ServiceError(f"A DNS record already exists for {hostname}; it will not be overwritten.")
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(f"Cloudflare API failed: {exc}")
    code, out = _run(["/usr/local/sbin/lnmt-tls", "issue", zone], timeout=180)
    if code != 0:
        raise ServiceError(f"TLS provisioning failed:\n{_tail(out)}")
    cert, key = CERTS_DIR / zone / "origin.pem", CERTS_DIR / zone / "origin.key"
    if not cert.exists() or not key.exists():
        raise ServiceError("The certificate files are missing after provisioning.")
    if not cert_covers(cert, hostname):
        raise ServiceError(f"The {zone} certificate covers {zone} and *.{zone} only, not {hostname}.")
    proxy = {"hostname": hostname, "zone": zone, "backend_ip": backend_ip, "backend_port": backend_port,
             "backend_protocol": backend_protocol, "certificate": str(cert), "certificate_key": str(key),
             "enabled": True}
    ok, message = backend_test(proxy)
    if not ok and not force:
        raise ServiceError(f"The backend does not answer ({message}). Fix it, or deploy anyway.")
    try:
        public_ip = cloudflare.get_public_ip()
    except Exception as exc:
        raise ServiceError(f"Could not find this server's public IP: {exc}")
    with state_lock(), captured() as log:
        try:
            proxy_mod._deploy_new_proxy(hostname, zone, cred, public_ip, proxy)
        except proxy_mod.DeployError:
            raise ServiceError(f"Deployment failed and was rolled back:\n{_tail(log.getvalue())}")
    return {"hostname": hostname, "backend_warning": "" if ok else message}


def remove_site(hostname):
    from . import proxy as proxy_mod
    with state_lock(), captured() as log:
        _proxy(load_state(), hostname)
        proxy_mod._remove_proxy(hostname)
        if hostname in load_state()["proxies"]:
            raise ServiceError(f"Not removed:\n{_tail(log.getvalue())}")
    import shutil
    shutil.rmtree(nginx.PAGES / hostname, ignore_errors=True)  # its maintenance page
    (nginx.SITE_LOGS / f"{hostname}.log").unlink(missing_ok=True)
    return {"removed": hostname}


def set_enabled(hostname, enabled):
    from . import proxy as proxy_mod
    _proxy(load_state(), hostname)
    try:
        proxy_mod.set_proxy_enabled(hostname, bool(enabled))
    except RuntimeError as exc:
        raise ServiceError(f"nginx refused the change; nothing changed:\n{_tail(str(exc))}")
    return {"hostname": hostname, "enabled": bool(enabled)}


def update_backend(hostname, backend_ip=None, backend_port=None, backend_protocol=None):
    from . import proxy as proxy_mod
    _proxy(load_state(), hostname)
    try:
        with captured():
            message = proxy_mod.update_proxy_backend(
                hostname,
                backend_ip=validate_ip(str(backend_ip)) if backend_ip else None,
                backend_port=validate_port(str(backend_port)) if backend_port else None,
                backend_protocol=backend_protocol or None)
    except (RuntimeError, ValueError) as exc:
        raise ServiceError(str(exc))
    return {"hostname": hostname, "message": message}


def update_backend_tls(hostname, verify, sni=""):
    from . import proxy as proxy_mod
    proxy = _proxy(load_state(), hostname)
    if proxy["backend_protocol"] != "https":
        raise ServiceError("Backend TLS settings apply to HTTPS backends only.")
    sni = (sni or "").strip()
    if sni:
        sni = normalize_hostname(sni)
    try:
        with captured():
            message = proxy_mod.update_proxy_backend_tls(hostname, bool(verify), sni)
    except (RuntimeError, ValueError) as exc:
        raise ServiceError(str(exc))
    return {"hostname": hostname, "message": message}


# --- site options (redirects, rate limit, headers, error and maintenance pages) ----------

REDIRECT_PATH = re.compile(r"^/[A-Za-z0-9._~/%-]{0,200}$")
REDIRECT_TO = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?)?/[A-Za-z0-9._~/%?&=+-]{0,300}$|^https://[A-Za-z0-9.-]+$")
DEFAULT_MAINTENANCE = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Down for maintenance</title>"
                       "<style>body{font-family:system-ui,sans-serif;max-width:36em;margin:18vh auto;padding:0 1em;color:#333}"
                       "</style></head><body><h1>Down for maintenance</h1><p>We're working on this site and will be back "
                       "shortly.</p></body></html>\n")


def site_options_public(proxy):
    options = proxy.get("options") or {}
    page = nginx.PAGES / proxy["hostname"] / "maintenance.html" if proxy.get("hostname") else None
    return {"redirects": options.get("redirects", []), "rate_limit": int(options.get("rate_limit") or 0),
            "security_headers": bool(options.get("security_headers")),
            "friendly_errors": bool(options.get("friendly_errors")),
            "maintenance": bool(options.get("maintenance")),
            "maintenance_page": page.read_text() if page and page.exists() else DEFAULT_MAINTENANCE,
            "rate_limits": list(nginx.RATE_LIMITS)}


def site_options(hostname, redirects=None, rate_limit=None, security_headers=None, friendly_errors=None,
                 maintenance=None, maintenance_page=None):
    """Change a site's options; nginx is tested before reloading and the old
    file comes back on any failure."""
    from .state import load_state as load, save_state as save
    with state_lock():
        state = load()
        proxy = _proxy(state, hostname)
        options = dict(proxy.get("options") or {})
        if redirects is not None:
            clean = []
            for item in list(redirects)[:50]:
                source, to, code = str(item.get("from", "")).strip(), str(item.get("to", "")).strip(), int(item.get("code") or 301)
                if not REDIRECT_PATH.match(source) or not REDIRECT_TO.match(to) or code not in (301, 302, 307, 308):
                    raise ServiceError(f"Bad redirect {source[:40]} -> {to[:60]}: from a path like /old, to a path or an https:// address.")
                if source == "/":
                    raise ServiceError("Redirecting / would hide the whole site; use the target site instead.")
                clean.append({"from": source, "to": to, "code": code})
            options["redirects"] = clean
        if rate_limit is not None:
            rate_limit = int(rate_limit or 0)
            if rate_limit not in (0, *nginx.RATE_LIMITS):
                raise ServiceError(f"Rate limit must be off or one of {', '.join(map(str, nginx.RATE_LIMITS))} per second.")
            options["rate_limit"] = rate_limit
        for key, value in (("security_headers", security_headers), ("friendly_errors", friendly_errors),
                           ("maintenance", maintenance)):
            if value is not None:
                options[key] = bool(value)
        if maintenance_page is not None:
            if len(maintenance_page) > 50000:
                raise ServiceError("The maintenance page is limited to 50 KB.")
            folder = nginx.PAGES / hostname
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "maintenance.html").write_text(maintenance_page or DEFAULT_MAINTENANCE)
            os.chmod(folder / "maintenance.html", 0o644)
        elif options.get("maintenance") and not (nginx.PAGES / hostname / "maintenance.html").exists():
            (nginx.PAGES / hostname).mkdir(parents=True, exist_ok=True)
            (nginx.PAGES / hostname / "maintenance.html").write_text(DEFAULT_MAINTENANCE)
        new_proxy = dict(proxy, options=options, hostname=hostname)
        nginx.ensure_common()
        try:
            nginx.install_nginx_config(new_proxy)
        except RuntimeError as exc:
            raise ServiceError(f"nginx refused the change; nothing changed:\n{_tail(str(exc))}")
        state["proxies"][hostname] = new_proxy
        save(state)
    return {"hostname": hostname, "options": site_options_public(dict(new_proxy, hostname=hostname))}


def sites_refresh():
    """Re-render every managed site (new shared settings, e.g. request counting)."""
    nginx.ensure_common()
    done, failed = [], []
    with state_lock():
        for hostname, proxy in load_state()["proxies"].items():
            try:
                nginx.install_nginx_config(dict(proxy, hostname=hostname))
                done.append(hostname)
            except RuntimeError as exc:
                failed.append(f"{hostname}: {_tail(str(exc), 3)}")
    return {"refreshed": done, "failed": failed}


def site_traffic(hostname):
    _proxy(load_state(), hostname)
    return monitor.traffic(hostname)


# --- Basic Auth ---------------------------------------------------------------------

def auth_set(hostname, enabled=None, realm=None):
    path = access._auth_path(hostname)
    with state_lock():
        state = load_state()
        proxy = _proxy(state, hostname)
        if access._migrate_legacy(hostname, proxy):
            save_state(state)
        users = access._read_users(path)
    if enabled and not users:
        raise ServiceError("Add a user before turning protection on.")
    if realm is not None:
        realm = str(realm).strip().replace('"', "").replace("$", "").replace("\\", "")[:80]
        if not realm:
            raise ServiceError("The login prompt cannot be empty.")

    def change(p):
        current = p.setdefault("access_protection", {})
        current.update({"auth_file": str(path),
                        "realm": realm if realm is not None else current.get("realm", "Restricted Service"),
                        "enabled": bool(enabled) if enabled is not None else current.get("enabled", False)})

    try:
        access._commit(hostname, change)
    except RuntimeError as exc:
        raise ServiceError(f"nginx refused the change; nothing changed:\n{_tail(str(exc))}")
    return site_detail(hostname)


def auth_user_set(hostname, username, password):
    if not access.USER_RE.match(username or ""):
        raise ServiceError("User names: letters, digits, _ . @ - (up to 64).")
    if not password or len(password) < 8:
        raise ServiceError("Passwords need at least 8 characters.")
    _proxy(load_state(), hostname)
    with state_lock():
        access._write_user(access._auth_path(hostname), username, password)
    return {"users": access._read_users(access._auth_path(hostname))}


def auth_user_remove(hostname, username):
    path = access._auth_path(hostname)
    with state_lock():
        proxy = _proxy(load_state(), hostname)
        users = access._read_users(path)
        if username not in users:
            raise ServiceError("No such user.")
        if (proxy.get("access_protection") or {}).get("enabled") and len(users) == 1:
            raise ServiceError("That is the last user: turn protection off first, or add another user.")
        lines = [ln for ln in path.read_text().splitlines() if not ln.startswith(username + ":")]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
        access._secure_auth_file(tmp)
        os.replace(tmp, path)
        access._secure_auth_file(path)
    return {"users": access._read_users(path)}


# --- Cloudflare credentials ---------------------------------------------------------

def list_credentials():
    state = load_state()
    used = {}
    for hostname, proxy in state["proxies"].items():
        used.setdefault(proxy.get("zone"), []).append(hostname)
    items = []
    for path in sorted(cloudflare.credential_files()):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        zone = data.get("zone") or path.stem
        items.append({"zone": zone, "zone_id": data.get("zone_id", ""), "sites": sorted(used.get(zone, [])),
                      "owner": data.get("owner", "")})
    return {"credentials": items}


def zone_owner(hostname):
    """(zone, owner) of the credential that manages hostname; owner "" = the administrators'."""
    zone = cloudflare.find_zone_for_hostname(normalize_hostname(hostname))
    if not zone:
        return None, ""
    return zone, (cloudflare.load_credential(zone) or {}).get("owner", "")


def add_credential(zone, token, owner=""):
    zone = credentials_cli.safe_zone(zone)
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", token):
        raise ServiceError("That does not look like a Cloudflare API token.")
    existing = cloudflare.load_credential(zone)
    if existing and existing.get("owner", "") != (owner or ""):
        raise ServiceError(f"{zone} is already managed here by someone else.")  # before asking Cloudflare
    try:
        zone_id = credentials_cli.find_zone(token, zone)
        credentials_cli.test_dns(token, zone_id)
    except Exception as exc:
        raise ServiceError(f"Cloudflare refused the token for {zone}: {exc}")
    credentials_cli.credential_path(zone).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = cloudflare.load_credential(zone)
    if existing and existing.get("owner", "") != (owner or ""):
        raise ServiceError(f"{zone} is already managed here by someone else.")
    credentials_cli.save_credential(zone, token, zone_id)
    if owner:  # a customer's own zone: only they (and administrators) use it
        path = credentials_cli.credential_path(zone)
        data = json.loads(path.read_text())
        data["owner"] = owner
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
    return {"zone": zone, "zone_id": zone_id, "owner": owner}


def test_credential(zone, owner=None):
    zone = credentials_cli.safe_zone(zone)
    if owner is not None and (cloudflare.load_credential(zone) or {}).get("owner", "") != owner:
        raise ServiceError("No such domain.")
    with captured() as log:
        ok = credentials_cli.verify(zone)
    return {"zone": zone, "ok": ok, "message": _tail(log.getvalue(), 2)}


def remove_credential(zone, owner=None):
    zone = credentials_cli.safe_zone(zone)
    if owner is not None and (cloudflare.load_credential(zone) or {}).get("owner", "") != owner:
        raise ServiceError("No such domain.")
    users = [h for h, p in load_state()["proxies"].items() if p.get("zone") == zone]
    if users:
        raise ServiceError(f"Still used by {', '.join(sorted(users))}: remove those sites first.")
    path = credentials_cli.credential_path(zone)
    if not path.exists():
        raise ServiceError("No such credential.")
    path.unlink()
    return {"removed": zone}


# --- a domain's own page: its sites and its DNS records ------------------------------------

RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX")
# Technical records: Cloudflare takes them as structured data. Each field:
# (key, label, kind, *limits) with kind int (min, max), host, text (max
# length), hex, b64 or choice (options). Cloudflare checks the rest.
U8, U16, U32 = (0, 255), (0, 65535), (0, 4294967295)
TECH_TYPES = {
    "SRV": [("priority", "Priority", "int", *U16), ("weight", "Weight", "int", *U16), ("port", "Port", "int", *U16),
            ("target", "Target host", "host")],
    "CAA": [("flags", "Flags", "int", *U8), ("tag", "Tag", "choice", ("issue", "issuewild", "iodef")),
            ("value", "Value", "text", 255)],
    "NS": [("content", "Name server", "host")],
    "PTR": [("content", "Points to", "host")],
    "HTTPS": [("priority", "Priority", "int", *U16), ("target", "Target", "host_or_dot"), ("value", "Parameters", "text", 1000)],
    "SVCB": [("priority", "Priority", "int", *U16), ("target", "Target", "host_or_dot"), ("value", "Parameters", "text", 1000)],
    "TLSA": [("usage", "Usage", "int", *U8), ("selector", "Selector", "int", *U8),
             ("matching_type", "Matching type", "int", *U8), ("certificate", "Certificate data (hex)", "hex")],
    "SMIMEA": [("usage", "Usage", "int", *U8), ("selector", "Selector", "int", *U8),
               ("matching_type", "Matching type", "int", *U8), ("certificate", "Certificate data (hex)", "hex")],
    "SSHFP": [("algorithm", "Algorithm", "int", *U8), ("type", "Fingerprint type", "int", *U8),
              ("fingerprint", "Fingerprint (hex)", "hex")],
    "NAPTR": [("order", "Order", "int", *U16), ("preference", "Preference", "int", *U16),
              ("flags", "Flags", "text", 16), ("service", "Service", "text", 64), ("regex", "Regex", "text", 255),
              ("replacement", "Replacement", "host_or_dot")],
    "URI": [("priority", "Priority", "int", *U16), ("weight", "Weight", "int", *U16), ("target", "Target URI", "text", 255)],
    "CERT": [("type", "Type", "int", *U16), ("key_tag", "Key tag", "int", *U16), ("algorithm", "Algorithm", "int", *U8),
             ("certificate", "Certificate (base64)", "b64")],
    "DS": [("key_tag", "Key tag", "int", *U16), ("algorithm", "Algorithm", "int", *U8),
           ("digest_type", "Digest type", "int", *U8), ("digest", "Digest (hex)", "hex")],
    "DNSKEY": [("flags", "Flags", "int", *U16), ("protocol", "Protocol", "int", *U8), ("algorithm", "Algorithm", "int", *U8),
               ("public_key", "Public key (base64)", "b64")],
    "OPENPGPKEY": [("content", "Key (base64)", "b64")],
    "LOC": [("lat_degrees", "Latitude °", "int", 0, 90), ("lat_minutes", "′", "int", 0, 59), ("lat_seconds", "″", "float", 0, 59.999),
            ("lat_direction", "N/S", "choice", ("N", "S")),
            ("long_degrees", "Longitude °", "int", 0, 180), ("long_minutes", "′", "int", 0, 59), ("long_seconds", "″", "float", 0, 59.999),
            ("long_direction", "E/W", "choice", ("E", "W")),
            ("altitude", "Altitude (m)", "float", -100000, 42849672.95), ("size", "Size (m)", "float", 0, 90000000),
            ("precision_horz", "Horizontal precision (m)", "float", 0, 90000000),
            ("precision_vert", "Vertical precision (m)", "float", 0, 90000000)],
}
# Records that would hand the domain (or a part of it) elsewhere: never at the
# domain itself, never on a site of this server.
DELEGATING = ("NS", "DS", "DNSKEY")


def _tech_value(field, raw):
    key, label, kind, *limits = field
    text = str(raw if raw is not None else "").strip()
    if kind in ("int", "float"):
        try:
            value = int(text) if kind == "int" else float(text)
        except ValueError:
            raise ServiceError(f"{label}: a number, please.")
        if not limits[0] <= value <= limits[1]:
            raise ServiceError(f"{label}: between {limits[0]} and {limits[1]}.")
        return value
    if kind == "choice":
        if text not in limits[0]:
            raise ServiceError(f"{label}: one of {', '.join(limits[0])}.")
        return text
    if kind in ("host", "host_or_dot"):
        text = text.lower().rstrip(".") if text != "." else text
        if not (kind == "host_or_dot" and text == ".") and not DNS_NAME.match(text):
            raise ServiceError(f"{label}: a hostname" + (" (or . for the record's own name)." if kind == "host_or_dot" else "."))
        return text
    if kind == "hex":
        text = text.replace(" ", "").replace(":", "")
        if not re.fullmatch(r"[0-9A-Fa-f]{2,4096}", text):
            raise ServiceError(f"{label}: hexadecimal digits.")
        return text
    if kind == "b64":
        text = text.replace(" ", "").replace("\n", "")
        if not re.fullmatch(r"[A-Za-z0-9+/=]{4,8192}", text):
            raise ServiceError(f"{label}: base64 text.")
        return text
    if not text or len(text) > limits[0] or "\n" in text:
        raise ServiceError(f"{label}: missing or too long.")
    return text
DNS_NAME = re.compile(r"^(\*\.)?([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$")


def _zone_credential(zone, owner=None):
    zone = credentials_cli.safe_zone(zone)
    cred = cloudflare.load_credential(zone)
    if not cred or (owner is not None and cred.get("owner", "") != owner):
        raise ServiceError("No such domain.")
    return zone, cred


def _managed_names(zone):
    """Record names this manager keeps for its sites (changed only through the site)."""
    return {h: p for h, p in load_state()["proxies"].items() if p.get("zone") == zone}


def _is_site_record(record, managed):
    """The address record of one of this server's sites, or a record that would
    hand the site's name elsewhere (its MX, TXT, SRV… records are free)."""
    return record["name"] in managed and record["type"] in ("A", "AAAA", "CNAME", *DELEGATING)


def zone_detail(zone, owner=None):
    """A domain with its sites (its children: any hostname in the zone, whether
    or not the domain itself points here) and its DNS records."""
    zone, cred = _zone_credential(zone, owner)
    state = load_state()
    ddns = state.get("ddns", {})
    sites = [_site_summary(h, p, ddns) for h, p in sorted(state["proxies"].items()) if p.get("zone") == zone]
    managed = _managed_names(zone)
    try:
        result = cloudflare.cf_api(cred["token"], "GET", f"/zones/{cred['zone_id']}/dns_records?per_page=500")
    except RuntimeError as exc:
        raise ServiceError(f"Cloudflare did not list the records: {exc}")
    records = []
    for r in sorted(result.get("result", []), key=lambda r: (r["name"].count("."), r["name"], r["type"])):
        site = r["name"] if _is_site_record(r, managed) else ""
        records.append({"id": r["id"], "type": r["type"], "name": r["name"], "content": r.get("content", ""),
                        "proxied": bool(r.get("proxied")), "proxiable": bool(r.get("proxiable")),
                        "ttl": r.get("ttl", 1), "priority": r.get("priority"), "comment": r.get("comment") or "",
                        "data": r.get("data") or {}, "technical": r["type"] not in RECORD_TYPES,
                        "site": site})
    return {"zone": zone, "owner": cred.get("owner", ""), "sites": sites, "records": records,
            "record_types": list(RECORD_TYPES),
            "technical_types": {t: [{"key": f[0], "label": f[1], "kind": f[2], "limits": list(f[3:])} for f in fields]
                                for t, fields in TECH_TYPES.items()}}


def _clean_record(zone, rtype, name, content, ttl, proxied, priority, data=None):
    rtype = str(rtype or "").upper()
    if rtype not in RECORD_TYPES and rtype not in TECH_TYPES:
        raise ServiceError(f"Unknown record type {rtype[:12]}.")
    name = str(name or "").strip().lower().rstrip(".")
    if name in ("", "@"):
        name = zone
    elif name != zone and not name.endswith("." + zone):
        name = f"{name}.{zone}"
    if not DNS_NAME.match(name) or len(name) > 253:
        raise ServiceError("That is not a valid name: use @ for the domain itself, or a name like www or _sip._tcp.")
    if rtype in TECH_TYPES:
        if rtype in DELEGATING and name == zone:
            raise ServiceError(f"{rtype} records at the domain itself would take it away from Cloudflare: only below it.")
        if rtype == "NS" and name.startswith("*."):
            raise ServiceError("NS records cannot be wildcards.")
        data = data if isinstance(data, dict) else {}
        values = {field[0]: _tech_value(field, data.get(field[0])) for field in TECH_TYPES[rtype]}
        payload = {"type": rtype, "name": name, "ttl": int(ttl or 1)}
        if "content" in values:
            payload["content"] = values["content"]
        else:
            if rtype == "URI":
                payload["priority"] = values.pop("priority")
            payload["data"] = values
        if payload["ttl"] != 1 and not 60 <= payload["ttl"] <= 86400:
            raise ServiceError("TTL: automatic, or 60-86400 seconds.")
        return payload
    content = str(content or "").strip()
    if rtype in ("A", "AAAA"):
        import ipaddress
        try:
            address = ipaddress.ip_address(content)
        except ValueError:
            raise ServiceError(f"{rtype} records point at an IP address.")
        if (rtype == "A") != (address.version == 4):
            raise ServiceError("A is for IPv4, AAAA for IPv6.")
    elif rtype in ("CNAME", "MX"):
        content = content.lower().rstrip(".")
        if not DNS_NAME.match(content):
            raise ServiceError(f"{rtype} records point at a hostname.")
    elif not content or len(content) > 2048 or "\n" in content:
        raise ServiceError("The record's content is missing or too long.")
    payload = {"type": rtype, "name": name, "content": content, "ttl": int(ttl or 1)}
    if payload["ttl"] != 1 and not 60 <= payload["ttl"] <= 86400:
        raise ServiceError("TTL: automatic, or 60-86400 seconds.")
    if rtype in ("A", "AAAA", "CNAME"):
        payload["proxied"] = bool(proxied)
    if rtype == "MX":
        payload["priority"] = int(priority if priority not in (None, "") else 10)
    return payload


def dns_record_set(zone, record_id="", rtype="", name="", content="", ttl=1, proxied=False, priority=None,
                   owner=None, by="", data=None):
    """Add a DNS record, or change one (record_id). Records of this manager's
    sites are changed through the site, never here."""
    zone, cred = _zone_credential(zone, owner)
    payload = _clean_record(zone, rtype, name, content, ttl, proxied, priority, data)
    managed = _managed_names(zone)
    if _is_site_record(payload, managed):
        raise ServiceError(f"{payload['name']} is a site here: change it on the site's page.")
    payload["comment"] = f"Added in LNMT by {by}"[:100] if by else "Added in LNMT"
    base = f"/zones/{cred['zone_id']}/dns_records"
    try:
        if record_id:
            if not re.fullmatch(r"[0-9a-f]{32}", str(record_id)):
                raise ServiceError("No such record.")
            current = cloudflare.cf_api(cred["token"], "GET", f"{base}/{record_id}")["result"]
            if _is_site_record(current, managed):
                raise ServiceError(f"{current['name']} is a site here: change it on the site's page.")
            result = cloudflare.cf_api(cred["token"], "PUT", f"{base}/{record_id}", payload)
        else:
            result = cloudflare.cf_api(cred["token"], "POST", base, payload)
    except RuntimeError as exc:
        raise ServiceError(f"Cloudflare refused it: {exc}")
    record = result.get("result", {})
    return {"id": record.get("id"), "name": record.get("name"), "type": record.get("type")}


def dns_record_delete(zone, record_id, owner=None):
    zone, cred = _zone_credential(zone, owner)
    if not re.fullmatch(r"[0-9a-f]{32}", str(record_id)):
        raise ServiceError("No such record.")
    base = f"/zones/{cred['zone_id']}/dns_records/{record_id}"
    try:
        current = cloudflare.cf_api(cred["token"], "GET", base)["result"]
        if _is_site_record(current, _managed_names(zone)):
            raise ServiceError(f"{current['name']} is a site here: remove the site instead.")
        cloudflare.cf_api(cred["token"], "DELETE", base)
    except RuntimeError as exc:
        raise ServiceError(f"Cloudflare refused it: {exc}")
    return {"removed": current["name"], "type": current["type"]}


# --- LAIka (the AI build system next door) -----------------------------------------------
#
# /etc/lnmt/laika.json (root-only): LAIka's address, the token LNMT sends
# when calling LAIka, and the SHA-256 of the token LAIka sends when calling
# LNMT. Pairing: an administrator makes a code here (LNMT's address, a token
# for LAIka, this dashboard's certificate fingerprint) and pastes it into
# LAIka, which calls /api/link/hello with a token of its own.

LAIKA_FILE = Path("/etc/lnmt/laika.json")
LAIKA_LINKS = Path("/var/lib/lnmt/laika-links.json")
DASHBOARD_CERT = Path("/etc/nginx/lnmt-admin/admin.pem")
LINK_TOKEN = re.compile(r"^lnmtlink_[A-Za-z0-9_-]{20,80}$")
# What LNMT may ask LAIka (method, path pattern).
LAIKA_CALLS = (("GET", r"/api/link/service/users"), ("POST", r"/api/link/service/token"),
               ("POST", r"/api/link/service/projects"),
               ("GET", r"/api/link/service/projects/[a-z0-9][a-z0-9-]{0,39}\?for_user=[A-Za-z0-9._-]{2,40}"))


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def _laika():
    try:
        return json.loads(LAIKA_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _laika_save(data):
    LAIKA_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(LAIKA_FILE.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=1)
    os.replace(LAIKA_FILE.with_suffix(".tmp"), LAIKA_FILE)


def laika_status():
    data = _laika()
    return {"connected": bool(data.get("token_out")), "url": data.get("url", ""),
            "connected_at": data.get("connected_at"), "pairing": bool(data.get("pending_sha256"))}


def laika_pair_code(lnmt_url, name="LNMT"):
    """A one-time pairing code to paste into LAIka (Settings → System)."""
    import base64
    import secrets
    import ssl
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", lnmt_url or ""):
        raise ServiceError("Open the dashboard by its address (https://host:port) to make a pairing code.")
    token = "lnmtlink_" + secrets.token_urlsafe(32)
    der = ssl.PEM_cert_to_DER_cert(DASHBOARD_CERT.read_text())
    import hashlib
    data = _laika()
    data.update(pending_sha256=_sha(token), pending_at=time.time(), self_url=lnmt_url)
    _laika_save(data)
    code = json.dumps({"v": 1, "name": name[:40], "url": lnmt_url, "token": token,
                       "cert_sha256": hashlib.sha256(der).hexdigest()})
    return {"code": base64.urlsafe_b64encode(code.encode()).decode().rstrip("=")}


def laika_hello(token, laika_url, laika_token):
    """LAIka finishing a pairing (within 30 minutes of the code)."""
    data = _laika()
    if not data.get("pending_sha256") or time.time() - data.get("pending_at", 0) > 1800 \
            or not LINK_TOKEN.match(token or "") or _sha(token) != data["pending_sha256"]:
        raise ServiceError("Unknown or expired pairing code.")
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", laika_url or "") or not LINK_TOKEN.match(laika_token or ""):
        raise ServiceError("Bad pairing answer.")
    _laika_save({"url": laika_url.rstrip("/"), "token_out": laika_token, "inbound_sha256": data["pending_sha256"],
                 "self_url": data.get("self_url", ""), "connected_at": time.time()})
    return {"ok": True}


def laika_check(token):
    """Is this LAIka's token (for its calls to /api/link/…)?"""
    import hmac
    data = _laika()
    return {"ok": bool(data.get("inbound_sha256")) and bool(LINK_TOKEN.match(token or ""))
            and hmac.compare_digest(_sha(token), data["inbound_sha256"])}


def laika_disconnect(token=None):
    if token is not None and not laika_check(token)["ok"]:
        raise ServiceError("Unknown caller.")
    LAIKA_FILE.unlink(missing_ok=True)
    return {"connected": False}


def laika_call(method, path, body=None):
    """A request to LAIka for the dashboard (only the calls in LAIKA_CALLS)."""
    import urllib.error
    import urllib.request
    if not any(method == m and re.fullmatch(p, path) for m, p in LAIKA_CALLS):
        raise ServiceError("That call to LAIka is not allowed.")
    data = _laika()
    if not data.get("token_out"):
        raise ServiceError("LAIka is not connected (System → LAIka).")
    request = urllib.request.Request(data["url"] + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {data['token_out']}",
                                              "Content-Type": "application/json", "User-Agent": "LNMT"})
    try:
        with urllib.request.urlopen(request, timeout=300) as answer:
            return json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except ValueError:
            detail = None
        raise ServiceError(f"LAIka: {detail or f'HTTP {exc.code}'}")
    except (OSError, ValueError) as exc:
        raise ServiceError(f"LAIka did not answer: {exc}")


def _laika_links():
    try:
        return json.loads(LAIKA_LINKS.read_text())
    except (OSError, ValueError):
        return {}


def app_deploy_key(customer, name, public_key="", label="", remove=False):
    from . import hosting
    return _hosting(hosting.call, "app_deploy_key", customer=customer, name=name, key=public_key, label=label,
                    remove=bool(remove))


def laika_build(customer, name, project, title, template, for_user):
    """Build an app with LAIka: a new LAIka project for the person (for_user,
    their LAIka name) whose every approved change is pushed to this app."""
    import secrets
    from . import hosting
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", project or ""):
        raise ServiceError("Project id: lower-case letters, digits and dashes.")
    app = _hosting(hosting.call, "app_status", customer=customer, name=name)
    if not app.get("build"):
        raise ServiceError("Only apps built from git (Dockerfile or WordPress theme) can be built with LAIka.")
    git_url = f"ssh://c-{customer}@{hosting.node()['ip']}/{name}.git"
    if not template:
        # A WordPress app (its own database settings) starts from LAIka's WordPress site template.
        names = _hosting(hosting.call, "app_env_names", customer=customer, name=name)["names"]
        template = "wordpress-theme" if "WORDPRESS_DB_HOST" in names else ""
    made = laika_call("POST", "/api/link/service/projects", {
        "id": project, "name": title or project, "template": template or None, "for_user": for_user,
        "git_url": git_url, "request_id": "lnmt-" + secrets.token_hex(8)})
    app_deploy_key(customer, name, made["public_key"], f"laika-{project}"[:40])
    links = _laika_links()
    links[f"{customer}/{name}"] = {"project": project, "url": made.get("url", ""), "for_user": for_user}
    LAIKA_LINKS.write_text(json.dumps(links, indent=1))
    return {"project": project, "url": made.get("url", ""), "app": name}


def laika_link_record(customer, name, project="", url="", for_user="", remove=False):
    """Remember (or forget) which LAIka project deploys an app."""
    links = _laika_links()
    if remove:
        links.pop(f"{customer}/{name}", None)
    else:
        links[f"{customer}/{name}"] = {"project": project, "url": url, "for_user": for_user}
    LAIKA_LINKS.write_text(json.dumps(links, indent=1))
    return links.get(f"{customer}/{name}")


def laika_app(customer, name):
    """The LAIka project behind an app, with its latest work."""
    link = _laika_links().get(f"{customer}/{name}")
    if not link:
        return {"linked": False, "connected": laika_status()["connected"]}
    try:
        status = laika_call("GET", f"/api/link/service/projects/{link['project']}?for_user={link['for_user']}")
    except ServiceError as exc:
        status = {"error": str(exc)}
    return {"linked": True, "connected": True, **link, "status": status}


# --- DDNS, edge, certificates, backups, nginx ------------------------------------------

def ddns_run():
    code, out = _run(["/usr/local/sbin/lnmt-ddns"], timeout=120)
    return {"ok": code == 0, "output": _tail(out, 20), **list_sites()["ddns"]}


def edge_status():
    try:
        data = json.loads(RANGES_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    stamp = RANGES_FILE.stat().st_mtime if RANGES_FILE.exists() else None
    return {"ipv4": data.get("ipv4_cidrs", []), "ipv6": data.get("ipv6_cidrs", []), "updated": stamp}


def edge_update():
    code, out = _run(["/usr/local/sbin/lnmt-edge"], timeout=120)
    return {"ok": code == 0, "output": _tail(out, 10), **edge_status()}


def certificates():
    items = []
    for meta in sorted(CERTS_DIR.glob("*/origin.json")):
        try:
            data = json.loads(meta.read_text())
        except (OSError, ValueError):
            data = {}
        items.append({"zone": meta.parent.name, "hostnames": data.get("hostnames", []),
                      "expires": cert_expiry(meta.parent / "origin.pem")})
    return {"certificates": items}


def backups():
    items = []
    for path in sorted(BACKUP_DIR.glob("lnmt-backup-*.tar.gz"), reverse=True):
        stat = path.stat()
        items.append({"name": path.name, "bytes": stat.st_size, "created": stat.st_mtime})
    return {"backups": items}


def backup_now():
    code, out = _run(["/usr/local/sbin/lnmt-backup", "--keep", "14"], timeout=300)
    if code != 0:
        raise ServiceError(f"Backup failed:\n{_tail(out)}")
    return backups()


def backup_verify(name):
    if not re.fullmatch(r"lnmt-backup-\d{8}-\d{6}\.tar\.gz", name or ""):
        raise ServiceError("Unknown backup.")
    path = BACKUP_DIR / name
    if not path.exists():
        raise ServiceError("Unknown backup.")
    code, out = _run(["/usr/local/sbin/lnmt-restore", "--verify", str(path)], timeout=300)
    return {"name": name, "ok": code == 0, "output": _tail(out, 15)}


def alerts_get():
    return monitor.public_notify_config()


def alerts_set(discord_webhook=None, ntfy_url=None, events=None):
    try:
        return monitor.save_notify_config(discord_webhook, ntfy_url, events)
    except ValueError as exc:
        raise ServiceError(str(exc))


def alerts_test():
    sent = monitor.send("Test alert", "Alerts from the proxy manager reach you here.")
    if not sent:
        raise ServiceError("Nothing was delivered: add a Discord webhook or an ntfy address first, or check it.")
    return {"sent": sent}


# --- hosting (apps on the node) -----------------------------------------------------------

def _hosting(function, *args, **kwargs):
    from . import hosting
    try:
        return function(*args, **kwargs)
    except hosting.HostingError as exc:
        raise ServiceError(str(exc))


def hosting_templates():
    from . import hosting
    return hosting.templates()


def hosting_enable(username, disk_gb=10, memory_gb=4, cpus=2, tasks=4096):
    """Turn hosting on, or change a customer's limits (the disk only grows)."""
    from . import hosting
    return _hosting(hosting.call, "customer_ensure", name=hosting.customer_name(username), disk_gb=int(disk_gb),
                    memory_gb=float(memory_gb), cpus=float(cpus), tasks=int(tasks))


def hosting_node():
    from . import hosting
    info = _hosting(hosting.call, "info")
    return {**info, "customers": _hosting(hosting.call, "customers")["customers"]}


def apps_list(customer=None):
    from . import hosting
    return _hosting(hosting.call, "apps", customer=customer)


def app_create(customer, name, template, version="", image="", port=0, env=None, memory_mb=0, cpus=1.0,
               volumes=None, command=""):
    from . import hosting
    return _hosting(hosting.create_app, customer, name, template, version, image, port, env, memory_mb, cpus,
                    volumes, command)


def app_status(customer, name):
    from . import hosting
    app = _hosting(hosting.call, "app_status", customer=customer, name=name)
    app["env_names"] = _hosting(hosting.call, "app_env_names", customer=customer, name=name)["names"]
    node = hosting.node()
    app["node_ip"] = node["ip"]
    backend = f"{node['ip']}:{app.get('host_port')}"
    app["sites"] = [h for h, p in load_state()["proxies"].items()
                    if app.get("host_port") and f"{p['backend_ip']}:{p['backend_port']}" == backend]
    return app


def app_action(customer, name, action):
    from . import hosting
    return _hosting(hosting.call, "app_action", customer=customer, name=name, action=action)


def app_update(customer, name, image=None, env=None, command=None, memory_mb=None, cpus=None, auto_update=None):
    from . import hosting
    return _hosting(hosting.call, "app_update", customer=customer, name=name, image=image, env=env,
                    command=command, memory_mb=memory_mb, cpus=cpus, auto_update=auto_update)


def app_db(customer, name, op, path=""):
    from . import hosting
    if op == "export":
        return _hosting(hosting.call, "app_db_export", customer=customer, name=name)
    if op == "import":
        return _hosting(hosting.call, "app_db_import", customer=customer, name=name, path=path)
    raise ServiceError("Unknown database operation.")


def app_tasks(customer, name, tasks=None, run=""):
    from . import hosting
    if run:
        return _hosting(hosting.call, "app_task_run", customer=customer, name=name, task=run)
    return _hosting(hosting.call, "app_tasks_set", customer=customer, name=name, tasks=list(tasks or []))


def app_usage(customer, name):
    return monitor.app_usage(customer, name)


def my_alerts_get(username):
    return monitor.public_person_config(username)


def my_alerts_set(username, discord_webhook=None, ntfy_url=None, events=None):
    try:
        return monitor.save_person_config(username, discord_webhook, ntfy_url, events)
    except ValueError as exc:
        raise ServiceError(str(exc))


def my_alerts_test(username):
    sent = monitor.send("Test alert", "Your alerts from the proxy manager reach you here.",
                        monitor.person_config(username))
    if not sent:
        raise ServiceError("Nothing was delivered: add a Discord webhook or an ntfy address first, or check it.")
    return {"sent": sent}


def app_logs(customer, name, lines=200):
    from . import hosting
    return _hosting(hosting.call, "app_logs", customer=customer, name=name, lines=int(lines))


def app_build(customer, name):
    from . import hosting
    return _hosting(hosting.call, "app_build", customer=customer, name=name)


def app_build_status(customer, name):
    from . import hosting
    return _hosting(hosting.call, "app_build_status", customer=customer, name=name)


def app_files(customer, name, op, args=None):
    from . import hosting
    return _hosting(hosting.call, "app_files", customer=customer, name=name, op=op, args=args or {})


def hosting_set_keys(customer, keys):
    from . import hosting
    return _hosting(hosting.call, "customer_set_keys", name=customer, keys=list(keys or []))


def hosting_customer(customer):
    from . import hosting
    return _hosting(hosting.call, "customer_info", name=customer)


def app_remove(customer, name, keep_data=False):
    from . import hosting
    status = app_status(customer, name)
    if status["sites"]:
        raise ServiceError(f"Remove its site(s) first: {', '.join(status['sites'])}.")
    return _hosting(hosting.call, "app_remove", customer=customer, name=name, keep_data=bool(keep_data))


def app_publish(customer, name, hostname, owner=None):
    """Serve an app at a hostname: an ordinary proxied site whose backend is the
    app. owner (a member) may only use a zone they added themselves."""
    from . import hosting
    if owner is not None:
        zone, zone_owner_name = zone_owner(hostname)
        if not zone or zone_owner_name != owner:
            raise ServiceError("Use a hostname on one of your own domains (add it under Domains first).")
    app = _hosting(hosting.call, "app_status", customer=customer, name=name)
    if not app.get("host_port"):
        raise ServiceError("This app is private (a database or cache); it cannot be published.")
    return add_site(hostname, hosting.node()["ip"], app["host_port"], "http", force=True)


def nginx_check():
    ok, out = nginx.nginx_test()
    return {"ok": ok, "output": out}


ACTIONS = {
    # name: (function, writes)
    "list_sites": (list_sites, False),
    "site_detail": (site_detail, False),
    "site_health": (site_health, False),
    "test_backend": (test_backend, False),
    "system_status": (system_status, False),
    "add_site": (add_site, True),
    "remove_site": (remove_site, True),
    "set_enabled": (set_enabled, True),
    "update_backend": (update_backend, True),
    "update_backend_tls": (update_backend_tls, True),
    "auth_set": (auth_set, True),
    "auth_user_set": (auth_user_set, True),
    "auth_user_remove": (auth_user_remove, True),
    "list_credentials": (list_credentials, False),
    "add_credential": (add_credential, True),
    "test_credential": (test_credential, False),
    "remove_credential": (remove_credential, True),
    "ddns_run": (ddns_run, True),
    "edge_status": (edge_status, False),
    "edge_update": (edge_update, True),
    "certificates": (certificates, False),
    "backups": (backups, False),
    "backup_now": (backup_now, True),
    "backup_verify": (backup_verify, False),
    "nginx_check": (nginx_check, False),
    "alerts_get": (alerts_get, False),
    "alerts_set": (alerts_set, True),
    "alerts_test": (alerts_test, True),
    "hosting_templates": (hosting_templates, False),
    "hosting_enable": (hosting_enable, True),
    "hosting_node": (hosting_node, False),
    "apps_list": (apps_list, False),
    "app_create": (app_create, True),
    "app_status": (app_status, False),
    "app_action": (app_action, True),
    "app_update": (app_update, True),
    "app_db": (app_db, True),
    "app_tasks": (app_tasks, True),
    "app_usage": (app_usage, False),
    "site_options": (site_options, True),
    "zone_detail": (zone_detail, False),
    "laika_status": (laika_status, False),
    "laika_pair_code": (laika_pair_code, True),
    "laika_hello": (laika_hello, True),
    "laika_check": (laika_check, False),
    "laika_disconnect": (laika_disconnect, True),
    "laika_call": (laika_call, False),
    "laika_build": (laika_build, True),
    "laika_link_record": (laika_link_record, True),
    "laika_app": (laika_app, False),
    "app_deploy_key": (app_deploy_key, True),
    "dns_record_set": (dns_record_set, True),
    "dns_record_delete": (dns_record_delete, True),
    "sites_refresh": (sites_refresh, True),
    "site_traffic": (site_traffic, False),
    "my_alerts_get": (my_alerts_get, False),
    "my_alerts_set": (my_alerts_set, True),
    "my_alerts_test": (my_alerts_test, True),
    "app_logs": (app_logs, False),
    "app_remove": (app_remove, True),
    "app_publish": (app_publish, True),
    "app_build": (app_build, True),
    "app_build_status": (app_build_status, False),
    "app_files": (app_files, True),
    "hosting_set_keys": (hosting_set_keys, True),
    "hosting_customer": (hosting_customer, False),
}
