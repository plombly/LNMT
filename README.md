# LNMT — LAIka NGINx Management Terminal

A self-hosted control panel for publishing websites and apps from your own
network through Cloudflare, with optional app hosting and an AI builder
([LAIka](https://github.com/plombly/LAIka)) next to it.

- **Sites**: put a service on your LAN online at a hostname: a proxied
  Cloudflare DNS record, Cloudflare Origin TLS, nginx, dynamic DNS for a
  home connection, a firewall that only lets Cloudflare reach port 443.
  Health checks, uptime, password protection, redirects, rate limits,
  security headers, maintenance mode.
- **Domains**: each Cloudflare zone with its sites and every DNS record type,
  managed from the dashboard.
- **Hosting** (optional, on a separate VM): apps in rootless containers for
  the people you give hosting to — templates (WordPress, PHP, Node, Python,
  databases), any image, or a Dockerfile built by git push; files and SFTP;
  usage graphs; scheduled tasks; previews. See [Hosting](docs/hosting.md).
- **People**: invite links, roles, per-site access, sign in with LAIka,
  per-person alerts (Discord, ntfy), an audit log.
- **LAIka**: pair it once, then people sign in with their LAIka account,
  LNMT apps can be built by LAIka projects, and every change is rendered and
  screenshotted before it is reviewed.

The dashboard is for your LAN and VPN only: it listens on port 8443 and the
installer limits it to the networks in `/etc/lnmt/lnmt.env`
(`LNMT_TRUSTED_NETS`). Never expose it to the internet.

## Status

LNMT runs in production for its author. The installer in this repository
(`bin/install-web.sh`) installs and updates the dashboard on an existing LNMT
edge server; a one-step installer for a fresh server is not included yet.

## Layout

```
src/            the edge: nginx sites, Cloudflare, DDNS, TLS, firewall ranges,
                backups, monitor (src/web: the dashboard, standard library only)
bin/            commands (lnmt menu, lnmt-web, lnmt-admin root helper, timers)
systemd/        units
node/           the hosting node agent (lnmt-node) and its installer
tests/          pytest (no root, no network: python -m pytest tests)
```

## License

MIT, see [LICENSE](LICENSE).
