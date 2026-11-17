"""Example grovy plugin: copy to ~/.config/grovy/plugins/worktime.py and restart grovy
(`systemctl --user restart grovy`). It counts how long each session has spent working since grovy started, and
serves the totals at /api/x/worktime/totals.

A plugin runs inside the daemon. It defines setup(grovy) and uses only `grovy`:

    grovy.on("event", fn)    fn(session, record): each event the conversation records (a copy)
    grovy.on("state", fn)    fn(session, old, new): a session's state changed (idle, working, needs_input, ...)
    grovy.on("notify", fn)   fn(session, kind, text): the moments GROVY_NOTIFY_CMD runs (kind: input, done, error)
    grovy.context(fn)        fn(session, info) returns text added to what Claude is told when a session starts,
                             resumes, or after /clear and /compact (info: cwd, source), or None. Answer fast:
                             all plugins together get 1.5s, a late one is skipped for that start.
    grovy.after_edit(fn, glob="*")      fn(session, info) right after Claude or a subagent edits a file whose absolute
                             path matches glob (fnmatch, * crosses /: "*.md" is any markdown file; info: path, tool,
                             cwd, agent_id). Text it returns is handed to Claude before its
                             next step, e.g. to point out a mistake. All plugins together get 1s; Claude waits.
    grovy.route(method, path, fn)   GET or POST /api/x/PLUGIN/PATH, behind grovy's login; fn(request) returns
                             something JSON-able, or (status, value). request.query (GET), request.body (POST).
                             Call it with grovy.api() from custom.js (a GET needs its X-Grovy header, so a link
                             can't trigger it); keep GET routes read-only all the same. 30s deadline.
    grovy.sessions(), grovy.session(name)   the session list / one session's detail (copies)
    grovy.send(session, text)               send a message, with the phone's rules (never forced)
    grovy.log(text)                          a line in grovy's log (journalctl --user -u grovy)

The on() callbacks run one at a time on a thread of their own; each route, context() and after_edit() call on a
thread of its own (they can overlap: keep shared state under a lock). on() and route()
only work inside setup(). If setup() fails or takes over 10s, the plugin is skipped; if a callback fails, the error is
logged and grovy carries on. GROVY_PLUGINS=0 turns plugins off. grovy's built-in plugins (plugins/ in the grovy
folder) work the same way: a file of yours with the same name replaces one, and an empty NAME.off turns it off.

To show the totals on the phone, add to ~/.config/grovy/custom.js:

    grovy.menu("Working time", async () => {
      const t = await grovy.api("/api/x/worktime/totals");
      grovy.say(Object.entries(t).map(([s, sec]) => `${s}: ${Math.round(sec / 60)} min`).join("\\n") || "Nothing yet.");
    }, {where: "both"});
"""
import threading
import time

lock = threading.Lock()   # callbacks and routes run on different threads
since, total = {}, {}


def setup(grovy):
    @grovy.on("state")
    def changed(session, old, new):
        with lock:
            if old == "working" and session in since:
                total[session] = total.get(session, 0) + time.time() - since.pop(session)
            if new == "working":
                since[session] = time.time()

    @grovy.route("GET", "totals")
    def totals(request):
        with lock:
            now = time.time()
            return {s: round(total.get(s, 0) + (now - since[s] if s in since else 0)) for s in set(total) | set(since)}
