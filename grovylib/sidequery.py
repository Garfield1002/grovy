"""Side questions: a grovy-native rebuild of Claude Code's /btw.

/btw answers a question about the current session from its conversation, without adding the question or the answer
to the history. But it is interactive-only and its answer lives in an in-memory terminal overlay that never reaches
a hook or the transcript, so grovy (which only sees hooks and the transcript) can't surface it. This stands in for
it: grovy copies the conversation's JSONL, resumes the *copy* with a throwaway `claude -p`, and reads the answer
from that process's stdout. The live session is never resumed, typed into, or changed, and the real transcript is
left byte-for-byte alone (verified: the copy gets its own cwd and session id, and lives under its own throwaway
project dir that is deleted afterwards). Like /btw, it answers from context with no tools, and grovy keeps the Q&A
only in memory (off the record).
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid

from . import claude, core

SYSTEM = "Answer only from the conversation so far. Do not use any tools. Be concise."
TIMEOUT = 120          # seconds for the throwaway claude to answer
MAX_QUESTION = 4000    # characters accepted (like /goal's condition)
MAX_ANSWER = 20000     # characters kept from the answer
KEEP = 20              # side questions kept per session (in memory only)


def _projects_root():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(cfg, "projects")


def _copy_transcript(src, cwd, sid, dst):
    """Copy the conversation JSONL to dst, re-pointing it at the throwaway cwd and session id so that
    `claude --resume` accepts it there. Bad lines are dropped, never guessed."""
    out = []
    with open(src, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                if "cwd" in obj:
                    obj["cwd"] = cwd
                if obj.get("sessionId"):
                    obj["sessionId"] = sid
            out.append(json.dumps(obj, ensure_ascii=False))
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + ("\n" if out else ""))


def run(transcript, question, timeout=TIMEOUT):
    """Answer `question` from the conversation in `transcript` (a JSONL path), returning the answer text.
    Blocking (it spawns claude): call off the lock, on a worker thread. Raises RuntimeError on any failure."""
    if not transcript or not os.path.isfile(transcript):
        raise RuntimeError("no conversation to ask about yet")
    base = os.path.realpath(tempfile.mkdtemp(prefix="grovy-side-"))   # throwaway cwd (realpath: matches what claude sees)
    sid = str(uuid.uuid4())   # a fresh session id, so the live session is never resumed or forked
    proj = os.path.join(_projects_root(), base.replace("/", "-"))
    try:
        os.makedirs(proj, exist_ok=True)
        try:
            _copy_transcript(transcript, base, sid, os.path.join(proj, sid + ".jsonl"))
        except OSError as e:
            raise RuntimeError(f"couldn't read the conversation: {e}")
        cmd = [claude.claude_cmd(), "-p", "--resume", sid,
               "--allowedTools", "GrovySideNoop",   # a tool that does not exist: no real tool is ever allowed
               "--append-system-prompt", SYSTEM]
        try:
            # The question goes on stdin, never argv, so a question starting with "-" (e.g. "--help",
            # "--dangerously-skip-permissions") can't be read as a flag. Verified: stdin is the prompt under --resume.
            r = subprocess.run(cmd, cwd=base, input=question, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"timed out after {timeout}s")
        except OSError as e:
            raise RuntimeError(f"couldn't run claude: {e}")
        if r.returncode != 0:
            tail = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
            raise RuntimeError(tail[-1][:300] if tail else f"claude exited {r.returncode}")
        answer = (r.stdout or "").strip()
        if not answer:
            raise RuntimeError("no answer came back")
        return answer[:MAX_ANSWER]
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(proj, ignore_errors=True)


class SideQueries:
    """Side questions: part of Store. The answer is produced by a throwaway claude over a copy of the conversation
    (the injectable `self.side_run`, default `run`); the live session is never touched. Kept in memory only, like
    the /btw it replaces, and shown in the UI while the daemon runs."""

    def ask_side(self, name, question):
        if not isinstance(question, str) or not question.strip():
            return 400, "empty question"
        if len(question) > MAX_QUESTION:
            return 413, "question too long"
        question = question.strip()
        with self.lock:
            s = self.sessions.get(name)
            if not s:
                return 404, "unknown session"
            if s["state"] == "exited":
                return 409, "the session has exited"
            transcript = s.get("transcript")
            if not transcript:
                return 409, "nothing to ask about yet — wait for Claude's first reply"
            q = {"id": uuid.uuid4().hex, "session": name, "question": question, "status": "pending",
                 "answer": None, "error": None, "created": core.now(), "updated": core.now()}
            self.sidequeries.append(q)
            self._trim_side(name)
        self.publish(name)
        threading.Thread(target=self._run_side, args=(name, q["id"], transcript, question), daemon=True).start()
        return 200, q

    def _run_side(self, name, qid, transcript, question):
        try:
            answer, error = self.side_run(transcript, question), None
        except Exception as e:   # the runner never kills the daemon; the question just fails
            answer, error = None, str(e)
        with self.lock:
            for q in self.sidequeries:
                if q["id"] == qid and q["status"] == "pending":
                    q["status"] = "done" if answer is not None else "failed"
                    q["answer"], q["error"], q["updated"] = answer, error, core.now()
                    break
            else:
                return
        self.publish(name)

    def _trim_side(self, name, keep=KEEP):
        mine = [q for q in self.sidequeries if q["session"] == name]
        for q in mine[:-keep]:
            self.sidequeries.remove(q)

    def side_for(self, name, limit=KEEP):
        return [q for q in self.sidequeries if q["session"] == name][-limit:]
