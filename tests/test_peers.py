"""Peers (grovylib/peers.py): a hub showing another machine's grovy. Run: python3 -m unittest discover -s tests

The "peer" is a real grovyd HTTP server on 127.0.0.1 with a fake tmux; the SSH reverse tunnel is stood in for by a
small forwarder from a Unix socket to it, which the tests stop and restart to cut the link.
"""
import io
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

import test_grovy as base   # its environment setup (private state, config and tmux) applies here too

import grovyd  # noqa: E402
from grovylib import core, pages, peers, sessions, store  # noqa: E402

TMP = base.TMP
PEER_TOKEN = "peer-token-0123456789"


def ev(name, **kw):
    return {"hook_event_name": name, "session_id": kw.pop("sid", "S1"), **kw}


class Forwarder:
    """A Unix socket that forwards every connection to a TCP port: what `ssh -R sock:localhost:port` does."""

    def __init__(self, path, port):
        self.path, self.port, self.conns, self.lock = path, port, [], threading.Lock()
        self.start()

    def start(self):
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self.srv.bind(self.path)
        finally:
            os.umask(old)
        self.srv.listen(16)
        threading.Thread(target=self._accept, args=(self.srv,), daemon=True).start()

    def _accept(self, srv):
        while True:
            try:
                a, _ = srv.accept()
            except OSError:
                return
            try:
                b = socket.create_connection(("127.0.0.1", self.port))
            except OSError:
                a.close()
                continue
            with self.lock:
                self.conns += [a, b]
            for x, y in ((a, b), (b, a)):
                threading.Thread(target=self._pump, args=(x, y), daemon=True).start()

    @staticmethod
    def _pump(x, y):
        try:
            while True:
                d = x.recv(65536)
                if not d:
                    break
                y.sendall(d)
        except OSError:
            pass
        for s in (x, y):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self):
        self.srv.close()
        with self.lock:
            for c in self.conns:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                c.close()
            self.conns = []
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, name, kind, text):
        self.sent.append((name, kind, text))


def serve(st, token):
    httpd = grovyd.HttpServer(("127.0.0.1", 0), grovyd.Http)
    httpd.store, httpd.token = st, token
    httpd.page = grovyd.load_index()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def wait_for(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PeerLink(unittest.TestCase):
    """A hub and a peer, linked through a forwarder."""

    def setUp(self):
        self.reg = mock.patch.object(sessions, "registry", lambda: {})
        self.reg.start()
        self.dir = tempfile.mkdtemp(dir=TMP)
        os.chmod(self.dir, 0o700)
        self.keys = []
        self.life = mock.Mock()
        self.life.resume.return_value = "resumed"
        self.life.trusted_projects.return_value = ["/x"]
        self.life.conversations.return_value = []
        quiet = mock.patch.object(peers, "log", lambda msg: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.peer = store.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%1"},
                                paste=lambda *a, **k: None, key=lambda n, k: self.keys.append((n, k)), lifecycle=self.life)
        self.peer.reconcile()
        self.peer.ingest("a", ev("SessionStart", source="startup", cwd="/pc/a"))
        self.phttpd = serve(self.peer, PEER_TOKEN)
        self.sock = os.path.join(self.dir, "pc.sock")
        self.fwd = Forwarder(self.sock, self.phttpd.server_address[1])
        self.hub = store.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%9"}, paste=lambda *a, **k: None)
        self.hub.reconcile()
        self.hub.notifier = Recorder()
        self.conf = os.path.join(self.dir, "peers.json")
        peers.write_config({"pc": {"socket": self.sock, "token": PEER_TOKEN, "tls": False}}, self.conf)
        self.hub.peers = peers.Peers(self.hub, path=self.conf)
        self.hub.peers.reload()
        self.httpd = serve(self.hub, "")
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.assertTrue(wait_for(lambda: self.pc().status()["state"] == "online"), self.pc().status())

    def tearDown(self):
        self.hub.peers.close()
        self.fwd.stop()
        for h in (self.httpd, self.phttpd):
            h.stopping = True
            h.shutdown()
            h.server_close()
        self.reg.stop()

    def pc(self):
        return self.hub.peers.get("pc")

    def req(self, path, body=None, headers=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": ctype} if data is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def get(self, path, **kw):
        code, body, _ = self.req(path, **kw)
        return code, json.loads(body)

    def post(self, path, body):
        code, raw, _ = self.req(path, body)
        return code, json.loads(raw)

    def subscribe(self):
        q = grovyd.Subscriber(maxlen=100)
        self.hub.subscribers.add(q)
        self.addCleanup(self.hub.subscribers.discard, q)
        return q

    def wait_msg(self, q, session):
        return wait_for(lambda: any(json.loads(m).get("session") == session for m in list(q)))


class TestPeerLink(PeerLink):
    def test_list_shows_both_machines(self):
        code, d = self.get("/api/sessions")
        self.assertEqual(code, 200)
        names = {s["name"]: s for s in d["sessions"]}
        self.assertIn("a", names)          # the hub's own "a"
        self.assertIn("pc:a", names)       # the peer's, never confused with it
        self.assertEqual(names["pc:a"]["machine"], "pc")
        self.assertEqual(names["pc:a"]["state"], "idle")
        self.assertNotIn("machine", names["a"])
        self.assertEqual(d["peers"][0]["name"], "pc")
        self.assertEqual(d["peers"][0]["state"], "online")

    def test_detail_history_and_local_untouched(self):
        code, d = self.get("/api/sessions/pc:a")
        self.assertEqual(code, 200)
        self.assertEqual(d["session"]["name"], "pc:a")
        self.assertEqual(d["session"]["cwd"], "/pc/a")
        self.assertTrue(d["events"] and all(e["session"] == "pc:a" for e in d["events"]))
        code, d2 = self.get("/api/sessions/pc%3Aa")   # what encodeURIComponent sends
        self.assertEqual(code, 200)
        self.assertEqual(d2["session"]["name"], "pc:a")
        code, h = self.get(f"/api/sessions/pc:a/history?before={d['events'][-1]['id'] + 1}")
        self.assertEqual(code, 200)
        self.assertTrue(all(e["session"] == "pc:a" for e in h["events"]))
        code, local = self.get("/api/sessions/a")
        self.assertEqual(code, 200)
        self.assertEqual(local["session"]["name"], "a")
        self.assertEqual(self.get("/api/sessions/mac:a")[0], 404)       # no such machine
        self.assertEqual(self.get("/api/sessions/pc:..%2Fx")[0], 404)   # not a session name

    def test_token_never_reaches_the_phone(self):
        for path in ("/api/sessions", "/api/sessions/pc:a", "/api/pages"):
            self.assertNotIn(PEER_TOKEN.encode(), self.req(path)[1])

    def test_prompt_reaches_the_peer(self):
        pasted = []
        self.peer.paste = lambda n, t, *a, **k: pasted.append((n, t))
        code, p = self.post("/api/sessions/pc:a/prompts", {"text": "hello from the hub"})
        self.assertEqual(code, 200, p)
        self.assertTrue(p["id"].startswith("pc:"))
        self.assertEqual(p["session"], "pc:a")
        self.assertTrue(wait_for(lambda: pasted))
        self.assertEqual(pasted[0][0], "a")
        self.assertIn("hello from the hub", pasted[0][1])

    def test_queued_prompt_cancel(self):
        self.peer.ingest("a", ev("UserPromptSubmit", prompt="busy"))   # working: a prompt queues
        code, p = self.post("/api/sessions/pc:a/prompts", {"text": "later"})
        self.assertEqual(code, 200)
        self.assertEqual(p["status"], "queued")
        code, r = self.post(f"/api/prompts/{p['id']}/cancel", {})
        self.assertEqual(code, 200, r)
        self.assertEqual([x["status"] for x in self.peer.prompts if x["text"] == "later"], ["cancelled"])

    def test_permission_answered_through_the_hub(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        p = self.peer.open_permission("a", ev("PermissionRequest", tool_name="Bash",
                                              tool_input={"command": "rm -rf pc:" + "0" * 32}))
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.peer.wait_permission(p, b)))
        t.start()
        code, d = self.get("/api/sessions/pc:a")
        perm = d["permissions"][0]
        self.assertEqual(perm["id"], "pc:" + p["id"])
        self.assertEqual(perm["tool_input"], {"command": "rm -rf pc:" + "0" * 32})   # shown exactly as sent
        self.assertTrue(wait_for(lambda: self.get("/api/sessions")[1]["sessions"][0]["name"] == "pc:a"))   # waiting on you: first
        code, r = self.post(f"/api/permissions/{perm['id']}/decision", {"decision": "allow"})
        self.assertEqual(code, 200, r)
        t.join(5)
        self.assertEqual(res["r"], {"decision": "allow"})
        code, r = self.post(f"/api/permissions/{perm['id']}/decision", {"decision": "deny"})
        self.assertEqual(code, 409)
        self.assertTrue(r["error"].startswith("pc: "))

    def test_lifecycle_verbs(self):
        self.peer.ingest("a", ev("UserPromptSubmit", prompt="work"))
        self.assertEqual(self.post("/api/sessions/pc:a/interrupt", {})[0], 200)
        self.assertEqual(self.keys, [("a", "Escape")])
        self.assertEqual(self.post("/api/sessions/pc:a/kill", {})[0], 200)
        self.life.kill.assert_called_with("a")
        self.assertEqual(self.post("/api/sessions/pc:a/resume", {})[0], 200)
        self.life.resume.assert_called_with("a")
        self.assertEqual(self.post("/api/sessions", {"name": "pc:new", "path": "/x"})[0], 200)
        self.assertEqual(self.life.start.call_args[0][:2], ("new", "/x"))

    def test_rename_stays_on_its_machine(self):
        with mock.patch.object(sessions, "rename_session"):
            self.life.rename.side_effect = None
            code, r = self.post("/api/sessions/pc:a/rename", {"name": "mac:b"})
            self.assertEqual(code, 400)
            code, r = self.post("/api/sessions/pc:a/rename", {"name": "b"})
            self.assertEqual(code, 200, r)
            self.assertEqual(r["status"], "pc:b")
            self.life.rename.assert_called_with("a", "b")

    def test_live_updates_are_prefixed(self):
        q = self.subscribe()
        self.peer.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.assertTrue(self.wait_msg(q, "pc:a"), list(q))
        # the list was refreshed before the phone was told
        self.assertEqual({s["name"]: s["state"] for s in self.get("/api/sessions")[1]["sessions"]}["pc:a"], "working")

    def test_notifications_are_passed_on(self):
        seen = []
        self.hub.plugins = mock.Mock()
        self.hub.plugins.emit.side_effect = lambda *a: seen.append(a)
        self.peer.ingest("a", ev("UserPromptSubmit", prompt="go"))
        self.peer.ingest("a", ev("Stop", last_assistant_message="all done"))
        self.assertTrue(wait_for(lambda: self.hub.notifier.sent))
        self.assertEqual(self.hub.notifier.sent[0][:2], ("pc:a", "done"))
        self.assertIn(("notify", "pc:a", "done", self.hub.notifier.sent[0][2]), seen)   # Web Push is a plugin

    def test_offline_then_recovery(self):
        q = self.subscribe()
        self.fwd.stop()
        self.assertTrue(wait_for(lambda: self.pc().status()["state"] == "offline"))
        s = {x["name"]: x for x in self.get("/api/sessions")[1]["sessions"]}["pc:a"]
        self.assertEqual(s["state"], "unknown")     # never "idle" while we can't know
        self.assertFalse(s["accepts_input"])
        self.assertIn("offline", s["activity"])
        code, d = self.get("/api/sessions/pc:a")
        self.assertEqual(code, 502)
        self.assertIn("pc is offline", d["error"])
        self.assertIn("is `grovy peer tunnel` running on pc?", d["error"])
        code, d = self.post("/api/sessions/pc:a/prompts", {"text": "x"})
        self.assertEqual(code, 502)
        self.assertTrue(any(json.loads(m).get("session") is None for m in q))   # phones re-fetch everything
        # while the link is down a permission request opens: after reconnecting the hub notifies it
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        p = self.peer.open_permission("a", ev("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}))
        threading.Thread(target=lambda: self.peer.wait_permission(p, b), daemon=True).start()
        self.hub.notifier.sent.clear()
        self.fwd.start()
        self.assertTrue(wait_for(lambda: self.pc().status()["state"] == "online", 15))
        s = {x["name"]: x for x in self.get("/api/sessions")[1]["sessions"]}["pc:a"]
        self.assertEqual(s["state"], "needs_input")
        self.assertEqual(s["pending_permissions"], 1)
        self.assertTrue(wait_for(lambda: ("pc:a", "input") in [x[:2] for x in self.hub.notifier.sent]))
        self.peer.decide(p["id"], "deny")

    def test_stale_refresh_never_shows_offline_peer_as_idle(self):
        """A list fetched just before the link dropped must not bring the sessions back as idle."""
        pc = self.pc()
        with pc.lock:
            gen = pc.gen
        pc._offline("test")
        with pc.lock:
            self.assertNotEqual(gen, pc.gen)
            pc.dirty.add("a")
        pc.wake.set()
        time.sleep(0.3)
        self.assertEqual(pc.listed()[0]["state"], "unknown")

    def test_pages_keep_the_hubs_sandbox(self):
        _, p = self.peer.publish_page("a", "r", "Report", "<!doctype html><p>hi")
        self.assertTrue(wait_for(lambda: any(x["id"] == "pc:" + p["id"] for x in self.get("/api/pages")[1]["pages"])))
        code, d = self.get("/api/sessions/pc:a")
        self.assertIn("pc:" + p["id"], d["pages"])
        page_ev = [e for e in d["events"] if e["type"] == "Page"]
        self.assertTrue(page_ev and page_ev[-1]["data"]["page"] == "pc:" + p["id"])
        code, body, h = self.req(f"/p/pc:{p['id']}?v=1")
        self.assertEqual(code, 200)
        self.assertEqual(h["Content-Security-Policy"], pages.PAGE_CSP)
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn(b"<p>hi", body)
        code, r = self.post(f"/api/pages/pc:{p['id']}/share", {"shared": True})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["page"]["share"].startswith("pc:"))
        code, body, h = self.req("/s/" + r["page"]["share"])
        self.assertEqual(code, 200)
        self.assertEqual(h["Content-Security-Policy"], pages.PAGE_CSP)
        self.assertEqual(self.req("/s/pc:" + "0" * 32)[0], 404)
        self.assertEqual(self.post(f"/api/pages/pc:{p['id']}/delete", {})[0], 200)
        self.assertEqual(self.req(f"/p/pc:{p['id']}")[0], 404)

    def test_shared_page_needs_no_login_but_peer_pages_do(self):
        self.httpd.token = "hubtoken-123456"
        try:
            _, p = self.peer.publish_page("a", "r2", "R", "<!doctype html><p>x")
            _, sp = self.peer.share_page(p["id"], True)
            self.assertEqual(self.req(f"/p/pc:{p['id']}")[0], 401)
            self.assertEqual(self.req(f"/api/sessions/pc:a")[0], 401)
            self.assertEqual(self.req(f"/s/pc:{sp['share']}")[0], 200)
        finally:
            self.httpd.token = ""

    def test_upload_streams_to_the_peer(self):
        code, raw, _ = self.req("/api/sessions/pc:a/attachments", raw=b"\x89PNG fake", ctype="application/octet-stream",
                                headers={"X-Filename": "shot%20one.png"})
        r = json.loads(raw)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["size"], 9)
        with open(r["path"], "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG fake")
        self.addCleanup(os.unlink, r["path"])
        sess, fname = r["path"].split("/")[-2:]
        code, body, h = self.req(f"/api/attachments/pc%3A{sess}/{fname}")
        self.assertEqual(code, 200)
        self.assertEqual(h["Content-Security-Policy"], "default-src 'none'")

    def test_plugin_routes_follow_the_session(self):
        code, body, _ = self.req("/api/x/git/graph?session=pc%3Aa")
        self.assertEqual(code, 403)   # a navigation can't reach a peer's plugin either
        code, body, _ = self.req("/api/x/git/graph?session=pc%3Aa", headers={"X-Grovy": "1"})
        self.assertIn(b"pc: plugins", body)   # answered by the peer (its plugins aren't loaded in this test)

    def test_wrong_token_is_a_readable_error(self):
        conf = {"pc": {"socket": self.sock, "token": "wrong-token-123456", "tls": False}}
        peers.write_config(conf, self.conf)
        os.utime(self.conf, ns=(1, 1))
        self.hub.peers.reload()
        self.assertTrue(wait_for(lambda: self.pc().status()["state"] == "offline"))
        self.assertIn("wrong token", self.pc().status()["error"])
        code, d = self.get("/api/sessions/pc:a")
        self.assertEqual(code, 502)
        self.assertIn("grovy peer token", d["error"])

    def test_bad_names_from_a_peer_are_dropped(self):
        pc = self.pc()
        with mock.patch.object(pc, "call", side_effect=[(200, {"sessions": [
                {"name": "../x", "state": "idle"}, {"name": "ok", "state": "weird", "pending_permissions": "9",
                                                    "last_event": 1}]}), (200, {"pages": [{"id": "nope"}]})]):
            with pc.lock:
                pc.full = True
            pc.wake.set()
            self.assertTrue(wait_for(lambda: [s["name"] for s in pc.listed()] == ["pc:ok"]))
        s = pc.listed()[0]
        self.assertEqual((s["state"], s["pending_permissions"]), ("unknown", 0))
        self.assertEqual(pc.listed_pages(), [])

    def test_removing_a_peer(self):
        peers.write_config({}, self.conf)
        os.utime(self.conf, ns=(2, 2))
        self.hub.peers.reload()
        self.assertFalse(self.hub.peers)
        code, d = self.get("/api/sessions")
        self.assertNotIn("peers", d)
        self.assertEqual([s["name"] for s in d["sessions"]], ["a"])


class TestHostilePeer(PeerLink):
    """A peer that sends what grovy never would. Review findings, each checked against the code before the fix."""
    EVIL = "x/../../sessions/main/kill?"
    HX = "d" * 32

    def detail(self, **over):
        d = {"session": {"name": "a", "state": "idle", "pending_permissions": 1, "last_event": 1},
             "events": [{"id": 1, "ts": 1, "session": "a", "type": "Page", "data": {"page": self.EVIL}},
                        {"id": 2, "ts": 2, "session": "a", "type": "Page", "data": {"page": self.HX}}],
             "permissions": [{"id": self.EVIL, "session": "a"}, {"id": self.HX + "\n", "session": "a"},
                             {"id": self.HX, "session": "a", "tool_input": {"command": "ls"}}],
             "prompts": [{"id": self.EVIL, "session": "a"}, {"id": self.HX, "session": "../b"}],
             "pages": {self.EVIL: {"version": 1}, self.HX: {"version": 1, "share": self.EVIL}}}
        d.update(over)
        return d

    def test_hostile_ids_are_dropped(self):
        with mock.patch.object(self.pc(), "call", return_value=(200, self.detail())):
            code, d = self.get("/api/sessions/pc:a")
        self.assertEqual(code, 200)
        self.assertEqual([p["id"] for p in d["permissions"]], ["pc:" + self.HX])
        self.assertEqual([(p["id"], p["session"]) for p in d["prompts"]], [("pc:" + self.HX, None)])
        self.assertEqual(list(d["pages"]), ["pc:" + self.HX])
        self.assertIsNone(d["pages"]["pc:" + self.HX]["share"])
        self.assertEqual([e["data"]["page"] for e in d["events"]], [None, "pc:" + self.HX])
        self.assertNotIn("..", json.dumps(d))

    def test_hostile_ids_in_action_answers(self):
        for verb, ans in (("prompts", {"id": self.EVIL, "session": "a"}), ("rename", {"status": "../x"})):
            with mock.patch.object(self.pc(), "call", return_value=(200, ans)):
                code, d = self.post(f"/api/sessions/pc:a/{verb}", {"text": "x", "name": "b"})
            self.assertEqual(code, 502, verb)
            self.assertIn("unexpected", d["error"])
        with mock.patch.object(self.pc(), "call", return_value=(200, {"page": {"id": self.EVIL, "share": "x"}})):
            self.assertEqual(self.post(f"/api/pages/pc:{self.HX}/share", {"shared": True})[0], 502)
        with mock.patch.object(self.pc(), "call", side_effect=[(200, {"sessions": []}),
                                                               (200, {"pages": [{"id": self.EVIL}, {"id": self.HX}]})]):
            with self.pc().lock:
                self.pc().full = True
            self.pc().wake.set()
            self.assertTrue(wait_for(lambda: [p["id"] for p in self.pc().listed_pages()] == ["pc:" + self.HX]))

    def test_malformed_types_never_break_the_hub(self):
        pc = self.pc()
        with pc.lock:   # what the refresher keeps is checked on the way in; this is the belt to those braces
            pc.sessions = [{"name": "a", "state": ["x"], "meta": "y", "issue": 3, "tasks": {}, "background": "2"},
                           {"name": "b", "state": {"k": 1}, "pending_permissions": -4}]
        code, d = self.get("/api/sessions")
        self.assertEqual(code, 200)
        names = {s["name"]: s for s in d["sessions"]}
        self.assertIn("a", names)   # the hub's own session is still listed
        self.assertEqual(names["pc:a"]["state"], "unknown")
        self.assertEqual((names["pc:a"]["meta"], names["pc:a"]["issue"], names["pc:a"]["tasks"]), (None, None, []))
        self.assertEqual(names["pc:b"]["pending_permissions"], 0)
        for bad in (self.detail(events=5, permissions={"a": 1}, prompts="x", pages=[1]),):
            with mock.patch.object(pc, "call", return_value=(200, bad)):
                code, d = self.get("/api/sessions/pc:a")
            self.assertEqual(code, 200)
            self.assertEqual((d["events"], d["permissions"], d["prompts"], d["pages"]), ([], [], [], {}))
        with mock.patch.object(pc, "call", return_value=(200, self.detail(session=[1]))):
            code, d = self.get("/api/sessions/pc:a")
        self.assertEqual((code, d["error"]), (502, "pc answered something unexpected"))
        with mock.patch.object(pc, "call", return_value=(200, {"conversations": 7})):
            self.assertEqual(self.get("/api/conversations?path=/x&peer=pc"), (200, {"conversations": []}))
        with mock.patch.object(pc, "call", return_value=(404, {"error": ["not", "text"]})):
            self.assertEqual(self.get("/api/sessions/pc:a"), (404, {"error": "pc answered HTTP 404"}))

    def test_refresher_survives_anything(self):
        pc = self.pc()
        with mock.patch.object(pc, "call", return_value=(200, {"sessions": 5})):   # used to kill the thread
            with pc.lock:
                pc.full = True
            pc.wake.set()
            time.sleep(0.3)
        with mock.patch.object(pc, "call", side_effect=RuntimeError("boom")):
            with pc.lock:
                pc.full = True
            pc.wake.set()
            self.assertTrue(wait_for(lambda: pc.status()["state"] == "stale"))
            self.assertIn("RuntimeError", pc.status()["error"])
        self.assertTrue(wait_for(lambda: pc.status()["state"] == "online"))   # it kept retrying, and recovered
        self.assertEqual(pc.listed()[0]["state"], "idle")

    def test_failing_lists_on_a_live_stream_show_unknown(self):
        pc = self.pc()
        with mock.patch.object(pc, "call", return_value=(502, {"error": "pc refused this grovy's login"})):
            self.peer.ingest("a", ev("UserPromptSubmit", prompt="go"))   # a hint: the refresh fails, again and again
            self.assertTrue(wait_for(lambda: pc.status()["state"] == "stale"))
            st = pc.status()
            self.assertIn("HTTP 502", st["error"])
            s = pc.listed()[0]
            self.assertEqual((s["state"], s["accepts_input"]), ("unknown", False))
            self.assertIn("isn't answering", s["activity"])
            self.assertEqual(self.get("/api/sessions")[1]["peers"][0]["state"], "stale")
        self.assertTrue(wait_for(lambda: pc.status()["state"] == "online"))
        self.assertEqual(pc.listed()[0]["state"], "working")

    def test_periodic_resync_repairs_a_lost_hint(self):
        pc = self.pc()
        with mock.patch.object(peers, "RESYNC_EVERY", 0.2):
            pc.wake.set()   # the next wait uses the short period
            with self.peer.lock:   # a change whose hint never arrived (the peer's stream queue overflowed)
                self.peer.sessions["a"]["state"] = "working"
            self.assertTrue(wait_for(lambda: pc.listed()[0]["state"] == "working"))

    def test_generation_guard(self):
        """A full refresh in flight when the link drops must not bring the peer back online with its old answer."""
        pc = self.pc()
        self.fwd.stop()   # no reconnect can make it legitimately online meanwhile
        self.assertTrue(wait_for(lambda: pc.status()["state"] == "offline"))
        entered, release = threading.Event(), threading.Event()
        answer = {"sessions": [{"name": "a", "state": "idle"}]}

        def slow(method, path, *a, **k):
            if path == "/api/sessions":
                entered.set()
                release.wait(5)
                return 200, answer
            return 200, {"pages": []}
        with mock.patch.object(pc, "call", side_effect=slow):
            with pc.lock:
                pc.linked, pc.full = True, True   # as on "hello"
            pc.wake.set()
            self.assertTrue(entered.wait(5))
            pc._offline("dropped while fetching")
            release.set()
            time.sleep(0.5)
        self.assertEqual(pc.status()["state"], "offline")
        self.assertEqual(pc.listed()[0]["state"], "unknown")

    def test_shared_route_tells_nothing_before_login(self):
        gone = (404, b"Not found (this page is no longer shared).")
        self.assertEqual(self.req("/s/mac:" + self.HX)[:2], gone)          # no such machine
        self.assertEqual(self.req("/s/pc:" + self.HX)[:2], gone)           # no such page
        self.fwd.stop()
        self.assertTrue(wait_for(lambda: self.pc().status()["state"] == "offline"))
        self.assertEqual(self.req("/s/pc:" + self.HX)[:2], gone)           # unreachable: nothing about it

    def test_upload_errors(self):
        wrong = peers.Peer("pc", {"socket": self.sock, "token": "wrong-token-123456", "tls": False}, self.hub.peers)
        code, d = wrong.upload("/api/sessions/a/attachments", "f.txt", io.BytesIO(b"abc").read, 3)
        self.assertEqual(code, 502)
        self.assertIn("refused this grovy's login", d["error"])

        def slow(n):
            raise socket.timeout("timed out")
        code, d = self.pc().upload("/api/sessions/a/attachments", "f.txt", slow, 3)
        self.assertEqual(code, 408)
        self.assertIn("stopped arriving from this device", d["error"])
        self.assertNotIn("offline", d["error"])

    def test_offline_sessions_have_no_background_work(self):
        pc = self.pc()
        with pc.lock:
            pc.sessions = [{"name": "a", "state": "idle", "background": 2, "tasks": [{"id": "t"}]}]
        pc._offline("test")
        s = pc.listed()[0]
        self.assertEqual((s["background"], s["tasks"]), (0, []))

    def test_notifications_throttled_and_stop_with_close(self):
        pc = self.pc()
        for i in range(peers.NOTIFY_MAX + 5):
            pc._message({"notify": {"session": "a", "kind": "done", "text": f"n{i}"}})
        self.assertEqual(len(self.hub.notifier.sent), peers.NOTIFY_MAX)
        old = peers.Peer("pc", pc.conf, self.hub.peers)
        old.close()
        old._message({"notify": {"session": "a", "kind": "input", "text": "late"}})
        self.assertEqual(len(self.hub.notifier.sent), peers.NOTIFY_MAX)
        conn = mock.Mock()
        with mock.patch.object(old, "connection", return_value=conn):   # closed before its stream connected
            old._stream()
        conn.request.assert_not_called()


class TestPeerRound2(PeerLink):
    """Second review: each of these failed on the code before the fix."""

    def test_a_flood_of_finished_never_swallows_needs_you(self):
        pc = self.pc()
        for i in range(peers.NOTIFY_MAX + 5):
            pc._message({"notify": {"session": "a", "kind": "done", "text": f"n{i}"}})
        pc._message({"notify": {"session": "a", "kind": "input", "text": "permission: Bash"}})
        kinds = [k for _, k, _ in self.hub.notifier.sent]
        self.assertEqual(kinds.count("done"), peers.NOTIFY_MAX)   # the flood is still cut
        self.assertEqual(kinds.count("input"), 1)
        with pc.lock:   # the catch-up after an outage uses the "needs you" budget too
            pc.sessions = [{"name": "b", "state": "idle"}]
        pc._missed(pc.sessions, [{"name": "b", "state": "needs_input"}])
        self.assertEqual([x[:2] for x in self.hub.notifier.sent][-1], ("pc:b", "input"))

    def test_full_resync_happens_under_steady_hints(self):
        pc = self.pc()
        q = self.subscribe()
        with pc.lock:
            pc.full_at = time.monotonic() - peers.RESYNC_EVERY - 1   # the last full refresh is a minute old
        self.peer.ingest("a", ev("UserPromptSubmit", prompt="go"))     # a hint, which also wakes the refresher
        self.assertTrue(wait_for(lambda: any(json.loads(m).get("session") is None for m in list(q))), list(q))

    def test_a_list_that_isnt_one_is_a_failure(self):
        pc = self.pc()
        with mock.patch.object(pc, "call", return_value=(200, {"sessions": 5, "pages": []})):
            with pc.lock:
                pc.full = True
            pc.wake.set()
            self.assertTrue(wait_for(lambda: pc.status()["state"] == "stale"))
            self.assertIn("isn't a list", pc.status()["error"])
            self.assertEqual(pc.listed()[0]["state"], "unknown")   # never "online with no sessions"

    def test_failed_refresh_on_a_dropped_link_does_not_spin(self):
        pc = self.pc()
        self.fwd.stop()
        self.assertTrue(wait_for(lambda: pc.status()["state"] == "offline"))
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("boom")
        # (the stream thread's reconnect attempts reset the queue when they fail: keep them out of the measurement)
        with mock.patch.object(pc, "call", side_effect=boom), mock.patch.object(pc, "_offline"):
            with pc.lock:
                pc.dirty.add("a")
            pc.wake.set()
            time.sleep(2.5)
        self.assertEqual(len(calls), 1)   # the next hello re-fetches everything; no retry loop meanwhile


class TestNoPeers(unittest.TestCase):
    def test_nothing_changes_without_peers_json(self):
        with mock.patch.object(sessions, "registry", lambda: {}):
            st = store.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
            st.reconcile()
            st.peers = peers.Peers(st, path=os.path.join(TMP, "none", "peers.json"))
            st.peers.reload()
            self.assertFalse(st.peers)
            httpd = serve(st, "")
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}"
                with urllib.request.urlopen(url + "/api/sessions", timeout=5) as r:
                    d = json.loads(r.read())
                self.assertEqual(set(d), {"sessions", "now"})
                with self.assertRaises(urllib.error.HTTPError) as e:   # not a valid name, as before
                    urllib.request.urlopen(url + "/api/sessions/pc:a", timeout=5)
                self.assertEqual(e.exception.code, 404)
            finally:
                httpd.shutdown()
                httpd.server_close()


    def test_only_a_hubs_stream_gets_notifications(self):
        with mock.patch.object(sessions, "registry", lambda: {}):
            st = store.Store(tempfile.mkdtemp(dir=TMP), tmux_live=lambda: {"a": "%1"}, paste=lambda *a: None)
        phone, hub = grovyd.Subscriber(maxlen=10), grovyd.Subscriber(maxlen=10)
        hub.notify = True
        st.subscribers |= {phone, hub}
        st._notify("a", "done", "finished")
        self.assertEqual(list(phone), [])   # browsers' streams are exactly as before
        self.assertEqual(json.loads(hub[0]), {"notify": {"session": "a", "kind": "done", "text": "finished"}})


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.path = os.path.join(self.dir, "peers.json")

    def write(self, data, mode=0o600):
        with open(self.path, "w") as f:
            json.dump(data, f)
        os.chmod(self.path, mode)

    def test_read_validates(self):
        self.write({"pc": {"socket": "/s/pc.sock", "token": "abcdefgh12"},
                    "Bad Name": {"socket": "/s", "token": "abcdefgh12"},
                    "rel": {"socket": "pc.sock", "token": "abcdefgh12"},
                    "inj": {"socket": "/s/x.sock", "token": "abc\r\nX-Evil: 1"},
                    "long": {"socket": "/" + "x" * 120, "token": "abcdefgh12"}})
        conf, problems = peers.read_config(self.path)
        self.assertEqual(conf, {"pc": {"socket": "/s/pc.sock", "token": "abcdefgh12", "tls": False}})
        self.assertEqual(len(problems), 4)

    def test_others_may_not_read_it(self):
        self.write({"pc": {"socket": "/s/pc.sock", "token": "abcdefgh12"}}, 0o644)
        conf, problems = peers.read_config(self.path)
        self.assertEqual(conf, {})
        self.assertIn("chmod 600", problems[0])

    def test_written_private(self):
        peers.write_config({"pc": {"socket": "/s/pc.sock", "token": "abcdefgh12", "tls": False}}, self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_cli_add_reads_the_token_from_stdin(self):
        cfg = os.path.join(self.dir, "cfg")
        with mock.patch.dict(os.environ, {"GROVY_CONFIG_DIR": cfg}), \
                mock.patch("sys.stdin", io.StringIO("tok-from-stdin-123\n")), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(peers.cmd_add(types.SimpleNamespace(name="pc", socket=None, tls=False)), 0)
            conf, _ = peers.read_config()
            self.assertEqual(conf["pc"]["token"], "tok-from-stdin-123")
            self.assertEqual(conf["pc"]["socket"], peers.default_socket("pc"))
            self.assertEqual(peers.cmd_rm(types.SimpleNamespace(name="pc")), 0)
            self.assertEqual(peers.read_config()[0], {})
        with self.assertRaises(SystemExit):
            peers.cmd_add(types.SimpleNamespace(socket=None, tls=False, name="PC Two"))

    def test_ids_are_whole_matches(self):
        hx = "a" * 32
        self.assertIsNone(core.ID_RE.match(hx + "\n"))   # "$" let a trailing newline through
        self.assertTrue(core.ID_RE.match(hx))
        self.assertFalse(peers.is_id(hx + "\n"))

    def test_uid_check(self):
        d = tempfile.mkdtemp(dir=TMP)
        os.chmod(d, 0o700)
        path = os.path.join(d, "s.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        self.addCleanup(s.close)
        with mock.patch.object(peers.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(PermissionError, "not a socket of this user's"):
                peers.check_socket(path)

    def test_names(self):
        self.assertEqual(peers.split("pc:foo"), ("pc", "foo"))
        self.assertIsNone(peers.split("foo"))
        self.assertTrue(peers.valid_peer("pc-2"))
        for bad in ("", "PC", "a:b", "-x", "x" * 17):
            self.assertFalse(peers.valid_peer(bad), bad)

    def test_socket_checks(self):
        d = tempfile.mkdtemp(dir=TMP)
        path = os.path.join(d, "s.sock")
        with self.assertRaises(FileNotFoundError):
            peers.check_socket(path)
        self.assertIn("grovy peer tunnel", peers.reason(FileNotFoundError(), "pc"))
        open(path, "w").close()
        with self.assertRaises(PermissionError):   # not a socket
            peers.check_socket(path)
        os.unlink(path)
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        self.addCleanup(s.close)
        os.chmod(d, 0o700)
        peers.check_socket(path)
        os.chmod(d, 0o777)   # anyone could have swapped it in
        with self.assertRaises(PermissionError):
            peers.check_socket(path)
        os.chmod(d, 0o700)


class TestRewrite(unittest.TestCase):
    def test_detail(self):
        hx = "a" * 32
        d = peers.rewrite_detail("pc", {
            "session": {"name": "x", "state": "idle", "pending_permissions": 0, "last_event": 5},
            "now": 1,
            "events": [{"id": 3, "session": "x", "type": "Page", "data": {"page": hx}},
                       {"id": 4, "session": "x", "type": "UserPromptSubmit", "data": {"prompt": "id " + hx}}],
            "permissions": [{"id": hx, "session": "x", "tool_input": {"id": hx, "session": "y"}}],
            "prompts": [{"id": "b" * 32, "session": "x", "text": "hi"}],
            "pages": {hx: {"version": 1, "title": "t", "share": "c" * 32}}})
        self.assertEqual(d["session"]["name"], "pc:x")
        self.assertNotIn("now", d)
        self.assertEqual(d["events"][0]["data"]["page"], "pc:" + hx)
        self.assertEqual(d["events"][1]["data"]["prompt"], "id " + hx)
        self.assertEqual(d["events"][0]["id"], 3)   # event ids stay numbers (history paging)
        self.assertEqual(d["permissions"][0]["id"], "pc:" + hx)
        self.assertEqual(d["permissions"][0]["tool_input"], {"id": hx, "session": "y"})   # never touched
        self.assertEqual(d["prompts"][0]["id"], "pc:" + "b" * 32)
        self.assertEqual(d["pages"]["pc:" + hx]["share"], "pc:" + "c" * 32)

    def test_conversations(self):
        d = peers.rewrite_conversations("pc", {"conversations": [{"open_in": "grovy:foo"}, {"open_in": "terminal"}]})
        self.assertEqual([c["open_in"] for c in d["conversations"]], ["grovy:pc:foo", "terminal"])


class TestPage(unittest.TestCase):
    def test_open_in_prefix(self):
        """Regression: "Open session y:NAME": open_in is "grovy:NAME" (6 characters before the name, not 4)."""
        with open(os.path.join(base.ROOT, "index.html")) as f:
            html = f.read()
        self.assertNotIn("open_in.slice(4)", html)
        self.assertIn('c.open_in.slice("grovy:".length)', html)

    def test_ids_are_encoded_in_paths(self):
        """Review: a peer's id went into /api/permissions/${p.id}/decision unencoded."""
        with open(os.path.join(base.ROOT, "index.html")) as f:
            html = f.read()
        self.assertIsNone(re.search(r"/\$\{(p\.)?id\}", html))
        self.assertNotIn('"/p/" + id', html)
        self.assertIn("/api/permissions/${encodeURIComponent(p.id)}/decision", html)

    def test_route_accepts_peer_names(self):
        with open(os.path.join(base.ROOT, "index.html")) as f:
            html = f.read()
        m = re.search(r"const m = hash\.match\((/.*/)\);", html)
        js = f"const r = {m.group(1)}; console.log(JSON.stringify(['#/s/pc:foo', '#/s/foo', '#/s/a:b:c', '#/s/PC:x']" \
             ".map(h => (h.match(r) || [])[1] || null)))"
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), ["pc:foo", "foo", None, None])


class TestTunnel(unittest.TestCase):
    def test_argv(self):
        argv = peers.tunnel_argv("me@hub", "/home/me/.local/state/grovy/peers/pc.sock", 7333)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("ServerAliveInterval=30", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[argv.index("-R") + 1], "/home/me/.local/state/grovy/peers/pc.sock:127.0.0.1:7333")
        self.assertEqual(argv[-2:], ["--", "me@hub"])
        self.assertIn("-N", argv)

    def run_script(self, script, home):
        return subprocess.run(["sh", "-c", script], env={"HOME": home, "PATH": os.environ["PATH"]},
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_prepare_default_and_custom(self):
        home = tempfile.mkdtemp(dir=TMP)
        argv = peers.prepare_argv("hub", "pc")
        self.assertEqual(argv[-2], "hub")
        self.assertTrue(argv[-1].startswith("sh -c "))
        stale = os.path.join(home, ".local/state/grovy/peers/pc.sock")
        os.makedirs(os.path.dirname(stale))
        open(stale, "w").close()
        out = self.run_script(peers.prepare_script("pc"), home)
        self.assertEqual(out, stale)
        self.assertFalse(os.path.exists(stale))   # a stale socket would make the forward fail
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(stale)).st_mode), 0o700)
        out = self.run_script(peers.prepare_script("pc", "~/s d/$(touch pwned).sock"), home)
        self.assertEqual(out, os.path.join(home, "s d/$(touch pwned).sock"))   # quoted, never run
        self.assertFalse(os.path.exists(os.path.join(home, "pwned")))
        # the remote login shell parses ssh's command: check the whole line the way a shell would
        line = peers.prepare_argv("hub", "pc", "~/x.sock")[-1]
        self.assertEqual(subprocess.run(["sh", "-c", line], env={"HOME": home, "PATH": os.environ["PATH"]},
                                        capture_output=True, text=True).stdout.strip(), os.path.join(home, "x.sock"))

    def test_loop_backs_off_and_reconnects(self):
        calls, sleeps = [], []
        ok = mock.Mock(returncode=0, stdout="/h/.local/state/grovy/peers/pc.sock\n", stderr="")
        fail = mock.Mock(returncode=255, stdout="", stderr="Connection refused")
        runs = iter([fail, ok, ok])
        proc = mock.Mock()
        proc.wait.return_value = 255

        def popen(argv, **kw):
            calls.append(argv)
            return proc
        out = io.StringIO()
        clock = iter(range(0, 1000, 1))
        code = peers.run_tunnel("hub", "pc", 7333, run=lambda *a, **k: next(runs), popen=popen,
                                sleep=sleeps.append, clock=lambda: next(clock), rounds=3, out=out)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][calls[0].index("-R") + 1], "/h/.local/state/grovy/peers/pc.sock:127.0.0.1:7333")
        self.assertEqual(sleeps, [2, 4])
        self.assertIn("Connection refused", out.getvalue())

    def test_no_prepare_runs_nothing_on_the_hub(self):
        calls = []
        proc = mock.Mock()
        proc.wait.return_value = 0
        run = mock.Mock()
        code = peers.run_tunnel("hub", "pc", 7333, remote="/h/s/pc.sock", run=run,
                                popen=lambda argv, **k: calls.append(argv) or proc, sleep=lambda s: None,
                                rounds=1, out=io.StringIO(), prepare=False)
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertEqual(calls[0][calls[0].index("-R") + 1], "/h/s/pc.sock:127.0.0.1:7333")
        self.assertNotIn("sh -c", " ".join(calls[0]))
        for bad in (None, "~/s.sock"):   # without a remote shell nothing expands ~
            with self.assertRaises(SystemExit):
                peers.cmd_tunnel(types.SimpleNamespace(host="hub", remote_socket=bad, port=7333, name="pc",
                                                       local_host="127.0.0.1", no_prepare=True))

    def test_bad_remote_path_stops(self):
        bad = mock.Mock(returncode=0, stdout="/" + "x" * 150 + "\n", stderr="")
        out = io.StringIO()
        self.assertEqual(peers.run_tunnel("hub", "pc", 7333, run=lambda *a, **k: bad, popen=mock.Mock(),
                                          sleep=lambda s: None, rounds=3, out=out), 2)
        self.assertIn("--remote-socket", out.getvalue())

    def test_cli_refuses_option_like_host(self):
        with self.assertRaises(SystemExit):
            peers.cmd_tunnel(types.SimpleNamespace(host="-oProxyCommand=x", remote_socket=None, port=7333, name="pc",
                                                   local_host="127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
