import subprocess
import getpass
import os
import re
import secrets
import shutil
import grp
from pathlib import Path
import copy
from .state import load_state, save_state, state_lock
from .nginx import install_nginx_config
from .common import pause

AUTH_DIR = Path("/etc/nginx/lnmt-auth")
LEGACY_AUTH_DIR = Path("/etc/lnmt/auth")
USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")

def _auth_path(hostname):
    return AUTH_DIR / f"{hostname}.htpasswd"

def _secure_auth_dir():
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    gid = grp.getgrnam("www-data").gr_gid
    os.chown(AUTH_DIR, 0, gid)
    os.chmod(AUTH_DIR, 0o750)
    return gid

def _secure_auth_file(path):
    gid = _secure_auth_dir()
    os.chown(path, 0, gid)
    os.chmod(path, 0o640)

def _migrate_legacy(hostname, proxy):
    """Move/copy legacy auth data to the Nginx-readable dedicated directory.
    Returns True when proxy state was changed.
    """
    target = _auth_path(hostname)
    current = Path(proxy.get("access_protection", {}).get("auth_file", target))
    legacy = LEGACY_AUTH_DIR / f"{hostname}.htpasswd"
    _secure_auth_dir()
    source = current if current.exists() and current != target else legacy
    if not target.exists() and source.exists():
        shutil.copy2(source, target)
    if target.exists():
        _secure_auth_file(target)
    ap = proxy.setdefault("access_protection", {})
    if ap.get("auth_file") != str(target):
        ap["auth_file"] = str(target)
        return True
    return False

def _read_users(path):
    if not path.exists(): return []
    return [ln.split(":",1)[0] for ln in path.read_text().splitlines() if ln and not ln.startswith("#") and ":" in ln]

def _hash_password(password):
    # OpenSSL SHA-512 crypt; nginx supports crypt() hashes.
    r = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password+"\n", text=True, capture_output=True)
    if r.returncode != 0: raise RuntimeError(r.stderr.strip() or "openssl passwd failed")
    return r.stdout.strip()

def _write_user(path, username, password):
    _secure_auth_dir()
    lines=[]
    if path.exists():
        lines=[ln for ln in path.read_text().splitlines() if not ln.startswith(username+":")]
    lines.append(f"{username}:{_hash_password(password)}")
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text("\n".join(lines)+"\n"); _secure_auth_file(tmp); os.replace(tmp,path); _secure_auth_file(path)

def _commit(hostname, mutate):
    # install_nginx_config restores the previous site itself on failure and
    # keeps a disabled proxy disabled; state is saved only after it worked.
    with state_lock():
        state=load_state(); proxy=copy.deepcopy(state["proxies"][hostname])
        mutate(proxy)
        install_nginx_config(proxy)
        state["proxies"][hostname]=proxy; save_state(state)

def access_menu(hostname):
    while True:
        state=load_state(); proxy=state["proxies"][hostname]
        if _migrate_legacy(hostname, proxy): save_state(state)
        ap=proxy.get("access_protection", {})
        path=_auth_path(hostname)
        users=_read_users(path)
        print("\nAccess Protection\n-----------------")
        print(f"Status: {'ENABLED' if ap.get('enabled') else 'DISABLED'}")
        print(f"Realm:  {ap.get('realm','Restricted Service')}")
        print(f"Users:  {len(users)}")
        print("\n1) Enable protection\n2) Add/change user\n3) Remove user\n4) Change login prompt\n5) Disable protection\n0) Back\n")
        c=input("Selection: ").strip()
        if c=="1":
            if not users:
                u=input("Initial username: ").strip()
                if not USER_RE.match(u): print("Invalid username."); pause(); continue
                a=getpass.getpass("Password: "); b=getpass.getpass("Confirm password: ")
                if not a or a!=b: print("Passwords did not match."); pause(); continue
                _write_user(path,u,a)
            def m(p): p["access_protection"]={"enabled":True,"realm":ap.get("realm","Restricted Service"),"auth_file":str(path)}
            try: _commit(hostname,m); print("\nAccess protection enabled.")
            except Exception as e: print(f"\nFAILED: {e}")
            pause()
        elif c=="2":
            u=input("Username: ").strip()
            if not USER_RE.match(u): print("Invalid username."); pause(); continue
            a=getpass.getpass("Password: "); b=getpass.getpass("Confirm password: ")
            if not a or a!=b: print("Passwords did not match."); pause(); continue
            _write_user(path,u,a); print("User saved."); pause()
        elif c=="3":
            u=input("Username to remove: ").strip()
            if u not in users: print("No such user."); pause(); continue
            if ap.get("enabled") and len(users)==1:
                print("That is the last user: disable protection first, or add another user."); pause(); continue
            lines=[ln for ln in path.read_text().splitlines() if not ln.startswith(u+":")]
            tmp=path.with_suffix(path.suffix+".tmp")
            tmp.write_text("\n".join(lines)+( "\n" if lines else "")); _secure_auth_file(tmp); os.replace(tmp,path); _secure_auth_file(path)
            print("User removed."); pause()
        elif c=="4":
            realm=input("Login prompt/realm: ").strip().replace('"',"").replace("$","").replace("\\","")
            if not realm: continue
            def m(p):
                cur=p.setdefault("access_protection",{}); cur.update({"realm":realm,"auth_file":str(path),"enabled":cur.get("enabled",False)})
            try: _commit(hostname,m); print("Prompt updated.")
            except Exception as e: print(f"FAILED: {e}")
            pause()
        elif c=="5":
            def m(p):
                cur=p.setdefault("access_protection",{}); cur.update({"enabled":False,"realm":cur.get("realm","Restricted Service"),"auth_file":str(path)})
            try: _commit(hostname,m); print("\nAccess protection disabled. User hashes retained for re-enable.")
            except Exception as e: print(f"\nFAILED: {e}")
            pause()
        elif c=="0": return
