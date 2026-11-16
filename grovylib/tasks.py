"""Background work (subagents, background commands) a session left running."""
import re

from . import core


MACHINE_PROMPT = re.compile(r"\s*<(agent-message|task-notification)\b")
# The background task a machine prompt reports on: a subagent's report, or a task's notification (it has finished).
MACHINE_TASK = re.compile(r'\s*<agent-message from="([\w-]{1,64})"|\s*<task-notification>\s*<task-id>([\w-]{1,64})</task-id>')
TASK_EVENTS = ("SubagentStart", "SubagentStop")
MAX_TASKS = 50


def _count(v):
    if isinstance(v, (list, dict)):
        return len(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return 0


class Tasks:
    """Background work a session left running (subagents, background commands): part of Store."""

    # ------------------------------------------------------------ background work
    # s["tasks"] (id -> task) mirrors Claude Code's own list of background shells and subagents. Every Stop and
    # SubagentStop carries that whole list and replaces ours; between them, launches, SubagentStart and finish
    # notices keep it current. A SubagentStop alone doesn't end a subagent: it also fires when one pauses for its
    # own background work (verified), so only Claude Code's list, or the subagent's report, says it is done.
    def _task_snapshot(self, s, tasks, t):
        """Replace the list with Claude Code's (Stop, SubagentStop); keeps when we first saw each and what it did.
        Returns the new list. Anything but a list (an older Claude Code sent a count) clears it, keeping the count."""
        old, new = s.get("tasks") or {}, {}
        for i, x in enumerate(tasks if isinstance(tasks, list) else []):
            x = x if isinstance(x, dict) else {}   # anything listed counts, even malformed: never hide running work
            tid = str(x["id"])[:64] if isinstance(x.get("id"), (str, int)) and str(x["id"]) else f"#{i}"
            if len(new) < MAX_TASKS:
                new[tid] = {**old.get(tid, {"since": t, "last": None, "tools": 0}), "id": tid,
                            "type": str(x.get("type") or "task")[:20], "status": str(x.get("status") or "")[:20],
                            "description": " ".join(str(x.get("description") or "").split())[:120],
                            "agent_type": str(x.get("agent_type") or "")[:60] or None,
                            "command": " ".join(str(x.get("command") or "").split())[:200] or None}
        s["tasks"] = new
        s["background"] = _count(tasks)
        return list(new.values())

    def _add_task(self, s, tid, t, **fields):
        tasks = s.setdefault("tasks", {})
        if tid not in tasks and len(tasks) >= MAX_TASKS:
            return
        tasks[tid] = {**tasks.get(tid, {"id": tid, "since": t, "last": None, "tools": 0, "status": "running",
                                        "command": None, "agent_type": None, "description": ""}),
                      **{k: v for k, v in fields.items() if v}}
        s["background"] = len(tasks)

    def _drop_task(self, s, tid):
        if (s.get("tasks") or {}).pop(tid, None) is not None:
            s["background"] = len(s["tasks"])

    def _task_launched(self, s, payload, t):
        """A PostToolUse that started background work: a shell (backgroundTaskId) or an async subagent."""
        resp, inp = payload.get("tool_response"), payload.get("tool_input")
        resp, inp = resp if isinstance(resp, dict) else {}, inp if isinstance(inp, dict) else {}
        bid = resp.get("backgroundTaskId")
        if isinstance(bid, str) and bid:
            self._add_task(s, bid[:64], t, type="shell",
                           description=" ".join(str(inp.get("description") or "").split())[:120],
                           command=" ".join(str(inp.get("command") or "").split())[:200] or None)
        aid = resp.get("agentId")
        if isinstance(aid, str) and aid and resp.get("isAsync") is True:
            self._add_task(s, aid[:64], t, type="subagent",
                           description=" ".join(str(resp.get("description") or inp.get("description") or "").split())[:120],
                           agent_type=str(inp.get("subagent_type") or "")[:60])

    def _track_tasks(self, s, ev, payload, t):
        aid = payload.get("agent_id")
        aid = aid[:64] if isinstance(aid, str) and aid else None
        if ev == "SubagentStart" and aid and payload.get("agent_type"):   # (unnamed internal agents are skipped)
            self._add_task(s, aid, t, type="subagent", status="running", agent_type=str(payload["agent_type"])[:60])
        elif ev == "SubagentStop" and "background_tasks" in payload:
            self._task_snapshot(s, payload.get("background_tasks"), t)
        elif ev == "PostToolUse" and aid:
            task = (s.get("tasks") or {}).get(aid)
            if task is not None:   # never re-adds one Claude Code's list has dropped (a late PostToolUse)
                task["last"] = core.tool_summary(payload.get("tool_name"), payload.get("tool_input"))
                task["tools"] = task.get("tools", 0) + 1
                task["active"] = t
            self._task_launched(s, payload, t)   # a subagent's own background shell
