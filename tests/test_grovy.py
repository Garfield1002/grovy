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
import grovy  # noqa: E402
import grovyd  # noqa: E402


def ev(name, **kw):
    return {"hook_event_name": name, "session_id": kw.pop("sid", "S1"), **kw}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.live = {"a": "%1", "b": "%2"}
        self.pasted = []
        self.store = grovyd.Store(self.dir, tmux_live=lambda: dict(self.live),
                                paste=lambda n, t, *a, **k: self.pasted.append((n, t)))
        self.reg = mock.patch.object(grovy, "registry", lambda: {"a": {"cwd": "/a"}, "b": {"cwd": "/b"}})
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
            self.assertTrue(grovy.valid_name(good), good)
        for bad in ("", "-x", "a.b", "a:b", "a b", "../x", "A" * 33, "é", None, 3, "a\n"):
            self.assertFalse(grovy.valid_name(bad), bad)

    def test_permission_output(self):
        self.assertEqual(grovy.permission_output("allow"),
                         {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                 "decision": {"behavior": "allow"}}})
        d = grovy.permission_output("deny")["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "deny")
        self.assertTrue(d["message"])

    def test_typed_mode(self):
        self.assertTrue(grovy.typed_mode("hello\nworld"))
        self.assertFalse(grovy.typed_mode("/compact"))
        self.assertFalse(grovy.typed_mode("  !rm -rf"))
        self.assertFalse(grovy.typed_mode("x" * (grovy.TYPE_MAX + 1)))

    def test_norm(self):
        self.assertEqual(grovyd.norm("a\r\nb  \n"), "a\nb")

    def test_hook_settings_file(self):
        hooks = json.load(open(grovy.hook_settings("x")))["hooks"]
        self.assertEqual(set(hooks), set(grovy.HOOK_EVENTS))
        self.assertGreater(hooks["PermissionRequest"][0]["hooks"][0]["timeout"], grovy.PERMISSION_TIMEOUT)
        self.assertTrue(hooks["Stop"][0]["hooks"][0]["command"].endswith("'hook' 'x'"))


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
            new = grovy.default_state_dir()
            self.assertEqual(new, os.path.join(base, "grovy"))
            self.assertTrue(os.path.islink(old))
            self.assertEqual(open(os.path.join(old, "token")).read(), "t0k\n")   # old path still resolves
            self.assertEqual(grovy.default_state_dir(), new)                     # idempotent

    def test_env_falls_back_to_pre_rename_names(self):
        with mock.patch.dict(os.environ, {"CCS_PORT": "1"}):
            os.environ.pop("GROVY_PORT", None)
            self.assertEqual(grovy.env("PORT"), "1")
            with mock.patch.dict(os.environ, {"GROVY_PORT": "2"}):
                self.assertEqual(grovy.env("PORT"), "2")


class TestHookAdapter(unittest.TestCase):
    """cmd_hook must never print anything except valid decision JSON, and never fail Claude."""

    def run_hook(self, stdin, name="x", reply=None, exc=None):
        out = io.StringIO()
        def fake(msg, timeout=2.0, wait_reply=True):
            if exc:
                raise exc
            return reply
        with mock.patch.object(grovy, "sock_request", fake), mock.patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
            rc = grovy.cmd_hook(name)
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

    def test_subagent_events_stay_out_of_the_session(self):
        """Hooks fired inside a subagent carry agent_id (verified, Claude Code 2.1.325). A background subagent's
        PostToolUse arrived after the parent's Stop and showed an idle session as working."""
        sent = []
        def fake(msg, timeout=2.0, wait_reply=True):
            sent.append(msg["event"]["hook_event_name"])
            return {"decision": "allow"} if wait_reply else None
        sub = {"agent_id": "ab4c", "agent_type": "general-purpose"}
        with mock.patch.object(grovy, "sock_request", fake), mock.patch("sys.stderr", io.StringIO()):
            for name in ("PostToolUse", "UserPromptSubmit", "Stop", "PermissionRequest", "Notification"):
                with mock.patch("sys.stdin", io.StringIO(json.dumps(ev(name, **sub)))), redirect_stdout(io.StringIO()):
                    grovy.cmd_hook("x")
            self.assertEqual(sent, ["PermissionRequest", "Notification"])   # what waits on the user still shows
            with mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse")))):
                grovy.cmd_hook("x")                                        # the main agent's own tool calls do
            with mock.patch.dict(os.environ, {"GROVY_SUBAGENT_HOOKS": "1"}), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse", **sub)))):
                grovy.cmd_hook("x")
        self.assertEqual(sent[2:], ["PostToolUse", "PostToolUse"])

    def test_auto_mode_hints_in_session_settings(self):
        with open(grovy.hook_settings("am")) as f:
            am = json.load(f)["autoMode"]
        self.assertEqual((am["environment"][0], am["allow"][0]), ("$defaults", "$defaults"))   # built-ins kept
        self.assertIn(grovy.GROVY_BIN, am["environment"][1])
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
        with mock.patch.object(grovy, "registry", lambda: {}):
            s = grovyd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
            self.assertEqual(len(s.events["a"]), 1)
            s.ingest("a", ev("Stop", last_assistant_message="after"))
            s2 = grovyd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual([r["id"] for r in s2.events["a"]], [5, 6])

    def test_restart_reconfirms_wrapped_prompts(self):
        d = tempfile.mkdtemp(dir=TMP)
        rec = {"id": 1, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "UserPromptSubmit",
               "data": {"prompt": '<pasted_content id="x">\nhello\n\n\nworld\n</pasted_content id="x">'}}
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps(rec) + "\n")
        grovy.atomic_write_json(os.path.join(d, "prompts.json"), [
            {"id": "q" * 32, "ccs": "a", "text": "hello\nworld", "status": "unconfirmed", "created": 1, "updated": 1, "error": None}])
        s = grovyd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual(s.prompts[0]["status"], "submitted")

    def test_restart_mid_permission_and_injection(self):
        d = tempfile.mkdtemp(dir=TMP)
        grovy.atomic_write_json(os.path.join(d, "state.json"), {"sessions": {"a": {
            "name": "a", "state": "needs_input", "cwd": "/", "claude_session_id": "S", "last_event": 1,
            "activity": "", "last_error": None, "background": 0}}})
        grovy.atomic_write_json(os.path.join(d, "prompts.json"), [
            {"id": "p" * 32, "ccs": "a", "text": "hi", "status": "injected", "created": 1, "updated": 1, "error": None}])
        s = grovyd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual(s.sessions["a"]["state"], "needs_input")  # Claude fell back to its terminal dialog
        self.assertEqual(s.prompts[0]["status"], "unconfirmed")  # never re-sent

    def test_force_consent_does_not_survive(self):
        """Regression: a force-queued prompt was typed into a terminal permission dialog after restart."""
        d = tempfile.mkdtemp(dir=TMP)
        pasted = []
        with mock.patch.object(grovy, "registry", lambda: {"a": {"cwd": "/"}}):
            s = grovyd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda n, t, *a, **k: pasted.append(t))
            s.ingest("a", ev("SessionStart"))
            s.ingest("a", ev("UserPromptSubmit", prompt="busy"))           # working, fresh
            s.enqueue("a", "later", force=True)                            # can't type now
            self.assertFalse(s.prompts[-1]["force"])
            s.sessions["a"]["state"] = "unknown"
            s.try_deliver("a")
            s.open_permission("a", ev("PermissionRequest", tool_name="Bash"))
            s2 = grovyd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda n, t, *a, **k: pasted.append(t))
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
        with mock.patch.object(grovyd, "PERM_TIMEOUT", 0.3):
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
        out = grovy.permission_output("allow", None, {"questions": [], "answers": {"q": "a"}})
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
        grovyd._cmd_cache.clear()
        self.store.paste = lambda n, t, command=False, **k: self.pasted.append((n, t, command))

    def test_discovery(self):
        names = {c["name"]: c for c in grovyd.commands_for(self.proj)}
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
        grovyd._cmd_cache.clear()
        self.assertNotIn("Settings", [c["name"] for c in grovyd.commands_for(self.proj)])

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
            got = grovy.trusted_projects()
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
        self.assertTrue(all(grovyd._owner(r) == "a" for r in hits))


class TestReviewRegressions(StoreCase):
    """Findings from the adversarial review (correctness + security)."""

    def test_repeated_event_is_not_a_duplicate(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="continue"))
        self.store.ingest("a", ev("Stop", last_assistant_message="step 1 done"))
        with mock.patch.object(grovyd, "now", lambda: time.time() + 5):     # the user types it again later
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
        with mock.patch.object(grovy, "registry", lambda: {"b": {"cwd": "/b"}}):   # `grovy forget a`
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
        grovyd._cmd_cache.clear()
        names = [c["name"] for c in grovyd.commands_for(proj)]
        self.assertNotIn("model", names)

    def test_live_request_shows_full_input(self):
        big = {"command": "echo " + "x" * 6000, "edits": list(range(60))}
        p = self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash", tool_input=big))
        pub = grovyd._public(p)
        self.assertEqual(pub["tool_input"], big)
        self.assertFalse(pub["truncated"])
        self.assertIn("more items not shown", str(p["tool_input"]["edits"][-1]))   # log copy says so
        huge = self.store.open_permission("a", ev("PermissionRequest", tool_name="Write",
                                                  tool_input={"content": "y" * (grovyd.MAX_REVIEW + 10)}))
        self.assertTrue(grovyd._public(huge)["truncated"])


class TestSecondPassRegressions(StoreCase):
    def test_slow_phone_compact_is_not_unconfirmed(self):
        """A /compact from the phone took 87s; its receipt (SessionStart) only comes when it finishes."""
        self.store.paste = lambda n, t, command=False, **k: self.pasted.append((n, t))
        self.store.ingest("a", ev("SessionStart"))
        self.assertEqual(self.store.enqueue("a", "/compact")[0], 200)
        self.wait_pasted(1)
        self.assertEqual(self.store.sessions["a"]["activity"], "compacting conversation")
        later = time.time() + 90
        with mock.patch.object(grovyd, "now", lambda: later):
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
                grovy.tmux("list-panes")


class TestEventLogRotation(unittest.TestCase):
    def test_ids_keep_counting_after_rotation(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps({"id": 41, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "Stop", "data": {}}) + "\n")
        with mock.patch.object(os.path, "getsize", lambda p: 60 * 1024 * 1024), \
                mock.patch.object(grovy, "registry", lambda: {}):
            s = grovyd.Store(d, tmux_live=lambda: {}, paste=lambda *a: None)
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
            got = grovy.conversations(folder)
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
        with mock.patch.object(grovy, "registry_locked", locked):
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
            raise grovy.PasteAborted(seen[-1])
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
                mock.patch.object(grovy, "registry", lambda: {"s1": {"cwd": proj}}), \
                mock.patch.object(grovy, "sock_request", mock.Mock(side_effect=OSError("down"))), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"cwd": evil}))), redirect_stdout(out):
            grovy.cmd_statusline("s1")
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(out.getvalue().strip(), "OWN")   # the session's own project settings still apply

    def test_sharing_is_not_in_the_cli(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            grovy.main(["page", "share", "x"])


class TestSetupAndCustomCss(unittest.TestCase):
    def test_service_unit(self):
        u = grovy.service_unit("100.1.2.3", 7333, path="/a:/b:/a:/x%y")
        self.assertIn("KillMode=process", u)              # restarting the daemon must not kill tmux
        self.assertIn("Environment=GROVY_BIND=100.1.2.3", u)
        self.assertIn("Environment=PATH=/a:/b:/x%%y\n", u)  # de-duplicated, % escaped for systemd
        self.assertIn(os.path.join(grovy.HERE, "grovy") + " serve", u)

    def test_dry_run_changes_nothing(self):
        home = tempfile.mkdtemp(dir=TMP)
        env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, "cfg"), "PATH": "/usr/bin:/bin"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(grovy.shutil, "which", lambda x: "/bin/true"), \
                mock.patch.object(grovy, "sock_request", mock.Mock(side_effect=OSError)), redirect_stdout(out):
            rc = grovy.main(["setup", "--dry-run", "--bind", "0.0.0.0", "--no-service"])
        self.assertEqual(rc, 0)
        self.assertIn("linking", out.getvalue())
        self.assertEqual(os.listdir(home), [])

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
        self.assertEqual(grovyd.Store(self.dir, tmux_live=lambda: {}, paste=None).list_pages("a"), [])   # persisted

    def test_publish_validation(self):
        self.assertEqual(self.store.publish_page("zz", "r", "", "x")[0], 404)
        for bad in ("../x", "", ".hidden", "a/b", "x" * 65, None):
            self.assertEqual(self.store.publish_page("a", bad, "", "x")[0], 400, bad)
        with mock.patch.object(grovyd, "PAGE_MAX", 10):
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
        with mock.patch.object(grovy, "registry_locked", locked), mock.patch.object(self.store, "tmux_live", lambda: {}):
            self.assertEqual(self.store.forget("c")[0], 200)
        self.assertIsNone(self.store.page_html(p["id"]))

    def test_bridge_goes_after_doctype(self):
        out = grovyd.with_bridge(b"\n<!DOCTYPE html><html>x")
        self.assertTrue(out.startswith(b"\n<!DOCTYPE html>" + grovyd.PAGE_BRIDGE))
        self.assertTrue(grovyd.with_bridge(b"<p>hi").startswith(grovyd.PAGE_BRIDGE))


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
        with mock.patch.object(grovy, "registry", lambda: regs):
            self.assertEqual(self.store.create("a", "/x", False)[0], 409)

    def test_hook_adapter_updates_the_renamed_entry(self):
        regs = {"jean": {"cwd": "/a", "aliases": ["a"], "claude_session_id": "OLD"}}
        import contextlib
        @contextlib.contextmanager
        def locked():
            yield regs
        out = io.StringIO()
        with mock.patch.object(grovy, "registry_locked", locked), mock.patch.object(grovy, "sock_request", lambda *a, **k: None), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(ev("SessionStart", sid="NEW")))), redirect_stdout(out):
            grovy.cmd_hook("a")
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
                mock.patch.object(grovy, "sock_request", lambda m, **k: msgs.append(m)), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({**self.SAMPLE, "transcript_path": "/secret"}))), \
                redirect_stdout(out):
            grovy.cmd_statusline("a")
            settings = json.load(open(grovy.hook_settings("sl")))
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
            self.assertIsNone(grovy.user_statusline())


class TestOutdated(StoreCase):
    def test_update_offered_only_when_the_settings_changed(self):
        fp = grovy.session_fingerprint("a")
        reg = {"a": {"cwd": "/a", "config": fp}, "b": {"cwd": "/b", "config": "old"}}
        with mock.patch.object(grovy, "registry", lambda: reg):
            self.store.reconcile()
            self.assertEqual((self.store.summary("a")["outdated"], self.store.summary("b")["outdated"]), (False, True))
            del reg["a"]["config"]   # started before grovy recorded it
            self.store.reconcile()
            self.assertTrue(self.store.summary("a")["outdated"])
            reg["a"]["config"] = fp
            with mock.patch.object(grovy, "SESSION_ENV_VERSION", 99):   # e.g. a new variable in the environment
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
        self.assertTrue(grovy.proc_scope(os.getpid()) is None or grovy.proc_scope(os.getpid()).endswith(".scope"))
        self.assertIsNone(grovy.proc_scope(10**9))
        for line in ("tmux-spawn-9.scope: systemd-oomd killed 14 process(es) in this unit.",
                     "x.scope: Failed with result 'oom-kill'."):
            self.assertTrue(grovy.OOM_LOG_RE.search(line))
        self.assertFalse(grovy.OOM_LOG_RE.search("x.scope: Consumed 2min CPU time, 6.1M memory peak."))
        with mock.patch.object(grovy.subprocess, "run", side_effect=OSError("no journalctl")):
            self.assertFalse(grovy.oom_killed(["x.scope"]))
            self.assertIsNone(grovy.oom_protect("a", 1))


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

    def test_commands_and_long_messages_keep_their_place(self):
        self.busy()
        self.store.enqueue("a", "first")
        self.assertEqual(self.store.enqueue("a", "/compact")[0], 200)
        self.store.enqueue("a", "after the compaction")
        self.assertIn("this message", self.store.take_steers("a"))
        self.assertEqual(self.statuses(), ["steered", "queued", "queued"])   # nothing jumps the command
        self.assertIsNone(self.store.take_steers("a"))
        self.store.enqueue("b", "x" * (grovyd.STEER_MAX + 1))
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
        with mock.patch.object(grovy, "sock_request", lambda m, timeout=2.0, wait_reply=True:
                               {"steer": "CTX"} if m.get("want_steer") else None), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse")))), redirect_stdout(out):
            grovy.cmd_hook("a")
        self.assertEqual(json.loads(out.getvalue()), {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                                              "additionalContext": "CTX"}})
        for reply in ({"steer": None}, None, OSError("down")):
            out = io.StringIO()
            def fake(m, timeout=2.0, wait_reply=True, r=reply):
                if isinstance(r, Exception):
                    raise r
                return r
            with mock.patch.object(grovy, "sock_request", fake), mock.patch("sys.stderr", io.StringIO()), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(ev("PostToolUse")))), redirect_stdout(out):
                self.assertEqual(grovy.cmd_hook("a"), 0)
            self.assertEqual(out.getvalue(), "")


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
                p["updated"] -= grovyd.CONFIRM_TIMEOUT + 1
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
        self.store.sessions["a"]["last_event"] -= grovyd.FORCE_STALE + 1
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
        self.assertEqual(self.store.enqueue("a", "x" * (grovyd.MAX_PROMPT + 1))[0], 413)
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
        with mock.patch.object(grovyd, "LIVE_MAX", 100):
            self.assertEqual(self.store.live_update("a", "k", {"type": "note", "text": "x" * 200})[0], 413)
        self.assertEqual(self.lives(), [])

    def test_new_cards_are_rate_limited_but_updates_are_not(self):
        with mock.patch.object(grovyd, "LIVE_NEW_PER_MIN", 3):
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
        s2 = grovyd.Store(self.dir, tmux_live=lambda: dict(self.live), paste=None)
        [e] = [e for e in s2.events["a"] if e["type"] == "Live"]
        self.assertEqual((e["data"]["rev"], e["data"]["spec"]["items"][0]["value"]), (5, 4))
        self.store.live_update("a", "job", {"type": "note", "text": "end"}, "failed", "boom")   # final: written now
        s3 = grovyd.Store(self.dir, tmux_live=lambda: dict(self.live), paste=None)
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
            for _ in range(grovyd.HISTORY):
                self.store._record("a", "Notification", {})
        _, r2 = self.store.live_update("a", "job", {"type": "note", "text": "later"})
        self.assertNotEqual(r2["id"], r["id"])
        self.assertEqual(r2["rev"], 1)

    def test_browser_refreshes_are_throttled_and_the_last_one_comes(self):
        published = []
        self.store.publish = lambda name=None: published.append(time.monotonic())
        with mock.patch.object(grovyd, "LIVE_PUBLISH_EVERY", 0.2):
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
        self.patches = [mock.patch.object(grovy, "sock_request", fake),
                        mock.patch.dict(os.environ, {"GROVY_SESSION": "a"}),
                        mock.patch.object(grovy, "LIVE_EVERY", 0.1),
                        mock.patch.dict(grovy._live, {"off_until": 0.0, "open": {}, "crash": None, "warned": set()})]
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
        grovy._live_exit()
        self.assertEqual(self.last(p.key)["status"], "stopped")
        self.assertEqual(self.last(q.key)["status"], "done")   # complete: just never closed
        r = grovy.show({"type": "stats", "items": []})
        grovy._live["crash"] = "KeyError: 'x'"
        grovy._live_exit()
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
        orig = grovy.sock_request
        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        with mock.patch.object(grovy, "sock_request", counting):
            p = grovy.progress("t", 100)
            for i in range(100):
                p.set(i)
                time.sleep(0.002)
            p.done()
            grovy.note("x")
        self.assertEqual(len(calls), 1)

    def test_errors_are_reported_once(self):
        with mock.patch.object(grovy, "sock_request", lambda *a, **k: {"error": "too many"}), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            grovy.note("a")
            grovy.note("b")
        self.assertEqual(err.getvalue().count("too many"), 1)

    def test_guide_spells_out_the_path_for_old_sessions(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": grovy.session_pythonpath()}):
            self.assertIn("(on PYTHONPATH;", grovy.widget_guide())
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/elsewhere"}):
            self.assertIn(f"sys.path.insert(0, {grovy.HERE!r})", grovy.widget_guide())
            self.assertEqual(grovy.session_pythonpath(), "/elsewhere" + os.pathsep + grovy.HERE)


class TestLiveEndToEnd(unittest.TestCase):
    """The real socket path: the CLI in a subprocess talking to a real SockServer."""

    def setUp(self):
        self.sdir = tempfile.mkdtemp(dir=TMP)
        self.reg = mock.patch.object(grovy, "registry", lambda: {"a": {"cwd": "/a"}})
        self.reg.start()
        self.store = grovyd.Store(self.sdir, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.store.reconcile()
        self.env = dict(os.environ, GROVY_STATE_DIR=self.sdir, GROVY_SESSION="a")
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": self.sdir}):
            self.sock = grovyd.SockServer(grovy.sock_path(), grovyd.SockHandler)
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
        cls.reg = mock.patch.object(grovy, "registry", lambda: {})
        cls.reg.start()
        cls.store = grovyd.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        cls.store.reconcile()
        cls.httpd = grovyd.HttpServer(("127.0.0.1", 0), grovyd.Http)
        cls.httpd.store, cls.httpd.token = cls.store, ""
        cls.httpd.index, cls.httpd.csp = grovyd.load_index()
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
        self.assertTrue(body.startswith(b"<!doctype html>" + grovyd.PAGE_BRIDGE))
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

    def test_idle_connections_cannot_lock_out_the_phone(self):
        class Small(grovyd.HttpServer):
            MAX_CONNECTIONS = 4
        srv = Small(("127.0.0.1", 0), grovyd.Http)
        srv.store, srv.token = self.store, ""
        srv.index, srv.csp = grovyd.load_index()
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
            srv.index, srv.csp = grovyd.load_index()
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
        real = grovy.tmux
        cls.patches = [mock.patch.object(grovy, "tmux", lambda *a, **k: real("-L", cls.sock, *a, **k)),
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
            out = grovy.capture(name)
            if needle in out:
                return out
            time.sleep(0.05)
        self.fail(f"{needle!r} not seen in:\n{grovy.capture(name)}")

    def test_typing_stops_without_enter_when_aborted(self):
        grovy.start_session("ab1", self.proj)
        self.wait_for("ab1", "READY")
        calls = []
        def abort():
            calls.append(1)
            return "Claude opened a permission request" if len(calls) >= 2 else None
        with self.assertRaises(grovy.PasteAborted):
            grovy.paste_prompt("ab1", "x" * (grovy.TYPE_CHUNK * 4), abort=abort)
        time.sleep(0.5)
        self.assertNotIn("GOT[", grovy.capture("ab1"))   # Enter never pressed
        self.assertEqual(len(calls), 2)                    # stopped right after the chunk where it changed
        grovy.kill_session("ab1")

    def test_session_can_import_grovy(self):
        grovy.start_session("py1", self.proj)
        self.assertEqual(grovy.registry()["py1"]["config"], grovy.session_fingerprint("py1"))
        out = grovy.tmux("show-environment", "-t", "=grovy-py1", "PYTHONPATH", check=False).stdout
        self.assertIn(grovy.HERE, out.strip().split("=", 1)[1].split(os.pathsep))
        grovy.kill_session("py1")

    def test_widgets_opt_out_reaches_the_session(self):
        with mock.patch.dict(os.environ, {"GROVY_WIDGETS": "0"}):
            grovy.start_session("ow1", self.proj)
        out = grovy.tmux("show-environment", "-t", "=grovy-ow1", "GROVY_WIDGETS", check=False).stdout
        self.assertEqual(out.strip(), "GROVY_WIDGETS=0")
        grovy.kill_session("ow1")

    def test_lifecycle_and_literal_delivery(self):
        grovy.start_session("it1", self.proj)
        grovy.start_session("it2", self.proj)
        with self.assertRaises(ValueError):
            grovy.start_session("it1", self.proj)
        with self.assertRaises(ValueError):
            grovy.start_session("bad name", self.proj)
        self.assertTrue({"it1", "it2"} <= set(grovy.tmux_managed()))
        # an unrelated session that merely looks like ours is never considered managed
        grovy.tmux("new-session", "-d", "-s", "grovy-impostor", "sleep 60")
        self.assertNotIn("impostor", grovy.tmux_managed())
        with self.assertRaises(ValueError):
            grovy.kill_session("impostor")
        self.wait_for("it1", "READY")
        nasty = "a 'b' \"c\" $(echo pwned) `id` ; \\ ünï 🚀 %s"
        grovy.paste_prompt("it1", nasty)
        self.wait_for("it1", f"GOT[{nasty}]")
        grovy.paste_prompt("it1", "l1\nl2")
        self.wait_for("it1", "GOT[l2]")
        # typed-path edge cases: tmux ';' separators, option-looking text, chunk boundaries, tabs
        semi = "x" * 63 + ";"            # a chunk that ends exactly in ';'
        grovy.paste_prompt("it1", semi + " ; \\; -t foo")
        self.wait_for("it1", "GOT[" + semi + " ; \\; -t foo]")
        grovy.paste_prompt("it1", "-n --help")
        self.wait_for("it1", "GOT[-n --help]")
        long = "".join(chr(0x41 + i % 26) for i in range(300))
        grovy.paste_prompt("it1", long + "\tend")
        self.wait_for("it1", "GOT[" + long + "    end]")
        grovy.paste_prompt("it1", "/literal slash stays literal")  # paste path
        self.wait_for("it1", "GOT[/literal slash stays literal]")
        grovy.paste_prompt("it1", "!echo a\x1b[201~INJECTED\x1b[200~")      # can't break out of the paste
        self.wait_for("it1", "GOT[!echo a[201~INJECTED[200~]")
        self.assertNotIn("GOT", grovy.capture("it2"))  # isolation
        reg = grovy.registry()
        self.assertEqual(reg["it1"]["cwd"], os.path.realpath(self.proj))
        grovy.kill_session("it1")
        self.assertNotIn("it1", grovy.tmux_managed())
        self.assertIn("it2", grovy.tmux_managed())
        with self.assertRaises(RuntimeError):
            grovy.paste_prompt("it1", "into the void")
        grovy.kill_session("it2")
        self.assertEqual(grovy.tmux("has-session", "-t", "=grovy-impostor", check=False).returncode, 0)

    def test_other_state_dirs_sessions_are_invisible(self):
        """Regression: a test daemon's session ("shadow t3") showed up in the live daemon's list."""
        grovy.start_session("mine", self.proj)
        other = os.path.join(TMP, "other-state")
        with mock.patch.dict(os.environ, {"GROVY_STATE_DIR": other}):
            grovy.start_session("theirs", self.proj)
            self.assertEqual(set(grovy.tmux_managed()), {"theirs"})
            grovy.kill_session("theirs")
        self.assertNotIn("theirs", grovy.tmux_managed())
        self.assertIn("mine", grovy.tmux_managed())
        grovy.kill_session("mine")

    def test_live_rename(self):
        grovy.start_session("rn1", self.proj)
        self.wait_for("rn1", "READY")
        self.assertTrue(grovy.rename_session("rn1", "rn2"))
        self.assertEqual(set(grovy.tmux_managed()) & {"rn1", "rn2"}, {"rn2"})
        reg = grovy.registry()
        self.assertEqual(reg["rn2"]["aliases"], ["rn1"])
        self.assertNotIn("rn1", reg)
        for f in ("rn1.json", "rn2.json"):   # the running Claude's file and the one for its next start
            cmd = json.load(open(os.path.join(grovy.state_dir(), "hooks", f)))["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertTrue(cmd.endswith("'hook' 'rn2'"), cmd)
        grovy.paste_prompt("rn2", "still typeable")              # pane ownership check passes after rename
        self.wait_for("rn2", "GOT[still typeable]")
        with self.assertRaises(ValueError):
            grovy.start_session("rn1", self.proj)                # old name is reserved as an alias
        grovy.kill_session("rn2")

    def test_hook_env_reaches_session(self):
        grovy.start_session("it3", self.proj)
        out = grovy.tmux("show-environment", "-t", "=grovy-it3", "GROVY_SESSION").stdout
        self.assertEqual(out.strip(), "GROVY_SESSION=it3")
        grovy.kill_session("it3")


if __name__ == "__main__":
    unittest.main()
