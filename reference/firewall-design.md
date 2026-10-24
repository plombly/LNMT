# Firewall architecture

The production system uses nftables with:

- Default-drop inbound policy
- Established/related connections allowed
- Loopback allowed
- ICMP/ICMPv6 allowed
- SSH restricted to trusted management/VPN networks
- Public TCP/443 restricted to Cloudflare IPv4/IPv6 ranges
- Optional LAN HTTPS access
- Default-drop forwarding
- Outbound connections allowed

Cloudflare edge ranges are refreshed by the edge updater.

Actual firewall rules, IP addresses, and runtime sets are
not included in this source export.
