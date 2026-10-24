"""The dashboard: sign-in, CSRF, roles and per-site access, audit, and the
root helper's request checks. The root side is faked."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("LNMT_WEB_DATA", str(tmp_path))
    import importlib
    from lnmt.web import store, server
    importlib.reload(store)
    importlib.reload(server)
    calls = []
    sites = {"sites": [{"hostname": "a.example.com"}, {"hostname": "b.example.com"}], "ddns": {"public_ip": "1.2.3.4"}}

    def fake_admin(action, /, **args):
        calls.append((action, args))
        if action == "list_sites":
            return json.loads(json.dumps(sites))
        if action == "site_detail":
            return {"hostname": args["hostname"]}
        if action == "list_credentials":
            return {"credentials": [{"zone": "example.com", "owner": "sam", "sites": []},
                                    {"zone": "other.com", "owner": "", "sites": []}]}
        return {"ok": True}

    monkeypatch.setattr(server, "call_admin", fake_admin)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    class Client:
        def __init__(self):
            self.cookie, self.csrf = "", ""

        def request(self, method, path, data=None, csrf=True):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Content-Type": "application/json"}
            if self.cookie:
                headers["Cookie"] = self.cookie
            if csrf and self.csrf:
                headers["X-CSRF-Token"] = self.csrf
            conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
            response = conn.getresponse()
            body = json.loads(response.read() or b"{}")
            cookie = response.getheader("Set-Cookie")
            if cookie:
                self.cookie = cookie.split(";")[0]
            if isinstance(body, dict) and body.get("csrf"):
                self.csrf = body["csrf"]
            return response.status, body

    Client.port = port
    yield store, Client, calls
    httpd.shutdown()


def test_first_administrator_needs_the_setup_code(web):
    store, Client, _ = web
    c = Client()
    assert c.request("GET", "/api/setup")[1] == {"needed": True}
    assert c.request("POST", "/api/setup", {"code": "nope", "username": "alex", "password": "x" * 12})[0] == 403
    code = store.make_setup_code()
    status, body = c.request("POST", "/api/setup", {"code": code, "username": "alex", "password": "correct horse 1"})
    assert status == 200 and body["role"] == "admin" and c.cookie.startswith("lnmt_session=")
    assert c.request("POST", "/api/setup", {"code": code, "username": "x", "password": "x" * 12})[0] == 409
    assert c.request("GET", "/api/me")[1]["username"] == "alex"


def test_sign_in_rate_limit_and_csrf(web):
    store, Client, calls = web
    store.create_user("alex", "correct horse 1", "admin")
    c = Client()
    for _ in range(5):
        assert c.request("POST", "/api/login", {"username": "alex", "password": "wrong"})[0] == 401
    assert "Too many" in c.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})[1]["detail"]
    other = Client()
    store._failures.clear()
    assert other.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})[0] == 200
    assert other.request("POST", "/api/ddns/run", csrf=False)[0] == 403          # no CSRF token
    assert other.request("POST", "/api/ddns/run")[0] == 200
    assert calls[-1][0] == "ddns_run"
    assert [e["action"] for e in store.read_audit()][:2] == ["ddns.run", "login"]


def test_members_see_and_change_only_their_sites(web):
    store, Client, calls = web
    store.create_user("sam", "member pass 12", "member", {"a.example.com": "manage", "b.example.com": "view"})
    c = Client()
    c.request("POST", "/api/login", {"username": "sam", "password": "member pass 12"})
    listed = c.request("GET", "/api/sites")[1]
    assert [s["hostname"] for s in listed["sites"]] == ["a.example.com", "b.example.com"] and listed["ddns"] == {}
    assert c.request("POST", "/api/sites/a.example.com/enabled", {"enabled": False})[0] == 200
    assert c.request("POST", "/api/sites/b.example.com/enabled", {"enabled": False})[0] == 403
    assert c.request("GET", "/api/sites/c.example.com")[0] == 404                 # never told it exists
    for path in ("/api/system", "/api/credentials", "/api/users", "/api/audit", "/api/backups"):
        assert c.request("GET", path)[0] == 403, path
    assert c.request("POST", "/api/sites", {"hostname": "x.example.com"})[0] == 403
    assert c.request("DELETE", "/api/sites/a.example.com")[0] == 403


def test_invites_make_accounts_and_admins_stay(web):
    store, Client, _ = web
    store.create_user("alex", "correct horse 1", "admin")
    c = Client()
    c.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})
    assert c.request("POST", "/api/users", {"username": "sam", "password": "long enough 1"})[0] == 404   # no direct accounts
    status, made = c.request("POST", "/api/invites", {"role": "member", "note": "Sam"})
    assert status == 200 and "/#/invite/" in made["url"]
    token = made["url"].rsplit("/", 1)[1]
    assert c.request("GET", "/api/invites")[1]["invites"][0]["note"] == "Sam"
    sam = Client()
    assert sam.request("GET", f"/api/invite/{token}")[1]["role"] == "member"
    assert sam.request("POST", f"/api/invite/{token}", {"username": "sam", "password": "short"})[0] == 400
    status, body = sam.request("POST", f"/api/invite/{token}", {"username": "sam", "password": "long enough 1"})
    assert status == 200 and body["role"] == "member" and body["access"] == {}
    assert Client().request("POST", f"/api/invite/{token}", {"username": "eve", "password": "long enough 1"})[0] == 400  # once
    assert c.request("PATCH", "/api/users/alex", {"role": "member"})[0] == 400
    assert c.request("DELETE", "/api/users/alex")[0] == 400
    assert c.request("PATCH", "/api/users/sam", {"disabled": True})[0] == 200
    assert sam.request("GET", "/api/me")[0] == 401                                # signed out at once
    assert "hash" not in json.dumps(c.request("GET", "/api/users")[1])
    other = c.request("POST", "/api/invites", {"role": "admin"})[1]["url"].rsplit("/", 1)[1]
    short = c.request("GET", "/api/invites")[1]["invites"][0]["id"]
    assert c.request("DELETE", f"/api/invites/{short}")[1] == {"revoked": True}
    assert Client().request("GET", f"/api/invite/{other}")[0] == 404


def test_site_access_is_set_on_the_site_and_takes_effect_at_once(web):
    store, Client, calls = web
    store.create_user("alex", "correct horse 1", "admin")
    store.create_user("sam", "member pass 12", "member")
    admin, sam = Client(), Client()
    admin.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})
    sam.request("POST", "/api/login", {"username": "sam", "password": "member pass 12"})
    assert sam.request("GET", "/api/sites/a.example.com")[0] == 404
    people = admin.request("POST", "/api/sites/a.example.com/people", {"username": "sam", "level": "view"})[1]["people"]
    assert {p["username"]: p["level"] for p in people} == {"alex": "manage", "sam": "view"}
    assert sam.request("GET", "/api/sites/a.example.com")[0] == 200          # no new sign-in needed
    assert sam.request("POST", "/api/sites/a.example.com/people", {"username": "sam", "level": "manage"})[0] == 403
    admin.request("POST", "/api/sites/a.example.com/people", {"username": "sam", "level": ""})
    assert sam.request("GET", "/api/sites/a.example.com")[0] == 404
    admin.request("POST", "/api/sites/a.example.com/people", {"username": "sam", "level": "manage"})
    admin.request("DELETE", "/api/sites/a.example.com")
    assert store.load_users()["sam"]["access"] == {}                          # removed sites are forgotten


def test_preferences_and_server_name(web):
    store, Client, _ = web
    store.create_user("alex", "correct horse 1", "admin")
    store.create_user("sam", "member pass 12", "member")
    c, sam = Client(), Client()
    c.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})
    sam.request("POST", "/api/login", {"username": "sam", "password": "member pass 12"})
    saved = c.request("POST", "/api/me/prefs", {"theme": "light", "accent": "#3FCF8E", "density": "loud", "x": 1})[1]
    assert saved == {"prefs": {"theme": "light", "accent": "#3fcf8e"}}
    assert c.request("GET", "/api/me")[1]["prefs"]["theme"] == "light"
    assert sam.request("POST", "/api/settings", {"server_name": "Mine"})[0] == 403
    assert c.request("POST", "/api/settings", {"server_name": "Home edge"})[1]["server_name"] == "Home edge"
    assert Client().request("GET", "/api/settings")[1]["server_name"] == "Home edge"


def test_passwords_are_scrypt_and_checked_in_constant_time(web):
    store, _, _ = web
    stored = store.hash_password("abc")
    assert stored.startswith("scrypt$") and store.check_password("abc", stored) and not store.check_password("abd", stored)


def test_the_root_helper_runs_only_known_actions_with_their_own_arguments(monkeypatch):
    from lnmt import admin, service
    monkeypatch.setitem(service.ACTIONS, "set_enabled", (lambda hostname, enabled: {"h": hostname, "e": enabled}, True))
    assert admin.handle('{"action": "set_enabled", "args": {"hostname": "a.example.com", "enabled": true}}') == {
        "ok": True, "result": {"h": "a.example.com", "e": True}}
    assert admin.handle('{"action": "rm_rf", "args": {}}')["error"] == "unknown action"
    assert "unknown argument" in admin.handle('{"action": "set_enabled", "args": {"hostname": "a", "enabled": 1, "cmd": "x"}}')["error"]
    assert admin.handle("not json")["error"] == "bad request"

    def boom(hostname, enabled):
        raise KeyError("secret path")
    monkeypatch.setitem(service.ACTIONS, "set_enabled", (boom, True))
    assert admin.handle('{"action": "set_enabled", "args": {"hostname": "a", "enabled": 1}}') == {
        "ok": False, "error": "set_enabled failed: KeyError"}


def test_static_pages_carry_a_strict_csp(web):
    _, Client, _ = web
    import http.client as hc
    from lnmt.web import server
    assert "style-src 'self'" in open(server.STATIC.parent / "server.py").read()
    for name in ("index.html", "styles.css", "js/main.js"):
        assert (server.STATIC / name).exists()
    for module in (server.STATIC / "js").glob("*.js"):
        assert 'style="' not in module.read_text(), module   # inline styles would be blocked


def test_sessions_survive_a_restart_and_store_no_tokens(web, tmp_path):
    store, Client, _ = web
    import importlib
    store.create_user("alex", "correct horse 1", "admin")
    c = Client()
    c.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})
    token = c.cookie.split("=", 1)[1]
    saved = (tmp_path / "sessions.json").read_text()
    assert token not in saved
    importlib.reload(store)                       # a restart: memory is gone
    assert store.session(token)["user"] == "alex"
    store.end_session(token)
    importlib.reload(store)
    assert store.session(token) is None


def test_members_reach_only_their_own_apps_and_cannot_publish(web, monkeypatch):
    store, Client, calls = web
    store.create_user("alex", "correct horse 1", "admin")
    store.create_user("sam", "member pass 12", "member")
    store.create_user("eve", "member pass 12", "member")
    store.set_hosting("sam", "sam", 10)
    sam, eve, admin = Client(), Client(), Client()
    sam.request("POST", "/api/login", {"username": "sam", "password": "member pass 12"})
    eve.request("POST", "/api/login", {"username": "eve", "password": "member pass 12"})
    admin.request("POST", "/api/login", {"username": "alex", "password": "correct horse 1"})
    assert sam.request("POST", "/api/apps", {"name": "shop", "template": "php", "customer": "eve"})[0] == 404
    assert sam.request("POST", "/api/apps", {"name": "shop", "template": "php"})[0] == 200
    assert calls[-1] == ("app_create", {"customer": "sam", "name": "shop", "template": "php"})   # their own customer
    assert sam.request("GET", "/api/apps/other/shop")[0] == 404
    assert sam.request("POST", "/api/apps/sam/shop/action", {"action": "restart"})[0] == 200
    assert sam.request("POST", "/api/apps/sam/shop/publish", {"hostname": "x.example.com"})[0] == 200
    assert calls[-1] == ("app_publish", {"customer": "sam", "name": "shop", "hostname": "x.example.com", "owner": "sam"})
    assert [d["zone"] for d in sam.request("GET", "/api/domains")[1]["domains"]] == ["example.com"]   # only theirs
    assert eve.request("GET", "/api/domains")[1]["domains"] == []        # members without hosting: their own domains (none)
    eve.request("GET", "/api/domains/example.com")
    assert calls[-1] == ("zone_detail", {"zone": "example.com", "owner": "eve"})   # the helper refuses others' domains
    sam.request("POST", "/api/domains/example.com/records", {"type": "A", "name": "www", "content": "1.2.3.4"})
    assert calls[-1] == ("dns_record_set", {"zone": "example.com", "rtype": "A", "owner": "sam", "by": "sam",
                                            "name": "www", "content": "1.2.3.4"})
    admin.request("DELETE", "/api/domains/example.com/records/" + "a" * 32)
    assert calls[-1] == ("dns_record_delete", {"zone": "example.com", "record_id": "a" * 32, "owner": None})
    assert eve.request("POST", "/api/apps", {"name": "x", "template": "php"})[0] == 403          # no hosting
    assert eve.request("GET", "/api/hosting/node")[0] == 403
    assert admin.request("GET", "/api/apps/sam/shop")[0] == 200


def test_laika_people_become_accounts_and_sign_in_with_laika(web, monkeypatch):
    from lnmt.web import server, store
    calls = []
    laika_people = [{"name": "devyn", "username": "Devyn", "role": "member", "disabled": False},
                    {"name": "alex", "username": "Alex", "role": "admin", "disabled": False}]

    def fake_admin(action, /, **args):
        calls.append((action, args))
        if action == "laika_status":
            return {"connected": True, "url": "http://laika.lan:8080"}
        if action == "laika_call" and args["path"] == "/api/link/service/users":
            return {"users": laika_people}
        if action == "laika_call" and args["path"] == "/api/link/service/token":
            return {"name": "devyn", "username": "Devyn", "role": "member"} if args["body"]["code"] == "good-code" else (_ for _ in ()).throw(server.ApiError(400, "expired"))
        if action == "laika_check":
            return {"ok": args["token"] == "lnmtlink_" + "x" * 30}
        if action == "apps_list":
            return {"apps": [{"name": "site", "build": True, "image": "wordpress"}, {"name": "db", "build": False}]}
        if action == "hosting_templates":
            return {"nodes": [{"ip": "192.0.2.121"}]}
        return {"ok": True}
    monkeypatch.setattr(server, "call_admin", fake_admin)
    store.create_user("alex", "a long password 1", "admin")
    made = server.sync_laika_users()["made"]
    users = store.load_users()
    assert made == ["devyn"] and users["devyn"]["laika"] == "devyn" and users["devyn"]["hash"] == ""
    assert users["alex"].get("laika") is None and users["alex"]["laika_suggest"] == "alex"   # made here: never taken over
    assert store.login("devyn", "", "1.2.3.4")[0] is None                                           # no password sign-in
    # the round trip: LNMT -> LAIka (#/connect) -> back with a code
    status, headers = _raw(web, "GET", "/api/laika/login")
    assert status == 302 and headers["Location"].startswith("http://laika.lan:8080/#/connect?state=")
    state = headers["Location"].split("state=")[1]
    cookie = headers["Set-Cookie"].split(";")[0]
    bad, _ = _raw(web, "GET", f"/api/laika/return?code=good-code&state={state}")                # no state cookie
    assert bad == 302 and _["Location"] == "/#/login-failed"
    status, _ = _raw(web, "GET", "/api/laika/login")
    state = _["Location"].split("state=")[1]
    cookie = _["Set-Cookie"].split(";")[0]
    status, back = _raw(web, "GET", f"/api/laika/return?code=good-code&state={state}", {"Cookie": cookie})
    assert status == 302 and back["Location"] == "/#/sites" and back["Set-Cookie"].startswith("lnmt_session=")
    # LAIka's own calls need its token
    assert _raw(web, "GET", "/api/link/apps?user=devyn")[0] == 401
    store.set_hosting("devyn", "devyn", 10)
    status, _ = _raw(web, "GET", "/api/link/apps?user=devyn", {"Authorization": "Bearer lnmtlink_" + "x" * 30})
    assert status == 200


def _raw(web, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", web[1].port, timeout=10)
    conn.request(method, path, headers=headers or {})
    answer = conn.getresponse()
    answer.read()
    result = {}
    for name, value in answer.getheaders():
        result.setdefault(name, value)
    return answer.status, result
