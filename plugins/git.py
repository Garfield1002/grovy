"""grovy's built-in git plugin: read-only answers about the git repository a session works in, for extensions/git.js
(the ⋯ → Git graph, and branch names and commit hashes in Claude's replies turned into links).

Routes (behind grovy's login, called with grovy.api() from the page):
    POST /api/x/git/resolve  {session, names: [..]}  -> which names are branches, tags or commits of that session's repo
    GET  /api/x/git/graph?session=S[&ref=R][&limit=N] -> recent commits with parents and refs, HEAD, upstream, changes
    GET  /api/x/git/commit?session=S&hash=H          -> one commit: message, author, files changed, its diff (capped)

Safety (docs/DESIGN.md, "Git", has the reasons): git only runs in the folder Claude last reported working in, never a
path from the request, with argument arrays. A ref from the request is used only if it is one of the repo's own ref
names, and a hash only if it is hexadecimal; both come after --end-of-options. That folder may hold a hostile repo:
nothing its config names is run (pager, fsmonitor, diff and textconv drivers, gpg, lazy fetch, its own clean filters).
Each command has a deadline that kills its whole process group, and its output is capped.
A plugin of your own named git.py (~/.config/grovy/plugins/) replaces this one.
"""
import os
import re
import signal
import subprocess
import tempfile
import threading
import time

# Settings a repository's own .git/config could use to make these read-only commands run a program: off.
# (Filter drivers, which `git status` runs, are switched off per repository: see changes().)
GIT = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "color.ui=false",
       "-c", "log.showSignature=false", "-c", "i18n.logOutputEncoding=UTF-8"]
# git older than 2.44 has no GIT_NO_LAZY_FETCH: a commit's files and diff could fetch (running the remote's uploadpack)
# in a partial clone, so they are refused there; and `git status` is skipped (its --show-scope needs 2.26).
MIN_GIT = (2, 44)
TIMEOUT = 10                  # seconds per git command
OUT_MAX = 4 * 1024 * 1024     # bytes read from one git command
DIFF_MAX = 200 * 1024         # characters of diff sent to the page
FILES_MAX = 300               # files listed for one commit
NAMES_MAX = 200               # names in one resolve request
LIMIT_MAX = 400               # commits in one graph
BUSY_MAX = 3                  # graphs and commits being read at once
HEX_RE = re.compile(r"[0-9a-f]{7,40}")
NAME_RE = re.compile(r"[A-Za-z0-9][\w./+-]{0,99}")
REF_KINDS = (("refs/heads/", "branch"), ("refs/remotes/", "remote"), ("refs/tags/", "tag"))
US = "\x1f"   # between for-each-ref fields (ref names can't contain control characters)


OLD_GIT = False   # set by setup(): git older than MIN_GIT


class GitError(Exception):
    status = 502


class NotFound(GitError):
    status = 404


class BadRequest(GitError):
    status = 400


class Unknown(LookupError):
    pass


def _env():
    """The daemon's environment without GIT_* (a stray GIT_DIR would point every command at another repository).
    GIT_NO_LAZY_FETCH: in a partial clone, a missing object must not start a fetch (network, and the remote's
    uploadpack / sshCommand settings run)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", LC_ALL="C")
    return env


def git(cwd, *args, stdin=None, limit=OUT_MAX, ok=(0,), raw=False):
    """Run git in cwd; its output as text (at most `limit` bytes: the rest is dropped and git stopped).
    git runs in a process group of its own, and the deadline kills the whole group: anything git started (a filter,
    gpg, ssh) would otherwise hold the output pipe open past it. stderr goes to a file, so it can't fill a pipe that
    nobody reads while stdout is being read. raw=True: (bytes, whether they were cut at `limit`)."""
    env = _env()
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(GIT + list(args), cwd=cwd, env=env, start_new_session=True,
                                 stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=err)
        except OSError as e:
            raise GitError(f"can't run git: {e}")

        def kill():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        timer = threading.Timer(TIMEOUT, kill)
        timer.start()
        try:
            if stdin:
                try:   # small inputs only (a few KB of names): written before reading, which can't deadlock
                    p.stdin.write(stdin.encode())
                    p.stdin.close()
                except OSError:   # git exited early: its exit status says why
                    pass
            out = p.stdout.read(limit + 1)
            cut = len(out) > limit
            if cut:
                kill()
                out = out[:limit]
            code = p.wait()
            timed_out = not timer.is_alive() and not cut
        finally:
            timer.cancel()
            p.stdout.close()
        if timed_out:
            raise GitError(f"git {args[0]} took longer than {TIMEOUT}s")
        if code not in ok and not cut:
            err.seek(0)
            raise GitError(err.read(4096).decode("utf-8", "replace").strip()[:300] or f"git {args[0]} failed")
    return (out, cut) if raw else out.decode("utf-8", "replace")


def refs(cwd):
    """{full ref name: {name, kind, hash, upstream?}} for branches, remote branches and tags (peeled).
    No ahead/behind here: that walks history for every branch with an upstream (see track())."""
    out = git(cwd, "for-each-ref", "--format=" + US.join(["%(refname)", "%(objectname)", "%(*objectname)",
                                                          "%(upstream:short)"]), "refs/heads", "refs/remotes", "refs/tags")
    found = {}
    for line in out.split("\n"):   # not splitlines(): it also splits on \x1c-\x1e and \x85
        parts = line.split(US)
        if len(parts) != 4:
            continue
        full, obj, peeled, up = parts
        for prefix, kind in REF_KINDS:
            if full.startswith(prefix):
                name = full[len(prefix):]
                if kind == "remote" and name.endswith("/HEAD"):
                    break
                r = {"name": name, "kind": kind, "hash": peeled or obj}
                if up:
                    r["upstream"] = up
                found[full] = r
                break
    return found


def track(cwd, full):
    """"ahead 1, behind 2" (or "") for one local branch against its upstream."""
    return git(cwd, "for-each-ref", "--format=%(upstream:track,nobracket)", "--end-of-options", full).strip()


def head(cwd):
    """(hash or None, branch name or None): None hash in a repo with no commit yet; None branch when detached."""
    h = git(cwd, "rev-parse", "--verify", "--quiet", "HEAD", ok=(0, 1)).strip() or None
    b = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1)).strip() or None
    return h, b


def commits_of(cwd, hexes):
    """{hex: full hash} for the names that are (unambiguous) commits. One `cat-file --batch-check` for all of them."""
    if not hexes:
        return {}
    out = git(cwd, "cat-file", "--batch-check=%(objectname) %(objecttype)",
              stdin="".join(x + "^{commit}\n" for x in hexes))
    found = {}
    for x, line in zip(hexes, out.splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit" and HEX_RE.fullmatch(parts[0]) and parts[0].startswith(x):
            found[x] = parts[0]
    return found


SCP_RE = re.compile(r"(?:[\w.-]+@)?([A-Za-z0-9][\w.-]*):(?!//)([^\s?#]+)")       # user@host:path (scp style)
URL_RE = re.compile(r"(https?|ssh|git)://(?:[^@/\s]*@)?([A-Za-z0-9][\w.-]*)(:\d{1,5})?/([^\s?#]+)", re.I)
HOST_RE = re.compile(r"[A-Za-z0-9][\w.-]*\.[A-Za-z]{2,}")   # a dotted name: "localhost" or a bare alias has no web page


def web_url(url):
    """The web page of a remote, or None: https://HOST/PATH for ssh, scp-style and git:// remotes, the same scheme for
    http(s) ones, without the user name or password and without ".git". Local paths and file:// give None."""
    m, port = URL_RE.fullmatch(url.strip()), ""
    if m:
        scheme, host, path = m.group(1).lower(), m.group(2), m.group(4)
        port = m.group(3) or "" if scheme.startswith("http") else ""   # an ssh port says nothing about the web one
    else:
        m = SCP_RE.fullmatch(url.strip())
        if not m or "/" in m.group(1):
            return None
        scheme, host, path = "ssh", m.group(1), m.group(2)
    path = path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    if not HOST_RE.fullmatch(host) or not path or ".." in path.split("/"):
        return None
    return f"{'http' if scheme == 'http' else 'https'}://{host}{port}/{path}"


def remote(cwd, branch):
    """(name, web page) of the remote the current branch tracks, else of origin, else of the only remote; or None."""
    try:
        names = [n for n in git(cwd, "remote").split("\n") if n]
        tracked = git(cwd, "config", "--get", f"branch.{branch}.remote", ok=(0, 1)).strip() if branch else ""
        name = next((n for n in (tracked, "origin") if n in names), None) or (names[0] if len(names) == 1 else None)
        # get-url applies url.<base>.insteadOf
        page = web_url(git(cwd, "remote", "get-url", "--", name).strip()) if name else None
    except GitError:
        return None
    return {"name": name, "url": page} if page else None


def changes(cwd):
    """Files changed in the working tree (untracked included), or None: when git status fails or is too slow, or when
    the repository's own config defines a filter. git status runs the clean filter of files whose attributes name one,
    and a filter from the repo's own config (local or worktree scope: what a cloned or unpacked .git/ can bring) would
    be run by the daemon. Switching such filters off one by one proved fragile (names with odd characters slipped
    through), so any filter there means no count: fail closed. The user's global filters, such as git-lfs, don't
    count. Submodules are not looked into (their configs would bring theirs)."""
    if OLD_GIT:
        return None
    try:
        # -z: "scope\0key\nvalue\0" per entry; only the scopes are looked at, so no name can confuse the parsing
        found = git(cwd, "config", "-z", "--show-scope", "--get-regexp", r"^filter\.", ok=(0, 1)).split("\0")
        if any(scope not in ("global", "system") for scope in found[0:-1:2]):
            return None
        out = git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all")
    except GitError:
        return None
    # -z: a rename or copy (R or C) is followed by its old path as an entry of its own
    entries, n, i = out.split("\0"), 0, 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) > 3 and e[2] == " ":
            n += 1
            if e[0] in "RC" or e[1] in "RC":
                i += 1
    return n


# With -z, commits are separated by NUL too: the output is 5 NUL-separated fields per commit, and NUL is the one byte
# no commit message, name or ref can contain.
LOG_FORMAT = "--format=%H%x00%P%x00%an%x00%at%x00%s"


def parse_log(out):
    f = out.split("\0")
    return [{"h": h, "p": p.split(), "a": a, "t": int(t) if t.isdigit() else 0, "s": s}
            for h, p, a, t, s in (f[i:i + 5] for i in range(0, len(f) - 4, 5)) if HEX_RE.fullmatch(h)]


def numstat(out):
    """`--numstat -z` output -> [{path, add, del, from?}]. Each entry is "ADD\\tDEL\\tPATH\\0", or for a rename
    "ADD\\tDEL\\t\\0OLD\\0NEW\\0". ADD and DEL are "-" for a binary file (None here)."""
    tokens, files, i = out.split("\0"), [], 0
    while i < len(tokens):
        parts = tokens[i].lstrip("\n").split("\t", 2)
        i += 1
        if len(parts) != 3:
            continue
        add, rm, path = parts
        f = {"path": path, "add": int(add) if add.isdigit() else None, "del": int(rm) if rm.isdigit() else None}
        if path == "" and i + 1 < len(tokens):
            f["from"], f["path"] = tokens[i], tokens[i + 1]
            i += 2
        files.append(f)
    return files


def setup(grovy):
    global OLD_GIT
    try:
        v = re.search(r"(\d+)\.(\d+)", git(".", "version"))
        OLD_GIT = not v or (int(v.group(1)), int(v.group(2))) < MIN_GIT
    except GitError:
        OLD_GIT = True
    if OLD_GIT:
        grovy.log(f"git older than {'.'.join(map(str, MIN_GIT))}: no changed-file count, no commit details")

    def repo(session):
        """The session's folder if it is inside a git repository, else None. Raises for an unknown session."""
        if not isinstance(session, str):
            raise LookupError
        d = grovy.session(session)
        if not d:
            raise LookupError
        cwd = (d.get("session") or {}).get("cwd")
        if not cwd:
            raise Unknown
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            return None
        # exit 128 (or "false", inside .git): not a work tree. Any other failure (a timeout, no git) is an error for
        # the page to show, not "no repository".
        return cwd if git(cwd, "rev-parse", "--is-inside-work-tree", ok=(0, 128)).strip() == "true" else None

    # The graph and a commit can take seconds each on a big repo, and every route holds one of the daemon's 8 plugin
    # route slots (shared by all plugins) until it ends, even after the page has given up on it. At most BUSY_MAX of
    # them run at once, so repeated taps can't take every slot from resolve and the user's own plugins.
    busy = threading.BoundedSemaphore(BUSY_MAX)

    def answer(fn, heavy=False):
        def run(request):
            args = request.body if request.method == "POST" else request.query
            if not isinstance(args, dict):
                return 400, {"error": "expected a JSON object"}
            if heavy and not busy.acquire(blocking=False):
                return 503, {"error": f"git: {BUSY_MAX} git views are still being read: try again in a moment"}
            try:
                try:
                    cwd = repo(args.get("session"))
                except Unknown:   # no hook has said where it works yet: unknown, not "no repository"
                    return {"repo": None}
                except LookupError:
                    return 404, {"error": "no such session"}
                if cwd is None:
                    return {"repo": False}
                return dict(fn(cwd, args), repo=True)
            except GitError as e:
                return e.status, {"error": f"git: {e}"}
            finally:
                if heavy:
                    busy.release()
        return run

    @grovy.route("POST", "resolve")
    @answer
    def resolve(cwd, args):
        names = args.get("names")
        if not isinstance(names, list):
            raise BadRequest("names must be a list")
        names = list(dict.fromkeys(n for n in names[:NAMES_MAX] if isinstance(n, str) and NAME_RE.fullmatch(n)))
        by_name = {}
        for full, r in refs(cwd).items():   # a local branch wins over a tag or a remote branch of the same name
            if r["name"] not in by_name or r["kind"] == "branch":
                by_name[r["name"]] = dict(r, ref=full)
        found = {n: {"kind": by_name[n]["kind"], "hash": by_name[n]["hash"], "ref": by_name[n]["ref"]}
                 for n in names if n in by_name}
        hexes = [n for n in names if n not in found and HEX_RE.fullmatch(n) and re.search("[a-f]", n)]
        for n, full in commits_of(cwd, hexes).items():
            found[n] = {"kind": "commit", "hash": full}
        return {"found": found}

    @grovy.route("GET", "graph")
    @lambda fn: answer(fn, heavy=True)
    def graph(cwd, args):
        started = time.monotonic()
        try:
            limit = max(1, min(LIMIT_MAX, int(args.get("limit") or 80)))
        except ValueError:
            limit = 80
        all_refs = refs(cwd)
        h, branch = head(cwd)
        ref = args.get("ref") or ""
        if ref and ref not in all_refs:   # only the repo's own ref names, never a revision expression
            raise NotFound(f"no such branch or tag: {ref[:100]}")
        if h is None and not all_refs:
            commits = []
        else:
            what = [ref] if ref else ["--branches", "--remotes", "--tags"] + (["HEAD"] if h else [])
            # --end-of-options can't come before --branches & co, so they come first and only `ref` after it
            pre = what if not ref else []
            post = ["--end-of-options", ref] if ref else []
            commits = parse_log(git(cwd, "log", "-z", "--topo-order", f"--max-count={limit}", LOG_FORMAT, *pre, *post,
                                    "--"))
        labels = {}
        for full, r in all_refs.items():
            labels.setdefault(r["hash"], []).append({"name": r["name"], "kind": r["kind"], "ref": full})
        shown = {c["h"] for c in commits}
        local = all_refs.get(f"refs/heads/{branch}") if branch else None
        return {"head": h, "branch": branch, "ref": ref or None, "limit": limit, "commits": commits,
                "refs": {k: v for k, v in labels.items() if k in shown},
                "branches": sorted({r["name"]: {"name": r["name"], "kind": r["kind"], "ref": f}
                                    for f, r in all_refs.items() if r["kind"] != "tag"}.values(),
                                   key=lambda r: (r["kind"] != "branch", r["name"])),
                "upstream": local.get("upstream") if local else None,
                "track": track(cwd, f"refs/heads/{branch}") if local and local.get("upstream") else None,
                # status is the slowest part on a big tree: skipped when the rest took long (the route has 30s)
                "changes": changes(cwd) if time.monotonic() - started < TIMEOUT else None,
                "remote": remote(cwd, branch)}

    @grovy.route("GET", "commit")
    @lambda fn: answer(fn, heavy=True)
    def commit(cwd, args):
        if OLD_GIT:
            raise GitError(f"showing a commit needs git {'.'.join(map(str, MIN_GIT))} or newer")
        x = args.get("hash") or ""
        full = commits_of(cwd, [x]).get(x) if HEX_RE.fullmatch(x) else None
        if not full:
            raise NotFound(f"no such commit: {x[:40]}")
        meta = git(cwd, "show", "--no-patch", "--format=%H%x00%P%x00%an%x00%at%x00%s%x00%b",
                   "--end-of-options", full).rstrip("\n").split("\0", 5)
        if len(meta) != 6:
            raise GitError("unexpected output from git show")
        # --submodule=short: with diff.submodule=diff, git would diff inside the submodule, with that submodule's own
        # config (its diff.external included) and without these options
        diff_opts = ["--no-ext-diff", "--no-textconv", "--submodule=short", "--diff-merges=first-parent", "-M",
                     "--format="]
        files = numstat(git(cwd, "show", "--numstat", "-z", *diff_opts, "--end-of-options", full))
        diff, cut = git(cwd, "show", "--patch", *diff_opts, "--end-of-options", full, limit=DIFF_MAX, raw=True)
        diff = diff.decode("utf-8", "replace")
        if cut:   # don't end on half a character
            diff = diff.rstrip("\ufffd")
        labels = [{"name": r["name"], "kind": r["kind"], "ref": f} for f, r in refs(cwd).items() if r["hash"] == full]
        return {"h": meta[0], "p": meta[1].split(), "a": meta[2], "t": int(meta[3]) if meta[3].isdigit() else 0,
                "s": meta[4], "body": meta[5].strip(),
                "refs": labels, "files": files[:FILES_MAX], "more_files": max(0, len(files) - FILES_MAX),
                "diff": diff, "truncated": cut}
