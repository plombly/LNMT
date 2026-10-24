#!/bin/bash
# Install or update the hosting-node agent (run as root on the node, from an
# unpacked copy of this repository). EDGE_PUBKEY: the edge's node key.
#   EDGE_IP=192.0.2.10 NODE_IP=192.0.2.20 EDGE_PUBKEY="ssh-ed25519 ..." [PREVIEW_NETS="192.0.2.0/24 …"] bash node/install-node.sh
# PREVIEW_NETS: who may open previews of changes (ports 30000-30999): your LAN
# and VPN; default this node's own LAN.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
EDGE_IP=${EDGE_IP:?} NODE_IP=${NODE_IP:?} EDGE_PUBKEY=${EDGE_PUBKEY:?}
PREVIEW_NETS=${PREVIEW_NETS:-$(ip -4 -o route show proto kernel scope link | awk '{print $1}' | head -1)}

install -d -m 755 /usr/local/lib/lnmt_node
install -m 644 "$HERE"/lnmt_node/*.py /usr/local/lib/lnmt_node/
rm -rf /usr/local/lib/lnmt_node/__pycache__
install -m 755 "$HERE/bin/lnmt-node" /usr/local/sbin/lnmt-node
install -m 755 "$HERE/bin/lnmt-customer-shell" /usr/local/sbin/lnmt-customer-shell
grep -qx /usr/local/sbin/lnmt-customer-shell /etc/shells || echo /usr/local/sbin/lnmt-customer-shell >> /etc/shells
install -d -m 755 /etc/lnmt-node
printf '{"node_ip": "%s", "edge_ip": "%s"}\n' "$NODE_IP" "$EDGE_IP" > /etc/lnmt-node/node.json
install -d -m 711 /srv/lnmt-hosting /srv/lnmt-hosting/home
install -d -m 700 /srv/lnmt-hosting/disks

# The edge, and only the edge, may run the agent (one forced command, no shell).
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
grep -v "lnmt-node-edge" /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.new || true
echo "from=\"$EDGE_IP\",restrict,command=\"/usr/local/sbin/lnmt-node\" $EDGE_PUBKEY lnmt-node-edge" >> /root/.ssh/authorized_keys.new
mv /root/.ssh/authorized_keys.new /root/.ssh/authorized_keys

# Customers (uid 20000-29999) may not open connections to private networks
# (the LAN, the node's own services, other customers' apps); DNS through the
# local resolver stays allowed.
if ! grep -q "skuid 20000-29999" /etc/nftables.conf; then
  cp -a /etc/nftables.conf /etc/nftables.conf.before-customers
  python3 - <<'PY'
p = "/etc/nftables.conf"
s = open(p).read()
anchor = "type filter hook output priority 0; policy accept;"
rule = anchor + """

        # Customers: no new connections to private networks (DNS via the local resolver only)
        meta skuid 20000-29999 ip daddr 127.0.0.53 meta l4proto { tcp, udp } th dport 53 accept
        meta skuid 20000-29999 ip daddr { 192.0.2.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 127.0.0.0/8 } ct state new drop
        meta skuid 20000-29999 ip6 daddr { fc00::/7, fe80::/10, ::1 } ct state new drop"""
assert s.count(anchor) == 1
open(p, "w").write(s.replace(anchor, rule))
PY
  if ! nft -c -f /etc/nftables.conf; then
    cp -a /etc/nftables.conf.before-customers /etc/nftables.conf; echo "firewall change refused" >&2; exit 1
  fi
  nft -f /etc/nftables.conf
fi
# Previews (preview.py, ports 30000-30999): the LAN and the VPN may look at them.
if ! grep -q "dport 30000-30999" /etc/nftables.conf; then
  cp -a /etc/nftables.conf /etc/nftables.conf.before-previews
  EDGE_IP="$EDGE_IP" PREVIEW_NETS="$PREVIEW_NETS" python3 - <<'PY'
import os
p = "/etc/nftables.conf"
s = open(p).read()
anchor = f"ip saddr {os.environ['EDGE_IP']} tcp dport 20000-29999 accept"
assert s.count(anchor) == 1
rule = anchor + """

        # Previews of changes (LAIka looks at them before review) - LAN/VPN only
        ip saddr { """ + ", ".join(os.environ["PREVIEW_NETS"].split()) + """ } tcp dport 30000-30999 accept"""
open(p, "w").write(s.replace(anchor, rule))
PY
  if ! nft -c -f /etc/nftables.conf; then
    cp -a /etc/nftables.conf.before-previews /etc/nftables.conf; echo "preview firewall change refused" >&2; exit 1
  fi
  nft -f /etc/nftables.conf
fi
# Built apps get the current git hook.
PYTHONPATH=/usr/local/lib python3 -c "from lnmt_node import agent; agent.refresh_hooks()"

echo "lnmt-node installed on $NODE_IP"
