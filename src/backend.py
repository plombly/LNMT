import socket
from .common import run

def tcp_test(ip, port):
    try:
        with socket.create_connection(
            (ip, port),
            timeout=3,
        ):
            return True
    except OSError:
        return False


def backend_test(proxy):
    ip = proxy["backend_ip"]
    port = proxy["backend_port"]
    proto = proxy["backend_protocol"]

    if not tcp_test(ip, port):
        return False, "TCP connection failed"

    url = f"{proto}://{ip}:{port}/"

    cmd = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "--connect-timeout",
        "4",
        "--max-time",
        "8",
        "-w",
        "%{http_code}",
    ]

    if proto == "https":
        verify = proxy.get(
            "backend_tls_verify",
            False,
        )

        sni = proxy.get(
            "backend_tls_sni",
            "",
        ).strip()

        if not verify:
            cmd.append("-k")

        if sni:
            # Connect to backend IP while using the configured
            # hostname for TLS SNI and certificate validation.
            url = f"https://{sni}:{port}/"

            cmd.extend([
                "--resolve",
                f"{sni}:{port}:{ip}",
            ])

    cmd.append(url)

    result = run(cmd)

    if result.returncode != 0:
        detail = result.stderr.strip()

        if detail:
            return False, detail

        return False, "HTTP request failed"

    code = result.stdout.strip()

    return True, f"HTTP response {code}"
