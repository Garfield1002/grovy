"""New projects: a blank git repository, or a clone of a remote, in the projects folder (~/dev), with a session in it.

⋯ → New project (extensions/projects.js) makes one in two steps. First grovy makes the folder (`git init`, or
`git clone`) and lists what in it Claude loads on trust. Then, once you have said so, it starts a session there and
answers Claude's workspace-trust question for you: a new git repository is a trust boundary of its own, so Claude
asks, and every hook waits until it's answered. A clone is never trusted without you looking at that list on the
phone first (its own .claude/settings.json and .mcp.json can run commands). See docs/DESIGN.md, "New projects".
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time

from . import core, projects, sessions, tmuxctl

CLONE_TIMEOUT = 300
MAX_TEXT = 4000
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# https (no user or password in it: the address is shown and logged), ssh://, or scp-like user@host:path; a host
# always starts with a letter or digit (never an option for ssh)
HOST = r"[A-Za-z0-9][A-Za-z0-9.-]*"
URL_RE = re.compile(rf"(https://{HOST}(:\d+)?/[^\s@]+|ssh://([A-Za-z0-9._-]+@)?{HOST}(:\d+)?/\S+|[A-Za-z0-9._-]+@{HOST}:\S+)")
REVIEW_MAX = 40
REVIEW_DEPTH = 4          # folders deep that nested CLAUDE.md and .claude/ are looked for
COMMANDS_MAX = 6          # commands quoted per settings file
SETTINGS_MAX = 1 << 20    # bytes of a settings file read for the review
# The workspace-trust question, as Claude Code 2.1.328 draws it (verified): "❯" marks the highlighted answer, and the
# highlighted one at first is "No, exit".
TRUST_YES, TRUST_NO = "Yes, I trust this folder", "No, exit"
TRUST_WAIT = 20
PRESS_AGAIN = 2.0         # a key that changed nothing in this long may have been read before Claude listened: again
PRESSES = 3               # at most, per key


class ProjectError(RuntimeError):
    pass


def projects_dir():
    return os.path.realpath(os.path.expanduser(core.env("PROJECTS_DIR") or "~/dev"))


def name_from_url(url):
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def session_name(folder, taken):
    """A session name for a project folder: its name in the characters sessions allow, made unique."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", folder).strip("-_")[:28].rstrip("-_") or "project"
    return next(n for n in [stem] + [f"{stem}-{i}" for i in range(2, 1000)] if not taken(n))


def _run(args, cwd, timeout):
    """git, never asking anything: no credential prompt, and no terminal for ssh to ask about a host key on (a new
    process session has none), so a private repo without a key fails at once instead of hanging. On a timeout the
    whole process group goes (git's helpers too: ssh, git-remote-https, index-pack), so nothing writes on after it.
    Git LFS fetches nothing: the repo's own .lfsconfig would choose where from, before you've seen it."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_LFS_SKIP_SMUDGE="1")
    try:
        p = subprocess.Popen(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             errors="replace", env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise ProjectError(f"can't run git: {e}") from None
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise ProjectError(f"git {args[0]} took more than {timeout}s") from None
    if p.returncode:
        raise ProjectError((err or out).strip()[-600:] or f"git {args[0]} failed")


def check(kind, name, url):
    """The folder a new project would get, and the clone address: ValueError if either won't do."""
    if kind == "clone":
        if not isinstance(url, str) or len(url) > 500 or not URL_RE.fullmatch(url.strip()):
            raise ValueError("give the repository's https:// or ssh address (git@host:owner/repo.git)")
        url = url.strip()
        name = name or name_from_url(url)
    elif kind != "blank":
        raise ValueError("kind: blank or clone")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("name: letters, digits, '.', '-' and '_' (max 64), starting with a letter or digit")
    return os.path.join(projects_dir(), name), url


def make(kind, dest, url):
    """Make the project's folder: `git init` (blank), or `git clone url`. Never touches a folder that already
    exists; a failed one leaves nothing behind."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.lexists(dest):
        raise ProjectError(f"{dest} already exists")
    try:
        if kind == "blank":
            os.mkdir(dest)
            _run(["init", "-q"], dest, 30)
        else:   # only https and ssh: no ext:: (runs a command) or file transport; no submodules
            _run(["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                  "clone", "-q", "--", url, dest], os.path.dirname(dest), CLONE_TIMEOUT)
    except BaseException:
        if os.path.isdir(dest) and not os.path.islink(dest):
            shutil.rmtree(dest, ignore_errors=True)
        raise


def _commands(path):
    """The commands a settings file (or .mcp.json) names: hooks, MCP servers, status line, key helpers..."""
    try:
        if os.path.getsize(path) > SETTINGS_MAX:
            return ["(too big to read here: Claude may still load it; don't trust it unless you know it)"]
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:   # unreadable, not JSON, nested too deep to walk...
        return ["(can't be read here: Claude may still load it; don't trust it unless you know it)"]
    out = []

    def walk(x, key=""):
        if isinstance(x, dict):
            if isinstance(x.get("command"), str):
                args = x.get("args") if isinstance(x.get("args"), list) else []
                out.append(" ".join([x["command"], *map(str, args)]))
            elif isinstance(x.get("url"), str) and key != "permissions":
                out.append(x["url"])
            for k, v in x.items():
                if k in ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper") and \
                        isinstance(v, str):
                    out.append(v)
                elif k in ("env", "enabledPlugins", "extraKnownMarketplaces") and isinstance(v, dict):
                    out.append(f"{k}: " + ", ".join(map(str, v))[:200])
                elif k != "command":
                    walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
    try:
        walk(data)
    except RecursionError:
        out.append("(nested too deep to read all of it)")
    more = [f"… and {len(out) - COMMANDS_MAX} more"] if len(out) > COMMANDS_MAX else []
    return [c if len(c) <= 160 else c[:159] + "…" for c in out[:COMMANDS_MAX]] + more


def review(path):
    """What Claude would load from the project once it is trusted: settings that can run commands (with the commands
    they name) and what else it reads (agents, skills, instructions), nested ones and symlinks included (a symlink
    within the clone is reviewed as what it points to; one leading out of it is flagged).
    [{file, what, runs, commands?}], the ones that can run commands first."""
    out = []
    top = os.path.realpath(path)

    def settings_like(rel):
        return rel.endswith(".mcp.json") or bool(re.search(r"(^|/)\.claude/(.*/)?settings[^/]*\.json$", rel))

    def add(rel, what, full):
        r = {"file": rel, "what": what, "runs": settings_like(rel)}
        if os.path.islink(full):
            target = os.path.realpath(full)
            if target != top and not target.startswith(top + os.sep):
                r.update(what=f"symlink out of the project → {os.readlink(full)} ({what})", runs=True)
                out.append(r)
                return
            r["what"] = f"{what} (symlink → {os.readlink(full)})"
        if r["runs"] and os.path.isfile(full):
            r["commands"] = _commands(full)
        out.append(r)

    def claude_dir(real, shown, seen):
        """Everything in a .claude folder (or a symlinked folder inside one): all of it is loaded."""
        for root, dirs, files in os.walk(real):
            rel = os.path.join(shown, os.path.relpath(root, real)) if root != real else shown
            for d in sorted(dirs):
                full = os.path.join(root, d)
                if os.path.islink(full):
                    add(os.path.join(rel, d), "a folder Claude loads", full)
                    t = os.path.realpath(full)
                    if (t.startswith(top + os.sep)) and t not in seen and len(seen) < 20:
                        seen.add(t)
                        claude_dir(t, os.path.join(rel, d), seen)
            dirs.sort()
            for f in sorted(files):
                runs = f.startswith("settings") and f.endswith(".json")
                add(os.path.join(rel, f), "settings: hooks and commands Claude runs" if runs else
                    "loaded by Claude (agents, commands, skills...)", os.path.join(root, f))
            if len(out) > REVIEW_MAX * 4:
                return

    if os.path.lexists(os.path.join(path, ".mcp.json")):
        add(".mcp.json", "MCP servers: commands Claude starts", os.path.join(path, ".mcp.json"))
    for root, dirs, files in os.walk(path):
        rel_root = os.path.relpath(root, path)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        if ".claude" in dirs or (".claude" in files and os.path.islink(os.path.join(root, ".claude"))):
            full = os.path.join(root, ".claude")
            shown = os.path.normpath(os.path.join(rel_root, ".claude"))
            if os.path.islink(full):
                add(shown, "the folder of Claude settings", full)
                t = os.path.realpath(full)
                if t.startswith(top + os.sep) and os.path.isdir(t):
                    claude_dir(t, shown, {t})
            else:
                claude_dir(full, shown, set())
        dirs[:] = sorted(d for d in dirs if d not in (".git", ".claude") and depth < REVIEW_DEPTH)
        for f in sorted(files):
            if f in ("CLAUDE.md", "CLAUDE.local.md"):
                add(os.path.normpath(os.path.join(rel_root, f)), "instructions Claude reads", os.path.join(root, f))
        if len(out) > REVIEW_MAX * 4:
            break
    out.sort(key=lambda r: not r["runs"])
    if len(out) > REVIEW_MAX:
        out = out[:REVIEW_MAX] + [{"file": f"… and {len(out) - REVIEW_MAX} more", "what": "not listed", "runs": False}]
    return out


def safe_review(path):
    try:
        return review(path)
    except Exception as e:   # never strand a clone the page has no token for
        return [{"file": "(the review failed)", "what": f"{type(e).__name__}: don't trust it unless you know it",
                 "runs": True}]


def trust_question(screen):
    """Which answer of Claude's trust question is highlighted on `screen`: "yes", "no", or None when the question
    isn't there, or the screen has more than one highlighted line (something else is drawn)."""
    if TRUST_YES not in screen or TRUST_NO not in screen:
        return None
    marked = [line.strip()[1:].strip() for line in screen.splitlines() if line.strip().startswith("❯")]
    if len(marked) != 1:
        return None
    return {TRUST_YES: "yes", TRUST_NO: "no"}.get(marked[0])


def accept_trust(name, path, wait=TRUST_WAIT):
    """Answer "Yes, I trust this folder" in the session's pane, and only that: each key is pressed while the visible
    screen shows the question with the expected answer highlighted (pressed again, a few times at most, if nothing
    changed: Claude may not have been listening yet). Done when Claude has recorded the trust itself (in
    ~/.claude.json, which grovy never writes). Raises RuntimeError otherwise, having pressed nothing else."""
    deadline = time.monotonic() + wait
    presses = {"Down": [], "Enter": []}   # (when, screen then)
    while time.monotonic() < deadline:
        if projects.folder_trusted(path):
            return
        screen = tmuxctl.screen(name)
        key = {"no": "Down", "yes": "Enter"}.get(trust_question(screen))
        if key:
            sent = presses[key]
            # again only if the screen is just as it was when it was pressed: the key was dropped, not slow
            if not sent or (len(sent) < PRESSES and time.monotonic() - sent[-1][0] > PRESS_AGAIN
                            and screen == sent[-1][1]):
                tmuxctl.send_key(name, key)
                sent.append((time.monotonic(), screen))
        time.sleep(0.25)
    raise RuntimeError("Claude's trust question didn't get answered: answer it in the terminal")


class NewProjects:
    """New projects (blank or cloned) and their first session: part of Store.

    self.fresh: {path: {kind, token}} for each folder new_project made that has no session yet (token None while
    it's being made). The token goes to the page that made it, and only that page can start or delete it."""

    def new_project(self, kind, name=None, url=None):
        try:
            dest, url = check(kind, name or None, url)
        except ValueError as e:
            return 400, str(e)
        with self.project_lock:
            old = self.fresh.get(dest)
            if old and old["token"] is None:
                return 409, f"{dest} is being made right now"
            if old:   # one made before and left behind (its sheet was closed, or another tab's): start over
                shutil.rmtree(dest, ignore_errors=True)
                self.fresh.pop(dest)
            elif os.path.lexists(dest):
                return 409, f"{dest} already exists"
            self.fresh[dest] = {"kind": kind, "token": None}   # held while git runs, outside the lock
        try:
            make(kind, dest, url)
        except Exception as e:   # anything: the reservation must go, or the name is blocked until a restart
            with self.project_lock:
                self.fresh.pop(dest, None)
            return 409, str(e) or type(e).__name__
        token = secrets.token_urlsafe(16)
        with self.project_lock:
            self.fresh[dest] = {"kind": kind, "token": token}
        return 200, {"path": dest, "kind": kind, "token": token, "review": safe_review(dest) if kind == "clone" else []}

    def _fresh(self, path, token):   # under project_lock
        f = self.fresh.get(path) if isinstance(path, str) else None
        if not f or not f["token"] or not isinstance(token, str) or not secrets.compare_digest(f["token"], token):
            return None
        return f

    def start_project(self, path, token, text="", ask=False, trust=False):
        """The first session in a project new_project made; a clone's only once you've said to trust it."""
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            return 400, f"the first message: at most {MAX_TEXT} characters"
        if text.lstrip()[:1] in ("/", "!"):
            return 400, "start with words: a leading / or ! would be taken as a command"
        with self.project_lock:
            f = self._fresh(path, token)
            if not f:
                return 404, "not the project you just made here (another tab made it again, or grovy restarted)"
            if f["kind"] == "clone" and trust is not True:
                return 400, "a clone is only trusted when you say so"
            reg = sessions.registry()
            live = self.tmux_live()

            def taken(n):
                return sessions.name_in_use(reg, n) or n in self.sessions or n in live or \
                    tmuxctl.tmux("has-session", "-t", "=" + tmuxctl.tmux_name(n), check=False).returncode == 0
            name = None
            try:
                name = session_name(os.path.basename(path), taken)
                self.lifecycle.start(name, path, ["--permission-mode", "default"] if ask is True else [])
            except Exception as e:   # tmux timeouts too
                there = name is not None and (name in self.tmux_live() or tmuxctl.tmux(
                    "has-session", "-t", "=" + tmuxctl.tmux_name(name), check=False).returncode == 0)
                if not there:   # nothing started: the project stays, to try again
                    return 409, str(e) or type(e).__name__
                # its tmux session is there all the same: never delete a folder Claude may be running in
                self.fresh.pop(path, None)
                self.reconcile()
                return 200, {"name": name, "queued": False, "warning": f"{e}: check the session in the terminal"}
            self.fresh.pop(path, None)
        self.reconcile()
        warning = None
        try:
            self.lifecycle.accept_trust(name, path)
        except Exception as e:   # the session is there either way: say so, and what to do
            warning = str(e) or "Claude's trust question didn't get answered: answer it in the terminal"
        queued = False
        if text.strip() and not warning:
            queued = self.enqueue(name, text.strip())[0] == 200   # typed once Claude is ready (hook-confirmed idle)
        return 200, {"name": name, "queued": queued, "warning": warning}

    def discard_project(self, path, token):
        """Delete a project new_project made that never got a session (you didn't trust the clone)."""
        with self.project_lock:
            if not self._fresh(path, token):
                return 404, "not the project you just made here"
            if os.path.dirname(path) != projects_dir():
                return 409, "not in the projects folder"
            self.fresh.pop(path, None)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            return 200, "deleted" if not os.path.lexists(path) else f"{path} couldn't be deleted entirely"
