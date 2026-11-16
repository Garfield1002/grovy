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
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

HERE = os.path.dirname(os.path.realpath(__file__))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
TMUX_PREFIX = "grovy-"

# Events the adapter forwards. Anything else is ignored (exit 0, no output).
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PermissionRequest",
               "Notification", "Stop", "StopFailure", "PostToolUse")


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


def tmux_alive(name):
    return name in tmux_managed()


def claude_cmd():
    return env("CLAUDE", "claude")


def grovy_cmd():
    """argv prefix that runs this CLI regardless of PATH or cwd."""
    return [sys.executable, os.path.join(HERE, "grovy.py")]


def hook_settings(name, file_name=None):
    """Settings file passed to claude via --settings; registers `grovy hook NAME` for our events.
    file_name lets a rename rewrite the file a running Claude already points at."""
    cmd = " ".join(_shquote(a) for a in grovy_cmd() + ["hook", name])

    def entry(timeout):
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}]

    hooks = {ev: entry(10) for ev in HOOK_EVENTS}
    # The hook gives up on the daemon at PERMISSION_TIMEOUT+10, well before Claude kills it.
    hooks["PermissionRequest"] = [{"matcher": "*", **entry(PERMISSION_TIMEOUT + 30)[0]}]
    hooks["PostToolUse"] = [{"matcher": "*", **entry(10)[0]}]
    path = os.path.join(state_dir(), "hooks", f"{file_name or name}.json")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # The status line feeds the phone (model, effort, context) and then shows the user's own status line.
    status = {"type": "command", "command": " ".join(_shquote(a) for a in grovy_cmd() + ["statusline", name])}
    own = user_statusline()
    if own and isinstance(own.get("padding"), int):
        status["padding"] = own["padding"]
    atomic_write_json(path, {"hooks": hooks, "statusLine": status})
    return path


def user_statusline(cwd=None):
    """The status line the user configured (local > project > user settings), unless it is ours."""
    paths = []
    if cwd:
        paths += [os.path.join(cwd, ".claude", "settings.local.json"), os.path.join(cwd, ".claude", "settings.json")]
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
        if valid_name(name) and isinstance(data, dict):
            msg = {"type": "statusline", "session": name, "data": {k: data.get(k) for k in STATUS_FIELDS}}
            sock_request(msg, timeout=0.3, wait_reply=False)
    except (ValueError, OSError):
        data = {}
    own = user_statusline((data or {}).get("cwd") if isinstance(data, dict) else None)
    if own:
        try:
            r = subprocess.run(["/bin/sh", "-c", own["command"]], input=raw, capture_output=True, text=True,
                               timeout=5, cwd=(data or {}).get("cwd") or None)
            sys.stdout.write(r.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


def _shquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


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
             "-e", f"GROVY_PERMISSION_TIMEOUT={PERMISSION_TIMEOUT}", "--", *argv)
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


def paste_prompt(name, text, command=False):
    """Deliver text to the session's input, then Enter. No shell anywhere.

    command=True types an allowlisted slash command. It always ends in a space before Enter:
    the space closes Claude Code's fuzzy command menu, so Enter can't pick a neighbour.

    The caller decides *whether* it is safe to type; this only does the typing. Raises if the
    session is gone or its pane changed owner.
    """
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
    else:
        buf = f"grovy-{name}-{os.getpid()}-{time.monotonic_ns()}"
        # Strip control characters (ESC especially): "\x1b[201~" would end the bracketed paste early
        # and turn the rest of the text into raw keystrokes.
        safe = "".join(c for c in text.replace("\r\n", "\n") if c in "\n\t" or c.isprintable())
        tmux("load-buffer", "-b", buf, "-", input=safe.encode())
        try:
            _own_pane(name)  # re-verify right before pasting
        except RuntimeError:
            tmux("delete-buffer", "-b", buf, check=False)
            raise
        tmux("paste-buffer", "-p", "-d", "-r", "-b", buf, "-t", pane)
    time.sleep(0.4)  # let the TUI ingest the input before submitting
    _own_pane(name)
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
You are running inside grovy: the user may be reading you on a phone, which renders the final message of each \
turn as markdown. That message can include widgets: a fenced code block with language `widget` whose body is one \
JSON object. grovy draws them itself (no HTML or scripts). Use one when it genuinely helps (numbers, progress, a \
decision, structure), not by default. The terminal shows the raw JSON, so keep the key facts in plain text too.
- chart: {"type":"chart","kind":"bar|line|pie","title":"..","labels":["a","b"],"series":[{"name":"..","values":[1,2]}],"unit":"ms"}
- stats: {"type":"stats","title":"..","items":[{"label":"Tests","value":"88/88","note":"..","tone":"ok|warn|bad"}]}
- progress: {"type":"progress","title":"..","items":[{"label":"..","value":3,"max":10}]}
- checklist: {"type":"checklist","title":"..","items":[{"label":"..","status":"done|todo|running|failed|skipped","note":".."}]}
- choices: {"type":"choices","question":"..","multi":false,"options":[{"label":"..","description":"..","message":"text to send if picked"}]}
- form: {"type":"form","title":"..","submit":"..","fields":[{"name":"k","label":"..","kind":"text|textarea|number|select|toggle","options":[".."],"default":".."}]}
- diagram: {"type":"diagram","direction":"down|right","nodes":[{"id":"a","label":"..","tone":"ok|warn|bad"}],"edges":[{"from":"a","to":"b","label":".."}]}
choices and form only fill the user's message box; the user reviews it and sends it as an ordinary prompt. \
Prefer them to asking the user to type out a pick. Keep widgets small: about 12 bars, 8 pie slices, 30 diagram nodes."""


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
    if event != "PermissionRequest":
        try:
            sock_request(msg, timeout=2.0, wait_reply=False)
        except OSError as e:
            print(f"grovy hook: daemon unreachable ({e}); event not recorded", file=sys.stderr)
        if event == "SessionStart" and env("WIDGETS", "1") != "0":   # also after /clear and /compact
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                "additionalContext": WIDGET_GUIDE}}))
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


def cmd_url(_):
    """Print the phone login link(s): open one once per device; it sets a cookie."""
    try:
        url = (sock_request({"type": "status"}) or {}).get("url", "")
    except (OSError, ValueError):
        die("grovyd not running (grovy serve)")
    tok = load_token()
    m = re.match(r"http://(.+):(\d+)/", url)
    host, port = (m.group(1), m.group(2)) if m else ("127.0.0.1", "7333")
    hosts = [host]
    if host in ("0.0.0.0", "::"):
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
        hosts = [h for h in out if ":" not in h] or ["127.0.0.1"]
    for h in hosts:
        print(f"http://{h}:{port}/" + (f"?token={tok}" if tok else ""))
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
    except (OSError, ValueError) as e:
        check("daemon socket", False, f"{e} (run: grovy serve)")
    live, reg = tmux_managed(), registry()
    for n in sorted(live):
        hs = os.path.join(sd, "hooks", f"{n}.json")
        check(f"session {n}: hooks file", os.path.exists(hs), hs)
    for n in sorted(set(reg) - set(live)):
        print(f"[info] session {n}: exited (grovy resume {n} / grovy forget {n})")
    return 0 if ok else 1


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
    sub.add_parser("doctor", help="check dependencies and connectivity").set_defaults(fn=cmd_doctor)
    sub.add_parser("url", help="print the web UI link(s), including the login token").set_defaults(fn=cmd_url)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
