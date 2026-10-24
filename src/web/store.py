"""Accounts, sessions and the audit log of the web dashboard.

Data lives in /var/lib/lnmt-web (owned by lnmt-web, 0700):
  users.json   {"users": {name: {hash, role admin|member, access {host: view|manage},
                                  disabled, created}}}
  setup-code   sha256 of a one-time code (first administrator), with expiry
  audit.jsonl  one JSON line per change: at, user, ip, action, target, ok, detail
  sessions.json  signed-in sessions by the SHA-256 of their token (never the
               token itself), so a restart signs nobody out
Passwords: scrypt (n=2**14, r=8, p=1, 16-byte salt).
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

DATA = Path(os.environ.get("LNMT_WEB_DATA", "/var/lib/lnmt-web"))
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
ROLES = ("admin", "member")
LEVELS = ("view", "manage")
SESSION_SECONDS = 12 * 3600
IDLE_SECONDS = 2 * 3600
_lock = threading.RLock()
_sessions = None   # loaded from sessions.json on first use
_failures = {}
_saved_at = 0.0


def _path(name):
    return DATA / name


def _write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- passwords ------------------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        kind, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        mine = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(mine.hex(), digest)
    except (ValueError, AttributeError):
        return False


_DUMMY = hash_password("not a real password")


def valid_password(password):
    if not isinstance(password, str) or len(password) < 10:
        return "Use at least 10 characters."
    if len(password) > 200:
        return "That password is too long."
    return ""


# --- users ----------------------------------------------------------------------------

def load_users():
    with _lock:
        try:
            return json.loads(_path("users.json").read_text()).get("users", {})
        except (OSError, ValueError):
            return {}


def save_users(users):
    with _lock:
        _write_json(_path("users.json"), {"users": users})


def public_user(name, user):
    return {"username": name, "role": user.get("role", "member"), "access": user.get("access", {}),
            "disabled": bool(user.get("disabled")), "created": user.get("created"), "hosting": user.get("hosting"),
            "laika": user.get("laika") or None, "laika_suggest": user.get("laika_suggest") or None,
            "password": bool(user.get("hash"))}


# --- LAIka accounts ----------------------------------------------------------------------
#
# A LAIka account appears here by itself (sync_laika): a member with no
# password who signs in with LAIka ("laika": their LAIka name). An account
# made here with the same name is not taken over: it is offered the link
# (laika_suggest) and links when its owner signs in with LAIka while signed
# in here. Roles and site access stay this server's own.

def user_for_laika(laika_name):
    laika_name = (laika_name or "").lower()
    return next((name for name, user in load_users().items() if (user.get("laika") or "").lower() == laika_name), None)


def sync_laika(laika_users):
    """Make accounts for LAIka's people; returns (made, disabled)."""
    made, disabled = [], []
    with _lock:
        users = load_users()
        known = {(u.get("laika") or "").lower(): name for name, u in users.items() if u.get("laika")}
        present = set()
        for person in laika_users:
            laika_name = str(person.get("name") or "").lower()
            if not USERNAME.match(laika_name):
                continue
            present.add(laika_name)
            if laika_name in known:
                user = users[known[laika_name]]
                if person.get("disabled") and not user.get("disabled"):
                    user["disabled"] = True
                    disabled.append(known[laika_name])
                continue
            if laika_name in users:
                users[laika_name]["laika_suggest"] = laika_name  # same name, made here: link only when they sign in
                continue
            if person.get("disabled"):
                continue
            users[laika_name] = {"hash": "", "role": "member", "access": {}, "disabled": False, "created": time.time(),
                                 "laika": laika_name, "display": str(person.get("username") or laika_name)[:40]}
            made.append(laika_name)
        for laika_name, name in known.items():
            if laika_name not in present and not users[name].get("disabled") and not users[name].get("hash"):
                users[name]["disabled"] = True  # gone from LAIka (accounts with a password of their own stay)
                disabled.append(name)
        save_users(users)
    for name in disabled:
        end_sessions_of(name)
    return made, disabled


def link_laika(username, laika_name):
    with _lock:
        users = load_users()
        other = next((n for n, u in users.items() if (u.get("laika") or "").lower() == laika_name.lower() and n != username), None)
        if other:
            raise ValueError(f"That LAIka account is already linked to {other}.")
        users[username]["laika"] = laika_name.lower()
        users[username].pop("laika_suggest", None)
        save_users(users)


def set_hosting(username, customer, disk_gb, memory_gb=4, cpus=2, tasks=4096):
    with _lock:
        users = load_users()
        users[username]["hosting"] = {"customer": customer, "disk_gb": int(disk_gb), "memory_gb": float(memory_gb),
                                      "cpus": float(cpus), "tasks": int(tasks)}
        save_users(users)


def create_user(username, password, role="member", access=None):
    username = (username or "").strip().lower()
    if not USERNAME.match(username):
        raise ValueError("User names: 2-32 of a-z, 0-9, . _ - (starting with a letter or digit).")
    problem = valid_password(password)
    if problem:
        raise ValueError(problem)
    if role not in ROLES:
        raise ValueError("Role must be admin or member.")
    with _lock:
        users = load_users()
        if username in users:
            raise ValueError("That user exists.")
        users[username] = {"hash": hash_password(password), "role": role, "access": clean_access(access),
                           "disabled": False, "created": time.time()}
        save_users(users)
    return public_user(username, users[username])


def clean_access(access):
    result = {}
    for host, level in (access or {}).items():
        if isinstance(host, str) and level in LEVELS and re.fullmatch(r"[a-z0-9.-]{1,253}", host):
            result[host] = level
    return result


def admins(users):
    return [n for n, u in users.items() if u.get("role") == "admin" and not u.get("disabled")]


# --- setup code (first administrator) -------------------------------------------------

def make_setup_code(minutes=60):
    code = "-".join(secrets.token_hex(3) for _ in range(3))
    _write_json(_path("setup-code"), {"sha256": hashlib.sha256(code.encode()).hexdigest(),
                                       "expires": time.time() + minutes * 60})
    return code


def use_setup_code(code):
    path = _path("setup-code")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    good = (time.time() < float(data.get("expires", 0))
            and hmac.compare_digest(hashlib.sha256((code or "").strip().encode()).hexdigest(), data.get("sha256", "")))
    if good:
        path.unlink(missing_ok=True)
    return good


def needs_setup():
    return not admins(load_users())


# --- sessions and login ---------------------------------------------------------------

def login_allowed(username, ip, now=None):
    now = now or time.time()
    with _lock:
        recent = [t for t in _failures.get((username, ip), []) if now - t < 900]
        _failures[(username, ip)] = recent
        return len(recent) < 5


def login(username, password, ip):
    username = (username or "").strip().lower()
    if not login_allowed(username, ip):
        return None, "Too many attempts; wait 15 minutes."
    user = load_users().get(username)
    good = check_password(password or "", user["hash"] if user else _DUMMY)
    if not user or not good or user.get("disabled"):
        with _lock:
            _failures.setdefault((username, ip), []).append(time.time())
        return None, "Wrong user name or password."
    with _lock:
        _failures.pop((username, ip), None)
    return new_session(username), ""


def _all_sessions():
    global _sessions
    if _sessions is None:
        try:
            _sessions = json.loads(_path("sessions.json").read_text())
        except (OSError, ValueError):
            _sessions = {}
    return _sessions


def _persist(force=True):
    """Save sessions (expired ones dropped); "seen" updates at most once a minute."""
    global _saved_at
    if not force and time.time() - _saved_at < 60:
        return
    now = time.time()
    live = {k: v for k, v in _all_sessions().items()
            if now - v["created"] <= SESSION_SECONDS and now - v["seen"] <= IDLE_SECONDS}
    _sessions.clear()
    _sessions.update(live)
    _write_json(_path("sessions.json"), live)
    _saved_at = now


def new_session(username):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    with _lock:
        _all_sessions()[hashlib.sha256(token.encode()).hexdigest()] = {
            "user": username, "csrf": csrf, "created": time.time(), "seen": time.time()}
        _persist()
    return token


def session(token):
    if not token:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _lock:
        sessions = _all_sessions()
        data = sessions.get(key)
        if not data:
            return None
        if now - data["created"] > SESSION_SECONDS or now - data["seen"] > IDLE_SECONDS:
            sessions.pop(key, None)
            _persist()
            return None
        user = load_users().get(data["user"])
        if not user or user.get("disabled"):
            sessions.pop(key, None)
            _persist()
            return None
        data["seen"] = now
        _persist(force=False)
        return {**data, "role": user.get("role", "member"), "access": user.get("access", {}),
                "prefs": user.get("prefs", {}), "hosting": user.get("hosting"), "laika": user.get("laika"),
                "laika_suggest": user.get("laika_suggest"), "has_password": bool(user.get("hash"))}


def end_session(token):
    with _lock:
        _all_sessions().pop(hashlib.sha256((token or "").encode()).hexdigest(), None)
        _persist()


def end_sessions_of(username, keep=None):
    keep_key = hashlib.sha256(keep.encode()).hexdigest() if keep else None
    with _lock:
        sessions = _all_sessions()
        for key in [k for k, v in sessions.items() if v["user"] == username and k != keep_key]:
            sessions.pop(key, None)
        _persist()


# --- audit ----------------------------------------------------------------------------

def audit(user, ip, action, target="", ok=True, detail=""):
    line = json.dumps({"at": time.time(), "user": user, "ip": ip, "action": action, "target": target,
                       "ok": ok, "detail": str(detail)[:300]})
    with _lock:
        DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(_path("audit.jsonl"), "a") as handle:
            handle.write(line + "\n")
        os.chmod(_path("audit.jsonl"), 0o600)


def read_audit(limit=200):
    try:
        lines = _path("audit.jsonl").read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# --- invites (accounts are made by the invited person, on the LAN/VPN) ----------------

INVITE_SECONDS = 24 * 3600


def _load(name, default):
    try:
        return json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return default


def create_invite(role, created_by, note=""):
    if role not in ROLES:
        raise ValueError("Role must be admin or member.")
    token = secrets.token_urlsafe(24)
    with _lock:
        invites = {k: v for k, v in _load("invites.json", {}).items() if v.get("expires", 0) > time.time()}
        invites[hashlib.sha256(token.encode()).hexdigest()] = {
            "role": role, "created_by": created_by, "note": str(note)[:80],
            "created": time.time(), "expires": time.time() + INVITE_SECONDS}
        _write_json(_path("invites.json"), invites)
    return token


def list_invites():
    now = time.time()
    return [{"id": key[:12], "role": v["role"], "created_by": v.get("created_by"), "note": v.get("note", ""),
             "expires": v["expires"]}
            for key, v in sorted(_load("invites.json", {}).items(), key=lambda item: item[1]["created"])
            if v.get("expires", 0) > now]


def revoke_invite(short_id):
    with _lock:
        invites = _load("invites.json", {})
        matches = [k for k in invites if k.startswith(short_id) and len(short_id) == 12]
        for key in matches:
            invites.pop(key)
        _write_json(_path("invites.json"), invites)
    return bool(matches)


def peek_invite(token):
    invite = _load("invites.json", {}).get(hashlib.sha256((token or "").encode()).hexdigest())
    if not invite or invite.get("expires", 0) <= time.time():
        return None
    return invite


def accept_invite(token, username, password):
    key = hashlib.sha256((token or "").encode()).hexdigest()
    with _lock:
        invites = _load("invites.json", {})
        invite = invites.get(key)
        if not invite or invite.get("expires", 0) <= time.time():
            raise ValueError("This invite link is not valid any more; ask for a new one.")
        user = create_user(username, password, invite["role"])
        invites.pop(key)
        _write_json(_path("invites.json"), invites)
    return user


# --- site access (set from each site's page) -------------------------------------------

def set_site_access(username, hostname, level):
    """level: view | manage | "" (no access)."""
    if level not in ("", *LEVELS):
        raise ValueError("Level must be view or manage.")
    with _lock:
        users = load_users()
        user = users.get(username)
        if not user:
            raise ValueError("No such user.")
        access = dict(user.get("access") or {})
        if level:
            access[hostname] = level
        else:
            access.pop(hostname, None)
        user["access"] = access
        save_users(users)


def site_people(hostname):
    users = load_users()
    return [{"username": name, "role": u.get("role"), "disabled": bool(u.get("disabled")),
             "level": "manage" if u.get("role") == "admin" else (u.get("access") or {}).get(hostname, "")}
            for name, u in sorted(users.items())]


def forget_site(hostname):
    with _lock:
        users = load_users()
        for user in users.values():
            (user.get("access") or {}).pop(hostname, None)
        save_users(users)


# --- preferences and server settings ---------------------------------------------------

PREFS = {"theme": ("system", "dark", "light"), "density": ("comfortable", "compact"),
         "sites_view": ("cards", "table"), "sites_sort": ("name", "status")}
ACCENT = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_prefs(raw):
    prefs = {}
    for key, choices in PREFS.items():
        if raw.get(key) in choices:
            prefs[key] = raw[key]
    if isinstance(raw.get("accent"), str) and ACCENT.match(raw["accent"]):
        prefs["accent"] = raw["accent"].lower()
    return prefs


def set_prefs(username, raw):
    with _lock:
        users = load_users()
        users[username]["prefs"] = {**users[username].get("prefs", {}), **clean_prefs(raw)}
        save_users(users)
        return users[username]["prefs"]


def settings():
    return {"server_name": "LNMT", **_load("settings.json", {})}


def save_settings(raw):
    name = str(raw.get("server_name") or "").strip()[:40]
    with _lock:
        current = _load("settings.json", {})
        if name:
            current["server_name"] = name
        _write_json(_path("settings.json"), current)
    return settings()
