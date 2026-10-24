"""The LAIka NGINx Management Terminal web dashboard (standard library only).

Runs as the unprivileged user lnmt-web on 127.0.0.1:8090; nginx serves it on
port 8443 to the LAN and VPN only. Everything that touches nginx, state or
Cloudflare goes to the root helper (admin.py) over /run/lnmt-admin.sock.

Who may do what:
  admin   everything: sites, Cloudflare credentials, DDNS, firewall ranges,
          certificates, backups, users, audit log
  member  only the sites in their access list: "view" (status, health,
          config) or "manage" (also enable/disable, backend, Basic Auth)
Every write needs the session cookie (HttpOnly, SameSite=Strict) and the
X-CSRF-Token header; every change is written to the audit log.
"""

import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import store

LAIKA_STATES = {}  # sign-in with LAIka in progress: state -> (started, account to link or "")

STATIC = Path(__file__).resolve().parent / "static"
ADMIN_SOCKET = os.environ.get("LNMT_ADMIN_SOCKET", "/run/lnmt-admin.sock")
COOKIE = "lnmt_session"
HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
MAX_BODY = 64 * 1024
MAX_UPLOAD = 12 * 1024 * 1024  # file manager uploads (base64 in JSON)
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class Redirect(Exception):
    """Answer with a redirect (the sign-in with LAIka round trip)."""

    def __init__(self, location, cookies=()):
        super().__init__(location)
        self.location, self.cookies = location, list(cookies)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def call_admin(action, /, **args):
    """One request to the root helper; returns its result or raises ApiError.
    (Positional-only: an action may itself take an argument named "action".)"""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(400)
            conn.connect(ADMIN_SOCKET)
            conn.sendall((json.dumps({"action": action, "args": args}) + "\n").encode())
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        raise ApiError(503, "The manager's root service is not answering (lnmt-admin.socket).")
    try:
        answer = json.loads(b"".join(chunks).decode())
    except ValueError:
        raise ApiError(502, "The root service gave no answer.")
    if not answer.get("ok"):
        raise ApiError(400, answer.get("error") or "failed")
    return answer.get("result")


class Handler(BaseHTTPRequestHandler):
    server_version = "lnmt-web"
    sys_version = ""

    # --- plumbing -----------------------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_ip(), fmt % args))

    def client_ip(self):
        # Only nginx on this machine talks to us (we listen on 127.0.0.1).
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send_json(self, status, data, extra=()):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        for name, value in extra:
            self.send_header(name, value)
        self.security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def body(self, limit=MAX_BODY):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ApiError(413, "Too large (use SFTP for big files)." if limit > MAX_BODY else "Too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Bad JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Bad JSON.")
        return data

    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    # --- access ---------------------------------------------------------------------------

    def me(self):
        info = store.session(self.cookie_token())
        if not info:
            raise ApiError(401, "Sign in first.")
        return info

    def need_admin(self):
        info = self.me()
        if info["role"] != "admin":
            raise ApiError(403, "Administrators only.")
        return info

    def need_site(self, host, level):
        info = self.me()
        if not HOST.match(host or ""):
            raise ApiError(404, "No such site.")
        if info["role"] == "admin":
            return info
        have = info["access"].get(host)
        if have is None:
            raise ApiError(404, "No such site.")  # members never learn other sites exist
        if level == "manage" and have != "manage":
            raise ApiError(403, "You may only view this site.")
        return info

    def check_csrf(self, info):
        if self.headers.get("X-CSRF-Token") != info["csrf"]:
            raise ApiError(403, "Missing or wrong CSRF token; reload the page.")

    def change(self, info, action, target, function):
        """Run a change, audit it either way."""
        try:
            result = function()
        except ApiError as exc:
            store.audit(info["user"], self.client_ip(), action, target, False, exc.message)
            raise
        store.audit(info["user"], self.client_ip(), action, target, True)
        return result

    # --- verbs ----------------------------------------------------------------------------

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path = urlsplit(self.path).path
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "Not allowed.")
                return self.static(path)
            parts = [unquote(p) for p in path[len("/api/"):].split("/") if p]
            result = self.route(method, parts)
            if isinstance(result, tuple):
                self.send_json(200, result[0], result[1])
            else:
                self.send_json(200, result)
        except Redirect as exc:
            self.send_response(302)
            self.send_header("Location", exc.location)
            self.send_header("Cache-Control", "no-store")
            for cookie in exc.cookies:
                self.send_header("Set-Cookie", cookie)
            self.security_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
        except ApiError as exc:
            self.send_json(exc.status, {"detail": exc.message})
        except Exception as exc:
            sys.stderr.write(f"error {method} {path}: {type(exc).__name__}: {exc}\n")
            self.send_json(500, {"detail": "Something went wrong; see journalctl -u lnmt-web."})

    def static(self, path):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if not re.fullmatch(r"(js/)?[a-z0-9-]+\.(html|js|css|svg)", name):
            name = "index.html"  # client-side routes
        file = STATIC / name
        if not file.is_file():
            raise ApiError(404, "Not found.")
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[file.suffix])
        self.send_header("Cache-Control", "no-cache")
        self.security_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- routes ---------------------------------------------------------------------------

    def route(self, method, parts):
        head = parts[0] if parts else ""
        if head == "health":
            return {"status": "ok"}
        if head == "setup":
            return self.first_setup(method)
        if head == "login" and method == "POST":
            return self.login()
        if head == "invite" and len(parts) == 2:
            return self.invite(method, parts[1])
        if head == "settings" and method == "GET":
            return store.settings()
        if head == "link":
            return self.route_link(method, parts)
        if head == "laika" and parts[1:2] in (["available"], ["login"], ["return"]) and method == "GET":
            return self.laika_sign_in(parts[1])
        info = self.me()
        if method != "GET":
            self.check_csrf(info)
        if head == "logout" and method == "POST":
            store.end_session(self.cookie_token())
            return {"ok": True}, [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")]
        if head == "me":
            return self.route_me(method, parts, info)
        if head == "sites":
            return self.route_sites(method, parts, info)
        if head == "users":
            return self.route_users(method, parts)
        if head in ("apps", "hosting"):
            return self.route_hosting(method, parts, info)
        if head == "domains":
            return self.route_domains(method, parts, info)
        if head == "laika":
            return self.route_laika(method, parts, info)
        admin_routes = {
            ("GET", "system"): lambda: call_admin("system_status"),
            ("GET", "certificates"): lambda: call_admin("certificates"),
            ("GET", "edge"): lambda: call_admin("edge_status"),
            ("GET", "credentials"): lambda: call_admin("list_credentials"),
            ("GET", "backups"): lambda: call_admin("backups"),
            ("GET", "nginx"): lambda: call_admin("nginx_check"),
            ("GET", "alerts"): lambda: call_admin("alerts_get"),
            ("GET", "audit"): lambda: {"entries": store.read_audit()},
        }
        if (method, head) in admin_routes and len(parts) == 1:
            self.need_admin()
            return admin_routes[(method, head)]()
        admin = self.need_admin()
        if method == "POST" and parts == ["settings"]:
            data = self.body()
            return self.change(admin, "settings", "", lambda: store.save_settings(data))
        if parts[:1] == ["invites"]:
            return self.route_invites(method, parts, admin)
        if method == "POST" and parts == ["alerts"]:
            d = self.body()
            args = {k: d[k] for k in ("discord_webhook", "ntfy_url", "events") if k in d}
            return self.change(admin, "alerts.update", "", lambda: call_admin("alerts_set", **args))
        if method == "POST" and parts == ["alerts", "test"]:
            return self.change(admin, "alerts.test", "", lambda: call_admin("alerts_test"))
        if method == "POST" and parts == ["ddns", "run"]:
            return self.change(admin, "ddns.run", "", lambda: call_admin("ddns_run"))
        if method == "POST" and parts == ["edge", "update"]:
            return self.change(admin, "edge.update", "", lambda: call_admin("edge_update"))
        if method == "POST" and parts == ["backups"]:
            return self.change(admin, "backup.now", "", lambda: call_admin("backup_now"))
        if method == "POST" and len(parts) == 3 and parts[0] == "backups" and parts[2] == "verify":
            return call_admin("backup_verify", name=parts[1])
        if parts[:1] == ["credentials"]:
            return self.route_credentials(method, parts, admin)
        raise ApiError(404, "Not found.")

    # --- LAIka --------------------------------------------------------------------------

    def laika_caller(self):
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or not call_admin("laika_check", token=token)["ok"]:
            raise ApiError(401, "Unknown caller.")
        return token

    def route_link(self, method, parts):
        """LAIka's calls (its token is the caller; no session)."""
        token = self.headers.get("Authorization", "")[7:].strip()
        if method == "POST" and parts == ["link", "hello"]:  # finishing a pairing: the code's token
            d = self.body()
            result = call_admin("laika_hello", token=token, laika_url=str(d.get("laika_url", "")),
                                laika_token=str(d.get("token", "")))
            store.audit("LAIka", self.client_ip(), "laika.connect", str(d.get("laika_url", ""))[:80])
            return result
        self.laika_caller()
        if method == "POST" and parts == ["link", "bye"]:
            store.audit("LAIka", self.client_ip(), "laika.disconnect", "")
            return call_admin("laika_disconnect", token=token)
        query = dict(p.partition("=")[::2] for p in (urlsplit(self.path).query or "").split("&") if p)
        if method == "GET" and parts == ["link", "apps"]:
            return {"apps": self.laika_person_apps(unquote(query.get("user", "")))}
        if method == "POST" and parts == ["link", "deploy-key"]:
            d = self.body()
            apps = self.laika_person_apps(str(d.get("user", "")))
            target = next((a for a in apps if a["customer"] == d.get("customer") and a["name"] == d.get("app")), None)
            if not target or not target.get("git_url"):
                raise ApiError(404, "No such app of that person's (built from git).")
            label = str(d.get("label") or "laika")[:40]
            result = call_admin("app_deploy_key", customer=target["customer"], name=target["name"],
                                public_key=str(d.get("public_key", "")), label=label)
            call_admin("laika_link_record", customer=target["customer"], name=target["name"],
                       project=str(d.get("project", ""))[:40], for_user=str(d.get("user", ""))[:40],
                       url=str(d.get("project_url", ""))[:300])
            store.audit(f"LAIka for {d.get('user', '')}", self.client_ip(), "app.deploy_key",
                        f"{target['customer']}/{target['name']} ({label})")
            return result
        raise ApiError(404, "Not found.")

    def laika_person_apps(self, laika_name):
        """The apps of the account linked to a LAIka user (git-built ones carry their address)."""
        username = store.user_for_laika(laika_name)
        user = store.load_users().get(username or "")
        if not user or user.get("disabled"):
            raise ApiError(404, "That LAIka user has no account on this server.")
        customer = (user.get("hosting") or {}).get("customer")
        if not customer:
            return []
        apps = call_admin("apps_list", customer=customer)["apps"]
        node_ip = call_admin("hosting_templates")["nodes"][0]["ip"]
        return [{"customer": customer, "name": a["name"], "image": a.get("image", ""), "state": a.get("state", ""),
                 "git_url": f"ssh://c-{customer}@{node_ip}/{a['name']}.git" if a.get("build") else ""} for a in apps]

    def laika_sign_in(self, step):
        status = call_admin("laika_status")
        if step == "available":
            return {"available": status["connected"]}
        if not status["connected"]:
            raise ApiError(404, "LAIka is not connected.")
        query = dict(p.partition("=")[::2] for p in (urlsplit(self.path).query or "").split("&") if p)
        if step == "login":
            state = secrets.token_urlsafe(24)
            linking = ""
            info = store.session(self.cookie_token())
            if query.get("link") == "1" and info:
                linking = info["user"]  # signed in here: link this account to the LAIka one
            LAIKA_STATES[state] = (time.time(), linking)
            for old in [k for k, (at, _) in LAIKA_STATES.items() if time.time() - at > 600]:
                LAIKA_STATES.pop(old, None)
            raise Redirect(f"{status['url']}/#/connect?state={state}",
                           [f"lnmt_laika_state={state}; Path=/api/laika; Max-Age=600; HttpOnly; Secure; SameSite=Lax"])
        state, code = query.get("state", ""), query.get("code", "")
        cookie_state = next((v for k, _, v in (p.strip().partition("=") for p in (self.headers.get("Cookie") or "").split(";"))
                             if k == "lnmt_laika_state"), "")
        started = LAIKA_STATES.pop(state, None)
        if not state or state != cookie_state or not started or time.time() - started[0] > 600:
            raise Redirect("/#/login-failed")
        try:
            person = call_admin("laika_call", method="POST", path="/api/link/service/token", body={"code": code})
        except ApiError:
            raise Redirect("/#/login-failed")
        laika_name = person["name"]
        username = store.user_for_laika(laika_name)
        if started[1]:
            try:
                store.link_laika(started[1], laika_name)
            except ValueError:
                raise Redirect("/#/login-failed")
            username = started[1]
        if not username:
            made, _ = store.sync_laika([person])
            username = store.user_for_laika(laika_name)
        user = store.load_users().get(username or "")
        if not user or user.get("disabled"):
            store.audit(laika_name, self.client_ip(), "login.laika", "", False, "no account or disabled")
            raise Redirect("/#/login-failed")
        token = store.new_session(username)
        store.audit(username, self.client_ip(), "login.laika", laika_name)
        raise Redirect("/#/sites", [f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={store.SESSION_SECONDS}",
                                    "lnmt_laika_state=; Path=/api/laika; Max-Age=0; HttpOnly; Secure; SameSite=Lax"])

    def route_laika(self, method, parts, info):
        """Administrators: connect LAIka; everyone: what LAIka builds for their apps."""
        if parts == ["laika"] and method == "GET":
            self.need_admin()
            return call_admin("laika_status")
        if parts == ["laika", "pair"] and method == "POST":
            admin = self.need_admin()
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            return self.change(admin, "laika.pair", "", lambda: call_admin(
                "laika_pair_code", lnmt_url=f"https://{host}", name=store.settings()["server_name"]))
        if parts == ["laika"] and method == "DELETE":
            admin = self.need_admin()
            return self.change(admin, "laika.disconnect", "", lambda: call_admin("laika_disconnect"))
        if parts == ["laika", "sync"] and method == "POST":
            admin = self.need_admin()
            return self.change(admin, "laika.sync", "", sync_laika_users)
        raise ApiError(404, "Not found.")

    def first_setup(self, method):
        if method == "GET":
            return {"needed": store.needs_setup()}
        if not store.needs_setup():
            raise ApiError(409, "Setup is done; sign in.")
        data = self.body()
        if not store.use_setup_code(data.get("code")):
            raise ApiError(403, "Wrong or expired setup code (sudo lnmt-web setup-code makes a new one).")
        try:
            user = store.create_user(data.get("username"), data.get("password"), "admin")
        except ValueError as exc:
            raise ApiError(400, str(exc))
        store.audit(user["username"], self.client_ip(), "setup", user["username"])
        return self.signed_in(user["username"])

    def login(self):
        data = self.body()
        token, error = store.login(data.get("username"), data.get("password"), self.client_ip())
        if not token:
            store.audit((data.get("username") or "")[:40], self.client_ip(), "login", "", False, error)
            raise ApiError(401, error)
        store.audit(store.session(token)["user"], self.client_ip(), "login")
        return self.signed_in(None, token)

    def signed_in(self, username, token=None):
        token = token or store.new_session(username)
        info = store.session(token)
        cookie = f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={store.SESSION_SECONDS}"
        return self.profile(info), [("Set-Cookie", cookie)]

    @staticmethod
    def profile(info):
        return {"username": info["user"], "role": info["role"], "access": info["access"], "csrf": info["csrf"],
                "prefs": info.get("prefs", {}), "settings": store.settings(), "hosting": info.get("hosting"),
                "laika": info.get("laika"), "laika_suggest": info.get("laika_suggest"), "has_password": info.get("has_password")}

    def invite(self, method, token):
        if method == "GET":
            invite = store.peek_invite(token)
            if not invite:
                raise ApiError(404, "This invite link is not valid any more; ask for a new one.")
            return {"role": invite["role"], "expires": invite["expires"], "server_name": store.settings()["server_name"]}
        if method != "POST":
            raise ApiError(405, "Not allowed.")
        data = self.body()
        try:
            user = store.accept_invite(token, data.get("username"), data.get("password"))
        except ValueError as exc:
            raise ApiError(400, str(exc))
        store.audit(user["username"], self.client_ip(), "invite.accept", user["username"])
        return self.signed_in(user["username"])

    def route_me(self, method, parts, info):
        if method == "GET" and len(parts) == 1:
            return self.profile(info)
        if method == "POST" and parts == ["me", "prefs"]:
            return {"prefs": store.set_prefs(info["user"], self.body())}
        if parts == ["me", "ssh-keys"]:
            own = (info.get("hosting") or {}).get("customer")
            if not own:
                raise ApiError(403, "Hosting is not enabled for your account.")
            if method == "GET":
                return {"keys": store.load_users()[info["user"]].get("ssh_keys", [])}
            keys = [str(k).strip() for k in (self.body().get("keys") or []) if str(k).strip()][:20]

            def save():
                result = call_admin("hosting_set_keys", customer=own, keys=keys)
                with store._lock:
                    users = store.load_users()
                    users[info["user"]]["ssh_keys"] = keys
                    store.save_users(users)
                return {**result, "keys": keys}
            return self.change(info, "ssh_keys.update", info["user"], save)
        if parts == ["me", "alerts"]:
            if method == "GET":
                return call_admin("my_alerts_get", username=info["user"])
            d = self.body()
            return self.change(info, "alerts.mine", info["user"], lambda: call_admin(
                "my_alerts_set", username=info["user"], discord_webhook=d.get("discord_webhook"),
                ntfy_url=d.get("ntfy_url"), events=d.get("events")))
        if method == "POST" and parts == ["me", "alerts", "test"]:
            return call_admin("my_alerts_test", username=info["user"])
        if method == "POST" and parts == ["me", "password"]:
            data = self.body()
            users = store.load_users()
            user = users.get(info["user"])
            if not store.check_password(data.get("current") or "", user["hash"]):
                raise ApiError(403, "The current password is wrong.")
            problem = store.valid_password(data.get("password"))
            if problem:
                raise ApiError(400, problem)
            user["hash"] = store.hash_password(data["password"])
            store.save_users(users)
            store.end_sessions_of(info["user"], keep=self.cookie_token())
            store.audit(info["user"], self.client_ip(), "password.change", info["user"])
            return {"ok": True}
        raise ApiError(404, "Not found.")

    def route_sites(self, method, parts, info):
        if len(parts) == 1:
            if method == "GET":
                data = call_admin("list_sites")
                if info["role"] != "admin":
                    data["sites"] = [s for s in data["sites"] if s["hostname"] in info["access"]]
                    data["ddns"] = {}
                for site in data["sites"]:
                    site["level"] = "manage" if info["role"] == "admin" else info["access"].get(site["hostname"])
                return data
            if method == "POST":
                admin = self.need_admin()
                d = self.body()
                return self.change(admin, "site.add", d.get("hostname", ""), lambda: call_admin(
                    "add_site", hostname=str(d.get("hostname", "")), backend_ip=str(d.get("backend_ip", "")),
                    backend_port=str(d.get("backend_port", "")),
                    backend_protocol=d.get("backend_protocol") or "http", force=bool(d.get("force"))))
            raise ApiError(405, "Not allowed.")
        host = parts[1].lower()
        rest = parts[2:]
        if method == "GET" and not rest:
            self.need_site(host, "view")
            detail = call_admin("site_detail", hostname=host)
            detail["level"] = "manage" if info["role"] == "admin" else info["access"].get(host)
            return detail
        if method == "GET" and rest == ["health"]:
            self.need_site(host, "view")
            return call_admin("site_health", hostname=host)
        if method == "POST" and rest == ["test"]:
            self.need_site(host, "view")
            return call_admin("test_backend", hostname=host)
        if method == "GET" and rest == ["traffic"]:
            self.need_site(host, "view")
            return call_admin("site_traffic", hostname=host)
        if method == "DELETE" and not rest:
            admin = self.need_admin()

            def remove():
                result = call_admin("remove_site", hostname=host)
                store.forget_site(host)  # nobody keeps access to a name that may be reused
                return result
            return self.change(admin, "site.remove", host, remove)
        if rest == ["people"]:
            admin = self.need_admin()
            if method == "GET":
                return {"people": store.site_people(host)}
            if method == "POST":
                d = self.body()
                username, level = str(d.get("username", "")), str(d.get("level") or "")

                def grant():
                    try:
                        store.set_site_access(username, host, level)
                    except ValueError as exc:
                        raise ApiError(400, str(exc))
                    return {"people": store.site_people(host)}
                return self.change(admin, "site.access", f"{host}:{username}={level or 'none'}", grant)
        who = self.need_site(host, "manage")
        if method == "POST" and rest == ["enabled"]:
            on = bool(self.body().get("enabled"))
            return self.change(who, "site.enable" if on else "site.disable", host,
                               lambda: call_admin("set_enabled", hostname=host, enabled=on))
        if method == "POST" and rest == ["backend"]:
            d = self.body()
            return self.change(who, "site.backend", host, lambda: call_admin(
                "update_backend", hostname=host, backend_ip=d.get("backend_ip") or None,
                backend_port=d.get("backend_port") or None, backend_protocol=d.get("backend_protocol") or None))
        if method == "POST" and rest == ["tls"]:
            d = self.body()
            return self.change(who, "site.backend_tls", host, lambda: call_admin(
                "update_backend_tls", hostname=host, verify=bool(d.get("verify")), sni=str(d.get("sni") or "")))
        if method == "POST" and rest == ["options"]:
            d = self.body(limit=MAX_BODY * 4)
            args = {k: d[k] for k in ("redirects", "rate_limit", "security_headers", "friendly_errors", "maintenance",
                                      "maintenance_page") if k in d}
            return self.change(who, "site.options", host, lambda: call_admin("site_options", hostname=host, **args))
        if method == "POST" and rest == ["auth"]:
            d = self.body()
            return self.change(who, "site.auth", host, lambda: call_admin(
                "auth_set", hostname=host, enabled=d.get("enabled"), realm=d.get("realm")))
        if method == "POST" and rest == ["auth", "users"]:
            d = self.body()
            return self.change(who, "site.auth_user", f"{host}:{d.get('username', '')}", lambda: call_admin(
                "auth_user_set", hostname=host, username=str(d.get("username", "")),
                password=str(d.get("password", ""))))
        if method == "DELETE" and len(rest) == 3 and rest[:2] == ["auth", "users"]:
            return self.change(who, "site.auth_user_remove", f"{host}:{rest[2]}", lambda: call_admin(
                "auth_user_remove", hostname=host, username=rest[2]))
        raise ApiError(404, "Not found.")

    def route_credentials(self, method, parts, admin):
        if method == "POST" and len(parts) == 1:
            d = self.body()
            return self.change(admin, "credential.add", str(d.get("zone", "")), lambda: call_admin(
                "add_credential", zone=str(d.get("zone", "")), token=str(d.get("token", ""))))
        if method == "GET" and len(parts) == 2:
            return call_admin("zone_detail", zone=parts[1], owner=owner)
        if method == "POST" and len(parts) == 3 and parts[2] == "records":
            d = self.body()
            args = {k: d[k] for k in ("record_id", "name", "content", "ttl", "proxied", "priority", "data") if k in d}
            return self.change(info, "dns.set", f"{parts[1]}:{d.get('type', '')} {d.get('name', '')}", lambda: call_admin(
                "dns_record_set", zone=parts[1], rtype=str(d.get("type", "")), owner=owner, by=info["user"], **args))
        if method == "DELETE" and len(parts) == 4 and parts[2] == "records":
            return self.change(info, "dns.remove", f"{parts[1]}:{parts[3]}", lambda: call_admin(
                "dns_record_delete", zone=parts[1], record_id=parts[3], owner=owner))
        if method == "POST" and len(parts) == 3 and parts[2] == "test":
            return call_admin("test_credential", zone=parts[1])
        if method == "DELETE" and len(parts) == 2:
            return self.change(admin, "credential.remove", parts[1],
                               lambda: call_admin("remove_credential", zone=parts[1]))
        raise ApiError(404, "Not found.")

    def route_invites(self, method, parts, admin):
        if method == "GET" and len(parts) == 1:
            return {"invites": store.list_invites()}
        if method == "POST" and len(parts) == 1:
            d = self.body()

            def make():
                try:
                    token = store.create_invite(d.get("role") or "member", admin["user"], d.get("note") or "")
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                host = self.headers.get("Host") or "this-server:8443"
                return {"url": f"https://{host}/#/invite/{token}", "expires_in_hours": store.INVITE_SECONDS // 3600}
            return self.change(admin, "invite.create", d.get("role") or "member", make)
        if method == "DELETE" and len(parts) == 2:
            return self.change(admin, "invite.revoke", parts[1], lambda: {"revoked": store.revoke_invite(parts[1])})
        raise ApiError(404, "Not found.")

    # --- hosting --------------------------------------------------------------------------

    def customer_for(self, info, customer):
        """Members act only on their own hosting customer; admins on any."""
        own = (info.get("hosting") or {}).get("customer")
        if info["role"] == "admin":
            return customer or own
        if not own:
            raise ApiError(403, "Hosting is not enabled for your account; ask an administrator.")
        if customer and customer != own:
            raise ApiError(404, "No such app.")
        return own

    def route_hosting(self, method, parts, info):
        if parts == ["hosting", "templates"] and method == "GET":
            return call_admin("hosting_templates")
        if parts == ["hosting", "me"] and method == "GET":
            own = self.customer_for(info, None)
            if not own:
                return {"customer": None}
            return {**call_admin("hosting_customer", customer=own), "node_ip": call_admin("hosting_templates")["nodes"][0]["ip"]}
        if parts == ["hosting", "node"] and method == "GET":
            self.need_admin()
            return call_admin("hosting_node")
        if parts[0] != "apps":
            raise ApiError(404, "Not found.")
        if len(parts) == 1:
            if method == "GET":
                customer = None if info["role"] == "admin" else self.customer_for(info, None)
                return call_admin("apps_list", customer=customer)
            if method == "POST":
                d = self.body()
                customer = self.customer_for(info, d.get("customer"))
                if not customer:
                    raise ApiError(400, "Choose whose app this is (enable hosting for a user first).")
                args = {k: d[k] for k in ("name", "template", "version", "image", "port", "env", "memory_mb", "cpus",
                                          "volumes", "command") if k in d and d[k] not in (None, "")}
                return self.change(info, "app.create", f"{customer}/{d.get('name', '')}",
                                   lambda: call_admin("app_create", customer=customer, **args))
            raise ApiError(405, "Not allowed.")
        if len(parts) < 3:
            raise ApiError(404, "Not found.")
        customer, name, rest = self.customer_for(info, parts[1]), parts[2], parts[3:]
        target = f"{customer}/{name}"
        if method == "GET" and not rest:
            return call_admin("app_status", customer=customer, name=name)
        if method == "GET" and rest == ["logs"]:
            return call_admin("app_logs", customer=customer, name=name, lines=300)
        if method == "POST" and rest == ["action"]:
            action = str(self.body().get("action"))
            return self.change(info, f"app.{action}", target,
                               lambda: call_admin("app_action", customer=customer, name=name, action=action))
        if method == "PATCH" and not rest:
            d = self.body()
            args = {k: d[k] for k in ("image", "env", "command", "memory_mb", "cpus", "auto_update") if k in d}
            return self.change(info, "app.update", target,
                               lambda: call_admin("app_update", customer=customer, name=name, **args))
        if method == "DELETE" and not rest:
            keep = self.body().get("keep_data") if self.headers.get("Content-Length") else False
            return self.change(info, "app.remove", target,
                               lambda: call_admin("app_remove", customer=customer, name=name, keep_data=bool(keep)))
        if rest == ["laika"]:
            if method == "GET":
                return call_admin("laika_app", customer=customer, name=name)
            if method == "POST":
                d = self.body()
                users = store.load_users()
                owner = next((u for u, rec in users.items() if (rec.get("hosting") or {}).get("customer") == customer), None)
                for_user = (users.get(owner or "", {}).get("laika") or users[info["user"]].get("laika") or "")
                if not for_user:
                    raise ApiError(400, "Sign in with LAIka once (or link your account under Account) so LAIka knows who the project is for.")
                project = str(d.get("project") or name).strip().lower()
                return self.change(info, "app.laika", f"{target} -> {project}", lambda: call_admin(
                    "laika_build", customer=customer, name=name, project=project, title=str(d.get("title") or name)[:80],
                    template=str(d.get("template") or ""), for_user=for_user))
        if method == "GET" and rest == ["usage"]:
            return call_admin("app_usage", customer=customer, name=name)
        if method == "POST" and rest == ["db"]:
            d = self.body()
            op = str(d.get("op", ""))
            return self.change(info, f"app.db.{op}", target, lambda: call_admin(
                "app_db", customer=customer, name=name, op=op, path=str(d.get("path", ""))))
        if method == "POST" and rest == ["tasks"]:
            d = self.body()
            if d.get("run"):
                return self.change(info, "app.task.run", f"{target}:{d['run']}", lambda: call_admin(
                    "app_tasks", customer=customer, name=name, run=str(d["run"])))
            return self.change(info, "app.tasks", target, lambda: call_admin(
                "app_tasks", customer=customer, name=name, tasks=d.get("tasks") or []))
        if method == "POST" and rest == ["build"]:
            return self.change(info, "app.build", target, lambda: call_admin("app_build", customer=customer, name=name))
        if method == "GET" and rest == ["build"]:
            return call_admin("app_build_status", customer=customer, name=name)
        if method == "POST" and rest == ["files"]:
            d = self.body(limit=MAX_UPLOAD)
            op = str(d.get("op", ""))
            args = {k: d[k] for k in ("path", "content", "to") if k in d}
            if op in ("list", "read"):
                return call_admin("app_files", customer=customer, name=name, op=op, args=args)
            return self.change(info, f"app.files.{op}", f"{target}:{args.get('path', '')}",
                               lambda: call_admin("app_files", customer=customer, name=name, op=op, args=args))
        if method == "POST" and rest == ["publish"]:
            admin = info
            hostname = str(self.body().get("hostname", "")).strip().lower()
            owner = None if info["role"] == "admin" else info["user"]

            def publish():
                result = call_admin("app_publish", customer=customer, name=name, hostname=hostname, owner=owner)
                app_owner = next((u for u, rec in store.load_users().items()
                                  if (rec.get("hosting") or {}).get("customer") == customer and rec.get("role") != "admin"), None)
                if app_owner:
                    store.set_site_access(app_owner, result.get("hostname", hostname), "manage")  # the app's owner manages its site
                return result
            return self.change(admin, "app.publish", f"{target} -> {hostname}", publish)
        raise ApiError(404, "Not found.")

    def route_domains(self, method, parts, info):
        """Cloudflare zones: members see and manage the ones they added (their
        DNS records too); administrators all of them."""
        member = info["role"] != "admin"
        owner = info["user"] if member else None
        if method == "GET" and len(parts) == 1:
            items = call_admin("list_credentials")["credentials"]
            return {"domains": [c for c in items if not member or c.get("owner") == info["user"]]}
        if method == "POST" and len(parts) == 1:
            d = self.body()
            return self.change(info, "domain.add", str(d.get("zone", "")), lambda: call_admin(
                "add_credential", zone=str(d.get("zone", "")), token=str(d.get("token", "")),
                owner=info["user"] if member else str(d.get("owner", ""))))
        if method == "GET" and len(parts) == 2:
            return call_admin("zone_detail", zone=parts[1], owner=owner)
        if method == "POST" and len(parts) == 3 and parts[2] == "records":
            d = self.body()
            args = {k: d[k] for k in ("record_id", "name", "content", "ttl", "proxied", "priority", "data") if k in d}
            return self.change(info, "dns.set", f"{parts[1]}:{d.get('type', '')} {d.get('name', '')}", lambda: call_admin(
                "dns_record_set", zone=parts[1], rtype=str(d.get("type", "")), owner=owner, by=info["user"], **args))
        if method == "DELETE" and len(parts) == 4 and parts[2] == "records":
            return self.change(info, "dns.remove", f"{parts[1]}:{parts[3]}", lambda: call_admin(
                "dns_record_delete", zone=parts[1], record_id=parts[3], owner=owner))
        if method == "POST" and len(parts) == 3 and parts[2] == "test":
            return call_admin("test_credential", zone=parts[1], owner=owner)
        if method == "DELETE" and len(parts) == 2:
            return self.change(info, "domain.remove", parts[1],
                               lambda: call_admin("remove_credential", zone=parts[1], owner=owner))
        raise ApiError(404, "Not found.")

    def route_users(self, method, parts):
        """Accounts are made by invite; here: role, disabled, password reset,
        removal. Site access is set on each site's page."""
        admin = self.need_admin()
        users = store.load_users()
        if method == "GET" and len(parts) == 1:
            try:
                sync_laika_users()
                users = store.load_users()
            except ApiError:
                pass  # LAIka away: the accounts as they are
            return {"users": [store.public_user(n, u) for n, u in sorted(users.items())]}
        if len(parts) == 3 and parts[2] == "hosting" and method == "POST" and parts[1] in users:
            d = self.body()
            current = users[parts[1]].get("hosting") or {}
            try:
                limits = {"disk_gb": int(d.get("disk_gb") or current.get("disk_gb") or 10),
                          "memory_gb": float(d.get("memory_gb") or current.get("memory_gb") or 4),
                          "cpus": float(d.get("cpus") or current.get("cpus") or 2),
                          "tasks": int(d.get("tasks") or current.get("tasks") or 4096)}
            except (TypeError, ValueError):
                raise ApiError(400, "Limits must be numbers.")
            if limits["disk_gb"] < int(current.get("disk_gb") or 0):
                raise ApiError(400, f"The disk can only grow (it is {current['disk_gb']} GB).")

            def enable():
                result = call_admin("hosting_enable", username=parts[1], **limits)
                store.set_hosting(parts[1], result["name"], **limits)
                return result
            summary = f"{limits['disk_gb']} GB disk, {limits['memory_gb']} GB memory, {limits['cpus']} CPUs"
            return self.change(admin, "hosting.limits" if current else "hosting.enable", f"{parts[1]} ({summary})", enable)
        if len(parts) != 2 or parts[1] not in users:
            raise ApiError(404, "No such user.")
        name = parts[1]
        if method == "POST" and len(parts) == 2:
            raise ApiError(405, "Not allowed.")
        if method == "DELETE":
            def remove():
                if name == admin["user"]:
                    raise ApiError(400, "You cannot remove yourself.")
                users.pop(name)
                store.save_users(users)
                store.end_sessions_of(name)
                return {"removed": name}
            return self.change(admin, "user.remove", name, remove)
        if method == "PATCH":
            d = self.body()

            def update():
                user = users[name]
                if "role" in d:
                    if d["role"] not in store.ROLES:
                        raise ApiError(400, "Role must be admin or member.")
                    user["role"] = d["role"]
                if "disabled" in d:
                    user["disabled"] = bool(d["disabled"])
                if d.get("password"):
                    problem = store.valid_password(d["password"])
                    if problem:
                        raise ApiError(400, problem)
                    user["hash"] = store.hash_password(d["password"])
                if not store.admins(users):
                    raise ApiError(400, "At least one active administrator must remain.")
                store.save_users(users)
                if user.get("disabled") or d.get("password") or "role" in d:
                    store.end_sessions_of(name)
                return store.public_user(name, user)
            return self.change(admin, "user.update", name, update)
        raise ApiError(405, "Not allowed.")


def sync_laika_users():
    """LAIka's people become accounts here (when LAIka is connected)."""
    if not call_admin("laika_status")["connected"]:
        return {"made": [], "disabled": []}
    made, disabled = store.sync_laika(call_admin("laika_call", method="GET", path="/api/link/service/users")["users"])
    for name in made:
        store.audit("LAIka", "", "user.from_laika", name)
    return {"made": made, "disabled": disabled}


def _sync_loop():
    while True:
        time.sleep(600)
        try:
            sync_laika_users()
        except Exception as exc:  # LAIka or the root helper away: next time
            sys.stderr.write(f"LAIka sync: {type(exc).__name__}: {exc}\n")


def serve(host="127.0.0.1", port=8090):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=_sync_loop, daemon=True).start()
    print(f"lnmt-web listening on {host}:{port}", flush=True)
    server.serve_forever()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["setup-code"]:
        print("Setup code (valid 1 hour):", store.make_setup_code())
        print("Open the dashboard and create the first administrator with it.")
        return 0
    if argv[:1] == ["reset-password"] and len(argv) == 2:
        import getpass
        users = store.load_users()
        if argv[1] not in users:
            print("No such user.", file=sys.stderr)
            return 1
        password = getpass.getpass("New password: ")
        problem = store.valid_password(password)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        users[argv[1]]["hash"] = store.hash_password(password)
        users[argv[1]]["disabled"] = False
        store.save_users(users)
        print("Password changed.")
        return 0
    if argv[:1] in ([], ["serve"]):
        serve(os.environ.get("LNMT_WEB_HOST", "127.0.0.1"), int(os.environ.get("LNMT_WEB_PORT", "8090")))
        return 0
    print("usage: lnmt-web [serve | setup-code | reset-password USER]", file=sys.stderr)
    return 2
