# Hosting

LNMT can host apps for the people who use it: WordPress, PHP, static sites,
Node, Python, databases, any container image, or a Dockerfile built from git
push. Apps run on a separate **hosting node** (a VM); the **edge** (the LNMT
server) stays the only machine on the internet side.

```
Internet ─ Cloudflare ─ edge (TLS, Cloudflare-only firewall, DNS, dashboard)
                          │  LAN, only edge -> node
                          ▼
                     hosting node: one Linux user per customer,
                     rootless Podman containers, disk and CPU/memory limits
```

- Every hosted app that is published is an ordinary proxied site whose
  backend is the app on the node: TLS, DNS records, DDNS, uptime and alerts
  work as for any site.
- The node runs `lnmt-node`, a root agent reached only from the edge over SSH
  with a forced command. The dashboard never talks to the node directly.
- Each customer is a Linux user on the node with rootless Podman, a home on
  its own disk image (a quota), and limits on all their processes together.
  Containers never run privileged and never get the host network; they
  publish only on the node's LAN address, in a port range the node firewall
  opens to the edge alone. Customers' processes cannot open connections to
  private networks.
- Customers sign in with SSH only for `git push` / `git fetch` of their apps
  and SFTP (a restricted login shell: no terminal, no tunnels).

## What a person with hosting can do

- **Apps** from templates (static, PHP with a chosen version, WordPress with
  its own database, Node, Python, MariaDB, PostgreSQL, Redis, Adminer), from
  any image, or from their own Dockerfile (git push or uploaded files: a
  failed build keeps the running version). Settings, environment variables
  (write-only), logs, start/stop/restart, scheduled tasks, database export
  and import, usage graphs, daily image updates with rollback.
- **Domains**: their own Cloudflare zones, DNS records (every record type),
  and publishing apps at hostnames on them.
- **Files**: a file manager in the browser and SFTP, confined to the app.
- **Previews**: a pushed `lnmt-preview/<name>` branch runs a throwaway copy
  of the app next to the live one (WordPress with its own empty database),
  on ports 30000-30999 for the LAN/VPN only.

## Setting up a hosting node

On a fresh Ubuntu VM on the same LAN as the edge, with the repository
unpacked (as root):

```
EDGE_IP=<edge address> NODE_IP=<this node's address> \
EDGE_PUBKEY="$(ssh <edge> sudo cat /etc/lnmt/node_ed25519.pub)" \
PREVIEW_NETS="<your LAN> <your VPN>" bash node/install-node.sh
```

Then enable hosting for a person under **Users** in the dashboard.
