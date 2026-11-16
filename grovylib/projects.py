"""Claude's own files: trusted folders and past conversations."""
import json
import os
import re
import stat


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
