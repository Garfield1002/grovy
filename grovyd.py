"""grovyd - the grovy daemon: session state from hooks, web UI, permission decisions, prompt queue.

Two listeners:
  * a Unix socket (state dir, mode 0600) for hook events and the CLI; never on the network
  * an HTTP server (default 127.0.0.1) for the browser: JSON API + SSE + index.html

One RLock guards all in-memory state. Nothing here is the parent of a Claude process.
"""
import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import sys
import threading
import time
from collections import deque
from html import escape as html_escape
from urllib.parse import urlsplit, parse_qs, unquote

from grovylib import claude, client, commands, core, notify, oom, pages, plugins
from grovylib.store import Store


MAX_BODY = 256 * 1024


SSE_PER_CLIENT = 4  # live-update streams kept per client IP (older ones are closed)


RECONCILE_EVERY = 10


class SockHandler(socketserver.StreamRequestHandler):
    def handle(self):
        store = self.server.store
        try:
            line = self.rfile.readline(8 * pages.PAGE_MAX)   # a page's HTML, JSON-escaped
            msg = json.loads(line)
        except (ValueError, OSError):
            return
        t = msg.get("type") if isinstance(msg, dict) else None
        if t == "hook":
            name, payload = msg.get("session") or msg.get("ccs"), msg.get("event")
            if not core.valid_name(name) or not isinstance(payload, dict):
                return
            if payload.get("hook_event_name") == "PermissionRequest":
                self._permission(store, name, payload)
            else:
                store.ingest(name, payload)
                if msg.get("want_steer"):   # the main agent's PostToolUse hook waits for this answer
                    steer = None
                    if payload.get("hook_event_name") == "PostToolUse" and not payload.get("agent_id"):
                        steer = store.take_steers(name)
                    try:
                        self._reply({"steer": steer})
                    except OSError:
                        pass   # the hook gave up waiting; the message was marked steered but may not have arrived
        elif t == "status":
            self._reply({"sessions": {s["name"]: s for s in store.list_sessions()}, "url": self.server.url,
                         "claude": shutil.which(claude.claude_cmd())})
        elif t == "refresh":
            store.reconcile()
        elif t == "statusline":
            name = msg.get("session")
            if core.valid_name(name) and isinstance(msg.get("data"), dict):
                store.status(name, msg["data"])
        elif t == "rename":
            code, res = store.rename(str(msg.get("old", "")), msg.get("new"))
            self._reply({"ok": res} if code == 200 else {"error": res})
        elif t == "live":
            code, res = store.live_update(str(msg.get("session") or ""), msg.get("key"), msg.get("spec"),
                                          msg.get("status", "running"), msg.get("message"))
            try:   # the client may not wait for the answer
                self._reply({"ok": res} if code == 200 else {"error": res})
            except OSError:
                pass
        elif t == "page":
            op, session = msg.get("op"), str(msg.get("session") or "")
            if op == "publish":
                code, res = store.publish_page(session, msg.get("name"), msg.get("title"), msg.get("html"))
                if code == 200:
                    res["url"] = (self.server.url or "").rstrip("/") + "/p/" + res["id"]
            elif op == "ls":
                code, res = 200, store.list_pages(session or None)
            elif op == "rm":   # sharing is deliberately not here: only the phone (a person) can share a page
                pid = next((p["id"] for p in store.list_pages(session) if p["name"] == msg.get("name")), None)
                code, res = store.delete_page(pid, session) if pid else (404, "no such page in this session")
            else:
                code, res = 400, "unknown page op"
            self._reply({"ok": res} if code == 200 else {"error": res})

    def _reply(self, obj):
        self.wfile.write(json.dumps(obj).encode() + b"\n")
        self.wfile.flush()

    def _permission(self, store, name, payload):
        p = store.open_permission(name, payload)
        reply = store.wait_permission(p, self.connection)
        try:
            self._reply(reply)
            store.complete_permission(p, delivered=True)
        except OSError as e:
            store.complete_permission(p, delivered=False, error=str(e))


class SockServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


INDEX_PATH = os.path.join(core.HERE, "index.html")


CUSTOM_MAX = 256 * 1024
# What is added to the page: grovy's built-in extensions (extensions/*.js in the grovy folder), then the user's own
# files from their config folder (never overwritten by updates): custom.css after the built-in styles, custom.js after
# every script. Text that would end its block early is refused, so a file can't change the page around it.
CUSTOM = {"custom.css": (b"</style",), "custom.js": (b"</script", b"<!--")}
EXTENSIONS_DIR = os.path.join(core.HERE, "extensions")


def custom_path(name):
    return os.path.join(core.config_dir(), name)


def builtin_extensions():
    """Paths of grovy's own extensions, in load order (by file name)."""
    try:
        return [os.path.join(EXTENSIONS_DIR, f) for f in sorted(os.listdir(EXTENSIONS_DIR)) if f.endswith(".js")]
    except OSError:
        return []


def page_file(path, forbidden):
    """A file to add to the page, as UTF-8 bytes with LF line endings, or b"" (missing, not a regular file, too big,
    not UTF-8, or containing text that would end its block). Line endings matter: the browser turns CR LF into LF
    before checking the script's CSP hash, so the hash must be of the LF text."""
    try:   # opened without blocking, then checked: a FIFO (even one swapped in at the last moment) can't hang us
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with open(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not a regular file")
            data = f.read(CUSTOM_MAX + 1)
        if len(data) > CUSTOM_MAX:
            raise ValueError(f"over {CUSTOM_MAX // 1024} KB")
        data.decode("utf-8")
        if b"\0" in data:
            raise ValueError("contains a NUL byte")
    except FileNotFoundError:
        return b""
    except (OSError, ValueError) as e:   # UnicodeDecodeError is a ValueError
        print(f"grovyd: ignoring {path}: {'not UTF-8' if isinstance(e, UnicodeDecodeError) else e}", file=sys.stderr,
              flush=True)
        return b""
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    bad = [t.decode() for t in forbidden if t in data.lower()]
    if bad:
        print(f"grovyd: ignoring {path}: contains {bad[0]}", file=sys.stderr, flush=True)
        return b""
    return data


def custom_file(name):
    """The user's custom.css or custom.js (see page_file)."""
    return page_file(custom_path(name), CUSTOM[name])


ASSETS_DIR = os.path.join(core.HERE, "assets")


# Public static files: an allowlist, never a path from the request.
ASSETS = {"icon-64.png": "image/png", "icon-180.png": "image/png", "icon-192.png": "image/png",
          "icon-512.png": "image/png", "backdrop.svg": "image/svg+xml"}


MANIFEST = json.dumps({
    "name": "grovy", "short_name": "grovy", "start_url": "/", "display": "standalone",
    "background_color": "#161615", "theme_color": "#161615",
    "icons": [{"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode()


def load_index():
    with open(INDEX_PATH, "rb") as f:   # LF, as the browser sees it (a CR LF checkout would break the hash)
        html = f.read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    scripts = re.findall(rb"<script>(.*?)</script>", html, re.S)   # grovy's own script (taken before anything is added)
    css = custom_file("custom.css")
    if css:   # after the built-in styles, so the same selectors and tokens override them
        html = html.replace(b"</style>", b"</style>\n<style id=\"custom-css\">\n" + css + b"\n</style>", 1)
    # After grovy's script, which starts the page only once it is all parsed: built-in extensions, then custom.js.
    added = [b"\n" + js + b"\n" for js in [page_file(p, CUSTOM["custom.js"]) for p in builtin_extensions()]
             + [custom_file("custom.js")] if js]
    if added:
        i = html.rindex(b"</script>") + len(b"</script>")
        html = html[:i] + b"".join(b"\n<script>" + js + b"</script>" for js in added) + html[i:]
    # Hashes of exactly the blocks above, never a pattern match over the assembled page (custom.css text such as
    # "<script>" in a comment would otherwise shift what gets hashed).
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(s).digest()).decode() for s in scripts + added)
    csp = (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; connect-src 'self'; "
           "img-src 'self' data:; frame-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'")
    return html, csp


REQ_ID_RE = re.compile(r"[A-Za-z0-9-]{16,64}")
def plugins_on():
    return core.env("PLUGINS", "1") != "0"


PLUGIN_PATH = re.compile(r"/api/x/([a-z0-9][a-z0-9_-]{0,31})/(.+)")   # a plugin's own routes (grovylib/plugins.py)


HOST_PAGE = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
             b"<title>grovy</title><body style='font:16px system-ui;padding:24px;max-width:30em'>"
             b"<h2>grovy doesn't know the name &ldquo;{host}&rdquo;</h2><p>To protect against DNS rebinding, grovy only "
             b"answers to IP addresses and names it was told about. Either open it by IP address, or restart the "
             b"daemon with <code>GROVY_HOSTS={host}</code> in its environment.</p>")


LOGIN_PAGE = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
              b"<title>grovy</title><body style='font:16px system-ui;padding:24px;max-width:30em'>"
              b"<h2>grovy: login needed</h2><p>Open the link printed by <code>grovy url</code> on the machine "
              b"(it ends in <code>?token=...</code>). Your browser remembers it for a year.</p>")


def _res(code, res, key=None):
    """(code, JSON body) for a Store answer: the result (under `key` if given) when it worked, else {"error": ...}."""
    if code != 200:
        return code, {"error": res}
    return code, ({key: res} if key else res)


class Http(http.server.BaseHTTPRequestHandler):
    server_version = "grovyd"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if core.env("DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json", extra=(), frame="DENY"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        textual = ctype.startswith("text/") or ctype.endswith(("json", "+xml", "javascript"))
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if textual else ""))
        self.send_header("Content-Length", str(len(body)))
        if not any(k == "Cache-Control" for k, _ in extra):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", frame)
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        # One request per connection: an idle keep-alive socket closed by our deadline at the moment the
        # browser reuses it makes that request fail ("page sometimes fails to load"). On a LAN a fresh
        # connection costs nothing.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _authed(self):
        token = self.server.token
        if not token or not self.server.enforce:
            return True
        got = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            got = auth[7:]
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k in ("grovy_token", "ccs_token"):  # ccs_token: cookie set before the rename
                got = got or v
        return hmac.compare_digest(got.encode(), token.encode())

    timeout = 30  # seconds of client silence before a connection is dropped
    HEADER_DEADLINE = 15  # whole request line + headers must arrive within this (per-recv timeouts can be trickled)

    def handle_one_request(self):
        t = self._deadline = threading.Timer(self.HEADER_DEADLINE, self._abort)
        t.daemon = True
        t.start()
        self.server.waiting_add(self)
        try:
            super().handle_one_request()
        finally:
            t.cancel()
            self.server.waiting_remove(self)

    def parse_request(self):
        ok = super().parse_request()  # reads the headers; past this point the deadline no longer applies
        self._deadline.cancel()
        self.server.waiting_remove(self)
        return ok

    def _abort(self):
        if core.env("DEBUG"):
            print(f"grovyd: closed idle connection from {self.client_address[0]} "
                  f"(no request within {self.HEADER_DEADLINE}s)", file=sys.stderr, flush=True)
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _host_ok(self):
        """DNS-rebinding guard: a browser reaching us through an attacker's name sends that name as
        Host. Accept only IP literals, localhost, this machine's names, and GROVY_HOSTS."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name in self.server.allowed_hosts

    def do_GET(self):
        if not self._host_ok():
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0][:100]
            if urlsplit(self.path).path == "/":
                return self._send(403, HOST_PAGE.replace(b"{host}", html_escape(host).encode()), "text/html")
            return self._err(403, "unknown Host header (add the name to GROVY_HOSTS)")
        url = urlsplit(self.path)
        path = url.path
        if path == "/" and self.server.token:
            q = parse_qs(url.query).get("token", [""])[0]
            if q and hmac.compare_digest(q.encode(), self.server.token.encode()):
                return self._send(303, b"", "text/plain", [
                    ("Location", "/"),
                    ("Set-Cookie", f"grovy_token={q}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")])
        if path.startswith("/assets/") and path[8:] in ASSETS:   # icons and backdrop: nothing private
            try:
                with open(os.path.join(ASSETS_DIR, path[8:]), "rb") as f:
                    data = f.read()
            except OSError:
                return self._err(404, "not found")
            ctype = ASSETS[path[8:]]
            return self._send(200, data, ctype, [("Cache-Control", "max-age=86400")] +
                              ([("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")]
                               if ctype == "image/svg+xml" else []))
        if path == "/manifest.webmanifest":
            return self._send(200, MANIFEST, "application/manifest+json")
        m = re.fullmatch(r"/s/([0-9a-f]{32})", path)
        if m:   # a shared page: its link is the credential, and it grants nothing but this page
            got = self.server.store.shared_page_html(m.group(1))
            if not got:
                return self._send(404, b"Not found (this page is no longer shared).", "text/plain")
            return self._send(200, got[1], "text/html", [("Content-Security-Policy", pages.PAGE_CSP)])
        if not self._authed():
            if path == "/":
                return self._send(401, LOGIN_PAGE, "text/html")
            return self._err(401, "token required: open /?token=...")
        store = self.server.store
        if path == "/":
            self.server.refresh_index()
            index, csp = self.server.page   # one read: a reload in between can't pair a page with another's CSP
            return self._send(200, index, "text/html", [("Content-Security-Policy", csp)])
        if path == "/api/sessions":
            return self._send(200, {"sessions": store.list_sessions(), "now": time.time()})
        if path == "/api/permissions":
            return self._send(200, {"permissions": store.permissions()})
        if path == "/api/events":
            return self._sse()
        if path == "/api/projects":
            return self._send(200, {"projects": store.lifecycle.trusted_projects()})
        if path == "/api/issues/projects":
            return self._send(*_res(*store.issue_projects(), key="projects"))
        m = re.fullmatch(r"/api/issues/([^/]+)", path)
        if m and core.valid_name(m.group(1)):
            return self._send(*_res(*store.issue_status(m.group(1))))
        if path == "/api/conversations":
            code, res = store.conversations(parse_qs(url.query).get("path", [""])[0])
            return self._send(code, {"conversations": res} if code == 200 else {"error": res})
        m = re.fullmatch(r"/api/(attachments|images)/([^/]+)/([^/]+)", path)
        if m:   # thumbnails and image widgets; only images, never rendered as anything else
            got = store.read_attachment(m.group(2), m.group(3), images=m.group(1) == "images")
            if not got:
                return self._err(404, "not found")
            return self._send(200, got[1], got[0], [("Cache-Control", "private, max-age=604800"),
                                                    ("Content-Security-Policy", "default-src 'none'")])
        m = re.fullmatch(r"/api/sessions/([^/]+)/history", path)
        if m and core.valid_name(m.group(1)):
            qs = parse_qs(url.query)
            q = (qs.get("q", [""])[0] or "").strip()[:200]
            try:
                before = int(qs.get("before", ["0"])[0]) or None
            except ValueError:
                return self._err(400, "bad 'before'")
            if not q and before is None:
                return self._err(400, "need 'before' or 'q'")
            return self._send(200, {"events": store.history(m.group(1), before=before, q=q or None)})
        m = re.fullmatch(r"/p/([0-9a-f]{32})", path)
        if m:
            got = store.page_html(m.group(1))
            if not got:
                return self._send(404, b"This page was deleted.", "text/plain")
            p, html = got
            return self._send(200, pages.with_bridge(html), "text/html",
                              [("Content-Security-Policy", pages.PAGE_CSP)], frame="SAMEORIGIN")
        if path == "/api/pages":
            return self._send(200, {"pages": store.list_pages()})
        m = PLUGIN_PATH.fullmatch(path)
        if m and store.plugins is None and plugins_on():
            return self._err(503, "plugins are still loading: try again in a moment")
        if m and store.plugins:
            if self.headers.get("X-Grovy") != "1":   # only fetch() can add it: a tapped link or a navigation can't
                return self._err(403, "plugin routes are for grovy.api() (X-Grovy header missing)")
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            return self._send(*store.plugins.handle("GET", m.group(1), m.group(2), query, None))
        m = re.fullmatch(r"/api/sessions/([^/]+)/pages", path)
        if m and core.valid_name(m.group(1)):
            return self._send(200, {"pages": store.list_pages(m.group(1))})
        m = re.fullmatch(r"/api/sessions/([^/]+)/commands", path)
        if m and core.valid_name(m.group(1)):
            s = store.sessions.get(m.group(1))
            return self._send(200, {"commands": commands.commands_for(s.get("cwd"))}) if s else self._err(404, "unknown session")
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if m and core.valid_name(m.group(1)):
            d = store.detail(m.group(1))
            return self._send(200, d) if d else self._err(404, "unknown session")
        self._err(404, "not found")

    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "unknown Host header (add the name to GROVY_HOSTS)")
        if not self._authed():
            return self._err(401, "unauthorized")
        # CSRF: JSON content type forces a CORS preflight we never answer; Origin, when sent, must be us.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._err(403, "cross-origin request rejected")
        m = re.fullmatch(r"/api/sessions/([^/]+)/attachments", urlsplit(self.path).path)
        if m:   # a raw file body, streamed to disk; octet-stream still forces a CORS preflight
            return self._upload(m.group(1))
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._err(415, "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._err(400, "bad Content-Length")
        if n > MAX_BODY:
            return self._err(413, "body too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._err(400, "invalid JSON object")
        path = urlsplit(self.path).path
        # Idempotency: the page retries an action once if the network drops it, with the same request id.
        # A repeat returns the first outcome (waiting if that one is still running) instead of acting twice.
        rid = self.headers.get("X-Request-Id", "")
        if not REQ_ID_RE.fullmatch(rid):
            return self._send(*self._route_post(path, body))
        with self.server.done_lock:
            entry = self.server.done.get(rid)
            first = entry is None
            if first:
                entry = self.server.done[rid] = {"ev": threading.Event(), "res": None, "t": core.now()}
                for k in [k for k, v in self.server.done.items() if core.now() - v["t"] > 600]:
                    del self.server.done[k]
        if first:
            try:
                entry["res"] = self._route_post(path, body)
            except Exception:
                entry["res"] = (500, {"error": "internal error"})
                raise
            finally:
                entry["ev"].set()
        elif not entry["ev"].wait(60):
            return self._err(503, "the original request is still running")
        return self._send(*entry["res"])

    def _upload(self, name):
        self.close_connection = True   # an error leaves the body unread
        if not core.valid_name(name):
            return self._err(404, "no such session")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/octet-stream":
            return self._err(415, "Content-Type must be application/octet-stream")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._err(411, "Content-Length required")
        code, res = self.server.store.save_attachment(name, unquote(self.headers.get("X-Filename", "")),
                                                      self.rfile.read, n)
        return self._send(code, res if code == 200 else {"error": res})

    def _route_post(self, path, body):
        """Run one POST action; returns (status, JSON body). Never sends anything itself."""
        store = self.server.store
        wrap = lambda code, res: (code, {"status": res} if code == 200 else {"error": res})  # noqa: E731
        m = re.fullmatch(r"/api/sessions/([^/]+)/prompts", path)
        if m and core.valid_name(m.group(1)):
            code, res = store.enqueue(m.group(1), body.get("text"), body.get("force") is True)
            return code, (res if code == 200 else {"error": res})
        m = re.fullmatch(r"/api/permissions/([^/]+)/decision", path)
        if m and core.ID_RE.match(m.group(1)):
            return wrap(*store.decide(m.group(1), body.get("decision"), body.get("answers")))
        if path == "/api/client-error":  # the phone reports render errors it would otherwise swallow
            if core.now() - self.server.last_client_error > 5:
                self.server.last_client_error = core.now()
                msg = " | ".join(str(body.get(k, ""))[:2000].replace("\n", " ⏎ ") for k in ("view", "message", "stack"))
                print(f"grovyd: CLIENT ERROR from {self.client_address[0]}: {msg}", file=sys.stderr, flush=True)
            return 200, {"status": "logged"}
        if path == "/api/login-link":
            # For ⋯ → Login link (signing in another device). The cookie is HttpOnly so the page can't read it; only
            # a logged-in, same-origin POST (JSON, so a cross-site page can't even send it) gets the token back.
            return 200, {"token": self.server.token or ""}
        if path == "/api/sessions":
            return wrap(*store.create(body.get("name"), body.get("path"), body.get("ask"),
                                      body.get("resume"), body.get("fork")))
        if path == "/api/issues":
            return _res(*store.create_issue(body.get("path"), body.get("text"), body.get("ask")))
        m = re.fullmatch(r"/api/issues/([^/]+)/(finish|discard)", path)
        if m and core.valid_name(m.group(1)):
            if m.group(2) == "finish":
                return _res(*store.finish_issue(m.group(1), body.get("tip")))
            return _res(*store.discard_issue(m.group(1)))
        m = re.fullmatch(r"/api/sessions/([^/]+)/rename", path)
        if m and core.valid_name(m.group(1)):
            return wrap(*store.rename(m.group(1), body.get("name")))
        m = re.fullmatch(r"/api/sessions/([^/]+)/restart", path)
        if m and core.valid_name(m.group(1)):
            return wrap(*store.restart(m.group(1), force=body.get("force") is True))
        m = re.fullmatch(r"/api/sessions/([^/]+)/(resume|kill|forget|interrupt)", path)
        if m and core.valid_name(m.group(1)):
            return wrap(*getattr(store, m.group(2))(m.group(1)))
        m = re.fullmatch(r"/api/pages/([^/]+)/share", path)
        if m and core.ID_RE.match(m.group(1)):
            if not isinstance(body.get("shared"), bool):
                return 400, {"error": "shared: true or false"}
            code, res = store.share_page(m.group(1), body["shared"])
            return code, ({"page": res} if code == 200 else {"error": res})
        m = re.fullmatch(r"/api/pages/([^/]+)/delete", path)
        if m and core.ID_RE.match(m.group(1)):
            return wrap(*store.delete_page(m.group(1)))
        m = PLUGIN_PATH.fullmatch(path)
        if m and store.plugins is None and plugins_on():
            return 503, {"error": "plugins are still loading: try again in a moment"}
        if m and store.plugins:
            return store.plugins.handle("POST", m.group(1), m.group(2), {}, body)
        m = re.fullmatch(r"/api/prompts/([^/]+)/cancel", path)
        if m and core.ID_RE.match(m.group(1)):
            return wrap(*store.cancel_prompt(m.group(1)))
        return 404, {"error": "not found"}

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        q = Subscriber(maxlen=100)
        q.client = self.client_address[0]
        store = self.server.store
        try:  # a phone that sleeps or changes network never sends FIN: abort once data goes unacknowledged 30s
            self.connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", 18), 30000)
        except OSError:
            pass
        # Each wake-up opens a new stream while the old one may linger: keep only the newest few per client,
        # so stale streams can't pile up to the connection cap and block page loads.
        same = sorted((s for s in list(store.subscribers) if getattr(s, "client", None) == q.client),
                      key=lambda s: s.opened)
        for old in same[:max(0, len(same) - (SSE_PER_CLIENT - 1))]:
            old.kill = True
            old.wake.set()
        store.subscribers.add(q)
        try:
            self.wfile.write(b"retry: 3000\ndata: {\"hello\":1}\n\n")
            self.wfile.flush()
            while not self.server.stopping and not q.kill:
                q.wake.wait(15)
                q.wake.clear()
                if q.kill:
                    break
                out = b""
                while q:
                    out += b"data: " + q.popleft().encode() + b"\n\n"
                self.wfile.write(out or b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            store.subscribers.discard(q)
            self.close_connection = True


class Subscriber(deque):
    """One SSE client: a bounded queue of messages plus a wake-up event."""

    def __init__(self, maxlen):
        super().__init__(maxlen=maxlen)
        self.wake = threading.Event()
        self.kill = False
        self.opened = time.monotonic()
        self.client = None

    __hash__ = object.__hash__  # identity semantics so subscribers can live in a set

    def __eq__(self, other):
        return self is other


class HttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    stopping = False
    enforce = True
    done_lock = threading.Lock()
    last_client_error = 0
    allowed_hosts = {"localhost"}
    MAX_CONNECTIONS = 64

    WAITING_PER_IP = 8   # connections from one address that haven't sent a request yet

    def server_activate(self):
        super().server_activate()
        self.done = {}  # X-Request-Id -> outcome of an action (idempotent retries)
        self._slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        self._waiting = {}   # handler -> (since, client ip): connected, request not yet received
        self._wlock = threading.Lock()

    def waiting_add(self, handler):
        with self._wlock:
            ip = handler.client_address[0]
            mine = sorted((t, h) for h, (t, i) in self._waiting.items() if i == ip)
            self._waiting[handler] = (time.monotonic(), ip)
        for _, h in mine[:max(0, len(mine) + 1 - self.WAITING_PER_IP)]:
            h._abort()

    def waiting_remove(self, handler):
        with self._wlock:
            self._waiting.pop(handler, None)

    def _evict_idle(self):
        """At the cap, close the connection that has waited longest without sending a request. A real
        request arrives within milliseconds of connecting, so idle sockets held open by someone without
        the token can't lock the phone out (they used to fill all slots for HEADER_DEADLINE each)."""
        with self._wlock:
            idle = [(t, h) for h, (t, _) in self._waiting.items() if time.monotonic() - t > 0.5]
        if idle:
            min(idle, key=lambda x: x[0])[1]._abort()
            return True
        return False

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):  # connection cap: refuse instead of spawning threads
            if not (self._evict_idle() and self._slots.acquire(timeout=1.0)):
                self.shutdown_request(request)
                return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
    index_mtime = None
    page = (b"", "default-src 'none'")   # (index.html as served, its CSP), always replaced together

    @property
    def index(self):
        return self.page[0]

    @property
    def csp(self):
        return self.page[1]

    def refresh_index(self):
        """Pick up edits to index.html, extensions/, custom.css and custom.js without a daemon restart (CSP hashes
        included)."""
        try:
            m = [os.stat(INDEX_PATH).st_mtime_ns]
        except OSError:
            return
        for path in [custom_path(name) for name in CUSTOM] + builtin_extensions():
            try:
                c = os.stat(path)
                m.append((path, c.st_mtime_ns, c.st_size))
            except OSError:
                m.append(None)
        if m != self.index_mtime:
            self.page = load_index()
            self.index_mtime = m


def serve(bind, port):
    sdir = core.state_dir()
    notifier = None
    if core.env("NOTIFY_CMD"):
        notifier = notify.Notifier(core.env("NOTIFY_CMD"), core.env("PUBLIC_URL"))
    st = Store(sdir, notifier=notifier, oom=oom.OomGuard() if oom.OomGuard.available() else None)

    sp = core.sock_path()
    if os.path.exists(sp):
        try:  # refuse to start twice
            client.sock_request({"type": "status"}, timeout=1)
            print(f"grovyd: already running (socket {sp})", file=sys.stderr)
            return 1
        except (OSError, ValueError):
            os.unlink(sp)
    old = os.umask(0o177)
    sock = SockServer(sp, SockHandler)
    os.umask(old)
    sock.store = st

    httpd = HttpServer((bind, port), Http)
    httpd.store = st
    # A token is always required, loopback included: other local users and DNS-rebinding pages can
    # reach 127.0.0.1 too. Generated once into the state dir; see `grovy url`. GROVY_AUTH=off keeps it
    # optional, e.g. while enrolling a phone that can only read the link through this page.
    loopback = bind in ("127.0.0.1", "::1", "localhost")
    httpd.token = core.load_token(create=True)
    httpd.allowed_hosts = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()} | {
        h.strip().lower() for h in core.env("HOSTS", "").split(",") if h.strip()}
    httpd.enforce = core.env("AUTH", "on") != "off"
    httpd.refresh_index()
    url = f"http://{bind}:{port}/"
    sock.url = url

    st.reconcile()
    threading.Thread(target=sock.serve_forever, daemon=True).start()

    def load_plugins():   # once the sockets are up (and this is the only daemon): plugins never delay hooks or the page
        try:
            st.plugins = plugins.Plugins(st, builtin=plugins.BUILTIN_DIR).load()
        except Exception as e:   # never leave st.plugins None: its routes would answer "still loading" for good
            print(f"grovyd: plugins failed to load: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            st.plugins = plugins.Plugins(st, folder="/nonexistent")

    threading.Thread(target=load_plugins, name="grovy-plugin-load", daemon=True).start()

    def loop():
        while True:
            time.sleep(RECONCILE_EVERY)
            try:
                st.reconcile()
                st.flush_live()
            except Exception as e:  # keep the daemon alive; surface the problem
                print(f"grovyd: reconcile error: {e}", file=sys.stderr)

    threading.Thread(target=loop, daemon=True).start()
    if not loopback and not httpd.enforce:
        print("grovyd: WARNING: listening beyond loopback with GROVY_AUTH=off; anyone on the network can use grovy",
              file=sys.stderr)
    print(f"grovyd: serving {url} (hooks on {sp})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping = True
        sock.server_close()
        try:
            os.unlink(sp)
        except OSError:
            pass
    return 0
