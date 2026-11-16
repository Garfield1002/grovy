#!/usr/bin/env python3
"""ccs - minimal Claude Code session manager (CLI, tmux helpers, hook adapter).

Identity model:
  ccs name   "api"          stable, human-readable, validated
  tmux name  "ccs-api"      tagged with tmux option @ccs=api, always targeted exactly ("=ccs-api")
  claude id  session_id     from hooks; may change over a tmux session's life (/clear, /resume)

Hooks are injected per managed session with `claude --settings FILE`, whose command is
`ccs hook NAME`. The session identity therefore comes from argv, never from cwd.
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
TMUX_PREFIX = "ccs-"

# Events the adapter forwards. Anything else is ignored (exit 0, no output).
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PermissionRequest",
               "Notification", "Stop", "StopFailure", "PostToolUse")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


PERMISSION_TIMEOUT = env_int("CCS_PERMISSION_TIMEOUT", 600)  # seconds the phone gets to decide


def default_state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "ccs")


def state_dir():
    d = os.environ.get("CCS_STATE_DIR") or default_state_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def token_path():
    return os.path.join(state_dir(), "token")


def load_token(create=False):
    """Shared secret for the web UI (state dir, mode 0600). CCS_TOKEN overrides the file."""
    if os.environ.get("CCS_TOKEN"):
        return os.environ["CCS_TOKEN"]
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
    p = os.path.join(state_dir(), "ccsd.sock")
    if len(p.encode()) < 100:
        return p
    # AF_UNIX paths are limited to ~108 bytes; fall back to the private per-user runtime dir.
    import hashlib
    rt = os.environ.get("XDG_RUNTIME_DIR") or state_dir()
    return os.path.join(rt, "ccs-" + hashlib.sha256(state_dir().encode()).hexdigest()[:12] + ".sock")


def die(msg, code=1):
    print(f"ccs: {msg}", file=sys.stderr)
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
        print(f"ccs: ignoring unreadable {path}: {e}", file=sys.stderr)
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
    """{ccs name: pane_id} for live tmux sessions carrying our @ccs marker AND owned by this state dir
    (@ccs_state; sessions predating that tag belong to the default dir). tmux is authoritative."""
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{@ccs}\t#{pane_id}\t#{@ccs_state}", check=False)
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
    return os.environ.get("CCS_CLAUDE", "claude")


def ccs_cmd():
    """argv prefix that runs this CLI regardless of PATH or cwd."""
    return [sys.executable, os.path.join(HERE, "ccs.py")]


def hook_settings(name):
    """Settings file passed to claude via --settings; registers `ccs hook NAME` for our events."""
    cmd = " ".join(_shquote(a) for a in ccs_cmd() + ["hook", name])

    def entry(timeout):
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}]

    hooks = {ev: entry(10) for ev in HOOK_EVENTS}
    # The hook gives up on the daemon at PERMISSION_TIMEOUT+10, well before Claude kills it.
    hooks["PermissionRequest"] = [{"matcher": "*", **entry(PERMISSION_TIMEOUT + 30)[0]}]
    hooks["PostToolUse"] = [{"matcher": "*", **entry(10)[0]}]
    path = os.path.join(state_dir(), "hooks", f"{name}.json")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    atomic_write_json(path, {"hooks": hooks})
    return path


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
    if not shutil.which(claude_cmd()):
        raise ValueError(f"{claude_cmd()!r} not found on PATH (set CCS_CLAUDE)")
    settings = hook_settings(name)
    argv = [claude_cmd(), "--settings", settings, *extra_args]
    # Multiple trailing args => tmux execs them directly, no shell, so paths/names are never interpolated.
    r = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", tmux_name(name), "-c", cwd,
             "-x", "200", "-y", "50", "-e", f"CCS_SESSION={name}", "-e", f"CCS_STATE_DIR={state_dir()}",
             "-e", f"CCS_PERMISSION_TIMEOUT={PERMISSION_TIMEOUT}", "--", *argv)
    pane = r.stdout.strip()
    tmux("set-option", "-t", "=" + tmux_name(name) + ":", "@ccs_state", state_dir())
    tmux("set-option", "-t", "=" + tmux_name(name) + ":", "@ccs", name)
    with registry_locked() as reg:
        prev = reg.get(name, {})
        args = list(extra_args)
        if "--resume" in args:  # remember the session's own flags (e.g. permission mode), not the resume id
            i = args.index("--resume")
            del args[i:i + 2]
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


def trusted_projects():
    """Folders a phone may start a session in: ones Claude already trusts (trust is inherited from
    any trusted ancestor, as observed with Claude Code 2.1.325), so no trust dialog can block hooks.
    Candidates are Claude's known projects plus git repos directly inside a trusted folder."""
    projects = _claude_projects()
    roots = [os.path.realpath(k) for k, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted")]

    def trusted(path):
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
    who = tmux("display-message", "-p", "-t", pane, "#{session_name}\t#{@ccs}", check=False)
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
        buf = f"ccs-{name}-{os.getpid()}-{time.monotonic_ns()}"
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
    """Send one JSON line to ccsd; optionally read one JSON line back. Raises OSError if unreachable."""
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
        decision["message"] = message or "Denied from the ccs web interface."
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


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
        print(f"ccs hook: malformed input: {e}", file=sys.stderr)
        return 0
    if not valid_name(name):
        print(f"ccs hook: invalid session name {name!r}", file=sys.stderr)
        return 0
    event = payload.get("hook_event_name")
    if event not in HOOK_EVENTS:
        return 0
    if event == "SessionStart" and isinstance(payload.get("session_id"), str):
        # Recorded here, not only in the daemon, so `ccs resume` works even if ccsd was down.
        try:
            with registry_locked() as reg:
                if name in reg:
                    reg[name]["claude_session_id"] = payload["session_id"]
        except OSError as e:
            print(f"ccs hook: registry update failed: {e}", file=sys.stderr)
    msg = {"type": "hook", "ccs": name, "event": payload, "pid": os.getppid()}
    if event != "PermissionRequest":
        try:
            sock_request(msg, timeout=2.0, wait_reply=False)
        except OSError as e:
            print(f"ccs hook: daemon unreachable ({e}); event not recorded", file=sys.stderr)
        return 0
    try:
        reply = sock_request(msg, timeout=PERMISSION_TIMEOUT + 10)
    except (OSError, ValueError) as e:
        print(f"ccs hook: no remote decision ({e}); falling back to terminal", file=sys.stderr)
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
        print("(ccsd not reachable: state shown from tmux only)", file=sys.stderr)
    return 0


def cmd_new(a):
    try:
        extra = [x for x in a.claude_args if x != "--"][:20]
        start_session(a.name, a.path, extra)
        check_started(a.name, a.path, extra)
    except (ValueError, RuntimeError) as e:
        die(str(e))
    print(f"started {a.name} in tmux session {tmux_name(a.name)}  (ccs attach {a.name})")
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
    import ccsd
    return ccsd.serve(a.bind, a.port)


def cmd_url(_):
    """Print the phone login link(s): open one once per device; it sets a cookie."""
    try:
        url = (sock_request({"type": "status"}) or {}).get("url", "")
    except (OSError, ValueError):
        die("ccsd not running (ccs serve)")
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
        print("(no token: anyone who can reach these addresses can use ccs)", file=sys.stderr)
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
        check("daemon socket", False, f"{e} (run: ccs serve)")
    live, reg = tmux_managed(), registry()
    for n in sorted(live):
        hs = os.path.join(sd, "hooks", f"{n}.json")
        check(f"session {n}: hooks file", os.path.exists(hs), hs)
    for n in sorted(set(reg) - set(live)):
        print(f"[info] session {n}: exited (ccs resume {n} / ccs forget {n})")
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="ccs", description="Minimal Claude Code session manager")
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
    s = sub.add_parser("resume", help="restart an exited session, resuming its Claude conversation")
    s.add_argument("name"), s.add_argument("--fresh", action="store_true", help="start a new conversation")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("logs", help="print recent tmux scrollback")
    s.add_argument("name"), s.add_argument("-n", "--lines", type=int, default=200)
    s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("serve", help="run the daemon")
    s.add_argument("--bind", default=os.environ.get("CCS_BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=env_int("CCS_PORT", 7333))
    s.set_defaults(fn=cmd_serve)
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
