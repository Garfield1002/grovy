"""grovyd - the grovy daemon: session state from hooks, web UI, permission decisions, prompt queue.

Two listeners:
  * a Unix socket (state dir, mode 0600) for hook events and the CLI; never on the network
  * an HTTP server (default 127.0.0.1) for the browser: JSON API + SSE + index.html

One RLock guards all in-memory state. Nothing here is the parent of a Claude process.
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import stat
import sys
import threading
import time
from collections import deque
from html import escape as html_escape
from urllib.parse import urlsplit, parse_qs, quote, unquote

from grovylib import claude, client, commands, core, newproject, notify, oom, pages, peers, plugins, terminal, tls, tmuxctl
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
                         "tls": self.server.tls_cert,
                         "claude": shutil.which(claude.claude_cmd()), "pid": os.getpid()})
        elif t == "refresh":
            store.reconcile()
        elif t == "peers":   # `grovy peer ls`
            self._reply(store.peers.status() if store.peers is not None else {"peers": [], "problems": []})
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
        elif t == "plugin":   # `grovy x PLUGIN ...` run inside a session; the plugin answers with text to print
            plugin = str(msg.get("plugin") or "")
            if store.plugins is None:
                self._reply({"error": "plugins are still loading; try again in a moment" if plugins_on()
                             else "plugins are off (GROVY_PLUGINS=0)"})
            else:
                code, res = store.plugins.command(plugin, msg.get("args") or [], msg.get("session"), msg.get("cwd"))
                try:
                    self._reply(dict(res, code=code))
                except OSError:
                    pass   # the client stopped waiting

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
    tls_cert = None   # the certificate the HTTP side serves, for `grovy tls init` / `grovy url`


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
          "icon-512.png": "image/png", "badge-96.png": "image/png", "backdrop.svg": "image/svg+xml"}


SW_PATH = os.path.join(core.HERE, "sw.js")
MANIFEST = json.dumps({
    "id": "/", "scope": "/", "name": "grovy", "short_name": "grovy", "start_url": "/", "display": "standalone",
    "description": "Your Claude Code sessions",
    "background_color": "#161615", "theme_color": "#161615",
    "icons": [{"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode()


EXT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ext_lock = threading.Lock()


def extensions_off_path():
    return os.path.join(core.state_dir(), "extensions-off.json")


def extensions_off():
    """The page extensions turned off in ⋯ → Extensions: one list for every device, kept in the state dir."""
    try:
        with open(extensions_off_path(), encoding="utf-8") as f:
            names = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(names, list):
        return []
    return sorted({n for n in names if isinstance(n, str) and EXT_NAME.fullmatch(n)})[:200]


def set_extension(name, on):
    """⋯ → Extensions: turn one on or off. It applies when a page next loads (an extension can't be undone live)."""
    if not isinstance(name, str) or not EXT_NAME.fullmatch(name):
        return 400, {"error": "bad extension name"}
    if not isinstance(on, bool):
        return 400, {"error": "on must be true or false"}
    with ext_lock:
        off = set(extensions_off())
        if on:
            off.discard(name)
        elif len(off) >= 200:
            return 400, {"error": "too many extensions turned off"}
        else:
            off.add(name)
        try:
            core.atomic_write_json(extensions_off_path(), sorted(off))
        except OSError as e:
            return 500, {"error": f"couldn't save it: {e}"}
    return 200, {"off": sorted(off)}


def load_index():
    with open(INDEX_PATH, "rb") as f:   # LF, as the browser sees it (a CR LF checkout would break the hash)
        html = f.read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    scripts = re.findall(rb"<script>(.*?)</script>", html, re.S)   # grovy's own script (taken before anything is added)
    # what ⋯ → Extensions turned off, read by the page before its extensions start
    off = html_escape(json.dumps(extensions_off())).encode()
    html = html.replace(b"</head>", b'<meta name="grovy-off" content="' + off + b'">\n</head>', 1)
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
           "img-src 'self' data:; frame-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
           "form-action 'none'; "
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


HTTPS_PAGE = """<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>
<title>grovy</title><body style='font:16px system-ui;padding:24px;max-width:30em;line-height:1.45'>
<h2>grovy uses HTTPS now</h2>{cert}
<p><a href="{link}" style='font-size:1.15em'>Open grovy with HTTPS</a></p>"""

HTTPS_CERT = """<p>The first time on this device (and after each <code>grovy tls init</code>), install grovy's
certificate, so the browser trusts it. It is the file <code>grovy.crt</code> that <code>grovy tls init</code> made on the
computer: copy it to this device yourself (cable, AirDrop, a USB stick...). It is never downloaded from here, since
anyone on the network could swap it on its way.</p>"""


LOGIN_PAGE = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
              b"<title>grovy</title><body style='font:16px system-ui;padding:24px;max-width:30em'>"
              b"<h2>grovy: login needed</h2>{wrong}<p>Open the link printed by <code>grovy url</code> on the machine "
              b"(it ends in <code>?token=...</code>). Your browser remembers it for a year.</p>"
              # The home-screen app keeps cookies of its own (iPhone) and has no address bar to open the link in.
              b"<p>In grovy added to the home screen, paste the link here instead. Copy it with "
              b"&#8943; &rarr; Login link on a device that is logged in, or from <code>grovy url</code>.</p>"
              b"<form method=get action='/' style='display:flex;gap:8px;flex-wrap:wrap'>"
              b"<input name=token required autocomplete=off autocapitalize=off spellcheck=false "
              b"placeholder='Login link or token' aria-label='Login link or token' "
              b"style='flex:1 1 14em;font:inherit;padding:8px;border:1px solid #888;border-radius:8px'>"
              b"<button style='font:inherit;padding:8px 16px;border-radius:8px'>Log in</button></form>")
LOGIN_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"


def _res(code, res, key=None):
    """(code, JSON body) for a Store answer: the result (under `key` if given) when it worked, else {"error": ...}."""
    if code != 200:
        return code, {"error": res}
    return code, ({key: res} if key else res)


class Http(http.server.BaseHTTPRequestHandler):
    server_version = "grovyd"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if core.env("DEBUG"):   # never the login token in the log (it comes in the URL)
            super().log_message(fmt, *(re.sub(r"token=[^&\s\"]*", "token=…", a) if isinstance(a, str) else a
                                       for a in args))

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

    def _send_file(self, path, filename):
        """Stream a file from disk as a download: an attachment, octet-stream, never rendered on grovy's origin
        (nosniff + CSP none), so an arbitrary file a session offered can't run as a page here. No size limit: the
        bytes are streamed, not read into memory. Behind grovy's login, like every route past _authed()."""
        try:   # O_NONBLOCK: opening a fifo/device path would otherwise block here, before the S_ISREG check, with no
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)   # deadline on this handler. Regular-file reads ignore it.
        except OSError:
            return self._err(404, "not found")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):   # not a directory, device, fifo, ...
                os.close(fd)
                fd = -1   # so a broken pipe while sending the 404 can't close this fd a second time
                return self._err(404, "not found")
            size = st.st_size
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "")).strip("._-")[:120] or "file"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            left, f = size, os.fdopen(fd, "rb")   # f now owns fd
            fd = -1
            with f:
                while left > 0:
                    chunk = f.read(min(left, 1 << 16))
                    if not chunk:   # file shrank under us; Connection: close ends it cleanly
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

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
            if k in ("__Secure-grovy_token", "grovy_token", "ccs_token"):  # ccs_token: cookie set before the rename
                got = got or v
        return hmac.compare_digest(got.encode(), token.encode())

    timeout = 30  # seconds of client silence before a connection is dropped
    HEADER_DEADLINE = 15  # whole request line + headers must arrive within this (per-recv timeouts can be trickled)
    tls = False         # this connection is HTTPS
    plain_on_tls = False   # plain HTTP to the HTTPS port: answered only with HTTPS_PAGE
    _raw = None         # with HTTPS: our own handle on the TCP socket, so _abort never touches the TLS object

    def setup(self):
        """With HTTPS, the handshake runs here, in this connection's thread (never in the accept loop, where one slow
        client would stall everyone), under the same deadline and idle eviction as a request's headers: the deadline
        covers both together."""
        ctx = self.server.ssl_ctx
        self.tls_failed = False
        self._since = time.monotonic()
        if not ctx:
            return super().setup()
        t = threading.Timer(self.HEADER_DEADLINE, self._abort)
        t.daemon = True
        try:
            self._raw = socket.socket(self.request.family, self.request.type, fileno=os.dup(self.request.fileno()))
            t.start()
            self.server.waiting_add(self)
            self.request.settimeout(self.timeout)
            if self.request.recv(1, socket.MSG_PEEK) == b"\x16":   # a TLS handshake record
                wrapped = ctx.wrap_socket(self.request, server_side=True, do_handshake_on_connect=False)
                self.request = wrapped   # only once wrapped: a failed wrap may already have detached the socket
                wrapped.do_handshake()
                self.tls = True
            else:
                self.plain_on_tls = True
            super().setup()
        except (OSError, ValueError, RuntimeError) as e:   # SSLError is an OSError: e.g. the phone doesn't trust us yet
            self.tls_failed = True   # no rfile/wfile: handle() and finish() skip their part
            if core.env("DEBUG"):
                print(f"grovyd: TLS handshake with {self.client_address[0]} failed: {e}", file=sys.stderr, flush=True)
        finally:
            t.cancel()
            self.server.waiting_remove(self)

    def handle(self):
        if self.tls_failed:
            return
        try:
            super().handle()
        except OSError:
            if not self.tls:
                raise
            # A TLS read on a socket the deadline shut down, or a browser's preconnect closed unused, raises where
            # plain HTTP reads b"" quietly. Nothing to report.

    def finish(self):
        try:
            if not self.tls_failed:
                super().finish()
        finally:
            if self._raw is not None:
                if isinstance(self.request, ssl.SSLSocket):   # wrapping detached the socket socketserver closes
                    try:
                        self.request.close()
                    except OSError:
                        pass
                self._raw.close()

    def handle_one_request(self):
        left = self.HEADER_DEADLINE - (time.monotonic() - self._since) if self._raw else self.HEADER_DEADLINE
        t = self._deadline = threading.Timer(max(left, 0.5), self._abort)
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
        if ok and self.plain_on_tls:
            self._https_page()
            return False
        return ok

    def _https_page(self):
        """Plain http:// to the HTTPS port (an old link, or a phone that hasn't installed the certificate yet): point
        to https://. Never the certificate itself: over plain HTTP it could be swapped. Nothing private: the token is
        only echoed back to a request that already carried it."""
        self.close_connection = True
        if not self._host_ok():
            return self._err(403, "unknown Host header (add the name to GROVY_HOSTS)")
        url = urlsplit(self.path)
        if self.command != "GET":
            return self._err(400, "this grovy uses HTTPS")
        q = parse_qs(url.query).get("token", [""])[0]
        good = q and self.server.token and hmac.compare_digest(q.encode(), self.server.token.encode())
        link = "https://" + (self.headers.get("Host") or "").strip() + "/" + (f"?token={quote(q)}" if good else "")
        own = self.server.tls_paths == (tls.own_cert(), None)   # not with GROVY_TLS_CERT: nothing to install
        page = HTTPS_PAGE.replace("{cert}", HTTPS_CERT if own else "")
        self._send(200, page.replace("{link}", html_escape(link)).encode(), "text/html",
                   [("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")])

    def _abort(self):
        if core.env("DEBUG"):
            print(f"grovyd: closed idle connection from {self.client_address[0]} "
                  f"(no request within {self.HEADER_DEADLINE}s)", file=sys.stderr, flush=True)
        try:
            (self._raw or self.connection).shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _cookies(self, token):
        """Set-Cookie headers for a login. Over HTTPS the cookie is `__Secure-grovy_token` (Secure: never sent over
        plain HTTP), and the plain-HTTP one is deleted. A different name, so that if HTTPS is turned off again a login
        over http:// can still set `grovy_token` (a browser won't let http:// overwrite a Secure cookie of the same name)."""
        # SameSite=Lax, not Strict: Strict withholds the cookie on top-level navigations the browser treats as
        # cross-site, which includes a standalone PWA launched from the Android home screen, so every cold launch landed
        # on the login page. Lax still blocks CSRF here (actions are POST, guarded by the JSON/octet-stream content
        # type's forced preflight and the Origin check; GET API routes need the X-Grovy header a navigation can't set),
        # and only sends on top-level GETs.
        attrs = "Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
        if not self.tls:
            return [("Set-Cookie", f"grovy_token={token}; {attrs}")]
        return [("Set-Cookie", f"__Secure-grovy_token={token}; {attrs}; Secure"),
                ("Set-Cookie", "grovy_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")]

    def _host_ok(self):
        """DNS-rebinding guard: a browser reaching us through an attacker's name sends that name as
        Host. Accept only IP literals, localhost, this machine's names, and GROVY_HOSTS."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if "%" in name:   # an IPv6 zone id passes ip_address() with any text after it ("[::1%@evil.example#]")
            return False
        port = host[host.find("]") + 1:] if host.startswith("[") else host[len(name):] if host.count(":") == 1 else ""
        if port and not re.fullmatch(r":\d{1,5}", port):   # e.g. "127.0.0.1:x@evil.example", echoed in links
            return False
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
        wrong = False
        if path == "/" and self.server.token:
            q = parse_qs(url.query).get("token", [""])[0].strip()
            if "token=" in q:   # the whole login link, pasted into the login page's field
                q = parse_qs(urlsplit(q).query).get("token", [""])[0].strip()
            if q and hmac.compare_digest(q.encode(), self.server.token.encode()):
                # Lax (see _cookies): a scanned QR or a messaged link opens this from outside the origin
                return self._send(303, b"", "text/plain", [
                    ("Location", "/"),
                    *self._cookies(q)])
            wrong = bool(q)
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
        if path == "/sw.js":   # the service worker (sw.js): at the root, so it may cover the whole app. Nothing private
            try:
                with open(SW_PATH, "rb") as f:
                    sw = f.read()
            except OSError:
                return self._err(404, "not found")
            return self._send(200, sw, "text/javascript", [("Cache-Control", "no-cache"),
                              ("Content-Security-Policy", "default-src 'none'; connect-src 'self'; img-src 'self'")])
        m = re.fullmatch(r"/s/([0-9a-f]{32})", path)
        if m:   # a shared page: its link is the credential, and it grants nothing but this page
            got = self.server.store.shared_page_html(m.group(1))
            if not got:
                return self._send(404, b"Not found (this page is no longer shared).", "text/plain")
            return self._send(200, got[1], "text/html", [("Content-Security-Policy", pages.PAGE_CSP)])
        if self.server.store.peers:   # a peer's shared page (/s/pc:TOKEN): the link is the credential there too
            got = self.server.store.peers.shared(path)
            if got:
                return self._peer_reply(got)
        if not self._authed():
            if path == "/":
                return self._send(401, LOGIN_PAGE.replace(b"{wrong}", b"<p style='color:#c33'>That link or token "
                                  b"didn't work: copy it again.</p>" if wrong else b""), "text/html",
                                  [("Content-Security-Policy", LOGIN_CSP)])
            return self._err(401, "token required: open /?token=...")
        store = self.server.store
        if store.peers:   # a peer's session, id or file (pc:...): proxied to it (grovylib/peers.py)
            got = store.peers.handle_get(path, url.query, self.headers.get("X-Grovy") == "1")
            if got:
                return self._peer_reply(got)
        if path == "/":
            self.server.refresh_index()
            index, csp = self.server.page   # one read: a reload in between can't pair a page with another's CSP
            extra = [("Content-Security-Policy", csp)]
            if self.tls and self.server.token and re.search(r"(^|;)\s*grovy_token=", self.headers.get("Cookie", "")):
                # A phone logged in over plain HTTP holds a cookie without Secure, which it would keep sending in
                # the clear to the http:// address: swap it for the Secure one.
                extra += self._cookies(self.server.token)
            return self._send(200, index, "text/html", extra)
        if path == "/api/sessions":
            if store.peers:   # other machines' sessions too, and whether each is reachable
                return self._send(200, {"sessions": store.peers.merge_sessions(store.list_sessions()), "now": time.time(),
                                        "host": socket.gethostname().split(".")[0],
                                        "peers": store.peers.status()["peers"]})
            return self._send(200, {"sessions": store.list_sessions(), "now": time.time()})
        if path == "/api/permissions":
            return self._send(200, {"permissions": store.permissions()})
        if path == "/api/events":
            return self._sse()
        if path == "/api/projects":
            return self._send(200, {"projects": store.lifecycle.trusted_projects()})
        if path == "/api/projects/new":   # where ⋯ → New project makes them
            return self._send(200, {"dir": newproject.projects_dir()})
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
        m = re.fullmatch(r"/xfile/([0-9a-f]{32})", path)
        if m:   # a file a plugin offered with grovy.download (file-sharing): served from disk as a download, never rendered
            entry = store.plugins.download_entry(m.group(1)) if store.plugins else None
            if not entry:
                return self._err(404, "not found")
            return self._send_file(entry["path"], entry["name"])
        m = re.fullmatch(r"/api/sessions/([^/]+)/screen", path)
        if m and core.valid_name(m.group(1)):   # `grovy logs pc:NAME` from the trunk: the pane's recent text
            if self.headers.get("X-Grovy") != "1":
                return self._err(403, "X-Grovy header missing")
            n = parse_qs(url.query).get("lines", ["200"])[0]
            try:
                text = tmuxctl.capture(m.group(1), min(int(n), 5000) if n.isdigit() else 200)
            except (ValueError, RuntimeError) as e:
                return self._err(404, str(e))
            return self._send(200, {"text": text})
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
            if store.peers:
                return self._send(200, {"pages": store.peers.merge_pages(store.list_pages())})
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
            return self._upload(m.group(1), urlsplit(self.path).path)
        m = re.fullmatch(r"/api/sessions/([^/]+)/terminal", urlsplit(self.path).path)
        if m:   # `grovy attach pc:NAME` from the trunk (grovylib/terminal.py)
            return self._terminal(m.group(1))
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

    def _peer_reply(self, got):
        """Send what grovylib/peers.py made of a request for a peer (see Peers.handle_get)."""
        kind = got[0]
        if kind == "json":
            return self._send(got[1], got[2])
        if kind == "raw":
            return self._send(got[1], got[3], got[2], got[4])
        if kind == "page":   # a peer's page: sandboxed by this hub's own PAGE_CSP, whatever the peer sent
            return self._send(200, got[1], "text/html", [("Content-Security-Policy", pages.PAGE_CSP)], frame="SAMEORIGIN")
        if kind == "shared":
            return self._send(200, got[1], "text/html", [("Content-Security-Policy", pages.PAGE_CSP)])
        if kind == "stream":
            return self._relay_download(got[1], got[2])
        return self._err(500, "internal error")

    def _relay_download(self, conn, resp):
        """A file a peer offered (/xfile/pc:TOKEN), streamed through as a download with the same headers as
        _send_file: never rendered here, whatever type the peer claims."""
        try:
            m = re.search(r'filename="([^"]*)"', resp.getheader("Content-Disposition") or "")
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", m.group(1) if m else "").strip("._-")[:120] or "file"
            try:
                size = int(resp.getheader("Content-Length") or "")
            except ValueError:
                size = None
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if size is not None and size >= 0:
                self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            pass   # either side went away: Connection: close ends the download short, and the browser says so
        finally:
            conn.close()

    def _upload(self, name, path):
        self.close_connection = True   # an error leaves the body unread
        store = self.server.store
        if store.peers:
            try:
                target = store.peers.upload_target(path)
            except peers.Answer as a:
                return self._send(a.code, a.body)
            if target:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/octet-stream":
                    return self._err(415, "Content-Type must be application/octet-stream")
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._err(411, "Content-Length required")
                if n < 0:
                    return self._err(400, "bad Content-Length")
                return self._send(*target[0].upload(target[1], self.headers.get("X-Filename", ""), self.rfile.read, n))
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
        if store.peers:   # an action on a peer's session or id (pc:...): proxied to it (grovylib/peers.py)
            rid = self.headers.get("X-Request-Id", "")
            got = store.peers.handle_post(path, body, rid if REQ_ID_RE.fullmatch(rid) else None)
            if got:
                return got
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
        if path == "/api/extensions":   # ⋯ → Extensions
            return set_extension(body.get("name"), body.get("on"))
        if path == "/api/projects/new":
            return _res(*store.new_project(body.get("kind"), body.get("name"), body.get("url")))
        if path == "/api/projects/start":
            return _res(*store.start_project(body.get("path"), body.get("token"), body.get("text") or "",
                                             body.get("ask"), body.get("trust")))
        if path == "/api/projects/discard":
            return _res(*store.discard_project(body.get("path"), body.get("token")), key="message")
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

    def _terminal(self, name):
        """A terminal on a session, for `grovy attach` from another machine: only with GROVY_TERMINAL=on, only from
        a client that sends the Upgrade header (a browser page can't), then relayed until either side ends it."""
        if not terminal.enabled():
            return self._err(403, "the terminal is off on this computer (its grovy setup, or GROVY_TERMINAL=on)")
        if self.headers.get("X-Grovy") != "1" or self.headers.get("Upgrade") != terminal.UPGRADE:
            return self._err(400, "not a terminal request")
        if not core.valid_name(name) or not tmuxctl.tmux_alive(name):
            return self._err(404, f"no live session {name!r} here")
        def num(h, d):
            v = self.headers.get(h, "")
            return int(v) if v.isdigit() and 1 <= int(v) <= 1000 else d
        term = self.headers.get("X-Term", "")
        self.close_connection = True
        self.send_response(101)
        self.send_header("Upgrade", terminal.UPGRADE)
        self.send_header("Connection", "Upgrade")
        self.end_headers()
        self.wfile.flush()
        print(f"grovyd: terminal opened on {name}", file=sys.stderr, flush=True)
        try:
            terminal.serve(self.connection, name, num("X-Rows", 24), num("X-Cols", 80),
                           term if terminal.TERM_RE.fullmatch(term) else "xterm-256color")
        except OSError:
            pass
        print(f"grovyd: terminal closed on {name}", file=sys.stderr, flush=True)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        q = Subscriber(maxlen=100)
        q.client = self.client_address[0]
        if parse_qs(urlsplit(self.path).query).get("notify") == ["1"]:
            # A hub following this daemon (grovylib/peers.py): it also gets the notifications, and it is counted apart
            # from the browsers at the same address (through the SSH tunnel that's 127.0.0.1, like a local browser).
            q.notify, q.client = True, ("peer", q.client)
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
    ssl_ctx = None      # HTTPS when set (grovylib/tls.py); replaced whole when the certificate files change
    tls_paths = None
    tls_stamp = None

    def reload_tls(self):
        """Load the certificate again if its files changed (`grovy tls init` again, a renewed Let's Encrypt
        certificate). A bad file keeps the previous certificate. Returns False if nothing could be loaded."""
        if not self.tls_paths:
            return True
        s = tls.stamp(*self.tls_paths)
        if s is None or s == self.tls_stamp:
            return self.ssl_ctx is not None
        self.tls_stamp = s   # set even on failure: a half-written file is retried when it changes again
        try:
            self.ssl_ctx = tls.context(*self.tls_paths)
        except (OSError, ValueError) as e:   # SSLError is an OSError
            print(f"grovyd: can't load the HTTPS certificate {self.tls_paths[0]}: {e}" +
                  ("; keeping the previous one" if self.ssl_ctx else ""), file=sys.stderr, flush=True)
        return self.ssl_ctx is not None

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
        for path in [custom_path(name) for name in CUSTOM] + builtin_extensions() + [extensions_off_path()]:
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
    try:   # HTTPS only if there is a certificate; one that's there but broken stops the daemon rather than fall back
        tls_paths = tls.cert_paths()
    except ValueError as e:
        core.die(str(e))
    ctx, tls_stamp = None, None
    if tls_paths:
        tls_stamp = tls.stamp(*tls_paths)   # before loading: a change in between is picked up by the next reload
        try:
            ctx = tls.context(*tls_paths)
        except (OSError, ValueError) as e:   # SSLError is an OSError
            core.die(f"can't load the HTTPS certificate {tls_paths[0]}: {e} (fix it, or start with GROVY_TLS=off)")
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
    httpd.tls_paths, httpd.tls_stamp, httpd.ssl_ctx = tls_paths, tls_stamp, ctx
    # A token is always required, loopback included: other local users and DNS-rebinding pages can
    # reach 127.0.0.1 too. Generated once into the state dir; see `grovy url`. GROVY_AUTH=off keeps it
    # optional, e.g. while enrolling a phone that can only read the link through this page.
    loopback = bind in ("127.0.0.1", "::1", "localhost")
    httpd.token = core.load_token(create=True)
    host = socket.gethostname().lower()
    httpd.allowed_hosts = {"localhost", host, host.split(".")[0] + ".local", socket.getfqdn().lower()} | {
        h.strip().lower() for h in core.env("HOSTS", "").split(",") if h.strip()}
    httpd.enforce = core.env("AUTH", "on") != "off"
    httpd.refresh_index()
    url = f"{'https' if httpd.ssl_ctx else 'http'}://{bind}:{port}/"
    sock.url = url
    sock.tls_cert = tls_paths[0] if tls_paths else None

    st.peers = peers.Peers(st)
    st.peers.reload()   # other machines' grovy, from ~/.config/grovy/peers.json (none: nothing changes)
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
            try:
                st.peers.reload()
            except Exception as e:
                print(f"grovyd: peers reload error: {e}", file=sys.stderr)
            try:
                httpd.reload_tls()
            except Exception as e:
                print(f"grovyd: HTTPS certificate reload error: {e}", file=sys.stderr)

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
