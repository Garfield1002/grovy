"""Issues: a session for one small fix, in a git worktree of its own, merged back and cleaned up in one tap.

⋯ → New issue (extensions/issues.js) starts one: a branch off the repo's default branch, checked out in
`<repo>/.claude/worktrees/<name>` (inside the trusted repo, so Claude starts there without its trust dialog: verified
with Claude Code 2.1.327), and a session there whose first message is what you wrote. ⋯ → Finish issue merges it:
fast-forward only, done by grovy, never by Claude. Then the session, the worktree and the branch go. If the default
branch moved on in the meantime, Claude is asked to rebase first. See docs/DESIGN.md, "Issues".

Imported by the hook (for the SessionStart note), so it stays import-cheap.
"""
import os
import re
import subprocess

from . import core, sessions, states

GIT_TIMEOUT = 60
MAX_TEXT = 4000
LIST_MAX = 20   # commits / changed files listed in a status


class GitError(RuntimeError):
    pass


def git(cwd, *args, timeout=GIT_TIMEOUT):
    """git in `cwd` (argument array, no prompt for credentials); stdout, or GitError with git's own message."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took more than {timeout}s") from None
    except OSError as e:
        raise GitError(f"can't run git: {e}") from None
    if r.returncode:
        raise GitError((r.stderr or r.stdout).strip()[-600:] or f"git {args[0]} failed")
    return r.stdout


def path():
    return os.path.join(core.state_dir(), "issues.json")


def repo_root(folder):
    """`folder` if it is the top of a git repository's main checkout (not a linked worktree), else None."""
    try:
        top = git(folder, "rev-parse", "--show-toplevel").strip()
        own, common = (git(folder, "rev-parse", "--path-format=absolute", x).strip()
                       for x in ("--git-dir", "--git-common-dir"))
    except GitError:
        return None
    return folder if os.path.realpath(top) == os.path.realpath(folder) and own == common else None


def default_branch(root):
    """The repository's default branch, as a local branch: origin's HEAD if it has one, else main or master."""
    names = []
    try:
        names.append(git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip().split("/", 1)[1])
    except (GitError, IndexError):
        pass
    for b in names + ["main", "master"]:
        try:
            git(root, "show-ref", "--verify", "--quiet", "refs/heads/" + b)
            return b
        except GitError:
            continue
    raise GitError("no default branch: this repository has no main or master branch")


def worktrees_dir(root):
    return os.path.join(root, ".claude", "worktrees")


def slug(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > 20:
            break
        out = f"{out}-{w}" if out else w
    return out or (words[0][:20] if words else "fix")


def _exclude(root):
    """Keep the worktrees out of `git status` in the main checkout (locally, never in a committed .gitignore)."""
    info = os.path.join(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip(), "info")
    line = "/.claude/worktrees/"
    f = os.path.join(info, "exclude")
    try:
        with open(f) as fh:
            if line in fh.read().split("\n"):
                return
    except FileNotFoundError:
        pass
    os.makedirs(info, exist_ok=True)
    with open(f, "a") as fh:
        fh.write("\n" + line + "\n")


def add_worktree(root, name, base):
    wt = os.path.join(worktrees_dir(root), name)
    if os.path.lexists(wt):
        raise GitError(f"{wt} already exists")
    _exclude(root)
    git(root, "worktree", "add", "-b", name, wt, base)
    return wt


def remove_worktree(issue, force=False):
    root, branch = issue["root"], issue["branch"]
    git(root, "worktree", "remove", *(["--force"] if force else []), issue["worktree"])
    if not force:
        # Merged into the base branch, not into whatever the main checkout has out (`git branch -d` checks that one,
        # so it refused whenever the checkout was on another branch).
        try:
            git(root, "merge-base", "--is-ancestor", f"refs/heads/{branch}", f"refs/heads/{issue['base']}")
        except GitError:
            raise GitError(f"{branch} isn't in {issue['base']}: kept") from None
    git(root, "branch", "-D", branch)


def status(issue):
    """Where the issue's branch stands against its base: commits on it, whether the base moved on, uncommitted
    changes in its worktree."""
    root, base, branch, wt = issue["root"], issue["base"], issue["branch"], issue["worktree"]
    rng = f"{base}..{branch}"
    log = git(root, "log", "--format=%h %s", f"--max-count={LIST_MAX}", rng, "--")
    out = {"branch": branch, "base": base, "root": root,
           "ahead": int(git(root, "rev-list", "--count", rng, "--")),
           "behind": int(git(root, "rev-list", "--count", f"{branch}..{base}", "--")),
           "commits": [line for line in log.split("\n") if line],
           "tip": git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()}
    try:
        changes = git(wt, "status", "--porcelain", "--untracked-files=normal")
        out["dirty"] = [line for line in changes.split("\n") if line][:LIST_MAX]
    except GitError as e:
        out["dirty"], out["error"] = [], str(e)
    return out


def fast_forward(issue, tip):
    """Move the base branch to the issue's branch, fast-forward only. Where the base is checked out, by a merge
    there (git refuses if your own uncommitted changes there are in the way, and changes nothing); otherwise by
    moving the ref, only from the commit it was checked against."""
    root, base, branch = issue["root"], issue["base"], issue["branch"]
    old = git(root, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}").strip()
    new = git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()
    if new != tip:   # Claude committed again after the user looked: they must see that first
        raise GitError(f"{branch} has new commits since you looked: open Finish issue again")
    try:
        git(root, "merge-base", "--is-ancestor", old, new)
    except GitError:
        raise GitError(f"{base} has moved on since this issue started: Claude must rebase first") from None
    where = None
    cur = None
    for line in git(root, "worktree", "list", "--porcelain").split("\n"):
        if line.startswith("worktree "):
            cur = line[9:]
        elif line == f"branch refs/heads/{base}":
            where = cur
    if where:
        git(where, "merge", "--ff-only", "--no-edit", new)
    else:
        git(root, "update-ref", "-m", f"grovy: finish issue {branch}", f"refs/heads/{base}", new, old)
    return old, new


def context_for(name):
    """What Claude is told at SessionStart in an issue's session (the hook calls this; never raises)."""
    try:
        issue = (core.read_json(path(), {}) or {}).get(name)
        if not isinstance(issue, dict):
            return ""
        return (f"grovy: this session is an issue: one small change, in its own git worktree on branch "
                f"`{issue['branch']}`, made from `{issue['base']}`. Make the change here, run the project's tests, "
                f"and commit it on this branch (leave nothing uncommitted). Don't merge, push or switch branches: "
                f"the user reviews the commits and merges them from grovy (fast-forward only). If asked to rebase, "
                f"rebase onto `{issue['base']}`.")
    except Exception:   # the hook must never fail on this
        return ""


class Issues:
    """Start, inspect, finish and discard issues: part of Store. Git runs outside self.lock (it can take seconds);
    self.issue_lock keeps two issue actions from interleaving."""

    def _save_issues(self):   # under self.lock
        core.atomic_write_json(self.issues_path, self.issues)

    def _issue(self, name):
        with self.lock:
            name = self._canon(name)
            issue = self.issues.get(name)
            return name, (dict(issue) if issue else None)

    def issue_projects(self):
        """Trusted folders that are git repositories, each with the branch an issue would start from."""
        out = []
        for p in self.lifecycle.trusted_projects():
            if repo_root(p):
                try:
                    out.append({"path": p, "base": default_branch(p)})
                except GitError:
                    pass
        return 200, out

    def create_issue(self, folder, text, ask=False):
        if not isinstance(text, str) or not text.strip():
            return 400, "say what the issue is"
        if text.lstrip()[:1] in ("/", "!"):
            return 400, "start with words: a leading / or ! would be taken as a command"
        if len(text) > MAX_TEXT:
            return 413, f"keep it under {MAX_TEXT} characters: an issue is one small change"
        if not isinstance(folder, str) or folder not in self.lifecycle.trusted_projects() or not repo_root(folder):
            return 400, "pick one of the listed projects"
        with self.issue_lock:
            try:
                base = default_branch(folder)
                reg = sessions.registry()
                branches = set(git(folder, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())
                stem = "issue-" + slug(text)
                name = next(n for n in [stem] + [f"{stem}-{i}" for i in range(2, 100)]
                            if not sessions.name_in_use(reg, n) and n not in self.sessions and n not in branches
                            and not os.path.lexists(os.path.join(worktrees_dir(folder), n)))
                wt = add_worktree(folder, name, base)
            except StopIteration:
                return 409, "too many issues with this name: finish some first"
            except GitError as e:
                return 409, str(e)
            issue = {"root": folder, "base": base, "branch": name, "worktree": wt, "created": core.now()}
            with self.lock:
                self.issues[name] = issue
                self._save_issues()   # before the session starts: its SessionStart hook reads this
            try:
                self.lifecycle.start(name, wt, ["--permission-mode", "default"] if ask is True else [])
            except (ValueError, RuntimeError) as e:
                with self.lock:
                    self.issues.pop(name, None)
                    self._save_issues()
                try:
                    remove_worktree(issue, force=True)
                except GitError:
                    pass
                return 409, str(e)
        self.reconcile()
        code, p = self.enqueue(name, text.strip())   # typed once Claude is ready (a hook-confirmed idle state)
        return 200, {"name": name, "queued": code == 200}

    def issue_status(self, name):
        name, issue = self._issue(name)
        if not issue:
            return 404, "not an issue"
        try:
            st = status(issue)
        except GitError as e:
            return 409, str(e)
        with self.lock:
            s = self.sessions.get(name) or {}
            st["state"] = s.get("state")
        return 200, st

    def _busy(self, name):
        with self.lock:
            s = self.sessions.get(name)
            if not s or s["state"] == "exited":
                return None
            if s["state"] not in states.INPUT_STATES or self._pending(name):
                return "Claude is still working on it: wait until it's done"
        return None

    def _close(self, name, issue, force):
        """After the merge (or to discard): stop the session, remove its worktree and branch, forget it."""
        notes = []
        if name in self.tmux_live():
            try:
                self.lifecycle.kill(name)
            except (ValueError, RuntimeError) as e:
                notes.append(f"couldn't stop the session: {e}")
        self.reconcile()
        try:
            remove_worktree(issue, force=force)
        except GitError as e:
            notes.append(f"the worktree or branch is still there ({e})")
        with self.lock:
            self.issues.pop(name, None)
            self._save_issues()
        if name not in self.tmux_live():
            self.forget(name)
        return notes

    def finish_issue(self, name, tip=None):
        """Fast-forward the base branch to the issue's commits, then close the issue. Refused, changing nothing,
        while Claude works, with uncommitted changes, with no commits, or when the base moved on."""
        with self.issue_lock:
            name, issue = self._issue(name)
            if not issue:
                return 404, "not an issue"
            if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", tip):
                return 400, "tip: the commit you reviewed (from the issue's status)"
            why = self._busy(name)
            if why:
                return 409, why
            try:
                st = status(issue)
                if st.get("error"):
                    return 409, st["error"]
                if st["dirty"]:
                    return 409, "there are uncommitted changes in the issue's worktree: ask Claude to commit them"
                if not st["ahead"]:
                    return 409, "nothing to merge: there are no commits on the issue's branch"
                old, new = fast_forward(issue, tip)
            except GitError as e:
                return 409, str(e)
            notes = self._close(name, issue, force=False)
        msg = f"{issue['base']} moved to {new[:7]} ({st['ahead']} commit{'s' if st['ahead'] > 1 else ''})"
        return 200, {"message": msg + ("; " + "; ".join(notes) if notes else ""), "base": issue["base"],
                     "from": old, "to": new}

    def discard_issue(self, name):
        """Drop the issue: its session, worktree and branch, commits and uncommitted changes included."""
        with self.issue_lock:
            name, issue = self._issue(name)
            if not issue:
                return 404, "not an issue"
            notes = self._close(name, issue, force=True)
        return 200, {"message": "discarded" + ("; " + "; ".join(notes) if notes else "")}
