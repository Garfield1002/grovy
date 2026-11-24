"""`grovy update`: bring this grovy folder up to date with its upstream branch, then restart the daemon.

Safe by construction: it only fast-forwards (a branch with commits of its own is left alone, with what to do), it
refuses with uncommitted changes to tracked files, it shows the incoming commits and asks first, and it checks the
new code imports (the hooks and the daemon) before restarting anything: if not, it goes back to the commit it came
from. Restarting never touches Claude sessions: the `grovy` (and `grovy-tunnel`) services, or a daemon started by
hand in a tmux pane (respawned with its own command line). Sessions started before keep their hooks settings until
⋯ → Update session (grovy shows it when they're out of date).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from . import client, core, terminal, tui


# ---------------------------------------------------------------- "is grovy up to date?" (GitHub tagged releases)
# A passive check, separate from `grovy update` (which fast-forwards the git upstream): the daemon asks GitHub at most
# once a day for the newest tag of grovy's repo, caches it, and the phone's Settings sheet shows it (extensions/update.js).
# Opt out with GROVY_UPDATE_CHECK=off; check another repo's tags with GROVY_UPDATE_REPO=owner/repo.
DEFAULT_REPO = "Garfield1002/grovy"       # grovy's own GitHub repo
CHECK_EVERY = 24 * 3600                    # at most one GitHub request a day
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_REPO_RE = re.compile(r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*")   # owner/repo; each part starts alnum, so no "..", no
#                                                                   extra path segments or query — can't reshape the URL


def check_enabled():
    """Whether the daily GitHub version check runs (GROVY_UPDATE_CHECK, on by default)."""
    return core.env("UPDATE_CHECK", "on").strip().lower() not in ("0", "off", "no", "false")


def check_repo():
    """The owner/repo whose tags to compare against, or None if it is set to something that isn't one."""
    repo = (core.env("UPDATE_REPO") or DEFAULT_REPO).strip().strip("/")
    return repo if _REPO_RE.fullmatch(repo) else None


def _semver(tag):
    m = _SEMVER.match((tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def current_version():
    """The newest tag reachable from HEAD (e.g. "v1.9.2"), or None if this isn't a git checkout with tags."""
    try:
        return git("describe", "--tags", "--abbrev=0") or None
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def fetch_latest(repo, timeout=10):
    """The newest semver tag name among owner/repo's 100 most recent tags on GitHub, or None if none are semver.
    Raises on a network error. Only the public tags API is called; the User-Agent is generic so nothing about the
    user is sent."""
    url = f"https://api.github.com/repos/{repo}/tags?per_page=100"
    req = urllib.request.Request(url, headers={"User-Agent": "grovy", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (hard-coded https host)
        data = json.load(r)
    best, name = None, None
    for t in data if isinstance(data, list) else []:
        v = _semver(t.get("name") if isinstance(t, dict) else None)
        if v and (best is None or v > best):
            best, name = v, t["name"]
    return name


def _cache_path():
    return os.path.join(core.state_dir(), "update.json")


_WRITE = threading.Lock()   # the reconcile loop and a "Check now" POST both write update.json; serialize those writes


def _save(rec):
    with _WRITE:   # core.atomic_write_json's temp file is per process, not per thread: don't let two writers collide
        core.atomic_write_json(_cache_path(), rec)


def check_for_update(force=False):
    """The daemon's daily check: fetch the newest tag and cache it. Never raises; any failure is kept in the cache
    so the UI can say the check failed. Does nothing (and leaves no cache) when the check is opted out. The fetch
    runs on the caller's thread (so the once-a-day fetch can hold the reconcile loop for up to the timeout) but
    outside the write lock, so it never blocks a concurrent "Check now"."""
    if not check_enabled():
        return
    repo = check_repo()
    if not repo:
        return _save({"error": "GROVY_UPDATE_REPO isn't an owner/repo", "checked": core.now()})
    old = core.read_json(_cache_path(), {})
    seen = old.get("checked") if isinstance(old.get("checked"), (int, float)) else None   # ignore a corrupt cache
    fresh = old.get("repo") == repo and seen and core.now() - seen < CHECK_EVERY
    if fresh and not force:
        return
    rec = {"repo": repo, "checked": core.now(), "latest": old.get("latest") if old.get("repo") == repo else None}
    try:
        rec["latest"] = fetch_latest(repo)
    except Exception as e:   # noqa: BLE001 — a passive check must never raise into the loop or the "Check now" POST;
        rec["error"] = f"{type(e).__name__}: {e}"[:200]   # http errors aren't all OSError (http.client.HTTPException)
    _save(rec)


def update_status():
    """What the phone's Settings sheet and `GET /api/update` show: the current version, the newest tag seen, and
    whether a newer one exists. Computed from the daemon's cached check, so it never touches the network."""
    if not check_enabled():
        return {"enabled": False}
    repo = check_repo()
    cur, cached = current_version(), core.read_json(_cache_path(), {})
    latest = cached.get("latest") if cached.get("repo") == repo else None
    cv, lv = _semver(cur), _semver(latest)
    behind = bool(cv and lv and lv > cv)
    url = f"https://github.com/{repo}/releases/tag/{latest}" if (repo and latest) \
        else (f"https://github.com/{repo}/tags" if repo else None)
    error = "GROVY_UPDATE_REPO isn't an owner/repo" if not repo else (cached.get("error") if latest is None else None)
    return {"enabled": True, "repo": repo, "current": cur, "latest": latest, "behind": behind,
            "checked": cached.get("checked"), "error": error, "url": url}


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
