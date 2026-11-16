"""Permission requests and questions: waiting for the user's decision, never guessing one."""
import json
import select
import threading
import uuid

from . import core


MAX_REVIEW = 512 * 1024   # bytes of tool input shown for approval; beyond that, approve in the terminal

PERM_TIMEOUT = core.PERMISSION_TIMEOUT

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


def _public(p):
    """A live request as the browser sees it: the full tool input (not the clipped log copy) when it
    fits, otherwise flagged so the UI refuses to let the user approve what it can't show."""
    d = {k: v for k, v in p.items() if not k.startswith("_")}
    raw = p.get("_input")
    if raw is not None:
        fits = len(json.dumps(raw, ensure_ascii=False)) <= MAX_REVIEW
        d["tool_input"], d["truncated"] = (raw if fits else p["tool_input"]), not fits
    return d


class Permissions:
    """Permission requests and questions waiting on the user: part of Store."""

    # ------------------------------------------------------------ permissions
    def _pending(self, name):
        return [p for p in self.perms.values() if p["session"] == name and p["status"] == "pending"]

    def open_permission(self, name, payload):
        name = self._canon(name)
        pid = uuid.uuid4().hex
        with self.lock:
            raw = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            p = {"id": pid, "session": name, "claude_session_id": payload.get("session_id"),
                 "tool_name": str(payload.get("tool_name", "?")), "tool_input": core.clip(raw),
                 "created": core.now(), "expires": core.now() + PERM_TIMEOUT, "status": "pending",
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
            if core.now() > p["expires"]:
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
            if core.now() > p["expires"]:
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

    def permissions(self):
        with self.lock:
            return [_public(p) for p in self.perms.values() if p["status"] in ("pending", "deciding")]
