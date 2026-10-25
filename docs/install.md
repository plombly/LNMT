# Installing LNMT

LNMT runs on one Debian or Ubuntu server (the **edge**) on your network.
Cloudflare sends your sites' visitors to it; nothing else on the internet
can reach it.

## Before you start

- A Debian 12+ or Ubuntu 24.04+ server with a LAN address, reachable from
  the internet on port 443 (a port forward on your router for a home
  connection). A fresh machine is best: the installer writes the firewall.
- A Cloudflare account with your domain on it, and an API token that can
  edit that zone's DNS and SSL settings.
- Know your LAN (for example `192.168.1.0/24`) and, if you use one, your
  VPN's network: only these may use SSH and the dashboard.

## Install

On the server, as root:

```
git clone https://github.com/plombly/LNMT.git
cd LNMT
sudo LNMT_TRUSTED_NETS="192.168.1.0/24 10.8.0.0/24" bash bin/install.sh
```

It installs nginx and nftables, writes a firewall (everything dropped;
SSH and the dashboard from your trusted networks; HTTPS only from
Cloudflare's addresses, refreshed daily), installs LNMT's commands and
services, and prints a **setup code**. Open `https://<server address>:8443/`
from your LAN (the certificate is self-signed), enter the code and create
the first administrator. Then add your Cloudflare token and your first site.

The installer refuses rather than lock you out: your SSH session must come
from a trusted network, and it never replaces a firewall that already has
rules or other nftables tables (Docker, for example).

## Updating

`git pull` and run `sudo bash bin/install.sh` again. Settings, sites,
certificates and the firewall are kept.

## Settings

- `/etc/lnmt/lnmt.env`: `LNMT_TRUSTED_NETS`. After changing it, run the
  installer again for the dashboard; the SSH rules are in
  `/etc/nftables.conf`.
- Your own firewall: run with `LNMT_FIREWALL=keep`. It must have
  `table inet filter` with a `chain input` and two interval sets,
  `cloudflare_ipv4` and `cloudflare_ipv6`, each with one `elements = { … }`
  block (lnmt-edge rewrites them), and the rules
  `ip saddr @cloudflare_ipv4 tcp dport 443 accept` /
  `ip6 saddr @cloudflare_ipv6 tcp dport 443 accept`.

## Hosting apps

See [Hosting](hosting.md) to add a hosting node.

## Testing the installer

`tests/install/clean-install.sh ubuntu:24.04` (or `debian:12`) installs
into a fresh privileged systemd container, checks the result, and runs the
installer a second time.
