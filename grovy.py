#!/usr/bin/env python3
"""grovy - minimal Claude Code session manager (CLI, tmux helpers, hook adapter).

Identity model:
  grovy name "api"          stable, human-readable, validated
  tmux name  "grovy-api"    tagged with tmux option @grovy=api, always targeted exactly ("=grovy-api")
  claude id  session_id     from hooks; may change over a tmux session's life (/clear, /resume)

Hooks are injected per managed session with `claude --settings FILE`, whose command is
`grovy hook NAME`. The session identity therefore comes from argv, never from cwd.
"""
import argparse
import atexit
import fcntl
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager

HERE = os.path.dirname(os.path.realpath(__file__))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
TMUX_PREFIX = "grovy-"

# Events the adapter forwards. Anything else is ignored (exit 0, no output).
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PermissionRequest",
               "Notification", "Stop", "StopFailure", "PostToolUse")
# The ones still forwarded when fired inside a subagent: requests waiting on the user (GROVY_SUBAGENT_HOOKS=1: all).
SUBAGENT_EVENTS = ("PermissionRequest", "Notification")


def env(name, default=None):
    """GROVY_<name>, falling back to the pre-rename CCS_<name>."""
    v = os.environ.get("GROVY_" + name)
    if v is None:
        v = os.environ.get("CCS_" + name)
    return default if v is None else v


def env_int(name, default):
    try:
        return int(env(name, default))
    except ValueError:
        return default


PERMISSION_TIMEOUT = env_int("PERMISSION_TIMEOUT", 600)  # seconds the phone gets to decide


def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    new, old = os.path.join(base, "grovy"), os.path.join(base, "ccs")
    if not os.path.exists(new) and os.path.isdir(old) and not os.path.islink(old):
        # One-time migration from the pre-rename location. The symlink keeps paths baked into already
        # running Claude sessions (their --settings file, CCS_STATE_DIR) working.
        try:
            os.rename(old, new)
            os.symlink(new, old)
        except OSError:
            pass
    return new


def config_dir():
    """Your own customizations (custom.css), kept out of the repo so `git pull` never conflicts with them."""
    return env("CONFIG_DIR") or os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "grovy")


def state_dir():
    d = env("STATE_DIR") or default_state_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def token_path():
    return os.path.join(state_dir(), "token")


def load_token(create=False):
    """Shared secret for the web UI (state dir, mode 0600). GROVY_TOKEN overrides the file."""
    if env("TOKEN"):
        return env("TOKEN")
    try:
        with open(token_path()) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    if not create:
        return ""
    import secrets
    tok = secrets.token_urlsafe(24)
    fd = os.open(token_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def sock_path():
    p = os.path.join(state_dir(), "grovyd.sock")
    if len(p.encode()) < 100:
        return p
    # AF_UNIX paths are limited to ~108 bytes; fall back to the private per-user runtime dir.
    import hashlib
    rt = os.environ.get("XDG_RUNTIME_DIR") or state_dir()
    return os.path.join(rt, "grovy-" + hashlib.sha256(state_dir().encode()).hexdigest()[:12] + ".sock")


def die(msg, code=1):
    print(f"grovy: {msg}", file=sys.stderr)
    sys.exit(code)


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name))


def atomic_write_json(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        print(f"grovy: ignoring unreadable {path}: {e}", file=sys.stderr)
        return default


# ---------------------------------------------------------------- registry
# sessions.json: {name: {name, cwd, created, claude_session_id}}; shared by CLI, hook and daemon,
# so every read-modify-write happens under an flock.

def registry_path():
    return os.path.join(state_dir(), "sessions.json")


@contextmanager
def registry_locked():
    lock = os.open(os.path.join(state_dir(), "sessions.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reg = read_json(registry_path(), {})
        if not isinstance(reg, dict):
            reg = {}
        before = json.dumps(reg, sort_keys=True)
        yield reg
        if json.dumps(reg, sort_keys=True) != before:
            atomic_write_json(registry_path(), reg)
    finally:
        os.close(lock)


def registry():
    reg = read_json(registry_path(), {})
    return reg if isinstance(reg, dict) else {}


# ---------------------------------------------------------------- tmux

def tmux(*args, input=None, check=True, timeout=5):
    """Run tmux with an argument array (never a shell). Bounded: a hung tmux server raises instead of
    blocking the daemon, which calls some of these while holding its state lock."""
    try:
        r = subprocess.run(["tmux", *args], input=input, capture_output=True, timeout=timeout,
                           text=isinstance(input, str) or input is None)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tmux {args[0]} timed out after {timeout}s")
    if check and r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        raise RuntimeError(f"tmux {args[0]} failed: {err.strip()}")
    return r


def tmux_name(name):
    return TMUX_PREFIX + name


def tmux_managed():
    """{grovy name: pane_id} for live tmux sessions carrying our @grovy marker AND owned by this state dir
    (@grovy_state; sessions predating that tag belong to the default dir). tmux is authoritative."""
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{@grovy}\t#{pane_id}\t#{@grovy_state}", check=False)
    if r.returncode != 0:  # no server running = no sessions
        return {}
    mine = os.path.realpath(state_dir())
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1] and parts[0] == tmux_name(parts[1]) and \
                os.path.realpath(parts[3] or default_state_dir()) == mine:
            out.setdefault(parts[1], parts[2])
    return out


# ---------------------------------------------------------------- the out-of-memory killer (systemd-oomd)
# systemd-oomd kills a whole cgroup when memory pressure stays high, and each tmux pane is its own cgroup, so it
# killed Claude sessions along with heavy jobs running elsewhere. The daemon moves each session's Claude into its own
# scope marked ManagedOOMPreference=avoid (oomd honours it: the scope and the monitored user@UID.service have the same
# owner), and when a session vanishes it asks the journal whether the OOM killer did it. All of it is best effort.

def pane_pids():
    """{grovy name: pid of the pane's process}. That's Claude itself: tmux execs our argv directly."""
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{@grovy}\t#{pane_pid}\t#{@grovy_state}", check=False)
    if r.returncode != 0:
        return {}
    mine, out = os.path.realpath(state_dir()), {}
    for parts in (line.split("\t") for line in r.stdout.splitlines()):
        if len(parts) == 4 and parts[1] and parts[0] == tmux_name(parts[1]) and parts[2].isdigit() and \
                os.path.realpath(parts[3] or default_state_dir()) == mine:
            out.setdefault(parts[1], int(parts[2]))
    return out


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


def tmux_alive(name):
    return name in tmux_managed()


def claude_cmd():
    return env("CLAUDE", "claude")


def grovy_cmd():
    """argv prefix that runs this CLI regardless of PATH or cwd."""
    return [sys.executable, os.path.join(HERE, "grovy.py")]


SESSION_ENV_VERSION = 1   # bump when start_session's environment changes (1: PYTHONPATH), so sessions show Update


def auto_mode_hints():
    """Auto mode's classifier can't know that grovy is local and blocked `grovy page publish` as "unsanctioned
    communication". These entries describe it (prose; the classifier reads them) and say that showing work to the
    user through grovy is fine. They cover grovy's own invocation only. "$defaults" keeps every built-in rule."""
    bin_ = GROVY_BIN
    return {
        "environment": ["$defaults",
                        f"**Org-specific CLIs**: grovy (`{bin_}`, also run as `python3 {HERE}/grovy.py`, and "
                        "`import grovy` in Python scripts) is the user's own tool that mirrors this Claude Code "
                        "session to the user's own phone. Its `page`, `note`, `progress`, `show` and `tail` "
                        "subcommands, and the Python functions grovy.note/progress/track/log/show, deliver only to "
                        "the user's grovy daemon on this machine over a private Unix socket, and the daemon shows the "
                        "content to the user alone. Nothing leaves the machine unless the user, on the phone, chooses "
                        "to share a page."],
        "allow": ["$defaults",
                  "grovy Display: showing work to the user through grovy (`grovy page publish|ls|rm`, `grovy note`, "
                  "`grovy progress`, `grovy show`, piping output into `grovy tail`, or the grovy Python functions) "
                  "is answering the user, not communication with others: not Unsanctioned Communication, External "
                  "System Writes, Unrequested Artifact Publish or Data Exfiltration. It covers only the grovy "
                  "invocation itself; judge any other command on the same line, including what is piped into "
                  "`grovy tail`, on its own."],
    }


def session_settings(name):
    """What a session started now gets via --settings: hooks, status line, auto-mode hints."""
    cmd = " ".join(_shquote(a) for a in grovy_cmd() + ["hook", name])

    def entry(timeout):
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}]

    hooks = {ev: entry(10) for ev in HOOK_EVENTS}
    # The hook gives up on the daemon at PERMISSION_TIMEOUT+10, well before Claude kills it.
    hooks["PermissionRequest"] = [{"matcher": "*", **entry(PERMISSION_TIMEOUT + 30)[0]}]
    hooks["PostToolUse"] = [{"matcher": "*", **entry(10)[0]}]
    # The status line feeds the phone (model, effort, context) and then shows the user's own status line.
    status = {"type": "command", "command": " ".join(_shquote(a) for a in grovy_cmd() + ["statusline", name])}
    own = user_statusline()
    if own and isinstance(own.get("padding"), int):
        status["padding"] = own["padding"]
    return {"hooks": hooks, "statusLine": status, "autoMode": auto_mode_hints()}


def session_fingerprint(name):
    """Identifies what a session started now would run with. Claude reads its settings only at start, so a
    session whose recorded fingerprint differs is out of date (offer ⋯ → Update session)."""
    import hashlib
    blob = json.dumps({"settings": session_settings(name), "env": SESSION_ENV_VERSION}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def hook_settings(name, file_name=None):
    """Settings file passed to claude via --settings; registers `grovy hook NAME` for our events.
    file_name lets a rename rewrite the file a running Claude already points at."""
    path = os.path.join(state_dir(), "hooks", f"{file_name or name}.json")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    atomic_write_json(path, session_settings(name))
    return path


def user_statusline(project=None):
    """The status line the user configured (local > project > user settings), unless it is ours.

    `project` must be the session's registered folder, never the cwd from Claude's status JSON: that follows
    Claude's Bash `cd`, so a cloned repo's .claude/settings.json would get its command run without the trust
    dialog (Claude Code itself only applies project settings from the trusted project root)."""
    paths = []
    if project:
        paths += [os.path.join(project, ".claude", "settings.local.json"), os.path.join(project, ".claude", "settings.json")]
    paths.append(os.path.join(claude_home(), "settings.json"))
    for p in paths:
        sl = read_json(p, {}).get("statusLine") if isinstance(read_json(p, {}), dict) else None
        if isinstance(sl, dict) and sl.get("type") == "command" and isinstance(sl.get("command"), str) \
                and " statusline " not in sl["command"]:
            return sl
    return None


STATUS_FIELDS = ("model", "effort", "fast_mode", "context_window", "cwd", "session_id")


def cmd_statusline(name):
    """Called by Claude Code (often) with its status JSON on stdin. Forward a trimmed copy to the daemon,
    then print the user's own status line for the same input. Never fails loudly."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if valid_name(name) and isinstance(data, dict):
        try:
            sock_request({"type": "statusline", "session": name, "data": {k: data.get(k) for k in STATUS_FIELDS}},
                         timeout=0.3, wait_reply=False)
        except OSError:
            pass
    reg = registry() if valid_name(name) else {}
    project = (reg.get(canonical_name(reg, name)) or {}).get("cwd") if valid_name(name) else None
    own = user_statusline(project)
    if own:
        cwd = data.get("cwd") if isinstance(data, dict) and isinstance(data.get("cwd"), str) else None
        try:
            r = subprocess.run(["/bin/sh", "-c", own["command"]], input=raw, capture_output=True, text=True,
                               timeout=5, cwd=cwd if cwd and os.path.isdir(cwd) else None)
            sys.stdout.write(r.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


def _shquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def session_pythonpath():
    """So that `import grovy` works in scripts Claude runs (live widgets). Appended, so a project's own
    PYTHONPATH entries win."""
    have = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    return os.pathsep.join(have + ([HERE] if HERE not in have else []))


def start_session(name, cwd, extra_args=()):
    """Create the tmux session running claude. Returns pane id. Never touches other sessions."""
    if not valid_name(name):
        raise ValueError(f"invalid session name {name!r} (use letters, digits, - and _, max 32)")
    cwd = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        raise ValueError(f"not a directory: {cwd}")
    if tmux("has-session", "-t", "=" + tmux_name(name), check=False).returncode == 0:
        raise ValueError(f"tmux session {tmux_name(name)!r} already exists")
    reg = registry()
    if name not in reg and name_in_use(reg, name):
        raise ValueError(f"{name!r} is the old name of {canonical_name(reg, name)!r}; pick another name")
    if not shutil.which(claude_cmd()):
        raise ValueError(f"{claude_cmd()!r} not found on PATH (set GROVY_CLAUDE)")
    settings = hook_settings(name)
    argv = [claude_cmd(), "--settings", settings, *extra_args]
    # Multiple trailing args => tmux execs them directly, no shell, so paths/names are never interpolated.
    r = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", tmux_name(name), "-c", cwd,
             "-x", "200", "-y", "50", "-e", f"GROVY_SESSION={name}", "-e", f"GROVY_STATE_DIR={state_dir()}",
             "-e", f"GROVY_PERMISSION_TIMEOUT={PERMISSION_TIMEOUT}", "-e", f"PYTHONPATH={session_pythonpath()}",
             *(["-e", f"GROVY_WIDGETS={env('WIDGETS')}"] if env("WIDGETS") is not None else []), "--", *argv)
    pane = r.stdout.strip()
    tmux("set-option", "-t", "=" + tmux_name(name) + ":", "@grovy_state", state_dir())
    tmux("set-option", "-t", "=" + tmux_name(name) + ":", "@grovy", name)
    with registry_locked() as reg:
        prev = reg.get(name, {})
        args = list(extra_args)
        if "--resume" in args:  # remember the session's own flags (e.g. permission mode), not the resume id
            i = args.index("--resume")
            del args[i:i + 2]
        args = [a for a in args if a != "--fork-session"]
        reg[name] = {"name": name, "cwd": cwd, "created": prev.get("created", time.time()),
                     "started": time.time(), "claude_session_id": prev.get("claude_session_id"),
                     "config": session_fingerprint(name),
                     "args": args if args or "args" not in prev else prev["args"]}
    notify_daemon({"type": "refresh"})
    return pane


def transcript_exists(sid):
    """Claude only saves a conversation after its first message; --resume on an unsaved id exits at once."""
    import glob
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return bool(glob.glob(os.path.join(base, "projects", "*", glob.escape(sid) + ".jsonl")))


def check_started(name, cwd, extra_args, wait=2.0):
    """claude errors (bad flag, unknown conversation...) kill the tmux session and its message with it."""
    time.sleep(wait)
    if not tmux_alive(name):
        cmd = " ".join(_shquote(a) for a in [claude_cmd(), *extra_args])
        raise RuntimeError(f"claude exited immediately; to see why run: cd {_shquote(cwd)} && {cmd}")


def _claude_projects():
    try:
        with open(os.path.expanduser("~/.claude.json")) as f:
            p = json.load(f).get("projects", {})
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _git_root(path):
    p = path
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def folder_trusted(path):
    """Would Claude start in `path` without stopping at the workspace-trust dialog? (rule: trusted_projects)"""
    path = os.path.realpath(path)
    explicit = {os.path.realpath(k) for k, v in _claude_projects().items()
                if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}
    g = _git_root(path)
    if g is not None:
        return g in explicit
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in explicit)


def trusted_projects():
    """Folders a phone may start a session in: ones Claude won't stop at the workspace-trust dialog
    (which would hold back every hook). Verified with Claude Code 2.1.325:
      * inside a git repo, only an explicitly trusted repo root counts (a trusted parent does not);
      * outside any repo, the folder or any ancestor being trusted is enough.
    Candidates are Claude's known projects plus git repos directly inside a trusted folder."""
    projects = _claude_projects()
    explicit = {os.path.realpath(k) for k, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}
    roots = list(explicit)

    def trusted(path):
        g = _git_root(path)
        if g is not None:
            return g in explicit
        return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)

    cands = {os.path.realpath(k) for k in projects}
    for r in roots:
        try:
            for e in os.scandir(r):
                if e.is_dir() and not e.name.startswith(".") and os.path.isdir(os.path.join(e.path, ".git")):
                    cands.add(os.path.realpath(e.path))
        except OSError:
            pass
    tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
    return sorted(p for p in cands if os.path.isdir(p) and trusted(p)
                  and not (p == tmp or p.startswith(tmp + "/")) and p != os.path.expanduser("~"))


# ---------------------------------------------------------------- past Claude conversations

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_conv_cache = {}  # transcript path -> ((mtime_ns, size), info)


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _text(content, user=False):
    """Plain text of a message: string content, or its text blocks (tool calls/results are skipped)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _conversation(path):
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _conv_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    info = {"id": os.path.basename(path)[:-6], "cwd": None, "title": None, "first": None, "last": None,
            "messages": 0, "mtime": st.st_mtime}
    custom = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("isSidechain"):
                continue
            t = r.get("type")
            info["cwd"] = info["cwd"] or r.get("cwd")
            if t == "ai-title":
                info["title"] = r.get("aiTitle") or info["title"]
            elif t == "custom-title":
                custom = r.get("customTitle") or custom
            elif t == "user" and not r.get("isMeta"):
                text = _text((r.get("message") or {}).get("content")).strip()
                if text and not text.startswith("<"):  # skip command/caveat/notification wrappers
                    info["messages"] += 1
                    info["first"] = info["first"] or text[:400]
            elif t == "assistant":
                text = _text((r.get("message") or {}).get("content")).strip()
                if text:
                    info["last"] = text[:1200]
    info["title"] = custom or info["title"]
    _conv_cache[path] = (key, info)
    return info


def _belongs(cwd, folder):
    """Claude files a conversation by its project directory, so that's what `claude --resume` from `folder`
    finds. The recorded cwd only disqualifies it when it is a *different* folder that exists here (two folders
    can map to the same directory name). Conversations copied from another machine (cwd /home/<other>/...,
    absent here) and ones that moved into a subfolder or worktree still belong."""
    if not cwd:
        return True
    real = os.path.realpath(cwd)
    if real == folder or real.startswith(folder.rstrip("/") + "/"):
        return True
    return not os.path.exists(cwd)


def conversations(folder, limit=50):
    """Saved Claude conversations started in `folder`, newest first. Claude stores them under
    projects/<folder with non-alphanumerics as '-'>/<id>.jsonl; each one's own cwd is verified too."""
    import glob
    folder = os.path.realpath(folder)
    d = os.path.join(claude_home(), "projects", re.sub(r"[^A-Za-z0-9]", "-", folder))
    out = []
    for p in glob.glob(os.path.join(glob.escape(d), "*.jsonl")):
        if not UUID_RE.match(os.path.basename(p)[:-6]):
            continue
        try:
            c = _conversation(p)
        except OSError:
            continue
        if c["messages"] and _belongs(c["cwd"], folder):
            out.append(c)
    return sorted(out, key=lambda c: -c["mtime"])[:limit]


def live_conversations():
    """{conversation id: pid} of running claude processes. Best effort: reads Claude Code's own
    (undocumented) ~/.claude/sessions/<pid>.json and requires the pid to be alive with the same start time."""
    import glob
    out = {}
    for f in glob.glob(os.path.join(glob.escape(claude_home()), "sessions", "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            with open(f"/proc/{int(d['pid'])}/stat") as fh:
                stat = fh.read()
            if stat[stat.rindex(")") + 2:].split()[19] == str(d.get("procStart")):
                out[d["sessionId"]] = int(d["pid"])
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
    return out


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
    if not valid_name(new):
        raise ValueError("name: letters, digits, - and _ (max 32)")
    if tmux("has-session", "-t", "=" + tmux_name(new), check=False).returncode == 0:
        raise ValueError(f"tmux session {tmux_name(new)!r} already exists")
    with registry_locked() as reg:
        old = canonical_name(reg, old)
        if old not in reg:
            raise ValueError(f"unknown session {old!r}")
        if old == new:
            return old in tmux_managed()
        if name_in_use(reg, new) and new not in (reg[old].get("aliases") or []):
            raise ValueError(f"the name {new!r} is already taken")
        live = old in tmux_managed()
        if live:
            tmux("rename-session", "-t", "=" + tmux_name(old), tmux_name(new))
            tmux("set-option", "-t", "=" + tmux_name(new) + ":", "@grovy", new)
            tmux("set-environment", "-t", "=" + tmux_name(new), "GROVY_SESSION", new)
        entry = reg.pop(old)
        entry["name"] = new
        entry["aliases"] = sorted((set(entry.get("aliases") or []) | {old}) - {new})
        reg[new] = entry
    hook_settings(new)  # used on its next start
    if live:            # the running Claude points at the old file: make it report the new name too
        hook_settings(new, file_name=old)
    return live


def kill_session(name):
    if not tmux_alive(name):
        raise ValueError(f"no live managed session {name!r}")
    tmux("kill-session", "-t", "=" + tmux_name(name))
    notify_daemon({"type": "refresh"})


def capture(name, lines=200):
    if not tmux_alive(name):
        raise ValueError(f"no live managed session {name!r}")
    return tmux("capture-pane", "-p", "-J", "-S", f"-{int(lines)}", "-t", "=" + tmux_name(name) + ":").stdout


TYPE_MAX = 20000   # longer prompts are pasted (Claude then labels them as pasted content)
TYPE_CHUNK = 64    # chars per send-keys; small bursts are not detected as a paste by Claude Code
TYPE_PAUSE = 0.015


def _own_pane(name):
    panes = tmux_managed()
    if name not in panes:
        raise RuntimeError("session is not running")
    pane = panes[name]
    who = tmux("display-message", "-p", "-t", pane, "#{session_name}\t#{@grovy}", check=False)
    if who.returncode != 0 or who.stdout.strip() != f"{tmux_name(name)}\t{name}":
        raise RuntimeError("pane no longer belongs to session")
    return pane


def typed_mode(text):
    """Typed keystrokes reach Claude as the user's own words; a big paste arrives wrapped as
    <pasted_content>. Leading / or ! would switch the input into command/bash mode when typed,
    so those (and huge prompts) take the literal paste path."""
    return len(text) <= TYPE_MAX and not text.lstrip().startswith(("/", "!"))


def send_key(name, key):
    """Press one key (e.g. Escape) in the session's pane, after verifying the pane is ours."""
    tmux("send-keys", "-t", _own_pane(name), key)


class PasteAborted(RuntimeError):
    """Typing stopped part-way because the session stopped being safe to type into."""


def paste_prompt(name, text, command=False, abort=None):
    """Deliver text to the session's input, then Enter. No shell anywhere.

    command=True types an allowlisted slash command. It always ends in a space before Enter:
    the space closes Claude Code's fuzzy command menu, so Enter can't pick a neighbour.

    The caller decides *whether* it is safe to type. `abort()` (if given) is asked again between chunks
    and right before Enter, and returns a reason to stop: Claude can start a turn on its own while we
    type (a background task finishing), and if that turn opens a permission dialog, the final Enter
    would answer it with its highlighted option. Raises PasteAborted then, and RuntimeError if the
    session is gone or its pane changed owner.
    """
    def check():
        why = abort() if abort else None
        if why:
            raise PasteAborted(why)

    pane = _own_pane(name)
    if command:
        text = text.strip() + " "
    if command or typed_mode(text):
        clean = "".join(c for c in text.replace("\r\n", "\n").replace("\t", "    ")
                        if c == "\n" or c.isprintable() or c == " ")
        for i, line in enumerate(clean.split("\n")):
            if i:
                tmux("send-keys", "-t", pane, "C-j")  # newline without submitting
            for j in range(0, len(line), TYPE_CHUNK):
                # tmux treats an argument ending in ';' as a command separator and drops the ';',
                # so every ';' is sent as a raw byte instead.
                for k, piece in enumerate(line[j:j + TYPE_CHUNK].split(";")):
                    if k:
                        tmux("send-keys", "-t", pane, "-H", "3b")
                    if piece:
                        tmux("send-keys", "-t", pane, "-l", "--", piece)
                time.sleep(TYPE_PAUSE)
                check()
    else:
        buf = f"grovy-{name}-{os.getpid()}-{time.monotonic_ns()}"
        # Strip control characters (ESC especially): "\x1b[201~" would end the bracketed paste early
        # and turn the rest of the text into raw keystrokes.
        safe = "".join(c for c in text.replace("\r\n", "\n") if c in "\n\t" or c.isprintable())
        tmux("load-buffer", "-b", buf, "-", input=safe.encode())
        try:
            _own_pane(name)  # re-verify right before pasting
            check()
        except RuntimeError:
            tmux("delete-buffer", "-b", buf, check=False)
            raise
        tmux("paste-buffer", "-p", "-d", "-r", "-b", buf, "-t", pane)
    time.sleep(0.4)  # let the TUI ingest the input before submitting
    _own_pane(name)
    check()
    tmux("send-keys", "-t", pane, "Enter")


# ---------------------------------------------------------------- daemon socket client

def sock_request(msg, timeout=2.0, wait_reply=True):
    """Send one JSON line to grovyd; optionally read one JSON line back. Raises OSError if unreachable."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path())
        s.sendall(json.dumps(msg).encode() + b"\n")
        if not wait_reply:
            return None
        # No half-close: the daemon treats EOF as "this hook process is gone".
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data) if data.strip() else None
    finally:
        s.close()


def notify_daemon(msg):
    try:
        sock_request(msg, timeout=1.0, wait_reply=False)
    except OSError:
        pass


# ---------------------------------------------------------------- hook adapter

def permission_output(behavior, message=None, updated_input=None):
    decision = {"behavior": behavior}
    if behavior == "allow" and isinstance(updated_input, dict):
        decision["updatedInput"] = updated_input  # e.g. AskUserQuestion answers from the phone
    if behavior == "deny":
        decision["message"] = message or "Denied from the grovy web interface."
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


WIDGET_GUIDE = """\
grovy: the user may read your final message of each turn on a phone, which renders markdown and ```widget \
blocks (one JSON object each; grovy draws them, no HTML). The terminal shows the raw JSON, so state key facts in \
text too. Use a widget only when it clearly beats text, usually at most one per reply.
- chart: {"type":"chart","kind":"bar|line|pie","title":"..","labels":["a","b"],"series":[{"name":"..","values":[1,2]}],"unit":"ms"}
- stats: {"type":"stats","title":"..","items":[{"label":"Tests","value":"88/88","note":"..","tone":"ok|warn|bad"}]}
- progress: {"type":"progress","title":"..","items":[{"label":"..","value":3,"max":10}]}
- checklist: {"type":"checklist","title":"..","items":[{"label":"..","status":"done|todo|running|failed|skipped","note":".."}]}
- choices: {"type":"choices","question":"..","multi":false,"options":[{"label":"..","description":"..","message":"text to send if picked"}]}
- form: {"type":"form","title":"..","submit":"..","fields":[{"name":"k","label":"..","kind":"text|textarea|number|select|toggle","options":[".."],"default":".."}]}
- diagram: {"type":"diagram","direction":"down|right","nodes":[{"id":"a","label":"..","tone":"ok|warn|bad"}],"edges":[{"from":"a","to":"b","label":".."}]}
choices/form only prefill the user's message box; they review and send it. Limits: ~12 bars, 8 pie slices, 30 nodes.
Only when the user asks for a page (they may say "local page": that means this), or the result is too big for \
widgets, make one: a self-contained HTML file \
(phone-first, light/dark; inline JS/CSS, pinned libraries from cdnjs/jsdelivr/unpkg, Google Fonts, data: images; \
no fetch, storage or cookies, so embed the data; `grovy.send(text)` prefills the message box), then run \
`GROVY page publish FILE --title T` as a command of its own (same file name updates it; `page ls`, `page rm NAME`). Mention it in your reply.
Commands you run that take more than ~30s (training, builds, long test runs, batch jobs) can show live cards on the \
phone as they go. Python: `import grovy` (on PYTHONPATH; does nothing outside grovy): `for x in grovy.track(xs, "T")`; \
`p = grovy.progress("T", total=N)` then `p.set(n, note="..")` or `p.advance()`; `grovy.note("markdown")`; \
`lg = grovy.log("T")` then `print(.., file=lg)`; `w = grovy.show(widget_dict)` then `w.update(d)`, `w.done()`. \
Shell: `cmd | GROVY tail --title T`, `GROVY progress KEY N --max M [--done]`, `GROVY note TEXT`. Use these in \
scripts you write to run, not in the user's code unless asked."""


GROVY_BIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "grovy")
WIDGET_GUIDE = WIDGET_GUIDE.replace("GROVY ", shlex.quote(GROVY_BIN) + " ")


def widget_guide():
    """Hooks inherit Claude's environment: a session started before PYTHONPATH was set needs the path spelled out."""
    if HERE in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        return WIDGET_GUIDE
    return WIDGET_GUIDE.replace("(on PYTHONPATH; ", f"(first `sys.path.insert(0, {HERE!r})`; ")


def cmd_hook(name):
    """Read one hook event from stdin. stdout is reserved for protocol output; diagnostics -> stderr.

    Every failure path exits 0 with no output, which means "no opinion": Claude carries on
    normally and, for PermissionRequest, shows its usual terminal dialog.
    """
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except ValueError as e:
        print(f"grovy hook: malformed input: {e}", file=sys.stderr)
        return 0
    if not valid_name(name):
        print(f"grovy hook: invalid session name {name!r}", file=sys.stderr)
        return 0
    event = payload.get("hook_event_name")
    if event not in HOOK_EVENTS:
        return 0
    if payload.get("agent_id") and event not in SUBAGENT_EVENTS and env("SUBAGENT_HOOKS", "0") != "1":
        # Fired inside a subagent (verified: such hooks carry agent_id/agent_type, and the parent's session_id).
        # Its tool calls aren't the session's state: a background subagent's PostToolUse can even arrive after
        # the parent's Stop and would show an idle session as working. Only what needs the user goes through.
        return 0
    if event == "SessionStart" and isinstance(payload.get("session_id"), str):
        # Recorded here, not only in the daemon, so `grovy resume` works even if grovyd was down.
        try:
            with registry_locked() as reg:
                canon = canonical_name(reg, name)  # a renamed session's Claude still reports its old name
                if canon in reg:
                    reg[canon]["claude_session_id"] = payload["session_id"]
        except OSError as e:
            print(f"grovy hook: registry update failed: {e}", file=sys.stderr)
    msg = {"type": "hook", "session": name, "event": payload, "pid": os.getppid()}
    if event == "PostToolUse" and not payload.get("agent_id"):
        # Steering: messages sent from the phone while Claude works come back here and are handed to Claude.
        try:
            reply = sock_request({**msg, "want_steer": True}, timeout=3.0)
        except (OSError, ValueError) as e:
            print(f"grovy hook: daemon unreachable ({e}); event not recorded", file=sys.stderr)
            return 0
        steer = (reply or {}).get("steer")
        if isinstance(steer, str) and steer:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                                "additionalContext": steer}}))
            sys.stdout.flush()
        return 0
    if event != "PermissionRequest":
        try:
            sock_request(msg, timeout=2.0, wait_reply=False)
        except OSError as e:
            print(f"grovy hook: daemon unreachable ({e}); event not recorded", file=sys.stderr)
        if event == "SessionStart" and env("WIDGETS", "1") != "0":   # also after /clear and /compact
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                "additionalContext": widget_guide()}}))
            sys.stdout.flush()
        return 0
    try:
        reply = sock_request(msg, timeout=PERMISSION_TIMEOUT + 10)
    except (OSError, ValueError) as e:
        print(f"grovy hook: no remote decision ({e}); falling back to terminal", file=sys.stderr)
        return 0
    behavior = (reply or {}).get("decision")
    if behavior in ("allow", "deny"):
        sys.stdout.write(json.dumps(permission_output(behavior, reply.get("message"), reply.get("updated_input"))))
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- live widgets: a library for scripts
# Scripts Claude runs inside a grovy session can `import grovy` (the session's PYTHONPATH includes this folder)
# and show cards in the phone chat that update in place while they run:
#
#     for x in grovy.track(items, "Converting"): ...       # a progress bar with an ETA
#     p = grovy.progress("Training", total=50); p.set(7)   # or p.advance()
#     grovy.note("**Epoch 3**: loss 0.41")                  # a markdown note
#     lg = grovy.log("pytest"); lg.write(text)             # the last lines of some output
#     w = grovy.show({"type": "chart", ...}); w.update({...})   # any widget, updated by replacing it
#
# Outside a grovy session, or when the daemon can't be reached, every call does nothing, quickly. A script must never
# fail or slow down because of the phone.

LIVE_EVERY = 0.5          # seconds between updates of one widget sent to the daemon (the last one always goes)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_live = {"off_until": 0.0, "open": {}, "crash": None, "thread": None, "warned": set()}
_live_lock = threading.RLock()


def active():
    """True inside a grovy session (whether or not the daemon is up)."""
    return bool(env("SESSION"))


def _live_send(key, spec, status="running", message=None):
    """One update to the daemon, acknowledged (so one widget's updates arrive in order). Never raises."""
    session = env("SESSION")
    if not session or time.monotonic() < _live["off_until"]:
        return None
    msg = {"type": "live", "session": session, "key": key, "spec": spec, "status": status, "message": message}
    try:
        r = sock_request(msg, timeout=1.0)
    except (OSError, ValueError, TypeError):
        _live["off_until"] = time.monotonic() + 30   # daemon down or stuck: don't make every call wait
        return None
    if isinstance(r, dict) and "error" in r and r["error"] not in _live["warned"]:
        _live["warned"].add(r["error"])
        print(f"grovy: widget not shown: {r['error']}", file=sys.stderr)
    return r


def _live_key():
    return "w" + os.urandom(6).hex()


class Live:
    """A widget card in the phone chat, updated in place. Use it as a context manager to mark it failed on an
    exception. Widgets left open when the script exits are closed then ("stopped", or "failed" after a crash)."""

    def __init__(self, spec=None, key=None):
        self.key = key or _live_key()
        self.status, self.message = "running", None
        self._spec, self._sent, self._pending = spec, 0.0, False
        self._lock = threading.Lock()   # the background thread flushes too
        if not active():
            return
        _live_start()
        with _live_lock:
            _live["open"][self.key] = self
        self._flush()

    def render(self):
        return self._spec

    def update(self, spec):
        """Replace the widget (any type grovy draws: chart, stats, checklist, ...)."""
        self._spec = spec
        self._changed()
        return self

    def done(self, message=None):
        self._close("done", message)

    def fail(self, message=None):
        self._close("failed", message)

    def _changed(self):
        if not active() or self.status != "running":
            return
        if time.monotonic() - self._sent >= LIVE_EVERY:
            self._flush()
        else:
            self._pending = True   # the background thread sends it, so the last state is never lost

    def _flush(self):
        with self._lock:
            self._sent, self._pending = time.monotonic(), False
            _live_send(self.key, self.render(), self.status, self.message)

    def _close(self, status, message=None):
        if self.status != "running":
            return
        self.status, self.message = status, message
        with _live_lock:
            _live["open"].pop(self.key, None)
        if active():
            self._flush()

    def _at_exit(self):
        if _live["crash"]:
            self.fail("the script crashed: " + _live["crash"])
        else:
            self._close("stopped", "the script ended without finishing it")

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            self.done()
        elif issubclass(et, GeneratorExit):   # the loop over track() was left early
            self._close("stopped", "stopped early")
        elif issubclass(et, KeyboardInterrupt):
            self._close("stopped", "interrupted")
        else:
            self.fail(f"{et.__name__}: {ev}"[:300])
        return False


class Progress(Live):
    """A progress bar. With total=None it shows a count and a moving bar. The phone adds the elapsed time and,
    with a total, an estimate of the time left."""

    def __init__(self, title=None, total=None, label=None, key=None):
        self.title, self.total, self.label, self.n, self.note = title, total, label, 0, None
        super().__init__(None, key)

    def render(self):
        item = {"label": str(self.label or ""), "value": self.n}
        if self.total is None:
            item["indeterminate"] = True
        else:
            item["max"] = self.total
        if self.note:
            item["note"] = str(self.note)
        return {"type": "progress", "title": self.title, "items": [item], "eta": True}

    def set(self, n, note=None, total=None, label=None):
        self.n = n
        if note is not None:
            self.note = note
        if total is not None:
            self.total = total
        if label is not None:
            self.label = label
        self._changed()
        return self

    def advance(self, k=1, note=None):
        return self.set(self.n + k, note)

    def _at_exit(self):
        if not _live["crash"] and self.total is not None and self.n >= self.total:
            self.done()
        else:
            super()._at_exit()


class Log(Live):
    """The last `lines` lines of some output (ANSI colours stripped). Works as a file: print(..., file=lg)."""

    def __init__(self, title=None, lines=40, key=None):
        self.title, self.lines, self.partial = title, deque(maxlen=max(1, min(int(lines), 200))), ""
        super().__init__(None, key)

    def render(self):
        lines = list(self.lines) + ([self.partial] if self.partial else [])
        return {"type": "log", "title": self.title, "lines": [l[:500] for l in lines[-self.lines.maxlen:]]}

    def write(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        parts = (self.partial + _ANSI.sub("", str(text))).replace("\r\n", "\n").split("\n")
        self.partial = parts.pop()
        self.lines.extend(p.rsplit("\r", 1)[-1] for p in parts)   # \r: a redrawn line, keep what's left
        self.partial = self.partial.rsplit("\r", 1)[-1]
        self._changed()
        return len(text)

    def print(self, *args, sep=" "):
        self.write(sep.join(map(str, args)) + "\n")

    def flush(self):
        pass


def show(spec, key=None, done=False):
    """Show any widget (a dict like the ```widget blocks). Returns a Live: .update(new_spec), .done(), .fail()."""
    w = Live(spec, key)
    if done:
        w.done()
    return w


def progress(title=None, total=None, label=None, key=None):
    return Progress(title, total, label, key)


def track(iterable, title=None, total=None, label=None):
    """Like tqdm: `for x in grovy.track(xs, "Title")` shows a live progress bar."""
    if total is None:
        try:
            total = len(iterable)
        except TypeError:
            pass
    with Progress(title, total, label) as p:
        for x in iterable:
            yield x
            p.advance()


def log(title=None, lines=40, key=None):
    return Log(title, lines, key)


def note(text, tone=None, title=None):
    """A markdown note in the chat, now (no need to wait for Claude's reply)."""
    if active():
        _live_send(_live_key(), {"type": "note", "text": str(text), "tone": tone, "title": title}, "done")


def _live_start():
    """Once per process: a thread that sends throttled last updates, and exit/crash handlers."""
    with _live_lock:
        if _live["thread"]:
            return
        _live["thread"] = t = threading.Thread(target=_live_loop, name="grovy-live", daemon=True)
        t.start()
    prev = sys.excepthook

    def hook(et, ev, tb):
        _live["crash"] = f"{et.__name__}: {ev}"[:300]
        prev(et, ev, tb)
    sys.excepthook = hook
    atexit.register(_live_exit)


def _live_loop():
    while True:
        time.sleep(LIVE_EVERY / 2)
        with _live_lock:
            due = [w for w in _live["open"].values() if w._pending and time.monotonic() - w._sent >= LIVE_EVERY]
        for w in due:
            w._flush()


def _live_exit():
    with _live_lock:
        left = list(_live["open"].values())
    for w in left:
        w._at_exit()


# ---------------------------------------------------------------- CLI

def cmd_ls(_):
    reg, live = registry(), tmux_managed()
    try:
        status = (sock_request({"type": "status"}) or {}).get("sessions", {})
    except (OSError, ValueError):
        status = None
    names = sorted(set(reg) | set(live))
    if not names:
        print("no sessions")
        return 0
    print(f"{'NAME':<16} {'STATE':<12} PATH")
    for n in names:
        if n not in live:
            st = "exited"
        elif status is None:
            st = "running?"  # daemon down: tmux says alive, Claude state unknown
        else:
            st = status.get(n, {}).get("state", "unknown")
        print(f"{n:<16} {st:<12} {reg.get(n, {}).get('cwd', '?')}")
    if status is None:
        print("(grovyd not reachable: state shown from tmux only)", file=sys.stderr)
    return 0


def cmd_new(a):
    try:
        extra = [x for x in a.claude_args if x != "--"][:20]
        start_session(a.name, a.path, extra)
        check_started(a.name, a.path, extra)
    except (ValueError, RuntimeError) as e:
        die(str(e))
    print(f"started {a.name} in tmux session {tmux_name(a.name)}  (grovy attach {a.name})")
    if not folder_trusted(a.path):
        print(f"note: Claude hasn't been trusted in this folder yet, so it is showing the workspace-trust dialog\n"
              f"and grovy sees nothing until you accept it once: grovy attach {a.name}", file=sys.stderr)
    return 0


def resume_session(name, fresh=False):
    """Restart an exited session with its original flags, continuing its last conversation when one
    was saved. Returns a human-readable outcome. Raises ValueError/RuntimeError."""
    reg = registry().get(name)
    if not reg:
        raise ValueError(f"unknown session {name!r}")
    if tmux_alive(name):
        raise ValueError(f"{name} is already running")
    sid = reg.get("claude_session_id")
    note = ""
    if not fresh and (not sid or not transcript_exists(sid)):
        fresh, note = True, " (previous conversation was never saved: started a new one)"
    extra = list(reg.get("args") or []) + ([] if fresh else ["--resume", sid])
    start_session(name, reg["cwd"], extra)
    check_started(name, reg["cwd"], extra)
    return f"resumed {name}" + (" with a new conversation" if fresh else f" (conversation {sid})") + note


def cmd_resume(a):
    try:
        print(resume_session(a.name, a.fresh))
    except (ValueError, RuntimeError) as e:
        die(str(e))
    return 0


def cmd_attach(a):
    if not tmux_alive(a.name):
        die(f"no live managed session {a.name!r}")
    verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
    os.execvp("tmux", ["tmux", verb, "-t", "=" + tmux_name(a.name)])


def cmd_kill(a):
    try:
        kill_session(a.name)
    except (ValueError, RuntimeError) as e:
        die(str(e))
    print(f"killed {a.name}")
    return 0


def cmd_rename(a):
    try:
        reply = sock_request({"type": "rename", "old": a.name, "new": a.new})
        if reply and reply.get("error"):
            die(reply["error"])
    except (OSError, ValueError):  # daemon not running: rename on our own
        try:
            rename_session(a.name, a.new)
        except (ValueError, RuntimeError) as e:
            die(str(e))
    print(f"renamed {a.name} -> {a.new}")
    return 0


def cmd_forget(a):
    if tmux_alive(a.name):
        die(f"{a.name} is running; kill it first")
    with registry_locked() as reg:
        if reg.pop(a.name, None) is None:
            die(f"unknown session {a.name!r}")
    notify_daemon({"type": "refresh"})
    return 0


def cmd_logs(a):
    try:
        sys.stdout.write(capture(a.name, a.lines))
    except (ValueError, RuntimeError) as e:
        die(str(e))
    return 0


def cmd_serve(a):
    import grovyd
    return grovyd.serve(a.bind, a.port)


def lan_bases(url):
    """http://HOST:PORT for each address the daemon is reachable on (all IPv4 addresses when bound to 0.0.0.0)."""
    m = re.match(r"http://(.+):(\d+)/?", url or "")
    host, port = (m.group(1), m.group(2)) if m else ("127.0.0.1", "7333")
    hosts = [host]
    if host in ("0.0.0.0", "::"):
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
        hosts = [h for h in out if ":" not in h] or ["127.0.0.1"]
    return [f"http://{h}:{port}" for h in hosts]


def cmd_url(_):
    """Print the phone login link(s): open one once per device; it sets a cookie."""
    try:
        url = (sock_request({"type": "status"}) or {}).get("url", "")
    except (OSError, ValueError):
        die("grovyd not running (grovy serve)")
    tok = load_token()
    bases = lan_bases(url)
    for base in bases:
        print(base + "/" + (f"?token={tok}" if tok else ""))
    if all(re.match(r"http://(127\.|localhost|\[::1\])", b) for b in bases):
        print("note: the daemon only listens on this computer, so these links won't open on a phone.\n"
              "Restart it with --bind 0.0.0.0 (or your VPN address), or set GROVY_BIND.", file=sys.stderr)
    if not tok:
        print("(no token: anyone who can reach these addresses can use grovy)", file=sys.stderr)
    return 0


def cmd_doctor(_):
    ok = True

    def check(label, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'ok' if good else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))

    check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    t = shutil.which("tmux")
    check("tmux", t, tmux("-V").stdout.strip() if t else "not found")
    c = shutil.which(claude_cmd())
    ver = subprocess.run([c, "--version"], capture_output=True, text=True).stdout.strip() if c else "not found"
    check("claude", c, ver)
    sd = state_dir()
    check("state dir private", (os.stat(sd).st_mode & 0o077) == 0, sd)
    try:
        st = sock_request({"type": "status"})
        check("daemon socket", True, f"{len(st.get('sessions', {}))} session(s) known")
        check("daemon http", True, st.get("url", ""))
        if "claude" in st:   # the daemon's PATH can differ from this shell's (systemd)
            check("daemon finds claude", st["claude"], st["claude"] or f"{claude_cmd()!r} not on the daemon's PATH "
                  "(set PATH or GROVY_CLAUDE in its environment)")
    except (OSError, ValueError) as e:
        print(f"[info] daemon not running ({e}): start it with `grovy serve`")
    live, reg = tmux_managed(), registry()
    for n in sorted(live):
        hs = os.path.join(sd, "hooks", f"{n}.json")
        check(f"session {n}: hooks file", os.path.exists(hs), hs)
    for n in sorted(set(reg) - set(live)):
        print(f"[info] session {n}: exited (grovy resume {n} / grovy forget {n})")
    return 0 if ok else 1


SERVICE_UNIT = """\
# Written by `grovy setup`; rerun it to change the address or port (or edit here, then
# systemctl --user daemon-reload && systemctl --user restart grovy).
[Unit]
Description=grovy - Claude Code session manager daemon

[Service]
ExecStart={exe} serve
Environment=GROVY_BIND={bind}
Environment=GROVY_PORT={port}
# The PATH of the shell that ran setup: claude (and the Claude sessions' Bash tool) need it.
Environment=PATH={path}
Restart=on-failure
RestartSec=2
# Essential: if this daemon starts the tmux server (first session from the phone), tmux is in this
# unit's cgroup, and the default KillMode would kill tmux and every Claude session on restart or crash.
KillMode=process

[Install]
WantedBy=default.target
"""


def service_unit(bind, port, path=None):
    path = path if path is not None else os.environ.get("PATH", "/usr/bin:/bin")
    path = ":".join(dict.fromkeys(p for p in path.split(":") if p))   # de-duplicated, order kept
    q = lambda v: str(v).replace("%", "%%")   # noqa: E731  systemd expands %-specifiers
    return SERVICE_UNIT.format(exe=q(os.path.join(HERE, "grovy")), bind=q(bind), port=int(port), path=q(path))


def _ask(prompt, default):
    try:
        got = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        got = ""
    return got or default


def _pick_bind(assume_yes):
    out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
    addrs = [a for a in out if ":" not in a]
    vpn = [a for a in addrs if ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10")]   # Tailscale
    choices = [("127.0.0.1", "this computer only (no phone access)"),
               ("0.0.0.0", "every network this computer is on")] + \
              [(a, "Tailscale/VPN address" if a in vpn else "this address only") for a in addrs]
    default = vpn[0] if vpn else "0.0.0.0"
    if assume_yes or not sys.stdin.isatty():
        return default
    print("Where should the phone reach grovy? Only expose it on networks you trust (it is plain HTTP).")
    for i, (a, what) in enumerate(choices, 1):
        print(f"  {i}) {a:<16} {what}")
    got = _ask("Address or number", default)
    if got.isdigit() and 1 <= int(got) <= len(choices):
        return choices[int(got) - 1][0]
    ipaddress.ip_address(got)   # ValueError on garbage
    return got


def cmd_setup(a):
    """One-time install: link grovy onto PATH, write and start the systemd user service, print the phone link."""
    dry = a.dry_run
    step = lambda msg: print(("(dry run) " if dry else "") + msg)  # noqa: E731
    for tool in ("tmux", claude_cmd()):
        if not shutil.which(tool):
            die(f"{tool} not found on PATH: install it first" + (" (or set GROVY_CLAUDE)" if tool != "tmux" else ""))
    # 1. grovy on PATH
    bindir = os.path.expanduser("~/.local/bin")
    link, target = os.path.join(bindir, "grovy"), os.path.join(HERE, "grovy")
    if os.path.realpath(link) == os.path.realpath(target):
        step(f"ok: {link} already points here")
    elif os.path.lexists(link):
        step(f"skipped: {link} exists and points elsewhere ({os.path.realpath(link)}); remove it to use this copy")
    else:
        step(f"linking {link} -> {target}")
        if not dry:
            os.makedirs(bindir, exist_ok=True)
            os.symlink(target, link)
    if bindir not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: {bindir} is not on your PATH; add it in your shell profile: export PATH=\"$HOME/.local/bin:$PATH\"")
    # 2. token and custom.css template
    if not dry:
        load_token(create=True)
    css = os.path.join(config_dir(), "custom.css")
    if not os.path.exists(css):
        step(f"creating {css} (your own styles; see the comments in it)")
        if not dry:
            os.makedirs(config_dir(), exist_ok=True)
            shutil.copyfile(os.path.join(HERE, "examples", "custom.css"), css)
    # 3. the daemon as a service
    bind = a.bind or _pick_bind(a.yes)
    ipaddress.ip_address(bind)
    unit = service_unit(bind, a.port)
    running = False
    try:
        running = bool(sock_request({"type": "status"}, timeout=1.0))
    except (OSError, ValueError):
        pass
    systemd = not a.no_service and shutil.which("systemctl") and subprocess.run(
        ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    if a.no_service or not systemd:
        if not a.no_service:
            print("no systemd user session here: start the daemon yourself, e.g. in tmux:")
        print(f"  tmux new -d -s grovyd 'GROVY_BIND={bind} GROVY_PORT={a.port} {target} serve'")
    else:
        path = os.path.expanduser("~/.config/systemd/user/grovy.service")
        old = open(path).read() if os.path.exists(path) else None
        if old == unit:
            step(f"ok: {path} is up to date")
        else:
            step(f"writing {path}" + (" (previous version kept as grovy.service.bak)" if old else ""))
            if dry:
                print(unit)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if old:
                    shutil.copyfile(path, path + ".bak")
                with open(path, "w") as f:
                    f.write(unit)
        active = subprocess.run(["systemctl", "--user", "is-active", "grovy"], capture_output=True,
                                text=True, timeout=10).stdout.strip() == "active"
        if running and not active:
            step("enabling the service for the next boot; a grovy daemon is already running outside it, so it "
                 "isn't started now.\n  Stop that one (sessions are unaffected), then: systemctl --user start grovy")
            cmds = [["daemon-reload"], ["enable", "grovy"]]
        else:
            step("starting the service and enabling it at boot")
            cmds = [["daemon-reload"], ["enable", "grovy"], ["restart", "grovy"]]
        for c in cmds if not dry else []:
            r = subprocess.run(["systemctl", "--user", *c], capture_output=True, text=True, timeout=30)
            if r.returncode:
                die(f"systemctl --user {' '.join(c)} failed: {r.stderr.strip()}")
        linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if linger != "Linger=yes":
            step("enabling linger, so the daemon runs after a reboot without you logging in")
            if not dry and subprocess.run(["loginctl", "enable-linger"], capture_output=True, timeout=30).returncode:
                print("note: `loginctl enable-linger` failed; without it the daemon starts when you log in")
    if dry:
        return 0
    # 4. the phone link
    for _ in range(50):
        try:
            if sock_request({"type": "status"}, timeout=1.0):
                break
        except (OSError, ValueError):
            time.sleep(0.2)
    else:
        print("the daemon isn't answering yet; when it is, run `grovy url` for the phone link")
        return 0
    print("\nOpen this once on your phone (it logs the phone in):")
    cmd_url(None)
    print("\nThen tap + to start a session. New projects: run `claude` once in the folder first, to accept its "
          "trust prompt.\nCustomize the look in " + css + "; check everything with `grovy doctor`.")
    return 0


def cmd_page(a):
    """Publish an HTML page to the phone (a sandboxed local "artifact"), or list/remove this session's pages."""
    session = a.session or env("SESSION")
    if a.op == "ls" and a.all:
        session = None
    elif not session:
        print("grovy page: no session: run this inside a grovy session or pass --session NAME", file=sys.stderr)
        return 2
    msg = {"type": "page", "op": a.op, "session": session}
    if a.op == "publish":
        try:
            with open(a.file, "rb") as f:
                html = f.read().decode("utf-8", "surrogateescape")
        except OSError as e:
            print(f"grovy page: {e}", file=sys.stderr)
            return 1
        stem = os.path.splitext(os.path.basename(a.file))[0]
        msg.update(html=html, title=a.title, name=a.name or re.sub(r"[^A-Za-z0-9_.-]", "-", stem).lstrip("-._")[:64] or "page")
    elif a.op == "rm":
        msg["name"] = a.name
    try:
        reply = sock_request(msg, timeout=30) or {}
    except (OSError, ValueError) as e:
        print(f"grovy page: daemon unreachable ({e})", file=sys.stderr)
        return 1
    if "error" in reply:
        print(f"grovy page: {reply['error']}", file=sys.stderr)
        return 1
    res = reply.get("ok")
    if a.op == "publish":
        print(f"published {res['name']} (version {res['version']}): it's in the chat on the phone. {res.get('url', '')}")
    elif a.op == "ls":
        for p in res:
            print((f"{p['session']:<16} " if a.all else "") + f"{p['name']:<24} v{p['version']:<4} "
                  + ("shared  " if p.get("share") else "        ") + p["title"])
        if not res:
            print("no pages")
    else:
        print(res)
    return 0


def _live_cli(key, spec, status="running", message=None):
    """Live widget from the command line: report problems on stderr, but never fail the caller's pipeline."""
    if not active():
        print("grovy: not in a grovy session; nothing shown", file=sys.stderr)
        return 0
    r = _live_send(key, spec, status, message)
    if r is None:
        print("grovy: the grovy daemon is unreachable; nothing shown", file=sys.stderr)
    return 0


def _status(a):
    return ("failed", a.failed) if a.failed is not None else ("done", None) if a.done else ("running", None)


def cmd_note(a):
    return _live_cli(_live_key(), {"type": "note", "text": " ".join(a.text), "tone": a.tone, "title": a.title}, "done")


def cmd_show(a):
    src = sys.stdin.read() if a.json == "-" else a.json
    try:
        spec = json.loads(src)
    except ValueError as e:
        print(f"grovy show: not valid JSON: {e}", file=sys.stderr)
        return 2
    status, message = _status(a)
    return _live_cli(a.key or _live_key(), spec, status if a.key else "done", message)


def cmd_progress(a):
    item = {"label": a.label or "", "value": a.value}
    if a.max is None:
        item["indeterminate"] = True
    else:
        item["max"] = a.max
    if a.note:
        item["note"] = a.note
    status, message = _status(a)
    return _live_cli(a.key, {"type": "progress", "title": a.title, "items": [item], "eta": True}, status, message)


def cmd_tail(a):
    """Copy stdin to stdout unchanged, showing its last lines live on the phone."""
    w = Log(a.title, a.lines, a.key)
    out = sys.stdout.buffer
    try:
        for line in iter(sys.stdin.buffer.readline, b""):
            try:
                out.write(line)
                out.flush()
            except BrokenPipeError:   # downstream closed: keep reading so the producer isn't killed by us
                out = open(os.devnull, "wb")
            w.write(line)
    except KeyboardInterrupt:
        w._close("stopped", "interrupted")
        return 130
    w.done()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="grovy", description="Minimal Claude Code session manager")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new", help="start Claude Code in a new tmux session")
    s.add_argument("name"), s.add_argument("path")
    s.add_argument("claude_args", nargs=argparse.REMAINDER, help="extra claude arguments, after --")
    s.set_defaults(fn=cmd_new)
    sub.add_parser("ls", help="list sessions").set_defaults(fn=cmd_ls)
    for cmd, fn, h in (("attach", cmd_attach, "attach to the tmux session"),
                       ("kill", cmd_kill, "terminate a session"),
                       ("forget", cmd_forget, "remove an exited session from the registry")):
        s = sub.add_parser(cmd, help=h)
        s.add_argument("name")
        s.set_defaults(fn=fn)
    s = sub.add_parser("rename", help="rename a session (works while it runs)")
    s.add_argument("name"), s.add_argument("new")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("resume", help="restart an exited session, resuming its Claude conversation")
    s.add_argument("name"), s.add_argument("--fresh", action="store_true", help="start a new conversation")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("logs", help="print recent tmux scrollback")
    s.add_argument("name"), s.add_argument("-n", "--lines", type=int, default=200)
    s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("serve", help="run the daemon")
    s.add_argument("--bind", default=env("BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=env_int("PORT", 7333))
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("statusline", help="(called by Claude Code) forward status info, print the user's status line")
    s.add_argument("name")
    s.set_defaults(fn=lambda a: cmd_statusline(a.name))
    s = sub.add_parser("hook", help="(called by Claude Code) process one hook event from stdin")
    s.add_argument("name")
    s.set_defaults(fn=lambda a: cmd_hook(a.name))
    s = sub.add_parser("setup", help="one-time install: PATH link, systemd service, phone link")
    s.add_argument("--bind", help="address to listen on (asked if omitted)")
    s.add_argument("--port", type=int, default=env_int("PORT", 7333))
    s.add_argument("--no-service", action="store_true", help="don't install the systemd service")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask; use the suggested address")
    s.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("page", help="publish an HTML page to the phone (sandboxed), or list/remove pages")
    ps = s.add_subparsers(dest="op", required=True)
    pp = ps.add_parser("publish", help="publish FILE; publishing the same name again updates it")
    pp.add_argument("file"), pp.add_argument("--title"), pp.add_argument("--name", help="default: the file name")
    pr = ps.add_parser("rm", help="remove a page")
    pr.add_argument("name")
    pl = ps.add_parser("ls", help="list pages")
    pl.add_argument("--all", action="store_true", help="pages of every session")
    for x in (pp, pr, pl):
        x.add_argument("--session", help="default: $GROVY_SESSION")
        x.set_defaults(fn=cmd_page)
    s = sub.add_parser("note", help="show a markdown note in this session's phone chat, now")
    s.add_argument("text", nargs="+"), s.add_argument("--title"), s.add_argument("--tone", choices=("ok", "warn", "bad"))
    s.set_defaults(fn=cmd_note)
    s = sub.add_parser("show", help="show a widget (JSON, or - for stdin); with --key, later calls update it")
    s.add_argument("json"), s.add_argument("--key")
    s = [s, sub.add_parser("progress", help="show or update a live progress bar: grovy progress KEY VALUE --max N")]
    s[1].add_argument("key"), s[1].add_argument("value", type=float), s[1].add_argument("--max", type=float)
    s[1].add_argument("--title"), s[1].add_argument("--label"), s[1].add_argument("--note")
    for x, fn in zip(s, (cmd_show, cmd_progress)):
        g = x.add_mutually_exclusive_group()
        g.add_argument("--done", action="store_true", help="mark it finished")
        g.add_argument("--failed", metavar="MESSAGE", help="mark it failed")
        x.set_defaults(fn=fn)
    s = sub.add_parser("tail", help="copy stdin to stdout, showing its last lines live on the phone")
    s.add_argument("--title"), s.add_argument("--lines", type=int, default=40), s.add_argument("--key")
    s.set_defaults(fn=cmd_tail)
    sub.add_parser("doctor", help="check dependencies and connectivity").set_defaults(fn=cmd_doctor)
    sub.add_parser("url", help="print the web UI link(s), including the login token").set_defaults(fn=cmd_url)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
