"""The prompt queue: typing into idle sessions, steering busy ones, interrupting."""
import threading
import uuid

from . import commands, core, states, tmuxctl


MAX_PROMPT = 100_000     # chars accepted for a browser prompt

FORCE_STALE = 60  # a "working" session silent this long may be force-typed into, if the user confirms

GOAL_CLEAR_WORDS = {"clear", "stop", "off", "reset", "none", "cancel"}  # /goal <one of these> removes the goal

STEER_MAX = 8000          # chars handed to Claude in one PostToolUse (longer messages wait for the turn's end)
STEER_INTRO = ("The user sent {what} from their phone (grovy) while you were working. {it} the user's own "
               "words, exactly as if they had typed {them} into the terminal; take {them} into account now:\n\n")


class Prompts:
    """The prompt queue: typing into idle sessions, steering busy ones, interrupting: part of Store."""

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
                m = commands.CMD_RE.match(lead)
                command = m.group(1) if m else None
                if not command or command.lower() in commands.RESERVED_COMMANDS - commands.PHONE_SAFE or \
                        command not in {c["name"] for c in commands.commands_for(self.sessions[name].get("cwd"))}:
                    return 400, (f"{lead.split()[0]} can't be run from the phone (only skills, custom commands, "
                                 f"/clear, /compact and /goal). Use the terminal: grovy attach {name}.")
            p = {"id": uuid.uuid4().hex, "session": name, "text": text, "status": "queued", "force": force is True,
                 "command": command, "created": core.now(), "updated": core.now(), "error": None}
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
                    p["status"], p["updated"] = "cancelled", core.now()
                    self._save_prompts()
                    break
            else:
                return 404, "unknown prompt"
        self.publish(p["session"])
        return 200, "cancelled"

    def take_steers(self, name):
        """Steering: messages queued while Claude works are handed to it by the hook of the main agent's next tool
        call (PostToolUse additionalContext), with no keystrokes, so nothing can land in a dialog. Verified with
        Claude Code 2.1.325, Haiku and Opus: Claude acts on them mid-turn. The leading run of queued plain messages
        goes, in order; a queued slash command (and everything after it) waits for the turn's end, as does a turn
        that ends before any tool call: then try_deliver types them as usual. Returns the context text or None."""
        name = self._canon(name)
        with self.lock:
            s = self.sessions.get(name)
            if not s or s["state"] == "exited":
                return None
            take, size = [], 0
            for p in self.prompts:
                if p["session"] != name or p["status"] != "queued":
                    continue
                if p.get("command") or size + len(p["text"]) > STEER_MAX:
                    break
                take.append(p)
                size += len(p["text"])
            if not take:
                return None
            for p in take:
                p["status"], p["updated"] = "steered", core.now()
                self._record(name, "Steered", {"prompt": core.clip(p["text"]), "sent": p["created"]})
            self._save_prompts()
        self.publish(name)
        one = len(take) == 1
        intro = STEER_INTRO.format(what="this message" if one else f"these {len(take)} messages",
                                   it="It is" if one else "They are", them="it" if one else "them")
        return intro + "\n\n---\n\n".join(p["text"] for p in take)

    def _fail_queued(self, name, why):
        """Queued prompts belong to the session instance that was running when they were queued."""
        for p in self.prompts:
            if p["session"] == name and p["status"] == "queued":
                p["status"], p["error"], p["updated"] = "failed", why, core.now()
        self._save_prompts()

    def _confirm_prompt(self, name, text):
        t = core.squash(text)
        order = sorted((p for p in self.prompts if p["session"] == name), reverse=True,
                       key=lambda p: (p["status"] == "injected", p["created"]))  # the one in flight first
        for p in order:
            if p["status"] in ("injected", "unconfirmed") and \
                    (core.squash(p["text"]) == t or core.squash(p["text"])[:200] == t[:200]):
                p["status"], p["updated"] = "submitted", core.now()
                self._save_prompts()
                return p
        return None

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
            if s["state"] not in states.INPUT_STATES and not (p.get("force") and self._forceable(s)):
                return
            # Persist "injected" BEFORE typing: a crash after this point can never cause a second send.
            p["status"], p["updated"] = "injected", core.now()
            self._set(name, "working", states.DELIVERY_ACTIVITY.get(p.get("command"), states.DELIVERY_ACTIVITY[None]))
            self._apply_goal(name, p)
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
            if not s or s["state"] != "working" or s["activity"] not in states.DELIVERY_ACTIVITY.values():
                return "Claude started something else while the prompt was being typed"
        return None

    def _paste(self, p):
        aborted = False
        try:
            self.paste(p["session"], p["text"], bool(p.get("command")), abort=lambda: self._still_safe(p))
            err = None
        except tmuxctl.PasteAborted as e:  # never pressed Enter; the partial text may be left in the input
            err, aborted = str(e), True
        except Exception as e:  # tmux failure, session gone, pane reused...
            err = str(e)
        with self.lock:
            if not err and p["status"] == "injected":
                p["typed"] = core.now()  # Enter pressed: Claude Code has it; its hook (the read receipt) may come much later
            if err and p["status"] == "injected":
                p["status"], p["updated"] = "failed", core.now()
                p["error"] = (err + ". Enter was not pressed; part of it may be left in the terminal input "
                              "(check it: grovy attach " + p["session"] + ")") if aborted else err
                if not aborted:
                    self._set(p["session"], "unknown", f"prompt delivery failed: {err}")
            self._save_prompts()
        self.publish(p["session"])

    def _apply_goal(self, name, p):
        """Track the goal the phone shows: delivering /goal <condition> sets it; /goal clear (and the other
        clear words), or /clear, removes it. Phone-side and best-effort: a goal set or finished in the terminal
        isn't reflected, and the evaluator meeting it doesn't clear this. Call under the lock."""
        cmd = p.get("command")
        if cmd == "clear":
            self.sessions[name]["goal"] = None
            return
        if cmd != "goal":
            return
        m = commands.CMD_RE.match(p["text"].lstrip())
        arg = p["text"].lstrip()[m.end():].strip() if m else ""
        if not arg:
            return  # "/goal" alone shows status; it doesn't change the goal
        self.sessions[name]["goal"] = None if arg.lower() in GOAL_CLEAR_WORDS else arg

    def _forceable(self, s):
        """unknown, or working but silent (e.g. turn interrupted with Esc: Claude emits no hook)."""
        return s["state"] == "unknown" or (
            s["state"] == "working" and core.now() - (s.get("last_event") or 0) > FORCE_STALE)
