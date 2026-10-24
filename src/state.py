import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

STATE_FILE = Path("/var/lib/lnmt/state.json")
# One lock for every read-modify-write of state.json and the DNS changes that
# go with it (menu operations, lnmt-ddns), so neither overwrites the other's
# changes and two DDNS runs cannot both create a record.
LOCK_FILE = Path("/run/lock/lnmt.lock")
_held = 0


@contextmanager
def state_lock():
    """Exclusive, re-entrant within this process."""
    global _held
    if _held:
        _held += 1
        try:
            yield
        finally:
            _held -= 1
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _held = 1
        try:
            yield
        finally:
            _held = 0
    finally:
        os.close(fd)

def load_json(path):
    with path.open() as f:
        return json.load(f)


def load_state():
    if not STATE_FILE.exists():
        state = {
            "version": 1,
            "zones": {},
            "proxies": {},
            "ddns": {"records": {}},
        }
        save_state(state)
        return state

    state = load_json(STATE_FILE)

    state.setdefault("version", 1)
    state.setdefault("zones", {})
    state.setdefault("proxies", {})
    state.setdefault("ddns", {})
    state["ddns"].setdefault("records", {})

    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        prefix=".state-",
        dir=str(STATE_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)

    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

