import fcntl
import ipaddress
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4/ips"
NGINX_OUT = Path("/etc/nginx/conf.d/cloudflare-realip.conf")
OLD_NGINX_OUT = Path("/etc/nginx/conf.d/lnmt-cloudflare-realip.conf")
CACHE = Path("/var/lib/lnmt/cloudflare-ranges.json")
LOCK = Path("/run/lock/lnmt-edge.lock")
NFT_FAMILY = "inet"
NFT_TABLE = "filter"
NFT_V4_SET = "cloudflare_ipv4"
NFT_V6_SET = "cloudflare_ipv6"
NFT_CONF = Path("/etc/nftables.conf")


def _run(cmd, *, input_text=None):
    p = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def fetch_ranges():
    req = urllib.request.Request(API, headers={"User-Agent": "LNMT/2.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    if not data.get("success", False):
        raise RuntimeError("Cloudflare /ips API did not report success")
    res = data.get("result", {})
    v4 = res.get("ipv4_cidrs", [])
    v6 = res.get("ipv6_cidrs", [])
    if not isinstance(v4, list) or not isinstance(v6, list) or len(v4) < 10 or len(v6) < 5:
        raise RuntimeError("Cloudflare range response was unexpectedly small")
    if len(v4) != len(set(v4)) or len(v6) != len(set(v6)):
        raise RuntimeError("Cloudflare range response contains duplicate CIDRs")
    for x in v4:
        if ipaddress.ip_network(x, strict=True).version != 4:
            raise RuntimeError(f"IPv4 list contains non-IPv4 CIDR: {x}")
    for x in v6:
        if ipaddress.ip_network(x, strict=True).version != 6:
            raise RuntimeError(f"IPv6 list contains non-IPv6 CIDR: {x}")
    return {"etag": res.get("etag"), "ipv4_cidrs": v4, "ipv6_cidrs": v6}


def render_nginx(d):
    lines = [
        "# Managed by LAIka NGINx Management Terminal - Cloudflare real visitor IPs",
        "real_ip_header CF-Connecting-IP;",
        "real_ip_recursive on;",
        "",
    ]
    lines += [f"set_real_ip_from {x};" for x in d["ipv4_cidrs"]]
    lines += [""] + [f"set_real_ip_from {x};" for x in d["ipv6_cidrs"]]
    return "\n".join(lines) + "\n"


def render_nft(d):
    # nft -f applies a batch transaction atomically: either both sets change or neither does.
    v4 = ", ".join(d["ipv4_cidrs"])
    v6 = ", ".join(d["ipv6_cidrs"])
    return (
        f"flush set {NFT_FAMILY} {NFT_TABLE} {NFT_V4_SET}\n"
        f"add element {NFT_FAMILY} {NFT_TABLE} {NFT_V4_SET} {{ {v4} }}\n"
        f"flush set {NFT_FAMILY} {NFT_TABLE} {NFT_V6_SET}\n"
        f"add element {NFT_FAMILY} {NFT_TABLE} {NFT_V6_SET} {{ {v6} }}\n"
    )


def _preflight_nft():
    for name in (NFT_V4_SET, NFT_V6_SET):
        ok, out = _run(["nft", "list", "set", NFT_FAMILY, NFT_TABLE, name])
        if not ok:
            raise RuntimeError(f"Required nftables set {NFT_FAMILY} {NFT_TABLE} {name} is missing: {out}")


def _update_edge_locked():
    from .nginx import nginx_test, nginx_reload

    d = fetch_ranges()  # Nothing local has changed yet.
    _preflight_nft()
    nginx_text = render_nginx(d)
    nft_text = render_nft(d)

    # Validate nft syntax/references without changing the live ruleset.
    with tempfile.NamedTemporaryFile("w", prefix="lnmt-edge-", suffix=".nft", delete=False) as f:
        f.write(nft_text)
        nft_candidate = Path(f.name)
    try:
        ok, out = _run(["nft", "-c", "-f", str(nft_candidate)])
        if not ok:
            raise RuntimeError("Candidate nftables update failed validation: " + out)

        old_nginx = NGINX_OUT.read_bytes() if NGINX_OUT.exists() else None
        old_duplicate = OLD_NGINX_OUT.read_bytes() if OLD_NGINX_OUT.exists() else None

        # Stage the canonical Nginx file, while temporarily removing the obsolete duplicate.
        tmp = NGINX_OUT.with_suffix(".candidate")
        tmp.write_text(nginx_text)
        if OLD_NGINX_OUT.exists():
            OLD_NGINX_OUT.unlink()
        tmp.replace(NGINX_OUT)
        ok, out = nginx_test()
        if not ok:
            if old_nginx is None:
                NGINX_OUT.unlink(missing_ok=True)
            else:
                NGINX_OUT.write_bytes(old_nginx)
            if old_duplicate is not None:
                OLD_NGINX_OUT.write_bytes(old_duplicate)
            raise RuntimeError("Candidate Nginx real-IP config failed validation: " + out)

        def restore_nginx():
            if old_nginx is None:
                NGINX_OUT.unlink(missing_ok=True)
            else:
                NGINX_OUT.write_bytes(old_nginx)
            if old_duplicate is not None:
                OLD_NGINX_OUT.write_bytes(old_duplicate)
            nginx_reload()

        # nginx first (it can still be put back), the firewall last: a
        # failure never leaves the two out of step.
        ok, out = nginx_reload()
        if not ok:
            restore_nginx()
            raise RuntimeError("Nginx reload failed; nothing changed: " + out)

        # Commit firewall atomically. If this fails, put nginx back as it was.
        ok, out = _run(["nft", "-f", str(nft_candidate)])
        if not ok:
            restore_nginx()
            raise RuntimeError("nftables update failed; Nginx was restored: " + out)

        # The ranges also go into the saved firewall file, so a reboot or an
        # nftables restart starts from them instead of older ones.
        persist_ranges(d)

        CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache_tmp = CACHE.with_suffix(".tmp")
        cache_tmp.write_text(json.dumps(d, indent=2) + "\n")
        cache_tmp.replace(CACHE)
        return d
    finally:
        nft_candidate.unlink(missing_ok=True)


_SET_BLOCK = r"(set\s+{name}\s*\{{[^}}]*?elements\s*=\s*\{{)([^}}]*)(\}})"


def with_ranges(conf, d):
    """conf text with both sets' elements replaced, or None if the file does
    not have the expected shape (then it is left alone)."""
    import re
    for name, key in ((NFT_V4_SET, "ipv4_cidrs"), (NFT_V6_SET, "ipv6_cidrs")):
        pattern = re.compile(_SET_BLOCK.format(name=re.escape(name)), re.S)
        if len(pattern.findall(conf)) != 1:
            return None
        body = "\n" + ",\n".join(f"            {cidr}" for cidr in d[key]) + "\n        "
        conf = pattern.sub(lambda m: m.group(1) + body + m.group(3), conf)
    return conf


def persist_ranges(d):
    try:
        current = NFT_CONF.read_text()
    except OSError:
        return False
    updated = with_ranges(current, d)
    if updated is None:
        print("note: /etc/nftables.conf has no single elements block per Cloudflare set; not updated")
        return False
    if updated == current:
        return True
    candidate = NFT_CONF.with_name(".nftables.conf.candidate")
    candidate.write_text(updated)
    try:
        ok, out = _run(["nft", "-c", "-f", str(candidate)])
        if not ok:
            print("note: the saved firewall file would not load with the new ranges; left as it was: " + out)
            return False
        NFT_CONF.with_name("nftables.conf.before-edge").write_text(current)
        candidate.chmod(NFT_CONF.stat().st_mode & 0o7777)
        candidate.replace(NFT_CONF)
        return True
    finally:
        candidate.unlink(missing_ok=True)


def update_edge():
    """
    Serialize the complete Cloudflare edge update transaction.

    Multiple callers may be started by systemd, an administrator,
    or the torture suite at the same time.  They must not share
    candidate/cache paths or modify Nginx/nftables concurrently.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)

    with LOCK.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return _update_edge_locked()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# Backward-compatible name used by the staged launcher.
def update_realip():
    return update_edge()
