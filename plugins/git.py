"""grovy's built-in git plugin: read-only answers about the git repository a session works in, for extensions/git.js
(the ⋯ → Git graph, and branch names and commit hashes in Claude's replies turned into links).

Routes (behind grovy's login, called with grovy.api() from the page):
    POST /api/x/git/resolve  {session, names: [..]}  -> which names are branches, tags or commits of that session's repo
    GET  /api/x/git/graph?session=S[&ref=R][&limit=N] -> recent commits with parents and refs, HEAD, upstream, changes
    GET  /api/x/git/commit?session=S&hash=H          -> one commit: message, author, files changed, its diff (capped)

Safety: git only ever runs in the folder grovy started the session in (never a path from the request), with argument
arrays. A ref from the request is used only if it is one of the repo's own branch or tag names, and a hash only if it
is hexadecimal; both come after --end-of-options. --no-optional-locks keeps `git status` from taking the index lock
while Claude may be committing. External diff and textconv drivers are off, and outputs are capped.
A plugin of your own named git.py (~/.config/grovy/plugins/) replaces this one.
"""
import os
import re
import subprocess
import threading

GIT = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "color.ui=false"]
TIMEOUT = 10                  # seconds per git command
OUT_MAX = 4 * 1024 * 1024     # bytes read from one git command
DIFF_MAX = 200 * 1024         # characters of diff sent to the page
FILES_MAX = 300               # files listed for one commit
NAMES_MAX = 200               # names in one resolve request
LIMIT_MAX = 400               # commits in one graph
HEX_RE = re.compile(r"[0-9a-f]{7,40}")
NAME_RE = re.compile(r"[A-Za-z0-9][\w./+-]{0,99}")
REF_KINDS = (("refs/heads/", "branch"), ("refs/remotes/", "remote"), ("refs/tags/", "tag"))
US, RS = "\x1f", "\x1e"


class GitError(Exception):
    status = 502


class NotFound(GitError):
    status = 404


class BadRequest(GitError):
    status = 400


def _env():
    """The daemon's environment without GIT_* (a stray GIT_DIR would point every command at another repository)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    return env


def git(cwd, *args, stdin=None, limit=OUT_MAX, ok=(0,)):
    """Run git in cwd; its output as text (at most `limit` bytes: the rest is dropped and git stopped)."""
    try:
        p = subprocess.Popen(GIT + list(args), cwd=cwd, env=_env(), stdin=subprocess.PIPE if stdin else
                             subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise GitError(f"can't run git: {e}")
    timer = threading.Timer(TIMEOUT, p.kill)
    timer.start()
    try:
        if stdin:
            # small inputs only (a few KB of names): written before reading, which can't deadlock at this size
            p.stdin.write(stdin.encode())
            p.stdin.close()
        out = p.stdout.read(limit + 1)
        if len(out) > limit:
            p.kill()
            out = out[:limit]
        err = p.stderr.read(4096)
        code = p.wait()
    finally:
        timer.cancel()
        for f in (p.stdout, p.stderr):
            f.close()
    if code == -9 and len(out) < limit:
        raise GitError(f"git {args[0]} took longer than {TIMEOUT}s")
    if code not in ok and code != -9:
        raise GitError(err.decode("utf-8", "replace").strip()[:300] or f"git {args[0]} failed")
    return out.decode("utf-8", "replace")


def refs(cwd):
    """{full ref name: {name, kind, hash, upstream?, track?}} for branches, remote branches and tags (peeled)."""
    out = git(cwd, "for-each-ref", "--format=" + US.join(
        ["%(refname)", "%(objectname)", "%(*objectname)", "%(upstream:short)", "%(upstream:track,nobracket)"]),
        "refs/heads", "refs/remotes", "refs/tags")
    found = {}
    for line in out.splitlines():
        parts = line.split(US)
        if len(parts) != 5:
            continue
        full, obj, peeled, up, track = parts
        for prefix, kind in REF_KINDS:
            if full.startswith(prefix):
                name = full[len(prefix):]
                if kind == "remote" and name.endswith("/HEAD"):
                    break
                r = {"name": name, "kind": kind, "hash": peeled or obj}
                if up:
                    r["upstream"], r["track"] = up, track
                found[full] = r
                break
    return found


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


def changes(cwd):
    """Files changed in the working tree (untracked included), or None if git status fails or is too slow."""
    try:
        out = git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    except GitError:
        return None
    return sum(1 for e in out.split("\0") if len(e) > 3 and e[2] == " ")


LOG_FORMAT = "--format=" + US.join(["%H", "%P", "%an", "%at", "%s"]) + RS


def parse_log(out):
    rows = []
    for rec in out.split(RS):
        parts = rec.strip("\n").split(US)
        if len(parts) != 5:
            continue
        h, p, a, t, s = parts
        rows.append({"h": h, "p": p.split(), "a": a, "t": int(t) if t.isdigit() else 0, "s": s})
    return rows


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
    def repo(session):
        """The session's folder if it is inside a git repository, else None. Raises for an unknown session."""
        if not isinstance(session, str):
            raise LookupError
        d = grovy.session(session)
        if not d:
            raise LookupError
        cwd = (d.get("session") or {}).get("cwd")
        if not cwd or not os.path.isdir(cwd):
            return None
        try:
            inside = git(cwd, "rev-parse", "--is-inside-work-tree", ok=(0, 128)).strip()
        except GitError:
            return None
        return cwd if inside == "true" else None

    def answer(fn):
        def run(request):
            args = request.body if request.method == "POST" else request.query
            if not isinstance(args, dict):
                return 400, {"error": "expected a JSON object"}
            try:
                cwd = repo(args.get("session"))
            except LookupError:
                return 404, {"error": "no such session"}
            if cwd is None:
                return {"repo": False}
            try:
                return dict(fn(cwd, args), repo=True)
            except GitError as e:
                return e.status, {"error": f"git: {e}"}
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
    @answer
    def graph(cwd, args):
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
            commits = parse_log(git(cwd, "log", "--topo-order", f"--max-count={limit}", LOG_FORMAT, *pre, *post, "--"))
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
                "upstream": local.get("upstream") if local else None, "track": local.get("track") if local else None,
                "changes": changes(cwd)}

    @grovy.route("GET", "commit")
    @answer
    def commit(cwd, args):
        x = args.get("hash") or ""
        full = commits_of(cwd, [x]).get(x) if HEX_RE.fullmatch(x) else None
        if not full:
            raise NotFound(f"no such commit: {x[:40]}")
        meta = git(cwd, "show", "--no-patch", "--format=" + US.join(["%H", "%P", "%an", "%at", "%s", "%b"]),
                   "--end-of-options", full).rstrip("\n").split(US)
        if len(meta) != 6:
            raise GitError("unexpected output from git show")
        diff_opts = ["--no-ext-diff", "--no-textconv", "--diff-merges=first-parent", "-M", "--format="]
        files = numstat(git(cwd, "show", "--numstat", "-z", *diff_opts, "--end-of-options", full))
        diff = git(cwd, "show", "--patch", *diff_opts, "--end-of-options", full, limit=DIFF_MAX + 1)
        labels = [{"name": r["name"], "kind": r["kind"], "ref": f} for f, r in refs(cwd).items() if r["hash"] == full]
        return {"h": meta[0], "p": meta[1].split(), "a": meta[2], "t": int(meta[3]) if meta[3].isdigit() else 0,
                "s": meta[4], "body": meta[5].strip(),
                "refs": labels, "files": files[:FILES_MAX], "more_files": max(0, len(files) - FILES_MAX),
                "diff": diff[:DIFF_MAX], "truncated": len(diff) > DIFF_MAX}
