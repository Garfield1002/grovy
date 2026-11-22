"""Peers: the sessions of grovy daemons on other machines, shown and commanded from this one (the hub).

Every machine runs its own, unchanged grovyd: its sessions, hooks, permissions and containers stay there, and it keeps
working if the link drops. A peer (say a PC with no public address) dials out with an SSH reverse tunnel to a Unix
socket on the hub (`grovy peer tunnel HOST`, below). The hub reads ~/.config/grovy/peers.json (mode 0600):

    {"pc": {"socket": "/home/me/.local/state/grovy/peers/pc.sock", "token": "<pc's grovy token>", "tls": false}}

(`grovy peer add/rm/ls`), and talks HTTP over that socket to the peer's grovyd with the peer's token, exactly as the
phone does. The peer's token never reaches the phone: the hub builds every request to the peer itself.

Names. A peer's session `foo` is `pc:foo` on the hub (`:` is not allowed in a session name, so it can't collide with a
local one). The ids the phone acts on (permission requests, queued prompts, pages, share links, offered downloads) are
prefixed the same way, `pc:<hex>`, so every route knows where to go without remembering anything.

Live updates. grovy's event stream only says "session X changed" (no event ids: the page re-fetches). The hub holds
one stream per peer (/api/events?notify=1), refreshes its copy of the peer's session and page lists when it says
something changed, then tells the phones `pc:X` changed. On every (re)connect it re-fetches everything, so nothing
can be missed silently: a hint lost while the link was down is replaced by the fresh state. `notify=1` also carries
the peer's notifications (needs you / finished / failed), which the hub sends on through its own notifier and plugins
(Web Push). Until a fresh list has arrived, and whenever the link is down, the peer's sessions show `unknown`.

Everything a peer sends is untrusted input, like Claude's output: names and ids are checked before use, answers are
size-capped, pages keep the hub's PAGE_CSP, downloads are re-served as downloads. Through grovy itself the link only
goes one way (the hub connects to the peer, never the reverse). The SSH login the tunnel uses is another matter: it
is the peer's way into the hub's account (a shell, unless the key is restricted; and even a key restricted to
forwarding can bind a socket anywhere the account can write). See docs/DESIGN.md, "Other machines".
"""
import collections
import getpass
import http.client
import json
import os
import re
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode

from . import core, states

SEP = ":"
PEER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,15}")    # short: it is part of every name and of the socket path
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{8,256}")   # what goes into the Authorization header: no spaces, no CR LF
SOCK_MAX = 100        # AF_UNIX paths are limited to 108 bytes (less on some systems)
REQUEST_TIMEOUT = 20  # seconds for one proxied request
STREAM_TIMEOUT = 45   # the peer's event stream pings every 15s: this much silence means the link is dead
ANSWER_MAX = 16 * 1024 * 1024
BACKOFF_MAX = 30
RESYNC_EVERY = 60     # seconds between full re-fetches of a peer's lists, whatever its stream says
STALE_AFTER = 3       # failed list fetches in a row before a peer's sessions show `unknown`
NOTIFY_MAX, NOTIFY_WINDOW = 20, 60   # notifications passed on per peer per window (seconds)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
KINDS = ("input", "done", "error")


def log(msg):
    print(f"grovyd: peers: {msg}", file=sys.stderr, flush=True)


def valid_peer(name):
    return isinstance(name, str) and bool(PEER_RE.fullmatch(name))


def join(peer, local):
    return f"{peer}{SEP}{local}"


def split(name):
    """"pc:foo" -> ("pc", "foo"); None when it isn't a peer's name (no ':')."""
    if not isinstance(name, str) or SEP not in name:
        return None
    peer, _, local = name.partition(SEP)
    return peer, local


def ago(t):
    if not t:
        return "never"
    s = max(0, core.now() - t)
    return "just now" if s < 45 else f"{round(s / 60)} min ago" if s < 3600 else \
        f"{round(s / 3600)} h ago" if s < 86400 else f"{round(s / 86400)} days ago"


# ------------------------------------------------------------------ peers.json
def config_path():
    return os.path.join(core.config_dir(), "peers.json")


def default_socket(name):
    """Where `grovy peer tunnel` puts a peer's socket on the hub by default (the same formula on both sides)."""
    return os.path.join(core.default_state_dir(), "peers", name + ".sock")


def check_entry(name, e):
    """A peers.json entry as the hub uses it, or a reason it can't be used."""
    if not valid_peer(name):
        return None, f"bad machine name {name!r} (lowercase letters, digits, - and _, at most 16)"
    if not isinstance(e, dict):
        return None, f"{name}: not an object"
    sock, tok, use_tls = e.get("socket"), e.get("token"), e.get("tls", False)
    if not isinstance(sock, str) or not os.path.isabs(sock) or len(sock.encode()) > SOCK_MAX:
        return None, f"{name}: socket must be an absolute path of at most {SOCK_MAX} bytes"
    if not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok):
        return None, f"{name}: token missing or malformed"
    if not isinstance(use_tls, bool):
        return None, f"{name}: tls must be true or false"
    return {"socket": sock, "token": tok, "tls": use_tls}, None


def read_config(path=None):
    """(peers, problems): the usable entries of peers.json, and what's wrong with the rest. A file others can read
    is refused whole: it holds login tokens."""
    path = path or config_path()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"{path}: {e}"]
    if st.st_mode & 0o077:
        return {}, [f"{path} is readable by others (it holds login tokens): chmod 600 it"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {}, [f"{path}: {e}"]
    if not isinstance(data, dict):
        return {}, [f"{path}: not a JSON object"]
    out, problems = {}, []
    for name, e in data.items():
        entry, why = check_entry(name, e)
        if entry:
            out[name] = entry
        else:
            problems.append(why)
    return out, problems


def write_config(peers, path=None):
    path = path or config_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    core.atomic_write_json(path, peers)   # written 0600
    os.chmod(path, 0o600)


def reason(e, name):
    """Why a peer can't be reached, in words: a refused or missing socket means no tunnel."""
    if isinstance(e, (ConnectionRefusedError, FileNotFoundError)):
        return f"no tunnel (is `grovy peer tunnel` running on {name}?)"
    if isinstance(e, socket.timeout):
        return "it doesn't answer (timed out)"
    return str(e) or type(e).__name__


# ------------------------------------------------------------------ HTTP over the socket
class PeerError(Exception):
    """The peer couldn't be reached, or answered nonsense."""


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a Unix socket (the tunnel's end on the hub). TLS optional: the peer's grovyd may serve HTTPS
    (then unverified: SSH already secures the link, and the certificate names don't match the socket anyway)."""

    def __init__(self, path, timeout=REQUEST_TIMEOUT, use_tls=False):
        super().__init__("localhost", timeout=timeout)
        self.path, self.use_tls = path, use_tls

    def connect(self):
        check_socket(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
            if self.use_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                s = ctx.wrap_socket(s, server_hostname="localhost")
        except BaseException:
            s.close()
            raise
        self.sock = s


def check_socket(path):
    """Only a socket of ours, in a folder others can't swap it in: the peer's token is sent through it (the hub is
    shared; another user who made the socket would be handed it)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise FileNotFoundError("no tunnel (the socket isn't there)") from None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError("not a socket of this user's: refusing to send it the token")
    d = os.stat(os.path.dirname(path))
    if d.st_uid not in (0, os.getuid()) or (d.st_mode & 0o022 and not d.st_mode & stat.S_ISVTX):
        raise PermissionError("its folder is writable by others: refusing to send it the token")


class Peer:
    """One peer: its link (event stream + list refresher threads) and what it last said about its sessions."""

    def __init__(self, name, conf, hub):
        self.name, self.conf, self.hub = name, conf, hub
        self.lock = threading.Lock()
        self.state = "connecting"   # connecting (no fresh list yet) | online | stale (lists failing) | offline
        self.error = None
        self.last_seen = None       # last time the peer answered anything
        self.sessions = None        # its session list as of the last refresh (None: never had one)
        self.pages = []
        self.gen = 0                # bumped on every connect/disconnect: a refresh from an older link is dropped
        self.dirty, self.full = set(), False
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.threads = []
        self.conn = None            # the event stream's connection, closed by close() to end its read at once
        self.linked = False         # the stream is subscribed (said hello) and hasn't dropped since
        self.failures = 0           # list refreshes failed in a row on a live stream
        self.notified, self.throttled = collections.deque(), False

    # -- requests
    def connection(self, timeout=REQUEST_TIMEOUT):
        return UnixHTTPConnection(self.conf["socket"], timeout, self.conf["tls"])

    def headers(self, extra=None):
        h = {"Authorization": "Bearer " + self.conf["token"], "X-Grovy": "1"}
        h.update(extra or {})
        return h

    def unreachable(self, e):
        with self.lock:
            seen = self.last_seen
        return f"{self.name} is offline (last seen {ago(seen)}): {reason(e, self.name)}"

    def open(self, method, path, body=None, headers=None, timeout=REQUEST_TIMEOUT):
        """Send a request; returns (connection, response) for the caller to read and close. Raises PeerError."""
        c = self.connection(timeout)
        try:
            c.request(method, path, body=body, headers=self.headers(headers))
            r = c.getresponse()
        except (OSError, http.client.HTTPException) as e:
            c.close()
            raise PeerError(self.unreachable(e)) from None
        self.seen()
        return c, r

    def request(self, method, path, body=None, headers=None, limit=ANSWER_MAX):
        """(status, content type, body bytes). Raises PeerError."""
        c, r = self.open(method, path, body, headers)
        try:
            data = r.read(limit + 1)
        except (OSError, http.client.HTTPException) as e:
            raise PeerError(self.unreachable(e)) from None
        finally:
            c.close()
        if len(data) > limit:
            raise PeerError(f"{self.name} answered with more than {limit // (1024 * 1024)} MB")
        return r.status, (r.getheader("Content-Type") or "").split(";")[0].strip(), data

    def call(self, method, path, body=None, headers=None):
        """A JSON API call: (status, dict). A refused login becomes a 502 the phone can read (never a 401, which
        would look like the phone's own login failing). Raises PeerError."""
        data = None
        h = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        code, ctype, raw = self.request(method, path, data, h)
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            raise PeerError(f"{self.name} answered something that isn't grovy's API (HTTP {code})")
        if code == 401:
            return 502, {"error": self.login_refused()}
        return code, obj

    def login_refused(self):
        """A peer's 401, for the phone: a 502 with what to do (a 401 would look like the phone's own login failing)."""
        return (f"{self.name} refused this grovy's login: its token changed? On {self.name} run `grovy peer token`, "
                f"then here `grovy peer add {self.name}`")

    def seen(self):
        with self.lock:
            self.last_seen = core.now()

    def upload(self, path, filename, read, n):
        """Stream a file the phone sends (n bytes from read) to the peer, without holding it in memory: (code, dict)."""
        if not isinstance(filename, str) or len(filename) > 1024 or re.search(r"[^\x21-\x7e]", filename):
            filename = ""   # the phone sends it percent-encoded; anything else is dropped (the peer names the file)
        c = self.connection()
        try:
            c.putrequest("POST", path, skip_accept_encoding=True)
            for k, v in self.headers({"Content-Type": "application/octet-stream", "Content-Length": str(n),
                                      **({"X-Filename": filename} if filename else {})}).items():
                c.putheader(k, v)
            c.endheaders()
            left = n
            try:
                while left > 0:
                    try:
                        chunk = read(min(left, 1 << 16))
                    except OSError as e:   # the phone's side, not the peer's (a slow or dropped upload)
                        return 408, {"error": f"the file stopped arriving from this device ({reason(e, self.name)})"}
                    if not chunk:
                        return 400, {"error": "the file arrived incomplete from this device"}
                    c.send(chunk)
                    left -= len(chunk)
            except BrokenPipeError:
                pass   # the peer refused early (too big, unknown session): its answer says why
            r = c.getresponse()
            raw = r.read(ANSWER_MAX)
        except (OSError, http.client.HTTPException) as e:
            return 502, {"error": self.unreachable(e)}
        finally:
            c.close()
        self.seen()
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            return 502, {"error": f"{self.name} answered something that isn't grovy's API (HTTP {r.status})"}
        if r.status == 401:
            return 502, {"error": self.login_refused()}
        if r.status != 200:
            err = obj.get("error")
            return r.status, {"error": f"{self.name}: {err}" if isinstance(err, str) else f"{self.name}: HTTP {r.status}"}
        return 200, {k: obj.get(k) for k in ("path", "name", "size")}

    # -- the link
    def start(self):
        for fn in (self._stream_loop, self._refresh_loop):
            t = threading.Thread(target=fn, name=f"grovy-peer-{self.name}", daemon=True)
            t.start()
            self.threads.append(t)

    def close(self):
        """Stop for good (removed or replaced in peers.json): nothing it still receives is acted on."""
        self.stop.set()
        self.wake.set()
        c = self.conn
        if c is not None and c.sock is not None:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _stream_loop(self):
        delay = 1
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self._stream()
                why = "the link closed"
            except PeerError as e:
                why = str(e)
            except (OSError, http.client.HTTPException, ValueError) as e:
                why = reason(e, self.name)
            except Exception as e:   # never let the thread die: the peer would stay "online" for good
                log(f"{self.name}: event stream error: {e!r}")
                why = f"unexpected error ({type(e).__name__})"
            self._offline(why)
            if time.monotonic() - started > 60:
                delay = 1
            self.stop.wait(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    def _stream(self):
        c = self.conn = self.connection(STREAM_TIMEOUT)
        try:
            if self.stop.is_set():   # closed before conn was set: close() couldn't reach it
                return
            try:
                c.request("GET", "/api/events?notify=1", headers=self.headers())
                r = c.getresponse()
            except (OSError, http.client.HTTPException) as e:
                raise PeerError(reason(e, self.name)) from None
            if r.status != 200:
                raise PeerError(f"its event stream answered HTTP {r.status}" +
                                (" (wrong token?)" if r.status == 401 else ""))
            while not self.stop.is_set():
                line = r.fp.readline(65537)
                if not line:
                    return
                self.seen()
                if not line.startswith(b"data:"):
                    continue
                try:
                    msg = json.loads(line[5:])
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._message(msg)
        finally:
            self.conn = None
            c.close()

    def _message(self, msg):
        if self.stop.is_set():
            return
        if msg.get("hello"):   # subscribed: from here on nothing is missed, so fetch everything once
            with self.lock:
                self.gen += 1
                self.linked, self.full = True, True
            self.wake.set()
            return
        n = msg.get("notify")
        if isinstance(n, dict):
            s, kind, text = n.get("session"), n.get("kind"), n.get("text")
            if core.valid_name(s) and kind in KINDS and isinstance(text, str) and self._may_notify():
                self.hub.notify(join(self.name, s), kind, core.norm(text)[:300])
            return
        if "session" in msg:
            s = msg["session"]
            with self.lock:
                if s is None:
                    self.full = True
                elif core.valid_name(s):
                    self.dirty.add(s)
                else:
                    return
            self.wake.set()

    def _may_notify(self):
        """At most NOTIFY_MAX notifications per NOTIFY_WINDOW seconds from one peer (a peer gone haywire can't
        flood your phone); the rest are dropped, and that is logged once per window."""
        t = time.monotonic()
        with self.lock:
            while self.notified and t - self.notified[0] > NOTIFY_WINDOW:
                self.notified.popleft()
            if len(self.notified) >= NOTIFY_MAX:
                first = not self.throttled
                self.throttled = True
            else:
                self.notified.append(t)
                self.throttled = False
                return True
        if first:
            log(f"{self.name}: too many notifications, dropping some")
        return False

    def _offline(self, why):
        with self.lock:
            was = self.state
            self.gen += 1
            self.linked = False
            self.state, self.error = "offline", why
            self.dirty, self.full = set(), False
        if was != "offline":
            log(f"{self.name}: offline ({why})")
            self.hub.publish(None)

    def _refresh_loop(self):
        while not self.stop.is_set():
            woken = self.wake.wait(RESYNC_EVERY)
            self.wake.clear()
            if self.stop.is_set():
                return
            try:
                self._refresh(woken)
            except Exception as e:   # never let the thread die with the state frozen
                log(f"{self.name}: refresh error: {e!r}")
                self._failed(None, set(), True, f"unexpected answer ({type(e).__name__})")
                if self.stop.wait(1):
                    return
                self.wake.set()

    def _failed(self, gen, names, full, why):
        """A refresh that didn't work on a link that is still up: retried; after STALE_AFTER in a row the sessions
        show `unknown` with the reason (the stream being up proves nothing about the lists)."""
        stale = False
        with self.lock:
            if gen is not None and gen != self.gen:
                return
            self.dirty |= names
            self.full = self.full or full
            self.error = why
            self.failures += 1
            if self.failures >= STALE_AFTER and self.state == "online":
                self.state, stale = "stale", True
        if stale:
            log(f"{self.name}: not answering ({why})")
            self.hub.publish(None)

    def _refresh(self, woken):
        with self.lock:
            if not woken and self.linked:   # a periodic full resync repairs a hint the peer's stream dropped
                self.full = True
            gen, names, full = self.gen, self.dirty, self.full
            self.dirty, self.full = set(), False
        if not names and not full:
            return
        try:
            code, got = self.call("GET", "/api/sessions")
            pcode, pg = self.call("GET", "/api/pages")
            if code != 200 or pcode != 200:
                raise PeerError(f"its session and page lists answered HTTP {code} and {pcode}")
        except PeerError as e:
            self._failed(gen, names, full, str(e))
            if not self.stop.wait(1):
                self.wake.set()
            return
        sessions = [s for s in _list(got.get("sessions")) if summary_ok(s)]
        page_list = [p for p in _list(pg.get("pages")) if isinstance(p, dict) and is_id(p.get("id"))]
        with self.lock:
            if gen != self.gen or self.stop.is_set():   # the link dropped (or reconnected) meanwhile: maybe stale
                return
            before, was = self.sessions, self.state
            self.sessions, self.pages = sessions, page_list
            self.failures = 0
            if full or was == "stale":
                self.state, self.error = "online", None
        if full or was == "stale":
            if was != "online":
                log(f"{self.name}: online")
            if before is not None and was != "online":
                self._missed(before, sessions)
            self.hub.publish(None)
        else:
            for n in names:
                self.hub.publish(join(self.name, n))

    def _missed(self, before, after):
        """After the link was down: a session that needs you now and didn't before gets the notification it would
        have had (its real one may have been lost with the link). Read from the peer's state, not guessed."""
        def waiting(s):
            n = s.get("pending_permissions")
            return (isinstance(n, int) and n > 0) or s.get("state") == "needs_input"
        old = {s["name"]: waiting(s) for s in before}
        for s in after:
            if waiting(s) and not old.get(s["name"]) and self._may_notify():
                self.hub.notify(join(self.name, s["name"]), "input", "needs you (while the link was down)")

    # -- what the hub shows
    def status(self):
        with self.lock:
            return {"name": self.name, "state": self.state, "last_seen": self.last_seen, "error": self.error}

    def listed(self):
        """This peer's sessions for the hub's list: named pc:NAME; `unknown` unless the link is up and fresh."""
        with self.lock:
            state, seen, err, sessions = self.state, self.last_seen, self.error, list(self.sessions or [])
        out = []
        for s in sessions:
            try:
                s = rewrite_summary(self.name, s)
            except Malformed:
                continue
            if state != "online":
                s.update(state="unknown", accepts_input=False, can_force=False, pending_permissions=0, tasks=[],
                         background=0,
                         activity=f"{self.name} is offline (last seen {ago(seen)})" if state == "offline"
                         else f"{self.name} isn't answering: {err}" if state == "stale"
                         else f"connecting to {self.name}…")
            out.append(s)
        return out

    def listed_pages(self):
        with self.lock:
            pages_ = list(self.pages)
        return [p for p in (rewrite_page(self.name, p) for p in pages_) if p is not None]


# ------------------------------------------------------------------ rewriting what a peer says
# Explicit, per shape: only the names and ids the phone acts on are prefixed. Nothing inside a tool's input or a
# message is touched (what the phone shows for approval must be exactly what the peer sent). Everything is checked:
# an id goes into URLs the phone builds, so anything but 32 hex digits is dropped (a hostile "x/../../sessions/a/kill"
# would otherwise turn a tap on Allow into another request); fields of the wrong type are replaced or dropped.
ID = re.compile(r"[0-9a-f]{32}")


class Malformed(ValueError):
    """An answer without the shape grovy's API gives."""


def is_id(v):
    return isinstance(v, str) and bool(ID.fullmatch(v))


def _name(peer, v):
    return join(peer, v) if core.valid_name(v) else None


def _id(peer, v):
    return join(peer, v) if is_id(v) else None


def _list(v):
    return v if isinstance(v, list) else []


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _str(v):
    return v if isinstance(v, str) else None


def summary_ok(s):
    try:
        rewrite_summary("x", s)
        return True
    except Malformed:
        return False


def rewrite_summary(peer, s):
    if not isinstance(s, dict) or not core.valid_name(s.get("name")):
        raise Malformed("a session without a valid name")
    s = dict(s)
    s["name"] = join(peer, s["name"])
    s["machine"] = peer
    st = s.get("state")
    s["state"] = st if isinstance(st, str) and st in states.STATE_RANK else "unknown"
    for k in ("pending_permissions", "queued_prompts", "background"):
        s[k] = s[k] if isinstance(s.get(k), int) and not isinstance(s.get(k), bool) and s[k] >= 0 else 0
    s["last_event"] = _num(s.get("last_event"))
    for k in ("cwd", "last_error", "claude_session_id"):
        s[k] = _str(s.get(k))
    s["activity"] = _str(s.get("activity")) or ""
    for k in ("accepts_input", "can_force", "outdated"):
        s[k] = s.get(k) is True
    s["tasks"] = [t for t in _list(s.get("tasks")) if isinstance(t, dict)]
    m = s.get("meta")
    s["meta"] = {"model": _str(m.get("model")), "effort": _str(m.get("effort")), "fast": m.get("fast") is True,
                 "context": _num(m.get("context"))} if isinstance(m, dict) else None
    i = s.get("issue")
    s["issue"] = {k: _str(i.get(k)) for k in ("branch", "base", "root")} if isinstance(i, dict) else None
    return s


def rewrite_event(peer, e):
    """An event, or None (dropped) when it isn't one."""
    if not isinstance(e, dict) or not isinstance(e.get("id"), int) or isinstance(e.get("id"), bool) \
            or not isinstance(e.get("type"), str):
        return None
    e = dict(e, session=_name(peer, e.get("session")), ts=_num(e.get("ts")))
    d = e.get("data") if isinstance(e.get("data"), dict) else {}
    if e["type"] == "Page":
        d = dict(d, page=_id(peer, d.get("page")))
    e["data"] = d
    return e


def rewrite_item(peer, p):
    """A permission request or a queued prompt, or None (dropped) without a well-formed id."""
    if not isinstance(p, dict) or not is_id(p.get("id")):
        return None
    return dict(p, id=join(peer, p["id"]), session=_name(peer, p.get("session")))


def rewrite_page(peer, p):
    if not isinstance(p, dict) or ("id" in p and not is_id(p["id"])):
        return None
    p = dict(p, session=_name(peer, p.get("session")), share=_id(peer, p.get("share")))
    if "id" in p:
        p["id"] = join(peer, p["id"])
    return p


def _kept(fn, peer, items):
    return [x for x in (fn(peer, i) for i in _list(items)) if x is not None]


def rewrite_detail(peer, d):
    d = dict(d)
    d["session"] = rewrite_summary(peer, d.get("session"))
    d["events"] = _kept(rewrite_event, peer, d.get("events"))
    d["permissions"] = _kept(rewrite_item, peer, d.get("permissions"))
    d["prompts"] = _kept(rewrite_item, peer, d.get("prompts"))
    pg = d.get("pages") if isinstance(d.get("pages"), dict) else {}
    d["pages"] = {join(peer, k): v for k, v in ((k, rewrite_page(peer, v)) for k, v in pg.items() if is_id(k))
                  if v is not None}
    d.pop("now", None)   # timestamps are the peer's; the phone keeps the hub's clock (clocks assumed in sync)
    return d


def rewrite_conversations(peer, d):
    d = dict(d)
    out = []
    for c in _list(d.get("conversations")):
        if not isinstance(c, dict):
            continue
        o = c.get("open_in")
        if isinstance(o, str) and o.startswith("grovy:"):
            c = dict(c, open_in="grovy:" + _name(peer, o[6:]) if core.valid_name(o[6:]) else None)
        out.append(c)
    d["conversations"] = out
    return d


def _events(peer, d):
    return dict(d, events=_kept(rewrite_event, peer, d.get("events")))


def _pages(peer, d):
    return dict(d, pages=_kept(rewrite_page, peer, d.get("pages")))


def _shared(peer, d):
    page = rewrite_page(peer, d.get("page"))
    if page is None:
        raise Malformed("no page in the answer")
    return dict(d, page=page)


def _prompt(peer, d):
    p = rewrite_item(peer, d)
    if p is None:
        raise Malformed("no prompt in the answer")
    return p


def _renamed(peer, d):
    if not core.valid_name(d.get("status")):
        raise Malformed("no new name in the answer")
    return dict(d, status=join(peer, d["status"]))


# ------------------------------------------------------------------ routing the phone's requests
class Answer(Exception):
    """A route's outcome decided early (an error): (status, JSON body)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.body = code, {"error": msg}


SESSION_GET = re.compile(r"/api/sessions/([^/]+)(/history|/pages|/commands)?")
ISSUE_GET = re.compile(r"/api/issues/([^/]+)")
FILE_GET = re.compile(r"/api/(attachments|images)/([^/]+)/([^/]+)")
PAGE_GET = re.compile(r"/p/([^/]+)")
SHARED_GET = re.compile(r"/s/([^/]+)")
XFILE_GET = re.compile(r"/xfile/([^/]+)")
PLUGIN = re.compile(r"/api/x/([a-z0-9][a-z0-9_-]{0,31})/(.+)")
SESSION_POST = re.compile(r"/api/sessions/([^/]+)/(prompts|rename|restart|resume|kill|forget|interrupt)")
ISSUE_POST = re.compile(r"/api/issues/([^/]+)/(finish|discard)")
ID_POST = re.compile(r"/api/(permissions/([^/]+)/decision|prompts/([^/]+)/cancel|pages/([^/]+)/(?:share|delete))")
UPLOAD = re.compile(r"/api/sessions/([^/]+)/attachments")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")


def _is_id(v):
    return is_id(v)


class Peers:
    """All peers of this hub: peers.json, re-read when it changes (every 10s, from grovyd's loop)."""

    def __init__(self, store, path=None, start=True):
        self.store, self.path, self.autostart = store, path, start
        self.peers = {}
        self.problems = []
        self.stamp = None
        self.lock = threading.Lock()

    def __bool__(self):
        return bool(self.peers)

    # -- configuration
    def reload(self):
        path = self.path or config_path()
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size, st.st_mode)
        except OSError:
            stamp = None
        if stamp == self.stamp:
            return
        self.stamp = stamp
        conf, problems = read_config(path)
        for p in problems:
            if p not in self.problems:
                log(p)
        with self.lock:
            self.problems = problems
            old = self.peers
            keep = {n: p for n, p in old.items() if conf.get(n) == p.conf}
            for n, p in old.items():
                if n not in keep:
                    p.close()
            new = {n: Peer(n, c, self) for n, c in conf.items() if n not in keep}
            self.peers = {**keep, **new}
        for p in new.values():
            log(f"{p.name}: via {p.conf['socket']}")
            if self.autostart:
                p.start()
        if new or len(keep) != len(old):
            self.publish(None)

    def close(self):
        with self.lock:
            for p in self.peers.values():
                p.close()

    def get(self, name):
        return self.peers.get(name)

    def status(self):
        return {"peers": [p.status() for p in list(self.peers.values())], "problems": list(self.problems)}

    # -- hooks back into the hub
    def publish(self, name):
        self.store.publish(name)

    def notify(self, name, kind, text):
        self.store._notify(name, kind, text)

    # -- lists
    def merge_sessions(self, local):
        if not self.peers:
            return local
        from .store import session_order
        out = list(local)
        for p in list(self.peers.values()):
            out += p.listed()
        out.sort(key=session_order)
        return out

    def merge_pages(self, local):
        if not self.peers:
            return local
        out = list(local)
        for p in list(self.peers.values()):
            out += p.listed_pages()
        out.sort(key=lambda p: -p["updated"] if isinstance(p.get("updated"), (int, float)) else 0)
        return out

    # -- routes
    def target(self, seg, check):
        """The (Peer, local part) a URL segment names, or None when it isn't a peer's (no ':'): then the hub's own
        route applies, exactly as without peers."""
        got = split(unquote(seg))
        if got is None:
            return None
        peer, local = got
        p = self.peers.get(peer)
        if p is None:
            raise Answer(404, f"no machine named {peer!r} (see `grovy peer ls`)")
        if not check(local):
            raise Answer(404, "not found")
        return p, local

    @staticmethod
    def query(query, session=None):
        q = [(k, v) for k, v in parse_qsl(query or "", keep_blank_values=True) if k != "peer"]
        if session is not None:
            q = [(k, session if k == "session" else v) for k, v in q]
        return ("?" + urlencode(q)) if q else ""

    def _json(self, p, method, path, body=None, rewrite=None, headers=None):
        try:
            code, obj = p.call(method, path, body, headers)
        except PeerError as e:
            return 502, {"error": str(e)}
        if code == 200 and rewrite:
            try:
                obj = rewrite(p.name, obj)
            except Exception as e:   # Malformed, or a type no check foresaw: never the hub's own error
                log(f"{p.name}: unexpected answer to {method} {path.split('?')[0]}: {e!r}")
                return 502, {"error": f"{p.name} answered something unexpected"}
        elif code != 200:
            err = obj.get("error")
            obj = {"error": f"{p.name}: {err}" if isinstance(err, str) else f"{p.name} answered HTTP {code}"}
        return code, obj

    def handle_get(self, path, query, xgrovy=False):
        """A GET the phone sent that belongs to a peer: ("json", code, obj), ("raw", code, ctype, bytes, headers),
        ("stream", conn, response), or None (not a peer's: the hub answers itself)."""
        try:
            return self._get(path, query, xgrovy)
        except Answer as a:
            return "json", a.code, a.body

    def _get(self, path, query, xgrovy):
        if path in ("/api/projects", "/api/conversations"):
            peer = dict(parse_qsl(query or "")).get("peer")
            if not peer:
                return None
            p = self.peers.get(peer)
            if p is None:
                raise Answer(404, f"no machine named {peer!r}")
            rw = rewrite_conversations if path == "/api/conversations" else None
            return ("json",) + self._json(p, "GET", path + self.query(query), rewrite=rw)
        m = SESSION_GET.fullmatch(path)
        if m:
            t = self.target(m.group(1), core.valid_name)
            if not t:
                return None
            p, local = t
            sub = m.group(2) or ""
            rw = {"": lambda n, d: rewrite_detail(n, d), "/history": _events, "/pages": _pages}.get(sub)
            return ("json",) + self._json(p, "GET", f"/api/sessions/{quote(local)}{sub}{self.query(query)}", rewrite=rw)
        m = ISSUE_GET.fullmatch(path)
        if m:
            t = self.target(m.group(1), core.valid_name)
            if not t:
                return None
            return ("json",) + self._json(t[0], "GET", f"/api/issues/{quote(t[1])}")
        m = FILE_GET.fullmatch(path)
        if m:
            t = self.target(m.group(2), core.valid_name)
            if not t:
                return None
            f = unquote(m.group(3))
            if not FILE_NAME.fullmatch(f):
                raise Answer(404, "not found")
            p, local = t
            code, ctype, data = self._raw(p, "GET", f"/api/{m.group(1)}/{quote(local)}/{quote(f)}")
            if code != 200 or ctype not in IMAGE_TYPES:
                raise Answer(404, "not found")
            return "raw", 200, ctype, data, [("Cache-Control", "private, max-age=604800"),
                                             ("Content-Security-Policy", "default-src 'none'")]
        m = PAGE_GET.fullmatch(path)
        if m:
            t = self.target(m.group(1), _is_id)
            if not t:
                return None
            code, ctype, data = self._raw(t[0], "GET", f"/p/{t[1]}")
            if code != 200 or ctype != "text/html":
                return "raw", 404, "text/plain", b"This page was deleted.", []
            return "page", data
        m = XFILE_GET.fullmatch(path)
        if m:
            t = self.target(m.group(1), _is_id)
            if not t:
                return None
            try:
                c, r = t[0].open("GET", f"/xfile/{t[1]}")
            except PeerError as e:
                raise Answer(502, str(e)) from None
            if r.status != 200:
                c.close()
                raise Answer(404, "not found")
            return "stream", c, r
        m = PLUGIN.fullmatch(path)
        if m:
            q = dict(parse_qsl(query or ""))
            t = self.target(quote(q.get("session", "")), core.valid_name) if q.get("session") else None
            if not t:
                return None
            if not xgrovy:   # as for the hub's own plugin routes: a tapped link or a navigation can't add it
                raise Answer(403, "plugin routes are for grovy.api() (X-Grovy header missing)")
            return ("json",) + self._json(t[0], "GET", f"/api/x/{m.group(1)}/{m.group(2)}"
                                          + self.query(query, session=t[1]))
        return None

    def shared(self, path):
        """/s/pc:TOKEN: a peer's shared page (no login, like the hub's own: the link is the credential). Before login,
        so every failure is the same plain 404: nothing about which peers exist or whether they are reachable."""
        m = SHARED_GET.fullmatch(path)
        if not m or SEP not in unquote(m.group(1)):
            return None
        gone = "raw", 404, "text/plain", b"Not found (this page is no longer shared).", []
        try:
            t = self.target(m.group(1), _is_id)
            code, ctype, data = t[0].request("GET", f"/s/{t[1]}")
        except (Answer, PeerError):
            return gone
        if code != 200 or ctype != "text/html":
            return gone
        return "shared", data

    def _raw(self, p, method, path):
        try:
            return p.request(method, path)
        except PeerError as e:
            raise Answer(502, str(e)) from None

    def handle_post(self, path, body, rid=None):
        """A JSON POST the phone sent that belongs to a peer: (code, obj), or None (the hub's own)."""
        try:
            return self._post(path, body, {"X-Request-Id": rid} if rid else None)
        except Answer as a:
            return a.code, a.body

    def _post(self, path, body, headers):
        if path == "/api/sessions":   # a new session: its name says which machine (pc:NAME)
            got = split(body.get("name"))
            if got is None:
                return None
            p = self.peers.get(got[0])
            if p is None:
                raise Answer(404, f"no machine named {got[0]!r}")
            return self._json(p, "POST", path, dict(body, name=got[1]), headers=headers)
        m = SESSION_POST.fullmatch(path)
        if m:
            t = self.target(m.group(1), core.valid_name)
            if not t:
                return None
            p, local = t
            verb = m.group(2)
            if verb == "rename":
                new = body.get("name")
                got = split(new)
                if got is not None:
                    if got[0] != p.name:
                        raise Answer(400, "a session can't move to another machine")
                    new = got[1]
                body = dict(body, name=new)
            rw = _renamed if verb == "rename" else _prompt if verb == "prompts" else None
            return self._json(p, "POST", f"/api/sessions/{quote(local)}/{verb}", body, rw, headers)
        m = ISSUE_POST.fullmatch(path)
        if m:
            t = self.target(m.group(1), core.valid_name)
            if not t:
                return None
            return self._json(t[0], "POST", f"/api/issues/{quote(t[1])}/{m.group(2)}", body, headers=headers)
        m = ID_POST.fullmatch(path)
        if m:
            seg = m.group(2) or m.group(3) or m.group(4)
            t = self.target(seg, _is_id)
            if not t:
                return None
            p, local = t
            rest = path.replace("/" + seg + "/", "/" + local + "/", 1)
            return self._json(p, "POST", rest, body, _shared if rest.endswith("/share") else None, headers)
        m = PLUGIN.fullmatch(path)
        if m and isinstance(body.get("session"), str):
            t = self.target(quote(body["session"]), core.valid_name)
            if not t:
                return None
            return self._json(t[0], "POST", path, dict(body, session=t[1]), headers=headers)
        return None

    def upload_target(self, path):
        """(Peer, peer path) for a file sent to a peer's session, or None."""
        m = UPLOAD.fullmatch(path)
        if not m:
            return None
        t = self.target(m.group(1), core.valid_name)
        return (t[0], f"/api/sessions/{quote(t[1])}/attachments") if t else None


# ------------------------------------------------------------------ command line
def _sock_request(msg):
    from . import client
    return client.sock_request(msg, timeout=3)


def cmd_ls(_):
    conf, problems = read_config()
    live = {}
    try:
        live = {p["name"]: p for p in (_sock_request({"type": "peers"}) or {}).get("peers", [])}
    except (OSError, ValueError):
        print("(grovyd isn't running here: showing peers.json only)", file=sys.stderr)
    if not conf:
        print(f"no peers ({config_path()})")
    for name, e in sorted(conf.items()):
        st = live.get(name)
        what = ("not loaded yet" if st is None else st["state"] + ("" if st["state"] == "online" else
                f", last seen {ago(st.get('last_seen'))}" + (f": {st['error']}" if st.get("error") else "")))
        print(f"{name}\t{e['socket']}\t{what}")
    for p in problems:
        print("problem: " + p, file=sys.stderr)
    return 1 if problems else 0


def read_token(name, stream=None):
    stream = stream or sys.stdin
    if stream.isatty():
        return getpass.getpass(f"{name}'s grovy token (on {name}, run `grovy peer token`): ").strip()
    return stream.readline().strip()


def cmd_add(a):
    if not valid_peer(a.name):
        core.die("machine name: lowercase letters, digits, - and _ (at most 16), e.g. pc")
    sock = a.socket or default_socket(a.name)
    tok = read_token(a.name)   # never on the command line: other users of this machine could read it in `ps`
    entry, why = check_entry(a.name, {"socket": sock, "token": tok, "tls": bool(a.tls)})
    if not entry:
        core.die(why)
    conf, problems = read_config()
    if problems and any("readable by others" in p for p in problems):
        core.die(problems[0])
    for p in problems:
        print(f"dropping an unusable entry: {p}", file=sys.stderr)
    conf[a.name] = entry
    write_config(conf)
    print(f"added {a.name}: the hub reaches it through {sock}\n"
          f"On {a.name}, run: grovy peer tunnel <this machine's ssh name> --name {a.name}"
          + ("" if sock == default_socket(a.name) else f" --remote-socket {sock}"))
    print("grovyd picks the change up within 10 seconds.")
    return 0


def cmd_rm(a):
    conf, _ = read_config()
    if a.name not in conf:
        core.die(f"no peer named {a.name!r}")
    del conf[a.name]
    write_config(conf)
    print(f"removed {a.name}")
    return 0


def cmd_token(_):
    """On the peer: print this grovy's login token, for `grovy peer add` on the hub."""
    tok = core.load_token()
    if not tok:
        core.die("no token yet: start grovy (grovy serve, or grovy setup) first")
    print(tok)
    return 0


# -- the tunnel (run on the peer)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
TUNNEL_OPTS = ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
               "-o", "StreamLocalBindMask=0177"]
HOST_RE = re.compile(r"[A-Za-z0-9_.@%\[\]:-]{1,255}")


def prepare_script(name, remote=None):
    """The shell script run on the hub before each connect: make the socket's folder, remove a stale socket (sshd
    won't bind over one unless it has StreamLocalBindUnlink yes), and print the socket's absolute path."""
    if remote is None:
        return ('d="${XDG_STATE_HOME:-$HOME/.local/state}/grovy/peers" && mkdir -p "$d" && chmod 700 "$d" && '
                f's="$d/{name}.sock" && rm -f "$s" && printf "%s\\n" "$s"')
    if remote.startswith("~/"):
        path = '"$HOME"/' + shlex.quote(remote[2:])
    else:
        path = shlex.quote(remote)
    return f's={path} && mkdir -p "$(dirname "$s")" && rm -f "$s" && printf "%s\\n" "$s"'


def prepare_argv(host, name, remote=None, ssh="ssh"):
    # The remote login shell parses the command line: one sh -c with the script quoted, the same in sh, bash and zsh.
    return [ssh, *SSH_OPTS, "--", host, "sh -c " + shlex.quote(prepare_script(name, remote))]


def tunnel_argv(host, sock, port, ssh="ssh", local="127.0.0.1"):
    # 127.0.0.1, not "localhost" (which may resolve to ::1 first, where grovy doesn't listen by default)
    return [ssh, *SSH_OPTS, *TUNNEL_OPTS, "-R", f"{sock}:{local}:{port}", "--", host]


def check_remote_socket(path):
    if not path or not path.startswith("/") or len(path.encode()) > SOCK_MAX or re.search(r"[\s:]", path):
        return f"the hub's socket path {path!r} won't do: it must be absolute, at most {SOCK_MAX} bytes, " \
               "with no spaces or ':' (pick a shorter one with --remote-socket)"
    return None


def run_tunnel(host, name, port, remote=None, ssh="ssh", run=subprocess.run, popen=subprocess.Popen,
               sleep=time.sleep, clock=time.monotonic, rounds=None, out=sys.stderr, local="127.0.0.1", prepare=True):
    """Keep the reverse tunnel up: prepare, connect, wait for ssh to end, back off, repeat (forever unless rounds).
    prepare=False skips the remote shell (for a key restricted to forwarding): remote is then the absolute path, and
    a stale socket is the hub's sshd's business (StreamLocalBindUnlink yes)."""
    def say(msg):
        print(f"grovy peer tunnel: {msg}", file=out, flush=True)
    delay, n = 2, 0
    while rounds is None or n < rounds:
        n += 1
        start = clock()
        try:
            if prepare:
                r = run(prepare_argv(host, name, remote, ssh), capture_output=True, text=True, timeout=60,
                        stdin=subprocess.DEVNULL)
                lines = (r.stdout or "").strip().splitlines()
            else:
                r, lines = None, [remote or ""]
            if r is not None and r.returncode:
                err = (r.stderr or "").strip()[-300:]
                say(f"couldn't prepare the socket on {host}: {err or f'ssh exit {r.returncode}'}" +
                    ("" if err else " (a key restricted to forwarding? then use --no-prepare --remote-socket PATH)"))
            elif not lines or check_remote_socket(lines[-1]):
                say(check_remote_socket(lines[-1] if lines else ""))
                return 2
            else:
                sock = lines[-1]
                say(f"forwarding {host}:{sock} -> {local}:{port}")
                p = popen(tunnel_argv(host, sock, port, ssh, local), stdin=subprocess.DEVNULL)
                try:
                    code = p.wait()
                except KeyboardInterrupt:
                    p.terminate()
                    raise
                say(f"ssh ended (exit {code})")
        except (OSError, subprocess.SubprocessError) as e:
            say(f"ssh failed: {e}")
        if clock() - start > 60:
            delay = 2
        if rounds is not None and n >= rounds:
            break
        say(f"reconnecting in {delay}s")
        sleep(delay)
        delay = min(delay * 2, 60)
    return 0


def cmd_tunnel(a):
    if not HOST_RE.fullmatch(a.host) or a.host.startswith("-"):
        core.die(f"bad host {a.host!r}")
    if not valid_peer(a.name):
        core.die("--name: lowercase letters, digits, - and _ (at most 16), e.g. pc")
    rs = a.remote_socket
    if rs is not None and (not rs.startswith(("/", "~/")) or re.search(r"[\s:]", rs)):
        core.die("--remote-socket: an absolute path (or ~/...), with no spaces or ':'")
    if a.no_prepare and (rs is None or check_remote_socket(rs)):
        core.die("--no-prepare needs --remote-socket with the socket's absolute path on the hub (no ~: no remote "
                 "shell expands it), e.g. --remote-socket /home/you/.local/state/grovy/peers/pc.sock")
    if not re.fullmatch(r"[0-9a-fA-F.:]{2,45}|localhost", a.local_host):
        core.die("--local-host: an IP address (where this machine's grovy listens)")
    try:
        socket.create_connection((a.local_host, a.port), timeout=2).close()
    except OSError:
        print(f"grovy peer tunnel: warning: nothing answers on {a.local_host}:{a.port} yet (is grovy serving here?)",
              file=sys.stderr)
    try:
        return run_tunnel(a.host, a.name, a.port, a.remote_socket, local=a.local_host, prepare=not a.no_prepare)
    except KeyboardInterrupt:
        return 0


def add_parser(sub):
    s = sub.add_parser("peer", help="show another machine's sessions here (hub), or connect this one to a hub")
    ps = s.add_subparsers(dest="op", required=True)
    x = ps.add_parser("add", help="(hub) add or update a peer; reads its token from stdin")
    x.add_argument("name"), x.add_argument("--socket", help="default: ~/.local/state/grovy/peers/NAME.sock")
    x.add_argument("--tls", action="store_true", help="the peer's grovyd serves HTTPS")
    x.set_defaults(fn=cmd_add)
    x = ps.add_parser("rm", help="(hub) remove a peer")
    x.add_argument("name")
    x.set_defaults(fn=cmd_rm)
    ps.add_parser("ls", help="(hub) list peers and whether they are online").set_defaults(fn=cmd_ls)
    ps.add_parser("token", help="(peer) print this grovy's token, for `grovy peer add` on the hub") \
        .set_defaults(fn=cmd_token)
    x = ps.add_parser("tunnel", help="(peer) keep an SSH reverse tunnel to the hub up (runs until stopped)")
    x.add_argument("host", help="the hub, as ssh knows it (user@host or a ~/.ssh/config name)")
    x.add_argument("--name", default="pc", help="this machine's name on the hub (default: pc)")
    x.add_argument("--remote-socket", help="socket path on the hub (default: ~/.local/state/grovy/peers/NAME.sock)")
    x.add_argument("--port", type=int, default=core.env_int("PORT", 7333), help="this machine's grovy port")
    x.add_argument("--local-host", default="127.0.0.1", help="the address this machine's grovy listens on")
    x.add_argument("--no-prepare", action="store_true",
                   help="run no command on the hub, only the forward (for a key restricted to forwarding); "
                        "needs --remote-socket ABSOLUTE-PATH, its folder made once by hand")
    x.set_defaults(fn=cmd_tunnel)
