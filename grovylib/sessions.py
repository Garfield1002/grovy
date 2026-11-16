"""The session registry (sessions.json) and starting, resuming, renaming and stopping sessions."""
import fcntl
import json
import os
import shutil
import time
from contextlib import contextmanager

from . import claude, client, core, tmuxctl


# sessions.json: {name: {name, cwd, created, claude_session_id}}; shared by CLI, hook and daemon,
# so every read-modify-write happens under an flock.

def registry_path():
    return os.path.join(core.state_dir(), "sessions.json")


@contextmanager
def registry_locked():
    lock = os.open(os.path.join(core.state_dir(), "sessions.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reg = core.read_json(registry_path(), {})
        if not isinstance(reg, dict):
            reg = {}
        before = json.dumps(reg, sort_keys=True)
        yield reg
        if json.dumps(reg, sort_keys=True) != before:
            core.atomic_write_json(registry_path(), reg)
    finally:
        os.close(lock)


def registry():
    reg = core.read_json(registry_path(), {})
    return reg if isinstance(reg, dict) else {}


def start_session(name, cwd, extra_args=()):
    """Create the tmux session running claude. Returns pane id. Never touches other sessions."""
    if not core.valid_name(name):
        raise ValueError(f"invalid session name {name!r} (use letters, digits, - and _, max 32)")
    cwd = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        raise ValueError(f"not a directory: {cwd}")
    if tmuxctl.tmux("has-session", "-t", "=" + tmuxctl.tmux_name(name), check=False).returncode == 0:
        raise ValueError(f"tmux session {tmuxctl.tmux_name(name)!r} already exists")
    reg = registry()
    if name not in reg and name_in_use(reg, name):
        raise ValueError(f"{name!r} is the old name of {canonical_name(reg, name)!r}; pick another name")
    if not shutil.which(claude.claude_cmd()):
        raise ValueError(f"{claude.claude_cmd()!r} not found on PATH (set GROVY_CLAUDE)")
    settings = claude.hook_settings(name)
    argv = [claude.claude_cmd(), "--settings", settings, *extra_args]
    # Multiple trailing args => tmux execs them directly, no shell, so paths/names are never interpolated.
    r = tmuxctl.tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", tmuxctl.tmux_name(name), "-c", cwd,
                     "-x", "200", "-y", "50", "-e", f"GROVY_SESSION={name}",
                     "-e", f"GROVY_STATE_DIR={core.state_dir()}",
                     "-e", f"GROVY_PERMISSION_TIMEOUT={core.PERMISSION_TIMEOUT}",
                     "-e", f"PYTHONPATH={claude.session_pythonpath()}",
                     *(["-e", f"GROVY_WIDGETS={core.env('WIDGETS')}"] if core.env("WIDGETS") is not None else []),
                     *(["-e", f"GROVY_CONFIG_DIR={core.env('CONFIG_DIR')}"] if core.env("CONFIG_DIR") else []),
                     "--", *argv)
    pane = r.stdout.strip()
    tmuxctl.tmux("set-option", "-t", "=" + tmuxctl.tmux_name(name) + ":", "@grovy_state", core.state_dir())
    tmuxctl.tmux("set-option", "-t", "=" + tmuxctl.tmux_name(name) + ":", "@grovy", name)
    with registry_locked() as reg:
        prev = reg.get(name, {})
        args = list(extra_args)
        if "--resume" in args:  # remember the session's own flags (e.g. permission mode), not the resume id
            i = args.index("--resume")
            del args[i:i + 2]
        args = [a for a in args if a != "--fork-session"]
        reg[name] = {"name": name, "cwd": cwd, "created": prev.get("created", time.time()),
                     "started": time.time(), "claude_session_id": prev.get("claude_session_id"),
                     "config": claude.session_fingerprint(name),
                     "args": args if args or "args" not in prev else prev["args"]}
    client.notify_daemon({"type": "refresh"})
    return pane


def transcript_exists(sid):
    """Claude only saves a conversation after its first message; --resume on an unsaved id exits at once."""
    import glob
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return bool(glob.glob(os.path.join(base, "projects", "*", glob.escape(sid) + ".jsonl")))


def check_started(name, cwd, extra_args, wait=2.0):
    """claude errors (bad flag, unknown conversation...) kill the tmux session and its message with it."""
    time.sleep(wait)
    if not tmuxctl.tmux_alive(name):
        cmd = " ".join(claude._shquote(a) for a in [claude.claude_cmd(), *extra_args])
        raise RuntimeError(f"claude exited immediately; to see why run: cd {claude._shquote(cwd)} && {cmd}")


def canonical_name(reg, name):
    """Current name for `name`: itself, or the session that took it over in a rename (its alias)."""
    if name in reg:
        return name
    for k, v in reg.items():
        if name in (v.get("aliases") or []):
            return k
    return name


def name_in_use(reg, name):
    """Taken as a session name or as a renamed session's alias (an alias still receives that session's
    hook events, so it must not be reused while the session exists)."""
    return name in reg or any(name in (v.get("aliases") or []) for v in reg.values())


def rename_session(old, new):
    """Rename a session everywhere outside the daemon's memory: tmux (live), registry (old name kept as an
    alias), and its hooks files. Returns True if the session was running. Raises ValueError."""
    if not core.valid_name(new):
        raise ValueError("name: letters, digits, - and _ (max 32)")
    if tmuxctl.tmux("has-session", "-t", "=" + tmuxctl.tmux_name(new), check=False).returncode == 0:
        raise ValueError(f"tmux session {tmuxctl.tmux_name(new)!r} already exists")
    with registry_locked() as reg:
        old = canonical_name(reg, old)
        if old not in reg:
            raise ValueError(f"unknown session {old!r}")
        if old == new:
            return old in tmuxctl.tmux_managed()
        if name_in_use(reg, new) and new not in (reg[old].get("aliases") or []):
            raise ValueError(f"the name {new!r} is already taken")
        live = old in tmuxctl.tmux_managed()
        if live:
            tmuxctl.tmux("rename-session", "-t", "=" + tmuxctl.tmux_name(old), tmuxctl.tmux_name(new))
            tmuxctl.tmux("set-option", "-t", "=" + tmuxctl.tmux_name(new) + ":", "@grovy", new)
            tmuxctl.tmux("set-environment", "-t", "=" + tmuxctl.tmux_name(new), "GROVY_SESSION", new)
        entry = reg.pop(old)
        entry["name"] = new
        entry["aliases"] = sorted((set(entry.get("aliases") or []) | {old}) - {new})
        reg[new] = entry
    claude.hook_settings(new)  # used on its next start
    if live:            # the running Claude points at the old file: make it report the new name too
        claude.hook_settings(new, file_name=old)
    return live


def kill_session(name):
    if not tmuxctl.tmux_alive(name):
        raise ValueError(f"no live managed session {name!r}")
    tmuxctl.tmux("kill-session", "-t", "=" + tmuxctl.tmux_name(name))
    client.notify_daemon({"type": "refresh"})


def resume_session(name, fresh=False):
    """Restart an exited session with its original flags, continuing its last conversation when one
    was saved. Returns a human-readable outcome. Raises ValueError/RuntimeError."""
    reg = registry().get(name)
    if not reg:
        raise ValueError(f"unknown session {name!r}")
    if tmuxctl.tmux_alive(name):
        raise ValueError(f"{name} is already running")
    sid = reg.get("claude_session_id")
    note = ""
    if not fresh and (not sid or not transcript_exists(sid)):
        fresh, note = True, " (previous conversation was never saved: started a new one)"
    extra = list(reg.get("args") or []) + ([] if fresh else ["--resume", sid])
    start_session(name, reg["cwd"], extra)
    check_started(name, reg["cwd"], extra)
    return f"resumed {name}" + (" with a new conversation" if fresh else f" (conversation {sid})") + note
