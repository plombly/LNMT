#!/bin/bash
# Install or update the web dashboard on the proxy server (run as root from
# an unpacked copy of this repository). Safe to run again.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)

# Which networks may reach the dashboard (your LAN and VPN, never the
# internet): /etc/lnmt/lnmt.env, made once with this machine's own LAN.
install -d -m 700 /etc/lnmt
if [ ! -f /etc/lnmt/lnmt.env ]; then
  LAN=$(ip -4 -o route show proto kernel scope link | awk '{print $1}' | head -1)
  printf '# Networks that may open the dashboard (space separated; your LAN and VPN).\nLNMT_TRUSTED_NETS="%s"\n' "${LAN:-192.168.0.0/16}" > /etc/lnmt/lnmt.env
  chmod 600 /etc/lnmt/lnmt.env
fi
. /etc/lnmt/lnmt.env
for net in $LNMT_TRUSTED_NETS; do
  python3 -c "import ipaddress, sys; ipaddress.ip_network(sys.argv[1])" "$net" || { echo "LNMT_TRUSTED_NETS: not a network: $net" >&2; exit 1; }
done

# 1. Code: the library (incl. web/ and its static files) and the commands.
install -d -m 755 /usr/local/lib/lnmt /usr/local/lib/lnmt/web
for f in "$HERE"/src/*.py; do install -m 644 "$f" /usr/local/lib/lnmt/; done
for f in "$HERE"/src/web/*.py; do install -m 644 "$f" /usr/local/lib/lnmt/web/; done
# The page and its modules: replaced as a whole, so removed files go too.
rm -rf /usr/local/lib/lnmt/web/static.new
cp -r "$HERE/src/web/static" /usr/local/lib/lnmt/web/static.new
find /usr/local/lib/lnmt/web/static.new -type d -exec chmod 755 {} + -o -type f -exec chmod 644 {} +
rm -rf /usr/local/lib/lnmt/web/static
mv /usr/local/lib/lnmt/web/static.new /usr/local/lib/lnmt/web/static
rm -rf /usr/local/lib/lnmt/__pycache__ /usr/local/lib/lnmt/web/__pycache__
# The commands (the terminal menu `lnmt` and its tools). The test and
# rollback tools of the original v2.1 are never installed on a live server.
for c in lnmt lnmt-admin lnmt-web lnmt-monitor lnmt-ddns lnmt-edge lnmt-tls lnmt-cloudflare; do
  install -m 755 "$HERE/bin/$c" /usr/local/sbin/$c
done
install -m 750 "$HERE/bin/lnmt-backup" /usr/local/sbin/lnmt-backup
install -m 750 "$HERE/bin/lnmt-restore" /usr/local/sbin/lnmt-restore

# 2. The unprivileged user the dashboard runs as.
id lnmt-web >/dev/null 2>&1 || useradd --system --home-dir /var/lib/lnmt-web --shell /usr/sbin/nologin lnmt-web
install -d -o lnmt-web -g lnmt-web -m 700 /var/lib/lnmt-web

# 3. Units: the root helper socket and the dashboard.
for u in lnmt-admin.socket lnmt-admin@.service lnmt-web.service lnmt-monitor.service lnmt-monitor.timer \
         lnmt-backup.service lnmt-backup.timer lnmt-edge.service lnmt-edge.timer lnmt-ddns.service lnmt-ddns.timer; do
  install -m 644 "$HERE/systemd/$u" /etc/systemd/system/$u
done
systemctl daemon-reload
systemctl enable --now lnmt-admin.socket lnmt-monitor.timer lnmt-backup.timer
systemctl enable lnmt-web.service
systemctl restart lnmt-web.service

# 4. nginx on 8443 for the LAN/VPN, with its own self-signed certificate.
install -d -m 700 /etc/nginx/lnmt-admin
if [ ! -s /etc/nginx/lnmt-admin/admin.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=LNMT dashboard" \
    -addext "subjectAltName=DNS:$(hostname),IP:$(hostname -I | awk '{print $1}')" \
    -keyout /etc/nginx/lnmt-admin/admin.key -out /etc/nginx/lnmt-admin/admin.pem 2>/dev/null
  chmod 600 /etc/nginx/lnmt-admin/admin.key
fi
CONF=/etc/nginx/conf.d/10-lnmt-admin.conf
OLD=$(mktemp); [ -f "$CONF" ] && cp -a "$CONF" "$OLD" || : > "$OLD"
ALLOW=$(for net in $LNMT_TRUSTED_NETS; do printf '    allow %s;\\n' "$net"; done)
sed "s|^    # @TRUSTED_NETS@.*|${ALLOW%\\n}|" "$HERE/reference/lnmt-admin-nginx.conf" > "$CONF"
chmod 644 "$CONF"
if ! nginx -t 2>/dev/null; then
  if [ -s "$OLD" ]; then cp -a "$OLD" "$CONF"; else rm -f "$CONF"; fi
  echo "nginx refused the dashboard site; put back the previous state" >&2; exit 1
fi
systemctl reload nginx
rm -f "$OLD"

# 5. Firewall: 8443 from the LAN and the VPN only (runtime + saved file).
# (The listing is read first: "nft list | grep -q" under pipefail fails
# whenever grep stops reading early, and added the rule again on every run.)
RULES=$(nft list chain inet filter input)
for net in $LNMT_TRUSTED_NETS; do
  grep -q "ip saddr $net tcp dport 8443 accept" <<<"$RULES" || \
    nft add rule inet filter input ip saddr "$net" tcp dport 8443 accept
done
if ! grep -q "dport 8443" /etc/nftables.conf; then
  cp -a /etc/nftables.conf /etc/nftables.conf.before-dashboard
  LNMT_TRUSTED_NETS="$LNMT_TRUSTED_NETS" python3 - <<'PY'
import os
p = "/etc/nftables.conf"
s = open(p).read()
rule = "        # Web dashboard - LAN/VPN only\n" + "".join(
    f"        ip saddr {net} tcp dport 8443 accept\n" for net in os.environ["LNMT_TRUSTED_NETS"].split())
anchor = s.index("ip saddr @cloudflare_ipv4 tcp dport 443 accept")
line_start = s.rindex("\n", 0, anchor) + 1
s = s[:line_start] + rule + s[line_start:]
open(p, "w").write(s)
PY
  if ! nft -c -f /etc/nftables.conf; then
    cp -a /etc/nftables.conf.before-dashboard /etc/nftables.conf
    echo "the saved firewall file would not load; left it as it was" >&2; exit 1
  fi
fi
# 6. Per-site request counting (graphs) and site options: the shared nginx
#    file, a short-lived count log per site, then every site re-rendered
#    (each one tested; a site nginx refuses keeps its previous file).
cat > /etc/logrotate.d/lnmt-sites <<'ROT'
/var/log/nginx/lnmt-sites/*.log {
	daily
	rotate 1
	missingok
	notifempty
	sharedscripts
	postrotate
		invoke-rc.d nginx rotate >/dev/null 2>&1
	endscript
}
ROT
python3 -c 'import sys, json; sys.path.insert(0, "/usr/local/lib"); from lnmt import service; r = service.sites_refresh(); print("sites refreshed:", len(r["refreshed"]), "failed:", r["failed"])'

echo "Dashboard: https://$(hostname -I | awk '{print $1}'):8443/  (first time: sudo lnmt-web setup-code)"
