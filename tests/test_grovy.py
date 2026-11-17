"""Unit + integration tests. Run: python3 -m unittest discover -s tests -v

No API key needed: integration tests use a fake `claude` (a shell loop) inside real tmux,
on a private tmux server socket (-L) so your own tmux sessions are never touched.
"""
import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="grovy-test-", dir="/tmp")  # short path: AF_UNIX limit
os.environ["GROVY_STATE_DIR"] = os.path.join(TMP, "state")
# Never let a test reach the real ~/.local/state (default_state_dir() migrates ~/.local/state/ccs on first use).
os.environ["XDG_STATE_HOME"] = os.path.join(TMP, "xdg")
os.environ.pop("CCS_STATE_DIR", None)
# Nor the user's own custom.css / custom.js / instructions.md.
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "xdg-config")
for _k in ("GROVY_CONFIG_DIR", "CCS_CONFIG_DIR"):
    os.environ.pop(_k, None)
import grovy  # noqa: E402
import grovyd  # noqa: E402
from grovylib import plugins, claude, client, commands, core, hook, install, live, manage, oom, pages, perms, projects, prompts, scriptlib, sessions, store, tmuxctl, uploads  # noqa: E402


def ev(name, **kw):
    return {"hook_event_name": name, "session_id": kw.pop("sid", "S1"), **kw}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.live = {"a": "%1", "b": "%2"}
        self.pasted = []
        self.store = store.Store(self.dir, tmux_live=lambda: dict(self.live),
                                paste=lambda n, t, *a, **k: self.pasted.append((n, t)))
        self.reg = mock.patch.object(sessions, "registry", lambda: {"a": {"cwd": "/a"}, "b": {"cwd": "/b"}})
        self.reg.start()
        self.store.reconcile()

    def tearDown(self):
        self.reg.stop()

    def wait_pasted(self, n):
        for _ in range(100):
            if len(self.pasted) >= n:
                return
            time.sleep(0.01)


class TestValidation(unittest.TestCase):
    def test_names(self):
        for good in ("a", "api", "my-proj_2", "A" * 32):
            self.assertTrue(core.valid_name(good), good)
        for bad in ("", "-x", "a.b", "a:b", "a b", "../x", "A" * 33, "é", None, 3, "a\n"):
            self.assertFalse(core.valid_name(bad), bad)

    def test_permission_output(self):
        self.assertEqual(hook.permission_output("allow"),
                         {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                 "decision": {"behavior": "allow"}}})
        d = hook.permission_output("deny")["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "deny")
        self.assertTrue(d["message"])

    def test_typed_mode(self):
        self.assertTrue(tmuxctl.typed_mode("hello\nworld"))
        self.assertFalse(tmuxctl.typed_mode("/compact"))
        self.assertFalse(tmuxctl.typed_mode("  !rm -rf"))
        self.assertFalse(tmuxctl.typed_mode("x" * (tmuxctl.TYPE_MAX + 1)))

    def test_norm(self):
        self.assertEqual(core.norm("a\r\nb  \n"), "a\nb")

    def test_hook_settings_file(self):
        hooks = json.load(open(claude.hook_settings("x")))["hooks"]
        self.assertEqual(set(hooks), set(claude.HOOK_EVENTS))
        self.assertGreater(hooks["PermissionRequest"][0]["hooks"][0]["timeout"], core.PERMISSION_TIMEOUT)
        self.assertTrue(hooks["Stop"][0]["hooks"][0]["command"].endswith("'hook' 'x'"))


class TestAttachmentsDir(unittest.TestCase):
    def test_sessions_may_read_attachments(self):
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": os.path.join(TMP, "sd1"), "TMPDIR": "/tmp"}):
            d = core.attachments_dir()
            self.assertTrue(d.startswith(f"/tmp/grovy-{os.getuid()}-"))   # a scratch daemon gets its own
            self.assertEqual(claude.session_settings("x")["permissions"]["additionalDirectories"], [d])
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": os.path.join(TMP, "sd2")}):
            self.assertNotEqual(core.attachments_dir(), d)


class TestRenameMigration(unittest.TestCase):
    def test_state_dir_moves_once_and_old_path_keeps_working(self):
        base = tempfile.mkdtemp(dir=TMP)
        old = os.path.join(base, "ccs")
        os.makedirs(old)
        with open(os.path.join(old, "token"), "w") as f:
            f.write("t0k\n")
        env = {"XDG_STATE_HOME": base}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("GROVY_STATE_DIR", None)
            new = core.default_state_dir()
            self.assertEqual(new, os.path.join(base, "grovy"))
            self.assertTrue(os.path.islink(old))
            self.assertEqual(open(os.path.join(old, "token")).read(), "t0k\n")   # old path still resolves
            self.assertEqual(core.default_state_dir(), new)                     # idempotent

    def test_env_falls_back_to_pre_rename_names(self):
        with mock.patch.dict(os.environ, {"CCS_PORT": "1"}):
            os.environ.pop("GROVY_PORT", None)
            self.assertEqual(core.env("PORT"), "1")
            with mock.patch.dict(os.environ, {"GROVY_PORT": "2"}):
                self.assertEqual(core.env("PORT"), "2")


class TestHookAdapter(unittest.TestCase):
    """cmd_hook must never print anything except valid decision JSON, and never fail Claude."""

    def run_hook(self, stdin, name="x", reply=None, exc=None):
        out = io.StringIO()
        def fake(msg, timeout=2.0, wait_reply=True):
            if exc:
                raise exc
            return reply
        with mock.patch.object(client, "sock_request", fake), mock.patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
            rc = hook.cmd_hook(name)
        return rc, out.getvalue()

    def test_malformed_and_unknown(self):
        self.assertEqual(self.run_hook("not json"), (0, ""))
        self.assertEqual(self.run_hook("[1]"), (0, ""))
        self.assertEqual(self.run_hook(json.dumps(ev("Bogus"))), (0, ""))
        self.assertEqual(self.run_hook(json.dumps(ev("Stop")), name="bad name"), (0, ""))

    def test_session_start_adds_widget_guide(self):
        for src in ("startup", "resume", "clear", "compact"):
            rc, out = self.run_hook(json.dumps(ev("SessionStart", source=src)), exc=OSError("daemon down"))
            ctx = json.loads(out)["hookSpecificOutput"]
            self.assertEqual(ctx["hookEventName"], "SessionStart")
            self.assertIn("```widget", ctx["additionalContext"])
            self.assertIn('"type":"chart"', ctx["additionalContext"])
        self.assertEqual(self.run_hook(json.dumps(ev("Stop")))[1], "")
        with mock.patch.dict(os.environ, {"GROVY_WIDGETS": "0"}):
            self.assertEqual(self.run_hook(json.dumps(ev("SessionStart", source="startup")))[1], "")

    def test_session_start_adds_plugin_context(self):
        """What the daemon's plugins add goes between grovy's guide and the user's own instructions."""
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "instructions.md"), "w") as f:
            f.write("MINE")
        start = json.dumps(ev("SessionStart", source="startup"))
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": d}):
            ctx = json.loads(self.run_hook(start, reply={"context": "SPEC"})[1])["hookSpecificOutput"]["additionalContext"]
            self.assertTrue(ctx.endswith("\n\nSPEC\n\nMINE"), ctx[-40:])
            self.assertIn("```widget", ctx)
            for bad in (None, {"context": 3}, {}):   # an older daemon, or nonsense: the rest still goes
                ctx = json.loads(self.run_hook(start, reply=bad)[1])["hookSpecificOutput"]["additionalContext"]
                self.assertTrue(ctx.endswith("```widget block it prints in your reply.") or ctx.endswith("\n\nMINE"))
                self.assertNotIn("SPEC", ctx)

    def test_post_tool_use_notes(self):
        """The main agent's PostToolUse always asks (steering); a subagent's only for an edit a plugin watches."""
        sent = []
        def fake(msg, timeout=2.0, wait_reply=True):
            sent.append((msg.get("want_steer"), msg.get("want_note"), wait_reply))
            return {"steer": "STEER" if msg.get("want_steer") else None, "note": "NOTE"} if wait_reply else None
        def run(payload):
            out = io.StringIO()
            with mock.patch.object(client, "sock_request", fake), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                    redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
                hook.cmd_hook("x")
            return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"] if out.getvalue() else None
        d = tempfile.mkdtemp(dir=TMP)
        edit = lambda path, **kw: ev("PostToolUse", tool_name="Edit", tool_input={"file_path": path}, **kw)
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": d}):
            self.assertEqual(run(edit("/p/a.md")), "STEER\n\nNOTE")
            self.assertIsNone(run(edit("/p/a.md", agent_id="sub")))   # no globs published: a subagent doesn't wait
            core.atomic_write_json(core.edit_watch_path(), ["*.md"])
            self.assertEqual(run(edit("/p/a.md", agent_id="sub")), "NOTE")
            self.assertIsNone(run(edit("/p/a.py", agent_id="sub")))
            self.assertIsNone(run(edit("a.md", agent_id="sub")))          # relative: the daemon would ignore it
            self.assertEqual(run(edit("/p/x/../b.md", agent_id="sub")), "NOTE")
            self.assertIsNone(run(ev("PostToolUse", tool_name="Read", tool_input={"file_path": "/p/a.md"}, agent_id="sub")))
        self.assertEqual(sent, [(True, None, True), (None, None, False), (None, True, True), (None, None, False),
                                (None, None, False), (None, True, True), (None, None, False)])
        for odd in ([1], "x", 3):   # a reply that isn't an object: nothing said, nothing broken
            with mock.patch.object(client, "sock_request", lambda *a, r=odd, **k: r), mock.patch("sys.stderr", io.StringIO()), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(edit("/p/a.md")))), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(hook.cmd_hook("x"), 0)
            self.assertEqual(out.getvalue(), "")
            ctx = json.loads(self.run_hook(json.dumps(ev("SessionStart", source="startup")), reply=odd)[1])
            self.assertIn("```widget", ctx["hookSpecificOutput"]["additionalContext"])

    def test_session_start_adds_user_instructions(self):
        d = tempfile.mkdtemp(dir=TMP)
        start = json.dumps(ev("SessionStart", source="clear"))
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": d}):
            with open(os.path.join(d, "instructions.md"), "w") as f:
                f.write("\nUse the `todo` widget for task lists.\n")
            ctx = json.loads(self.run_hook(start, exc=OSError("down"))[1])["hookSpecificOutput"]["additionalContext"]
            self.assertIn("```widget", ctx)
            self.assertTrue(ctx.endswith("\n\nUse the `todo` widget for task lists."))   # after grovy's own guide
            with mock.patch.dict(os.environ, {"GROVY_WIDGETS": "0"}):   # widgets off: the user's notes still go
                ctx = json.loads(self.run_hook(start, exc=OSError("down"))[1])["hookSpecificOutput"]["additionalContext"]
                self.assertEqual(ctx, "Use the `todo` widget for task lists.")
                with open(os.path.join(d, "instructions.md"), "w") as f:
                    f.write("x" * (claude.INSTRUCTIONS_MAX + 1))
                self.assertEqual(self.run_hook(start, exc=OSError("down"))[1], "")   # too big: ignored, nothing sent

    def test_subagent_events_stay_out_of_the_session(self):
        """Hooks fired inside a subagent carry agent_id (verified, Claude Code 2.1.325). A background subagent's
        PostToolUse arrived after the parent's Stop and showed an idle session as working: its tool calls now
        only feed the background list (never waited on, never steered) and the rest stays out."""
        sent = []
        def fake(msg, timeout=2.0, wait_reply=True):
            sent.append((msg["event"]["hook_event_name"], wait_reply, msg["event"].get("tool_response")))
            return {"decision": "allow", "steer": "hijack"} if wait_reply else None
        sub = {"agent_id": "ab4c", "agent_type": "general-purpose"}
        with mock.patch.object(client, "sock_request", fake), mock.patch("sys.stderr", io.StringIO()):
            for name in ("PostToolUse", "UserPromptSubmit", "Stop", "PermissionRequest", "Notification",
                         "SubagentStart", "SubagentStop"):
                out = io.StringIO()
                payload = ev(name, **sub, tool_response={"stdout": "x" * 9999, "backgroundTaskId": "b1"})
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
                    hook.cmd_hook("x")
                if name == "PostToolUse":
                    self.assertEqual(out.getvalue(), "")                    # a subagent is never steered
            self.assertEqual([x[0] for x in sent], ["PostToolUse", "PermissionRequest", "Notification",
                                                    "SubagentStart", "SubagentStop"])
            self.assertEqual(sent[0], ("PostToolUse", False, {"backgroundTaskId": "b1"}))   # output not shipped
            with mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse")))):
                hook.cmd_hook("x")                                        # the main agent's own tool calls
            self.assertEqual(sent[-1][:2], ("PostToolUse", True))           # wait for steering
            with mock.patch.dict(os.environ, {"GROVY_SUBAGENT_HOOKS": "1"}), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(ev("Stop", **sub)))):
                hook.cmd_hook("x")
        self.assertEqual(sent[-1][0], "Stop")

    def test_auto_mode_hints_in_session_settings(self):
        with open(claude.hook_settings("am")) as f:
            am = json.load(f)["autoMode"]
        self.assertEqual((am["environment"][0], am["allow"][0]), ("$defaults", "$defaults"))   # built-ins kept
        self.assertIn(claude.GROVY_BIN, am["environment"][1])
        self.assertEqual(set(am), {"environment", "allow"})   # never soft_deny/hard_deny: those would replace rules

    def test_daemon_down_permission_falls_back(self):
        rc, out = self.run_hook(json.dumps(ev("PermissionRequest", tool_name="Bash")), exc=OSError("nope"))
        self.assertEqual((rc, out), (0, ""))  # no output => Claude's terminal dialog

    def test_permission_decisions(self):
        for d in ("allow", "deny"):
            rc, out = self.run_hook(json.dumps(ev("PermissionRequest")), reply={"decision": d})
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"]["behavior"], d)
        for reply in ({"decision": None}, {"decision": "yes please"}, None):
            self.assertEqual(self.run_hook(json.dumps(ev("PermissionRequest")), reply=reply), (0, ""))


class TestState(StoreCase):
    def test_transitions(self):
        st = lambda: self.store.sessions["a"]["state"]
        self.assertEqual(st(), "starting")
        self.store.ingest("a", ev("SessionStart", source="startup"))
        self.assertEqual(st(), "idle")
        self.assertEqual(self.store.sessions["a"]["claude_session_id"], "S1")
        self.store.ingest("a", ev("UserPromptSubmit", prompt="hi"))
        self.assertEqual(st(), "working")
        self.store.ingest("a", ev("Stop", last_assistant_message="done", background_tasks=[{"id": 1}]))
        self.assertEqual(st(), "idle")
        self.assertEqual(self.store.sessions["a"]["background"], 1)
        self.store.ingest("a", ev("UserPromptSubmit", prompt="again"))
        self.store.ingest("a", ev("StopFailure", error="rate_limit"))
        self.assertEqual(st(), "error")
        self.store.ingest("a", ev("SessionStart", sid="S2", source="clear"))
        self.assertEqual(self.store.sessions["a"]["claude_session_id"], "S2")
        self.store.ingest("a", ev("SessionEnd", reason="other"))
        self.assertEqual(st(), "exited")

    def test_injected_agent_prompt_is_not_shown_as_the_user(self):
        self.store.ingest("a", ev("UserPromptSubmit", prompt='<agent-message from="x">\nreport</agent-message>'))
        self.assertEqual(self.store.sessions["a"]["activity"], "subagent report")
        self.store.ingest("a", ev("UserPromptSubmit", prompt="<task-notification>\n<status>done</status>"))
        self.assertEqual(self.store.sessions["a"]["activity"], "task notification")

    def test_terminal_only_dialog(self):
        self.store.ingest("a", ev("Notification", notification_type="permission_prompt", message="x"))
        self.assertEqual(self.store.sessions["a"]["state"], "needs_input")

    def test_duplicate_event_ignored(self):
        e = ev("Stop", last_assistant_message="x")
        self.assertTrue(self.store.ingest("a", e))
        self.assertFalse(self.store.ingest("a", dict(e)))
        self.assertEqual(sum(r["type"] == "Stop" for r in self.store.events["a"]), 1)

    def test_tmux_gone_means_exited(self):
        self.store.ingest("a", ev("SessionStart"))
        del self.live["a"]
        self.store.reconcile()
        self.assertEqual(self.store.sessions["a"]["state"], "exited")

    def test_sessions_isolated_and_sorted(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("b", ev("Notification", notification_type="permission_prompt", sid="T"))
        names = [s["name"] for s in self.store.list_sessions()]
        self.assertEqual(names[0], "b")  # needs attention first
        self.assertEqual(self.store.sessions["a"]["state"], "idle")

    def test_large_fields_clipped(self):
        self.store.ingest("a", ev("Stop", last_assistant_message="x" * 50000))
        self.assertLess(len(self.store.events["a"][-1]["data"]["last_assistant_message"]), 21000)


class TestEventLog(unittest.TestCase):
    def test_recovers_from_truncated_and_corrupt_lines(self):
        d = tempfile.mkdtemp(dir=TMP)
        good = {"id": 5, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "Stop", "data": {}}
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps(good) + "\nnot json\n" + '{"id": 6, "ts": 1, "ccs": "a", "ty')
        with mock.patch.object(sessions, "registry", lambda: {}):
            s = store.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
            self.assertEqual(len(s.events["a"]), 1)
            s.ingest("a", ev("Stop", last_assistant_message="after"))
            s2 = store.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual([r["id"] for r in s2.events["a"]], [5, 6])

    def test_restart_reconfirms_wrapped_prompts(self):
        d = tempfile.mkdtemp(dir=TMP)
        rec = {"id": 1, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "UserPromptSubmit",
               "data": {"prompt": '<pasted_content id="x">\nhello\n\n\nworld\n</pasted_content id="x">'}}
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps(rec) + "\n")
        core.atomic_write_json(os.path.join(d, "prompts.json"), [
            {"id": "q" * 32, "ccs": "a", "text": "hello\nworld", "status": "unconfirmed", "created": 1, "updated": 1, "error": None}])
        s = store.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual(s.prompts[0]["status"], "submitted")

    def test_restart_mid_permission_and_injection(self):
        d = tempfile.mkdtemp(dir=TMP)
        core.atomic_write_json(os.path.join(d, "state.json"), {"sessions": {"a": {
            "name": "a", "state": "needs_input", "cwd": "/", "claude_session_id": "S", "last_event": 1,
            "activity": "", "last_error": None, "background": 0}}})
        core.atomic_write_json(os.path.join(d, "prompts.json"), [
            {"id": "p" * 32, "ccs": "a", "text": "hi", "status": "injected", "created": 1, "updated": 1, "error": None}])
        s = store.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual(s.sessions["a"]["state"], "needs_input")  # Claude fell back to its terminal dialog
        self.assertEqual(s.prompts[0]["status"], "unconfirmed")  # never re-sent

    def test_force_consent_does_not_survive(self):
        """Regression: a force-queued prompt was typed into a terminal permission dialog after restart."""
        d = tempfile.mkdtemp(dir=TMP)
        pasted = []
        with mock.patch.object(sessions, "registry", lambda: {"a": {"cwd": "/"}}):
            s = store.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda n, t, *a, **k: pasted.append(t))
            s.ingest("a", ev("SessionStart"))
            s.ingest("a", ev("UserPromptSubmit", prompt="busy"))           # working, fresh
            s.enqueue("a", "later", force=True)                            # can't type now
            self.assertFalse(s.prompts[-1]["force"])
            s.sessions["a"]["state"] = "unknown"
            s.try_deliver("a")
            s.open_permission("a", ev("PermissionRequest", tool_name="Bash"))
            s2 = store.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda n, t, *a, **k: pasted.append(t))
            s2.reconcile()
            time.sleep(0.1)
        self.assertEqual(pasted, [])
        self.assertEqual(s2.sessions["a"]["state"], "needs_input")


class TestPermissions(StoreCase):
    def open_and_wait(self, payload):
        """Simulate the socket handler: returns (perm, thread, result holder)."""
        a, b = socket.socketpair()
        p = self.store.open_permission("a", payload)
        res = {}
        def run():
            res["reply"] = self.store.wait_permission(p, b)
            self.store.complete_permission(p, delivered=True)
        t = threading.Thread(target=run)
        t.start()
        return p, t, res, a

    def test_decision_once(self):
        p, t, res, peer = self.open_and_wait(ev("PermissionRequest", tool_name="Bash",
                                                tool_input={"command": "ls"}))
        self.assertEqual(self.store.sessions["a"]["state"], "needs_input")
        self.assertEqual(self.store.decide(p["id"], "maybe")[0], 400)
        self.assertEqual(self.store.decide("0" * 32, "allow")[0], 404)
        self.assertEqual(self.store.decide(p["id"], "deny")[0], 200)
        self.assertEqual(self.store.decide(p["id"], "allow")[0], 409)
        t.join(5)
        self.assertEqual(res["reply"], {"decision": "deny"})
        self.assertEqual(p["status"], "delivered")
        self.assertEqual(self.store.sessions["a"]["state"], "working")
        peer.close()

    def test_concurrent_decisions_single_winner(self):
        p, t, res, peer = self.open_and_wait(ev("PermissionRequest", tool_name="Edit"))
        codes, barrier = [], threading.Barrier(8)
        def go(i):
            barrier.wait()
            codes.append(self.store.decide(p["id"], "allow" if i % 2 else "deny")[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [x.start() for x in ts]
        [x.join() for x in ts]
        t.join(5)
        self.assertEqual(sorted(codes), [200] + [409] * 7)
        self.assertIn(res["reply"]["decision"], ("allow", "deny"))
        peer.close()

    def test_hook_gone_expires(self):
        p, t, res, peer = self.open_and_wait(ev("PermissionRequest", tool_name="Bash"))
        peer.close()  # Claude killed the hook (user answered in the terminal)
        t.join(5)
        self.assertEqual(res["reply"], {"decision": None})
        self.assertEqual(p["status"], "expired")
        self.assertEqual(self.store.decide(p["id"], "allow")[0], 409)

    def test_timeout(self):
        with mock.patch.object(perms, "PERM_TIMEOUT", 0.3):
            p, t, res, peer = self.open_and_wait(ev("PermissionRequest", tool_name="Bash"))
            t.join(5)
        self.assertEqual(res["reply"], {"decision": None})
        self.assertEqual(p["status"], "timeout")
        self.assertEqual(self.store.sessions["a"]["state"], "needs_input")  # now in the terminal
        peer.close()

    def test_session_exit_expires(self):
        p, t, res, peer = self.open_and_wait(ev("PermissionRequest", tool_name="Bash"))
        del self.live["a"]
        self.store.reconcile()
        t.join(5)
        self.assertEqual(res["reply"], {"decision": None})
        peer.close()


class TestQuestions(StoreCase):
    Q = {"questions": [{"question": "Pick a color", "header": "Color", "multiSelect": False,
                        "options": [{"label": "Red", "description": ""}, {"label": "Green", "description": ""}]}]}

    def start(self):
        a, b = socket.socketpair()
        p = self.store.open_permission("a", ev("PermissionRequest", tool_name="AskUserQuestion", tool_input=self.Q))
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.store.wait_permission(p, b)))
        t.start()
        return p, t, res, a

    def test_answer(self):
        p, t, res, peer = self.start()
        self.assertEqual(p["kind"], "question")
        self.assertIn("question: Color", self.store.sessions["a"]["activity"])
        self.assertEqual(self.store.decide(p["id"], "allow")[0], 400)                    # must answer
        self.assertEqual(self.store.decide(p["id"], "answer", {"Other?": "x"})[0], 400)  # wrong question
        self.assertEqual(self.store.decide(p["id"], "answer", {"Pick a color": " "})[0], 400)
        self.assertEqual(self.store.decide(p["id"], "answer", {"Pick a color": "Mauve"})[0], 200)
        self.assertEqual(self.store.decide(p["id"], "answer", {"Pick a color": "Red"})[0], 409)
        t.join(5)
        self.assertEqual(res["r"], {"decision": "allow",
                                    "updated_input": {**self.Q, "answers": {"Pick a color": "Mauve"}}})
        peer.close()

    def test_skip(self):
        p, t, res, peer = self.start()
        self.assertEqual(self.store.decide(p["id"], "deny")[0], 200)
        t.join(5)
        self.assertEqual(res["r"]["decision"], "deny")
        self.assertIn("skipped", res["r"]["message"])
        peer.close()

    def test_permissions_reject_answer(self):
        p = self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}))
        self.assertEqual(self.store.decide(p["id"], "answer", {"x": "y"})[0], 400)

    def test_hook_emits_updated_input(self):
        out = hook.permission_output("allow", None, {"questions": [], "answers": {"q": "a"}})
        self.assertEqual(out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"], {"q": "a"})


class TestCommands(StoreCase):
    def setUp(self):
        super().setUp()
        self.proj = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(self.proj, ".claude", "skills", "hello"))
        with open(os.path.join(self.proj, ".claude", "skills", "hello", "SKILL.md"), "w") as f:
            f.write("---\nname: hello\ndescription: says hi\n---\nbody\n")
        os.makedirs(os.path.join(self.proj, ".claude", "commands", "git"))
        with open(os.path.join(self.proj, ".claude", "commands", "git", "pr.md"), "w") as f:
            f.write("Open a pull request\n")
        self.store.sessions["a"]["cwd"] = self.proj
        self.store.ingest("a", ev("SessionStart"))
        self.store.sessions["a"]["cwd"] = self.proj
        commands._cmd_cache.clear()
        self.store.paste = lambda n, t, command=False, **k: self.pasted.append((n, t, command))

    def test_discovery(self):
        names = {c["name"]: c for c in commands.commands_for(self.proj)}
        self.assertIn("clear", names)
        self.assertEqual(names["hello"]["description"], "says hi")
        self.assertEqual(names["git:pr"]["description"], "Open a pull request")

    def test_allowlist(self):
        code, msg = self.store.enqueue("a", "/model opus")
        self.assertEqual(code, 400)
        self.assertIn("terminal", msg)
        self.assertEqual(self.store.enqueue("a", "/hello world")[0], 200)
        self.wait_pasted(1)
        self.assertEqual(self.pasted, [("a", "/hello world", True)])

    def test_leading_whitespace_does_not_bypass_the_allowlist(self):
        """Regression: Claude Code trims input, so a pasted " /model" or "\n/model" opened the menu."""
        for t in (" /model", "\n/model", "\t /config x", "/MODEL", "  !rm -rf ~", " /home/x is broken"):
            self.assertEqual(self.store.enqueue("a", t)[0], 400, t)
        self.assertEqual(self.store.enqueue("a", "  /hello world")[0], 200)
        self.wait_pasted(1)
        self.assertEqual(self.pasted[0][2], True)

    def test_reserved_names_case_insensitive(self):
        os.makedirs(os.path.join(self.proj, ".claude", "skills", "Settings"))
        with open(os.path.join(self.proj, ".claude", "skills", "Settings", "SKILL.md"), "w") as f:
            f.write("---\nname: Settings\n---\n")
        commands._cmd_cache.clear()
        self.assertNotIn("Settings", [c["name"] for c in commands.commands_for(self.proj)])

    def test_clear_confirms_on_session_start(self):
        self.store.enqueue("a", "/clear")
        self.wait_pasted(1)
        self.store.ingest("a", ev("SessionStart", sid="S2", source="clear"))
        self.assertEqual(self.store.prompts[-1]["status"], "submitted")


class TestLifecycle(StoreCase):
    def test_trusted_projects(self):
        home = tempfile.mkdtemp(dir=TMP)
        dev = os.path.join(home, "dev")
        for d in ("dev/known", "dev/newrepo/.git", "dev/okrepo/.git", "dev/notgit", "other/untrusted"):
            os.makedirs(os.path.join(home, d))
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"projects": {dev: {"hasTrustDialogAccepted": True},
                                    os.path.join(dev, "known"): {"hasTrustDialogAccepted": False},
                                    os.path.join(dev, "okrepo"): {"hasTrustDialogAccepted": True},
                                    os.path.join(home, "other", "untrusted"): {"hasTrustDialogAccepted": False}}}, f)
        with mock.patch.dict(os.environ, {"HOME": home, "TMPDIR": "/nonexistent-tmp"}):
            got = projects.trusted_projects()
        rp = os.path.realpath
        self.assertIn(rp(dev), got)
        self.assertIn(rp(os.path.join(dev, "known")), got)       # plain folder: inherits ~/dev's trust
        # A git repo is its own trust boundary: inside trusted ~/dev it still shows the trust dialog
        # (verified with Claude Code 2.1.325), so only an explicitly trusted repo may be offered.
        self.assertNotIn(rp(os.path.join(dev, "newrepo")), got)
        self.assertIn(rp(os.path.join(dev, "okrepo")), got)
        self.assertNotIn(rp(os.path.join(dev, "notgit")), got)
        self.assertNotIn(rp(os.path.join(home, "other", "untrusted")), got)

    def test_create_validation(self):
        started = []

        class Fake:
            trusted_projects = staticmethod(lambda: ["/ok"])
            start = staticmethod(lambda n, p, e: started.append((n, p, e)))

        self.store.lifecycle = Fake()
        self.assertEqual(self.store.create("bad name", "/ok", False)[0], 400)
        self.assertEqual(self.store.create("x", "/etc", False)[0], 400)      # not a listed folder
        self.assertEqual(self.store.create("x", "/ok/../etc", False)[0], 400)
        self.assertEqual(self.store.create("a", "/ok", False)[0], 409)        # name taken (registry)
        self.assertEqual(self.store.create("x", "/ok", True)[0], 200)
        self.assertEqual(started, [("x", "/ok", ["--permission-mode", "default"])])

    def test_history_and_search(self):
        self.store.ingest("a", ev("SessionStart"))
        for i in range(5):
            self.store.ingest("a", ev("UserPromptSubmit", prompt=f"prompt {i} about Tables"))
            self.store.ingest("a", ev("Stop", last_assistant_message=f"reply {i}"))
        self.store.ingest("b", ev("UserPromptSubmit", sid="T", prompt="tables in b"))
        ids = [r["id"] for r in self.store.events["a"]]
        older = self.store.history("a", before=ids[5], limit=3)
        self.assertEqual([r["id"] for r in older], ids[2:5])
        hits = self.store.history("a", q="TABLES")
        self.assertEqual([r["data"]["prompt"] for r in hits][:2], ["prompt 4 about Tables", "prompt 3 about Tables"])
        self.assertTrue(all(store._owner(r) == "a" for r in hits))


class TestReviewRegressions(StoreCase):
    """Findings from the adversarial review (correctness + security)."""

    def test_repeated_event_is_not_a_duplicate(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="continue"))
        self.store.ingest("a", ev("Stop", last_assistant_message="step 1 done"))
        with mock.patch.object(core, "now", lambda: time.time() + 5):     # the user types it again later
            self.store.ingest("a", ev("UserPromptSubmit", prompt="continue"))
        self.assertEqual(self.store.sessions["a"]["state"], "working")
        self.store.enqueue("a", "phone prompt")
        time.sleep(0.05)
        self.assertEqual(self.pasted, [])

    def test_forgotten_sessions_queued_prompt_never_reaches_namesake(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="busy"))
        self.store.enqueue("a", "rm the build dir")
        del self.live["a"]
        with mock.patch.object(sessions, "registry", lambda: {"b": {"cwd": "/b"}}):   # `grovy forget a`
            self.store.reconcile()
        self.assertEqual(self.store.prompts[-1]["status"], "failed")
        self.live["a"] = "%9"
        self.store.reconcile()
        self.store.ingest("a", ev("SessionStart", sid="NEW"))
        time.sleep(0.05)
        self.assertEqual(self.pasted, [])

    def test_newest_injected_prompt_gets_the_confirmation(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.prompts.append({"id": "o" * 32, "session": "a", "text": "run the tests", "status": "unconfirmed",
                                   "created": 1, "updated": 1, "error": None})
        self.store.enqueue("a", "run the tests")
        self.wait_pasted(1)
        self.store.ingest("a", ev("UserPromptSubmit", prompt="run the tests"))
        self.assertEqual([p["status"] for p in self.store.prompts], ["unconfirmed", "submitted"])

    def test_auto_compaction_mid_turn_keeps_working(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="big task"))
        self.store.enqueue("a", "next")
        self.store.ingest("a", ev("SessionStart", source="compact"))
        time.sleep(0.05)
        self.assertEqual(self.store.sessions["a"]["state"], "working")
        self.assertEqual(self.pasted, [])

    def test_clear_end_does_not_exit(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("SessionStart", sid="S2", source="clear"))   # arrives first
        self.store.ingest("a", ev("SessionEnd", reason="clear"))
        self.assertEqual(self.store.sessions["a"]["state"], "idle")

    def test_interrupt_holds_lock_across_keypress(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        opened = []

        def key(n, k):   # a permission request racing the key press must wait for the lock
            t = threading.Thread(target=lambda: opened.append(
                self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash"))))
            t.start(); t.join(0.2)
            self.assertEqual(opened, [])
        self.store.key = key
        self.assertEqual(self.store.interrupt("a")[0], 200)
        for _ in range(100):
            if opened:
                break
            time.sleep(0.01)
        self.assertEqual(self.store.sessions["a"]["state"], "needs_input")   # not overwritten

    def test_reserved_command_names(self):
        proj = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(proj, ".claude", "skills", "evil"))
        with open(os.path.join(proj, ".claude", "skills", "evil", "SKILL.md"), "w") as f:
            f.write("---\nname: model\n---\n")
        commands._cmd_cache.clear()
        names = [c["name"] for c in commands.commands_for(proj)]
        self.assertNotIn("model", names)

    def test_live_request_shows_full_input(self):
        big = {"command": "echo " + "x" * 6000, "edits": list(range(60))}
        p = self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash", tool_input=big))
        pub = perms._public(p)
        self.assertEqual(pub["tool_input"], big)
        self.assertFalse(pub["truncated"])
        self.assertIn("more items not shown", str(p["tool_input"]["edits"][-1]))   # log copy says so
        huge = self.store.open_permission("a", ev("PermissionRequest", tool_name="Write",
                                                  tool_input={"content": "y" * (perms.MAX_REVIEW + 10)}))
        self.assertTrue(perms._public(huge)["truncated"])


class TestSecondPassRegressions(StoreCase):
    def test_slow_phone_compact_is_not_unconfirmed(self):
        """A /compact from the phone took 87s; its receipt (SessionStart) only comes when it finishes."""
        self.store.paste = lambda n, t, command=False, **k: self.pasted.append((n, t))
        self.store.ingest("a", ev("SessionStart"))
        self.assertEqual(self.store.enqueue("a", "/compact")[0], 200)
        self.wait_pasted(1)
        self.assertEqual(self.store.sessions["a"]["activity"], "compacting conversation")
        later = time.time() + 90
        with mock.patch.object(core, "now", lambda: later):
            self.store.reconcile()
            self.assertEqual(self.store.prompts[0]["status"], "injected")
            self.assertEqual(self.store.sessions["a"]["state"], "working")
            self.store.ingest("a", ev("SessionStart", source="compact"))
        self.assertEqual(self.store.prompts[0]["status"], "submitted")
        self.assertEqual(self.store.sessions["a"]["state"], "idle")

    def test_stale_phone_compact_does_not_end_a_turn(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.prompts.append({"id": "c" * 32, "session": "a", "text": "/compact", "command": "compact",
                                   "status": "unconfirmed", "created": 1, "updated": 1, "error": None})
        self.store.ingest("a", ev("UserPromptSubmit", prompt="big task"))
        self.store.enqueue("a", "rm -rf build")
        self.store.ingest("a", ev("SessionStart", source="compact"))     # automatic, mid-turn
        time.sleep(0.05)
        self.assertEqual(self.store.sessions["a"]["state"], "working")
        self.assertEqual(self.pasted, [])

    def test_exit_fails_queued_prompts_before_a_namesake_starts(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="busy"))
        self.store.enqueue("a", "delete the old migrations")
        self.store.ingest("a", ev("SessionEnd", reason="prompt_input_exit"))
        self.store.reconcile()                                            # `grovy new a ~/other` refresh
        self.store.ingest("a", ev("SessionStart", sid="NEW"))
        time.sleep(0.05)
        self.assertEqual(self.pasted, [])
        self.assertEqual(self.store.prompts[-1]["status"], "failed")

    def test_hung_tmux_skips_reconcile_instead_of_freezing_or_guessing(self):
        self.store.ingest("a", ev("SessionStart"))
        def hung():
            raise RuntimeError("tmux list-panes timed out after 5s")
        self.store.tmux_live = hung
        self.store.reconcile()
        self.assertEqual(self.store.sessions["a"]["state"], "idle")      # not marked exited

    def test_tmux_wrapper_times_out(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 5)):
            with self.assertRaises(RuntimeError):
                tmuxctl.tmux("list-panes")


class TestEventLogRotation(unittest.TestCase):
    def test_ids_keep_counting_after_rotation(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps({"id": 41, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "Stop", "data": {}}) + "\n")
        with mock.patch.object(os.path, "getsize", lambda p: 60 * 1024 * 1024), \
                mock.patch.object(sessions, "registry", lambda: {}):
            s = store.Store(d, tmux_live=lambda: {}, paste=lambda *a: None)
        self.assertEqual(s.seq, 41)
        self.assertTrue(os.path.exists(os.path.join(d, "events.jsonl.1")))
        self.assertEqual(len(s.events["a"]), 1)


class TestConversations(StoreCase):
    ID1, ID2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"

    def write(self, home, folder, cid, recs):
        d = os.path.join(home, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(folder)))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, cid + ".jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
            f.write("{torn line\n")

    def test_index(self):
        home, folder = tempfile.mkdtemp(dir=TMP), tempfile.mkdtemp(dir=TMP)
        u = lambda c, **k: {"type": "user", "cwd": folder, "message": {"content": c}, **k}
        self.write(home, folder, self.ID1, [
            u("<command-name>/clear</command-name>"), u("hi", isMeta=True),
            u("Fix the build please"), {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
            u([{"type": "tool_result", "content": "x"}]), {"type": "ai-title", "aiTitle": "Build fix"},
            {"type": "custom-title", "customTitle": "My build"}, u("sub", isSidechain=True)])
        self.write(home, folder, self.ID2, [u("<only wrappers>")])                    # no real messages
        self.write(home, folder, "not-a-uuid", [u("x")])
        self.write(home, "/elsewhere-xyz", self.ID2, [u("other folder")])
        # copied from another machine: recorded cwd doesn't exist here -> still this folder's conversation
        id3, id4 = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
        self.write(home, folder, id3, [{"type": "user", "cwd": "/home/jack-nonexistent/dev/x", "message": {"content": "from another box"}}])
        # same directory name but the recorded cwd is a different folder that exists here -> not ours
        self.write(home, folder, id4, [{"type": "user", "cwd": TMP, "message": {"content": "collision"}}])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            got = projects.conversations(folder)
        self.assertEqual(sorted(c["first"] for c in got), ["Fix the build please", "from another box"])
        c = next(c for c in got if c["id"] == self.ID1)
        self.assertEqual((c["id"], c["title"], c["first"], c["last"], c["messages"]),
                         (self.ID1, "My build", "Fix the build please", "Done.", 1))

    def test_resume_rules(self):
        started = []
        live_terminal = {self.ID2}

        class Fake:
            trusted_projects = staticmethod(lambda: ["/ok"])
            conversations = staticmethod(lambda p: [{"id": self.ID1}, {"id": self.ID2}])
            live_conversations = staticmethod(lambda: {i: 1 for i in live_terminal})
            start = staticmethod(lambda n, p, e: started.append(e))

        self.store.lifecycle = Fake()
        self.assertEqual(self.store.create("x", "/ok", False, resume="../../etc")[0], 400)
        self.assertEqual(self.store.create("x", "/ok", False, resume=self.ID2)[0], 409)          # open in a terminal
        self.assertEqual(self.store.create("x", "/ok", True, resume=self.ID2, fork=True)[0], 200)
        self.assertEqual(started[-1], ["--permission-mode", "default", "--resume", self.ID2, "--fork-session"])
        self.store.sessions["a"]["claude_session_id"] = self.ID1                                  # open in grovy session "a"
        code, msg = self.store.create("y", "/ok", False, resume=self.ID1)
        self.assertEqual(code, 409)
        self.assertIn("session a", msg)
        self.store.sessions["a"]["claude_session_id"] = None
        self.assertEqual(self.store.create("y", "/ok", False, resume=self.ID1)[0], 200)
        self.assertEqual(started[-1], ["--resume", self.ID1])


class TestForget(StoreCase):
    def test_forget_only_ended_sessions(self):
        regs = {"a": {"cwd": "/a"}, "b": {"cwd": "/b"}}
        import contextlib
        @contextlib.contextmanager
        def locked():
            yield regs
        with mock.patch.object(sessions, "registry_locked", locked):
            self.assertEqual(self.store.forget("a")[0], 409)          # still running
            del self.live["b"]
            self.store.reconcile()
            self.assertEqual(self.store.forget("b"), (200, "removed"))
            self.assertNotIn("b", regs)
            self.assertNotIn("b", self.store.sessions)
            self.assertEqual(self.store.forget("zz")[0], 404)


class TestSecurityReview(StoreCase):
    def test_permission_mid_typing_stops_before_enter(self):
        """A turn Claude starts by itself (e.g. a background task finished) opens a permission dialog while
        we type: the final Enter would answer it. Typing must stop and the request stay pending."""
        self.store.ingest("a", ev("SessionStart"))
        seen = []
        def paste(n, t, command=False, abort=None):
            self.assertIsNone(abort())
            self.store.ingest("a", ev("UserPromptSubmit", prompt="<task-notification>done</task-notification>"))
            self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash", tool_input={"command": "rm x"}))
            seen.append(abort())
            raise tmuxctl.PasteAborted(seen[-1])
        self.store.paste = paste
        self.store.enqueue("a", "hello")
        for _ in range(100):
            if self.store.prompts[0]["status"] != "injected":
                break
            time.sleep(0.01)
        self.assertIn("permission request", seen[0])
        p = self.store.prompts[0]
        self.assertEqual(p["status"], "failed")
        self.assertIn("Enter was not pressed", p["error"])
        self.assertEqual(self.store.sessions["a"]["state"], "needs_input")   # not overwritten with "unknown"
        self.assertEqual(len(self.store._pending("a")), 1)

    def test_other_turn_mid_typing_stops(self):
        self.store.ingest("a", ev("SessionStart"))
        reasons = []
        def paste(n, t, command=False, abort=None):
            self.store.ingest("a", ev("UserPromptSubmit", prompt="<agent-message from=x>hi</agent-message>"))
            reasons.append(abort())
        self.store.paste = paste
        self.store.enqueue("a", "hello")
        for _ in range(100):
            if reasons:
                break
            time.sleep(0.01)
        self.assertIn("something else", reasons[0])

    def test_statusline_ignores_settings_in_claude_cwd(self):
        home = tempfile.mkdtemp(dir=TMP)
        proj, evil = os.path.join(home, "proj"), os.path.join(home, "proj", "cloned")
        marker = os.path.join(home, "PWNED")
        for d, cmd in ((proj, "echo OWN"), (evil, f"touch {marker}; echo EVIL")):
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(d, ".claude", "settings.json"), "w") as f:
                json.dump({"statusLine": {"type": "command", "command": cmd}}, f)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(home, "cfg")}), \
                mock.patch.object(sessions, "registry", lambda: {"s1": {"cwd": proj}}), \
                mock.patch.object(client, "sock_request", mock.Mock(side_effect=OSError("down"))), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"cwd": evil}))), redirect_stdout(out):
            hook.cmd_statusline("s1")
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(out.getvalue().strip(), "OWN")   # the session's own project settings still apply

    def test_sharing_is_not_in_the_cli(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            grovy.main(["page", "share", "x"])


class TestSetupAndCustomCss(unittest.TestCase):
    def test_service_unit(self):
        u = install.service_unit("100.1.2.3", 7333, path="/a:/b:/a:/x%y")
        self.assertIn("KillMode=process", u)              # restarting the daemon must not kill tmux
        self.assertIn("Environment=GROVY_BIND=100.1.2.3", u)
        self.assertIn("Environment=PATH=/a:/b:/x%%y\n", u)  # de-duplicated, % escaped for systemd
        self.assertIn(os.path.join(core.HERE, "grovy") + " serve", u)

    def test_dry_run_changes_nothing(self):
        home = tempfile.mkdtemp(dir=TMP)
        env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, "cfg"), "PATH": "/usr/bin:/bin"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(shutil, "which", lambda x: "/bin/true"), \
                mock.patch.object(client, "sock_request", mock.Mock(side_effect=OSError)), redirect_stdout(out):
            rc = grovy.main(["setup", "--dry-run", "--bind", "0.0.0.0", "--no-service"])
        self.assertEqual(rc, 0)
        self.assertIn("linking", out.getvalue())
        self.assertEqual(os.listdir(home), [])

    def test_setup_finishes_with_the_phone_link(self):
        """It ended in a NameError (the custom.css path variable was gone) right after printing the link."""
        home = tempfile.mkdtemp(dir=TMP)
        env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, "cfg"), "PATH": "/usr/bin:/bin"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(shutil, "which", lambda x: "/bin/true"), \
                mock.patch.object(client, "sock_request", lambda *a, **k: {"ok": 1}), \
                mock.patch.object(install, "cmd_url", lambda a: print("LINK")), redirect_stdout(out):
            rc = grovy.main(["setup", "--bind", "127.0.0.1", "--no-service"])
        self.assertEqual(rc, 0)
        self.assertIn("LINK", out.getvalue())
        self.assertIn(os.path.join(home, "cfg", "grovy"), out.getvalue())
        self.assertLessEqual({"custom.css", "custom.js"}, set(os.listdir(os.path.join(home, "cfg", "grovy"))))

    def test_config_dir_changes_the_fingerprint_only_when_set(self):
        base = claude.session_fingerprint("fp")
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": "/somewhere"}):
            self.assertNotEqual(claude.session_fingerprint("fp"), base)
        self.assertEqual(claude.session_fingerprint("fp"), base)

    def test_custom_css_is_served_and_reloaded(self):
        d = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": d}):
            base, csp = grovyd.load_index()
            with open(os.path.join(d, "custom.css"), "w") as f:
                f.write(":root { --user:#123456; }")
            html, csp2 = grovyd.load_index()
            self.assertIn(b'<style id="custom-css">\n:root { --user:#123456; }', html)
            self.assertLess(html.index(b"</style>"), html.index(b"custom-css"))   # after the built-in styles
            self.assertEqual(csp, csp2)                                         # scripts (and hashes) unchanged
            with open(os.path.join(d, "custom.css"), "w") as f:
                f.write("/* no <script> tags here */ body{}")
            self.assertEqual(grovyd.load_index()[1], csp)   # grovy's script keeps its hash (it broke the page)
            with open(os.path.join(d, "custom.css"), "w") as f:
                f.write("a{}</style><script>alert(1)</script>")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(grovyd.load_index()[0], base)                  # can't close the style block
            srv = grovyd.HttpServer.__new__(grovyd.HttpServer)
            srv.index_mtime = None
            with open(os.path.join(d, "custom.css"), "w") as f:
                f.write("b{color:red}")
            srv.refresh_index()
            self.assertIn(b"b{color:red}", srv.index)
            with open(os.path.join(d, "custom.css"), "w") as f:
                f.write("b{color:blue}")
            os.utime(os.path.join(d, "custom.css"), ns=(1, 1))
            srv.refresh_index()
            self.assertIn(b"b{color:blue}", srv.index)                          # hot reload, no restart

    def test_crlf_checkout_keeps_the_hash(self):
        """Second review: with index.html checked out with CR LF, grovy's own script hash didn't match the browser's."""
        import base64
        import hashlib
        d = tempfile.mkdtemp(dir=TMP)
        page = os.path.join(d, "index.html")
        with open(page, "wb") as f:
            f.write(b"<html><script>\r\nlet a = 1;\r\n</script></html>\r\n")
        with mock.patch.object(grovyd, "INDEX_PATH", page), mock.patch.object(grovyd, "EXTENSIONS_DIR", d):
            html, csp = grovyd.load_index()
        self.assertNotIn(b"\r", html)
        self.assertIn(base64.b64encode(hashlib.sha256(b"\nlet a = 1;\n").digest()).decode(), csp)

    def test_builtin_extensions_come_before_custom_js(self):
        import base64
        import hashlib
        cfg, exts = tempfile.mkdtemp(dir=TMP), tempfile.mkdtemp(dir=TMP)
        for name, code in (("b.js", "grovy.extension('b', g => 0);"), ("a.js", "grovy.extension('a', g => 0);"),
                           ("notes.txt", "not a script")):
            with open(os.path.join(exts, name), "w") as f:
                f.write(code)
        with open(os.path.join(cfg, "custom.js"), "w") as f:
            f.write("grovy.disable('a');")
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": cfg}), mock.patch.object(grovyd, "EXTENSIONS_DIR", exts):
            html, csp = grovyd.load_index()
            order = [html.index(x) for x in (b"DOMContentLoaded", b"extension('a'", b"extension('b'", b"disable('a')")]
            self.assertEqual(order, sorted(order))                  # grovy, built-ins by name, then the user's file
            self.assertNotIn(b"not a script", html)
            for block in (b"\ngrovy.extension('a', g => 0);\n", b"\ngrovy.disable('a');\n"):
                self.assertIn(base64.b64encode(hashlib.sha256(block).digest()).decode(), csp)
            self.assertEqual(csp.count("sha256-"), 4)
            srv = grovyd.HttpServer.__new__(grovyd.HttpServer)
            srv.index_mtime = None
            srv.refresh_index()
            with open(os.path.join(exts, "a.js"), "w") as f:
                f.write("grovy.extension('a', g => 1); // edited")
            os.utime(os.path.join(exts, "a.js"), ns=(1, 1))
            srv.refresh_index()
            self.assertIn(b"// edited", srv.index)                  # reloaded without a restart
            with open(os.path.join(exts, "a.js"), "w") as f:
                f.write("x = '</script>';")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertNotIn(b"x = '", grovyd.load_index()[0])  # same rules as custom.js
        self.assertTrue(os.path.exists(os.path.join(ROOT, "extensions", "context-bar.js")))
        self.assertIn(b'grovy.extension("context-bar"', grovyd.load_index()[0])   # the real one ships and loads

    def test_custom_js_is_served_hashed_and_reloaded(self):
        import base64
        import hashlib
        d = tempfile.mkdtemp(dir=TMP)
        js = os.path.join(d, "custom.js")
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": d}):
            base, csp = grovyd.load_index()
            with open(js, "w") as f:
                f.write('grovy.menu("Hi", () => grovy.say("hi"));')
            html, csp2 = grovyd.load_index()
            block = b'\ngrovy.menu("Hi", () => grovy.say("hi"));\n'
            self.assertIn(b"<script>" + block + b"</script>", html)
            self.assertLess(html.index(b"DOMContentLoaded"), html.index(block))   # after grovy's own script
            self.assertTrue(html.endswith(base[base.rindex(b"</script>") + 9:]))  # and nothing else moved
            h = "'sha256-%s'" % base64.b64encode(hashlib.sha256(block).digest()).decode()
            self.assertNotIn(h, csp)
            self.assertIn(h, csp2)                                                # allowed by its hash, nothing looser
            self.assertEqual(csp2.replace(" " + h, ""), csp)
            for bad in ('x = "</script><script>alert(1)";', "x = 1 <!-- 2", "y = '</SCRIPT >'"):
                with open(js, "w") as f:
                    f.write(bad)
                with mock.patch("sys.stderr", io.StringIO()) as err:
                    self.assertEqual(grovyd.load_index(), (base, csp))           # refused, page unchanged
                self.assertIn("ignoring", err.getvalue())
            srv = grovyd.HttpServer.__new__(grovyd.HttpServer)
            srv.index_mtime = None
            with open(js, "w") as f:
                f.write("let a = 1;")
            srv.refresh_index()
            self.assertIn(b"let a = 1;", srv.index)
            with open(js, "w") as f:
                f.write("let a = 2;")
            os.utime(js, ns=(1, 1))
            srv.refresh_index()
            self.assertIn(b"let a = 2;", srv.index)                               # hot reload, no restart
            os.unlink(js)
            srv.refresh_index()
            self.assertEqual(srv.index, base)
            with open(js, "wb") as f:                                             # saved on Windows: CR LF
                f.write(b"let a = 3;\r\nlet b = 4;\r")
            html, csp3 = grovyd.load_index()
            lf = b"\nlet a = 3;\nlet b = 4;\n\n"
            self.assertIn(b"<script>" + lf + b"</script>", html)                  # the browser hashes the LF text
            self.assertIn(base64.b64encode(hashlib.sha256(lf).digest()).decode(), csp3)
            for bad in (b"let a = '\0';", b"let a = '\xff';"):
                with open(js, "wb") as f:
                    f.write(bad)
                with mock.patch("sys.stderr", io.StringIO()):
                    self.assertEqual(grovyd.load_index(), (base, csp))
            os.unlink(js)
            os.mkfifo(js)                                                          # would block the page request
            with mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(grovyd.load_index(), (base, csp))
            self.assertIn("not a regular file", err.getvalue())
            os.unlink(js)
            shutil.copyfile(os.path.join(ROOT, "examples", "custom.js"), js)      # what `grovy setup` installs
            self.assertIn(b"grovy.widget(type, render)", grovyd.load_index()[0])


def write_plugin(folder, name, code):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w") as f:
        f.write(code)


GOOD_PLUGIN = """
import time
seen = []
def setup(grovy):
    grovy.on("event", lambda s, rec: seen.append(("event", s, rec["type"])) or rec["data"].clear())
    @grovy.on("state")
    def st(s, old, new):
        seen.append(("state", s, old, new))
    grovy.on("notify", lambda s, kind, text: seen.append(("notify", s, kind)))
    @grovy.route("GET", "hello")
    def hello(req):
        return {"hi": req.query.get("who", "?")}
    @grovy.route("POST", "echo/body")
    def echo(req):
        return 201, {"got": req.body}
    grovy.route("GET", "boom", lambda req: 1 / 0)
    grovy.route("GET", "notjson", lambda req: {"x": object()})
"""


class TestPlugins(StoreCase):
    def load(self, files, env=None, builtin=None):
        folder, ours = tempfile.mkdtemp(dir=TMP), tempfile.mkdtemp(dir=TMP)
        for name, code in files.items():
            write_plugin(folder, name, code)
        for name, code in (builtin or {}).items():
            write_plugin(ours, name, code)
        with mock.patch.dict(os.environ, env or {}), mock.patch("sys.stderr", io.StringIO()) as err:
            host = plugins.Plugins(self.store, folder, builtin=ours).load()
        self.store.plugins = host
        return host, err.getvalue()

    def wait_for(self, cond):
        for _ in range(200):
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_load_and_skip(self):
        host, log = self.load({"good.py": GOOD_PLUGIN, "Bad Name.py": GOOD_PLUGIN, "_off.py": "raise SystemExit",
                               "broken.py": "def setup(grovy):\n    grovy.on('state', print)\n    raise RuntimeError('nope')\n",
                               "nosetup.py": "x = 1\n", "syntax.py": "def setup(:\n"})
        self.assertEqual(host.loaded, ["good"])
        self.assertEqual(sorted(host.failed), ["Bad Name", "broken", "nosetup", "syntax"])
        self.assertIn("RuntimeError: nope", host.failed["broken"])
        # what a failing plugin registered before failing is gone: only good's handlers remain
        self.assertEqual({n for e in host.handlers.values() for n, _ in e}, {"good"})
        self.assertIn("failed to load", log)
        host, _ = self.load({"good.py": GOOD_PLUGIN}, env={"GROVY_PLUGINS": "0"}, builtin={"ours.py": GOOD_PLUGIN})
        self.assertEqual((host.loaded, host.thread), ([], None))

    def test_builtin_plugins(self):
        """grovy's own plugins load like the user's; the user's file of the same name replaces one, NAME.off drops it."""
        mark = "def setup(grovy):\n    grovy.route('GET', 'who', lambda r: %r)\n"
        host, _ = self.load({"mine.py": mark % "mine", "swap.py": mark % "user's", "gone.off": ""},
                            builtin={"keep.py": mark % "built-in", "swap.py": mark % "built-in", "gone.py": mark % "x",
                                     "_skip.py": "raise SystemExit"})
        self.assertEqual(host.loaded, ["keep", "mine", "swap"])
        self.assertEqual(host.handle("GET", "swap", "who", {}, None)[0], 200)
        self.assertIn(b"user's", host.handle("GET", "swap", "who", {}, None)[1])
        self.assertIn(b"built-in", host.handle("GET", "keep", "who", {}, None)[1])
        self.assertEqual(host.failed, {})
        # a-b.py and a_b.py are one name across the folders too; .off only ever drops a built-in
        host, _ = self.load({"a_b.py": mark % "user's", "mine.py": mark % "mine", "mine.off": "", "c_d.off": ""},
                            builtin={"a-b.py": mark % "built-in", "c-d.py": mark % "x"})
        self.assertEqual((host.loaded, host.failed), (["a_b", "mine"], {}))
        self.assertIn(b"user's", host.handle("GET", "a_b", "who", {}, None)[1])

    def test_context(self):
        """grovy.context: texts in load order; a slow, failing or oversized one is skipped, the others still count."""
        code = "import time\ndef setup(grovy):\n    grovy.context(lambda s, info: %s)\n"
        with mock.patch.object(plugins, "CONTEXT_TIMEOUT", 0.3):
            host, _ = self.load({"a.py": code % "f'A {s} {info[\"cwd\"]} {info[\"source\"]}'",
                                 "b.py": code % "None", "c.py": code % "1 / 0", "d.py": code % "time.sleep(5)",
                                 "e.py": code % "'x' * 20000", "f.py": code % "3", "g.py": code % "'  G  '"})
            with mock.patch("sys.stderr", io.StringIO()) as err:
                t = time.monotonic()
                self.assertEqual(host.context("s1", {"cwd": "/p", "source": "clear"}), "A s1 /p clear\n\nG")
                self.assertLess(time.monotonic() - t, 1.0)
            for why in ("ZeroDivisionError", "no answer", "at most", "not text"):
                self.assertIn(why, err.getvalue())
        none, _ = self.load({})
        self.assertEqual(none.context("s1", {}), "")

    def test_context_over_the_socket(self):
        host, _ = self.load({"a.py": "def setup(grovy):\n    grovy.context(lambda s, info: info['cwd'])\n"})
        def ask(payload, **extra):
            h = grovyd.SockHandler.__new__(grovyd.SockHandler)
            h.server = type("S", (), {"store": self.store})()
            h.rfile = io.BytesIO(json.dumps({"type": "hook", "session": "a", "event": payload, **extra}).encode() + b"\n")
            h.wfile = io.BytesIO()
            h.handle()
            return json.loads(h.wfile.getvalue() or b"null")
        self.assertEqual(ask(ev("SessionStart", source="startup", cwd="/proj"), want_context=True), {"context": "/proj"})
        self.assertIsNone(ask(ev("SessionStart", source="startup", cwd="/proj")))   # nobody asked: no answer
        self.store.plugins = None   # plugins not loaded yet: an answer all the same
        self.assertEqual(ask(ev("SessionStart", source="startup"), want_context=True), {"context": ""})

    def test_after_edit(self):
        """grovy.after_edit: only edits whose path matches the plugin's glob, with what was edited and by whom."""
        code = ("def setup(grovy):\n    @grovy.after_edit(glob=%r)\n"
                "    def f(s, i): return f\"%s {s} {i['path']} {i['tool']} {i['agent_id']} {i['cwd']}\"\n")
        host, _ = self.load({"md.py": code % ("*.md", "MD"), "all.py": code % ("*", "ALL")})
        self.assertEqual(host.edit_globs(), ["*", "*.md"])
        edit = lambda path, tool="Edit", **kw: ev("PostToolUse", tool_name=tool, tool_input={"file_path": path}, cwd="/p", **kw)
        self.assertEqual(host.after_edit("s", edit("/p/a.md")), "ALL s /p/a.md Edit None /p\n\nMD s /p/a.md Edit None /p")
        self.assertEqual(host.after_edit("s", edit("/p/a.py", "Write", agent_id="ag1")), "ALL s /p/a.py Write ag1 /p")
        self.assertEqual(host.after_edit("s", edit("/p/a.md", "Read")), "")          # not an edit
        self.assertEqual(host.after_edit("s", edit("a.md")), "")                     # not an absolute path
        self.assertEqual(host.after_edit("s", ev("PostToolUse", tool_name="NotebookEdit",
                                                 tool_input={"notebook_path": "/p/n.ipynb"})), "ALL s /p/n.ipynb NotebookEdit None None")
        with self.assertRaises(ValueError):
            plugins.Api(host, "x").after_edit(lambda s, i: None, glob="")

    def test_hung_callback_holds_one_thread(self):
        """A callback that never returns is skipped while its earlier call runs: one stuck thread, not one per edit."""
        code = ("import threading\ngate = threading.Event()\ncalls = []\ndef setup(grovy):\n"
                "    @grovy.after_edit(glob='*.md')\n    def f(s, i):\n        calls.append(1)\n        gate.wait(5)\n"
                "        return 'LATE'\n"
                "    grovy.after_edit(lambda s, i: 'OK', glob='*.md')\n")
        with mock.patch.object(plugins, "NOTE_TIMEOUT", 0.2):
            host, _ = self.load({"hang.py": code})
            mod = sys.modules["grovy_plugin_hang"]
            edit = ev("PostToolUse", tool_name="Edit", tool_input={"file_path": "/p/a.md"})
            with mock.patch("sys.stderr", io.StringIO()):
                for _ in range(5):
                    self.assertEqual(host.after_edit("s", edit), "OK")
                self.assertEqual(len(mod.calls), 1)
                mod.gate.set()                           # it returns at last: its late answer is never used...
                self.assertTrue(self.wait_for(lambda: not host.hung))
                self.assertEqual(host.after_edit("s", edit), "LATE\n\nOK")   # ...and it is asked again
                self.assertEqual(len(mod.calls), 2)

    def test_overlapping_calls_both_answer(self):
        """Two subagents editing at once: a callback still busy (in good time) with one edit answers the other too."""
        code = ("import threading\nstarted = threading.Barrier(2, timeout=5)\ndef setup(grovy):\n"
                "    @grovy.after_edit(glob='*.md')\n    def f(s, i):\n        started.wait()\n        return 'N ' + s\n")
        host, _ = self.load({"both.py": code})
        edit = ev("PostToolUse", tool_name="Edit", tool_input={"file_path": "/p/a.md"})
        got = {}
        ts = [threading.Thread(target=lambda s=s: got.__setitem__(s, host.after_edit(s, edit))) for s in ("a", "b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual(got, {"a": "N a", "b": "N b"})

    def test_answers_are_capped_in_all(self):
        code = "def setup(grovy):\n" + "".join(f"    grovy.context(lambda s, i: '{c}' * 10000)\n" for c in "abcd")
        host, _ = self.load({"big.py": code})
        with mock.patch("sys.stderr", io.StringIO()) as err:
            text = host.context("s", {})
        self.assertEqual(text, "a" * 10000 + "\n\n" + "b" * 10000 + "\n\n" + "c" * 10000)
        self.assertIn("together", err.getvalue())

    def test_after_edit_over_the_socket(self):
        host, _ = self.load({"md.py": "def setup(grovy):\n    grovy.after_edit(lambda s, i: 'NOTE', glob='*.md')\n"})
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.store.enqueue("a", "steer me")
        def ask(payload, **extra):
            h = grovyd.SockHandler.__new__(grovyd.SockHandler)
            h.server = type("S", (), {"store": self.store})()
            h.rfile = io.BytesIO(json.dumps({"type": "hook", "session": "a", "event": payload, **extra}).encode() + b"\n")
            h.wfile = io.BytesIO()
            h.handle()
            return json.loads(h.wfile.getvalue() or b"null")
        edit = lambda path, **kw: ev("PostToolUse", tool_name="Edit", tool_input={"file_path": path}, **kw)
        r = ask(edit("/p/x.md", agent_id="sub"), want_note=True)   # a subagent never takes the main agent's steers
        self.assertEqual(r, {"steer": None, "note": "NOTE"})
        with mock.patch.object(grovyd, "STEER_TAKE_BY", 0):   # the hook may have stopped waiting: it stays queued
            self.assertEqual(ask(edit("/p/x.py"), want_steer=True), {"steer": None, "note": None})
        r = ask(edit("/p/x.py"), want_steer=True)
        self.assertIn("steer me", r["steer"])
        self.assertIsNone(r["note"])
        r = ask(edit("/p/x.md"), want_steer=True)
        self.assertEqual(r["note"], "NOTE")
        self.assertIsNone(r["steer"])   # taken by the previous edit already

    def test_events_reach_plugins_as_copies_off_the_lock(self):
        host, _ = self.load({"good.py": GOOD_PLUGIN})
        seen = sys.modules["grovy_plugin_good"].seen
        self.store.ingest("a", ev("UserPromptSubmit", prompt="hello"))
        self.assertTrue(self.wait_for(lambda: ("state", "a", "starting", "working") in seen), seen)
        self.assertIn(("event", "a", "UserPromptSubmit"), seen)
        rec = [r for r in self.store.events["a"] if r["type"] == "UserPromptSubmit"][-1]
        self.assertEqual(rec["data"].get("prompt"), "hello")      # the plugin cleared its copy, not the record
        self.store._notify("a", "done", "finished")
        self.assertTrue(self.wait_for(lambda: ("notify", "a", "done") in seen))

    def test_slow_or_failing_plugin_never_blocks_the_store(self):
        gate = threading.Event()
        slow = "import threading\nGATE = None\ndef setup(grovy):\n    grovy.on('event', lambda s, r: GATE.wait(5))\n"
        bad = "def setup(grovy):\n    grovy.on('event', lambda s, r: 1 / 0)\n"
        host, _ = self.load({"aslow.py": slow, "zbad.py": bad})   # in this order: bad runs after slow
        sys.modules["grovy_plugin_aslow"].GATE = gate
        t = time.monotonic()
        for i in range(5):
            self.store.ingest("a", ev("Notification", message=f"m{i}"))
        self.assertLess(time.monotonic() - t, 1.0)              # ingest doesn't wait for the slow plugin
        with mock.patch.object(plugins, "QUEUE_MAX", 2), mock.patch("sys.stderr", io.StringIO()):
            small = plugins.Plugins(self.store, "/nonexistent", builtin="/nonexistent")
            small.handlers["event"].append(("x", lambda *a: None))
            for _ in range(5):
                small.emit("event", "a", {})                    # no worker: the queue fills, then events are dropped
            self.assertEqual(small.dropped, 3)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            gate.set()
            self.assertTrue(self.wait_for(lambda: host.q.empty() and "ZeroDivisionError" in err.getvalue()))
            time.sleep(0.05)
        self.assertIn("plugin zbad (event)", err.getvalue())    # logged, and the worker keeps going

    def test_example_plugin(self):
        with open(os.path.join(ROOT, "examples", "plugin.py")) as f:
            host, log = self.load({"worktime.py": f.read()})
        self.assertEqual(host.loaded, ["worktime"], log)
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.assertTrue(self.wait_for(lambda: "a" in sys.modules["grovy_plugin_worktime"].since))
        code, res = host.handle("GET", "worktime", "totals", {}, None)
        self.assertEqual((code, list(json.loads(res))), (200, ["a"]))

    def test_review_findings(self):
        """Each case broke a promise of the plugin API (adversarial review): see the comments."""
        late = "API = None\ndef setup(grovy):\n    global API\n    API = grovy\n"
        hang = "import threading\ndef setup(grovy):\n    threading.Event().wait()\n"
        exits = ("def setup(grovy):\n    grovy.on('event', lambda s, r: exec('raise SystemExit'))\n"
                 "    grovy.on('event', lambda s, r: SEEN.append(r['type']))\nSEEN = []\n")
        with mock.patch.object(plugins, "SETUP_TIMEOUT", 0.3):
            host, log = self.load({"quits.py": "def setup(grovy):\n    raise SystemExit(1)\n",   # stopped the daemon
                                   "x-y.py": "def setup(grovy):\n    pass\n",
                                   "x_y.py": "def setup(grovy):\n    raise ValueError\n",          # removed x-y's module
                                   "late.py": late, "hangs.py": hang, "exits.py": exits})
        self.assertIn("SystemExit", host.failed["quits"])
        self.assertIn("same module name as x-y.py", host.failed["x_y"])
        self.assertIn("grovy_plugin_x_y", sys.modules)
        self.assertIn("longer than", host.failed["hangs"])                  # a stuck setup() doesn't stop the rest
        self.assertEqual(sorted(host.loaded), ["exits", "late", "x-y"])
        with self.assertRaises(RuntimeError):                               # registering after setup(): refused
            sys.modules["grovy_plugin_late"].API.on("event", print)
        self.store.ingest("a", ev("Notification", message="one"))
        self.store.ingest("a", ev("Notification", message="two"))
        seen = sys.modules["grovy_plugin_exits"].SEEN
        self.assertTrue(self.wait_for(lambda: seen.count("Notification") == 2))   # SystemExit killed the worker
        self.assertTrue(host.thread.is_alive())
        api = plugins.Api(host, "t")
        self.store.ingest("a", ev("UserPromptSubmit", prompt="busy"))
        code, res = api.send("a", "queued")
        res["text"] = "rm -rf ~"                                            # the result was grovy's own prompt
        self.assertEqual([p["text"] for p in self.store.prompts if p["id"] == res["id"]], ["queued"])

    def test_failed_plugin_loses_its_api(self):
        """Second review: a plugin whose setup() failed could still read sessions and send from a thread it started."""
        out = os.path.join(tempfile.mkdtemp(dir=TMP), "got")
        code = ("import threading, time\ndef setup(grovy):\n"
                "    def later():\n        time.sleep(0.2)\n        got = []\n"
                "        for f in (grovy.sessions, lambda: grovy.send('a', 'hi'), lambda: grovy.on('event', print)):\n"
                "            try:\n                f(); got.append('worked')\n"
                "            except RuntimeError as e:\n                got.append(str(e))\n"
                f"        open({out!r}, 'w').write('\\n'.join(got))\n"
                "    threading.Thread(target=later).start()\n    raise ValueError('nope')\n")
        host, _ = self.load({"dead.py": code})
        self.assertIn("dead", host.failed)
        self.assertTrue(self.wait_for(lambda: os.path.exists(out) and os.path.getsize(out)))
        time.sleep(0.05)
        with open(out) as f:
            got = f.read().split("\n")
        self.assertEqual(len(got), 3, got)
        self.assertTrue(all("failed to load" in g for g in got), got)
        self.assertEqual(self.pasted, [])

    def test_routes_have_deadlines_and_json_answers(self):
        gate = threading.Event()
        code = ("import threading\nGATE = threading.Event()\ndef setup(grovy):\n"
                "    grovy.route('GET', 'hang', lambda r: GATE.wait(5) and {})\n"
                "    grovy.route('GET', 'nan', lambda r: {'x': float('nan')})\n"
                "    grovy.route('GET', 'empty', lambda r: (204, {}))\n"
                "    grovy.route('GET', 'exit', lambda r: exec('raise SystemExit'))\n"
                "    grovy.route('GET', 'surrogate', lambda r: {'name': b'\\xff'.decode('utf-8', 'surrogateescape')})\n"
                "    grovy.route('GET', 'pair', lambda r: (1, 2))\n")
        host, _ = self.load({"r.py": code})
        sys.modules["grovy_plugin_r"].GATE = gate
        with mock.patch.object(plugins, "ROUTE_TIMEOUT", 0.2), mock.patch("sys.stderr", io.StringIO()):
            host.running = threading.BoundedSemaphore(2)
            self.assertEqual(host.handle("GET", "r", "hang", {}, None)[0], 504)   # it held a server slot forever
            self.assertEqual(host.handle("GET", "r", "hang", {}, None)[0], 504)
            self.assertEqual(host.handle("GET", "r", "hang", {}, None)[0], 503)   # hung routes are capped
            gate.set()
            self.assertTrue(self.wait_for(lambda: host.running.acquire(blocking=False)))
            host.running.release()
            self.assertEqual(host.handle("GET", "r", "nan", {}, None)[0], 500)    # the browser can't parse NaN
            self.assertEqual(host.handle("GET", "r", "empty", {}, None)[0], 500)  # 204 can't carry a body
            self.assertEqual(host.handle("GET", "r", "exit", {}, None)[0], 500)
            # second review: it passed the check (ASCII-escaped) and then failed to encode when sent: no answer at all
            self.assertEqual(host.handle("GET", "r", "surrogate", {}, None)[0], 500)
            self.assertEqual(host.handle("GET", "r", "pair", {}, None), (200, b"[1, 2]"))   # plain data, not a status

    def test_api_helpers(self):
        host, _ = self.load({})
        api = plugins.Api(host, "t")
        self.assertEqual({s["name"] for s in api.sessions()}, {"a", "b"})
        self.assertEqual(api.session("a")["session"]["name"], "a")
        self.assertIsNone(api.session("nope"))
        self.store.ingest("a", ev("Stop"))
        code, res = api.send("a", "from a plugin")
        self.assertEqual(code, 200)
        self.wait_pasted(1)
        self.assertEqual(self.pasted[-1], ("a", "from a plugin"))
        for bad in (lambda: api.on("nope", print), lambda: api.route("PUT", "x", print),
                    lambda: api.route("GET", "../x", print), lambda: api.on("event", 3)):
            self.assertRaises((ValueError, TypeError), bad)


class TestPluginRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = mock.patch.object(sessions, "registry", lambda: {})
        cls.reg.start()
        cls.store = store.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {}, paste=lambda *a: None)
        folder = tempfile.mkdtemp(dir=TMP)
        write_plugin(folder, "good.py", GOOD_PLUGIN)
        with mock.patch("sys.stderr", io.StringIO()):
            cls.store.plugins = plugins.Plugins(cls.store, folder, builtin=tempfile.mkdtemp(dir=TMP)).load()
        cls.httpd = grovyd.HttpServer(("127.0.0.1", 0), grovyd.Http)
        cls.httpd.store, cls.httpd.token = cls.store, "t" * 32
        cls.httpd.page = grovyd.load_index()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.stopping = True
        cls.httpd.shutdown()
        cls.reg.stop()

    def req(self, path, body=None, token=True, header=True):
        h = {"Cookie": "grovy_token=" + "t" * 32} if token else {}
        if header:
            h["X-Grovy"] = "1"
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with mock.patch("sys.stderr", io.StringIO()), urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_routes(self):
        self.assertEqual(self.req("/api/x/good/hello?who=me"), (200, {"hi": "me"}))
        self.assertEqual(self.req("/api/x/good/echo/body", {"a": 1}), (201, {"got": {"a": 1}}))
        self.assertEqual(self.req("/api/x/good/hello", token=False)[0], 401)       # behind the login
        self.assertEqual(self.req("/api/x/good/echo/body", {"a": 1}, token=False)[0], 401)
        self.assertEqual(self.req("/api/x/good/hello", header=False)[0], 403)   # a tapped link can't run a GET route
        self.assertEqual(self.req("/api/x/good/nope")[0], 404)
        self.assertEqual(self.req("/api/x/other/hello")[0], 404)
        self.assertEqual(self.req("/api/x/good/hello", {"a": 1})[0], 404)           # GET-only route
        code, res = self.req("/api/x/good/boom")
        self.assertEqual(code, 500)
        self.assertIn("ZeroDivisionError", res["error"])
        self.assertEqual(self.req("/api/x/good/notjson")[0], 500)
        self.assertEqual(self.req("/api/sessions")[0], 200)                          # the daemon is still fine


class TestPages(StoreCase):
    def test_publish_versions_list_delete(self):
        code, p = self.store.publish_page("a", "report", "Bench", "<h1>v1</h1>")
        self.assertEqual((code, p["version"]), (200, 1))
        code, p2 = self.store.publish_page("a", "report", None, "<h1>v2</h1>")
        self.assertEqual((p2["id"], p2["version"], p2["title"]), (p["id"], 2, "report"))   # same page, new version
        self.assertEqual(self.store.page_html(p["id"])[1], b"<h1>v2</h1>")
        self.assertEqual(oct(os.stat(os.path.join(self.store.pages_dir, p["id"] + ".html")).st_mode & 0o777), "0o600")
        self.assertEqual([e["type"] for e in self.store.events["a"]][-2:], ["Page", "Page"])
        self.assertEqual(self.store.detail("a")["pages"], {p["id"]: {"version": 2, "title": "report", "share": None}})
        self.assertEqual(self.store.publish_page("b", "report", "", "x")[1]["version"], 1)   # per session
        self.assertEqual(len(self.store.list_pages("a")), 1)
        self.assertEqual(self.store.delete_page(p["id"], "b")[0], 404)                       # not b's page
        self.assertEqual(self.store.delete_page(p["id"])[0], 200)
        self.assertIsNone(self.store.page_html(p["id"]))
        self.assertEqual(store.Store(self.dir, tmux_live=lambda: {}, paste=None).list_pages("a"), [])   # persisted

    def test_publish_validation(self):
        self.assertEqual(self.store.publish_page("zz", "r", "", "x")[0], 404)
        for bad in ("../x", "", ".hidden", "a/b", "x" * 65, None):
            self.assertEqual(self.store.publish_page("a", bad, "", "x")[0], 400, bad)
        with mock.patch.object(pages, "PAGE_MAX", 10):
            self.assertEqual(self.store.publish_page("a", "big", "", "x" * 11)[0], 413)

    def test_pages_follow_rename_and_go_with_forget(self):
        _, p = self.store.publish_page("a", "r", "", "x")
        with mock.patch.object(self.store.lifecycle, "rename", lambda o, n: None):
            self.assertEqual(self.store.rename("a", "c")[0], 200)
        self.assertEqual(self.store.list_pages("c")[0]["id"], p["id"])
        self.assertEqual(self.store.publish_page("a", "r", "", "y")[1]["id"], p["id"])   # old name routes to c
        import contextlib
        @contextlib.contextmanager
        def locked():
            yield {"c": {}}
        del self.live["a"]
        with mock.patch.object(sessions, "registry_locked", locked), mock.patch.object(self.store, "tmux_live", lambda: {}):
            self.assertEqual(self.store.forget("c")[0], 200)
        self.assertIsNone(self.store.page_html(p["id"]))

    def test_bridge_goes_after_doctype(self):
        out = pages.with_bridge(b"\n<!DOCTYPE html><html>x")
        self.assertTrue(out.startswith(b"\n<!DOCTYPE html>" + pages.PAGE_BRIDGE))
        self.assertTrue(pages.with_bridge(b"<p>hi").startswith(pages.PAGE_BRIDGE))


class TestRename(StoreCase):
    def test_rename_moves_state_and_routes_old_name(self):
        regs = {"a": {"cwd": "/a"}, "b": {"cwd": "/b"}}
        renamed = []

        class Fake:
            trusted_projects = staticmethod(lambda: ["/x"])
            start = staticmethod(lambda *a: None)

            @staticmethod
            def rename(old, new):
                renamed.append((old, new))
                regs[new] = dict(regs.pop(old), aliases=[old])
        self.store.lifecycle = Fake()
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="busy"))
        self.store.enqueue("a", "queued for a")
        self.assertEqual(self.store.rename("a", "b")[0], 409)            # taken
        self.assertEqual(self.store.rename("a", "bad name")[0], 400)
        self.assertEqual(self.store.rename("a", "jean")[0], 200)
        self.assertEqual(renamed, [("a", "jean")])
        self.assertNotIn("a", self.store.sessions)
        self.assertEqual(self.store.sessions["jean"]["state"], "working")
        self.assertEqual(self.store.prompts[-1]["session"], "jean")
        self.assertTrue(any(e["type"] == "UserPromptSubmit" for e in self.store.events["jean"]))
        # the running Claude still reports "a": it must land on "jean"
        self.store.ingest("a", ev("Stop", last_assistant_message="done"))
        self.assertNotIn("a", self.store.sessions)
        # ...which made "jean" idle, so the prompt queued before the rename is typed into "jean"
        self.wait_pasted(1)
        self.assertEqual(self.pasted, [("jean", "queued for a")])
        # history from disk follows the rename too
        self.assertTrue(self.store.history("jean", q="busy"))
        # the old name can't be reused while it is an alias
        with mock.patch.object(sessions, "registry", lambda: regs):
            self.assertEqual(self.store.create("a", "/x", False)[0], 409)

    def test_hook_adapter_updates_the_renamed_entry(self):
        regs = {"jean": {"cwd": "/a", "aliases": ["a"], "claude_session_id": "OLD"}}
        import contextlib
        @contextlib.contextmanager
        def locked():
            yield regs
        out = io.StringIO()
        with mock.patch.object(sessions, "registry_locked", locked), mock.patch.object(client, "sock_request", lambda *a, **k: None), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(ev("SessionStart", sid="NEW")))), redirect_stdout(out):
            hook.cmd_hook("a")
        self.assertEqual(regs["jean"]["claude_session_id"], "NEW")


class TestStatusLine(StoreCase):
    SAMPLE = {"model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, "effort": {"level": "medium"},
              "fast_mode": False, "context_window": {"remaining_percentage": 66.4, "used_percentage": 30}}

    def test_meta_and_change_only_notifications(self):
        sent = []
        self.store.publish = lambda name=None: sent.append(name)
        self.store.status("a", self.SAMPLE)
        self.assertEqual(self.store.sessions["a"]["meta"],
                         {"model": "Opus 5.5", "effort": "medium", "fast": False, "context": 34})
        self.store.status("a", self.SAMPLE)                      # unchanged: no browser update
        self.assertEqual(sent, ["a"])
        self.store.status("a", {**self.SAMPLE, "context_window": {"used_percentage": 12.9}})
        self.assertEqual(self.store.sessions["a"]["meta"]["context"], 12)
        self.store.status("a", {**self.SAMPLE, "context_window": {"current_usage": None}, "model": "junk"})
        self.assertEqual(self.store.sessions["a"]["meta"]["context"], None)
        self.assertEqual(self.store.sessions["a"]["meta"]["model"], None)
        self.store.status("zz", self.SAMPLE)                     # unknown session: ignored

    def test_command_forwards_and_prints_users_statusline(self):
        home = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(home, "settings.json"), "w") as f:
            json.dump({"statusLine": {"type": "command", "command": "cat >/dev/null; echo my-own-status-line", "padding": 2}}, f)
        msgs, out = [], io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}), \
                mock.patch.object(client, "sock_request", lambda m, **k: msgs.append(m)), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({**self.SAMPLE, "transcript_path": "/secret"}))), \
                redirect_stdout(out):
            hook.cmd_statusline("a")
            settings = json.load(open(claude.hook_settings("sl")))
        self.assertEqual(out.getvalue(), "my-own-status-line\n")
        self.assertEqual(msgs[0]["type"], "statusline")
        self.assertNotIn("transcript_path", msgs[0]["data"])     # only the fields grovy uses are forwarded
        self.assertEqual(settings["statusLine"]["padding"], 2)
        self.assertTrue(settings["statusLine"]["command"].endswith("'statusline' 'sl'"))

    def test_no_recursion_into_our_own_statusline(self):
        home = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(home, "settings.json"), "w") as f:
            json.dump({"statusLine": {"type": "command", "command": "python3 grovy.py statusline x"}}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            self.assertIsNone(claude.user_statusline())


class TestOutdated(StoreCase):
    def test_update_offered_only_when_the_settings_changed(self):
        fp = claude.session_fingerprint("a")
        reg = {"a": {"cwd": "/a", "config": fp}, "b": {"cwd": "/b", "config": "old"}}
        with mock.patch.object(sessions, "registry", lambda: reg):
            self.store.reconcile()
            self.assertEqual((self.store.summary("a")["outdated"], self.store.summary("b")["outdated"]), (False, True))
            del reg["a"]["config"]   # started before grovy recorded it
            self.store.reconcile()
            self.assertTrue(self.store.summary("a")["outdated"])
            reg["a"]["config"] = fp
            with mock.patch.object(claude, "SESSION_ENV_VERSION", 99):   # e.g. a new variable in the environment
                self.store.reconcile()
            self.assertTrue(self.store.summary("a")["outdated"])
            del self.live["b"]       # ended: Resume, not Update
            self.store.reconcile()
            self.assertFalse(self.store.summary("b")["outdated"])


class TestOomGuard(StoreCase):
    """systemd-oomd killed whole tmux panes (Claude included) when another job ran the machine out of memory."""

    def setUp(self):
        super().setUp()
        f = self.f = type("F", (), {})()
        f.pids_now, f.cgroup, f.journal, f.moved = {}, {}, set(), []
        f.pids = lambda: dict(f.pids_now)
        f.scope = lambda pid: f.cgroup.get(pid)
        def protect(name, pid):
            f.moved.append((name, pid))
            f.cgroup[pid] = f"grovy-{name}-{pid}.scope"
            return f.cgroup[pid]
        f.protect = protect
        f.killed = lambda scopes: bool(set(scopes) & f.journal)
        self.notes = []
        self.store.notifier = type("N", (), {"send": lambda _, n, k, t: self.notes.append((n, k, t))})()
        self.store.oom = f

    def test_sessions_are_moved_once_into_an_avoid_scope(self):
        self.f.pids_now = {"a": 100, "b": 200}
        self.f.cgroup = {100: "tmux-spawn-x.scope", 200: "grovy-b-200.scope"}   # b: already shielded (restart)
        self.store.reconcile()
        self.store.reconcile()
        self.assertEqual(self.f.moved, [("a", 100)])
        self.assertEqual(self.store.sessions["a"]["scopes"], ["tmux-spawn-x.scope", "grovy-a-100.scope"])
        self.f.pids_now["a"] = 101    # resumed: a new Claude
        self.f.cgroup[101] = "tmux-spawn-y.scope"
        self.store.reconcile()
        self.assertEqual(self.f.moved[-1], ("a", 101))

    def test_oom_kill_is_reported(self):
        self.f.pids_now = {"a": 100, "b": 200}
        self.f.cgroup = {100: "tmux-spawn-x.scope", 200: "tmux-spawn-z.scope"}
        self.store.reconcile()
        self.f.journal = {"grovy-a-100.scope"}
        del self.live["a"], self.live["b"]
        self.f.pids_now = {}
        self.store.reconcile()
        types = lambda n: [e["type"] for e in self.store.events[n]]
        self.assertEqual(types("a")[-2:], ["Exited", "Killed"])
        self.assertNotIn("Killed", types("b"))              # b just ended
        self.assertEqual(self.store.sessions["a"]["activity"], "killed by the out-of-memory killer")
        self.assertEqual([(n, k) for n, k, _ in self.notes], [("a", "error")])
        self.store.reconcile()
        self.assertEqual(types("a").count("Killed"), 1)

    def test_failures_are_quiet(self):
        self.f.pids = lambda: (_ for _ in ()).throw(RuntimeError("tmux down"))
        with mock.patch("sys.stderr", io.StringIO()):
            self.store.reconcile()                           # no exception
        self.f.pids = lambda: {"a": 100}
        self.f.cgroup = {100: "tmux-spawn-x.scope"}
        self.f.protect = lambda n, p: None                   # no systemd user manager
        with mock.patch("sys.stderr", io.StringIO()):
            self.store.reconcile()
        self.assertEqual(self.store.sessions["a"]["scopes"], ["tmux-spawn-x.scope"])   # still checked at exit


class TestOomHelpers(unittest.TestCase):
    def test_proc_scope_and_journal_match(self):
        self.assertTrue(oom.proc_scope(os.getpid()) is None or oom.proc_scope(os.getpid()).endswith(".scope"))
        self.assertIsNone(oom.proc_scope(10**9))
        for line in ("tmux-spawn-9.scope: systemd-oomd killed 14 process(es) in this unit.",
                     "x.scope: Failed with result 'oom-kill'."):
            self.assertTrue(oom.OOM_LOG_RE.search(line))
        self.assertFalse(oom.OOM_LOG_RE.search("x.scope: Consumed 2min CPU time, 6.1M memory peak."))
        with mock.patch.object(subprocess, "run", side_effect=OSError("no journalctl")):
            self.assertFalse(oom.oom_killed(["x.scope"]))
            self.assertIsNone(oom.oom_protect("a", 1))


class TestSteering(StoreCase):
    """A message sent while Claude works is handed to it by the main agent's next PostToolUse hook (no typing)."""

    def statuses(self, name="a"):
        return [p["status"] for p in self.store.prompts if p["session"] == name]

    def busy(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="long job"))

    def test_steered_at_the_next_tool_call(self):
        self.busy()
        self.store.enqueue("a", "use the staging db")
        self.store.enqueue("a", "and be quick")
        self.assertEqual(self.statuses(), ["queued", "queued"])
        ctx = self.store.take_steers("a")
        self.assertIn("these 2 messages", ctx)
        self.assertTrue(ctx.endswith("use the staging db\n\n---\n\nand be quick"))
        self.assertEqual(self.statuses(), ["steered", "steered"])
        self.assertEqual([e["data"]["prompt"] for e in self.store.events["a"] if e["type"] == "Steered"],
                         ["use the staging db", "and be quick"])
        self.assertIsNone(self.store.take_steers("a"))             # handed over once
        self.store.ingest("a", ev("Stop", last_assistant_message="ok"))
        time.sleep(0.1)
        self.assertEqual(self.pasted, [])                          # never typed as well

    def test_turn_ends_before_any_tool_call(self):
        self.busy()
        self.store.enqueue("a", "one more thing")
        self.store.ingest("a", ev("Stop", last_assistant_message="done, no tools used"))
        self.wait_pasted(1)
        self.assertEqual(self.pasted, [("a", "one more thing")])    # typed as a normal message
        self.assertIsNone(self.store.take_steers("a"))

    def test_events_carry_the_send_time(self):
        """The bubble shows when the user sent a message, not when Claude got to it."""
        self.busy()
        self.store.enqueue("a", "steer me")
        created = self.store.prompts[-1]["created"]
        time.sleep(0.05)
        self.store.take_steers("a")
        self.store.ingest("a", ev("Stop", last_assistant_message="ok"))
        self.store.enqueue("a", "hello there")
        self.wait_pasted(1)
        typed = self.store.prompts[-1]["created"]
        time.sleep(0.05)
        self.store.ingest("a", ev("UserPromptSubmit", prompt="hello there"))
        sent = {e["type"]: e["data"].get("sent") for e in self.store.events["a"] if e["type"] in ("Steered", "UserPromptSubmit")}
        self.assertEqual(sent, {"Steered": created, "UserPromptSubmit": typed})
        self.store.ingest("a", ev("UserPromptSubmit", prompt="typed in the terminal"))
        self.assertNotIn("sent", self.store.events["a"][-1]["data"])

    def test_commands_and_long_messages_keep_their_place(self):
        self.busy()
        self.store.enqueue("a", "first")
        self.assertEqual(self.store.enqueue("a", "/compact")[0], 200)
        self.store.enqueue("a", "after the compaction")
        self.assertIn("this message", self.store.take_steers("a"))
        self.assertEqual(self.statuses(), ["steered", "queued", "queued"])   # nothing jumps the command
        self.assertIsNone(self.store.take_steers("a"))
        self.store.enqueue("b", "x" * (prompts.STEER_MAX + 1))
        self.assertIsNone(self.store.take_steers("b"))

    def test_socket_hands_it_to_the_main_agent_only(self):
        self.busy()
        self.store.enqueue("a", "steer me")
        replies = []
        def ask(payload):
            h = grovyd.SockHandler.__new__(grovyd.SockHandler)
            h.server = type("S", (), {"store": self.store})()
            h.rfile = io.BytesIO(json.dumps({"type": "hook", "session": "a", "event": payload,
                                             "want_steer": True}).encode() + b"\n")
            h.wfile = io.BytesIO()
            h.handle()
            replies.append(json.loads(h.wfile.getvalue() or b"null"))
        ask(ev("PostToolUse", tool_name="Bash", agent_id="sub1"))      # a subagent's tool call: not it
        ask(ev("PostToolUse", tool_name="Read"))
        self.assertIsNone(replies[0]["steer"])
        self.assertIn("steer me", replies[1]["steer"])
        self.assertEqual(self.store.sessions["a"]["state"], "working")   # the event itself was recorded

    def test_hook_adapter_outputs_the_context(self):
        out = io.StringIO()
        with mock.patch.object(client, "sock_request", lambda m, timeout=2.0, wait_reply=True:
                               {"steer": "CTX"} if m.get("want_steer") else None), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse")))), redirect_stdout(out):
            hook.cmd_hook("a")
        self.assertEqual(json.loads(out.getvalue()), {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                                              "additionalContext": "CTX"}})
        for reply in ({"steer": None}, None, OSError("down")):
            out = io.StringIO()
            def fake(m, timeout=2.0, wait_reply=True, r=reply):
                if isinstance(r, Exception):
                    raise r
                return r
            with mock.patch.object(client, "sock_request", fake), mock.patch("sys.stderr", io.StringIO()), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse")))), redirect_stdout(out):
                self.assertEqual(hook.cmd_hook("a"), 0)
            self.assertEqual(out.getvalue(), "")


class TestBackgroundTasks(StoreCase):
    """The list of background work follows Claude Code's own (payloads as captured from 2.1.325)."""
    SUB = {"agent_id": "a51", "agent_type": "general-purpose"}
    SHELL = {"id": "bre", "type": "shell", "status": "running", "description": "Sleep for 70 seconds", "command": "sleep 70"}
    AGENT = {"id": "a51", "type": "subagent", "status": "running", "description": "sleeper", "agent_type": "general-purpose"}

    def tasks(self):
        return {t["id"]: t for t in self.store.summary("a")["tasks"]}

    def test_subagent_listed_until_it_has_reported(self):
        st = lambda: self.store.sessions["a"]["state"]
        self.store.ingest("a", ev("SessionStart", source="startup"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.store.ingest("a", ev("PostToolUse", tool_name="Bash", tool_input={"command": "sleep 70",
                          "description": "Sleep for 70 seconds", "run_in_background": True},
                          tool_response={"stdout": "", "backgroundTaskId": "bre"}))
        self.store.ingest("a", ev("PostToolUse", tool_name="Agent", tool_input={"description": "sleeper",
                          "prompt": "p", "subagent_type": "general-purpose", "run_in_background": True},
                          tool_response={"isAsync": True, "status": "async_launched", "agentId": "a51",
                                         "description": "sleeper"}))
        self.store.ingest("a", ev("SubagentStart", **self.SUB))
        self.assertEqual(set(self.tasks()), {"bre", "a51"})                    # listed as soon as launched
        self.assertEqual(self.tasks()["a51"]["description"], "sleeper")
        self.store.ingest("a", ev("Stop", last_assistant_message="started", background_tasks=[self.SHELL, self.AGENT]))
        self.assertEqual(st(), "idle")                                         # Claude takes input: truthful
        self.assertEqual(self.store.events["a"][-1]["data"]["background_names"], ["Sleep for 70 seconds", "sleeper"])
        # the subagent works on: its tool calls show what it does, without making the session "working"
        self.store.ingest("a", ev("PostToolUse", **self.SUB, tool_name="Bash", tool_input={"command": "sleep 45",
                          "run_in_background": True, "description": "Wait"}, tool_response={"backgroundTaskId": "b5k"}))
        self.assertEqual(st(), "idle")
        self.assertEqual(self.tasks()["a51"]["last"], {"tool": "Bash", "arg": "sleep 45"})
        self.assertEqual(self.tasks()["a51"]["tools"], 1)
        self.assertIn("b5k", self.tasks())                                     # its own background shell
        n = len(self.store.events["a"])
        # it pauses on that shell: SubagentStop fires, but Claude Code still lists it as running
        self.store.ingest("a", ev("SubagentStop", **self.SUB, background_tasks=[self.SHELL, self.AGENT,
                          {"id": "b5k", "type": "shell", "status": "running", "description": "Wait"}]))
        self.assertIn("a51", self.tasks())                                     # not "done": it is still working
        self.assertEqual(len(self.store.events["a"]), n)                       # nothing in the conversation
        # its report: done
        self.store.ingest("a", ev("UserPromptSubmit", prompt='<agent-message from="a51">\n[Subagent hand-back] done'))
        self.assertNotIn("a51", self.tasks())
        # the shell's notification: done too
        self.store.ingest("a", ev("UserPromptSubmit", prompt="<task-notification>\n<task-id>bre</task-id>\n<status>completed"))
        self.assertNotIn("bre", self.tasks())
        self.store.ingest("a", ev("Stop", last_assistant_message="ok", background_tasks=[]))
        self.assertEqual(self.tasks(), {})
        self.assertEqual(self.store.summary("a")["background"], 0)
        # a late tool call of a finished subagent neither revives it nor touches the state
        self.store.ingest("a", ev("PostToolUse", **self.SUB, tool_name="Read", tool_input={"file_path": "/x"}))
        self.assertEqual((self.tasks(), st()), ({}, "idle"))

    def test_first_seen_time_kept_and_list_cleared_on_exit(self):
        self.store.ingest("a", ev("SessionStart", source="startup"))
        self.store.ingest("a", ev("Stop", background_tasks=[self.SHELL]))
        since = self.tasks()["bre"]["since"]
        self.store.ingest("a", ev("UserPromptSubmit", prompt="more"))
        self.store.ingest("a", ev("Stop", last_assistant_message="x", background_tasks=[self.SHELL, "junk"]))
        self.assertEqual(self.tasks()["bre"]["since"], since)                  # how long it has run, not since when re-listed
        self.assertEqual(self.store.summary("a")["background"], 2)             # malformed entries still count
        self.store.ingest("a", ev("SubagentStart", agent_id="zz", agent_type=""))   # unnamed internal agent
        self.assertNotIn("zz", self.tasks())
        self.store.ingest("a", ev("SessionEnd", reason="other"))
        self.assertEqual(self.tasks(), {})
        self.store.ingest("a", ev("SessionStart", source="startup"))
        self.assertEqual(self.tasks(), {})


class TestTurnTools(StoreCase):
    def test_stop_lists_the_turns_tools(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}))   # before any turn
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.store.ingest("a", ev("PostToolUse", tool_name="Bash", tool_input={"command": "git  status\n-s"}))
        self.store.ingest("a", ev("PostToolUse", tool_name="Read", tool_input={"file_path": "/x/y.py", "limit": 5}))
        self.store.ingest("a", ev("PostToolUse", tool_name="TodoWrite", tool_input={"todos": []}))
        self.store.ingest("a", ev("Stop", last_assistant_message="done"))
        d = self.store.events["a"][-1]["data"]
        self.assertEqual(d["tools"], [{"tool": "Bash", "arg": "git status -s"}, {"tool": "Read", "arg": "/x/y.py"},
                                      {"tool": "TodoWrite", "arg": ""}])
        self.assertEqual(d["tool_count"], 3)
        self.assertGreaterEqual(d["worked"], 0)
        self.store.ingest("a", ev("Stop", last_assistant_message="again"))   # no new turn: nothing to report
        self.assertNotIn("worked", self.store.events["a"][-1]["data"])

    def test_long_turns_are_capped(self):
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        for i in range(store.TURN_TOOLS + 5):
            self.store.ingest("a", ev("PostToolUse", tool_name="Bash", tool_input={"command": f"echo {i} " + "x" * 300}))
        self.store.ingest("a", ev("Stop", last_assistant_message="done"))
        d = self.store.events["a"][-1]["data"]
        self.assertEqual((len(d["tools"]), d["tool_count"]), (store.TURN_TOOLS, store.TURN_TOOLS + 5))
        self.assertEqual(len(d["tools"][0]["arg"]), 118)


class TestRestart(StoreCase):
    def test_restart_only_when_nothing_is_cut_off(self):
        calls = []

        class Fake:
            kill = staticmethod(lambda n: calls.append(("kill", n)))
            resume = staticmethod(lambda n: calls.append(("resume", n)) or f"resumed {n}")
        self.store.lifecycle = Fake()
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="work"))
        self.assertEqual(self.store.restart("a")[0], 409)                     # working
        self.store.ingest("a", ev("Stop", last_assistant_message="x", background_tasks=[{"id": 1}]))
        code, msg = self.store.restart("a")
        self.assertEqual(code, 409)                                           # background task running
        self.assertIn("background", msg)
        self.store.ingest("a", ev("Stop", last_assistant_message="y"))
        self.assertEqual(calls, [])
        self.assertEqual(self.store.restart("a")[0], 200)
        self.assertEqual(calls, [("kill", "a"), ("resume", "a")])


    def test_forced_update_cuts_off_and_is_recorded(self):
        calls = []

        class Fake:
            kill = staticmethod(lambda n: calls.append(("kill", n)))
            resume = staticmethod(lambda n: calls.append(("resume", n)) or f"resumed {n}")
        self.store.lifecycle = Fake()
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="long job"))
        code, msg = self.store.restart("a")
        self.assertEqual(code, 409)
        self.assertTrue(msg.startswith("not now: Claude is working"))      # the phone offers "Update anyway"
        self.assertEqual(calls, [])
        code, msg = self.store.restart("a", force=True)
        self.assertEqual(code, 200)
        self.assertTrue(msg.startswith("updated: "))
        self.assertEqual(calls, [("kill", "a"), ("resume", "a")])
        rec = [e for e in self.store.events["a"] if e["type"] == "Updated"][-1]
        self.assertEqual(rec["data"]["cut_off"], ["Claude is working"])
        del self.live["a"]
        self.store.reconcile()
        self.assertEqual(self.store.restart("a", force=True)[0], 409)       # ended: that's Resume, not Update

class TestInterrupt(StoreCase):
    def test_only_while_working(self):
        keys = []
        self.store.key = lambda n, k: keys.append((n, k))
        self.store.ingest("a", ev("SessionStart"))
        self.assertEqual(self.store.interrupt("a")[0], 409)        # idle: nothing to interrupt
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash"))
        self.assertEqual(self.store.interrupt("a")[0], 409)        # never Esc into a dialog
        self.store.perms.clear()
        self.store.sessions["a"]["state"] = "working"
        self.assertEqual(self.store.interrupt("a")[0], 200)
        self.assertEqual(keys, [("a", "Escape")])
        self.assertEqual(self.store.sessions["a"]["state"], "unknown")


class TestPromptQueue(StoreCase):
    def statuses(self, name="a"):
        return [p["status"] for p in self.store.prompts if p["session"] == name]

    def test_queue_while_busy_then_deliver_and_confirm(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="from terminal"))
        self.assertEqual(self.store.enqueue("a", "one\ntwo")[0], 200)
        self.assertEqual(self.statuses(), ["queued"])
        self.assertEqual(self.pasted, [])  # working: must not type
        self.store.ingest("a", ev("Stop", last_assistant_message="ok"))
        self.wait_pasted(1)
        self.assertEqual(self.pasted, [("a", "one\ntwo")])
        self.assertEqual(self.statuses(), ["injected"])
        self.store.ingest("a", ev("UserPromptSubmit", prompt="one\r\ntwo\n"))
        self.assertEqual(self.statuses(), ["submitted"])

    def test_receipts_typed_then_read(self):
        """A prompt shows ✓✓ once Enter is pressed, before Claude's hook confirms it (a /compact takes minutes)."""
        self.store.ingest("a", ev("SessionStart"))
        self.store.enqueue("a", "/compact")
        self.wait_pasted(1)
        p = self.store.prompts[-1]
        for _ in range(100):
            if p.get("typed"):
                break
            time.sleep(0.01)
        self.assertEqual(p["status"], "injected")
        self.assertTrue(p.get("typed"))
        self.store.ingest("a", ev("SessionStart", source="compact"))
        self.assertEqual(p["status"], "submitted")

    def test_aborted_paste_is_not_typed(self):
        def abort(*a, **k):
            raise tmuxctl.PasteAborted("stopped")
        self.store.paste = abort
        self.store.ingest("a", ev("SessionStart"))
        self.store.enqueue("a", "x")
        p = self.store.prompts[-1]
        for _ in range(100):
            if p["status"] != "injected":
                break
            time.sleep(0.01)
        self.assertEqual(p["status"], "failed")
        self.assertFalse(p.get("typed"))

    def test_wrapped_paste_confirms(self):
        """Regression: long prompts came back wrapped in <pasted_content> and showed as duplicates."""
        self.store.ingest("a", ev("SessionStart"))
        text = "Tables need formatting\n\nNo https now, so no PWA"
        self.store.enqueue("a", text)
        self.wait_pasted(1)
        self.store.ingest("a", ev("UserPromptSubmit", prompt='\n\n<pasted_content id="16f9">\nTables need formatting\n\n\nNo https now, so no PWA\n</pasted_content id="16f9">'))
        self.assertEqual(self.statuses(), ["submitted"])

    def test_never_during_permission(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash"))
        self.store.enqueue("a", "x")
        time.sleep(0.1)
        self.assertEqual(self.pasted, [])

    def test_one_in_flight_and_no_double_send(self):
        self.store.ingest("a", ev("SessionStart"))
        for i in range(3):
            self.store.enqueue("a", f"p{i}")
        self.wait_pasted(1)
        time.sleep(0.05)
        self.assertEqual(len(self.pasted), 1)
        # confirmation never arrives -> unconfirmed, not retried
        for p in self.store.prompts:
            if p["status"] == "injected":
                p["updated"] -= manage.CONFIRM_TIMEOUT + 1
        self.store.reconcile()
        self.assertEqual(self.statuses()[0], "unconfirmed")
        self.assertEqual([t for _, t in self.pasted].count("p0"), 1)

    def test_concurrent_enqueue(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="busy"))
        ts = [threading.Thread(target=self.store.enqueue, args=("a", f"m{i}")) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.statuses().count("queued"), 20)
        self.assertEqual(len({p["id"] for p in self.store.prompts}), 20)

    def test_isolation(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("b", ev("SessionStart", sid="T"))
        self.store.ingest("b", ev("UserPromptSubmit", sid="T", prompt="busy"))
        self.store.enqueue("b", "for b")
        self.store.enqueue("a", "for a")
        self.wait_pasted(1)
        time.sleep(0.05)
        self.assertEqual(self.pasted, [("a", "for a")])

    def test_force_only_in_unknown(self):
        self.store.sessions["a"]["state"] = "unknown"
        self.store.enqueue("a", "normal")
        time.sleep(0.05)
        self.assertEqual(self.pasted, [])
        self.store.cancel_prompt(self.store.prompts[0]["id"])
        self.store.enqueue("a", "forced", force=True)
        self.wait_pasted(1)
        self.assertEqual(self.pasted, [("a", "forced")])
        self.store.ingest("a", ev("UserPromptSubmit", prompt="forced"))  # confirms; now working + fresh
        _, p2 = self.store.enqueue("a", "forced2", force=True)
        time.sleep(0.05)
        self.assertEqual(len(self.pasted), 1)  # working and recently active: never
        self.store.cancel_prompt(p2["id"])
        self.store.sessions["a"]["last_event"] -= prompts.FORCE_STALE + 1
        self.store.enqueue("a", "forced3", force=True)  # silent "working" + explicit force, now
        self.wait_pasted(2)
        self.assertEqual(self.pasted[-1], ("a", "forced3"))

    def test_paste_failure_marks_failed(self):
        def boom(n, t):
            raise RuntimeError("session is not running")
        self.store.paste = boom
        self.store.ingest("a", ev("SessionStart"))
        self.store.enqueue("a", "x")
        time.sleep(0.2)
        self.assertEqual(self.statuses(), ["failed"])

    def test_validation_and_cancel(self):
        self.assertEqual(self.store.enqueue("a", "  ")[0], 400)
        self.assertEqual(self.store.enqueue("a", 5)[0], 400)
        self.assertEqual(self.store.enqueue("nope", "x")[0], 404)
        self.assertEqual(self.store.enqueue("a", "x" * (prompts.MAX_PROMPT + 1))[0], 413)
        _, p = self.store.enqueue("a", "x")  # session "starting": queued
        self.assertEqual(self.store.cancel_prompt(p["id"]), (200, "cancelled"))
        self.assertEqual(self.store.cancel_prompt(p["id"])[0], 409)



class TestLive(StoreCase):
    """Widgets that scripts in a session show and update in place (`import grovy`, `grovy progress`, ...)."""

    def lives(self, name="a"):
        return [e for e in self.store.events.get(name, ()) if e["type"] == "Live"]

    def test_update_in_place(self):
        spec = {"type": "progress", "items": [{"label": "x", "value": 1, "max": 3}]}
        code, r = self.store.live_update("a", "job", spec)
        self.assertEqual((code, r["rev"]), (200, 1))
        spec2 = {"type": "progress", "items": [{"label": "x", "value": 3, "max": 3}]}
        code, r2 = self.store.live_update("a", "job", spec2, "done", "all good")
        self.assertEqual((code, r2["id"], r2["rev"]), (200, r["id"], 2))
        [e] = self.lives()
        self.assertEqual((e["data"]["spec"], e["data"]["status"], e["data"]["message"]), (spec2, "done", "all good"))
        self.assertEqual(self.store.live_update("a", "other", spec)[0], 200)
        self.assertEqual(len(self.lives()), 2)
        self.assertEqual(self.store.live_update("b", "job", spec)[1]["rev"], 1)   # keys are per session

    def test_validation(self):
        ok = {"type": "note", "text": "hi"}
        self.assertEqual(self.store.live_update("zz", "k", ok)[0], 404)
        for key in ("", "-x", "a b", "a/b", "k" * 65, None, 3):
            self.assertEqual(self.store.live_update("a", key, ok)[0], 400, key)
        for spec in (None, [], "x", {"text": "no type"}, {"type": 3}):
            self.assertEqual(self.store.live_update("a", "k", spec)[0], 400, spec)
        self.assertEqual(self.store.live_update("a", "k", ok, "exploded")[0], 400)
        with mock.patch.object(live, "LIVE_MAX", 100):
            self.assertEqual(self.store.live_update("a", "k", {"type": "note", "text": "x" * 200})[0], 413)
        self.assertEqual(self.lives(), [])

    def test_new_cards_are_rate_limited_but_updates_are_not(self):
        with mock.patch.object(live, "LIVE_NEW_PER_MIN", 3):
            for i in range(3):
                self.assertEqual(self.store.live_update("a", f"k{i}", {"type": "note"})[0], 200)
            self.assertEqual(self.store.live_update("a", "k3", {"type": "note"})[0], 429)
            self.assertEqual(self.store.live_update("a", "k0", {"type": "note", "text": "again"})[0], 200)
            self.assertEqual(self.store.live_update("b", "k3", {"type": "note"})[0], 200)     # per session
        self.assertEqual(len(self.lives()), 3)

    def test_persisted_once_with_last_state(self):
        self.store.ingest("a", ev("UserPromptSubmit", prompt="go"))
        for v in range(5):    # running updates within LIVE_LOG_EVERY aren't each written
            self.store.live_update("a", "job", {"type": "progress", "items": [{"label": "", "value": v}]})
        with open(self.store.log_path) as f:
            self.assertEqual(sum('"Live"' in l for l in f), 1)
        self.store.flush_live()   # the reconcile loop's flush writes the latest state
        s2 = store.Store(self.dir, tmux_live=lambda: dict(self.live), paste=None)
        [e] = [e for e in s2.events["a"] if e["type"] == "Live"]
        self.assertEqual((e["data"]["rev"], e["data"]["spec"]["items"][0]["value"]), (5, 4))
        self.store.live_update("a", "job", {"type": "note", "text": "end"}, "failed", "boom")   # final: written now
        s3 = store.Store(self.dir, tmux_live=lambda: dict(self.live), paste=None)
        lives = [e for e in s3.events["a"] if e["type"] == "Live"]
        self.assertEqual([(e["data"]["status"], e["data"]["rev"]) for e in lives], [("failed", 6)])
        self.assertEqual([e["type"] for e in s3.events["a"]], ["UserPromptSubmit", "Live"])   # position kept
        # history (Load earlier) shows each card once, in its last state
        hist = [e for e in s3.history("a", before=10**9) if e["type"] == "Live"]
        self.assertEqual([e["data"]["status"] for e in hist], ["failed"])
        # and after a restart the same key still updates the same card
        s3.reconcile()
        self.assertEqual(s3.live_update("a", "job", {"type": "note"})[1]["id"], lives[0]["id"])

    def test_card_scrolled_out_of_memory_comes_back_as_new(self):
        _, r = self.store.live_update("a", "job", {"type": "note"})
        with self.store.lock:
            for _ in range(store.HISTORY):
                self.store._record("a", "Notification", {})
        _, r2 = self.store.live_update("a", "job", {"type": "note", "text": "later"})
        self.assertNotEqual(r2["id"], r["id"])
        self.assertEqual(r2["rev"], 1)

    def test_browser_refreshes_are_throttled_and_the_last_one_comes(self):
        published = []
        self.store.publish = lambda name=None: published.append(time.monotonic())
        with mock.patch.object(live, "LIVE_PUBLISH_EVERY", 0.2):
            for v in range(20):
                self.store.live_update("a", "job", {"type": "note", "text": str(v)})
            self.assertEqual(len(published), 1)
            time.sleep(0.35)
        self.assertEqual(len(published), 2)   # the trailing refresh shows the last update

    def test_follows_rename_and_goes_with_forget(self):
        _, r = self.store.live_update("a", "job", {"type": "note"})
        with self.store.lock:   # what rename does to the live index
            self.store.sessions["c"] = self.store.sessions.pop("a")
            self.store.events["c"] = self.store.events.pop("a")
            self.store.live["c"] = self.store.live.pop("a")
        self.assertEqual(self.store.live_update("c", "job", {"type": "note"})[1]["id"], r["id"])


class TestLiveClient(unittest.TestCase):
    """The library scripts use. It must never fail or block the script, and its last state must arrive."""

    def setUp(self):
        self.sent = []
        self.fail = None
        def fake(msg, timeout=2.0, wait_reply=True):
            if self.fail:
                raise self.fail
            self.sent.append(msg)
            return {"ok": {}}
        self.patches = [mock.patch.object(client, "sock_request", fake),
                        mock.patch.dict(os.environ, {"GROVY_SESSION": "a"}),
                        mock.patch.object(scriptlib, "LIVE_EVERY", 0.1),
                        mock.patch.dict(scriptlib._live, {"off_until": 0.0, "open": {}, "crash": None, "warned": set()})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def last(self, key=None):
        return [m for m in self.sent if key is None or m["key"] == key][-1]

    def test_does_nothing_outside_grovy(self):
        with mock.patch.dict(os.environ, {"GROVY_SESSION": "", "CCS_SESSION": ""}):
            self.assertEqual(list(grovy.track(range(3), "t")), [0, 1, 2])
            grovy.note("x")
            p = grovy.progress("t", 3)
            p.set(2)
            p.done()
            print("x", file=grovy.log("l"))
        self.assertEqual(self.sent, [])

    def test_track(self):
        self.assertEqual(list(grovy.track(["a", "b", "c"], "Converting")), ["a", "b", "c"])
        m = self.last()
        self.assertEqual((m["type"], m["session"], m["status"]), ("live", "a", "done"))
        self.assertEqual(m["spec"]["items"], [{"label": "", "value": 3, "max": 3}])
        self.assertEqual(m["spec"]["title"], "Converting")
        self.assertEqual(self.sent[0]["spec"]["items"][0]["value"], 0)   # shown at once
        self.assertEqual(len({m["key"] for m in self.sent}), 1)

    def test_unknown_total_and_early_exit(self):
        for x in grovy.track(iter(range(10)), "gen"):
            if x == 4:
                break
        import gc
        gc.collect()
        m = self.last()
        self.assertEqual((m["status"], m["spec"]["items"][0]["value"]), ("stopped", 4))
        self.assertTrue(m["spec"]["items"][0]["indeterminate"])

    def test_updates_are_throttled_but_the_last_one_arrives(self):
        p = grovy.progress("t", total=1000)
        for i in range(1000):
            p.set(i + 1, note=f"item {i}")
        self.assertLess(len(self.sent), 10)
        time.sleep(0.4)   # no more calls: the background thread sends the last state
        m = self.last()
        self.assertEqual((m["status"], m["spec"]["items"][0]["value"], m["spec"]["items"][0]["note"]),
                         ("running", 1000, "item 999"))
        p.done()
        self.assertEqual(self.last()["status"], "done")
        n = len(self.sent)
        p.set(5)   # after done: ignored
        time.sleep(0.3)
        self.assertEqual(len(self.sent), n)

    def test_exception_marks_failed(self):
        with self.assertRaises(ZeroDivisionError):
            with grovy.progress("t", 3) as p:
                p.advance()
                1 / 0
        m = self.last()
        self.assertEqual(m["status"], "failed")
        self.assertIn("ZeroDivisionError", m["message"])

    def test_left_open_at_exit(self):
        p = grovy.progress("t", 3)
        q = grovy.progress("u", 2)
        q.set(2)
        scriptlib._live_exit()
        self.assertEqual(self.last(p.key)["status"], "stopped")
        self.assertEqual(self.last(q.key)["status"], "done")   # complete: just never closed
        r = grovy.show({"type": "stats", "items": []})
        scriptlib._live["crash"] = "KeyError: 'x'"
        scriptlib._live_exit()
        self.assertEqual((self.last(r.key)["status"], self.last(r.key)["message"]),
                         ("failed", "the script crashed: KeyError: 'x'"))

    def test_log_lines(self):
        lg = grovy.log("build", lines=3)
        lg.write("one\ntw")
        lg.write("o\r\n\x1b[31mred\x1b[0m\nprogress 10%\rprogress 90%\npart")
        print("x", "y", file=lg)
        lg.done()
        self.assertEqual(self.last()["spec"], {"type": "log", "title": "build", "lines": ["red", "progress 90%", "partx y"]})

    def test_note_and_show(self):
        grovy.note("**hi**", tone="ok")
        self.assertEqual((self.last()["spec"]["text"], self.last()["status"]), ("**hi**", "done"))
        w = grovy.show({"type": "stats", "items": [{"label": "a", "value": 1}]}, key="mystats")
        w.update({"type": "stats", "items": [{"label": "a", "value": 2}]})
        w.done()
        self.assertEqual((self.last()["key"], self.last()["spec"]["items"][0]["value"]), ("mystats", 2))

    def test_daemon_down_costs_one_try(self):
        self.fail = OSError("down")
        calls = []
        orig = client.sock_request
        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        with mock.patch.object(client, "sock_request", counting):
            p = grovy.progress("t", 100)
            for i in range(100):
                p.set(i)
                time.sleep(0.002)
            p.done()
            grovy.note("x")
        self.assertEqual(len(calls), 1)

    def test_errors_are_reported_once(self):
        with mock.patch.object(client, "sock_request", lambda *a, **k: {"error": "too many"}), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            grovy.note("a")
            grovy.note("b")
        self.assertEqual(err.getvalue().count("too many"), 1)

    def test_guide_spells_out_the_path_for_old_sessions(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": claude.session_pythonpath()}):
            self.assertIn("(on PYTHONPATH;", claude.widget_guide())
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/elsewhere"}):
            self.assertIn(f"sys.path.insert(0, {core.HERE!r})", claude.widget_guide())
            self.assertEqual(claude.session_pythonpath(), "/elsewhere" + os.pathsep + core.HERE)


class TestLiveEndToEnd(unittest.TestCase):
    """The real socket path: the CLI in a subprocess talking to a real SockServer."""

    def setUp(self):
        self.sdir = tempfile.mkdtemp(dir=TMP)
        self.reg = mock.patch.object(sessions, "registry", lambda: {"a": {"cwd": "/a"}})
        self.reg.start()
        self.store = store.Store(self.sdir, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.store.reconcile()
        self.env = dict(os.environ, GROVY_STATE_DIR=self.sdir, GROVY_SESSION="a")
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": self.sdir}):
            self.sock = grovyd.SockServer(core.sock_path(), grovyd.SockHandler)
        self.sock.store = self.store
        threading.Thread(target=self.sock.serve_forever, daemon=True).start()

    def tearDown(self):
        self.sock.shutdown()
        self.sock.server_close()
        self.reg.stop()

    def run_cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, os.path.join(ROOT, "grovy.py"), *args], input=stdin, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def lives(self):
        return [e["data"] for e in self.store.events.get("a", ()) if e["type"] == "Live"]

    def test_tail_passes_output_through(self):
        text = "".join(f"line {i}\n" for i in range(100))
        r = self.run_cli("tail", "--title", "make", "--lines", "5", stdin=text)
        self.assertEqual((r.returncode, r.stdout), (0, text))
        [d] = self.lives()
        self.assertEqual((d["status"], d["spec"]["title"], d["spec"]["lines"]),
                         ("done", "make", [f"line {i}" for i in range(95, 100)]))

    def test_progress_note_show(self):
        self.assertEqual(self.run_cli("progress", "job", "3", "--max", "10", "--title", "T").returncode, 0)
        self.assertEqual(self.run_cli("progress", "job", "10", "--max", "10", "--done").returncode, 0)
        self.assertEqual(self.run_cli("note", "hello", "world", "--tone", "ok").returncode, 0)
        self.assertEqual(self.run_cli("show", "-", stdin='{"type":"stats","items":[]}').returncode, 0)
        r = self.run_cli("show", "{nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not valid JSON", r.stderr)
        job, note, stats = self.lives()
        self.assertEqual((job["status"], job["rev"], job["spec"]["items"][0]["value"]), ("done", 2, 10))
        self.assertEqual((note["spec"]["text"], note["status"]), ("hello world", "done"))
        self.assertEqual(stats["status"], "done")
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "grovy.py"), "progress", "j", "1"],
                            env=dict(self.env, GROVY_SESSION="zz"), capture_output=True, text=True, timeout=20)
        self.assertEqual(r2.returncode, 0)   # a widget problem never fails the caller
        self.assertIn("unknown session", r2.stderr)

    def test_library_in_a_script(self):
        script = ("import grovy\nfor i in grovy.track(range(50), 'Steps'):\n    pass\n"
                  "p = grovy.progress('Left open', total=5)\np.set(2)\n")
        r = subprocess.run([sys.executable, "-c", script], env=dict(self.env, PYTHONPATH=ROOT),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        steps, left = self.lives()
        self.assertEqual((steps["status"], steps["spec"]["items"][0]["value"]), ("done", 50))
        self.assertEqual((left["status"], left["spec"]["items"][0]["value"]), ("stopped", 2))


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = mock.patch.object(sessions, "registry", lambda: {})
        cls.reg.start()
        cls.store = store.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        cls.store.reconcile()
        cls.httpd = grovyd.HttpServer(("127.0.0.1", 0), grovyd.Http)
        cls.httpd.store, cls.httpd.token = cls.store, ""
        cls.httpd.page = grovyd.load_index()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.stopping = True
        cls.httpd.shutdown()
        cls.reg.stop()

    def req(self, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if data is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_index_headers(self):
        code, body, h = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("sha256-", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_server_time_for_clock_offset(self):
        """Regression: the phone's clock ran behind the server's, so the typing timer showed "-1:-1"."""
        for path in ("/api/sessions", "/api/sessions/a"):
            code, body, _ = self.req(path)
            self.assertEqual(code, 200)
            self.assertAlmostEqual(json.loads(body)["now"], time.time(), delta=5)

    def test_page_is_sandboxed(self):
        _, p = self.store.publish_page("a", "r", "T", "<!doctype html><script>1</script>")
        code, body, h = self.req("/p/" + p["id"])
        self.assertEqual(code, 200)
        csp = h["Content-Security-Policy"]
        self.assertTrue(csp.startswith("sandbox allow-scripts"))
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("img-src data: blob:;", csp)
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")
        self.assertTrue(body.startswith(b"<!doctype html>" + pages.PAGE_BRIDGE))
        self.assertIn("frame-src 'self'", self.req("/")[2]["Content-Security-Policy"])
        self.assertEqual([x["id"] for x in json.loads(self.req("/api/pages")[1])["pages"]], [p["id"]])
        self.assertEqual(self.req("/p/" + "0" * 32)[0], 404)
        self.assertEqual(self.req("/p/../state.json")[0], 404)
        self.assertEqual(self.req(f"/api/pages/{p['id']}/delete", {})[0], 200)
        self.assertEqual(self.req("/p/" + p["id"])[0], 404)

    def test_shared_page_link(self):
        _, p = self.store.publish_page("a", "shr", "T", "<!doctype html><p>hi")
        code, res = self.store.share_page(p["id"], True)
        tok = res["share"]
        self.assertRegex(tok, r"^[0-9a-f]{32}$")
        self.assertNotEqual(tok, p["id"])
        with mock.patch.object(self.httpd, "token", "secret"):
            self.assertEqual(self.req("/p/" + p["id"])[0], 401)             # the normal route still needs the token
            code, body, h = self.req("/s/" + tok)                           # the share link doesn't
            self.assertEqual(code, 200)
            self.assertEqual(body, b"<!doctype html><p>hi")                 # no grovy bridge on public copies
            self.assertTrue(h["Content-Security-Policy"].startswith("sandbox allow-scripts"))
            self.assertEqual(h["Referrer-Policy"], "no-referrer")
            self.assertEqual(self.req("/s/" + "0" * 32)[0], 404)
            self.assertEqual(self.req("/api/pages")[0], 401)                # grants nothing else
            self.store.publish_page("a", "shr", "T", "v2")                  # republish keeps the link
            self.assertEqual(self.req("/s/" + tok)[1], b"v2")
            self.store.share_page(p["id"], False)
            self.assertEqual(self.req("/s/" + tok)[0], 404)                 # revoked
            self.assertNotEqual(self.store.share_page(p["id"], True)[1]["share"], tok)   # new link
        self.assertEqual(self.req(f"/api/pages/{p['id']}/share", {"shared": "yes"})[0], 400)
        self.assertEqual(self.req(f"/api/pages/{p['id']}/share", {"shared": False})[0], 200)

    def upload(self, name, data, filename="x.txt", ctype="application/octet-stream"):
        return self.req(f"/api/sessions/{name}/attachments", raw=data,
                        headers={"Content-Type": ctype, "X-Filename": urllib.parse.quote(filename)})

    def test_attachment_upload(self):
        top = os.path.join(tempfile.mkdtemp(dir=TMP), "grovy-u", "attachments")
        with mock.patch.object(core, "attachments_dir", lambda: top):
            code, body, _ = self.upload("a", b"\x89PNG data", "../My photo (1).png")
            self.assertEqual(code, 200, body)
            res = json.loads(body)
            self.assertEqual(os.path.dirname(res["path"]), os.path.join(top, "a"))
            self.assertTrue(os.path.basename(res["path"]).endswith("-My_photo_1_.png"))
            self.assertEqual(res["size"], 9)
            with open(res["path"], "rb") as f:
                self.assertEqual(f.read(), b"\x89PNG data")
            self.assertEqual(os.stat(res["path"]).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(top)).st_mode & 0o777, 0o700)
            self.assertEqual(self.upload("a", b"x", ctype="application/json")[0], 415)
            self.assertEqual(self.upload("nope", b"x")[0], 404)
            self.assertEqual(self.upload("a", b"")[0], 400)
            with mock.patch.object(uploads, "MAX_UPLOAD", 4):
                self.assertEqual(self.upload("a", b"12345")[0], 413)
            with mock.patch.object(self.httpd, "token", "secret"):
                self.assertEqual(self.upload("a", b"x")[0], 401)
        self.assertEqual(sorted(os.listdir(os.path.join(top, "a"))), [os.path.basename(res["path"])])

    def test_attachment_thumbnails(self):
        top = os.path.join(tempfile.mkdtemp(dir=TMP), "grovy-t", "attachments")
        with mock.patch.object(core, "attachments_dir", lambda: top):
            img = json.loads(self.upload("a", b"\x89PNG", "p.png")[1])["path"]
            txt = json.loads(self.upload("a", b"secret", "notes.txt")[1])["path"]
            code, body, h = self.req("/api/attachments/a/" + os.path.basename(img))
            self.assertEqual((code, body, h["Content-Type"]), (200, b"\x89PNG", "image/png"))
            self.assertEqual(h["Content-Security-Policy"], "default-src 'none'")
            self.assertEqual(self.req("/api/attachments/a/" + os.path.basename(txt))[0], 404)   # images only
            self.assertEqual(self.req("/api/attachments/a/..%2F..%2Fstate.png")[0], 404)
            outside = os.path.join(TMP, "secret.png")
            open(outside, "wb").write(b"no")
            os.symlink(outside, os.path.join(top, "a", "20260101-000000-abcd-link.png"))
            self.assertEqual(self.req("/api/attachments/a/20260101-000000-abcd-link.png")[0], 404)   # no symlinks
            with mock.patch.object(self.httpd, "token", "secret"):
                self.assertEqual(self.req("/api/attachments/a/" + os.path.basename(img))[0], 401)

    def test_image_widget_files(self):
        src = os.path.join(TMP, "plot.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nrest")
        sd = self.store.dir
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": sd, "GROVY_SESSION": "a"}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(grovy.main(["image", src, "--caption", "Loss"]), 0)
        block = out.getvalue()
        self.assertTrue(block.startswith("```widget\n") and block.endswith("\n```\n"))
        spec = json.loads(block.split("\n")[1])
        self.assertEqual(spec["type"], "image")
        self.assertEqual(spec["caption"], "Loss")
        self.assertRegex(spec["src"], r"^a/\d{8}-\d{6}-[0-9a-f]{4}-plot\.png$")
        code, body, h = self.req("/api/images/" + spec["src"])
        self.assertEqual((code, body, h["Content-Type"]), (200, b"\x89PNG\r\n\x1a\nrest", "image/png"))
        self.assertEqual(self.req("/api/images/a/..%2Fstate.json")[0], 404)
        with mock.patch.object(self.httpd, "token", "secret"):
            self.assertEqual(self.req("/api/images/" + spec["src"])[0], 401)

    def test_image_command_refuses_non_images(self):
        fake = os.path.join(TMP, "notes.png")
        with open(fake, "wb") as f:
            f.write(b"just text")
        txt = os.path.join(TMP, "notes.txt")
        open(txt, "w").close()
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": self.store.dir, "GROVY_SESSION": "a"}), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(grovy.main(["image", fake]), 2)      # the extension lies
            self.assertEqual(grovy.main(["image", txt]), 2)       # not an image type
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": self.store.dir}), mock.patch("sys.stderr", io.StringIO()):
            os.environ.pop("GROVY_SESSION", None); os.environ.pop("CCS_SESSION", None)
            self.assertEqual(grovy.main(["image", fake]), 2)      # no session

    def test_attachments_never_written_through_a_planted_dir(self):
        """Anyone can create names in /tmp: a symlink or someone else's open directory there must be refused."""
        base = tempfile.mkdtemp(dir=TMP)
        elsewhere = tempfile.mkdtemp(dir=TMP)
        os.symlink(elsewhere, os.path.join(base, "grovy-s"))
        os.makedirs(os.path.join(base, "grovy-o"), mode=0o777)
        os.chmod(os.path.join(base, "grovy-o"), 0o777)
        for d in ("grovy-s", "grovy-o"):
            top = os.path.join(base, d, "attachments")
            with mock.patch.object(core, "attachments_dir", lambda: top):
                code, body, _ = self.upload("a", b"secret")
                self.assertEqual(code, 500, body)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_old_attachments_pruned(self):
        top = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(top, "s1")); os.makedirs(os.path.join(top, "s2"))
        old, new = os.path.join(top, "s1", "old"), os.path.join(top, "s2", "new")
        for p in (old, new):
            open(p, "w").close()
        os.utime(old, (time.time() - uploads.ATTACH_TTL - 60,) * 2)
        uploads.prune_attachments(top)
        self.assertEqual(sorted(os.listdir(top)), ["s2"])   # the emptied folder goes too
        self.assertTrue(os.path.exists(new))

    def test_idle_connections_cannot_lock_out_the_phone(self):
        class Small(grovyd.HttpServer):
            MAX_CONNECTIONS = 4
        srv = Small(("127.0.0.1", 0), grovyd.Http)
        srv.store, srv.token = self.store, ""
        srv.page = grovyd.load_index()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(6)]   # no token, no request
            time.sleep(0.8)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=5) as r:
                self.assertEqual(r.status, 200)
            [x.close() for x in idle]
        finally:
            srv.stopping = True
            srv.shutdown()

    def test_unknown_host_gets_a_readable_page(self):
        code, body, h = self.req("/", headers={"Host": "box.<b>tail</b>.ts.net:7333"})
        self.assertEqual(code, 403)
        self.assertIn("text/html", h["Content-Type"])
        self.assertIn(b"GROVY_HOSTS=box.&lt;b&gt;tail&lt;/b&gt;.ts.net", body)
        self.assertNotIn(b"<b>tail", body)

    def test_public_assets_are_an_allowlist(self):
        with mock.patch.object(self.httpd, "token", "secret"):
            code, body, h = self.req("/assets/icon-192.png")                 # no token needed: nothing private
            self.assertEqual((code, h["Content-Type"]), (200, "image/png"))
            self.assertTrue(body.startswith(b"\x89PNG"))
            self.assertEqual(h["Cache-Control"], "max-age=86400")
            code, _, h = self.req("/assets/backdrop.svg")
            self.assertEqual(code, 200)
            self.assertIn("default-src 'none'", h["Content-Security-Policy"])  # an SVG opened directly can't run script
            for bad in ("/assets/README.md", "/assets/../grovy.py", "/assets/%2e%2e/token", "/assets/"):
                self.assertIn(self.req(bad)[0], (401, 404), bad)
            self.assertEqual(json.loads(self.req("/manifest.webmanifest")[1])["name"], "grovy")
            self.assertEqual(self.req("/api/sessions")[0], 401)
        self.assertIn("manifest-src 'self'", self.req("/")[2]["Content-Security-Policy"])

    def test_slowloris_deadline_and_connection_cap(self):
        class Small(grovyd.HttpServer):
            MAX_CONNECTIONS = 3
        with mock.patch.object(grovyd.Http, "HEADER_DEADLINE", 0.5):
            srv = Small(("127.0.0.1", 0), grovyd.Http)
            srv.store, srv.token = self.store, ""
            srv.page = grovyd.load_index()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.server_address[1]
            try:
                s = socket.create_connection(("127.0.0.1", port))
                s.sendall(b"GET / HTTP/1.1\r\n")       # never finishes its headers
                s.settimeout(3)
                t0 = time.time()
                self.assertEqual(s.recv(1), b"")         # cut by the deadline, not left open
                self.assertLess(time.time() - t0, 2.5)
                s.close()
                time.sleep(0.2)
                held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
                extra = socket.create_connection(("127.0.0.1", port))
                extra.settimeout(2)
                self.assertEqual(extra.recv(1), b"")     # over the cap: refused at once
                for c in held + [extra]:
                    c.close()
            finally:
                srv.shutdown()
                srv.server_close()

    def test_client_error_report_is_authenticated_and_rate_limited(self):
        self.httpd.token = "s3cret"
        try:
            self.assertEqual(self.req("/api/client-error", {"message": "x"})[0], 401)
        finally:
            self.httpd.token = ""
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(self.req("/api/client-error", {"message": "boom", "stack": "a\nb"})[0], 200)
            self.assertEqual(self.req("/api/client-error", {"message": "again"})[0], 200)
        self.assertIn("CLIENT ERROR", err.getvalue())
        self.assertIn("boom", err.getvalue())
        self.assertNotIn("again", err.getvalue())                 # rate-limited

    def test_responses_close_the_connection(self):
        code, _, h = self.req("/api/sessions")
        self.assertEqual(h["Connection"], "close")

    def test_old_event_streams_from_same_client_are_closed(self):
        streams = [urllib.request.urlopen(self.base + "/api/events", timeout=5) for _ in range(grovyd.SSE_PER_CLIENT + 2)]
        for r in streams:
            r.readline()
        time.sleep(0.3)
        mine = [s for s in self.store.subscribers if s.client == "127.0.0.1"]
        self.assertEqual(len(mine), grovyd.SSE_PER_CLIENT)
        self.assertEqual(streams[0].read(), b'data: {"hello":1}\n\n')   # read() returns: the server ended it
        for r in streams:
            r.close()

    def test_retried_action_runs_once(self):
        before = len(self.store.prompts)
        hdr = {"X-Request-Id": "a1b2c3d4e5f6a7b8c9d0"}
        r1 = self.req("/api/sessions/a/prompts", {"text": "once"}, hdr)
        r2 = self.req("/api/sessions/a/prompts", {"text": "once"}, hdr)       # network retry
        self.assertEqual((r1[0], r2[0]), (200, 200))
        self.assertEqual(json.loads(r1[1])["id"], json.loads(r2[1])["id"])   # same outcome returned
        self.assertEqual(len(self.store.prompts), before + 1)                 # acted once
        self.assertEqual(self.req("/api/sessions/a/prompts", {"text": "x"}, {"X-Request-Id": "bad id!"})[0], 200)

    def test_concurrent_duplicate_waits_for_the_first(self):
        gate, calls = threading.Event(), []
        real = self.store.enqueue
        def slow(*a):
            calls.append(1)
            gate.wait(2)
            return real(*a)
        with mock.patch.object(self.store, "enqueue", slow):
            hdr = {"X-Request-Id": "f" * 20}
            out = []
            ts = [threading.Thread(target=lambda: out.append(self.req("/api/sessions/a/prompts", {"text": "dup"}, hdr)))
                  for _ in range(2)]
            [t.start() for t in ts]
            time.sleep(0.3)
            gate.set()
            [t.join(5) for t in ts]
        self.assertEqual(len(calls), 1)
        self.assertEqual([c for c, _, _ in out], [200, 200])

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.req("/api/sessions", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.req("/api/sessions/a/prompts", {"text": "x"},
                                  {"Host": "evil.example", "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/sessions", headers={"Host": "localhost:1"})[0], 200)
        self.assertEqual(self.req("/api/sessions", headers={"Host": "[::1]:1"})[0], 200)

    def test_api(self):
        self.assertEqual(self.req("/api/sessions")[0], 200)
        self.assertEqual(self.req("/api/sessions/a")[0], 200)
        self.assertEqual(self.req("/api/sessions/zz")[0], 404)
        self.assertEqual(self.req("/api/sessions/..%2F..%2Fetc")[0], 404)
        self.assertEqual(self.req("/etc/passwd")[0], 404)

    def test_post_validation(self):
        p = "/api/sessions/a/prompts"
        self.assertEqual(self.req(p, raw=b"{bad")[0], 400)
        self.assertEqual(self.req(p, raw=b"[1]")[0], 400)
        self.assertEqual(self.req(p, {"text": "x"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req(p, {"text": "x"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/permissions/notanid/decision", {"decision": "allow"})[0], 404)
        self.assertEqual(self.req(p, raw=b"x" * (grovyd.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.req(p, {"text": "ok"})[0], 200)

    def test_login_page_and_enrollment(self):
        self.httpd.token = "s3cret"
        try:
            code, body, _ = self.req("/")
            self.assertEqual(code, 401)
            self.assertIn(b"grovy url", body)
            self.httpd.enforce = False          # CCS_AUTH=off: open, but ?token= still sets the cookie
            self.assertEqual(self.req("/api/sessions")[0], 200)
            conn = __import__("http.client").client.HTTPConnection("127.0.0.1", self.httpd.server_address[1])
            conn.request("GET", "/?token=s3cret")
            r = conn.getresponse()
            self.assertEqual(r.status, 303)
            self.assertIn("grovy_token=s3cret", r.getheader("Set-Cookie"))
            self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
            conn.close()
        finally:
            self.httpd.token, self.httpd.enforce = "", True

    def test_token(self):
        self.httpd.token = "s3cret"
        try:
            self.assertEqual(self.req("/api/sessions")[0], 401)
            self.assertEqual(self.req("/api/sessions", headers={"Authorization": "Bearer s3cret"})[0], 200)
            self.assertEqual(self.req("/api/sessions", headers={"Cookie": "grovy_token=s3cret"})[0], 200)
            self.assertEqual(self.req("/api/sessions", headers={"Cookie": "grovy_token=wrong"})[0], 401)
            # phones logged in before the rename still carry the old cookie name
            self.assertEqual(self.req("/api/sessions", headers={"Cookie": "ccs_token=s3cret"})[0], 200)
        finally:
            self.httpd.token = ""

    def test_login_link_only_for_a_logged_in_page(self):
        self.httpd.token = "s3cret"
        try:
            self.assertEqual(self.req("/api/login-link", {})[0], 401)
            ok = {"Cookie": "grovy_token=s3cret"}
            code, body, _ = self.req("/api/login-link", {}, headers=ok)
            self.assertEqual((code, json.loads(body)["token"]), (200, "s3cret"))
            self.assertEqual(self.req("/api/login-link", {}, headers={**ok, "Origin": "http://evil.example"})[0], 403)
            self.assertEqual(self.req("/api/login-link", headers=ok)[0], 404)   # GET: not an endpoint
            self.assertEqual(self.req("/api/login-link", raw=b"{}", headers={**ok, "Content-Type": "text/plain"})[0], 415)
        finally:
            self.httpd.token = ""

    def test_sse(self):
        r = urllib.request.urlopen(self.base + "/api/events", timeout=5)
        self.assertEqual(r.readline(), b"retry: 3000\n")
        r.readline(); r.readline()
        self.store.publish("a")
        self.assertIn(b'"session": "a"', r.readline())
        r.close()


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestTmuxIntegration(unittest.TestCase):
    """Real tmux, fake claude, private tmux server (-L) so the user's server is untouched."""

    @classmethod
    def setUpClass(cls):
        cls.fake = os.path.join(TMP, "fakeclaude")
        with open(cls.fake, "w") as f:
            f.write("#!/bin/bash\necho READY\nwhile IFS= read -r l; do printf 'GOT[%s]\\n' \"$l\"; done\n")
        os.chmod(cls.fake, 0o755)
        cls.sock = "grovy-test-%d" % os.getpid()
        real = tmuxctl.tmux
        cls.patches = [mock.patch.object(tmuxctl, "tmux", lambda *a, **k: real("-L", cls.sock, *a, **k)),
                       mock.patch.dict(os.environ, {"GROVY_CLAUDE": cls.fake})]
        [p.start() for p in cls.patches]
        cls.proj = os.path.join(TMP, "dir with spaces")
        os.makedirs(cls.proj, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "-L", cls.sock, "kill-server"], capture_output=True)
        [p.stop() for p in cls.patches]

    def wait_for(self, name, needle, t=5):
        end = time.time() + t
        while time.time() < end:
            out = tmuxctl.capture(name)
            if needle in out:
                return out
            time.sleep(0.05)
        self.fail(f"{needle!r} not seen in:\n{tmuxctl.capture(name)}")

    def test_typing_stops_without_enter_when_aborted(self):
        sessions.start_session("ab1", self.proj)
        self.wait_for("ab1", "READY")
        calls = []
        def abort():
            calls.append(1)
            return "Claude opened a permission request" if len(calls) >= 2 else None
        with self.assertRaises(tmuxctl.PasteAborted):
            tmuxctl.paste_prompt("ab1", "x" * (tmuxctl.TYPE_CHUNK * 4), abort=abort)
        time.sleep(0.5)
        self.assertNotIn("GOT[", tmuxctl.capture("ab1"))   # Enter never pressed
        self.assertEqual(len(calls), 2)                    # stopped right after the chunk where it changed
        sessions.kill_session("ab1")

    def test_session_can_import_grovy(self):
        sessions.start_session("py1", self.proj)
        self.assertEqual(sessions.registry()["py1"]["config"], claude.session_fingerprint("py1"))
        out = tmuxctl.tmux("show-environment", "-t", "=grovy-py1", "PYTHONPATH", check=False).stdout
        self.assertIn(core.HERE, out.strip().split("=", 1)[1].split(os.pathsep))
        sessions.kill_session("py1")

    def test_widgets_opt_out_reaches_the_session(self):
        with mock.patch.dict(os.environ, {"GROVY_WIDGETS": "0"}):
            sessions.start_session("ow1", self.proj)
        out = tmuxctl.tmux("show-environment", "-t", "=grovy-ow1", "GROVY_WIDGETS", check=False).stdout
        self.assertEqual(out.strip(), "GROVY_WIDGETS=0")
        sessions.kill_session("ow1")

    def test_config_dir_reaches_the_session(self):
        """The hooks read instructions.md from the same config folder as the daemon that started the session."""
        cfg = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": cfg}):
            sessions.start_session("cd1", self.proj)
        out = tmuxctl.tmux("show-environment", "-t", "=grovy-cd1", "GROVY_CONFIG_DIR", check=False).stdout
        self.assertEqual(out.strip(), "GROVY_CONFIG_DIR=" + cfg)
        sessions.kill_session("cd1")

    def test_lifecycle_and_literal_delivery(self):
        sessions.start_session("it1", self.proj)
        sessions.start_session("it2", self.proj)
        with self.assertRaises(ValueError):
            sessions.start_session("it1", self.proj)
        with self.assertRaises(ValueError):
            sessions.start_session("bad name", self.proj)
        self.assertTrue({"it1", "it2"} <= set(tmuxctl.tmux_managed()))
        # an unrelated session that merely looks like ours is never considered managed
        tmuxctl.tmux("new-session", "-d", "-s", "grovy-impostor", "sleep 60")
        self.assertNotIn("impostor", tmuxctl.tmux_managed())
        with self.assertRaises(ValueError):
            sessions.kill_session("impostor")
        self.wait_for("it1", "READY")
        nasty = "a 'b' \"c\" $(echo pwned) `id` ; \\ ünï 🚀 %s"
        tmuxctl.paste_prompt("it1", nasty)
        self.wait_for("it1", f"GOT[{nasty}]")
        tmuxctl.paste_prompt("it1", "l1\nl2")
        self.wait_for("it1", "GOT[l2]")
        # typed-path edge cases: tmux ';' separators, option-looking text, chunk boundaries, tabs
        semi = "x" * 63 + ";"            # a chunk that ends exactly in ';'
        tmuxctl.paste_prompt("it1", semi + " ; \\; -t foo")
        self.wait_for("it1", "GOT[" + semi + " ; \\; -t foo]")
        tmuxctl.paste_prompt("it1", "-n --help")
        self.wait_for("it1", "GOT[-n --help]")
        long = "".join(chr(0x41 + i % 26) for i in range(300))
        tmuxctl.paste_prompt("it1", long + "\tend")
        self.wait_for("it1", "GOT[" + long + "    end]")
        tmuxctl.paste_prompt("it1", "/literal slash stays literal")  # paste path
        self.wait_for("it1", "GOT[/literal slash stays literal]")
        tmuxctl.paste_prompt("it1", "!echo a\x1b[201~INJECTED\x1b[200~")      # can't break out of the paste
        self.wait_for("it1", "GOT[!echo a[201~INJECTED[200~]")
        self.assertNotIn("GOT", tmuxctl.capture("it2"))  # isolation
        reg = sessions.registry()
        self.assertEqual(reg["it1"]["cwd"], os.path.realpath(self.proj))
        sessions.kill_session("it1")
        self.assertNotIn("it1", tmuxctl.tmux_managed())
        self.assertIn("it2", tmuxctl.tmux_managed())
        with self.assertRaises(RuntimeError):
            tmuxctl.paste_prompt("it1", "into the void")
        sessions.kill_session("it2")
        self.assertEqual(tmuxctl.tmux("has-session", "-t", "=grovy-impostor", check=False).returncode, 0)

    def test_other_state_dirs_sessions_are_invisible(self):
        """Regression: a test daemon's session ("shadow t3") showed up in the live daemon's list."""
        sessions.start_session("mine", self.proj)
        other = os.path.join(TMP, "other-state")
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": other}):
            sessions.start_session("theirs", self.proj)
            self.assertEqual(set(tmuxctl.tmux_managed()), {"theirs"})
            sessions.kill_session("theirs")
        self.assertNotIn("theirs", tmuxctl.tmux_managed())
        self.assertIn("mine", tmuxctl.tmux_managed())
        sessions.kill_session("mine")

    def test_live_rename(self):
        sessions.start_session("rn1", self.proj)
        self.wait_for("rn1", "READY")
        self.assertTrue(sessions.rename_session("rn1", "rn2"))
        self.assertEqual(set(tmuxctl.tmux_managed()) & {"rn1", "rn2"}, {"rn2"})
        reg = sessions.registry()
        self.assertEqual(reg["rn2"]["aliases"], ["rn1"])
        self.assertNotIn("rn1", reg)
        for f in ("rn1.json", "rn2.json"):   # the running Claude's file and the one for its next start
            cmd = json.load(open(os.path.join(core.state_dir(), "hooks", f)))["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertTrue(cmd.endswith("'hook' 'rn2'"), cmd)
        tmuxctl.paste_prompt("rn2", "still typeable")              # pane ownership check passes after rename
        self.wait_for("rn2", "GOT[still typeable]")
        with self.assertRaises(ValueError):
            sessions.start_session("rn1", self.proj)                # old name is reserved as an alias
        sessions.kill_session("rn2")

    def test_hook_env_reaches_session(self):
        sessions.start_session("it3", self.proj)
        out = tmuxctl.tmux("show-environment", "-t", "=grovy-it3", "GROVY_SESSION").stdout
        self.assertEqual(out.strip(), "GROVY_SESSION=it3")
        sessions.kill_session("it3")


def load_spec_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("grovy_test_spec", os.path.join(ROOT, "plugins", "spec.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPEC_INDEX = """---
spec: 1
title: Demo
status: active
next: {R: 6, D: 3, G: 2}
parts: [decisions.md]
---

# Demo

## Done so far

Login links expire after a year {#R1}. Example syntax: `{#R99}` is not an ID.

## Roadmap

- [ ] Write the parser {#R2}
- [~] @s1 Draw the view, a long item
      that goes on {#R3}
- [x] Lint {#R4}
- [-] Dropped: too slow {#R5}

```
- [ ] not an item {#R42}
```

## Glossary

**Spec** {#G1}: a folder with an index.
"""
SPEC_DECISIONS = """# Decisions

- {#D1} 2026-11-17: Markdown in the repo. Why: agents edit files.
- {#D2} 2026-11-18: Plain checkboxes. Supersedes D1.
"""


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestSpecPlugin(unittest.TestCase):
    """plugins/spec.py: finding, parsing and checking specs in a git repo."""

    def setUp(self):
        self.sp = load_spec_plugin()
        self.repo = tempfile.mkdtemp(dir=TMP)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.folder = os.path.join(self.repo, "specs", "demo")
        os.makedirs(self.folder)
        self.write("index.md", SPEC_INDEX)
        self.write("decisions.md", SPEC_DECISIONS)
        self.write("../../README.md", "# not a spec\n")
        self.index = os.path.join(self.folder, "index.md")

    def write(self, rel, text):
        with open(os.path.join(self.folder, rel), "w") as f:
            f.write(text)
        self.sp._scans.clear()

    def test_parse(self):
        self.assertEqual(self.sp.find_specs(self.repo), [self.index])
        spec = self.sp.load(self.index, self.repo)
        self.assertEqual(spec["problems"], [])
        self.assertEqual([(i["id"], i["state"]) for i in spec["items"]],
                         [("R2", "todo"), ("R3", "doing"), ("R4", "done"), ("R5", "dropped")])
        r3 = spec["items"][1]
        self.assertEqual((r3["claim"], r3["text"], r3["section"]), ("s1", "@s1 Draw the view, a long item that goes on",
                                                                     ["Demo", "Roadmap"]))
        self.assertEqual([a["id"] for a in spec["anchors"]], ["R1"])            # code spans and fences are left out
        self.assertEqual(self.sp.summary(spec), {"todo": 1, "doing": 1, "done": 2, "dropped": 1, "total": 4})
        self.assertEqual([(d["id"], d["date"], d.get("superseded_by")) for d in spec["decisions"]],
                         [("D1", "2026-11-17", "D2"), ("D2", "2026-11-18", None)])
        self.assertEqual(spec["terms"][0]["term"], "Spec")

    def test_lint_and_note(self):
        self.assertIsNone(self.sp.note_for(self.index))
        self.assertIsNone(self.sp.note_for(os.path.join(self.repo, "README.md")))   # not part of a spec
        self.write("decisions.md", SPEC_DECISIONS + "- {#D2} 2026-11-19: Reused.\n- {#D3} no date\n")
        note = self.sp.note_for(os.path.join(self.folder, "decisions.md"))      # a part leads to its spec
        self.assertIn("D2 is already used at decisions.md:4", note)
        self.assertIn("next D is 3 but D3 exists: set it to 4", note)
        self.assertIn("decision D3 has no date", note)
        self.write("index.md", SPEC_INDEX.replace("- [~] @s1 Draw", "- [~] Draw").replace("- [ ] Write the parser {#R2}",
                                                                                       "- [ ] Write the parser"))
        note = self.sp.note_for(self.index)
        self.assertIn("item has no ID", note)
        self.assertIn("R3 is marked doing but not claimed", note)
        self.write("index.md", SPEC_INDEX.replace("parts: [decisions.md]", "parts: [decisions.md, gone.md]"))
        self.assertIn("gone.md: listed in parts but missing", self.sp.note_for(self.index))

    def test_context(self):
        ctx = self.sp.context_for(self.folder)
        self.assertIn("grovy spec mode", ctx)
        self.assertIn("specs/demo/index.md: Demo [active], 2/4 done; in progress: R3 (s1)", ctx)
        self.assertIn('<spec-index path="' + self.index + '">', ctx)                 # the active spec's index
        self.assertNotIn("Plain checkboxes", ctx)                                     # parts are read on demand
        self.assertIsNone(self.sp.context_for(tempfile.mkdtemp(dir=TMP)))              # not a git repo
        empty = tempfile.mkdtemp(dir=TMP)
        subprocess.run(["git", "init", "-q", empty], check=True)
        self.assertIsNone(self.sp.context_for(empty))                                 # a repo without specs

    def test_routes(self):
        """The page asks by session; only a spec found in that session's repo is ever read."""
        routes = {}
        class FakeApi:
            def __init__(s, cwd): s.cwd = cwd
            context = staticmethod(lambda fn: fn)
            def after_edit(s, fn=None, glob="*"): return (lambda f: f) if fn is None else fn
            def route(s, method, path, fn=None):
                def add(f): routes[path] = f; return f
                return add if fn is None else add(fn)
            def session(s, name): return {"session": {"cwd": s.cwd}} if name == "t1" else None
        self.sp.setup(FakeApi(self.folder))
        req = lambda **q: type("R", (), {"query": q})()
        listed = routes["list"](req(session="t1"))
        self.assertEqual([(x["rel"], x["summary"]["done"]) for x in listed["specs"]], [("specs/demo", 2)])
        one = routes["spec"](req(session="t1", rel="specs/demo"))
        self.assertEqual([(x["path"][-1], x["done"], x["total"], x["items"]) for x in one["sections"]],
                         [("Done so far", 1, 1, []), ("Roadmap", 1, 3, ["R2", "R3", "R4", "R5"])])
        self.assertIn("Plain checkboxes", one["texts"]["decisions.md"])
        refs = routes["ids"](req(session="t1"))["ids"]
        self.assertEqual(refs["R3"]["rel"], "specs/demo")
        self.assertEqual(refs["demo#D2"]["kind"], "decisions")
        self.assertEqual(refs["G1"]["text"], "a folder with an index.")
        self.assertNotIn("R99", refs)                                   # an example in a code span
        other = os.path.join(self.repo, "specs", "other")
        os.makedirs(other)
        with open(os.path.join(other, "index.md"), "w") as f:
            f.write("---\nspec: 1\nnext: {R: 4}\n---\n- [ ] Elsewhere {#R3}\n")
        self.sp._scans.clear()
        refs = routes["ids"](req(session="t1"))["ids"]
        self.assertNotIn("R3", refs)                                    # two specs have one: only qualified
        self.assertEqual((refs["demo#R3"]["rel"], refs["other#R3"]["rel"]), ("specs/demo", "specs/other"))
        for bad in ("../..", "specs", "/etc", ""):
            self.assertEqual(routes["spec"](req(session="t1", rel=bad))[0], 404)
        self.assertEqual(routes["spec"](req(session="nobody", rel="specs/demo"))[0], 404)
        self.assertEqual(routes["list"](req()), {"root": None, "specs": []})

    def test_header(self):
        meta, n = self.sp.parse_header("---\nspec: 1\nparts:\n  - a.md\n  - b.md\nnext: {R: 3}  # c\n---\nbody")
        self.assertEqual((meta, n), ({"spec": 1, "parts": ["a.md", "b.md"], "next": {"R": 3}}, 7))
        self.assertEqual(self.sp.parse_header("no header"), (None, 0))
        self.write("index.md", SPEC_INDEX.replace("spec: 1", "spec: 2"))
        self.assertIn("this grovy reads format 1", self.sp.note_for(self.index))

@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestPaint(unittest.TestCase):
    """index.html's paint(), run in node against a minimal DOM."""
    FAKE_DOM = r"""
class Node { constructor(n) { this.n = n; this.parent = null; this.removed = 0; }
  get isConnected() { return !!this.parent; } remove() { if (this.parent) this.parent._take(this); } }
class Box extends Node { constructor() { super("box"); this.childNodes = []; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  contains(x) { return this.childNodes.includes(x); }
  _take(x) { this.childNodes.splice(this.childNodes.indexOf(x), 1); x.parent = null; x.removed++; }
  insertBefore(x, ref) { if (x.parent) x.parent._take(x);
    const i = ref ? this.childNodes.indexOf(ref) : this.childNodes.length; this.childNodes.splice(i, 0, x); x.parent = this; }
  replaceChildren(...ks) { [...this.childNodes].forEach(c => this._take(c)); ks.forEach(k => this.insertBefore(k, null)); }
  querySelectorAll() { return []; } }
globalThis.Node = Node;
const document = {activeElement: null, body: {}, createTextNode: t => new Node("text:" + t)};
"""

    def run_paint(self, body):
        src = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        fn = re.search(r"^function paint\(box, \.\.\.kids\) \{.*?^\}", src, re.S | re.M).group(0)
        out = subprocess.run(["node", "-e", self.FAKE_DOM + fn + body], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_kept_node_is_never_taken_out(self):
        """A pending question card blinked: each refresh took it out of the page and back, replaying its animation."""
        r = self.run_paint("""
const box = new Box(), a = new Node("a"), card = new Node("card"), b = new Node("b"), c = new Node("c");
paint(box, a, card);
paint(box, b, card, c);          // new nodes before and after the cached card
paint(box, card);                // others go away
console.log(JSON.stringify({removed: card.removed, order: box.childNodes.map(x => x.n),
                            gone: [a, b, c].map(x => x.isConnected)}));""")
        self.assertEqual(r, {"removed": 0, "order": ["card"], "gone": [False, False, False]})

    def test_reorders_and_text(self):
        r = self.run_paint("""
const box = new Box(), a = new Node("a"), b = new Node("b");
paint(box, a, b); paint(box, b, "hi", a);
console.log(JSON.stringify(box.childNodes.map(x => x.n)));""")
        self.assertEqual(r, ["b", "text:hi", "a"])


if __name__ == "__main__":
    unittest.main()
