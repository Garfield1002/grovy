"""grovyd - the grovy daemon: session state from hooks, web UI, permission decisions, prompt queue.

Two listeners:
  * a Unix socket (state dir, mode 0600) for hook events and the CLI; never on the network
  * an HTTP server (default 127.0.0.1) for the browser: JSON API + SSE + index.html

One RLock guards all in-memory state. Nothing here is the parent of a Claude process.
"""
import base64
import hashlib
import hmac
import http.server
import ipaddress
import socket
import json
import os
import re
import secrets
import select
import shlex
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from html import escape as html_escape
from urllib.parse import urlsplit, parse_qs

import grovy

HISTORY = 300            # events kept in memory per session
MAX_FIELD = 20000        # chars kept of any one text field in the event log
MAX_PROMPT = 100_000     # chars accepted for a browser prompt
MAX_BODY = 256 * 1024
MAX_REVIEW = 512 * 1024
SSE_PER_CLIENT = 4  # live-update streams kept per client IP (older ones are closed)  # bytes of tool input shown for approval; beyond that, approve in the terminal
CONFIRM_TIMEOUT = 30     # seconds for UserPromptSubmit to confirm an injected prompt
DELIVERY_ACTIVITY = {None: "sending prompt from web", "compact": "compacting conversation",
                     "clear": "clearing conversation"}
COMMAND_TIMEOUT = 600    # /clear, /compact: their receipt is SessionStart, sent only once they finish
RECONCILE_EVERY = 10
INPUT_STATES = ("idle", "error")  # states in which we will type into the session
DUP_WINDOW = 1.0  # seconds
FORCE_STALE = 60  # a "working" session silent this long may be force-typed into, if the user confirms
PERM_TIMEOUT = grovy.PERMISSION_TIMEOUT

LABELS = {"working": "working", "needs_input": "waiting on a terminal dialog", "starting": "starting",
          "unknown": "in an unknown state"}
STATE_RANK = {"needs_input": 0, "error": 1, "unknown": 2, "working": 3, "starting": 4, "idle": 5, "exited": 6}


def now():
    return time.time()


def norm(text):
    return "\n".join(l.rstrip() for l in str(text).replace("\r\n", "\n").replace("\r", "\n").strip().split("\n"))


PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")
MACHINE_PROMPT = re.compile(r"\s*<(agent-message|task-notification)\b")


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


class Store:
    def __init__(self, sdir, tmux_live=grovy.tmux_managed, paste=grovy.paste_prompt, notifier=None,
                 key=grovy.send_key, lifecycle=None):
        self.dir = sdir
        self.lock = threading.RLock()
        self.tmux_live = tmux_live
        self.paste = paste
        self.key = key
        self.lifecycle = lifecycle or Lifecycle()
        self.notifier = notifier
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
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        self.aliases = self._read_aliases()
        pages = grovy.read_json(self.pages_path, {})
        self.pages = {k: v for k, v in pages.items() if ID_RE.match(k) and isinstance(v, dict)} \
            if isinstance(pages, dict) else {}
        st = grovy.read_json(self.state_path, {})
        for name, s in (st.get("sessions") or {}).items():
            if grovy.valid_name(name) and isinstance(s, dict):
                self.sessions[name] = s
                if s.get("state") == "needs_input":
                    # The waiting hook saw EOF and exited without output, so Claude is now showing its
                    # terminal dialog: still needs_input (and therefore never typed into).
                    s["activity"] = "waiting in terminal (daemon restarted during a dialog)"
        self.prompts = [p for p in grovy.read_json(self.prompts_path, []) if isinstance(p, dict) and "id" in p]
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
                        self.events.setdefault(rec["session"], deque(maxlen=HISTORY)).append(rec)
                        self.seq = max(self.seq, int(rec["id"]))
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
        seen = {squash(r["data"].get("prompt", "")) for q in self.events.values() for r in q
                if r.get("type") == "UserPromptSubmit" and isinstance(r.get("data"), dict)}
        for p in self.prompts:
            if p.get("status") == "unconfirmed" and squash(p.get("text", "")) in seen:
                p["status"] = "submitted"
        self._log = open(self.log_path, "a", buffering=1)
        os.chmod(self.log_path, 0o600)

    def _save_state(self):
        grovy.atomic_write_json(self.state_path, {"sessions": self.sessions})

    def _save_pages(self):
        grovy.atomic_write_json(self.pages_path, self.pages)

    def _save_prompts(self):
        keep, count = [], {}
        for p in reversed(self.prompts):  # keep all pending + the last 50 per session
            count[p["session"]] = count.get(p["session"], 0) + 1
            if p["status"] in ("queued", "injected") or count[p["session"]] <= 50:
                keep.append(p)
        self.prompts = keep[::-1]
        grovy.atomic_write_json(self.prompts_path, self.prompts)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _read_aliases():
        return {a: n for n, v in grovy.registry().items() for a in (v.get("aliases") or [])}

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
        rec = {"id": self.seq, "ts": now(), "session": name, "claude_session_id": sid, "type": etype, "data": data}
        self._log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.events.setdefault(name, deque(maxlen=HISTORY)).append(rec)
        return rec

    def _set(self, name, state, activity=None):
        s = self._session(name)
        s["state"] = state
        if activity is not None:
            s["activity"] = activity[:200]
        s["last_event"] = now()

    def publish(self, name=None):
        msg = json.dumps({"session": name, "ts": now()})
        for q in list(self.subscribers):
            q.append(msg)
            q.wake.set()

    def _notify(self, name, kind, text):
        if self.notifier:
            self.notifier.send(name, kind, text)

    # ------------------------------------------------------------ hook ingestion
    def ingest(self, name, payload):
        """Apply one non-blocking hook event. Returns False if it was a duplicate."""
        name = self._canon(name)
        ev = payload.get("hook_event_name")
        sid = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
        digest = hashlib.sha256(json.dumps([name, payload], sort_keys=True).encode()).hexdigest()
        with self.lock:
            t = now()
            # Hook payloads carry no unique id, so identical content can be a genuine repeat
            # ("continue" typed twice, the same idle notification). Only a near-instant re-delivery
            # of the same payload is treated as a duplicate.
            if t - self.seen.get(digest, -1e9) < DUP_WINDOW:
                return False
            self.seen[digest] = t
            if len(self.seen) > 1000:
                self.seen = {k: v for k, v in self.seen.items() if t - v < DUP_WINDOW}
            s = self._session(name)
            if payload.get("cwd"):
                s["cwd"] = str(payload["cwd"])
            if ev != "PostToolUse":
                data = {k: clip(payload[k]) for k in KEEP.get(ev, ()) if k in payload}
                if ev == "Stop":
                    data["background_tasks"] = _count(payload.get("background_tasks"))
                self._record(name, ev, data, sid)
            if ev == "SessionStart":
                s["claude_session_id"] = sid
                s["last_error"] = None
                src = payload.get("source")
                ours = False
                for p in self.prompts:  # /clear and /compact emit no UserPromptSubmit; this is their receipt
                    if p["session"] == name and p["status"] == "injected" and p.get("command") == src \
                            and now() - p["updated"] < COMMAND_TIMEOUT:  # a stale one must not turn auto-compaction into "idle"
                        p["status"], p["updated"], ours = "submitted", now(), True
                        self._save_prompts()
                if src == "compact" and not ours and s["state"] == "working":
                    s["last_event"] = now()  # automatic compaction mid-turn: Claude is still working
                else:
                    self._set(name, "idle", f"session {src or 'start'}")
            elif ev == "UserPromptSubmit":
                text = norm(payload.get("prompt", ""))
                m = MACHINE_PROMPT.match(text)  # injected by Claude Code itself, not typed by the user
                self._set(name, "working", ("subagent report" if m.group(1) == "agent-message" else "task notification")
                          if m else "prompt: " + text[:120])
                self._confirm_prompt(name, payload.get("prompt", ""))
            elif ev == "PostToolUse":
                if s["state"] != "exited" and not self._pending(name):
                    self._set(name, "working", f"ran {payload.get('tool_name', 'tool')}")
            elif ev == "Stop":
                s["background"] = _count(payload.get("background_tasks"))
                msg = norm(payload.get("last_assistant_message") or "")
                # With background tasks Claude still accepts input, so "idle" stays truthful; the UI shows the count.
                self._set(name, "idle", msg[:200] or "turn finished")
                self._notify(name, "done", "finished: " + (msg[:80] or "turn complete"))
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
                    s["last_event"] = now()
            elif ev == "SessionEnd":
                if payload.get("reason") in ("clear", "resume"):
                    s["last_event"] = now()  # same process continues with a new conversation (SessionStart)
                else:
                    self._expire_perms(name, "Claude session ended")
                    self._set(name, "exited", f"conversation ended ({payload.get('reason', '')})")
                    self._fail_queued(name, "Claude exited before it was sent")
            self._save_state()
        self.publish(name)
        self.try_deliver(name)
        return True

    # ------------------------------------------------------------ permissions
    def _pending(self, name):
        return [p for p in self.perms.values() if p["session"] == name and p["status"] == "pending"]

    def open_permission(self, name, payload):
        name = self._canon(name)
        pid = uuid.uuid4().hex
        with self.lock:
            raw = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            p = {"id": pid, "session": name, "claude_session_id": payload.get("session_id"),
                 "tool_name": str(payload.get("tool_name", "?")), "tool_input": clip(raw),
                 "created": now(), "expires": now() + PERM_TIMEOUT, "status": "pending",
                 "decision": None, "answers": None, "_input": raw, "_ev": threading.Event()}
            p["kind"] = "question" if p["tool_name"] == QUESTION_TOOL and _questions(raw) else "permission"
            self._record(name, "PermissionRequest", {"id": pid, "tool_name": p["tool_name"], "kind": p["kind"],
                                                      "tool_input": p["tool_input"]}, p["claude_session_id"])
            what = ("question: " + str(_questions(raw)[0].get("header") or "Claude asks")) if p["kind"] == "question" \
                else f"permission: {p['tool_name']}"
            self._set(name, "needs_input", what)
            self._save_state()
            self.perms[pid] = p  # only once logged: a failure above can't leave a phantom pending request
        self.publish(name)
        self._notify(name, "input", what)
        return p

    def decide(self, pid, decision, answers=None):
        """Browser decision. First valid decision wins; anything else is rejected.

        Permissions take allow/deny. Questions (AskUserQuestion) take "answer" with
        {question text: answer}, delivered as updatedInput.answers, or "deny" to skip.
        """
        if decision not in ("allow", "deny", "answer"):
            return 400, "decision must be allow, deny or answer"
        with self.lock:
            p = self.perms.get(pid)
            if not p:
                return 404, "unknown permission request"
            if p["status"] != "pending":
                return 409, f"already {p['status']}" + (f" ({p['decision']})" if p["decision"] else "")
            if now() > p["expires"]:
                return 409, "expired"
            reply = {"decision": decision}
            if p["kind"] == "question":
                if decision == "allow":
                    return 400, "answer the question (or skip it)"
                if decision == "answer":
                    err = _check_answers(p["_input"], answers)
                    if err:
                        return 400, err
                    p["answers"] = answers
                    reply = {"decision": "allow", "updated_input": {**p["_input"], "answers": answers}}
                else:
                    reply["message"] = ("The user skipped this question from their phone. "
                                        "Continue with your best judgment.")
            elif decision == "answer":
                return 400, "not a question"
            p["status"], p["decision"], p["_reply"] = "deciding", decision, reply
            p["_ev"].set()
        self.publish(p["session"])
        return 200, "sent to Claude"

    def _finish_perm(self, p, status, note):
        """Final state of a request; called with the lock held."""
        p["status"] = status
        self._record(p["session"], "PermissionResult", {"id": p["id"], "status": status, "kind": p.get("kind"),
                                                    "decision": p["decision"], "answers": p.get("answers"),
                                                    "note": note})
        s = self._session(p["session"])
        if s["state"] == "needs_input" and not self._pending(p["session"]):
            if status == "delivered":
                self._set(p["session"], "working", f"{p['decision']}: {p['tool_name']}")
            elif status == "timeout":
                self._set(p["session"], "needs_input", f"permission for {p['tool_name']} now in terminal")
            else:
                self._set(p["session"], "unknown", note)
        self._save_state()

    def _expire_perms(self, name, note):
        for p in self._pending(name):
            p["decision"] = None
            p["status"] = "expiring"
            p["_note"] = note
            p["_ev"].set()

    def wait_permission(self, p, conn):
        """Block the hook's socket until decided, expired, or the hook goes away. Returns the reply."""
        while not p["_ev"].wait(0.5):
            if now() > p["expires"]:
                with self.lock:
                    if p["status"] == "pending":
                        p["status"] = "expiring"
                        p["_note"] = "timed out; falling back to terminal dialog"
                break
            r, _, _ = select.select([conn], [], [], 0)
            if r:
                try:
                    gone = conn.recv(1, 0x40) == b""  # MSG_DONTWAIT
                except BlockingIOError:
                    gone = False
                except OSError:
                    gone = True
                if gone:
                    with self.lock:
                        if p["status"] == "pending":
                            p["status"] = "expiring"
                            p["_note"] = "hook went away (answered in terminal or Claude moved on)"
                    break
        with self.lock:
            if p["status"] == "deciding":
                return p["_reply"]
            return {"decision": None}

    def complete_permission(self, p, delivered, error=None):
        with self.lock:
            if p["status"] == "deciding":
                if delivered:
                    self._finish_perm(p, "delivered", f"{p['decision']} delivered to Claude")
                else:
                    self._finish_perm(p, "failed", f"could not deliver decision: {error}")
            else:
                note = p.get("_note", "expired")
                self._finish_perm(p, "timeout" if "timed out" in note else "expired", note)
        self.publish(p["session"])
        self.try_deliver(p["session"])

    # ------------------------------------------------------------ prompts
    def enqueue(self, name, text, force=False):
        if not isinstance(text, str) or not text.strip():
            return 400, "empty prompt"
        if len(text) > MAX_PROMPT:
            return 413, "prompt too long"
        with self.lock:
            if name not in self.sessions:
                return 404, "unknown session"
            command = None
            lead = text.lstrip()  # Claude Code trims leading whitespace/newlines before parsing (verified)
            if lead.startswith("!"):
                return 400, "Bash mode (!) isn't available from the phone. Ask Claude to run the command instead."
            if lead.startswith("/"):
                m = CMD_RE.match(lead)
                command = m.group(1) if m else None
                if not command or command.lower() in RESERVED_COMMANDS - {"clear", "compact"} or \
                        command not in {c["name"] for c in commands_for(self.sessions[name].get("cwd"))}:
                    return 400, (f"{lead.split()[0]} can't be run from the phone (only skills, custom commands, "
                                 f"/clear and /compact). Use the terminal: grovy attach {name}.")
            p = {"id": uuid.uuid4().hex, "session": name, "text": text, "status": "queued", "force": force is True,
                 "command": command, "created": now(), "updated": now(), "error": None}
            self.prompts.append(p)
            self._save_prompts()
        self.publish(name)
        self.try_deliver(name)
        with self.lock:
            # "Type anyway" was confirmed for the state the user saw *now*. If it couldn't be typed
            # immediately, it becomes an ordinary queued prompt: a later uncertain state (e.g. an open
            # terminal dialog after a restart) must never inherit that consent.
            p["force"] = False
        return 200, p

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
        if not grovy.valid_name(name):
            return 400, "name: letters, digits, - and _ (max 32)"
        if not isinstance(path, str) or path not in self.lifecycle.trusted_projects():
            return 400, "pick one of the listed folders"
        if grovy.name_in_use(grovy.registry(), name):
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
        if not isinstance(new, str) or not grovy.valid_name(new):
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
                for p in self.prompts + list(self.perms.values()) + list(self.pages.values()):
                    if p["session"] == old:
                        p["session"] = new
                self._save_pages()
                self.aliases = {a: (new if n == old else n) for a, n in self.aliases.items()}
                self.aliases[old] = new
                self.aliases.pop(new, None)
                self._record(new, "Renamed", {"from": old, "to": new})
                self._save_state()
                self._save_prompts()
        self.publish(old)
        self.publish(new)
        return 200, new

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
                (f"Claude is {LABELS.get(s['state'], s['state'])}", s["state"] not in ("idle", "error")),
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
        with grovy.registry_locked() as reg:
            if reg.pop(name, None) is None and name not in self.sessions:
                return 404, "unknown session"
        with self.lock:
            self.sessions.pop(name, None)
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

    # ------------------------------------------------------------ pages (local "artifacts")
    def publish_page(self, session, name, title, html):
        """Store a copy of an HTML page Claude made (`grovy page publish`). Same session + name = new version."""
        if not isinstance(name, str) or not PAGE_NAME_RE.fullmatch(name):
            return 400, "page name: letters, digits, '.', '-', '_' (max 64)"
        if not isinstance(html, str):
            return 400, "no HTML"
        data = html.encode("utf-8", "surrogateescape")
        if len(data) > PAGE_MAX:
            return 413, f"page too large ({len(data) // 1024} KB, max {PAGE_MAX // 1024} KB)"
        title = (title if isinstance(title, str) and title.strip() else name).strip()[:200]
        with self.lock:
            session = self._canon(session)
            if session not in self.sessions:
                return 404, f"unknown session {session!r}"
            pid = next((k for k, p in self.pages.items() if p["session"] == session and p["name"] == name), None)
            pid = pid or uuid.uuid4().hex
            os.makedirs(self.pages_dir, mode=0o700, exist_ok=True)
            path = os.path.join(self.pages_dir, pid + ".html")
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            p = self.pages.setdefault(pid, {"id": pid, "session": session, "name": name, "version": 0})
            p.update(title=title, version=p["version"] + 1, updated=now(), size=len(data))
            self._save_pages()
            self._record(session, "Page", {"page": pid, "name": name, "title": title, "version": p["version"]})
            out = dict(p)
        self.publish(session)
        return 200, out

    def share_page(self, pid, shared):
        """A shared page gets an unguessable link (/s/<token>) that works without the grovy token. Unsharing
        revokes it; sharing again issues a new one. Republishing keeps the link."""
        with self.lock:
            p = self.pages.get(pid)
            if not p:
                return 404, "unknown page"
            if bool(shared) != bool(p.get("share")):
                p["share"] = secrets.token_hex(16) if shared else None
                self._save_pages()
            out = dict(p)
        self.publish(out["session"])
        return 200, out

    def shared_page_html(self, token):
        with self.lock:
            pid = next((k for k, p in self.pages.items()
                        if p.get("share") and hmac.compare_digest(p["share"], token)), None)
        return self.page_html(pid) if pid else None

    def _drop_page(self, pid):
        p = self.pages.pop(pid, None)
        try:
            os.unlink(os.path.join(self.pages_dir, pid + ".html"))
        except FileNotFoundError:
            pass
        self._save_pages()
        return p

    def delete_page(self, pid, session=None):
        with self.lock:
            p = self.pages.get(pid)
            if not p or (session is not None and p["session"] != self._canon(session)):
                return 404, "unknown page"
            self._drop_page(pid)
        self.publish(p["session"])
        return 200, "deleted"

    def list_pages(self, session=None):
        with self.lock:
            session = session and self._canon(session)
            return sorted((dict(p) for p in self.pages.values() if session is None or p["session"] == session),
                          key=lambda p: -p["updated"])

    def page_html(self, pid):
        with self.lock:
            p = self.pages.get(pid)
            if not p:
                return None
            try:
                with open(os.path.join(self.pages_dir, pid + ".html"), "rb") as f:
                    return dict(p), f.read()
            except OSError:
                return None

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
                        if r.get("type") in ("UserPromptSubmit", "Stop") and q in str(text).lower():
                            out.append(r)
                    elif before is not None and r.get("id", 0) < before:
                        out.append(r)
        except OSError:
            return []
        return out[::-1][:limit] if q else out[-limit:]

    def interrupt(self, name):
        """Press Esc for the user, only while Claude is known to be working (never into a dialog)."""
        with self.lock:  # held across the key press, so no permission can be registered in between
            s = self.sessions.get(name)
            if not s:
                return 404, "unknown session"
            if s["state"] != "working" or self._pending(name):
                return 409, f"nothing to interrupt (Claude is {s['state']})"
            try:
                self.key(name, "Escape")
            except Exception as e:
                return 409, str(e)
            # Esc emits no hook. We pressed it, so say so instead of leaving a stale "working".
            self._record(name, "Interrupted", {})
            self._set(name, "unknown", "interrupted from the phone")
            self._save_state()
        self.publish(name)
        return 200, "interrupted"

    def cancel_prompt(self, pid):
        with self.lock:
            for p in self.prompts:
                if p["id"] == pid:
                    if p["status"] != "queued":
                        return 409, f"prompt is {p['status']}"
                    p["status"], p["updated"] = "cancelled", now()
                    self._save_prompts()
                    break
            else:
                return 404, "unknown prompt"
        self.publish(p["session"])
        return 200, "cancelled"

    def _fail_queued(self, name, why):
        """Queued prompts belong to the session instance that was running when they were queued."""
        for p in self.prompts:
            if p["session"] == name and p["status"] == "queued":
                p["status"], p["error"], p["updated"] = "failed", why, now()
        self._save_prompts()

    def _confirm_prompt(self, name, text):
        t = squash(text)
        order = sorted((p for p in self.prompts if p["session"] == name), reverse=True,
                       key=lambda p: (p["status"] == "injected", p["created"]))  # the one in flight first
        for p in order:
            if p["status"] in ("injected", "unconfirmed") and \
                    (squash(p["text"]) == t or squash(p["text"])[:200] == t[:200]):
                p["status"], p["updated"] = "submitted", now()
                self._save_prompts()
                return

    def try_deliver(self, name):
        """Type the oldest queued prompt into the session, if and only if that is known to be safe."""
        with self.lock:
            s = self.sessions.get(name)
            if not s or self._pending(name):
                return
            mine = [p for p in self.prompts if p["session"] == name]
            if any(p["status"] == "injected" for p in mine):
                return  # one in flight at a time
            queued = [p for p in mine if p["status"] == "queued"]
            if not queued:
                return
            p = queued[0]
            # Uncertain states are only typed into when the user explicitly confirmed this prompt.
            if s["state"] not in INPUT_STATES and not (p.get("force") and self._forceable(s)):
                return
            # Persist "injected" BEFORE typing: a crash after this point can never cause a second send.
            p["status"], p["updated"] = "injected", now()
            self._set(name, "working", DELIVERY_ACTIVITY.get(p.get("command"), DELIVERY_ACTIVITY[None]))
            self._save_prompts()
            self._save_state()
        self.publish(name)
        threading.Thread(target=self._paste, args=(p,), daemon=True).start()

    def _still_safe(self, p):
        """Asked by the typing loop between chunks and before Enter: a reason to stop, or None."""
        with self.lock:
            s = self.sessions.get(p["session"])
            if p["status"] != "injected":
                return "the prompt was cancelled"
            if self._pending(p["session"]):
                return "Claude opened a permission request while the prompt was being typed"
            if not s or s["state"] != "working" or s["activity"] not in DELIVERY_ACTIVITY.values():
                return "Claude started something else while the prompt was being typed"
        return None

    def _paste(self, p):
        aborted = False
        try:
            self.paste(p["session"], p["text"], bool(p.get("command")), abort=lambda: self._still_safe(p))
            err = None
        except grovy.PasteAborted as e:  # never pressed Enter; the partial text may be left in the input
            err, aborted = str(e), True
        except Exception as e:  # tmux failure, session gone, pane reused...
            err = str(e)
        with self.lock:
            if err and p["status"] == "injected":
                p["status"], p["updated"] = "failed", now()
                p["error"] = (err + ". Enter was not pressed; part of it may be left in the terminal input "
                              "(check it: grovy attach " + p["session"] + ")") if aborted else err
                if not aborted:
                    self._set(p["session"], "unknown", f"prompt delivery failed: {err}")
            self._save_prompts()
        self.publish(p["session"])

    # ------------------------------------------------------------ reconciliation
    def reconcile(self):
        changed = set()
        with self.lock:
            # Snapshot under the state lock: a snapshot taken outside it could be applied after a
            # newer hook event and mark a just-started session "exited".
            try:
                live = self.tmux_live()
            except RuntimeError as e:  # tmux unresponsive: skip this round rather than guess "exited"
                print(f"grovyd: reconcile skipped: {e}", file=sys.stderr)
                return
            reg = grovy.registry()
            self.aliases = {a: n for n, v in reg.items() for a in (v.get("aliases") or [])}
            for name in set(reg) | set(live) | set(self.sessions):
                s = self._session(name)
                if name in reg:
                    s["cwd"] = s.get("cwd") or reg[name].get("cwd")
                    s["claude_session_id"] = s.get("claude_session_id") or reg[name].get("claude_session_id")
                if name not in live and s["state"] != "exited":
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
                        if p["status"] not in ("pending", "deciding") and now() - p["created"] > 3600]:
                del self.perms[pid]
            for p in self.prompts:
                limit = COMMAND_TIMEOUT if p.get("command") in ("clear", "compact") else CONFIRM_TIMEOUT
                if p["status"] == "injected" and now() - p["updated"] > limit:
                    p["status"], p["updated"] = "unconfirmed", now()
                    p["error"] = "no UserPromptSubmit seen; check the terminal"
                    changed.add(p["session"])
                    s = self.sessions.get(p["session"])
                    if s and s["state"] == "working" and s["activity"] in DELIVERY_ACTIVITY.values():
                        self._set(p["session"], "unknown", "prompt not confirmed")
                elif p["status"] == "queued" and p["session"] not in live:  # incl. forgotten sessions: never
                    # deliver into a future session that merely reuses the name
                    p["status"], p["error"], p["updated"] = "failed", "session not running", now()
                    changed.add(p["session"])
            if changed:
                self._save_state()
                self._save_prompts()
        for name in changed:
            self.publish(name)
        for name in list(self.sessions):
            self.try_deliver(name)

    def _forceable(self, s):
        """unknown, or working but silent (e.g. turn interrupted with Esc: Claude emits no hook)."""
        return s["state"] == "unknown" or (
            s["state"] == "working" and now() - (s.get("last_event") or 0) > FORCE_STALE)

    # ------------------------------------------------------------ views
    def summary(self, name):
        s = dict(self.sessions[name])
        mine = [p for p in self.prompts if p["session"] == name]
        s["pending_permissions"] = len(self._pending(name))
        s["queued_prompts"] = sum(p["status"] in ("queued", "injected") for p in mine)
        s["accepts_input"] = s["state"] in INPUT_STATES and not s["pending_permissions"]
        s["can_force"] = self._forceable(s) and not s["pending_permissions"]
        return s

    def list_sessions(self):
        with self.lock:
            out = [self.summary(n) for n in self.sessions]
        out.sort(key=lambda s: (STATE_RANK.get(s["state"], 9) if not s["pending_permissions"] else -1,
                                -(s["last_event"] or 0)))
        return out

    def detail(self, name, limit=100):
        with self.lock:
            if name not in self.sessions:
                return None
            return {"session": self.summary(name),
                    "events": list(self.events.get(name, ()))[-limit:],
                    "permissions": [_public(p) for p in self.perms.values()
                                    if p["session"] == name and p["status"] in ("pending", "deciding")],
                    "prompts": [p for p in self.prompts if p["session"] == name][-30:],
                    "pages": {k: {"version": p["version"], "title": p["title"], "share": p.get("share")}
                              for k, p in self.pages.items() if p["session"] == name}}

    def permissions(self):
        with self.lock:
            return [_public(p) for p in self.perms.values() if p["status"] in ("pending", "deciding")]


CMD_RE = re.compile(r"/([A-Za-z0-9][\w:.-]*)(?:\s|$)")
BUILTIN_COMMANDS = [{"name": "clear", "description": "Start a new conversation (clears context)", "source": "built-in"},
                    {"name": "compact", "description": "Summarize the conversation to free up context",
                     "source": "built-in"}]
_cmd_cache = {}
# Claude Code built-ins (and common aliases) that open menus or act outside the conversation.
# Names are compared lowercased. Skills can't shadow these: the phone must never open a built-in menu.
RESERVED_COMMANDS = set("""add-dir agents allowed-tools approved-tools bashes btw bug chrome clear compact config
context continue copy cost doctor effort exit export extra-usage fast feedback fork help hooks ide init
install-github-app install-slack-app keybindings login logout mcp memory migrate-installer model new
output-style permissions plan plugin plugins pr-comments privacy-settings quit release-notes reload-plugins
remote-env rename reset resume review rewind sandbox security-review settings skills stats status statusline
tasks teleport terminal-setup theme todos upgrade usage vim""".split())


def _frontmatter(path):
    meta, body = {}, []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(8192).splitlines()
    except OSError:
        return meta, ""
    if lines and lines[0].strip() == "---":
        for i, l in enumerate(lines[1:], 1):
            if l.strip() == "---":
                body = lines[i + 1:]
                break
            k, _, v = l.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    else:
        body = lines
    return meta, next((l.strip("# ").strip() for l in body if l.strip()), "")


def commands_for(cwd):
    """Skills and custom commands Claude Code would offer in this project, plus /clear and /compact.
    Cached briefly; read from the same places Claude Code reads them."""
    hit = _cmd_cache.get(cwd)
    if hit and now() - hit[0] < 30:
        return hit[1]
    import glob
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    found = {}

    def add(name, desc, source):
        if name.lower() in RESERVED_COMMANDS:  # a repo's SKILL.md named "model"/"Model" must not unlock /model
            return
        if name and re.fullmatch(r"[A-Za-z0-9][\w:.-]*", name) and name not in found:
            found[name] = {"name": name, "description": desc[:140], "source": source}

    roots = [(os.path.join(cwd, ".claude"), "project")] if cwd else []
    roots.append((home, "user"))
    for root, source in roots:
        for f in sorted(glob.glob(os.path.join(glob.escape(root), "skills", "*", "SKILL.md"))):
            meta, first = _frontmatter(f)
            add(meta.get("name") or os.path.basename(os.path.dirname(f)), meta.get("description") or first, source)
        cdir = os.path.join(root, "commands")
        for f in sorted(glob.glob(os.path.join(glob.escape(cdir), "**", "*.md"), recursive=True)):
            meta, first = _frontmatter(f)
            rel = os.path.relpath(f, cdir)[:-3].replace(os.sep, ":")
            add(rel, meta.get("description") or first, source)
    for f in sorted(glob.glob(os.path.join(glob.escape(home), "skills", "synced", "*", "*", "SKILL.md"))):
        meta, first = _frontmatter(f)  # claude.ai-synced skills are invoked with this prefix
        add("anthropic-skills:" + (meta.get("name") or os.path.basename(os.path.dirname(f))),
            meta.get("description") or first, "synced")
    out = BUILTIN_COMMANDS + sorted(found.values(), key=lambda c: c["name"])
    _cmd_cache[cwd] = (now(), out)
    return out


QUESTION_TOOL = "AskUserQuestion"


def _questions(inp):
    qs = inp.get("questions") if isinstance(inp, dict) else None
    return [q for q in qs if isinstance(q, dict) and isinstance(q.get("question"), str)] if isinstance(qs, list) else []


def _check_answers(inp, answers):
    """Every question needs a non-empty string answer; nothing else is accepted."""
    qs = _questions(inp)
    if not isinstance(answers, dict) or set(answers) != {q["question"] for q in qs}:
        return "answers must cover exactly the questions asked"
    for v in answers.values():
        if not isinstance(v, str) or not v.strip() or len(v) > 4000:
            return "each answer must be non-empty text (max 4000 chars)"
    return None


def _owner(r):
    """Session name of a stored record; records written before the rename used the key "ccs"."""
    return r.get("session", r.get("ccs"))


def _count(v):
    if isinstance(v, (list, dict)):
        return len(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return 0


def _public(p):
    """A live request as the browser sees it: the full tool input (not the clipped log copy) when it
    fits, otherwise flagged so the UI refuses to let the user approve what it can't show."""
    d = {k: v for k, v in p.items() if not k.startswith("_")}
    raw = p.get("_input")
    if raw is not None:
        fits = len(json.dumps(raw, ensure_ascii=False)) <= MAX_REVIEW
        d["tool_input"], d["truncated"] = (raw if fits else p["tool_input"]), not fits
    return d


# ---------------------------------------------------------------- notifications

class Lifecycle:
    """tmux/claude side effects for phone-initiated lifecycle actions (swappable in tests)."""

    @staticmethod
    def trusted_projects():
        return grovy.trusted_projects()

    @staticmethod
    def start(name, path, extra):
        grovy.start_session(name, path, extra)
        grovy.check_started(name, path, extra)

    @staticmethod
    def resume(name):
        return grovy.resume_session(name)

    @staticmethod
    def conversations(path):
        return grovy.conversations(path)

    @staticmethod
    def live_conversations():
        return grovy.live_conversations()

    @staticmethod
    def kill(name):
        grovy.kill_session(name)

    @staticmethod
    def rename(old, new):
        grovy.rename_session(old, new)


class Notifier:
    """Runs GROVY_NOTIFY_CMD (argv, shlex-split) with the message on stdin. Never blocks callers."""

    def __init__(self, cmd, url=None, min_gap=60):
        self.cmd, self.url, self.min_gap = shlex.split(cmd), url, min_gap
        self.last = {}

    def send(self, name, kind, text):
        key = (name, kind)
        if now() - self.last.get(key, 0) < self.min_gap:
            return
        self.last[key] = now()
        msg = f"[{name}] {text}" + (f"\n{self.url}#/s/{name}" if self.url else "")
        threading.Thread(target=self._run, args=(msg, kind, name), daemon=True).start()

    def _run(self, msg, kind, name):
        try:
            subprocess.run(self.cmd, input=msg, text=True, timeout=15, capture_output=True,
                           env={**os.environ, "GROVY_NOTIFY_KIND": kind, "GROVY_NOTIFY_SESSION": name,
                                "CCS_NOTIFY_KIND": kind, "CCS_NOTIFY_SESSION": name})
        except (OSError, subprocess.SubprocessError) as e:
            print(f"grovyd: notify failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------- unix socket (hooks + CLI)

class SockHandler(socketserver.StreamRequestHandler):
    def handle(self):
        store = self.server.store
        try:
            line = self.rfile.readline(8 * PAGE_MAX)   # a page's HTML, JSON-escaped
            msg = json.loads(line)
        except (ValueError, OSError):
            return
        t = msg.get("type") if isinstance(msg, dict) else None
        if t == "hook":
            name, payload = msg.get("session") or msg.get("ccs"), msg.get("event")
            if not grovy.valid_name(name) or not isinstance(payload, dict):
                return
            if payload.get("hook_event_name") == "PermissionRequest":
                self._permission(store, name, payload)
            else:
                store.ingest(name, payload)
        elif t == "status":
            self._reply({"sessions": {s["name"]: s for s in store.list_sessions()}, "url": self.server.url,
                         "claude": shutil.which(grovy.claude_cmd())})
        elif t == "refresh":
            store.reconcile()
        elif t == "statusline":
            name = msg.get("session")
            if grovy.valid_name(name) and isinstance(msg.get("data"), dict):
                store.status(name, msg["data"])
        elif t == "rename":
            code, res = store.rename(str(msg.get("old", "")), msg.get("new"))
            self._reply({"ok": res} if code == 200 else {"error": res})
        elif t == "page":
            op, session = msg.get("op"), str(msg.get("session") or "")
            if op == "publish":
                code, res = store.publish_page(session, msg.get("name"), msg.get("title"), msg.get("html"))
                if code == 200:
                    res["url"] = (self.server.url or "").rstrip("/") + "/p/" + res["id"]
            elif op == "ls":
                code, res = 200, store.list_pages(session or None)
            elif op == "rm":   # sharing is deliberately not here: only the phone (a person) can share a page
                pid = next((p["id"] for p in store.list_pages(session) if p["name"] == msg.get("name")), None)
                code, res = store.delete_page(pid, session) if pid else (404, "no such page in this session")
            else:
                code, res = 400, "unknown page op"
            self._reply({"ok": res} if code == 200 else {"error": res})

    def _reply(self, obj):
        self.wfile.write(json.dumps(obj).encode() + b"\n")
        self.wfile.flush()

    def _permission(self, store, name, payload):
        p = store.open_permission(name, payload)
        reply = store.wait_permission(p, self.connection)
        try:
            self._reply(reply)
            store.complete_permission(p, delivered=True)
        except OSError as e:
            store.complete_permission(p, delivered=False, error=str(e))


class SockServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


# ---------------------------------------------------------------- HTTP

INDEX_PATH = os.path.join(grovy.HERE, "index.html")


CUSTOM_CSS_MAX = 256 * 1024


def custom_css_path():
    return os.path.join(grovy.config_dir(), "custom.css")


def custom_css():
    """The user's custom.css, or b"" (missing, too big, or containing "</style", which would end the block)."""
    try:
        with open(custom_css_path(), "rb") as f:
            css = f.read(CUSTOM_CSS_MAX + 1)
    except OSError:
        return b""
    if len(css) > CUSTOM_CSS_MAX or b"</style" in css.lower():
        print(f"grovyd: ignoring {custom_css_path()}: over {CUSTOM_CSS_MAX // 1024} KB or contains </style",
              file=sys.stderr, flush=True)
        return b""
    return css


def load_index():
    with open(INDEX_PATH, "rb") as f:
        html = f.read()
    css = custom_css()
    if css:   # after the built-in styles, so the same selectors and tokens override them
        html = html.replace(b"</style>", b"</style>\n<style id=\"custom-css\">\n" + css + b"\n</style>", 1)
    scripts = re.findall(rb"<script>(.*?)</script>", html, re.S)
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(s).digest()).decode() for s in scripts)
    csp = (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; connect-src 'self'; "
           "img-src 'self' data:; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return html, csp


ID_RE = re.compile(r"^[0-9a-f]{32}$")


def with_bridge(html):
    """Insert PAGE_BRIDGE after the doctype (anything before it would put the page in quirks mode)."""
    m = re.match(rb"(\xef\xbb\xbf)?\s*<!doctype[^>]*>", html, re.I)
    return html[:m.end()] + PAGE_BRIDGE + html[m.end():] if m else PAGE_BRIDGE + html

PAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PAGE_MAX = 5 * 1024 * 1024
PAGE_CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
# Pages are Claude-written HTML: sandboxed (opaque origin: no cookies, no grovy API, no other pages), libraries
# only from the CDN allowlist, and no fetch/XHR/WebSocket/images from the network, so a page can't send data out.
# Browsers can't stop a page from navigating its own frame elsewhere; docs/DESIGN.md "Known limitations" says so.
PAGE_CSP = ("sandbox allow-scripts allow-forms allow-modals allow-downloads; default-src 'none'; "
            f"script-src 'unsafe-inline' 'unsafe-eval' {PAGE_CDNS}; "
            f"style-src 'unsafe-inline' {PAGE_CDNS} https://fonts.googleapis.com; "
            f"font-src data: {PAGE_CDNS} https://fonts.gstatic.com; img-src data: blob:; media-src data: blob:; "
            "connect-src 'none'; frame-src 'none'; worker-src blob:; form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'self'")
# Injected first into every page: grovy.send(text) proposes a message (the phone puts it in the message box).
PAGE_BRIDGE = (b"<script>window.grovy={send:function(t){if(window.parent===window){alert('Open this page from grovy "
               b"to send it to Claude.');return}window.parent.postMessage({grovy:'send',text:String(t)},'*')}};</script>")
REQ_ID_RE = re.compile(r"[A-Za-z0-9-]{16,64}")
HOST_PAGE = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
             b"<title>grovy</title><body style='font:16px system-ui;padding:24px;max-width:30em'>"
             b"<h2>grovy doesn't know the name &ldquo;{host}&rdquo;</h2><p>To protect against DNS rebinding, grovy only "
             b"answers to IP addresses and names it was told about. Either open it by IP address, or restart the "
             b"daemon with <code>GROVY_HOSTS={host}</code> in its environment.</p>")
LOGIN_PAGE = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
              b"<title>grovy</title><body style='font:16px system-ui;padding:24px;max-width:30em'>"
              b"<h2>grovy: login needed</h2><p>Open the link printed by <code>grovy url</code> on the machine "
              b"(it ends in <code>?token=...</code>). Your browser remembers it for a year.</p>")


class Http(http.server.BaseHTTPRequestHandler):
    server_version = "grovyd"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if grovy.env("DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json", extra=(), frame="DENY"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", frame)
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        # One request per connection: an idle keep-alive socket closed by our deadline at the moment the
        # browser reuses it makes that request fail ("page sometimes fails to load"). On a LAN a fresh
        # connection costs nothing.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _authed(self):
        token = self.server.token
        if not token or not self.server.enforce:
            return True
        got = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            got = auth[7:]
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k in ("grovy_token", "ccs_token"):  # ccs_token: cookie set before the rename
                got = got or v
        return hmac.compare_digest(got.encode(), token.encode())

    timeout = 30  # seconds of client silence before a connection is dropped
    HEADER_DEADLINE = 15  # whole request line + headers must arrive within this (per-recv timeouts can be trickled)

    def handle_one_request(self):
        t = self._deadline = threading.Timer(self.HEADER_DEADLINE, self._abort)
        t.daemon = True
        t.start()
        self.server.waiting_add(self)
        try:
            super().handle_one_request()
        finally:
            t.cancel()
            self.server.waiting_remove(self)

    def parse_request(self):
        ok = super().parse_request()  # reads the headers; past this point the deadline no longer applies
        self._deadline.cancel()
        self.server.waiting_remove(self)
        return ok

    def _abort(self):
        if grovy.env("DEBUG"):
            print(f"grovyd: closed idle connection from {self.client_address[0]} "
                  f"(no request within {self.HEADER_DEADLINE}s)", file=sys.stderr, flush=True)
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _host_ok(self):
        """DNS-rebinding guard: a browser reaching us through an attacker's name sends that name as
        Host. Accept only IP literals, localhost, this machine's names, and GROVY_HOSTS."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name in self.server.allowed_hosts

    def do_GET(self):
        if not self._host_ok():
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0][:100]
            if urlsplit(self.path).path == "/":
                return self._send(403, HOST_PAGE.replace(b"{host}", html_escape(host).encode()), "text/html")
            return self._err(403, "unknown Host header (add the name to GROVY_HOSTS)")
        url = urlsplit(self.path)
        path = url.path
        if path == "/" and self.server.token:
            q = parse_qs(url.query).get("token", [""])[0]
            if q and hmac.compare_digest(q.encode(), self.server.token.encode()):
                return self._send(303, b"", "text/plain", [
                    ("Location", "/"),
                    ("Set-Cookie", f"grovy_token={q}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")])
        m = re.fullmatch(r"/s/([0-9a-f]{32})", path)
        if m:   # a shared page: its link is the credential, and it grants nothing but this page
            got = self.server.store.shared_page_html(m.group(1))
            if not got:
                return self._send(404, b"Not found (this page is no longer shared).", "text/plain")
            return self._send(200, got[1], "text/html", [("Content-Security-Policy", PAGE_CSP)])
        if not self._authed():
            if path == "/":
                return self._send(401, LOGIN_PAGE, "text/html")
            return self._err(401, "token required: open /?token=...")
        store = self.server.store
        if path == "/":
            self.server.refresh_index()
            return self._send(200, self.server.index, "text/html",
                              [("Content-Security-Policy", self.server.csp)])
        if path == "/api/sessions":
            return self._send(200, {"sessions": store.list_sessions()})
        if path == "/api/permissions":
            return self._send(200, {"permissions": store.permissions()})
        if path == "/api/events":
            return self._sse()
        if path == "/api/projects":
            return self._send(200, {"projects": store.lifecycle.trusted_projects()})
        if path == "/api/conversations":
            code, res = store.conversations(parse_qs(url.query).get("path", [""])[0])
            return self._send(code, {"conversations": res} if code == 200 else {"error": res})
        m = re.fullmatch(r"/api/sessions/([^/]+)/history", path)
        if m and grovy.valid_name(m.group(1)):
            qs = parse_qs(url.query)
            q = (qs.get("q", [""])[0] or "").strip()[:200]
            try:
                before = int(qs.get("before", ["0"])[0]) or None
            except ValueError:
                return self._err(400, "bad 'before'")
            if not q and before is None:
                return self._err(400, "need 'before' or 'q'")
            return self._send(200, {"events": store.history(m.group(1), before=before, q=q or None)})
        m = re.fullmatch(r"/p/([0-9a-f]{32})", path)
        if m:
            got = store.page_html(m.group(1))
            if not got:
                return self._send(404, b"This page was deleted.", "text/plain")
            p, html = got
            return self._send(200, with_bridge(html), "text/html",
                              [("Content-Security-Policy", PAGE_CSP)], frame="SAMEORIGIN")
        if path == "/api/pages":
            return self._send(200, {"pages": store.list_pages()})
        m = re.fullmatch(r"/api/sessions/([^/]+)/pages", path)
        if m and grovy.valid_name(m.group(1)):
            return self._send(200, {"pages": store.list_pages(m.group(1))})
        m = re.fullmatch(r"/api/sessions/([^/]+)/commands", path)
        if m and grovy.valid_name(m.group(1)):
            s = store.sessions.get(m.group(1))
            return self._send(200, {"commands": commands_for(s.get("cwd"))}) if s else self._err(404, "unknown session")
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if m and grovy.valid_name(m.group(1)):
            d = store.detail(m.group(1))
            return self._send(200, d) if d else self._err(404, "unknown session")
        self._err(404, "not found")

    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "unknown Host header (add the name to GROVY_HOSTS)")
        if not self._authed():
            return self._err(401, "unauthorized")
        # CSRF: JSON content type forces a CORS preflight we never answer; Origin, when sent, must be us.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._err(403, "cross-origin request rejected")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._err(415, "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._err(400, "bad Content-Length")
        if n > MAX_BODY:
            return self._err(413, "body too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._err(400, "invalid JSON object")
        path = urlsplit(self.path).path
        # Idempotency: the page retries an action once if the network drops it, with the same request id.
        # A repeat returns the first outcome (waiting if that one is still running) instead of acting twice.
        rid = self.headers.get("X-Request-Id", "")
        if not REQ_ID_RE.fullmatch(rid):
            return self._send(*self._route_post(path, body))
        with self.server.done_lock:
            entry = self.server.done.get(rid)
            first = entry is None
            if first:
                entry = self.server.done[rid] = {"ev": threading.Event(), "res": None, "t": now()}
                for k in [k for k, v in self.server.done.items() if now() - v["t"] > 600]:
                    del self.server.done[k]
        if first:
            try:
                entry["res"] = self._route_post(path, body)
            except Exception:
                entry["res"] = (500, {"error": "internal error"})
                raise
            finally:
                entry["ev"].set()
        elif not entry["ev"].wait(60):
            return self._err(503, "the original request is still running")
        return self._send(*entry["res"])

    def _route_post(self, path, body):
        """Run one POST action; returns (status, JSON body). Never sends anything itself."""
        store = self.server.store
        wrap = lambda code, res: (code, {"status": res} if code == 200 else {"error": res})  # noqa: E731
        m = re.fullmatch(r"/api/sessions/([^/]+)/prompts", path)
        if m and grovy.valid_name(m.group(1)):
            code, res = store.enqueue(m.group(1), body.get("text"), body.get("force") is True)
            return code, (res if code == 200 else {"error": res})
        m = re.fullmatch(r"/api/permissions/([^/]+)/decision", path)
        if m and ID_RE.match(m.group(1)):
            return wrap(*store.decide(m.group(1), body.get("decision"), body.get("answers")))
        if path == "/api/client-error":  # the phone reports render errors it would otherwise swallow
            if now() - self.server.last_client_error > 5:
                self.server.last_client_error = now()
                msg = " | ".join(str(body.get(k, ""))[:2000].replace("\n", " ⏎ ") for k in ("view", "message", "stack"))
                print(f"grovyd: CLIENT ERROR from {self.client_address[0]}: {msg}", file=sys.stderr, flush=True)
            return 200, {"status": "logged"}
        if path == "/api/sessions":
            return wrap(*store.create(body.get("name"), body.get("path"), body.get("ask"),
                                      body.get("resume"), body.get("fork")))
        m = re.fullmatch(r"/api/sessions/([^/]+)/rename", path)
        if m and grovy.valid_name(m.group(1)):
            return wrap(*store.rename(m.group(1), body.get("name")))
        m = re.fullmatch(r"/api/sessions/([^/]+)/restart", path)
        if m and grovy.valid_name(m.group(1)):
            return wrap(*store.restart(m.group(1), force=body.get("force") is True))
        m = re.fullmatch(r"/api/sessions/([^/]+)/(resume|kill|forget|interrupt)", path)
        if m and grovy.valid_name(m.group(1)):
            return wrap(*getattr(store, m.group(2))(m.group(1)))
        m = re.fullmatch(r"/api/pages/([^/]+)/share", path)
        if m and ID_RE.match(m.group(1)):
            if not isinstance(body.get("shared"), bool):
                return 400, {"error": "shared: true or false"}
            code, res = store.share_page(m.group(1), body["shared"])
            return code, ({"page": res} if code == 200 else {"error": res})
        m = re.fullmatch(r"/api/pages/([^/]+)/delete", path)
        if m and ID_RE.match(m.group(1)):
            return wrap(*store.delete_page(m.group(1)))
        m = re.fullmatch(r"/api/prompts/([^/]+)/cancel", path)
        if m and ID_RE.match(m.group(1)):
            return wrap(*store.cancel_prompt(m.group(1)))
        return 404, {"error": "not found"}

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        q = Subscriber(maxlen=100)
        q.client = self.client_address[0]
        store = self.server.store
        try:  # a phone that sleeps or changes network never sends FIN: abort once data goes unacknowledged 30s
            self.connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", 18), 30000)
        except OSError:
            pass
        # Each wake-up opens a new stream while the old one may linger: keep only the newest few per client,
        # so stale streams can't pile up to the connection cap and block page loads.
        same = sorted((s for s in list(store.subscribers) if getattr(s, "client", None) == q.client),
                      key=lambda s: s.opened)
        for old in same[:max(0, len(same) - (SSE_PER_CLIENT - 1))]:
            old.kill = True
            old.wake.set()
        store.subscribers.add(q)
        try:
            self.wfile.write(b"retry: 3000\ndata: {\"hello\":1}\n\n")
            self.wfile.flush()
            while not self.server.stopping and not q.kill:
                q.wake.wait(15)
                q.wake.clear()
                if q.kill:
                    break
                out = b""
                while q:
                    out += b"data: " + q.popleft().encode() + b"\n\n"
                self.wfile.write(out or b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            store.subscribers.discard(q)
            self.close_connection = True


class Subscriber(deque):
    """One SSE client: a bounded queue of messages plus a wake-up event."""

    def __init__(self, maxlen):
        super().__init__(maxlen=maxlen)
        self.wake = threading.Event()
        self.kill = False
        self.opened = time.monotonic()
        self.client = None

    __hash__ = object.__hash__  # identity semantics so subscribers can live in a set

    def __eq__(self, other):
        return self is other


class HttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    stopping = False
    enforce = True
    done_lock = threading.Lock()
    last_client_error = 0
    allowed_hosts = {"localhost"}
    MAX_CONNECTIONS = 64

    WAITING_PER_IP = 8   # connections from one address that haven't sent a request yet

    def server_activate(self):
        super().server_activate()
        self.done = {}  # X-Request-Id -> outcome of an action (idempotent retries)
        self._slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        self._waiting = {}   # handler -> (since, client ip): connected, request not yet received
        self._wlock = threading.Lock()

    def waiting_add(self, handler):
        with self._wlock:
            ip = handler.client_address[0]
            mine = sorted((t, h) for h, (t, i) in self._waiting.items() if i == ip)
            self._waiting[handler] = (time.monotonic(), ip)
        for _, h in mine[:max(0, len(mine) + 1 - self.WAITING_PER_IP)]:
            h._abort()

    def waiting_remove(self, handler):
        with self._wlock:
            self._waiting.pop(handler, None)

    def _evict_idle(self):
        """At the cap, close the connection that has waited longest without sending a request. A real
        request arrives within milliseconds of connecting, so idle sockets held open by someone without
        the token can't lock the phone out (they used to fill all slots for HEADER_DEADLINE each)."""
        with self._wlock:
            idle = [(t, h) for h, (t, _) in self._waiting.items() if time.monotonic() - t > 0.5]
        if idle:
            min(idle, key=lambda x: x[0])[1]._abort()
            return True
        return False

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):  # connection cap: refuse instead of spawning threads
            if not (self._evict_idle() and self._slots.acquire(timeout=1.0)):
                self.shutdown_request(request)
                return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
    index_mtime = None

    def refresh_index(self):
        """Pick up index.html and custom.css edits without a daemon restart (the CSP hash is recomputed too)."""
        try:
            m = os.stat(INDEX_PATH).st_mtime_ns
        except OSError:
            return
        try:
            c = os.stat(custom_css_path())
            m = (m, c.st_mtime_ns, c.st_size)
        except OSError:
            m = (m, None)
        if m != self.index_mtime:
            self.index, self.csp = load_index()
            self.index_mtime = m


def serve(bind, port):
    sdir = grovy.state_dir()
    notifier = None
    if grovy.env("NOTIFY_CMD"):
        notifier = Notifier(grovy.env("NOTIFY_CMD"), grovy.env("PUBLIC_URL"))
    store = Store(sdir, notifier=notifier)

    sp = grovy.sock_path()
    if os.path.exists(sp):
        try:  # refuse to start twice
            grovy.sock_request({"type": "status"}, timeout=1)
            print(f"grovyd: already running (socket {sp})", file=sys.stderr)
            return 1
        except (OSError, ValueError):
            os.unlink(sp)
    old = os.umask(0o177)
    sock = SockServer(sp, SockHandler)
    os.umask(old)
    sock.store = store

    httpd = HttpServer((bind, port), Http)
    httpd.store = store
    # A token is always required, loopback included: other local users and DNS-rebinding pages can
    # reach 127.0.0.1 too. Generated once into the state dir; see `grovy url`. GROVY_AUTH=off keeps it
    # optional, e.g. while enrolling a phone that can only read the link through this page.
    loopback = bind in ("127.0.0.1", "::1", "localhost")
    httpd.token = grovy.load_token(create=True)
    httpd.allowed_hosts = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()} | {
        h.strip().lower() for h in grovy.env("HOSTS", "").split(",") if h.strip()}
    httpd.enforce = grovy.env("AUTH", "on") != "off"
    httpd.refresh_index()
    url = f"http://{bind}:{port}/"
    sock.url = url

    store.reconcile()
    threading.Thread(target=sock.serve_forever, daemon=True).start()

    def loop():
        while True:
            time.sleep(RECONCILE_EVERY)
            try:
                store.reconcile()
            except Exception as e:  # keep the daemon alive; surface the problem
                print(f"grovyd: reconcile error: {e}", file=sys.stderr)

    threading.Thread(target=loop, daemon=True).start()
    if not loopback and not httpd.enforce:
        print("grovyd: WARNING: listening beyond loopback with GROVY_AUTH=off; anyone on the network can use grovy",
              file=sys.stderr)
    print(f"grovyd: serving {url} (hooks on {sp})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping = True
        sock.server_close()
        try:
            os.unlink(sp)
        except OSError:
            pass
    return 0
