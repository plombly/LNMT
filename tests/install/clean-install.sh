#!/bin/bash
# Install LNMT from this checkout on a fresh Debian/Ubuntu machine (a
# privileged systemd container) and check the result, then run it again.
#   tests/install/clean-install.sh ubuntu:24.04
set -u  # no pipefail: "box ... | grep -q" checks must not fail when grep stops reading early
IMAGE=${1:?image, e.g. ubuntu:24.04}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
NAME="lnmt-install-test-$(echo "$IMAGE" | tr -c 'a-z0-9' '-' | sed 's/-*$//')"
TAG="lnmt-install-base:$(echo "$IMAGE" | tr ':/' '--')"
WORK=$(mktemp -d)
failures=0
pass() { echo "PASS $*"; }
bad() { echo "FAIL $*"; failures=$((failures + 1)); }
box() { docker exec "$NAME" bash -c "$*"; }
cleanup() { [ "${KEEP:-0}" = 1 ] && echo "kept $NAME" || docker rm -f -v "$NAME" >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT

cat > "$WORK/Dockerfile" <<DOCKER
FROM $IMAGE
RUN apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q systemd systemd-sysv dbus iproute2 && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
DOCKER
docker build -q -t "$TAG" "$WORK" >/dev/null || { bad "base image"; exit 1; }
docker rm -f -v "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --hostname lnmt-test --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
  --memory=2g --cpus=2 "$TAG" >/dev/null || { bad "container"; exit 1; }
for _ in $(seq 30); do box 'systemctl is-system-running 2>/dev/null' | grep -Eq 'running|degraded' && break; sleep 1; done
(cd "$REPO" && git ls-files -co --exclude-standard | tar -c -T -) | docker exec -i "$NAME" bash -c 'mkdir -p /root/lnmt && tar -x -C /root/lnmt'

NET=$(box "ip -4 -o route show proto kernel scope link | awk '{print \$1}' | head -1")
if box "LNMT_TRUSTED_NETS='$NET 10.99.0.0/24' bash /root/lnmt/bin/install.sh" > "$WORK/install.log" 2>&1; then pass "install"; else bad "install"; tail -20 "$WORK/install.log"; fi
grep -q "Setup code (valid 1 hour)" "$WORK/install.log" && pass "setup code shown" || bad "setup code shown"
for u in nginx nftables lnmt-web lnmt-admin.socket lnmt-edge.timer lnmt-monitor.timer lnmt-backup.timer; do
  [ "$(box "systemctl is-active $u")" = active ] && pass "$u active" || bad "$u active"
done
box "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/" | grep -q 200 && pass "dashboard answers" || bad "dashboard answers"
box "nft list chain inet filter input" > "$WORK/rules"
grep -q "policy drop" "$WORK/rules" && pass "default drop" || bad "default drop"
grep -q "ip saddr 10.99.0.0/24 tcp dport 22 accept" "$WORK/rules" && pass "SSH from trusted nets" || bad "SSH from trusted nets"
grep -q "ip saddr 10.99.0.0/24 tcp dport 8443 accept" "$WORK/rules" && pass "dashboard from trusted nets" || bad "dashboard from trusted nets"
[ "$(grep -c 'dport 8443' "$WORK/rules")" = 2 ] && pass "no duplicate rules" || bad "no duplicate rules"
box "nft list set inet filter cloudflare_ipv4" | grep -q "173.245.48.0/20" && pass "Cloudflare ranges loaded" || bad "Cloudflare ranges loaded"
box "test -f /etc/nginx/conf.d/00-lnmt-default-sink.conf && test -f /etc/nginx/conf.d/cloudflare-realip.conf" && pass "sink and real-IP config" || bad "sink and real-IP config"
SINK=$(box "curl -sk -o /dev/null -w '%{http_code}' --resolve nothing.example:443:127.0.0.1 https://nothing.example/")
[ "$SINK" = 000 ] && pass "unknown names are dropped" || bad "unknown names are dropped"
box "stat -c %a /etc/lnmt /etc/lnmt/lnmt.env" | tr '\n' ' ' | grep -q "^700 600" && pass "settings root-only" || bad "settings root-only"

if box "bash /root/lnmt/bin/install.sh" > "$WORK/again.log" 2>&1; then pass "second run"; else bad "second run"; tail -20 "$WORK/again.log"; fi
[ "$(box "nft list chain inet filter input" | grep -c 'dport 8443')" = 2 ] && pass "second run adds no rules" || bad "second run adds no rules"

echo; [ "$failures" = 0 ] && echo "ALL PASSED ($IMAGE)" || echo "$failures FAILED ($IMAGE)"
exit "$failures"
