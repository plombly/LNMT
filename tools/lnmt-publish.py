#!/usr/bin/env python3
"""Publish an LNMT release to its public repository (maintainer tool).

    tools/lnmt-publish.py VERSION [--push]

1. Exports the tracked files of HEAD, minus EXCLUDE (the private development
   guide), into a separate repository (PUBLIC_DIR) as one commit
   "LNMT <VERSION>" tagged v<VERSION> on top of the previous releases:
   the development history is never published.
2. Scans every exported file for secrets and for the maintainer's private
   patterns (PRIVATE_PATTERNS, never in any repository; on the LAIka host
   /etc/laika/release-forbidden.txt). Any finding stops here.
3. With --push: checks with GitHub who can read the repository (and says so),
   then pushes main and the tag with the deploy key.
"""

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = Path(os.environ.get("LNMT_PUBLIC_DIR", "/root/lnmt-public"))
REMOTE = os.environ.get("LNMT_PUBLIC_REMOTE", "git@github.com:plombly/LNMT.git")
DEPLOY_KEY = os.environ.get("LNMT_DEPLOY_KEY", "/root/.ssh/lnmt_public_deploy")
PRIVATE_PATTERNS = Path(os.environ.get("LNMT_PRIVATE_PATTERNS", "/etc/laika/release-forbidden.txt"))
EXCLUDE = ("CLAUDE.md", "*-todo.md")
ALLOW = "release-scan: allow"
SECRETS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Anthropic key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI key", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ("GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    ("Discord webhook", r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"),
    ("password in a URL", r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{6,}@"),
    ("credential assignment", r"(?i)\b(?:password|passwd|secret|token|api_key)\s*[:=]\s*['\"][A-Za-z0-9/+=_-]{16,}['\"]"),
]


def git(repo, *args, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if result.returncode:
        sys.exit(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def files():
    return [f for f in git(ROOT, "ls-files").splitlines() if not any(fnmatch.fnmatch(f, p) for p in EXCLUDE)]


def patterns():
    found = [(label, re.compile(rx)) for label, rx in SECRETS]
    try:
        lines = PRIVATE_PATTERNS.read_text().splitlines()
    except OSError:
        sys.exit(f"cannot read the private patterns ({PRIVATE_PATTERNS}): refusing to publish unchecked")
    found += [("private pattern", re.compile(line.strip(), re.I)) for line in lines if line.strip() and not line.startswith("#")]
    return found


def scan(base, names):
    findings = []
    checks = patterns()
    for name in names:
        try:
            text = (base / name).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for label, pattern in checks:
                if pattern.search(line):
                    findings.append(f"{name}:{number}: {label}")
    return findings


def visibility():
    owner_repo = re.sub(r"\.git$", "", REMOTE.split(":", 1)[1])
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{owner_repo}", timeout=15) as answer:
            return "public" if not json.load(answer).get("private") else "private"
    except urllib.error.HTTPError as error:
        return "private or missing" if error.code == 404 else f"unknown (HTTP {error.code})"


def main(argv):
    if not argv or not re.fullmatch(r"\d+\.\d+\.\d+", argv[0]):
        sys.exit(__doc__)
    version, push = argv[0], "--push" in argv
    names = files()
    if not (PUBLIC_DIR / ".git").exists():
        PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
        git(PUBLIC_DIR, "init", "-q", "-b", "main")
    for entry in PUBLIC_DIR.iterdir():
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for name in names:
        target = PUBLIC_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    findings = scan(PUBLIC_DIR, names)
    if findings:
        print("\n".join(findings[:50]))
        sys.exit(f"{len(findings)} finding(s): nothing committed or pushed")
    git(PUBLIC_DIR, "add", "-A")
    if git(PUBLIC_DIR, "status", "--porcelain").strip():
        git(PUBLIC_DIR, "-c", "user.name=LNMT", "-c", "user.email=lnmt@users.noreply.github.com",
            "commit", "-q", "-m", f"LNMT {version}")
    git(PUBLIC_DIR, "tag", "-f", f"v{version}")
    print(f"exported {len(names)} files as LNMT {version} in {PUBLIC_DIR}; scan clean")
    if not push:
        return 0
    seen = visibility()
    print(f"{REMOTE} is {seen}")
    env = {**os.environ, "GIT_SSH_COMMAND": f"ssh -i {DEPLOY_KEY} -o IdentitiesOnly=yes"}
    git(PUBLIC_DIR, "push", "-q", REMOTE, "main", f"v{version}", env=env)
    print("pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
