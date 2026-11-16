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
TMP = tempfile.mkdtemp(prefix="ccs-test-", dir="/tmp")  # short path: AF_UNIX limit
os.environ["CCS_STATE_DIR"] = os.path.join(TMP, "state")
import ccs  # noqa: E402
import ccsd  # noqa: E402


def ev(name, **kw):
    return {"hook_event_name": name, "session_id": kw.pop("sid", "S1"), **kw}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.live = {"a": "%1", "b": "%2"}
        self.pasted = []
        self.store = ccsd.Store(self.dir, tmux_live=lambda: dict(self.live),
                                paste=lambda n, t: self.pasted.append((n, t)))
        self.reg = mock.patch.object(ccs, "registry", lambda: {"a": {"cwd": "/a"}, "b": {"cwd": "/b"}})
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
            self.assertTrue(ccs.valid_name(good), good)
        for bad in ("", "-x", "a.b", "a:b", "a b", "../x", "A" * 33, "é", None, 3, "a\n"):
            self.assertFalse(ccs.valid_name(bad), bad)

    def test_permission_output(self):
        self.assertEqual(ccs.permission_output("allow"),
                         {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                 "decision": {"behavior": "allow"}}})
        d = ccs.permission_output("deny")["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "deny")
        self.assertTrue(d["message"])

    def test_typed_mode(self):
        self.assertTrue(ccs.typed_mode("hello\nworld"))
        self.assertFalse(ccs.typed_mode("/compact"))
        self.assertFalse(ccs.typed_mode("  !rm -rf"))
        self.assertFalse(ccs.typed_mode("x" * (ccs.TYPE_MAX + 1)))

    def test_norm(self):
        self.assertEqual(ccsd.norm("a\r\nb  \n"), "a\nb")

    def test_hook_settings_file(self):
        hooks = json.load(open(ccs.hook_settings("x")))["hooks"]
        self.assertEqual(set(hooks), set(ccs.HOOK_EVENTS))
        self.assertGreater(hooks["PermissionRequest"][0]["hooks"][0]["timeout"], ccs.PERMISSION_TIMEOUT)
        self.assertTrue(hooks["Stop"][0]["hooks"][0]["command"].endswith("'hook' 'x'"))


class TestHookAdapter(unittest.TestCase):
    """cmd_hook must never print anything except valid decision JSON, and never fail Claude."""

    def run_hook(self, stdin, name="x", reply=None, exc=None):
        out = io.StringIO()
        def fake(msg, timeout=2.0, wait_reply=True):
            if exc:
                raise exc
            return reply
        with mock.patch.object(ccs, "sock_request", fake), mock.patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
            rc = ccs.cmd_hook(name)
        return rc, out.getvalue()

    def test_malformed_and_unknown(self):
        self.assertEqual(self.run_hook("not json"), (0, ""))
        self.assertEqual(self.run_hook("[1]"), (0, ""))
        self.assertEqual(self.run_hook(json.dumps(ev("Bogus"))), (0, ""))
        self.assertEqual(self.run_hook(json.dumps(ev("Stop")), name="bad name"), (0, ""))

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
        with mock.patch.object(ccs, "registry", lambda: {}):
            s = ccsd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
            self.assertEqual(len(s.events["a"]), 1)
            s.ingest("a", ev("Stop", last_assistant_message="after"))
            s2 = ccsd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual([r["id"] for r in s2.events["a"]], [5, 6])

    def test_restart_reconfirms_wrapped_prompts(self):
        d = tempfile.mkdtemp(dir=TMP)
        rec = {"id": 1, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "UserPromptSubmit",
               "data": {"prompt": '<pasted_content id="x">\nhello\n\n\nworld\n</pasted_content id="x">'}}
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps(rec) + "\n")
        ccs.atomic_write_json(os.path.join(d, "prompts.json"), [
            {"id": "q" * 32, "ccs": "a", "text": "hello\nworld", "status": "unconfirmed", "created": 1, "updated": 1, "error": None}])
        s = ccsd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual(s.prompts[0]["status"], "submitted")

    def test_restart_mid_permission_and_injection(self):
        d = tempfile.mkdtemp(dir=TMP)
        ccs.atomic_write_json(os.path.join(d, "state.json"), {"sessions": {"a": {
            "name": "a", "state": "needs_input", "cwd": "/", "claude_session_id": "S", "last_event": 1,
            "activity": "", "last_error": None, "background": 0}}})
        ccs.atomic_write_json(os.path.join(d, "prompts.json"), [
            {"id": "p" * 32, "ccs": "a", "text": "hi", "status": "injected", "created": 1, "updated": 1, "error": None}])
        s = ccsd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        self.assertEqual(s.sessions["a"]["state"], "needs_input")  # Claude fell back to its terminal dialog
        self.assertEqual(s.prompts[0]["status"], "unconfirmed")  # never re-sent

    def test_force_consent_does_not_survive(self):
        """Regression: a force-queued prompt was typed into a terminal permission dialog after restart."""
        d = tempfile.mkdtemp(dir=TMP)
        pasted = []
        with mock.patch.object(ccs, "registry", lambda: {"a": {"cwd": "/"}}):
            s = ccsd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda n, t: pasted.append(t))
            s.ingest("a", ev("SessionStart"))
            s.ingest("a", ev("UserPromptSubmit", prompt="busy"))           # working, fresh
            s.enqueue("a", "later", force=True)                            # can't type now
            self.assertFalse(s.prompts[-1]["force"])
            s.sessions["a"]["state"] = "unknown"
            s.try_deliver("a")
            s.open_permission("a", ev("PermissionRequest", tool_name="Bash"))
            s2 = ccsd.Store(d, tmux_live=lambda: {"a": "%1"}, paste=lambda n, t: pasted.append(t))
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
        with mock.patch.object(ccsd, "PERM_TIMEOUT", 0.3):
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
        out = ccs.permission_output("allow", None, {"questions": [], "answers": {"q": "a"}})
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
        ccsd._cmd_cache.clear()
        self.store.paste = lambda n, t, command=False: self.pasted.append((n, t, command))

    def test_discovery(self):
        names = {c["name"]: c for c in ccsd.commands_for(self.proj)}
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
        ccsd._cmd_cache.clear()
        self.assertNotIn("Settings", [c["name"] for c in ccsd.commands_for(self.proj)])

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
            got = ccs.trusted_projects()
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
        self.assertTrue(all(r["ccs"] == "a" for r in hits))


class TestReviewRegressions(StoreCase):
    """Findings from the adversarial review (correctness + security)."""

    def test_repeated_event_is_not_a_duplicate(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.ingest("a", ev("UserPromptSubmit", prompt="continue"))
        self.store.ingest("a", ev("Stop", last_assistant_message="step 1 done"))
        with mock.patch.object(ccsd, "now", lambda: time.time() + 5):     # the user types it again later
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
        with mock.patch.object(ccs, "registry", lambda: {"b": {"cwd": "/b"}}):   # `ccs forget a`
            self.store.reconcile()
        self.assertEqual(self.store.prompts[-1]["status"], "failed")
        self.live["a"] = "%9"
        self.store.reconcile()
        self.store.ingest("a", ev("SessionStart", sid="NEW"))
        time.sleep(0.05)
        self.assertEqual(self.pasted, [])

    def test_newest_injected_prompt_gets_the_confirmation(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.prompts.append({"id": "o" * 32, "ccs": "a", "text": "run the tests", "status": "unconfirmed",
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
        ccsd._cmd_cache.clear()
        names = [c["name"] for c in ccsd.commands_for(proj)]
        self.assertNotIn("model", names)

    def test_live_request_shows_full_input(self):
        big = {"command": "echo " + "x" * 6000, "edits": list(range(60))}
        p = self.store.open_permission("a", ev("PermissionRequest", tool_name="Bash", tool_input=big))
        pub = ccsd._public(p)
        self.assertEqual(pub["tool_input"], big)
        self.assertFalse(pub["truncated"])
        self.assertIn("more items not shown", str(p["tool_input"]["edits"][-1]))   # log copy says so
        huge = self.store.open_permission("a", ev("PermissionRequest", tool_name="Write",
                                                  tool_input={"content": "y" * (ccsd.MAX_REVIEW + 10)}))
        self.assertTrue(ccsd._public(huge)["truncated"])


class TestSecondPassRegressions(StoreCase):
    def test_stale_phone_compact_does_not_end_a_turn(self):
        self.store.ingest("a", ev("SessionStart"))
        self.store.prompts.append({"id": "c" * 32, "ccs": "a", "text": "/compact", "command": "compact",
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
        self.store.reconcile()                                            # `ccs new a ~/other` refresh
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
                ccs.tmux("list-panes")


class TestEventLogRotation(unittest.TestCase):
    def test_ids_keep_counting_after_rotation(self):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.write(json.dumps({"id": 41, "ts": 1, "ccs": "a", "claude_session_id": None, "type": "Stop", "data": {}}) + "\n")
        with mock.patch.object(os.path, "getsize", lambda p: 60 * 1024 * 1024), \
                mock.patch.object(ccs, "registry", lambda: {}):
            s = ccsd.Store(d, tmux_live=lambda: {}, paste=lambda *a: None)
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
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            got = ccs.conversations(folder)
        self.assertEqual(len(got), 1)
        c = got[0]
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
        self.store.sessions["a"]["claude_session_id"] = self.ID1                                  # open in ccs "a"
        code, msg = self.store.create("y", "/ok", False, resume=self.ID1)
        self.assertEqual(code, 409)
        self.assertIn("session a", msg)
        self.store.sessions["a"]["claude_session_id"] = None
        self.assertEqual(self.store.create("y", "/ok", False, resume=self.ID1)[0], 200)
        self.assertEqual(started[-1], ["--resume", self.ID1])


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
        return [p["status"] for p in self.store.prompts if p["ccs"] == name]

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
                p["updated"] -= ccsd.CONFIRM_TIMEOUT + 1
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
        self.store.sessions["a"]["last_event"] -= ccsd.FORCE_STALE + 1
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
        self.assertEqual(self.store.enqueue("a", "x" * (ccsd.MAX_PROMPT + 1))[0], 413)
        _, p = self.store.enqueue("a", "x")  # session "starting": queued
        self.assertEqual(self.store.cancel_prompt(p["id"]), (200, "cancelled"))
        self.assertEqual(self.store.cancel_prompt(p["id"])[0], 409)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = mock.patch.object(ccs, "registry", lambda: {})
        cls.reg.start()
        cls.store = ccsd.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        cls.store.reconcile()
        cls.httpd = ccsd.HttpServer(("127.0.0.1", 0), ccsd.Http)
        cls.httpd.store, cls.httpd.token = cls.store, ""
        cls.httpd.index, cls.httpd.csp = ccsd.load_index()
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

    def test_slowloris_deadline_and_connection_cap(self):
        class Small(ccsd.HttpServer):
            MAX_CONNECTIONS = 3
        with mock.patch.object(ccsd.Http, "HEADER_DEADLINE", 0.5):
            srv = Small(("127.0.0.1", 0), ccsd.Http)
            srv.store, srv.token = self.store, ""
            srv.index, srv.csp = ccsd.load_index()
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
        streams = [urllib.request.urlopen(self.base + "/api/events", timeout=5) for _ in range(ccsd.SSE_PER_CLIENT + 2)]
        for r in streams:
            r.readline()
        time.sleep(0.3)
        mine = [s for s in self.store.subscribers if s.client == "127.0.0.1"]
        self.assertEqual(len(mine), ccsd.SSE_PER_CLIENT)
        self.assertEqual(streams[0].read(), b'data: {"hello":1}\n\n')   # read() returns: the server ended it
        for r in streams:
            r.close()

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
        self.assertEqual(self.req(p, raw=b"x" * (ccsd.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.req(p, {"text": "ok"})[0], 200)

    def test_login_page_and_enrollment(self):
        self.httpd.token = "s3cret"
        try:
            code, body, _ = self.req("/")
            self.assertEqual(code, 401)
            self.assertIn(b"ccs url", body)
            self.httpd.enforce = False          # CCS_AUTH=off: open, but ?token= still sets the cookie
            self.assertEqual(self.req("/api/sessions")[0], 200)
            conn = __import__("http.client").client.HTTPConnection("127.0.0.1", self.httpd.server_address[1])
            conn.request("GET", "/?token=s3cret")
            r = conn.getresponse()
            self.assertEqual(r.status, 303)
            self.assertIn("ccs_token=s3cret", r.getheader("Set-Cookie"))
            self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
            conn.close()
        finally:
            self.httpd.token, self.httpd.enforce = "", True

    def test_token(self):
        self.httpd.token = "s3cret"
        try:
            self.assertEqual(self.req("/api/sessions")[0], 401)
            self.assertEqual(self.req("/api/sessions", headers={"Authorization": "Bearer s3cret"})[0], 200)
            self.assertEqual(self.req("/api/sessions", headers={"Cookie": "ccs_token=s3cret"})[0], 200)
            self.assertEqual(self.req("/api/sessions", headers={"Cookie": "ccs_token=wrong"})[0], 401)
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
        cls.sock = "ccs-test-%d" % os.getpid()
        real = ccs.tmux
        cls.patches = [mock.patch.object(ccs, "tmux", lambda *a, **k: real("-L", cls.sock, *a, **k)),
                       mock.patch.dict(os.environ, {"CCS_CLAUDE": cls.fake})]
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
            out = ccs.capture(name)
            if needle in out:
                return out
            time.sleep(0.05)
        self.fail(f"{needle!r} not seen in:\n{ccs.capture(name)}")

    def test_lifecycle_and_literal_delivery(self):
        ccs.start_session("it1", self.proj)
        ccs.start_session("it2", self.proj)
        with self.assertRaises(ValueError):
            ccs.start_session("it1", self.proj)
        with self.assertRaises(ValueError):
            ccs.start_session("bad name", self.proj)
        self.assertTrue({"it1", "it2"} <= set(ccs.tmux_managed()))
        # an unrelated session that merely looks like ours is never considered managed
        ccs.tmux("new-session", "-d", "-s", "ccs-impostor", "sleep 60")
        self.assertNotIn("impostor", ccs.tmux_managed())
        with self.assertRaises(ValueError):
            ccs.kill_session("impostor")
        self.wait_for("it1", "READY")
        nasty = "a 'b' \"c\" $(echo pwned) `id` ; \\ ünï 🚀 %s"
        ccs.paste_prompt("it1", nasty)
        self.wait_for("it1", f"GOT[{nasty}]")
        ccs.paste_prompt("it1", "l1\nl2")
        self.wait_for("it1", "GOT[l2]")
        # typed-path edge cases: tmux ';' separators, option-looking text, chunk boundaries, tabs
        semi = "x" * 63 + ";"            # a chunk that ends exactly in ';'
        ccs.paste_prompt("it1", semi + " ; \\; -t foo")
        self.wait_for("it1", "GOT[" + semi + " ; \\; -t foo]")
        ccs.paste_prompt("it1", "-n --help")
        self.wait_for("it1", "GOT[-n --help]")
        long = "".join(chr(0x41 + i % 26) for i in range(300))
        ccs.paste_prompt("it1", long + "\tend")
        self.wait_for("it1", "GOT[" + long + "    end]")
        ccs.paste_prompt("it1", "/literal slash stays literal")  # paste path
        self.wait_for("it1", "GOT[/literal slash stays literal]")
        ccs.paste_prompt("it1", "!echo a\x1b[201~INJECTED\x1b[200~")      # can't break out of the paste
        self.wait_for("it1", "GOT[!echo a[201~INJECTED[200~]")
        self.assertNotIn("GOT", ccs.capture("it2"))  # isolation
        reg = ccs.registry()
        self.assertEqual(reg["it1"]["cwd"], os.path.realpath(self.proj))
        ccs.kill_session("it1")
        self.assertNotIn("it1", ccs.tmux_managed())
        self.assertIn("it2", ccs.tmux_managed())
        with self.assertRaises(RuntimeError):
            ccs.paste_prompt("it1", "into the void")
        ccs.kill_session("it2")
        self.assertEqual(ccs.tmux("has-session", "-t", "=ccs-impostor", check=False).returncode, 0)

    def test_other_state_dirs_sessions_are_invisible(self):
        """Regression: a test daemon's session ("shadow t3") showed up in the live daemon's list."""
        ccs.start_session("mine", self.proj)
        other = os.path.join(TMP, "other-state")
        with mock.patch.dict(os.environ, {"CCS_STATE_DIR": other}):
            ccs.start_session("theirs", self.proj)
            self.assertEqual(set(ccs.tmux_managed()), {"theirs"})
            ccs.kill_session("theirs")
        self.assertNotIn("theirs", ccs.tmux_managed())
        self.assertIn("mine", ccs.tmux_managed())
        ccs.kill_session("mine")

    def test_hook_env_reaches_session(self):
        ccs.start_session("it3", self.proj)
        out = ccs.tmux("show-environment", "-t", "=ccs-it3", "CCS_SESSION").stdout
        self.assertEqual(out.strip(), "CCS_SESSION=it3")
        ccs.kill_session("it3")


if __name__ == "__main__":
    unittest.main()
