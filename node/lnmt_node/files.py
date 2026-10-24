"""File operations inside one app's folder, run as the customer inside
`podman unshare` (so files owned by the container's users can be read and
written). Usage: python3 files.py ROOT OP JSON  -> JSON on stdout.

ROOT is the app's folder; every path is relative to it and must stay in it
(no "..", no absolute paths, symlinks never followed out).
"""

import base64
import json
import os
import shutil
import stat
import sys
from pathlib import Path

MAX_READ = 2 * 1024 * 1024


def resolve(root, rel):
    rel = (rel or "").strip("/")
    if any(part in ("..", "") for part in rel.split("/")) and rel:
        raise ValueError("bad path")
    path = Path(root, rel) if rel else Path(root)
    real = Path(os.path.realpath(path))
    if real != Path(root) and Path(root) not in real.parents:
        raise ValueError("that path leaves the app's folder")
    return path


def run(root, op, args):
    root = os.path.realpath(root)
    path = resolve(root, args.get("path", ""))
    if op == "list":
        items = []
        for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            info = entry.lstat()
            items.append({"name": entry.name, "dir": stat.S_ISDIR(info.st_mode), "size": info.st_size,
                          "modified": info.st_mtime, "link": stat.S_ISLNK(info.st_mode)})
        return {"path": args.get("path", ""), "items": items}
    if op == "read":
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a file")
        if path.stat().st_size > MAX_READ:
            raise ValueError("files over 2 MB: use SFTP")
        return {"content": base64.b64encode(path.read_bytes()).decode()}
    if op == "write":
        if path.is_symlink() or path.is_dir():
            raise ValueError("not a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".upload")
        tmp.write_bytes(base64.b64decode(args["content"]))
        os.replace(tmp, path)
        return {"written": args.get("path")}
    if op == "mkdir":
        path.mkdir(parents=True, exist_ok=True)
        return {"created": args.get("path")}
    if op == "delete":
        if Path(os.path.realpath(path)) == Path(root) or args.get("path", "").strip("/") in ("data", "src"):
            raise ValueError("that folder cannot be deleted")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"deleted": args.get("path")}
    if op == "rename":
        target = resolve(root, args["to"])
        if target.exists():
            raise ValueError("something with that name exists")
        os.rename(path, target)
        return {"renamed": args.get("to")}
    raise ValueError("unknown file operation")


if __name__ == "__main__":
    try:
        result = {"ok": True, "result": run(sys.argv[1], sys.argv[2], json.loads(sys.stdin.read() or "{}"))}
    except (OSError, ValueError, KeyError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result))
