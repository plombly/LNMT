#!/bin/bash
# Install LNMT on a fresh Debian or Ubuntu server (run as root from an
# unpacked copy of this repository). Safe to run again: later runs update
# the code and keep your settings, sites and firewall.
#
#   sudo LNMT_TRUSTED_NETS="192.168.1.0/24 10.8.0.0/24" bash bin/install.sh
#
# LNMT_TRUSTED_NETS: the networks that may use SSH and the dashboard (your
#   LAN and VPN; never the internet). Default: this server's own LAN.
# LNMT_FIREWALL=keep: never write /etc/nftables.conf (you manage it; it
#   needs LNMT's Cloudflare sets, see docs/install.md).
#
# What it sets up: nginx, the nftables firewall (default drop; SSH and the
# dashboard from the trusted networks; HTTPS only from Cloudflare), LNMT's
# commands, units, the dashboard on port 8443 and a setup code to create the
# first administrator.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
say() { printf '\n== %s\n' "$*"; }
die() { printf 'LNMT install: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ -r /etc/os-release ] && . /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) ;;
  *) die "Debian or Ubuntu only (found ${PRETTY_NAME:-unknown})" ;;
esac

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx nftables openssl python3 curl git logrotate ca-certificates iproute2 >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is needed"

say "settings"
install -d -m 700 /etc/lnmt /etc/lnmt/credentials /etc/lnmt/certs
install -d -m 755 /var/lib/lnmt
install -d -m 700 /var/backups/lnmt
if [ -f /etc/lnmt/lnmt.env ]; then
  . /etc/lnmt/lnmt.env
else
  NETS=${LNMT_TRUSTED_NETS:-$(ip -4 -o route show proto kernel scope link | awk '{print $1}' | head -1)}
  [ -n "$NETS" ] || die "could not find this server's LAN: set LNMT_TRUSTED_NETS"
  printf '# Networks that may use SSH and open the dashboard (space separated; your LAN and VPN).\nLNMT_TRUSTED_NETS="%s"\n' "$NETS" > /etc/lnmt/lnmt.env
  chmod 600 /etc/lnmt/lnmt.env
  LNMT_TRUSTED_NETS=$NETS
fi
for net in $LNMT_TRUSTED_NETS; do
  python3 - "$net" <<'PY' || die "LNMT_TRUSTED_NETS: $net is not a private network"
import ipaddress, sys
net = ipaddress.ip_network(sys.argv[1], strict=False)
sys.exit(0 if net.is_private else 1)
PY
done
echo "trusted networks: $LNMT_TRUSTED_NETS"

say "firewall"
if [ "${LNMT_FIREWALL:-}" = keep ]; then
  echo "LNMT_FIREWALL=keep: /etc/nftables.conf left alone"
elif grep -q "cloudflare_ipv4" /etc/nftables.conf 2>/dev/null; then
  echo "LNMT firewall already in place: kept (the Cloudflare ranges are refreshed by lnmt-edge)"
else
  # Never lock out the person installing: an SSH session must come from a
  # trusted network.
  FROM=${SSH_CLIENT:-}; FROM=${FROM%% *}
  if [ -n "$FROM" ]; then
    python3 - "$FROM" $LNMT_TRUSTED_NETS <<'PY' || die "your SSH session comes from $FROM, outside LNMT_TRUSTED_NETS ($LNMT_TRUSTED_NETS): the new firewall would lock you out. Add your network and run again."
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
sys.exit(0 if any(ip in ipaddress.ip_network(n, strict=False) for n in sys.argv[2:]) else 1)
PY
  fi
  # Other tables (Docker, another firewall) would be wiped by this file's
  # "flush ruleset": refuse rather than break them.
  OTHER=$(nft list tables 2>/dev/null | grep -v -x "table inet filter" || true)
  [ -z "$OTHER" ] || die "nftables already has other tables ($(echo $OTHER | tr '\n' ' ')): set up the firewall yourself and run again with LNMT_FIREWALL=keep"
  if [ -s /etc/nftables.conf ] && grep -v '^\s*#' /etc/nftables.conf | grep -Eq '\b(accept|drop|reject|saddr|dport)\b'; then
    die "/etc/nftables.conf already has rules: merge LNMT's (docs/install.md) and run again with LNMT_FIREWALL=keep"
  fi
  RANGES=$(mktemp -d)
  trap 'rm -rf "$RANGES"' EXIT
  curl -fsS --max-time 30 https://www.cloudflare.com/ips-v4 -o "$RANGES/v4" || die "could not fetch Cloudflare's address ranges"
  curl -fsS --max-time 30 https://www.cloudflare.com/ips-v6 -o "$RANGES/v6" || die "could not fetch Cloudflare's address ranges"
  [ -f /etc/nftables.conf ] && cp -a /etc/nftables.conf /etc/nftables.conf.before-lnmt
  LNMT_TRUSTED_NETS="$LNMT_TRUSTED_NETS" python3 - "$RANGES" <<'PY'
import ipaddress, os, sys
base = sys.argv[1]
def ranges(name, version):
    found = [str(ipaddress.ip_network(line.strip())) for line in open(os.path.join(base, name)) if line.strip()]
    assert found and all(ipaddress.ip_network(n).version == version for n in found)
    return ",\n".join(f"            {n}" for n in found)
trusted = os.environ["LNMT_TRUSTED_NETS"].split()
def rules(port, comment):
    return f"        # {comment}\n" + "".join(
        f"        {'ip6' if ipaddress.ip_network(n, strict=False).version == 6 else 'ip'} saddr {n} tcp dport {port} accept\n"
        for n in trusted)
conf = f"""#!/usr/sbin/nft -f
# Written by the LNMT installer. lnmt-edge keeps the Cloudflare sets current.

flush ruleset

table inet filter {{

    set cloudflare_ipv4 {{
        type ipv4_addr
        flags interval
        elements = {{
{ranges("v4", 4)}
        }}
    }}

    set cloudflare_ipv6 {{
        type ipv6_addr
        flags interval
        elements = {{
{ranges("v6", 6)}
        }}
    }}

    chain input {{
        type filter hook input priority 0; policy drop;

        iifname "lo" accept
        ct state established,related accept
        ct state invalid drop
        ip protocol icmp accept
        ip6 nexthdr ipv6-icmp accept

{rules(22, "SSH - LAN/VPN only")}
{rules(8443, "Web dashboard - LAN/VPN only")}
        # Web access from Cloudflare ONLY
        ip saddr @cloudflare_ipv4 tcp dport 443 accept
        ip6 saddr @cloudflare_ipv6 tcp dport 443 accept
    }}

    chain forward {{
        type filter hook forward priority 0; policy drop;
    }}

    chain output {{
        type filter hook output priority 0; policy accept;
    }}
}}
"""
open("/etc/nftables.conf.lnmt-new", "w").write(conf)
PY
  nft -c -f /etc/nftables.conf.lnmt-new || { rm -f /etc/nftables.conf.lnmt-new; die "the new firewall would not load; nothing changed"; }
  chmod 755 /etc/nftables.conf.lnmt-new
  mv /etc/nftables.conf.lnmt-new /etc/nftables.conf
  nft -f /etc/nftables.conf
  systemctl enable --now nftables.service >/dev/null 2>&1
  echo "firewall: default drop; SSH and 8443 from $LNMT_TRUSTED_NETS; 443 from Cloudflare only"
fi

say "nginx"
# The stock "Welcome to nginx" site is not wanted; unmatched names on 443
# go to LNMT's sink, which closes the connection.
[ -L /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
systemctl enable --now nginx >/dev/null 2>&1

say "LNMT"
bash "$HERE/bin/install-web.sh"
python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib"); from lnmt import nginx; nginx.ensure_default_sink()'
nginx -t 2>/dev/null && systemctl reload nginx
systemctl enable --now lnmt-edge.timer >/dev/null 2>&1
/usr/local/sbin/lnmt-edge >/dev/null 2>&1 || echo "note: the first Cloudflare range refresh failed; lnmt-edge.timer retries"

say "done"
if [ -s /var/lib/lnmt-web/users.json ] && python3 -c 'import json,sys; sys.exit(0 if json.load(open("/var/lib/lnmt-web/users.json")).get("users") else 1)' 2>/dev/null; then
  echo "Updated. Dashboard: https://$(hostname -I | awk '{print $1}'):8443/"
else
  echo "Open https://$(hostname -I | awk '{print $1}'):8443/ from your LAN and enter this setup code to create the first administrator:"
  /usr/local/sbin/lnmt-web setup-code
fi
echo "Next: add your Cloudflare token (sudo lnmt, or the dashboard), then your first site."
