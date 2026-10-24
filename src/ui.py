import shutil
import subprocess
import sys
from pathlib import Path
from .state import STATE_FILE, load_state
from .common import pause, require_root
from .cloudflare import CREDS_DIR, add_credential, credential_files
from .nginx import nginx_test
from .proxy import add_proxy, list_proxies, select_proxy, manage_proxy, remove_proxy
from .health import system_status

def ddns_menu():
    while True:
        state = load_state()

        print()
        print("DDNS")
        print("----")
        print(
            f"Overall: "
            f"{state['ddns'].get('status', 'UNKNOWN')}"
        )

        if state["ddns"].get("public_ip"):
            print(
                f"WAN IP:  "
                f"{state['ddns']['public_ip']}"
            )

        print()
        print("1) Show managed records")
        print("2) Run DDNS now")
        print("0) Back")
        print()

        choice = input("Selection: ").strip()

        if choice == "1":
            print()

            for hostname, record in sorted(
                state["ddns"]["records"].items()
            ):
                health = record.get(
                    "health",
                    {},
                )

                print(
                    f"{hostname:<40} "
                    f"{health.get('status', 'UNKNOWN')}"
                )

            pause()

        elif choice == "2":
            print()
            result = subprocess.run(
                ["/usr/local/sbin/lnmt-ddns"],
                text=True,
            )

            print(
                "\nDDNS completed with exit code "
                f"{result.returncode}."
            )
            pause()

        elif choice == "0":
            return


def credentials_menu():
    while True:
        print()
        print("Cloudflare Credentials")
        print("----------------------")
        print("1) Add credential")
        print("2) List credentials")
        print("3) Open full credential manager")
        print("0) Back")
        print()

        choice = input("Selection: ").strip()

        if choice == "1":
            add_credential()
            pause()

        elif choice == "2":
            print()

            files = credential_files()

            if not files:
                print("No credentials stored.")
            else:
                for path in files:
                    print(path.stem)

            pause()

        elif choice == "3":
            subprocess.run(
                ["/usr/local/sbin/lnmt-cloudflare"]
            )

        elif choice == "0":
            return




def startup_checks():
    required = [
        "nginx",
        "curl",
        "systemctl",
    ]

    missing = [
        item
        for item in required
        if shutil.which(item) is None
    ]

    if missing:
        sys.exit(
            "Missing required commands: "
            + ", ".join(missing)
        )

    if not STATE_FILE.exists():
        sys.exit(
            f"Missing state file: {STATE_FILE}"
        )

    if not CREDS_DIR.exists():
        sys.exit(
            f"Missing credential directory: {CREDS_DIR}"
        )


def main():
    require_root()
    startup_checks()

    while True:
        print()
        print("========================================")
        print("          LAIka NGINx Management Terminal")
        print("========================================")
        print()
        print("1) Add proxy")
        print("2) Manage proxy")
        print("3) Remove proxy")
        print("4) List proxies")
        print("5) System status")
        print("6) Cloudflare credentials")
        print("7) DDNS status / run now")
        print("8) Test Nginx")
        print("9) Exit")
        print()

        choice = input("Selection: ").strip()

        if choice == "1":
            add_proxy()

        elif choice == "2":
            manage_proxy()

        elif choice == "3":
            state = load_state()
            hostname = select_proxy(state)

            if hostname:
                remove_proxy(hostname)

            pause()

        elif choice == "4":
            list_proxies()

        elif choice == "5":
            system_status()

        elif choice == "6":
            credentials_menu()

        elif choice == "7":
            ddns_menu()

        elif choice == "8":
            ok, output = nginx_test()

            print()
            print(output)
            print()
            print(
                "NGINX CONFIGURATION: "
                + ("OK" if ok else "FAILED")
            )

            pause()

        elif choice == "9":
            return

        else:
            print("Invalid selection.")
