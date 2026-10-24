"""lnmt-admin: the root side of the web dashboard.

The dashboard runs as the unprivileged user lnmt-web and cannot touch nginx,
state or credentials. It sends one JSON request per connection to the unix
socket /run/lnmt-admin.sock (systemd socket activation, Accept=yes, socket
root:lnmt-web 0660); systemd starts this program as root with the connection
on stdin/stdout:

    request:  {"action": "set_enabled", "args": {"hostname": "...", "enabled": true}}
    answer:   {"ok": true, "result": {...}}  or  {"ok": false, "error": "..."}

Only the actions in service.ACTIONS exist, with exactly their own argument
names. Requests are never logged (they may carry a token or a password).
"""

import inspect
import json
import sys

from . import service

MAX_REQUEST = 16 * 1024 * 1024  # file uploads (base64) for hosted apps


def handle(raw):
    try:
        request = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "bad request"}
    if not isinstance(request, dict):
        return {"ok": False, "error": "bad request"}
    action, args = request.get("action"), request.get("args") or {}
    if action not in service.ACTIONS or not isinstance(args, dict):
        return {"ok": False, "error": "unknown action"}
    function, _ = service.ACTIONS[action]
    allowed = set(inspect.signature(function).parameters)
    if set(args) - allowed:
        return {"ok": False, "error": f"unknown argument(s): {', '.join(sorted(set(args) - allowed))}"}
    try:
        return {"ok": True, "result": function(**args)}
    except service.ServiceError as exc:
        return {"ok": False, "error": str(exc)}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # never a traceback (could hold paths/values) to the page
        print(f"lnmt-admin {action}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"ok": False, "error": f"{action} failed: {type(exc).__name__}"}


def main():
    raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        answer = {"ok": False, "error": "request too large"}
    else:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # anything the reused code prints stays out of the answer
        try:
            answer = handle(raw.decode("utf-8", "replace"))
        finally:
            sys.stdout = real_stdout
    real = json.dumps(answer, default=str) + "\n"
    sys.stdout.write(real)
    sys.stdout.flush()
    return 0
