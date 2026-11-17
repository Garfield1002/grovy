"""The hook adapter: Claude Code runs `grovy hook NAME` for every hook event (and `grovy statusline`)."""
import fnmatch
import json
import os
import subprocess
import sys

from . import claude, client, core, sessions


STATUS_FIELDS = ("model", "effort", "fast_mode", "context_window", "cwd", "session_id")


def cmd_statusline(name):
    """Called by Claude Code (often) with its status JSON on stdin. Forward a trimmed copy to the daemon,
    then print the user's own status line for the same input. Never fails loudly."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if core.valid_name(name) and isinstance(data, dict):
        try:
            client.sock_request({"type": "statusline", "session": name,
                                 "data": {k: data.get(k) for k in STATUS_FIELDS}}, timeout=0.3, wait_reply=False)
        except OSError:
            pass
    reg = sessions.registry() if core.valid_name(name) else {}
    project = (reg.get(sessions.canonical_name(reg, name)) or {}).get("cwd") if core.valid_name(name) else None
    own = claude.user_statusline(project)
    if own:
        cwd = data.get("cwd") if isinstance(data, dict) and isinstance(data.get("cwd"), str) else None
        try:
            r = subprocess.run(["/bin/sh", "-c", own["command"]], input=raw, capture_output=True, text=True,
                               timeout=5, cwd=cwd if cwd and os.path.isdir(cwd) else None)
            sys.stdout.write(r.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


def permission_output(behavior, message=None, updated_input=None):
    decision = {"behavior": behavior}
    if behavior == "allow" and isinstance(updated_input, dict):
        decision["updatedInput"] = updated_input  # e.g. AskUserQuestion answers from the phone
    if behavior == "deny":
        decision["message"] = message or "Denied from the grovy web interface."
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def plugins_watch(payload):
    """Whether a daemon plugin wants to hear about the file this PostToolUse edited: its path matches a glob grovyd
    published (core.edit_watch_path). Read on each edit, so it follows a daemon restart."""
    inp = payload.get("tool_input")
    if payload.get("tool_name") not in claude.EDIT_TOOLS or not isinstance(inp, dict):
        return False
    path = inp.get("file_path") or inp.get("notebook_path")
    if not isinstance(path, str) or not os.path.isabs(path):   # the daemon ignores those too
        return False
    path = os.path.normpath(path)
    try:
        with open(core.edit_watch_path()) as f:
            globs = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(globs, list) and any(isinstance(g, str) and fnmatch.fnmatchcase(path, g) for g in globs)


def cmd_hook(name):
    """Read one hook event from stdin. stdout is reserved for protocol output; diagnostics -> stderr.

    Every failure path exits 0 with no output, which means "no opinion": Claude carries on
    normally and, for PermissionRequest, shows its usual terminal dialog.
    """
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except ValueError as e:
        print(f"grovy hook: malformed input: {e}", file=sys.stderr)
        return 0
    if not core.valid_name(name):
        print(f"grovy hook: invalid session name {name!r}", file=sys.stderr)
        return 0
    event = payload.get("hook_event_name")
    if event not in claude.HOOK_EVENTS:
        return 0
    if payload.get("agent_id") and event not in claude.SUBAGENT_EVENTS and core.env("SUBAGENT_HOOKS", "0") != "1":
        # Fired inside a subagent (verified: such hooks carry agent_id/agent_type, and the parent's session_id).
        return 0
    if payload.get("agent_id") and event == "PostToolUse":
        # A subagent's tool call: only "what is it doing" for the background list, never session state (a
        # background subagent's PostToolUse can arrive after the parent's Stop). The bulky output isn't needed.
        resp = payload.get("tool_response")
        bid = resp.get("backgroundTaskId") if isinstance(resp, dict) else None
        payload = {**payload, "tool_response": {"backgroundTaskId": bid} if isinstance(bid, str) else None}
    if event == "SessionStart" and isinstance(payload.get("session_id"), str):
        # Recorded here, not only in the daemon, so `grovy resume` works even if grovyd was down.
        try:
            with sessions.registry_locked() as reg:
                canon = sessions.canonical_name(reg, name)  # a renamed session's Claude still reports its old name
                if canon in reg:
                    reg[canon]["claude_session_id"] = payload["session_id"]
        except OSError as e:
            print(f"grovy hook: registry update failed: {e}", file=sys.stderr)
    msg = {"type": "hook", "session": name, "event": payload, "pid": os.getppid()}
    if event == "PostToolUse" and (not payload.get("agent_id") or plugins_watch(payload)):
        # Steering: messages sent from the phone while Claude works come back here and are handed to Claude, with
        # what daemon plugins say about an edit. A subagent waits only for an edit some plugin watches.
        want = {"want_note": True} if payload.get("agent_id") else {"want_steer": True}
        try:
            reply = client.sock_request({**msg, **want}, timeout=3.0)
        except (OSError, ValueError) as e:
            print(f"grovy hook: daemon unreachable ({e}); event not recorded", file=sys.stderr)
            return 0
        reply = reply if isinstance(reply, dict) else {}
        said = [t for t in (reply.get("steer"), reply.get("note")) if isinstance(t, str) and t]
        if said:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                                "additionalContext": "\n\n".join(said)}}))
            sys.stdout.flush()
        return 0
    if event == "SessionStart":   # also after /clear and /compact
        # The daemon answers with what plugins add (their deadline is shorter than ours); without it, Claude still
        # gets the rest.
        added = None
        try:
            reply = client.sock_request({**msg, "want_context": True}, timeout=3.0)
            added = reply.get("context") if isinstance(reply, dict) else None
        except (OSError, ValueError) as e:
            print(f"grovy hook: no plugin context ({e})", file=sys.stderr)
        context = "\n\n".join(filter(None, [claude.widget_guide() if core.env("WIDGETS", "1") != "0" else "",
                                             added if isinstance(added, str) else "", claude.user_instructions()]))
        if context:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                "additionalContext": context}}))
            sys.stdout.flush()
        return 0
    if event != "PermissionRequest":
        try:
            client.sock_request(msg, timeout=2.0, wait_reply=False)
        except OSError as e:
            print(f"grovy hook: daemon unreachable ({e}); event not recorded", file=sys.stderr)
        return 0
    try:
        reply = client.sock_request(msg, timeout=core.PERMISSION_TIMEOUT + 10)
    except (OSError, ValueError) as e:
        print(f"grovy hook: no remote decision ({e}); falling back to terminal", file=sys.stderr)
        return 0
    behavior = (reply or {}).get("decision")
    if behavior in ("allow", "deny"):
        sys.stdout.write(json.dumps(permission_output(behavior, reply.get("message"), reply.get("updated_input"))))
        sys.stdout.flush()
    return 0
