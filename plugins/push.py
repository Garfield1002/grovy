"""Web Push: notifications on your devices when a session needs you, finishes or fails, even with grovy closed.

A built-in plugin (written only against the plugin Api, like a user's own); ⋯ → Notifications (extensions/push.js)
turns it on per device, and the service worker (sw.js) shows what arrives. Needs HTTPS (`grovy tls init`): browsers
only allow push on a secure page.

How it keeps your data in: every push is *empty*. It goes through the browser maker's push service (Apple, Google,
Mozilla: there is no other way to wake a phone), which so learns only that something happened, and when. The phone
then asks grovy itself, over HTTPS and with its login, what it was (POST pending). That's also why no message
encryption is needed, and why only the openssl command is (to make and use the VAPID key that identifies this grovy
to push services).

Routes (behind grovy's login): GET key, POST subscribe {endpoint}, POST unsubscribe {endpoint},
POST pending {endpoint}, POST test {endpoint}. GROVY_PUSH_CONTACT sets the contact push services may use
(default: grovy's project page; Apple refuses placeholder addresses).
"""
import base64
import collections
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PENDING_MAX = 20     # messages kept per device until it fetches them
TTL = 6 * 3600       # how long a push service keeps trying a device that is off (seconds)
JWT_LIFE = 12 * 3600
CONTACT = "https://github.com/Garfield1002/grovy"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):   # a push service never redirects: don't carry the token anywhere else
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _der_int(b, i):
    """One DER INTEGER at b[i]: (value, next index)."""
    if b[i] != 0x02:
        raise ValueError("not a DER integer")
    n = b[i + 1]
    return int.from_bytes(b[i + 2:i + 2 + n], "big"), i + 2 + n


def raw_signature(der):
    """An ECDSA signature as openssl writes it (DER SEQUENCE of r and s) -> the 64 bytes r||s that JWT's ES256 uses."""
    if der[0] != 0x30 or der[1] & 0x80:   # a P-256 signature is under 128 bytes: short-form length
        raise ValueError("unexpected signature encoding")
    r, i = _der_int(der, 2)
    s, _ = _der_int(der, i)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


class Vapid:
    """This grovy's VAPID key (P-256, made once, kept in the plugin's folder) and the tokens it signs."""

    def __init__(self, folder, contact):
        self.key, self.contact = os.path.join(folder, "vapid.pem"), contact
        if not os.path.exists(self.key):
            tmp = self.key + ".new"
            old = os.umask(0o077)
            try:
                _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", tmp)
            finally:
                os.umask(old)
            os.replace(tmp, self.key)
        der = _openssl("ec", "-in", self.key, "-pubout", "-outform", "DER", binary=True)
        self.public = der[-65:]   # the uncompressed point closes the SubjectPublicKeyInfo
        if len(self.public) != 65 or self.public[0] != 4:
            raise ValueError("unexpected VAPID public key")
        self.cache, self.lock = {}, threading.Lock()

    def header(self, endpoint):
        """The Authorization header for a push to `endpoint` (a token per push service, reused until near expiry)."""
        u = urllib.parse.urlsplit(endpoint)
        aud = f"{u.scheme}://{u.netloc}"
        with self.lock:
            tok, exp = self.cache.get(aud, (None, 0))
            if time.time() > exp - 3600:
                exp = int(time.time()) + JWT_LIFE
                head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
                claims = b64u(json.dumps({"aud": aud, "exp": exp, "sub": self.contact}).encode())
                signing = f"{head}.{claims}".encode()
                sig = _openssl("dgst", "-sha256", "-sign", self.key, data=signing, binary=True)
                tok = f"{head}.{claims}.{b64u(raw_signature(sig))}"
                self.cache[aud] = (tok, exp)
        return f"vapid t={tok}, k={b64u(self.public)}"


def _openssl(*args, data=None, binary=False):
    r = subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"openssl {args[0]} failed: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout if binary else r.stdout.decode()


def valid_endpoint(e):
    if not isinstance(e, str) or len(e) > 2048:
        return False
    u = urllib.parse.urlsplit(e)
    return u.scheme == "https" and bool(u.hostname) and not u.username and not u.password


class Push:
    def __init__(self, grovy, folder, contact, opener=None):
        self.grovy, self.opener = grovy, opener
        self.path = os.path.join(folder, "subscriptions.json")
        self.vapid = Vapid(folder, contact)
        self.lock = threading.Lock()
        try:
            with open(self.path) as f:
                self.subs = {e: v for e, v in json.load(f).items() if valid_endpoint(e)}
        except (OSError, ValueError, AttributeError):
            self.subs = {}
        self.pending = collections.defaultdict(lambda: collections.deque(maxlen=PENDING_MAX))
        self.latest = {}

    def _save(self):   # under self.lock
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            json.dump(self.subs, f)
        os.replace(tmp, self.path)

    # -- routes
    def subscribe(self, endpoint):
        with self.lock:
            if endpoint not in self.subs:
                self.subs[endpoint] = {"since": int(time.time())}
                self._save()

    def unsubscribe(self, endpoint):
        with self.lock:
            if self.subs.pop(endpoint, None) is not None:
                self._save()
            self.pending.pop(endpoint, None)
            self.latest.pop(endpoint, None)

    def known(self, endpoint):
        with self.lock:
            return endpoint in self.subs

    def take(self, endpoint):
        """What a device's service worker shows: the messages it hasn't fetched, and the latest one (for a push that
        finds nothing new, so it can still show something)."""
        with self.lock:
            if endpoint not in self.subs:
                return None
            q = self.pending.pop(endpoint, ())
            return {"messages": list(q), "latest": self.latest.get(endpoint)}

    # -- sending
    def notify(self, session, kind, text, only=None, title=None):
        msg = {"session": session, "kind": kind, "text": str(text)[:300], "ts": int(time.time())}
        if title:
            msg["title"] = title
        with self.lock:
            targets = [only] if only else list(self.subs)
            for e in targets:
                self.pending[e].append(msg)
                self.latest[e] = msg
        topic = b64u(hashlib.sha256(f"{session}/{kind}".encode()).digest()[:16])   # 22 characters, as RFC 8030 allows
        for e in targets:   # each on its own thread: a slow push service never holds up the others (or grovy)
            threading.Thread(target=self._send, args=(e, kind == "input", topic), daemon=True).start()

    def _send(self, endpoint, urgent, topic):
        try:
            req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
                "Authorization": self.vapid.header(endpoint), "TTL": str(TTL), "Urgency": "high" if urgent else "normal",
                # pushes not yet delivered (a phone that is off) with the same topic replace each other
                "Topic": topic, "Content-Type": "application/octet-stream", "Content-Length": "0",
                "User-Agent": "grovy"})
            try:
                with (self.opener or OPENER.open)(req, timeout=20) as r:
                    r.read(1024)
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):   # the device unsubscribed, or the browser dropped it
                    self.grovy.log(f"a device's subscription has ended ({e.code}); removed")
                    self.unsubscribe(endpoint)
                else:
                    self.grovy.log(f"push service answered {e.code}: {e.read(300).decode(errors='replace')}")
        except Exception as e:   # its own thread: log, never a stray traceback
            self.grovy.log(f"push failed: {type(e).__name__}: {e}")


def setup(grovy):
    push = Push(grovy, grovy.data_dir(), os.environ.get("GROVY_PUSH_CONTACT") or CONTACT)

    def endpoint_of(request):
        e = (request.body or {}).get("endpoint")
        if not valid_endpoint(e):
            raise ValueError("endpoint must be an https:// push service address")
        return e

    def route(method, path):
        def wrap(fn):
            def run(request):
                try:
                    return fn(request)
                except ValueError as e:
                    return 400, {"error": str(e)}
            grovy.route(method, path, run)
            return fn
        return wrap

    @route("GET", "key")
    def key(request):
        return {"key": b64u(push.vapid.public)}

    @route("POST", "subscribe")
    def subscribe(request):
        push.subscribe(endpoint_of(request))
        return {"ok": True}

    @route("POST", "unsubscribe")
    def unsubscribe(request):
        push.unsubscribe(endpoint_of(request))
        return {"ok": True}

    @route("POST", "pending")
    def pending(request):
        got = push.take(endpoint_of(request))
        return got if got is not None else (404, {"error": "this device isn't subscribed"})

    @route("POST", "test")
    def test(request):
        e = endpoint_of(request)
        if not push.known(e):
            return 404, {"error": "this device isn't subscribed"}
        push.notify("", "done", "Notifications work on this device.", only=e, title="grovy")
        return {"ok": True}

    @grovy.on("notify")
    def notified(session, kind, text):
        push.notify(session, kind, text)
