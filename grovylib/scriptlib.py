"""The library scripts use inside a session: `import grovy` then grovy.track/progress/note/log/show."""
import atexit
import os
import re
import sys
import threading
import time
from collections import deque

from . import client, core


# Scripts Claude runs inside a grovy session can `import grovy` (the session's PYTHONPATH includes this folder)
# and show cards in the phone chat that update in place while they run:
#
#     for x in grovy.track(items, "Converting"): ...       # a progress bar with an ETA
#     p = grovy.progress("Training", total=50); p.set(7)   # or p.advance()
#     grovy.note("**Epoch 3**: loss 0.41")                  # a markdown note
#     lg = grovy.log("pytest"); lg.write(text)             # the last lines of some output
#     w = grovy.show({"type": "chart", ...}); w.update({...})   # any widget, updated by replacing it
#
# Outside a grovy session, or when the daemon can't be reached, every call does nothing, quickly. A script must never
# fail or slow down because of the phone.

LIVE_EVERY = 0.5          # seconds between updates of one widget sent to the daemon (the last one always goes)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_live = {"off_until": 0.0, "open": {}, "crash": None, "thread": None, "warned": set()}
_live_lock = threading.RLock()


def active():
    """True inside a grovy session (whether or not the daemon is up)."""
    return bool(core.env("SESSION"))


def _live_send(key, spec, status="running", message=None):
    """One update to the daemon, acknowledged (so one widget's updates arrive in order). Never raises."""
    session = core.env("SESSION")
    if not session or time.monotonic() < _live["off_until"]:
        return None
    msg = {"type": "live", "session": session, "key": key, "spec": spec, "status": status, "message": message}
    try:
        r = client.sock_request(msg, timeout=1.0)
    except (OSError, ValueError, TypeError):
        _live["off_until"] = time.monotonic() + 30   # daemon down or stuck: don't make every call wait
        return None
    if isinstance(r, dict) and "error" in r and r["error"] not in _live["warned"]:
        _live["warned"].add(r["error"])
        print(f"grovy: widget not shown: {r['error']}", file=sys.stderr)
    return r


def _live_key():
    return "w" + os.urandom(6).hex()


class Live:
    """A widget card in the phone chat, updated in place. Use it as a context manager to mark it failed on an
    exception. Widgets left open when the script exits are closed then ("stopped", or "failed" after a crash)."""

    def __init__(self, spec=None, key=None):
        self.key = key or _live_key()
        self.status, self.message = "running", None
        self._spec, self._sent, self._pending = spec, 0.0, False
        self._lock = threading.Lock()   # the background thread flushes too
        if not active():
            return
        _live_start()
        with _live_lock:
            _live["open"][self.key] = self
        self._flush()

    def render(self):
        return self._spec

    def update(self, spec):
        """Replace the widget (any type grovy draws: chart, stats, checklist, ...)."""
        self._spec = spec
        self._changed()
        return self

    def done(self, message=None):
        self._close("done", message)

    def fail(self, message=None):
        self._close("failed", message)

    def _changed(self):
        if not active() or self.status != "running":
            return
        if time.monotonic() - self._sent >= LIVE_EVERY:
            self._flush()
        else:
            self._pending = True   # the background thread sends it, so the last state is never lost

    def _flush(self):
        with self._lock:
            self._sent, self._pending = time.monotonic(), False
            _live_send(self.key, self.render(), self.status, self.message)

    def _close(self, status, message=None):
        if self.status != "running":
            return
        self.status, self.message = status, message
        with _live_lock:
            _live["open"].pop(self.key, None)
        if active():
            self._flush()

    def _at_exit(self):
        if _live["crash"]:
            self.fail("the script crashed: " + _live["crash"])
        else:
            self._close("stopped", "the script ended without finishing it")

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            self.done()
        elif issubclass(et, GeneratorExit):   # the loop over track() was left early
            self._close("stopped", "stopped early")
        elif issubclass(et, KeyboardInterrupt):
            self._close("stopped", "interrupted")
        else:
            self.fail(f"{et.__name__}: {ev}"[:300])
        return False


class Progress(Live):
    """A progress bar. With total=None it shows a count and a moving bar. The phone adds the elapsed time and,
    with a total, an estimate of the time left."""

    def __init__(self, title=None, total=None, label=None, key=None):
        self.title, self.total, self.label, self.n, self.note = title, total, label, 0, None
        super().__init__(None, key)

    def render(self):
        item = {"label": str(self.label or ""), "value": self.n}
        if self.total is None:
            item["indeterminate"] = True
        else:
            item["max"] = self.total
        if self.note:
            item["note"] = str(self.note)
        return {"type": "progress", "title": self.title, "items": [item], "eta": True}

    def set(self, n, note=None, total=None, label=None):
        self.n = n
        if note is not None:
            self.note = note
        if total is not None:
            self.total = total
        if label is not None:
            self.label = label
        self._changed()
        return self

    def advance(self, k=1, note=None):
        return self.set(self.n + k, note)

    def _at_exit(self):
        if not _live["crash"] and self.total is not None and self.n >= self.total:
            self.done()
        else:
            super()._at_exit()


class Log(Live):
    """The last `lines` lines of some output (ANSI colours stripped). Works as a file: print(..., file=lg)."""

    def __init__(self, title=None, lines=40, key=None):
        self.title, self.lines, self.partial = title, deque(maxlen=max(1, min(int(lines), 200))), ""
        super().__init__(None, key)

    def render(self):
        lines = list(self.lines) + ([self.partial] if self.partial else [])
        return {"type": "log", "title": self.title, "lines": [l[:500] for l in lines[-self.lines.maxlen:]]}

    def write(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        parts = (self.partial + _ANSI.sub("", str(text))).replace("\r\n", "\n").split("\n")
        self.partial = parts.pop()
        self.lines.extend(p.rsplit("\r", 1)[-1] for p in parts)   # \r: a redrawn line, keep what's left
        self.partial = self.partial.rsplit("\r", 1)[-1]
        self._changed()
        return len(text)

    def print(self, *args, sep=" "):
        self.write(sep.join(map(str, args)) + "\n")

    def flush(self):
        pass


def show(spec, key=None, done=False):
    """Show any widget (a dict like the ```widget blocks). Returns a Live: .update(new_spec), .done(), .fail()."""
    w = Live(spec, key)
    if done:
        w.done()
    return w


def progress(title=None, total=None, label=None, key=None):
    return Progress(title, total, label, key)


def track(iterable, title=None, total=None, label=None):
    """Like tqdm: `for x in grovy.track(xs, "Title")` shows a live progress bar."""
    if total is None:
        try:
            total = len(iterable)
        except TypeError:
            pass
    with Progress(title, total, label) as p:
        for x in iterable:
            yield x
            p.advance()


def log(title=None, lines=40, key=None):
    return Log(title, lines, key)


def note(text, tone=None, title=None):
    """A markdown note in the chat, now (no need to wait for Claude's reply)."""
    if active():
        _live_send(_live_key(), {"type": "note", "text": str(text), "tone": tone, "title": title}, "done")


def _live_start():
    """Once per process: a thread that sends throttled last updates, and exit/crash handlers."""
    with _live_lock:
        if _live["thread"]:
            return
        _live["thread"] = t = threading.Thread(target=_live_loop, name="grovy-live", daemon=True)
        t.start()
    prev = sys.excepthook

    def hook(et, ev, tb):
        _live["crash"] = f"{et.__name__}: {ev}"[:300]
        prev(et, ev, tb)
    sys.excepthook = hook
    atexit.register(_live_exit)


def _live_loop():
    while True:
        time.sleep(LIVE_EVERY / 2)
        with _live_lock:
            due = [w for w in _live["open"].values() if w._pending and time.monotonic() - w._sent >= LIVE_EVERY]
        for w in due:
            w._flush()


def _live_exit():
    with _live_lock:
        left = list(_live["open"].values())
    for w in left:
        w._at_exit()
