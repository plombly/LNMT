import os
import subprocess
from pathlib import Path
from .state import load_state, save_state, state_lock
from .common import (
    pause, normalize_hostname, validate_ip, validate_port,
)
from .cloudflare import (
    add_credential, load_credential, find_zone_for_hostname,
    cf_get_record, get_public_ip, cf_create_record, cf_delete_record,
)
from .backend import backend_test
from .nginx import (
    nginx_test, nginx_reload, nginx_path, nginx_enabled_path,
    render_nginx, install_nginx_config,
)
from .health import full_proxy_health
from .access import access_menu

def add_proxy():
    state = load_state()

    print("\nAdd Proxy")
    print("---------")

    try:
        hostname = normalize_hostname(
            input("Public hostname: ")
        )

        backend_ip = validate_ip(
            input("Backend IPv4: ")
        )

        backend_port = validate_port(
            input("Backend port: ")
        )

    except Exception as e:
        print(f"\nERROR: {e}")
        pause()
        return

    if hostname in state["proxies"]:
        print("\nERROR: That hostname is already managed.")
        pause()
        return

    if nginx_path(hostname).exists():
        print(
            "\nERROR: An Nginx configuration already "
            "exists for this hostname."
        )
        pause()
        return

    print()
    print("Backend protocol:")
    print("1) HTTP")
    print("2) HTTPS")

    choice = input("Selection [1]: ").strip()

    if choice in ("", "1"):
        protocol = "http"
    elif choice == "2":
        protocol = "https"
    else:
        print("Invalid selection.")
        pause()
        return

    zone = find_zone_for_hostname(hostname)

    if not zone:
        print()
        print(
            f"No stored Cloudflare credential manages "
            f"{hostname}."
        )
        print()
        print("1) Add Cloudflare API token")
        print("2) Cancel")

        if input("Selection: ").strip() != "1":
            return

        if not add_credential():
            pause()
            return

        zone = find_zone_for_hostname(hostname)

        if not zone:
            print(
                "\nThe added credential does not manage "
                "this hostname."
            )
            pause()
            return

    cred = load_credential(zone)

    print()
    print(f"Cloudflare zone......... {zone}")
    print("Cloudflare credential... FOUND")

    try:
        existing_record = cf_get_record(
            cred,
            hostname,
        )
    except Exception as e:
        print(f"Cloudflare API.......... FAILED")
        print(f"\n{e}")
        pause()
        return

    if existing_record:
        print()
        print(
            "ERROR: A Cloudflare DNS record already "
            f"exists for {hostname}."
        )
        print()
        print(
            "For safety, LAIka NGINx Management Terminal will not "
            "overwrite an unmanaged record."
        )
        pause()
        return

    # TLS selection.
    print()
    print(
        f"Using (or provisioning) the Origin CA certificate "
        f"for zone {zone}..."
    )
    print()

    result = subprocess.run(
        [
            "/usr/local/sbin/lnmt-tls",
            "issue",
            zone,
        ],
        text=True,
    )

    if result.returncode != 0:
        print()
        print(
            "TLS provisioning failed. "
            "Deployment stopped."
        )
        print(
            "No DNS or Nginx changes were made."
        )
        pause()
        return

    zone_cert = (
        Path("/etc/lnmt/certs")
        / zone
        / "origin.pem"
    )

    zone_key = (
        Path("/etc/lnmt/certs")
        / zone
        / "origin.key"
    )

    if not zone_cert.exists() or not zone_key.exists():
        print(
            "\nERROR: TLS helper completed but "
            "certificate files are missing."
        )
        pause()
        return

    # Origin CA certificates cover the zone and one label below it
    # (*.zone); a deeper name would fail in Cloudflare's Full (strict) mode.
    from .tls import cert_covers
    if not cert_covers(zone_cert, hostname):
        print(
            f"\nERROR: the {zone} certificate does not cover {hostname} "
            f"(it covers {zone} and *.{zone} only)."
        )
        print("No DNS or Nginx changes were made.")
        pause()
        return

    cert = str(zone_cert)
    key = str(zone_key)

    tls_description = (
        f"Cloudflare Origin CA ({zone})"
    )

    proxy = {
        "hostname": hostname,
        "zone": zone,
        "backend_ip": backend_ip,
        "backend_port": backend_port,
        "backend_protocol": protocol,
        "certificate": cert,
        "certificate_key": key,
        "enabled": True,
    }

    # Warn about backend reuse.
    reused = []

    for other, data in state["proxies"].items():
        if (
            data.get("backend_ip") == backend_ip
            and data.get("backend_port") == backend_port
        ):
            reused.append(other)

    if reused:
        print()
        print("WARNING: Backend destination is reused by:")
        for other in reused:
            print(f"  - {other}")

    print()
    print("Testing backend...")

    backend_ok, backend_message = backend_test(proxy)

    if backend_ok:
        print(f"Backend................. OK ({backend_message})")
    else:
        print(f"Backend................. WARNING ({backend_message})")
        print()
        answer = input(
            "Deploy despite failed backend test? [y/N]: "
        ).strip().lower()

        if answer not in ("y", "yes"):
            print("Cancelled.")
            pause()
            return

    try:
        public_ip = get_public_ip()
    except Exception as e:
        print(f"\nCould not determine WAN IP: {e}")
        pause()
        return

    print()
    print("Proposed deployment")
    print("-------------------")
    print(f"Public:     https://{hostname}")
    print(f"Backend:    {protocol}://{backend_ip}:{backend_port}")
    print(f"Cloudflare: {zone}")
    print(f"DNS:        proxied A -> {public_ip}")
    print(f"DDNS:       enabled")
    print(f"TLS:        {tls_description}")
    print()

    if input("Deploy? [y/N]: ").strip().lower() not in (
        "y",
        "yes",
    ):
        print("Cancelled.")
        return

    with state_lock():
        try:
            _deploy_new_proxy(hostname, zone, cred, public_ip, proxy)
        except DeployError:
            pass  # already reported and rolled back

    pause()


class DeployError(RuntimeError):
    """A new proxy could not be deployed; everything was rolled back."""


def _deploy_new_proxy(hostname, zone, cred, public_ip, proxy):
    """The add transaction, holding the state lock with state read fresh
    (the menu may have been open for minutes; lnmt-ddns writes state too)."""
    state = load_state()
    if hostname in state["proxies"]:
        print(f"\n{hostname} was added meanwhile; nothing was changed.")
        raise DeployError(f"{hostname} is already managed")

    dns_created = False
    nginx_created = False
    record = None

    try:
        print("\nCreating Cloudflare DNS record...")

        result = cf_create_record(
            cred,
            hostname,
            public_ip,
        )

        if not result.get("success"):
            raise RuntimeError(
                "Cloudflare rejected DNS creation."
            )

        record = result["result"]
        dns_created = True

        print("DNS..................... OK")

        state["ddns"]["records"][hostname] = {
            "zone": zone,
            "enabled": True,
            "repair_missing": True,
            "health": {},
        }

        print("DDNS registration....... OK")

        print("Installing Nginx configuration...")

        install_nginx_config(proxy)
        nginx_created = True

        print("Nginx................... OK")

        proxy["cloudflare_record_id"] = record["id"]

        state["proxies"][hostname] = proxy

        save_state(state)

        print("State................... SAVED")

        print()
        print("DEPLOYED")
        print(f"https://{hostname}")

    except Exception as e:
        print()
        print(f"DEPLOYMENT FAILED: {e}")
        print("Rolling back this deployment...")

        # Remove Nginx created by this transaction.
        if nginx_created:
            nginx_enabled_path(hostname).unlink(
                missing_ok=True
            )
            nginx_path(hostname).unlink(
                missing_ok=True
            )

            nginx_test()
            nginx_reload()

        # Remove DDNS state created in memory.
        state["ddns"]["records"].pop(
            hostname,
            None,
        )

        # Remove only the DNS record WE created (by its id).
        if dns_created and record:
            try:
                cf_delete_record(
                    cred,
                    record["id"],
                )

                print("Cloudflare DNS rollback. OK")

            except Exception as rollback_error:
                print(
                    "WARNING: Could not roll back "
                    f"Cloudflare DNS: {rollback_error}"
                )

        print("Existing proxies were not modified.")
        raise DeployError(str(e)) from e


def list_proxies():
    state = load_state()

    print("\nManaged Proxies")
    print("---------------")

    if not state["proxies"]:
        print("No managed proxies.")
        pause()
        return

    for hostname, p in sorted(
        state["proxies"].items()
    ):
        enabled = (
            "ENABLED"
            if p.get("enabled", True)
            else "DISABLED"
        )

        health = (
            state["ddns"]["records"]
            .get(hostname, {})
            .get("health", {})
            .get("status", "UNKNOWN")
        )

        print()
        print(hostname)
        print(
            f"  Backend: "
            f"{p['backend_protocol']}://"
            f"{p['backend_ip']}:"
            f"{p['backend_port']}"
        )
        print(f"  Proxy:   {enabled}")
        print(f"  DDNS:    {health}")

    pause()


def select_proxy(state):
    hosts = sorted(state["proxies"])

    if not hosts:
        print("No managed proxies.")
        return None

    print()

    for i, host in enumerate(hosts, 1):
        print(f"{i}) {host}")

    print()

    try:
        choice = int(
            input("Select proxy: ").strip()
        )

        return hosts[choice - 1]

    except Exception:
        print("Invalid selection.")
        return None


def set_proxy_enabled(hostname, enabled):
    with state_lock():
        _set_proxy_enabled(hostname, enabled)


def _set_proxy_enabled(hostname, enabled):
    state = load_state()
    proxy = state["proxies"][hostname]

    link = nginx_enabled_path(hostname)
    source = nginx_path(hostname)

    if enabled:
        if not source.exists():
            raise RuntimeError(
                "Nginx configuration is missing."
            )

        if not link.exists():
            link.symlink_to(source)

    else:
        link.unlink(missing_ok=True)

    ok, output = nginx_test()

    if not ok:
        if enabled:
            link.unlink(missing_ok=True)
        else:
            if source.exists():
                link.symlink_to(source)

        raise RuntimeError(output)

    ok, output = nginx_reload()

    if not ok:
        # Put the link back as it was, so disk and state agree.
        if enabled:
            link.unlink(missing_ok=True)
        elif source.exists() and not link.is_symlink():
            link.symlink_to(source)
        nginx_test()
        nginx_reload()
        raise RuntimeError(output)

    proxy["enabled"] = enabled
    save_state(state)


def remove_proxy(hostname):
    state = load_state()
    proxy = state["proxies"][hostname]

    print()
    print(f"REMOVE {hostname}")
    print()
    print("This will remove:")
    print("  - Nginx proxy configuration")
    print("  - LNMT DDNS registration")
    print("  - Cloudflare DNS record")
    print()
    print("It will NOT remove:")
    print("  - Cloudflare API credential")
    print("  - Other proxies")
    print("  - Shared certificates")
    print()

    confirm = input(
        f"Type {hostname} to confirm: "
    ).strip().lower()

    if confirm != hostname:
        print("Cancelled.")
        return

    with state_lock():
        _remove_proxy(hostname)


def _remove_proxy(hostname):
    state = load_state()
    proxy = state["proxies"].get(hostname)
    if proxy is None:
        print(f"\n{hostname} is no longer managed; nothing was changed.")
        return

    zone = proxy["zone"]
    cred = load_credential(zone)

    if not cred:
        print()
        print(
            "ERROR: Cloudflare credential is missing."
        )
        print(
            "For safety, removal has been stopped "
            "before changing the site."
        )
        return

    # Verify API access before touching Nginx. Only the A record this
    # manager created (by id) or an A record of the name: never another type.
    try:
        record = cf_get_record(
            cred,
            hostname,
            record_type="A",
            record_id=proxy.get("cloudflare_record_id"),
        )
    except Exception as e:
        print(f"\nCloudflare API failed: {e}")
        print("Nothing was removed.")
        return

    # Remove Nginx first, but retain config content
    # so it can be restored if needed.
    source = nginx_path(hostname)
    link = nginx_enabled_path(hostname)

    backup = None

    if source.exists():
        backup = source.read_text()

    link.unlink(missing_ok=True)
    source.unlink(missing_ok=True)

    ok, output = nginx_test()

    if not ok:
        if backup is not None:
            source.write_text(backup)
            os.chmod(source, 0o644)
            link.symlink_to(source)

        print(
            f"\nNginx validation failed. Restored.\n{output}"
        )
        return

    ok, output = nginx_reload()

    if not ok:
        if backup is not None:
            source.write_text(backup)
            os.chmod(source, 0o644)
            link.symlink_to(source)
            nginx_reload()

        print(
            f"\nNginx reload failed. Restored.\n{output}"
        )
        return

    # Now remove DNS.
    try:
        if record:
            cf_delete_record(
                cred,
                record["id"],
            )

    except Exception as e:
        # Restore Nginx because removal wasn't complete.
        if backup is not None:
            source.write_text(backup)
            os.chmod(source, 0o644)
            link.symlink_to(source)
            nginx_test()
            nginx_reload()

        print()
        print(
            "Cloudflare DNS removal failed."
        )
        print("Nginx proxy was restored.")
        print(e)
        return

    state["ddns"]["records"].pop(
        hostname,
        None,
    )

    state["proxies"].pop(
        hostname,
        None,
    )

    save_state(state)

    print(f"\n{hostname} removed.")


def show_proxy(hostname):
    state = load_state()
    p = state["proxies"][hostname]

    ddns = (
        state["ddns"]["records"]
        .get(hostname, {})
        .get("health", {})
    )

    print()
    print(hostname)
    print("=" * len(hostname))

    print(
        f"Backend: "
        f"{p['backend_protocol']}://"
        f"{p['backend_ip']}:"
        f"{p['backend_port']}"
    )

    print(
        f"Proxy:   "
        f"{'ENABLED' if p.get('enabled', True) else 'DISABLED'}"
    )

    print(
        f"DDNS:    {ddns.get('status', 'UNKNOWN')}"
    )

    if ddns.get("message"):
        print(f"         {ddns['message']}")


def update_proxy_backend(
    hostname,
    backend_ip=None,
    backend_port=None,
    backend_protocol=None,
):
    with state_lock():
        return _update_proxy_backend(hostname=hostname, backend_ip=backend_ip, backend_port=backend_port, backend_protocol=backend_protocol)


def _update_proxy_backend(
    hostname,
    backend_ip=None,
    backend_port=None,
    backend_protocol=None,
):
    """
    Transactionally update a proxy backend.

    The existing Nginx configuration and state are preserved
    unless the candidate configuration passes nginx -t and
    Nginx reloads successfully.
    """

    state = load_state()

    if hostname not in state["proxies"]:
        raise RuntimeError("Proxy does not exist.")

    old_proxy = dict(state["proxies"][hostname])
    new_proxy = dict(old_proxy)

    if backend_ip is not None:
        new_proxy["backend_ip"] = backend_ip

    if backend_port is not None:
        new_proxy["backend_port"] = backend_port

    if backend_protocol is not None:
        new_proxy["backend_protocol"] = backend_protocol

    # Test proposed backend before touching Nginx.
    ok, message = backend_test(new_proxy)

    if not ok:
        raise RuntimeError(
            "Proposed backend failed testing: "
            f"{message}"
        )

    source = nginx_path(hostname)
    link = nginx_enabled_path(hostname)

    old_config = (
        source.read_text()
        if source.exists()
        else None
    )

    was_enabled = link.exists()

    candidate = render_nginx(new_proxy)

    try:
        source.write_text(candidate)
        os.chmod(source, 0o644)

        # Keep disabled proxies disabled while validating.
        # Temporarily enable only if it was previously enabled.
        if was_enabled and not link.exists():
            link.symlink_to(source)

        ok, output = nginx_test()

        if not ok:
            raise RuntimeError(
                "Nginx validation failed:\n"
                f"{output}"
            )

        if was_enabled:
            ok, output = nginx_reload()

            if not ok:
                raise RuntimeError(
                    "Nginx reload failed:\n"
                    f"{output}"
                )

        state["proxies"][hostname] = new_proxy
        save_state(state)

    except Exception:
        # Restore previous configuration.
        if old_config is not None:
            source.write_text(old_config)
            os.chmod(source, 0o644)
        else:
            source.unlink(missing_ok=True)

        if was_enabled:
            if source.exists() and not link.exists():
                link.symlink_to(source)

            nginx_test()
            nginx_reload()
        else:
            link.unlink(missing_ok=True)

        raise

    return message


def change_backend_ip(hostname):
    state = load_state()
    proxy = state["proxies"][hostname]

    print()
    print(f"Current IP: {proxy['backend_ip']}")

    value = input(
        "New backend IPv4 (blank to cancel): "
    ).strip()

    if not value:
        return

    try:
        new_ip = validate_ip(value)
    except Exception as e:
        print(f"\nFAILED: {e}")
        pause()
        return

    if new_ip == proxy["backend_ip"]:
        print("\nNo change.")
        pause()
        return

    try:
        message = update_proxy_backend(
            hostname,
            backend_ip=new_ip,
        )

        print()
        print("Backend IP updated.")
        print(f"Backend test: {message}")

    except Exception as e:
        print()
        print("UPDATE FAILED.")
        print("Existing proxy was preserved.")
        print(e)

    pause()


def change_backend_port(hostname):
    state = load_state()
    proxy = state["proxies"][hostname]

    print()
    print(f"Current port: {proxy['backend_port']}")

    value = input(
        "New backend port (blank to cancel): "
    ).strip()

    if not value:
        return

    try:
        port = int(value)

        if not 1 <= port <= 65535:
            raise ValueError(
                "Port must be between 1 and 65535."
            )

    except Exception as e:
        print(f"\nFAILED: {e}")
        pause()
        return

    if port == proxy["backend_port"]:
        print("\nNo change.")
        pause()
        return

    try:
        message = update_proxy_backend(
            hostname,
            backend_port=port,
        )

        print()
        print("Backend port updated.")
        print(f"Backend test: {message}")

    except Exception as e:
        print()
        print("UPDATE FAILED.")
        print("Existing proxy was preserved.")
        print(e)

    pause()


def change_backend_protocol(hostname):
    state = load_state()
    proxy = state["proxies"][hostname]

    current = proxy["backend_protocol"]

    print()
    print(f"Current protocol: {current}")
    print()
    print("1) HTTP")
    print("2) HTTPS")
    print("0) Cancel")
    print()

    choice = input("Selection: ").strip()

    if choice == "0":
        return

    if choice == "1":
        protocol = "http"

    elif choice == "2":
        protocol = "https"

    else:
        print("\nInvalid selection.")
        pause()
        return

    if protocol == current:
        print("\nNo change.")
        pause()
        return

    try:
        message = update_proxy_backend(
            hostname,
            backend_protocol=protocol,
        )

        print()
        print("Backend protocol updated.")
        print(f"Backend test: {message}")

    except Exception as e:
        print()
        print("UPDATE FAILED.")
        print("Existing proxy was preserved.")
        print(e)

    pause()


def backend_tls_settings(hostname):
    state = load_state()
    proxy = state["proxies"][hostname]

    if proxy["backend_protocol"] != "https":
        print()
        print(
            "Backend TLS settings only apply "
            "to HTTPS backends."
        )
        pause()
        return

    while True:
        state = load_state()
        proxy = state["proxies"][hostname]

        verify = proxy.get(
            "backend_tls_verify",
            False,
        )

        sni = proxy.get(
            "backend_tls_sni",
            "",
        ).strip()

        print()
        print("Backend TLS")
        print("-----------")
        print(
            f"Verification: "
            f"{'VERIFIED' if verify else 'INTERNAL / SELF-SIGNED'}"
        )
        print(
            f"SNI hostname: "
            f"{sni if sni else '(none)'}"
        )
        print()
        print("1) Use verified TLS")
        print("2) Use internal / self-signed TLS")
        print("3) Set SNI hostname")
        print("4) Clear SNI hostname")
        print("5) Test current TLS backend")
        print("0) Back")
        print()

        choice = input("Selection: ").strip()

        if choice == "1":
            candidate = dict(proxy)
            candidate["backend_tls_verify"] = True

            ok, message = backend_test(candidate)

            if not ok:
                print()
                print("VERIFICATION FAILED.")
                print(message)
                print()
                print(
                    "No settings were changed."
                )
                pause()
                continue

            try:
                update_proxy_backend_tls(
                    hostname,
                    verify=True,
                    sni=sni,
                )

                print()
                print("Verified TLS enabled.")

            except Exception as e:
                print()
                print("UPDATE FAILED.")
                print(
                    "Existing proxy was preserved."
                )
                print(e)

            pause()

        elif choice == "2":
            try:
                update_proxy_backend_tls(
                    hostname,
                    verify=False,
                    sni=sni,
                )

                print()
                print(
                    "Internal / self-signed TLS enabled."
                )

            except Exception as e:
                print()
                print("UPDATE FAILED.")
                print(
                    "Existing proxy was preserved."
                )
                print(e)

            pause()

        elif choice == "3":
            value = input(
                "SNI hostname: "
            ).strip().lower().rstrip(".")

            if not value:
                print("\nNo change.")
                pause()
                continue

            try:
                value = normalize_hostname(value)

                update_proxy_backend_tls(
                    hostname,
                    verify=verify,
                    sni=value,
                )

                print()
                print(
                    f"SNI hostname set to {value}."
                )

            except Exception as e:
                print()
                print("UPDATE FAILED.")
                print(
                    "Existing proxy was preserved."
                )
                print(e)

            pause()

        elif choice == "4":
            try:
                update_proxy_backend_tls(
                    hostname,
                    verify=verify,
                    sni="",
                )

                print()
                print("SNI hostname cleared.")

            except Exception as e:
                print()
                print("UPDATE FAILED.")
                print(
                    "Existing proxy was preserved."
                )
                print(e)

            pause()

        elif choice == "5":
            ok, message = backend_test(proxy)

            print()
            print(
                f"{'OK' if ok else 'FAILED'}: "
                f"{message}"
            )

            pause()

        elif choice == "0":
            return

        else:
            print("\nInvalid selection.")


def update_proxy_backend_tls(
    hostname,
    verify,
    sni,
):
    with state_lock():
        return _update_proxy_backend_tls(hostname=hostname, verify=verify, sni=sni)


def _update_proxy_backend_tls(
    hostname,
    verify,
    sni,
):
    state = load_state()

    if hostname not in state["proxies"]:
        raise RuntimeError(
            "Proxy does not exist."
        )

    old_proxy = dict(
        state["proxies"][hostname]
    )

    if old_proxy["backend_protocol"] != "https":
        raise RuntimeError(
            "Backend is not using HTTPS."
        )

    new_proxy = dict(old_proxy)

    new_proxy["backend_tls_verify"] = bool(
        verify
    )

    new_proxy["backend_tls_sni"] = (
        sni.strip().lower().rstrip(".")
        if sni
        else ""
    )

    ok, message = backend_test(new_proxy)

    if not ok:
        raise RuntimeError(
            "Proposed TLS settings failed "
            f"backend testing: {message}"
        )

    source = nginx_path(hostname)
    link = nginx_enabled_path(hostname)

    old_config = (
        source.read_text()
        if source.exists()
        else None
    )

    was_enabled = link.exists()

    try:
        source.write_text(
            render_nginx(new_proxy)
        )
        os.chmod(source, 0o644)

        ok, output = nginx_test()

        if not ok:
            raise RuntimeError(
                "Nginx validation failed:\n"
                f"{output}"
            )

        if was_enabled:
            ok, output = nginx_reload()

            if not ok:
                raise RuntimeError(
                    "Nginx reload failed:\n"
                    f"{output}"
                )

        state["proxies"][hostname] = new_proxy
        save_state(state)

    except Exception:
        if old_config is not None:
            source.write_text(old_config)
            os.chmod(source, 0o644)

        if was_enabled:
            nginx_test()
            nginx_reload()

        raise

    return message




def manage_proxy():
    state = load_state()
    hostname = select_proxy(state)

    if not hostname:
        pause()
        return

    while True:
        state = load_state()

        if hostname not in state["proxies"]:
            return

        show_proxy(hostname)

        print()
        print("1) Change backend IP")
        print("2) Change backend port")
        print("3) Change backend protocol")

        if (
            state["proxies"][hostname]
            ["backend_protocol"] == "https"
        ):
            print("4) Backend TLS settings")
        else:
            print("4) Backend TLS settings [HTTPS only]")

        print("5) Access protection")
        print("6) Test backend")
        print("7) Full health check")
        print("8) Disable proxy")
        print("9) Enable proxy")
        print("10) Show Nginx configuration")
        print("11) Remove proxy")
        print("0) Back")
        print()

        choice = input("Selection: ").strip()

        if choice == "1":
            change_backend_ip(hostname)

        elif choice == "2":
            change_backend_port(hostname)

        elif choice == "3":
            change_backend_protocol(hostname)

        elif choice == "4":
            backend_tls_settings(hostname)

        elif choice == "5":
            access_menu(hostname)

        elif choice == "6":
            ok, message = backend_test(
                state["proxies"][hostname]
            )

            print(
                f"\n{'OK' if ok else 'FAILED'}: "
                f"{message}"
            )

            pause()

        elif choice == "7":
            full_proxy_health(hostname)

        elif choice == "8":
            try:
                set_proxy_enabled(
                    hostname,
                    False,
                )

                print("\nProxy disabled.")

            except Exception as e:
                print(f"\nFAILED: {e}")

            pause()

        elif choice == "9":
            try:
                set_proxy_enabled(
                    hostname,
                    True,
                )

                print("\nProxy enabled.")

            except Exception as e:
                print(f"\nFAILED: {e}")

            pause()

        elif choice == "10":
            config = nginx_path(hostname)

            print()

            if config.exists():
                print(config.read_text())
            else:
                print(
                    "Nginx configuration missing."
                )

            pause()

        elif choice == "11":
            remove_proxy(hostname)
            pause()
            return

        elif choice == "0":
            return

        else:
            print("\nInvalid selection.")
