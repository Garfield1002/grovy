"""Daemon plugins: grovy's built-in ones (plugins/*.py in the grovy folder, such as git.py) and the user's own
(~/.config/grovy/plugins/*.py), loaded once when grovyd starts (restart it to reload). A user's plugin with the same
file name as a built-in one replaces it.

Each plugin file defines setup(grovy) and uses only the Api object it is given (examples/plugin.py shows it all).
A plugin runs inside the daemon with the daemon's rights, like the rest of grovy: it is the user's own code.
What grovy guarantees in return: callbacks run one at a time on a worker thread of their own, never while Store's
lock is held, so a slow or failing plugin can't stall hooks, permissions or the page. Errors are logged and skipped.
GROVY_PLUGINS=0 loads none (to rule them out when something misbehaves).
"""
import copy
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import traceback

from . import core

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ROUTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}){0,3}")
EVENTS = ("event", "state", "notify")
QUEUE_MAX = 1000
SETUP_TIMEOUT = 10        # seconds for one plugin's setup(); past that it is skipped (and its thread left behind)
ROUTE_TIMEOUT = 30        # seconds a route may take before the request gets 504 (its thread is left to finish)
ROUTES_RUNNING = 8        # routes running at once (hung ones included); more get 503, so they can't use up the server
ANSWER_MAX = 4 * 1024 * 1024
BUILTIN_DIR = os.path.join(core.HERE, "plugins")


def log(msg):
    print(f"grovyd: {msg}", file=sys.stderr, flush=True)


class Api:
    """What setup(grovy) receives. Everything here is safe to call from any thread."""

    def __init__(self, host, name):
        self._host, self.name = host, name
        self._open = True    # on() and route() work only during setup(): a late registration can't sneak in
        self._dead = False   # a plugin that failed to load can't use anything, even from a thread it left running

    def _alive(self, what):
        if self._dead:
            raise RuntimeError(f"grovy.{what}: plugin {self.name} failed to load")

    def _register(self, what, add):
        """Under the host's lock, so the end of setup() (and a rollback) can't interleave with a registration."""
        with self._host.reg_lock:
            self._alive(what)
            if not self._open:
                raise RuntimeError(f"grovy.{what}: only works inside setup()")
            add()

    def on(self, event, fn=None):
        """grovy.on("event", fn): fn(session, record) for each event the conversation records (UserPromptSubmit,
        Stop, PermissionRequest, PermissionResult, Notification, ...; record is a copy).
        grovy.on("state", fn): fn(session, old, new) when a session's state changes (idle, working, needs_input, ...).
        grovy.on("notify", fn): fn(session, kind, text) at the moments GROVY_NOTIFY_CMD runs (input, done, error).
        Also usable as a decorator: @grovy.on("state")."""
        if event not in EVENTS:
            raise ValueError(f"grovy.on: unknown event {event!r} (one of {', '.join(EVENTS)})")
        if fn is None:
            return lambda f: self.on(event, f)
        if not callable(fn):
            raise TypeError("grovy.on needs a function")
        self._register("on", lambda: self._host.handlers[event].append((self.name, fn)))
        return fn

    def route(self, method, path, fn=None):
        """grovy.route("GET", "stats", fn) answers /api/x/PLUGIN/stats, behind grovy's login like the rest of the API.
        fn(request) gets request.method, .path, .query (dict of str) and .body (the JSON object, for POST), and returns
        a JSON-able value (status 200) or (status, value). POST bodies must be JSON, as for grovy's own API."""
        method = str(method).upper()
        if method not in ("GET", "POST"):
            raise ValueError("grovy.route: method must be GET or POST")
        if not ROUTE_RE.fullmatch(str(path)):
            raise ValueError(f"grovy.route: bad path {path!r} (letters, digits, _ . -, up to 4 parts)")
        if fn is None:
            return lambda f: self.route(method, path, f)
        if not callable(fn):
            raise TypeError("grovy.route needs a function")
        self._register("route", lambda: self._host.routes.__setitem__((self.name, method, path), fn))
        return fn

    # Copies are made under Store's lock: the originals change under other threads, and a plugin must never hold
    # (or change) grovy's own objects.
    def sessions(self):
        """The session list, as the home screen gets it (a copy)."""
        self._alive("sessions")
        with self._host.store.lock:
            return copy.deepcopy(self._host.store.list_sessions())

    def session(self, name):
        """One session's detail (state, recent events, permissions, prompts), or None."""
        self._alive("session")
        with self._host.store.lock:
            return copy.deepcopy(self._host.store.detail(name))

    def send(self, session, text):
        """Send a message to a session, with exactly the rules of the phone's Send button (queued while Claude is busy,
        never forced). Returns (status, result); the result is a copy."""
        self._alive("send")
        with self._host.store.lock:
            code, res = self._host.store.enqueue(session, text, False)
            return code, copy.deepcopy(res)

    def log(self, msg):
        log(f"plugin {self.name}: {msg}")


def _modname(f):
    return f"grovy_plugin_{f[:-3].replace('-', '_')}"


def _scripts(folder):
    """{file name: path} of the plugins in a folder (files starting with _ are left out)."""
    try:
        return {f: os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".py") and not f.startswith("_")}
    except OSError:
        return {}


class Request:
    def __init__(self, method, path, query, body):
        self.method, self.path, self.query, self.body = method, path, query, body


class Plugins:
    def __init__(self, store, folder=None, builtin=None):
        self.store = store
        self.folder = folder or os.path.join(core.config_dir(), "plugins")
        self.builtin = builtin   # grovyd passes BUILTIN_DIR; None loads only the user's (as the tests do)
        self.handlers = {e: [] for e in EVENTS}
        self.routes = {}
        self.loaded, self.failed = [], {}
        self.q = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self.thread = None
        self.running = threading.BoundedSemaphore(ROUTES_RUNNING)
        self.reg_lock = threading.Lock()

    def load(self):
        if core.env("PLUGINS", "1") == "0":
            return self
        mine = _scripts(self.folder)
        # built-in ones first (unless replaced by one of the user's), then the user's, each in file-name order
        builtin = _scripts(self.builtin) if self.builtin else {}
        taken = {_modname(f) for f in mine}   # a user's a-b.py also replaces a built-in a_b.py (same module name)
        files = sorted((f, p) for f, p in builtin.items() if _modname(f) not in taken) + sorted(mine.items())
        modules = {}
        for f, path in files:
            name = f[:-3]
            modname = _modname(f)
            why = ("file name must be lowercase letters, digits, _ or - (max 32)" if not NAME_RE.fullmatch(name)
                   else f"same module name as {modules[modname]}.py" if modname in modules else None)
            if why:
                self.failed[name] = why
                log(f"plugin {f} skipped: {why}")
                continue
            modules[modname] = name
            err = self._setup(name, modname, path)
            if err:
                self.failed[name] = err
                log(f"plugin {name} failed to load: {err}")
                continue
            self.loaded.append(name)
        if self.loaded:
            log(f"plugins loaded: {', '.join(self.loaded)}")
        if any(self.handlers.values()):
            self.thread = threading.Thread(target=self._run, name="grovy-plugins", daemon=True)
            self.thread.start()
        return self

    def _setup(self, name, modname, path):
        """Import one plugin and run its setup() on a thread of its own, with a deadline. Returns an error or None.
        A plugin that fails is removed whole: what it registered, and its module."""
        api = Api(self, name)
        before = ({e: list(h) for e, h in self.handlers.items()}, dict(self.routes))
        out = {}

        def run():
            try:
                spec = importlib.util.spec_from_file_location(modname, path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[modname] = mod    # as for any import (dataclasses, pickle and others look it up there)
                spec.loader.exec_module(mod)
                if not callable(getattr(mod, "setup", None)):
                    raise TypeError("it has no setup(grovy) function")
                mod.setup(api)
            except BaseException as e:   # SystemExit included: a plugin must never stop the daemon
                out["err"] = f"{type(e).__name__}: {e}"
                traceback.print_exc(file=sys.stderr)

        t = threading.Thread(target=run, name=f"grovy-plugin-{name}", daemon=True)
        t.start()
        t.join(SETUP_TIMEOUT)
        with self.reg_lock:
            api._open = False
            err = out.get("err") or (f"setup() took longer than {SETUP_TIMEOUT}s" if t.is_alive() else None)
            if err:
                api._dead = True
                self.handlers, self.routes = before
                sys.modules.pop(modname, None)
        return err

    def emit(self, event, *args):
        """Called by Store, often under its lock: only queues, never waits."""
        if not self.handlers[event]:
            return
        try:
            self.q.put_nowait((event, copy.deepcopy(args)))
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 100) or self.dropped % 1000 == 0:
                log(f"plugins are too slow: {self.dropped} events dropped so far")
        except Exception as e:   # never let passing it on break the state change that emitted it
            log(f"plugins: couldn't pass on {event}: {type(e).__name__}: {e}")

    def _run(self):
        while True:
            event, args = self.q.get()
            for name, fn in list(self.handlers[event]):
                try:
                    fn(*args)
                except BaseException as e:   # SystemExit included: the worker must outlive any plugin
                    log(f"plugin {name} ({event}): {type(e).__name__}: {e}")

    def handle(self, method, plugin, path, query, body):
        """An /api/x/PLUGIN/PATH request. Returns (status, body): the JSON bytes to send, or an error dict. The route runs on a thread of its own with
        a deadline, so one that never returns costs a thread, not one of the server's connection slots."""
        fn = self.routes.get((plugin, method, path))
        if fn is None:
            return 404, {"error": "not found"}
        if not self.running.acquire(blocking=False):
            return 503, {"error": f"too many plugin requests are still running (at most {ROUTES_RUNNING})"}
        out = {}

        def run():
            try:
                out["res"] = fn(Request(method, path, query, body))
            except BaseException as e:
                out["err"] = f"{type(e).__name__}: {e}"
            finally:
                self.running.release()

        t = threading.Thread(target=run, name=f"grovy-route-{plugin}", daemon=True)
        try:
            t.start()
        except RuntimeError as e:   # no thread to run it: give the slot back
            self.running.release()
            return 503, {"error": f"can't run plugin {plugin} now: {e}"}
        t.join(ROUTE_TIMEOUT)
        if t.is_alive():
            log(f"plugin {plugin} ({method} {path}): no answer after {ROUTE_TIMEOUT}s")
            return 504, {"error": f"plugin {plugin} didn't answer in {ROUTE_TIMEOUT}s"}
        if "err" in out:
            log(f"plugin {plugin} ({method} {path}): {out['err']}")
            return 500, {"error": f"plugin {plugin} failed: {out['err']}"}
        res, code = out.get("res"), 200
        if (isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int) and not isinstance(res[0], bool)
                and 100 <= res[0] <= 599):   # (status, value); other tuples are data
            code, res = res
            if code < 200 or code in (204, 304):   # statuses that can't carry the JSON body
                return 500, {"error": f"plugin {plugin} answered status {code}, which can't carry an answer"}
        try:   # encoded here, once, exactly as sent: no NaN (JSON.parse rejects it), no lone surrogates
            body = json.dumps(res, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError) as e:   # UnicodeEncodeError is a ValueError
            log(f"plugin {plugin} ({method} {path}): answer is not JSON: {e}")
            return 500, {"error": f"plugin {plugin} answered something that isn't JSON"}
        if len(body) > ANSWER_MAX:
            return 500, {"error": f"plugin {plugin} answered more than {ANSWER_MAX // 1048576} MB"}
        return code, body
