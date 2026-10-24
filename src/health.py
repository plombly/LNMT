import subprocess
from pathlib import Path
from .state import load_state
from .common import pause, run
from .cloudflare import load_credential, cf_get_record
from .backend import backend_test
from .nginx import nginx_path, nginx_enabled_path, nginx_test

def full_proxy_health(hostname):
    state = load_state()
    proxy = state["proxies"][hostname]

    print()
    print(f"Health: {hostname}")
    print("-" * (8 + len(hostname)))
    print()

    # Proxy state
    enabled = proxy.get("enabled", True)

    print(
        f"Proxy............. "
        f"{'ENABLED' if enabled else 'DISABLED'}"
    )

    # Backend
    ok, message = backend_test(proxy)

    print(
        f"Backend........... "
        f"{'HEALTHY' if ok else 'FAILED'}"
    )

    print(f"  {message}")

    # Nginx files
    source = nginx_path(hostname)
    link = nginx_enabled_path(hostname)

    if source.exists():
        nginx_file = "HEALTHY"
    else:
        nginx_file = "MISSING"

    print(
        f"Nginx config...... {nginx_file}"
    )

    if enabled:
        link_status = (
            "HEALTHY"
            if link.exists()
            else "MISSING"
        )
    else:
        link_status = (
            "DISABLED"
            if not link.exists()
            else "UNEXPECTEDLY ENABLED"
        )

    print(
        f"Nginx enabled..... {link_status}"
    )

    # Nginx syntax
    ok, output = nginx_test()

    print(
        f"Nginx syntax...... "
        f"{'HEALTHY' if ok else 'FAILED'}"
    )

    if not ok:
        print(f"  {output}")

    # DDNS
    ddns = (
        state["ddns"]["records"]
        .get(hostname, {})
        .get("health", {})
    )

    print(
        f"DDNS.............. "
        f"{ddns.get('status', 'UNKNOWN')}"
    )

    if ddns.get("message"):
        print(f"  {ddns['message']}")

    # Cloudflare credential
    zone = proxy.get("zone")
    cred = load_credential(zone) if zone else None

    print(
        f"Cloudflare token.. "
        f"{'FOUND' if cred else 'MISSING'}"
    )

    # Live Cloudflare DNS check
    if cred:
        try:
            record = cf_get_record(
                cred,
                hostname,
            )

            if record:
                print(
                    "Cloudflare DNS.... HEALTHY"
                )
                print(
                    f"  {record.get('type', '?')} "
                    f"{record.get('name', hostname)} -> "
                    f"{record.get('content', '?')}"
                )
                print(
                    f"  Proxied: "
                    f"{record.get('proxied', False)}"
                )
            else:
                print(
                    "Cloudflare DNS.... MISSING"
                )

        except Exception as e:
            print(
                "Cloudflare DNS.... DEGRADED"
            )
            print(f"  {e}")

    else:
        print(
            "Cloudflare DNS.... UNKNOWN"
        )

    # TLS files
    cert = Path(proxy["certificate"])
    key = Path(proxy["certificate_key"])

    if cert.exists() and key.exists():
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

        if result.returncode == 0:
            print("TLS............... HEALTHY")
        else:
            print(
                "TLS............... EXPIRING/INVALID"
            )

        details = subprocess.run(
            [
                "openssl",
                "x509",
                "-in",
                str(cert),
                "-noout",
                "-enddate",
            ],
            capture_output=True,
            text=True,
        )

        if details.stdout.strip():
            print(f"  {details.stdout.strip()}")

    else:
        print("TLS............... MISSING")

    pause()


def system_status():
    state = load_state()

    print()
    print("========================================")
    print("        LNMT System Status")
    print("========================================")
    print()

    overall = "HEALTHY"

    def worsen(status):
        nonlocal overall

        if status == "FAILED":
            overall = "FAILED"
        elif (
            status == "DEGRADED"
            and overall == "HEALTHY"
        ):
            overall = "DEGRADED"

    # --------------------------------------------------------
    # System health
    # --------------------------------------------------------

    print("System")
    print("------")

    # Nginx configuration
    nginx_ok, nginx_output = nginx_test()

    nginx_config_status = (
        "HEALTHY"
        if nginx_ok
        else "FAILED"
    )

    print(
        f"Nginx config....... "
        f"{nginx_config_status}"
    )

    if not nginx_ok:
        worsen("FAILED")

    # Nginx service
    result = run([
        "systemctl",
        "is-active",
        "nginx",
    ])

    nginx_service = (
        "HEALTHY"
        if result.returncode == 0
        else "FAILED"
    )

    print(
        f"Nginx service...... "
        f"{nginx_service}"
    )

    if nginx_service == "FAILED":
        worsen("FAILED")

    # DDNS timer
    result = run([
        "systemctl",
        "is-active",
        "lnmt-ddns.timer",
    ])

    timer_status = (
        "HEALTHY"
        if result.returncode == 0
        else "FAILED"
    )

    print(
        f"DDNS timer......... "
        f"{timer_status}"
    )

    if timer_status == "FAILED":
        worsen("DEGRADED")

    # Last DDNS service result.
    result = run([
        "systemctl",
        "show",
        "lnmt-ddns.service",
        "--property=Result",
        "--value",
    ])

    service_result = (
        result.stdout.strip()
        if result.returncode == 0
        else "unknown"
    )

    if service_result in ("success", ""):
        ddns_service_status = "HEALTHY"
    else:
        ddns_service_status = "DEGRADED"
        worsen("DEGRADED")

    print(
        f"Last DDNS run...... "
        f"{ddns_service_status}"
    )

    # Cloudflare edge-trust updater timer (v2.1 release candidate).
    result = run([
        "systemctl",
        "is-active",
        "lnmt-edge.timer",
    ])
    edge_timer_status = "HEALTHY" if result.returncode == 0 else "DEGRADED"
    print(f"CF edge timer...... {edge_timer_status}")
    if edge_timer_status != "HEALTHY":
        worsen("DEGRADED")

    result = run([
        "systemctl",
        "show",
        "lnmt-edge.service",
        "--property=Result",
        "--value",
    ])
    edge_result = result.stdout.strip() if result.returncode == 0 else "unknown"
    if edge_result in ("success", ""):
        edge_run_status = "HEALTHY"
    elif edge_result == "unknown":
        edge_run_status = "UNKNOWN"
        worsen("DEGRADED")
    else:
        edge_run_status = "DEGRADED"
        worsen("DEGRADED")
    print(f"Last CF edge run... {edge_run_status}")

    # WAN IP from state.
    wan_ip = (
        state.get("ddns", {})
        .get("public_ip")
    )

    print(
        f"WAN IPv4........... "
        f"{wan_ip or 'UNKNOWN'}"
    )

    if not wan_ip:
        worsen("DEGRADED")

    print()
    print("Managed Proxies")
    print("---------------")

    proxies = state.get("proxies", {})

    if not proxies:
        print("No managed proxies.")
        print()
        print(f"Overall............ {overall}")
        pause()
        return

    # --------------------------------------------------------
    # Individual proxies
    # --------------------------------------------------------

    for hostname in sorted(proxies):
        proxy = proxies[hostname]

        print()
        print(hostname)
        print("-" * len(hostname))

        enabled = proxy.get(
            "enabled",
            True,
        )

        print(
            f"Proxy.............. "
            f"{'ENABLED' if enabled else 'DISABLED'}"
        )

        # Disabled intentionally is not a failure.
        if not enabled:
            print(
                "Backend............ SKIPPED"
            )
        else:
            ok, message = backend_test(proxy)

            backend_status = (
                "HEALTHY"
                if ok
                else "FAILED"
            )

            print(
                f"Backend............ "
                f"{backend_status}"
            )

            if not ok:
                print(f"  {message}")
                worsen("FAILED")

        # Nginx site config.
        source = nginx_path(hostname)
        link = nginx_enabled_path(hostname)

        if not source.exists():
            site_status = "FAILED"
            worsen("FAILED")

        elif enabled and not link.exists():
            site_status = "FAILED"
            worsen("FAILED")

        elif (
            not enabled
            and link.exists()
        ):
            site_status = "DEGRADED"
            worsen("DEGRADED")

        else:
            site_status = "HEALTHY"

        print(
            f"Nginx site......... "
            f"{site_status}"
        )

        # DDNS state.
        record_state = (
            state.get("ddns", {})
            .get("records", {})
            .get(hostname, {})
        )

        ddns_health = record_state.get(
            "health",
            {},
        )

        ddns_status = ddns_health.get(
            "status",
            "UNKNOWN",
        )

        print(
            f"DDNS............... "
            f"{ddns_status}"
        )

        if ddns_status in (
            "FAILED",
            "ERROR",
        ):
            worsen("DEGRADED")

        elif ddns_status in (
            "UNKNOWN",
            "DEGRADED",
        ):
            worsen("DEGRADED")

        # Cloudflare credential/API/DNS.
        zone = proxy.get("zone")
        cred = (
            load_credential(zone)
            if zone
            else None
        )

        if not cred:
            print(
                "Cloudflare API..... MISSING"
            )
            print(
                "Cloudflare DNS..... UNKNOWN"
            )

            # Management plane problem only.
            worsen("DEGRADED")

        else:
            try:
                record = cf_get_record(
                    cred,
                    hostname,
                )

                print(
                    "Cloudflare API..... HEALTHY"
                )

                if not record:
                    print(
                        "Cloudflare DNS..... MISSING"
                    )
                    worsen("DEGRADED")

                else:
                    proxied = record.get(
                        "proxied",
                        False,
                    )

                    content = record.get(
                        "content",
                        "",
                    )

                    if (
                        wan_ip
                        and content != wan_ip
                    ):
                        dns_status = "DEGRADED"

                    elif not proxied:
                        dns_status = "DEGRADED"

                    else:
                        dns_status = "HEALTHY"

                    print(
                        f"Cloudflare DNS..... "
                        f"{dns_status}"
                    )

                    print(
                        f"  {content} "
                        f"(proxied="
                        f"{str(proxied).lower()})"
                    )

                    if dns_status != "HEALTHY":
                        worsen("DEGRADED")

            except Exception as e:
                print(
                    "Cloudflare API..... DEGRADED"
                )
                print(
                    "Cloudflare DNS..... UNKNOWN"
                )
                print(f"  {e}")

                # Do NOT mark the site failed merely
                # because management API access failed.
                worsen("DEGRADED")

        # TLS certificate.
        cert_path = proxy.get(
            "certificate",
            "",
        )

        key_path = proxy.get(
            "certificate_key",
            "",
        )

        cert = (
            Path(cert_path)
            if cert_path
            else None
        )

        key = (
            Path(key_path)
            if key_path
            else None
        )

        if (
            not cert
            or not key
            or not cert.exists()
            or not key.exists()
        ):
            print(
                "TLS................ FAILED"
            )
            worsen("FAILED")

        else:
            # First: is it valid for at least 30 days?
            result = run([
                "openssl",
                "x509",
                "-checkend",
                str(30 * 24 * 60 * 60),
                "-noout",
                "-in",
                str(cert),
            ])

            if result.returncode == 0:
                tls_status = "HEALTHY"
            else:
                # Determine whether already expired
                # or merely approaching expiration.
                result_now = run([
                    "openssl",
                    "x509",
                    "-checkend",
                    "0",
                    "-noout",
                    "-in",
                    str(cert),
                ])

                if result_now.returncode == 0:
                    tls_status = "DEGRADED"
                    worsen("DEGRADED")
                else:
                    tls_status = "FAILED"
                    worsen("FAILED")

            print(
                f"TLS................ "
                f"{tls_status}"
            )

            result = run([
                "openssl",
                "x509",
                "-in",
                str(cert),
                "-noout",
                "-enddate",
            ])

            expiry = (
                result.stdout.strip()
                if result.returncode == 0
                else ""
            )

            if expiry:
                print(f"  {expiry}")

        # Backend TLS info.
        if (
            proxy.get(
                "backend_protocol"
            ) == "https"
        ):
            verify = proxy.get(
                "backend_tls_verify",
                False,
            )

            sni = proxy.get(
                "backend_tls_sni",
                "",
            )

            print(
                f"Backend TLS........ "
                f"{'VERIFIED' if verify else 'INTERNAL'}"
            )

            print(
                f"Backend SNI........ "
                f"{sni or '(none)'}"
            )

    print()
    print("=" * 40)
    print(
        f"Overall............ {overall}"
    )
    print("=" * 40)

    if overall == "HEALTHY":
        print(
            "All monitored components are healthy."
        )

    elif overall == "DEGRADED":
        print(
            "Traffic may still be working, but one "
            "or more management/maintenance "
            "components need attention."
        )

    else:
        print(
            "One or more traffic-path components "
            "have failed."
        )

    pause()

