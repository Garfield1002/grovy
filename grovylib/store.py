"""The daemon's state: sessions, events, history. Store is assembled from the feature classes below."""
import hashlib
import json
import os
import threading
from collections import deque

from . import core, manage, perms, sessions, states, tasks, tmuxctl
from .tasks import Tasks
from .perms import Permissions
from .prompts import Prompts
from .manage import SessionLifecycle
from .pages import Pages
from .live import LiveCards
from .uploads import Attachments
from .issues import Issues
from .newproject import NewProjects
from .steps import Steps


HISTORY = 600            # events kept in memory per session (a turn's steps count too)

TURN_TOOLS = 100   # tool calls listed per turn ("Worked for …"); beyond that only counted

DUP_WINDOW = 1.0  # seconds

# Fields of each hook payload worth keeping. Tool outputs are deliberately not stored.
KEEP = {
    "SessionStart": ("source", "model"),
    "SessionEnd": ("reason",),
    "UserPromptSubmit": ("prompt",),
    "PermissionRequest": ("tool_name", "tool_input"),
    "Notification": ("message", "title", "notification_type"),
    "Stop": ("last_assistant_message",),
    "StopFailure": ("error", "error_details", "last_assistant_message"),
}


def session_order(s):
    """How the session list is sorted: waiting on you first, then by state, then the most recently active."""
    return (states.STATE_RANK.get(s["state"], 9) if not s["pending_permissions"] else -1, -(s["last_event"] or 0))


def _last(events, limit):
    """The last `limit` events, not counting steps: a long turn's steps don't push the conversation out of view."""
    out, n = [], 0
    for r in reversed(events):
        if n >= limit:
            break
        out.append(r)
        n += r.get("type") != "Step"
    return out[::-1]


def _owner(r):
    """Session name of a stored record; records written before the rename used the key "ccs"."""
    return r.get("session", r.get("ccs"))


class Store(Tasks, Permissions, Prompts, SessionLifecycle, Pages, LiveCards, Attachments, Issues, NewProjects, Steps):
    def __init__(self, sdir, tmux_live=tmuxctl.tmux_managed, paste=tmuxctl.paste_prompt, notifier=None,
                 key=tmuxctl.send_key, lifecycle=None, oom=None):
        self.dir = sdir
        self.lock = threading.RLock()
        self.tmux_live = tmux_live
        self.paste = paste
        self.key = key
        self.lifecycle = lifecycle or manage.Lifecycle()
        self.notifier = notifier
        self.oom = oom          # OomGuard or None (tests, systems without systemd)
        self.sessions = {}      # name -> runtime dict
        self.events = {}        # name -> deque of event records
        self.perms = {}         # id -> permission dict (includes threading.Event under "_ev")
        self.prompts = []       # list of prompt dicts, persisted
        self.seen = {}  # payload digest -> time; only a re-delivery within DUP_WINDOW is a duplicate
        self.subscribers = set()
        self.seq = 0
        self.log_path = os.path.join(sdir, "events.jsonl")
        self.state_path = os.path.join(sdir, "state.json")
        self.prompts_path = os.path.join(sdir, "prompts.json")
        self.pages_dir = os.path.join(sdir, "pages")
        self.pages_path = os.path.join(sdir, "pages.json")
        self.pages = {}         # id -> {id, session, name, title, version, updated, size}
        self.live = {}          # name -> {key -> {"rec": event record, "logged": time, "dirty": bool}}
        self.live_new = {}      # name -> deque of creation times (rate limit)
        self._pub_at, self._pub_timer = {}, {}
        self.plugins = None     # plugins.Plugins once grovyd has loaded the user's plugins
        self.peers = None       # peers.Peers in grovyd: other machines' sessions (empty without peers.json)
        self.issues_path = os.path.join(sdir, "issues.json")
        self.issues = {}        # session name -> {root, base, branch, worktree, created} (issues.py)
        self.issue_lock = threading.Lock()
        self.fresh = {}         # project folder new_project made -> kind, until its first session (newproject.py)
        self.project_lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        self.aliases = self._read_aliases()
        pages = core.read_json(self.pages_path, {})
        self.pages = {k: v for k, v in pages.items() if core.ID_RE.match(k) and isinstance(v, dict)} \
            if isinstance(pages, dict) else {}
        issues = core.read_json(self.issues_path, {})
        self.issues = {k: v for k, v in issues.items() if core.valid_name(k) and isinstance(v, dict)} \
            if isinstance(issues, dict) else {}
        st = core.read_json(self.state_path, {})
        for name, s in (st.get("sessions") or {}).items():
            if core.valid_name(name) and isinstance(s, dict):
                self.sessions[name] = s
                if s.get("state") == "needs_input":
                    # The waiting hook saw EOF and exited without output, so Claude is now showing its
                    # terminal dialog: still needs_input (and therefore never typed into).
                    s["activity"] = "waiting in terminal (daemon restarted during a dialog)"
        self.prompts = [p for p in core.read_json(self.prompts_path, []) if isinstance(p, dict) and "id" in p]
        for p in self.prompts:
            if "session" not in p and "ccs" in p:  # pre-rename record
                p["session"] = p.pop("ccs")
        for p in self.prompts:
            p["force"] = False  # a confirmation never outlives the moment it was given
            if p.get("status") == "injected":  # crashed between typing and confirmation: never re-send
                p["status"], p["error"] = "unconfirmed", "daemon restarted before confirmation"
        try:
            with open(self.log_path, "rb") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if "session" not in rec and "ccs" in rec:  # pre-rename record
                            rec["session"] = rec.pop("ccs")
                        rec["session"] = self._canon(rec["session"])  # recorded under a name since renamed
                        self.seq = max(self.seq, int(rec["id"]))
                        q = self.events.setdefault(rec["session"], deque(maxlen=HISTORY))
                        if rec.get("type") == "Live":   # a live widget is re-logged under its id on each update
                            key = (rec.get("data") or {}).get("key")
                            old = self.live.get(rec["session"], {}).get(key)
                            if old and old["rec"]["id"] == rec["id"]:
                                at = next((i for i, r in enumerate(q) if r is old["rec"]), None)
                                if at is not None:
                                    q[at] = old["rec"] = rec
                                continue   # (an update of a card that has scrolled out of memory is dropped)
                            self.live.setdefault(rec["session"], {})[key] = {"rec": rec, "logged": 0, "dirty": False}
                        q.append(rec)
                    except (ValueError, KeyError, TypeError):
                        continue  # truncated / corrupt record: skip it
            with open(self.log_path, "rb+") as f:  # make sure a torn last line can't swallow the next record
                f.seek(0, 2)
                if f.tell():
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
        except FileNotFoundError:
            pass
        try:  # rotate only after reading, so ids keep counting up and recent history stays in memory
            if os.path.getsize(self.log_path) > 50 * 1024 * 1024:
                os.replace(self.log_path, self.log_path + ".1")
        except OSError:
            pass
        # Prompts left "unconfirmed" by an older matcher: confirm them if Claude did receive them.
        seen = {core.squash(r["data"].get("prompt", "")) for q in self.events.values() for r in q
                if r.get("type") == "UserPromptSubmit" and isinstance(r.get("data"), dict)}
        for p in self.prompts:
            if p.get("status") == "unconfirmed" and core.squash(p.get("text", "")) in seen:
                p["status"] = "submitted"
        self._log = open(self.log_path, "a", buffering=1)
        os.chmod(self.log_path, 0o600)

    def _save_state(self):
        core.atomic_write_json(self.state_path, {"sessions": self.sessions})

    def _save_pages(self):
        core.atomic_write_json(self.pages_path, self.pages)

    def _save_prompts(self):
        keep, count = [], {}
        for p in reversed(self.prompts):  # keep all pending + the last 50 per session
            count[p["session"]] = count.get(p["session"], 0) + 1
            if p["status"] in ("queued", "injected") or count[p["session"]] <= 50:
                keep.append(p)
        self.prompts = keep[::-1]
        core.atomic_write_json(self.prompts_path, self.prompts)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _read_aliases():
        return {a: n for n, v in sessions.registry().items() for a in (v.get("aliases") or [])}

    def _canon(self, name):
        """A renamed session's running Claude still reports its old name until it restarts."""
        return self.aliases.get(name, name)

    def _session(self, name):
        s = self.sessions.get(name)
        if s is None:
            s = self.sessions[name] = {"name": name, "state": "starting", "cwd": None,
                                       "claude_session_id": None, "last_event": None, "activity": "",
                                       "last_error": None, "background": 0}
        return s

    def _record(self, name, etype, data, sid=None):
        self.seq += 1
        rec = {"id": self.seq, "ts": core.now(), "session": name, "claude_session_id": sid, "type": etype, "data": data}
        self._log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.events.setdefault(name, deque(maxlen=HISTORY)).append(rec)
        if self.plugins:
            self.plugins.emit("event", name, rec)
        return rec

    def _set(self, name, state, activity=None):
        s = self._session(name)
        old, s["state"] = s.get("state"), state
        if self.plugins and old != state:
            self.plugins.emit("state", name, old, state)
        if activity is not None:
            s["activity"] = activity[:200]
        s["last_event"] = core.now()

    def publish(self, name=None):
        msg = json.dumps({"session": name, "ts": core.now()})
        for q in list(self.subscribers):
            q.append(msg)
            q.wake.set()

    def _notify(self, name, kind, text):
        # A hub following this daemon (grovylib/peers.py) asked for these on its event stream (?notify=1).
        msg = None
        for q in list(self.subscribers):
            if getattr(q, "notify", False):
                msg = msg or json.dumps({"notify": {"session": name, "kind": kind, "text": str(text)[:300]}})
                q.append(msg)
                q.wake.set()
        if self.notifier:
            self.notifier.send(name, kind, text)
        if self.plugins:
            self.plugins.emit("notify", name, kind, text)

    # ------------------------------------------------------------ hook ingestion
    def ingest(self, name, payload):
        """Apply one non-blocking hook event. Returns False if it was a duplicate."""
        name = self._canon(name)
        ev = payload.get("hook_event_name")
        sid = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
        digest = hashlib.sha256(json.dumps([name, payload], sort_keys=True).encode()).hexdigest()
        if ev in ("PostToolUse", "Stop", "StopFailure") and not payload.get("agent_id"):
            self.read_steps(name)   # what Claude wrote up to here comes before this event
        with self.lock:
            t = core.now()
            # Hook payloads carry no unique id, so identical content can be a genuine repeat
            # ("continue" typed twice, the same idle notification). Only a near-instant re-delivery
            # of the same payload is treated as a duplicate.
            if t - self.seen.get(digest, -1e9) < DUP_WINDOW:
                return False
            self.seen[digest] = t
            if len(self.seen) > 1000:
                self.seen = {k: v for k, v in self.seen.items() if t - v < DUP_WINDOW}
            s = self._session(name)
            if ev in tasks.TASK_EVENTS or (ev == "PostToolUse" and payload.get("agent_id")):
                # Fired inside a subagent, or about one: it updates the list of background work and nothing else.
                # A subagent's tool calls aren't the session's state (they can even arrive after the parent's Stop).
                self._track_tasks(s, ev, payload, t)
                if ev != "PostToolUse":   # a subagent's every tool call isn't worth a state write
                    self._save_state()
            else:
                self._apply(name, s, ev, payload, sid, t)
                self._save_state()
        self.publish(name)
        self.try_deliver(name)
        return True

    def _apply(self, name, s, ev, payload, sid, t):
        """One hook event of the session itself (not of a subagent), under the lock."""
        if payload.get("cwd"):
            s["cwd"] = str(payload["cwd"])
        self._steps_follow(s, payload)
        if ev != "PostToolUse":
            data = {k: core.clip(payload[k]) for k in KEEP.get(ev, ()) if k in payload}
            if ev in ("Stop", "StopFailure"):
                self._steps_end(s, data)   # (read up to here just before, in ingest)
            if ev == "Stop":
                data["background_tasks"] = tasks._count(payload.get("background_tasks"))
                names = [x["description"] or x["type"] for x in self._task_snapshot(s, payload.get("background_tasks"), t)]
                if names:
                    data["background_names"] = names[:10]
                turn = s.pop("turn", None)
                if turn:   # "Worked for …": how long the turn took and what it ran
                    data["worked"] = round(t - turn["start"], 1)
                    data["tools"], data["tool_count"] = turn["tools"], turn["count"]
            if ev == "UserPromptSubmit":
                p = self._confirm_prompt(name, payload.get("prompt", ""))
                if p:
                    data["sent"] = p["created"]  # when the user sent it; the event's ts is when Claude read it
            self._record(name, ev, data, sid)
        if ev == "SessionStart":
            s["claude_session_id"] = sid
            s["last_error"] = None
            src = payload.get("source")
            if src in ("startup", "clear"):
                s["goal"] = None   # a fresh conversation, or /clear (even typed in the terminal), has no goal
            ours = False
            for p in self.prompts:  # /clear and /compact emit no UserPromptSubmit; this is their receipt
                if p["session"] == name and p["status"] == "injected" and p.get("command") == src \
                        and core.now() - p["updated"] < states.COMMAND_TIMEOUT:  # a stale one must not turn auto-compaction into "idle"
                    p["status"], p["updated"], ours = "submitted", core.now(), True
                    self._save_prompts()
            if src in ("startup", "resume"):
                self._task_snapshot(s, [], t)   # a new Claude process: whatever the old one ran is gone
                self._steps_end(s)
            if src == "compact" and not ours and s["state"] == "working":
                s["last_event"] = core.now()  # automatic compaction mid-turn: Claude is still working
            else:
                self._set(name, "idle", f"session {src or 'start'}")
        elif ev == "UserPromptSubmit":
            text = core.norm(payload.get("prompt", ""))
            m = tasks.MACHINE_PROMPT.match(text)  # injected by Claude Code itself, not typed by the user
            self._steps_begin(s, payload)   # (before the state changes: it tells a new turn from one going on)
            self._set(name, "working", ("subagent report" if m.group(1) == "agent-message" else "task notification")
                      if m else "prompt: " + text[:120])
            done = tasks.MACHINE_TASK.match(text)
            if done:   # that task has finished (Claude Code's next list will agree)
                self._drop_task(s, done.group(1) or done.group(2))
            s["turn"] = {"start": t, "tools": [], "count": 0}
        elif ev == "PostToolUse":
            self._task_launched(s, payload, t)
            turn = s.get("turn")
            if turn is not None:
                turn["count"] += 1
                if len(turn["tools"]) < TURN_TOOLS:
                    turn["tools"].append(core.tool_summary(payload.get("tool_name"), payload.get("tool_input")))
            if s["state"] != "exited" and not self._pending(name):
                self._set(name, "working", f"ran {payload.get('tool_name', 'tool')}")
        elif ev == "Stop":
            msg = core.norm(payload.get("last_assistant_message") or "")
            # With background tasks Claude still accepts input, so "idle" stays truthful; the UI shows the tasks.
            self._set(name, "idle", msg[:200] or "turn finished")
            bg = f" ({s['background']} still running)" if s.get("background") else ""
            self._notify(name, "done", "finished: " + (msg[:80] or "turn complete") + bg)
        elif ev == "StopFailure":
            err = str(payload.get("error") or "error")
            s["last_error"] = err
            self._set(name, "error", f"failed: {err}")
            self._notify(name, "error", f"error: {err}")
        elif ev == "Notification":
            nt = payload.get("notification_type")
            if nt == "permission_prompt" and not self._pending(name):
                # A terminal dialog we have no hook for (or a hook that gave up): only the terminal can answer.
                self._set(name, "needs_input", "waiting in terminal: " + str(payload.get("message", ""))[:120])
                self._notify(name, "input", "needs attention in terminal")
            elif nt in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
                self._set(name, "needs_input", str(payload.get("message", ""))[:160])
                self._notify(name, "input", "needs input in terminal")
            elif nt == "idle_prompt" and s["state"] not in ("needs_input",):
                self._set(name, "idle")
            else:
                s["last_event"] = core.now()
        elif ev == "SessionEnd":
            if payload.get("reason") in ("clear", "resume"):
                s["last_event"] = core.now()  # same process continues with a new conversation (SessionStart)
            else:
                self._expire_perms(name, "Claude session ended")
                self._set(name, "exited", f"conversation ended ({payload.get('reason', '')})")
                s["goal"] = None
                self._task_snapshot(s, [], t)
                self._steps_end(s)
                self._fail_queued(name, "Claude exited before it was sent")

    def status(self, name, d):
        """Status-line data (model, effort, context). Claude refreshes it often: keep it in memory and
        only notify browsers when something visible changed."""
        def num(v):
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        cw = d.get("context_window") if isinstance(d.get("context_window"), dict) else {}
        used = None
        if num(cw.get("remaining_percentage")) is not None:  # accounts for the compaction reserve
            used = 100 - int(cw["remaining_percentage"])
        elif num(cw.get("used_percentage")) is not None:
            used = int(cw["used_percentage"])
        model = d.get("model") if isinstance(d.get("model"), dict) else {}
        effort = d.get("effort") if isinstance(d.get("effort"), dict) else {}
        meta = {"model": str(model.get("display_name") or model.get("id") or "")[:40] or None,
                "effort": str(effort.get("level") or "")[:20] or None,
                "fast": d.get("fast_mode") is True,
                "context": max(0, min(100, used)) if used is not None else None}
        with self.lock:
            name = self._canon(name)
            s = self.sessions.get(name)
            if not s or s.get("meta") == meta:
                return
            s["meta"] = meta
            self._save_state()
        self.publish(name)

    # ------------------------------------------------------------ history / search (from the log on disk)
    def history(self, name, before=None, q=None, limit=100):
        """Older events (id < before), or prompts/replies containing q (newest first)."""
        q = q.lower() if q else None
        out = []
        try:
            with open(self.log_path, "rb") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or self._canon(_owner(r)) != name:
                        continue
                    if q:
                        d = r.get("data") or {}
                        text = d.get("prompt") or d.get("last_assistant_message") or ""
                        if r.get("type") in ("UserPromptSubmit", "Stop", "Steered") and q in str(text).lower():
                            out.append(r)
                    elif before is not None and r.get("id", 0) < before:
                        out.append(r)
        except OSError:
            return []
        latest = {r.get("id"): r for r in out}   # a live widget appears once per logged update: keep the last
        out = [r for r in out if latest.get(r.get("id")) is r]
        return out[::-1][:limit] if q else _last(out, limit)

    # ------------------------------------------------------------ views
    def summary(self, name):
        s = dict(self.sessions[name])
        s.pop("tx", None)   # where the transcript is read: the daemon's business
        mine = [p for p in self.prompts if p["session"] == name]
        s["pending_permissions"] = len(self._pending(name))
        s["queued_prompts"] = sum(p["status"] in ("queued", "injected") for p in mine)
        s["accepts_input"] = s["state"] in states.INPUT_STATES and not s["pending_permissions"]
        issue = self.issues.get(name)
        s["issue"] = {"branch": issue["branch"], "base": issue["base"], "root": issue["root"]} if issue else None
        s["can_force"] = self._forceable(s) and not s["pending_permissions"]
        # background work, oldest first (none once Claude has exited, even if its end went unreported)
        s["tasks"] = [] if s["state"] == "exited" else sorted(
            (dict(x) for x in (s.get("tasks") or {}).values()), key=lambda x: x.get("since") or 0)
        return s

    def list_sessions(self):
        with self.lock:
            out = [self.summary(n) for n in self.sessions]
        out.sort(key=session_order)
        return out

    def detail(self, name, limit=100):
        with self.lock:
            if name not in self.sessions:
                return None
            return {"session": self.summary(name), "now": core.now(),   # the page corrects for its own clock's offset
                    "events": _last(list(self.events.get(name, ())), limit),
                    "permissions": [perms._public(p) for p in self.perms.values()
                                    if p["session"] == name and p["status"] in ("pending", "deciding")],
                    "prompts": [p for p in self.prompts if p["session"] == name][-30:],
                    "pages": {k: {"version": p["version"], "title": p["title"], "share": p.get("share")}
                              for k, p in self.pages.items() if p["session"] == name}}
