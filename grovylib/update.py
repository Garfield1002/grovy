"""`grovy update`: bring this grovy folder up to date with its upstream branch, then restart the daemon.

Safe by construction: it only fast-forwards (a branch with commits of its own is left alone, with what to do), it
refuses with uncommitted changes to tracked files, it shows the incoming commits and asks first, and it checks the
new code imports (the hooks and the daemon) before restarting anything: if not, it goes back to the commit it came
from. Restarting never touches Claude sessions: the `grovy` (and `grovy-tunnel`) services, or a daemon started by
hand in a tmux pane (respawned with its own command line). Sessions started before keep their hooks settings until
⋯ → Update session (grovy shows it when they're out of date).
"""
import os
import subprocess
import sys
import time

from . import client, core, terminal, tui


def git(*args, timeout=60, check=True):
    r = subprocess.run(["git", "-C", core.HERE, *args], capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def imports_ok():
    """Whether the code now on disk loads: the hooks (every session runs them) and the daemon."""
    r = subprocess.run([sys.executable, "-c", "import grovy; from grovylib import hook; import grovyd"],
                       cwd=core.HERE, capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or "").strip()[-600:]


def _active(unit):
    try:
        return subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True,
                              timeout=10).stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _status():
    try:
        return client.sock_request({"type": "status"}, timeout=2.0) or {}
    except (OSError, ValueError):
        return {}


def daemon_pane(pid):
    """(target, start command) of the tmux pane the daemon runs in (started by hand: `tmux new -s grovyd ...`), or
    None. The pane's own process is the daemon once its shell has exec'd it."""
    if not pid:
        return None
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_start_command}"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    import shlex
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[1] == str(pid) and parts[2]:
            try:   # tmux shows the start command shell-quoted: back to its arguments, or respawn runs one bad name
                argv = shlex.split(parts[2])
            except ValueError:
                return None
            return (parts[0], argv) if argv else None
    return None


def plan_restart():
    """[(label, action)] restarting what runs this code; plus a note when nothing can be restarted for you."""
    steps, note = [], None
    st = _status()
    service = _active("grovy")
    if service:
        steps.append(("restart the grovy service (Claude sessions keep running)",
                      lambda: _run(["systemctl", "--user", "restart", "grovy"])))
    elif st:
        pane = daemon_pane(st.get("pid"))
        if pane:
            target, argv = pane
            steps.append((f"restart the grovy daemon in its tmux pane {target} (Claude sessions keep running)",
                          lambda: _run(["tmux", "respawn-pane", "-k", "-t", target, *argv])))
        else:
            note = "grovy is running outside its service: restart it yourself to use the new version"
    if _active("grovy-tunnel"):
        steps.append(("restart the tunnel to the trunk", lambda: _run(["systemctl", "--user", "restart", "grovy-tunnel"])))
    return steps, note


def _run(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"{argv[0]} failed")


def _wait_daemon(seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _status():
            return True
        time.sleep(0.3)
    return False


def cmd_update(a):
    tui.AUTO = bool(a.yes)
    try:
        return _update(a)
    except KeyboardInterrupt:
        tui.say("\nStopped: nothing was changed after that point.")
        return 130
    finally:
        tui.AUTO = False


def _update(a):
    tui.say(tui.paint("Update grovy", "bold") + tui.paint(f"  ({tui.short(core.HERE)})", "dim"))
    try:
        if git("rev-parse", "--is-inside-work-tree") != "true":
            raise RuntimeError
    except (RuntimeError, OSError, subprocess.SubprocessError):
        core.die(f"{core.HERE} isn't a git checkout: update it the way you installed it")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    try:
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except RuntimeError:
        core.die(f"the branch {branch} follows no upstream branch, so there's nothing to update from "
                 f"(git branch --set-upstream-to origin/main, for one)")
    if git("status", "--porcelain", "--untracked-files=no"):
        core.die("there are uncommitted changes in the grovy folder: commit or stash them first (git status)")
    tui.note(f"  fetching {upstream}…")
    try:
        git("fetch", "--quiet", timeout=120)
    except (RuntimeError, subprocess.SubprocessError) as e:
        core.die(f"git fetch: {e}")
    ahead, behind = (int(x) for x in git("rev-list", "--left-right", "--count", f"HEAD...{upstream}").split())
    if not behind:
        tui.ok(f"Up to date ({branch}, {git('rev-parse', '--short', 'HEAD')})" +
               (f"; {ahead} commit(s) of your own not upstream" if ahead else ""))
        return 0
    if ahead:
        core.die(f"{branch} has {ahead} commit(s) of its own and {upstream} has {behind} new one(s): update it "
                 f"yourself (git pull --rebase), then restart grovy")
    new = git("log", "--oneline", "--no-decorate", f"HEAD..{upstream}").splitlines()
    tui.say()
    tui.say(tui.paint(f"{behind} new commit(s) on {upstream}:", "bold"))
    for line in new[:15]:
        tui.say(f"  • {terminal.clean(line)}")
    if len(new) > 15:
        tui.note(f"  … and {len(new) - 15} more (git log HEAD..{upstream})")
    restart, note = plan_restart() if not a.no_restart else ([], None)
    tui.say()
    tui.say(tui.paint("Will:", "bold"))
    tui.say(f"  • fast-forward {branch} to {upstream}, and check the new code loads (else go back)")
    for label, _ in restart:
        tui.say(f"  • {label}")
    if a.check:
        tui.note("(--check: nothing changed)")
        return 0
    if not tui.AUTO and not tui.interactive():
        tui.note("(no terminal to ask: nothing changed; run it in a terminal, or with -y)")
        return 2
    if not tui.confirm("Update?", True):
        tui.say("Nothing changed.")
        return 1
    old = git("rev-parse", "HEAD")
    git("merge", "--ff-only", "--quiet", upstream)
    tui.ok(f"{branch}: {old[:7]} → {git('rev-parse', '--short', 'HEAD')}")
    good, err = imports_ok()
    if not good:
        git("reset", "--keep", "--quiet", old)
        tui.fail("the new code doesn't load, so grovy went back to the commit it came from:")
        tui.say("  " + err.replace("\n", "\n  "))
        return 1
    tui.ok("the new code loads (hooks and daemon)")
    for label, action in restart:
        try:
            action()
        except Exception as e:   # the code is updated either way: say what to restart by hand
            tui.fail(f"{label}: {e}")
            return 1
        tui.ok(label)
    if restart and not _wait_daemon():
        tui.warn("grovy isn't answering after the restart: grovy doctor, or journalctl --user -u grovy")
        return 1
    if note:
        tui.warn(note)
    elif not restart and not a.no_restart:
        tui.note("grovy isn't running here: it starts with the new version.")
    tui.note("Sessions started before keep their hook settings until ⋯ → Update session (grovy flags them).")
    return 0


def add_parser(sub):
    s = sub.add_parser("update", help="update this grovy folder (git, fast-forward only) and restart the daemon")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask")
    s.add_argument("--check", action="store_true", help="only show what's new")
    s.add_argument("--no-restart", action="store_true", help="update the code, restart nothing")
    s.set_defaults(fn=cmd_update)
