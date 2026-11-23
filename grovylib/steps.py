"""A turn's steps, read live from Claude Code's own transcript: what Claude writes between tool calls, and the calls.

Hooks carry only the final answer (Stop.last_assistant_message). The rest of a turn is in the conversation's JSONL
(`transcript_path` in every hook payload), where Claude Code appends one record per finished content block. From
UserPromptSubmit to Stop, s["tx"] = {"path", "off", "n", "more", "skip"} follows that file: `read_steps` reads the
new complete lines and records each `text` / `tool_use` block of the main conversation as a `Step` event. Thinking,
tool results and subagents (their own files) are left out. Unknown or unreadable content is skipped, never guessed.
"""
import json
import os
import sys

from . import core

STEP_TEXT = 4000      # characters kept per text block
TURN_STEPS = 100      # steps recorded per turn; beyond that only counted
READ_MAX = 4 << 20    # bytes read per call (a longer line, e.g. a huge Write, is skipped whole)


def blocks(line):
    """The steps one JSONL line holds: [{"kind": "text", "text"} | {"kind": "tool", "tool", "arg"}]."""
    try:
        r = json.loads(line)
    except ValueError:
        return []
    if not isinstance(r, dict) or r.get("type") != "assistant" or r.get("isSidechain"):
        return []
    content = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
    out = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
            out.append({"kind": "text", "text": core.clip(b["text"].strip(), STEP_TEXT)})
        elif b.get("type") == "tool_use":
            out.append({"kind": "tool", **core.tool_summary(b.get("name"), b.get("input"))})
    return out


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0   # not written yet (a new conversation): everything in it will be new


def _path(payload):
    p = payload.get("transcript_path")
    return p if isinstance(p, str) and os.path.isabs(p) else None


class Steps:
    """Live steps of the current turn: part of Store."""

    def _steps_begin(self, s, payload):
        """UserPromptSubmit (under the lock). Text typed while Claude works fires it mid-turn: the turn goes on."""
        path, tx = _path(payload), s.get("tx")
        if not path:
            s.pop("tx", None)
            return
        if tx and tx.get("path") == path:   # still exact: nothing in between was missed or will be repeated
            if s.get("state") != "working":
                tx["n"], tx["more"] = 0, 0
            return
        s["tx"] = {"path": path, "off": _size(path), "n": 0, "more": 0, "skip": False}

    def _steps_follow(self, s, payload):
        """Any hook of the main conversation: /clear or a resume can move it to another file mid-turn. The new
        file starts being read where it ends now, so nothing already shown is shown again."""
        path, tx = _path(payload), s.get("tx")
        if tx and path and tx.get("path") != path:
            s["tx"] = {**tx, "path": path, "off": _size(path), "skip": False}

    def _steps_end(self, s, data=None):
        tx = s.pop("tx", None)
        if tx and tx.get("more") and data is not None:
            data["steps_more"] = tx["more"]

    def read_steps(self, name):
        """Record the steps written since the last read. Safe to call from any thread, at any time."""
        with self.lock:
            s = self.sessions.get(name)
            tx = s and s.get("tx")
            if not tx or s.get("state") == "exited":
                return 0
            path, off, skip = tx["path"], tx["off"], tx.get("skip")
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                if f.tell() <= off:
                    return 0
                f.seek(off)
                chunk = f.read(READ_MAX)
        except OSError:
            return 0
        end = chunk.rfind(b"\n") + 1
        if not end:   # no complete line yet; or one line longer than READ_MAX: drop it to its end
            if len(chunk) < READ_MAX:
                return 0
            end, skip_next = len(chunk), True
        else:
            skip_next = False
        lines = chunk[:end].split(b"\n")
        if skip:
            lines = lines[1:]   # the rest of a line that was too long
        found = [b for line in lines if line.strip() for b in blocks(line)]
        with self.lock:
            if s.get("tx") is not tx or tx["off"] != off:   # another reader got there first, or the turn ended
                return 0
            tx["off"], tx["skip"] = off + end, skip_next
            n = 0
            for b in found:
                if tx["n"] < TURN_STEPS:
                    tx["n"] += 1
                    n += 1
                    self._record(name, "Step", b, s.get("claude_session_id"))
                else:
                    tx["more"] = tx.get("more", 0) + 1
            self._save_state()
        if n:
            self.publish(name)
        return n

    def tail_steps(self):
        """The daemon's tailer, about once a second: every session in a turn."""
        with self.lock:
            names = [n for n, s in self.sessions.items() if s.get("tx")]
        for name in names:
            try:
                self.read_steps(name)
            except Exception as e:   # one bad file must not stop the others
                print(f"grovyd: reading {name}'s transcript: {e}", file=sys.stderr)
