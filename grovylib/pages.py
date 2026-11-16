"""HTML pages Claude publishes: storage, sharing, and the sandbox they are served in."""
import hmac
import os
import re
import secrets
import uuid

from . import core


def with_bridge(html):
    """Insert PAGE_BRIDGE after the doctype (anything before it would put the page in quirks mode)."""
    m = re.match(rb"(\xef\xbb\xbf)?\s*<!doctype[^>]*>", html, re.I)
    return html[:m.end()] + PAGE_BRIDGE + html[m.end():] if m else PAGE_BRIDGE + html


PAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PAGE_MAX = 5 * 1024 * 1024
PAGE_CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
# Pages are Claude-written HTML: sandboxed (opaque origin: no cookies, no grovy API, no other pages), libraries
# only from the CDN allowlist, and no fetch/XHR/WebSocket/images from the network, so a page can't send data out.
# Browsers can't stop a page from navigating its own frame elsewhere; docs/DESIGN.md "Known limitations" says so.
PAGE_CSP = ("sandbox allow-scripts allow-forms allow-modals allow-downloads; default-src 'none'; "
            f"script-src 'unsafe-inline' 'unsafe-eval' {PAGE_CDNS}; "
            f"style-src 'unsafe-inline' {PAGE_CDNS} https://fonts.googleapis.com; "
            f"font-src data: {PAGE_CDNS} https://fonts.gstatic.com; img-src data: blob:; media-src data: blob:; "
            "connect-src 'none'; frame-src 'none'; worker-src blob:; form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'self'")
# Injected first into every page: grovy.send(text) proposes a message (the phone puts it in the message box).
PAGE_BRIDGE = (b"<script>window.grovy={send:function(t){if(window.parent===window){alert('Open this page from grovy "
               b"to send it to Claude.');return}window.parent.postMessage({grovy:'send',text:String(t)},'*')}};</script>")


class Pages:
    """HTML pages Claude publishes, and sharing them: part of Store."""

    def publish_page(self, session, name, title, html):
        """Store a copy of an HTML page Claude made (`grovy page publish`). Same session + name = new version."""
        if not isinstance(name, str) or not PAGE_NAME_RE.fullmatch(name):
            return 400, "page name: letters, digits, '.', '-', '_' (max 64)"
        if not isinstance(html, str):
            return 400, "no HTML"
        data = html.encode("utf-8", "surrogateescape")
        if len(data) > PAGE_MAX:
            return 413, f"page too large ({len(data) // 1024} KB, max {PAGE_MAX // 1024} KB)"
        title = (title if isinstance(title, str) and title.strip() else name).strip()[:200]
        with self.lock:
            session = self._canon(session)
            if session not in self.sessions:
                return 404, f"unknown session {session!r}"
            pid = next((k for k, p in self.pages.items() if p["session"] == session and p["name"] == name), None)
            pid = pid or uuid.uuid4().hex
            os.makedirs(self.pages_dir, mode=0o700, exist_ok=True)
            path = os.path.join(self.pages_dir, pid + ".html")
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            p = self.pages.setdefault(pid, {"id": pid, "session": session, "name": name, "version": 0})
            p.update(title=title, version=p["version"] + 1, updated=core.now(), size=len(data))
            self._save_pages()
            self._record(session, "Page", {"page": pid, "name": name, "title": title, "version": p["version"]})
            out = dict(p)
        self.publish(session)
        return 200, out

    def share_page(self, pid, shared):
        """A shared page gets an unguessable link (/s/<token>) that works without the grovy token. Unsharing
        revokes it; sharing again issues a new one. Republishing keeps the link."""
        with self.lock:
            p = self.pages.get(pid)
            if not p:
                return 404, "unknown page"
            if bool(shared) != bool(p.get("share")):
                p["share"] = secrets.token_hex(16) if shared else None
                self._save_pages()
            out = dict(p)
        self.publish(out["session"])
        return 200, out

    def shared_page_html(self, token):
        with self.lock:
            pid = next((k for k, p in self.pages.items()
                        if p.get("share") and hmac.compare_digest(p["share"], token)), None)
        return self.page_html(pid) if pid else None

    def _drop_page(self, pid):
        p = self.pages.pop(pid, None)
        try:
            os.unlink(os.path.join(self.pages_dir, pid + ".html"))
        except FileNotFoundError:
            pass
        self._save_pages()
        return p

    def delete_page(self, pid, session=None):
        with self.lock:
            p = self.pages.get(pid)
            if not p or (session is not None and p["session"] != self._canon(session)):
                return 404, "unknown page"
            self._drop_page(pid)
        self.publish(p["session"])
        return 200, "deleted"

    def list_pages(self, session=None):
        with self.lock:
            session = session and self._canon(session)
            return sorted((dict(p) for p in self.pages.values() if session is None or p["session"] == session),
                          key=lambda p: -p["updated"])

    def page_html(self, pid):
        with self.lock:
            p = self.pages.get(pid)
            if not p:
                return None
            try:
                with open(os.path.join(self.pages_dir, pid + ".html"), "rb") as f:
                    return dict(p), f.read()
            except OSError:
                return None
