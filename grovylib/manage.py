"""Start, resume, rename, restart, stop and forget sessions; reconcile with tmux."""
import sys

from . import claude, core, newproject, projects, sessions, states


CONFIRM_TIMEOUT = 30     # seconds for UserPromptSubmit to confirm an injected prompt


class Lifecycle:
    """tmux/claude side effects for phone-initiated lifecycle actions (swappable in tests)."""

    @staticmethod
    def trusted_projects():
        return projects.trusted_projects()

    @staticmethod
    def start(name, path, extra):
        sessions.start_session(name, path, extra)
        sessions.check_started(name, path, extra)

    @staticmethod
    def accept_trust(name, path):
        newproject.accept_trust(name, path)

    @staticmethod
    def resume(name):
        return sessions.resume_session(name)

    @staticmethod
    def conversations(path):
        return projects.conversations(path)

    @staticmethod
    def live_conversations():
        return projects.live_conversations()

    @staticmethod
    def kill(name):
        sessions.kill_session(name)

    @staticmethod
    def rename(old, new):
        sessions.rename_session(old, new)


class SessionLifecycle:
    """Start, resume, rename, restart, stop and forget sessions; reconcile with tmux: part of Store."""

    # ------------------------------------------------------------ lifecycle (phone)
    def conversations(self, path):
        """Past conversations in a trusted folder, each marked with where it's currently open, if anywhere."""
        if not isinstance(path, str) or path not in self.lifecycle.trusted_projects():
            return 400, "pick one of the listed folders"
        convs = self.lifecycle.conversations(path)
        live_ccs = self._live_ccs_conversations()
        live_any = self.lifecycle.live_conversations()
        for c in convs:
            c["open_in"] = ("grovy:" + live_ccs[c["id"]]) if c["id"] in live_ccs else \
                ("terminal" if c["id"] in live_any else None)
        return 200, convs

    def _live_ccs_conversations(self):
        live = self.tmux_live()
        with self.lock:
            return {s["claude_session_id"]: n for n, s in self.sessions.items()
                    if n in live and s.get("claude_session_id") and s.get("state") != "exited"}

    def create(self, name, path, ask, resume=None, fork=False):
        """New session from the phone: only in a folder Claude already trusts (no blocking trust dialog).
        With resume=<conversation id>, continue (or, with fork, copy) a past conversation from that folder."""
        if not core.valid_name(name):
            return 400, "name: letters, digits, - and _ (max 32)"
        if not isinstance(path, str) or path not in self.lifecycle.trusted_projects():
            return 400, "pick one of the listed folders"
        if sessions.name_in_use(sessions.registry(), name):
            return 409, f"the name {name} is taken (by a session or a renamed session's old name)"
        extra = ["--permission-mode", "default"] if ask is True else []
        if resume is not None:
            if not isinstance(resume, str) or resume not in {c["id"] for c in self.lifecycle.conversations(path)}:
                return 400, "unknown conversation for this folder"
            live_ccs = self._live_ccs_conversations()
            if resume in live_ccs:
                return 409, f"already open in session {live_ccs[resume]}"
            if resume in self.lifecycle.live_conversations() and fork is not True:
                return 409, "this conversation is open in another terminal: fork a copy instead"
            extra += ["--resume", resume] + (["--fork-session"] if fork is True else [])
        try:
            self.lifecycle.start(name, path, extra)
        except (ValueError, RuntimeError) as e:
            return 409, str(e)
        self.reconcile()
        return 200, "started"

    def resume(self, name):
        try:
            msg = self.lifecycle.resume(name)
        except (ValueError, RuntimeError) as e:
            return 409, str(e)
        self.reconcile()
        return 200, msg

    def rename(self, old, new):
        """Rename a session (live or ended) and move everything the daemon holds for it."""
        if not isinstance(new, str) or not core.valid_name(new):
            return 400, "name: letters, digits, - and _ (max 32)"
        with self.lock:  # held across the tmux rename so no reconcile sees a half-renamed session
            old = self._canon(old)
            if old not in self.sessions:
                return 404, "unknown session"
            if new in self.sessions and new != old:
                return 409, f"the name {new} is already taken"
            try:
                self.lifecycle.rename(old, new)
            except (ValueError, RuntimeError) as e:
                return 409, str(e)
            if new != old:
                s = self.sessions.pop(old)
                s["name"] = new
                self.sessions[new] = s
                if old in self.events:
                    q = self.events.pop(old)
                    for r in q:
                        r["session"] = new
                    self.events[new] = q
                if old in self.live:
                    self.live[new] = self.live.pop(old)
                for p in self.prompts + list(self.perms.values()) + list(self.pages.values()):
                    if p["session"] == old:
                        p["session"] = new
                self._save_pages()
                if old in self.issues:
                    self.issues[new] = self.issues.pop(old)
                    self._save_issues()
                self.aliases = {a: (new if n == old else n) for a, n in self.aliases.items()}
                self.aliases[old] = new
                self.aliases.pop(new, None)
                self._record(new, "Renamed", {"from": old, "to": new})
                self._save_state()
                self._save_prompts()
        self.publish(old)
        self.publish(new)
        return 200, new

    def restart(self, name, force=False):
        """"Update": stop + resume in one step, so the session picks up new grovy settings (hooks, status line).
        Without force, only when nothing can be cut off: Claude idle, no background tasks, no pending request,
        no prompt in flight. force=True is the user's explicit, one-time "update anyway": what was running is
        cut off (the conversation itself is resumed)."""
        with self.lock:
            s = self.sessions.get(name)
            if not s:
                return 404, "unknown session"
            if s["state"] == "exited":
                return 409, "the session has ended: use Resume"
            busy = [why for why, cond in (
                (f"Claude is {states.LABELS.get(s['state'], s['state'])}", s["state"] not in ("idle", "error")),
                (f"{s.get('background')} background task(s) are running", s.get("background")),
                ("a request is waiting for your answer", self._pending(name)),
                ("a message is being sent", any(p["session"] == name and p["status"] in ("queued", "injected")
                                                for p in self.prompts))) if cond]
            if busy and not force:
                return 409, "not now: " + "; ".join(busy)
            if busy:
                self._record(name, "Updated", {"forced": True, "cut_off": busy})
        try:
            self.lifecycle.kill(name)
        except (ValueError, RuntimeError) as e:
            return 409, str(e)
        self.reconcile()
        code, msg = self.resume(name)
        return (200, "updated: " + msg) if code == 200 else (code, "stopped, but resume failed: " + msg)

    def forget(self, name):
        """Remove an ended session from the list (its conversation stays resumable from ＋)."""
        if name in self.tmux_live():
            return 409, f"{name} is still running; stop it first"
        if name in self.issues:   # its worktree and branch would be left with no way to finish or discard them
            return 409, f"{name} is an issue: open it and use ⋯ → Finish issue (or Discard) instead"
        with sessions.registry_locked() as reg:
            if reg.pop(name, None) is None and name not in self.sessions:
                return 404, "unknown session"
        with self.lock:
            self.sessions.pop(name, None)
            self.live.pop(name, None)
            self._save_state()
            for pid in [k for k, p in self.pages.items() if p["session"] == name]:
                self._drop_page(pid)
        self.publish(name)
        return 200, "removed"

    def kill(self, name):
        try:
            self.lifecycle.kill(name)
        except (ValueError, RuntimeError) as e:
            return 409, str(e)
        self.reconcile()
        return 200, "stopped"

    # ------------------------------------------------------------ reconciliation
    def reconcile(self):
        changed, gone = set(), []
        try:   # outside the lock: reads the user's settings files
            expected = {n: claude.session_fingerprint(n) for n in sessions.registry()}
        except (OSError, ValueError) as e:
            print(f"grovyd: can't tell whether sessions are up to date: {e}", file=sys.stderr)
            expected = {}
        with self.lock:
            # Snapshot under the state lock: a snapshot taken outside it could be applied after a
            # newer hook event and mark a just-started session "exited".
            try:
                live = self.tmux_live()
            except RuntimeError as e:  # tmux unresponsive: skip this round rather than guess "exited"
                print(f"grovyd: reconcile skipped: {e}", file=sys.stderr)
                return
            reg = sessions.registry()
            self.aliases = {a: n for n, v in reg.items() for a in (v.get("aliases") or [])}
            for name in set(reg) | set(live) | set(self.sessions):
                s = self._session(name)
                if name in reg:
                    s["cwd"] = s.get("cwd") or reg[name].get("cwd")
                    s["claude_session_id"] = s.get("claude_session_id") or reg[name].get("claude_session_id")
                    if name in expected:
                        outdated = name in live and reg[name].get("config") != expected[name]
                        if s.get("outdated") != outdated:
                            s["outdated"] = outdated
                            changed.add(name)
                if name not in live and s["state"] != "exited":
                    if s.get("scopes"):
                        gone.append((name, s["scopes"]))
                    self._expire_perms(name, "tmux session gone")
                    self._set(name, "exited", "tmux session gone")
                    self._record(name, "Exited", {"note": "tmux session no longer exists"})
                    self._fail_queued(name, "session exited before it was sent")
                    changed.add(name)
                elif name in live and s["state"] == "exited":
                    self._set(name, "starting", "process started")
                    changed.add(name)
                if name not in reg and name not in live:
                    del self.sessions[name]  # forgotten
                    changed.add(name)
            for pid in [k for k, p in self.perms.items()
                        if p["status"] not in ("pending", "deciding") and core.now() - p["created"] > 3600]:
                del self.perms[pid]
            for p in self.prompts:
                limit = states.COMMAND_TIMEOUT if p.get("command") in ("clear", "compact") else CONFIRM_TIMEOUT
                if p["status"] == "injected" and core.now() - p["updated"] > limit:
                    p["status"], p["updated"] = "unconfirmed", core.now()
                    p["error"] = "no UserPromptSubmit seen; check the terminal"
                    changed.add(p["session"])
                    s = self.sessions.get(p["session"])
                    if s and s["state"] == "working" and s["activity"] in states.DELIVERY_ACTIVITY.values():
                        self._set(p["session"], "unknown", "prompt not confirmed")
                elif p["status"] == "queued" and p["session"] not in live:  # incl. forgotten sessions: never
                    # deliver into a future session that merely reuses the name
                    p["status"], p["error"], p["updated"] = "failed", "session not running", core.now()
                    changed.add(p["session"])
            if changed:
                self._save_state()
                self._save_prompts()
        for name in changed:
            self.publish(name)
        if self.oom:
            self._oom_check(gone)
        for name in list(self.sessions):
            self.try_deliver(name)

    def _oom_check(self, gone):
        """Outside the lock (systemd and journal calls): say when the OOM killer took a session, and keep new
        sessions' Claude out of its way."""
        for name, scopes in gone:
            if self.oom.killed(scopes):
                with self.lock:
                    self._record(name, "Killed", {"by": "oom"})
                    self._set(name, "exited", "killed by the out-of-memory killer")
                    self._save_state()
                self._notify(name, "error", "killed by the out-of-memory killer (memory pressure on this machine)")
                self.publish(name)
        try:
            pids = self.oom.pids()
        except Exception as e:  # never let this break reconciliation
            print(f"grovyd: oom guard: {e}", file=sys.stderr)
            return
        with self.lock:
            todo = [(n, pid) for n, pid in pids.items() if n in self.sessions and self.sessions[n].get("oom_pid") != pid]
        for name, pid in todo:
            scope = self.oom.scope(pid)
            scopes = [scope] if scope else []
            if scope and not scope.startswith("grovy-"):
                new = self.oom.protect(name, pid)
                if new:
                    scopes.append(new)
                else:
                    print(f"grovyd: couldn't shield {name} from the OOM killer", file=sys.stderr)
            with self.lock:
                s = self.sessions.get(name)
                if s:
                    s["oom_pid"], s["scopes"] = pid, scopes
                    self._save_state()
