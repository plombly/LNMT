import os
from pathlib import Path
from .common import run
NGINX_AVAILABLE=Path("/etc/nginx/sites-available")
NGINX_ENABLED=Path("/etc/nginx/sites-enabled")
MARKER="# Managed by LAIka NGINx Management Terminal"
COMMON_CONF = Path("/etc/nginx/conf.d/05-lnmt-common.conf")
SITE_LOGS = Path("/var/log/nginx/lnmt-sites")
PAGES = Path("/etc/nginx/lnmt-pages")
RATE_LIMITS = (5, 20, 100)  # requests per second per visitor, one zone each

# Shared by every site: a tiny log format the monitor counts requests from,
# and one rate-limit zone per allowed rate (keyed by visitor IP and site).
COMMON = MARKER + """ - shared by the managed sites
log_format lnmt_count '$msec $status';
""" + "".join(
    f"limit_req_zone $binary_remote_addr$host zone=lnmt_rl_{rate}:10m rate={rate}r/s;\n" for rate in RATE_LIMITS)


def ensure_common():
    """The shared file, written once; returns True when it changed."""
    SITE_LOGS.mkdir(parents=True, exist_ok=True)
    (PAGES / "_default").mkdir(parents=True, exist_ok=True)
    error_page = PAGES / "_default" / "lnmt-error.html"
    if not error_page.exists():
        error_page.write_text(ERROR_PAGE)
    if COMMON_CONF.exists() and COMMON_CONF.read_text() == COMMON:
        return False
    COMMON_CONF.write_text(COMMON)
    os.chmod(COMMON_CONF, 0o644)
    return True


ERROR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Back soon</title>
<style>body{font-family:system-ui,sans-serif;max-width:36em;margin:18vh auto;padding:0 1em;color:#333}</style>
</head><body><h1>We'll be right back</h1><p>This site is not answering at the moment. Please try again in a few minutes.</p></body></html>
"""

def nginx_test():
    result = run(["nginx", "-t"])
    return result.returncode == 0, (
        result.stdout + result.stderr
    ).strip()


def nginx_reload():
    result = run(["systemctl", "reload", "nginx"])
    return result.returncode == 0, (
        result.stdout + result.stderr
    ).strip()


def nginx_path(hostname):
    return NGINX_AVAILABLE / hostname


def nginx_enabled_path(hostname):
    return NGINX_ENABLED / hostname


def render_nginx(proxy):
    hostname = proxy["hostname"]
    ip = proxy["backend_ip"]
    port = proxy["backend_port"]
    proto = proxy["backend_protocol"]

    cert = proxy["certificate"]
    key = proxy["certificate_key"]

    https_extra = ""
    options = proxy.get("options") or {}

    access_extra = ""
    access = proxy.get("access_protection", {})
    if access.get("enabled"):
        realm = str(access.get("realm", "Restricted Service")).replace(chr(34), "").replace("$", "").replace(chr(92), "")
        auth_file = access.get("auth_file", f"/etc/nginx/lnmt-auth/{hostname}.htpasswd")
        access_extra = (
            f'        auth_basic "{realm}";\n'
            f'        auth_basic_user_file {auth_file};'
        )

    if proto == "https":
        verify = proxy.get(
            "backend_tls_verify",
            False,
        )

        sni = proxy.get(
            "backend_tls_sni",
            "",
        ).strip()

        lines = [
            "        proxy_ssl_server_name on;",
        ]

        if sni:
            lines.append(
                f"        proxy_ssl_name {sni};"
            )

        if verify:
            lines.extend([
                "        proxy_ssl_verify on;",
                "        proxy_ssl_verify_depth 5;",
                "        proxy_ssl_trusted_certificate "
                "/etc/ssl/certs/ca-certificates.crt;",
            ])
        else:
            lines.append(
                "        proxy_ssl_verify off;"
            )

        https_extra = "\n".join(lines)

    # The shared file (log format, rate zones) exists once the dashboard ran;
    # without it a site renders exactly as before.
    common = COMMON_CONF.exists()
    server_extra = (["    access_log /var/log/nginx/access.log;",  # keep the usual log as well
                     f"    access_log {SITE_LOGS}/{hostname}.log lnmt_count;"] if common else [])
    if options.get("security_headers"):
        server_extra += [
            '    add_header Strict-Transport-Security "max-age=31536000" always;',
            '    add_header X-Content-Type-Options "nosniff" always;',
            '    add_header X-Frame-Options "SAMEORIGIN" always;',
            '    add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        ]
    if options.get("friendly_errors") and common:
        server_extra += [
            "    error_page 502 504 /lnmt-error.html;",
            f"    location = /lnmt-error.html {{ root {PAGES}/_default; internal; }}",
        ]
    for redirect in options.get("redirects") or []:
        server_extra.append(f"    location = {redirect['from']} {{ return {int(redirect['code'])} {redirect['to']}; }}")
    rate = int(options.get("rate_limit") or 0)
    rate_extra = (f"        limit_req zone=lnmt_rl_{rate} burst={rate * 2} nodelay;\n        limit_req_status 429;"
                  if rate in RATE_LIMITS and common else "")
    if options.get("maintenance"):
        server_extra += [
            "    error_page 503 /maintenance.html;",
            f"    location = /maintenance.html {{ root {PAGES}/{hostname}; internal; }}",
            "    location / { return 503; }",
        ]
        main_location = ""
    else:
        main_location = "main"
    extra = "\n".join(server_extra)

    head = f"""{MARKER}
# Hostname: {hostname}
# Backend: {proto}://{ip}:{port}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name {hostname};

    ssl_certificate     {cert};
    ssl_certificate_key {key};

    ssl_protocols TLSv1.2 TLSv1.3;

{extra}
"""
    if not main_location:
        return head + "}\n"
    return head + f"""
    location / {{
        proxy_pass {proto}://{ip}:{port};

        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
{https_extra}
{access_extra}
{rate_extra}
    }}
}}
"""


def _write_atomic(path, data, mode=0o644):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install_nginx_config(proxy):
    """Write the proxy's site, link it only if the proxy is enabled, test,
    reload; on any failure put the previous file and link back exactly.

    (v2.1 tested a dot-named candidate that nginx's include glob never
    reads, re-enabled disabled proxies, and left a failing file behind.)"""
    hostname = proxy["hostname"]
    path = nginx_path(hostname)
    link = nginx_enabled_path(hostname)
    want_link = proxy.get("enabled", True)

    content = render_nginx(proxy).encode()
    old_bytes = path.read_bytes() if path.exists() else None
    old_target = os.readlink(link) if link.is_symlink() else None

    def restore():
        if old_bytes is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, old_bytes)
        if link.is_symlink() or link.exists():
            link.unlink()
        if old_target is not None:
            link.symlink_to(old_target)

    _write_atomic(path, content)
    if link.is_symlink() or link.exists():
        link.unlink()
    if want_link:
        link.symlink_to(path)

    ok, output = nginx_test()
    if not ok:
        restore()
        raise RuntimeError(f"Nginx validation failed:\n{output}")

    ok, output = nginx_reload()
    if not ok:
        restore()
        nginx_test()
        nginx_reload()
        raise RuntimeError(f"Nginx reload failed:\n{output}")


# ---------------------------------------------------------------------------
# LNMT default TLS sink
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# LNMT default TLS sink
# ---------------------------------------------------------------------------

LNMT_DEFAULT_SINK = Path("/etc/nginx/conf.d/00-lnmt-default-sink.conf")
LNMT_DEFAULT_SINK_DIR = Path("/etc/nginx/lnmt-sink")
LNMT_DEFAULT_SINK_CERT = LNMT_DEFAULT_SINK_DIR / "sink.pem"
LNMT_DEFAULT_SINK_KEY = LNMT_DEFAULT_SINK_DIR / "sink.key"


def ensure_default_sink():
    """Install/repair LNMT's fail-closed default TLS virtual host."""
    import os
    import subprocess

    LNMT_DEFAULT_SINK_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(LNMT_DEFAULT_SINK_DIR, 0o700)

    if not LNMT_DEFAULT_SINK_CERT.exists() or not LNMT_DEFAULT_SINK_KEY.exists():
        subprocess.run(
            [
                "openssl", "req",
                "-x509",
                "-newkey", "rsa:2048",
                "-nodes",
                "-days", "3650",
                "-subj", "/CN=LNMT Default Sink",
                "-keyout", str(LNMT_DEFAULT_SINK_KEY),
                "-out", str(LNMT_DEFAULT_SINK_CERT),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    os.chmod(LNMT_DEFAULT_SINK_KEY, 0o600)
    os.chmod(LNMT_DEFAULT_SINK_CERT, 0o644)

    config = f"""# Managed by LAIka NGINx Management Terminal
# Default TLS sink: unmatched hostnames must never reach a managed proxy.

server {{
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;

    server_name _;

    ssl_certificate     {LNMT_DEFAULT_SINK_CERT};
    ssl_certificate_key {LNMT_DEFAULT_SINK_KEY};

    ssl_protocols TLSv1.2 TLSv1.3;

    return 444;
}}
"""

    old = None
    existed = LNMT_DEFAULT_SINK.exists()

    if existed:
        old = LNMT_DEFAULT_SINK.read_bytes()

    try:
        LNMT_DEFAULT_SINK.write_text(config)
        os.chmod(LNMT_DEFAULT_SINK, 0o644)

        subprocess.run(
            ["nginx", "-t"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    except Exception:
        if existed:
            LNMT_DEFAULT_SINK.write_bytes(old)
            os.chmod(LNMT_DEFAULT_SINK, 0o644)
        else:
            LNMT_DEFAULT_SINK.unlink(missing_ok=True)

        # Verify that rollback returned Nginx to a valid on-disk state.
        subprocess.run(
            ["nginx", "-t"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        raise

    return LNMT_DEFAULT_SINK
