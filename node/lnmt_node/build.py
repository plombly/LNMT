"""lnmt-app-build APP [REV]: build an app's image from its source and redeploy.

Runs as the customer (from the git post-receive hook, or for the dashboard
through the agent). The source is ~/apps/<app>/src (a checkout of the pushed
commit, or files uploaded with the file manager / SFTP). The image is built
with Podman (rootless, 2 GB memory), tagged localhost/<app>:current, and the
app restarted; if the build fails, the running version stays. Every build is
recorded in ~/apps/<app>/build.json and its output in build.log.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    app, rev = argv[0], (argv[1] if len(argv) > 1 else "")
    home = Path(os.path.expanduser("~"))
    folder = home / "apps" / app
    src = folder / "src"
    status = {"app": app, "rev": rev[:12], "started": time.time(), "state": "building"}
    (folder / "build.json").write_text(json.dumps(status))
    log = (folder / "build.log").open("w")
    if not (src / "Dockerfile").exists() and not (src / "Containerfile").exists():
        log.write("No Dockerfile (or Containerfile) at the top of the source.\n")
        log.close()
        status.update(state="failed", finished=time.time(), error="no Dockerfile")
        (folder / "build.json").write_text(json.dumps(status))
        print("Build failed: no Dockerfile at the top of the source.")
        return 1
    tag = f"localhost/{app}:{rev[:12] or int(time.time())}"
    print(f"Building {app} …", flush=True)
    build = subprocess.Popen(["podman", "build", "--memory", "2g", "--pull=newer", "-t", tag, str(src)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(src))
    for line in build.stdout:
        log.write(line)
        sys.stdout.write(line)
    code = build.wait()
    log.close()
    if code != 0:
        status.update(state="failed", finished=time.time(), error=f"podman build exited {code}")
        (folder / "build.json").write_text(json.dumps(status))
        print("Build failed; the running version stays.")
        return 1
    subprocess.run(["podman", "tag", tag, f"localhost/{app}:current"], check=True)
    restart = subprocess.run(["systemctl", "--user", "restart", f"{app}.service"], capture_output=True, text=True)
    status.update(state="deployed" if restart.returncode == 0 else "built", finished=time.time(), image=tag,
                  error=restart.stderr.strip()[-300:] if restart.returncode else "")
    (folder / "build.json").write_text(json.dumps(status))
    print(f"Deployed {tag}." if restart.returncode == 0 else f"Built {tag}, but the restart failed: {status['error']}")
    subprocess.run(["podman", "image", "prune", "-f"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
