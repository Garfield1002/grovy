"""Live widgets that scripts in a session show and update."""
import json
import re
import threading
from collections import deque

from . import core


LIVE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
LIVE_MAX = 64 * 1024        # bytes of JSON in one live widget
LIVE_NEW_PER_MIN = 30       # new live cards per session per minute: a runaway loop must not flush the chat history
LIVE_STATUS = ("running", "done", "failed", "stopped")
LIVE_PUBLISH_EVERY = 0.5    # seconds between browser refreshes caused by live updates (per session)
LIVE_LOG_EVERY = 2.0        # seconds between event-log writes of one running widget (the reconcile loop flushes)


class LiveCards:
    """Live widgets that scripts in a session show and update: part of Store."""

    # ------------------------------------------------------------ live widgets (from scripts: `import grovy`)
    def live_update(self, session, key, spec, status="running", message=None):
        """Create or update, in place, a widget card a script running in the session shows on the phone.
        The page renders and validates the widget; here we only bound it."""
        if not isinstance(key, str) or not LIVE_KEY_RE.fullmatch(key):
            return 400, "key: letters, digits, '.', ':', '-', '_' (max 64)"
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
            return 400, "a widget is a JSON object with a \"type\""
        if status not in LIVE_STATUS:
            return 400, "status: " + ", ".join(LIVE_STATUS)
        if len(json.dumps(spec, ensure_ascii=False).encode()) > LIVE_MAX:
            return 413, f"widget too large (max {LIVE_MAX // 1024} KB of JSON)"
        message = message[:500] if isinstance(message, str) and message.strip() else None
        t = core.now()
        with self.lock:
            session = self._canon(session)
            if session not in self.sessions:
                return 404, f"unknown session {session!r}"
            cards = self.live.setdefault(session, {})
            meta = cards.get(key)
            if meta and not any(r is meta["rec"] for r in self.events.get(session, ())):
                meta = None   # scrolled out of the history kept in memory: show it again as a new card
            if meta is None:
                recent = self.live_new.setdefault(session, deque())
                while recent and t - recent[0] > 60:
                    recent.popleft()
                if len(recent) >= LIVE_NEW_PER_MIN:
                    return 429, (f"too many new widgets (max {LIVE_NEW_PER_MIN} a minute per session); "
                                 "update an existing one by key instead")
                recent.append(t)
                rec = self._record(session, "Live", {"key": key, "spec": spec, "status": status, "message": message,
                                                     "rev": 1, "created": t, "updated": t})
                cards[key] = {"rec": rec, "logged": t, "dirty": False}
            else:
                rec, d = meta["rec"], meta["rec"]["data"]
                # a new dict, not d.update(): the HTTP thread may be serializing the old one outside the lock
                rec["data"] = {**d, "spec": spec, "status": status, "message": message, "rev": d["rev"] + 1, "updated": t}
                meta["dirty"] = True
                if status != "running" or t - meta["logged"] >= LIVE_LOG_EVERY:
                    self._log_live(meta)
            out = {"key": key, "id": rec["id"], "rev": rec["data"]["rev"]}
        self._publish_soon(session)
        return 200, out

    def _log_live(self, meta):
        self._log.write(json.dumps(meta["rec"], ensure_ascii=False) + "\n")   # same id: the last line wins on load
        meta["logged"], meta["dirty"] = core.now(), False

    def flush_live(self):
        with self.lock:
            for cards in self.live.values():
                for meta in cards.values():
                    if meta["dirty"]:
                        self._log_live(meta)

    def _publish_soon(self, name):
        """Live updates can come many times a second: refresh browsers at most every LIVE_PUBLISH_EVERY,
        always including the last update."""
        with self.lock:
            wait = self._pub_at.get(name, 0) + LIVE_PUBLISH_EVERY - core.now()
            if wait > 0:
                if name not in self._pub_timer:
                    tm = self._pub_timer[name] = threading.Timer(wait, self._publish_due, (name,))
                    tm.daemon = True
                    tm.start()
                return
            self._pub_at[name] = core.now()
        self.publish(name)

    def _publish_due(self, name):
        with self.lock:
            self._pub_timer.pop(name, None)
            self._pub_at[name] = core.now()
        self.publish(name)
