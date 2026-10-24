"""lnmt-customer-shell: the login shell of every hosting customer.

sshd runs it as `lnmt-customer-shell -c "<command>"` for each SSH request
(authorized_keys lines carry "restrict": no terminal, no forwarding). Only
two things are allowed:
  git push / fetch   git-receive-pack|git-upload-pack '<app>.git'
                     -> git-shell on ~/git/<app>.git (a push builds and
                        redeploys the app: the repository's post-receive hook)
  SFTP               the sftp subsystem, starting in the customer's apps folder
Anything else, including an interactive login, is refused. A deploy key
(authorized_keys command="lnmt-customer-shell --deploy APP") may only push
to and fetch APP's repository.
"""

import os
import re
import sys

GIT = re.compile(r"^(git-receive-pack|git-upload-pack|git-upload-archive) '?/?(?:git/)?([a-z][a-z0-9-]{1,30})(?:\.git)?/?'?$")
SFTP = ("internal-sftp", "/usr/lib/openssh/sftp-server", "/usr/lib/sftp-server")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    home = os.path.expanduser("~")
    if len(argv) != 2 or argv[0] != "-c":
        sys.stderr.write("This account only accepts git push and SFTP.\n")
        return 1
    command = argv[1].strip()
    deploy = re.fullmatch(r"\S*lnmt-customer-shell --deploy ([a-z][a-z0-9-]{1,30})", command)
    if deploy:
        # A deploy key (authorized_keys command=): git push/fetch of that one app only.
        match = GIT.match(os.environ.get("SSH_ORIGINAL_COMMAND", "").strip())
        if not match or match.group(2) != deploy.group(1):
            sys.stderr.write(f"This key may only push to and fetch {deploy.group(1)}.git.\n")
            return 1
        verb, app = match.groups()
        os.execvp("git-shell", ["git-shell", "-c", f"{verb} '{os.path.join(home, 'git', f'{app}.git')}'"])
    match = GIT.match(command)
    if match:
        verb, app = match.groups()
        repo = os.path.join(home, "git", f"{app}.git")
        if not os.path.isdir(repo):
            sys.stderr.write(f"No git repository for an app named {app}.\n")
            return 1
        os.execvp("git-shell", ["git-shell", "-c", f"{verb} '{repo}'"])
    if command.split(" ")[0] in SFTP:
        os.execv("/usr/lib/openssh/sftp-server", ["sftp-server", "-d", os.path.join(home, "apps")])
    sys.stderr.write("This account only accepts git push and SFTP.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
