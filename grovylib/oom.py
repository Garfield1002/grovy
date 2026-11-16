"""systemd-oomd: keep Claude sessions out of the out-of-memory killer's way, and find out when it struck."""
import re
import shutil
import subprocess

from . import core, tmuxctl


def proc_scope(pid):
    """The systemd scope a process is in (cgroup v2), e.g. tmux-spawn-….scope, or None."""
    try:
        with open(f"/proc/{int(pid)}/cgroup") as f:
            m = re.search(r"^0::.*/([^/\s]+\.scope)\s*$", f.read(), re.M)
    except (OSError, ValueError):
        return None
    return m.group(1) if m else None


def oom_protect(name, pid):
    """Move a running Claude into a new scope that systemd-oomd only picks when nothing else is left (the D-Bus call
    `systemd-run --scope` makes; ManagedOOMPreference only takes effect when given at creation). Commands it starts
    from then on are in that scope too. Returns the unit name, or None."""
    unit = f"grovy-{name}-{int(pid)}.scope"
    try:
        r = subprocess.run(["busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                            "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))", unit, "fail",
                            "4", "PIDs", "au", "1", str(int(pid)), "ManagedOOMPreference", "s", "avoid",
                            "CollectMode", "s", "inactive-or-failed",
                            "Description", "s", f"grovy session {name} (Claude Code)", "0"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return unit if r.returncode == 0 else None


OOM_LOG_RE = re.compile(r"systemd-oomd killed|result 'oom-kill'|Out of memory: Killed")


def oom_killed(scopes):
    """Whether the user journal says the OOM killer killed one of these scopes (they're gone by now)."""
    args = ["journalctl", "--user", "-o", "cat", "--no-pager", "-n", "50"]
    for s in scopes:
        args += ["-u", s]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(OOM_LOG_RE.search(r.stdout))


class OomGuard:
    """systemd-oomd side effects (swappable in tests). Off without busctl/journalctl or with GROVY_OOM_AVOID=0."""
    pids = staticmethod(tmuxctl.pane_pids)
    scope = staticmethod(proc_scope)
    protect = staticmethod(oom_protect)
    killed = staticmethod(oom_killed)

    @staticmethod
    def available():
        return core.env("OOM_AVOID", "1") != "0" and bool(shutil.which("busctl") and shutil.which("journalctl"))
