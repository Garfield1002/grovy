"""Basics shared by everything: settings from the environment, paths, the login token, JSON files, names."""
import hashlib
import json
import os
import re
import secrets
import sys
import time


HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # the grovy folder
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
TMUX_PREFIX = "grovy-"


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


def attachments_dir():
    """Files sent from the phone. They're transient, so under the temp dir, in a directory private to the user
    (the daemon checks that before writing). Each session may read it without asking (session_settings)."""
    base = os.path.join(os.environ.get("TMPDIR") or "/tmp", f"grovy-{os.getuid()}")
    sd = env("STATE_DIR")
    if sd and os.path.realpath(sd) != os.path.realpath(default_state_dir()):   # a scratch daemon gets its own
        import hashlib
        base += "-" + hashlib.sha256(os.path.realpath(sd).encode()).hexdigest()[:8]
    return os.path.join(base, "attachments")


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


MAX_FIELD = 20000        # chars kept of any one text field in the event log

TOOL_ARG = ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "skill", "prompt")


def now():
    return time.time()


def norm(text):
    return "\n".join(l.rstrip() for l in str(text).replace("\r\n", "\n").replace("\r", "\n").strip().split("\n"))


PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")


def squash(text):
    """Comparison key for prompts: Claude wraps long pastes in <pasted_content> tags and re-spaces
    them, so drop the tags and collapse all whitespace."""
    return " ".join(PASTE_TAG.sub(" ", str(text)).split())


def clip(v, n=MAX_FIELD):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"\n… [{len(v) - n} chars truncated]"
    if isinstance(v, dict):
        return {k: clip(x, min(n, 4000)) for k, x in list(v.items())[:50]}
    if isinstance(v, list):
        return [clip(x, min(n, 4000)) for x in v[:50]] + ([f"… {len(v) - 50} more items not shown"] if len(v) > 50 else [])
    return v


def tool_summary(name, inp):
    """One short line for a tool call: its name and its most telling argument."""
    arg = ""
    if isinstance(inp, dict):
        arg = next((str(inp[k]) for k in TOOL_ARG if isinstance(inp.get(k), (str, int)) and str(inp[k]).strip()), "")
    arg = " ".join(arg.split())
    return {"tool": str(name or "tool")[:60], "arg": arg[:117] + "…" if len(arg) > 120 else arg}


ID_RE = re.compile(r"^[0-9a-f]{32}$")
