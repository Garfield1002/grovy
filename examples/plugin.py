"""Example grovy plugin: copy to ~/.config/grovy/plugins/worktime.py and restart grovy
(`systemctl --user restart grovy`). It counts how long each session has spent working since grovy started, and
serves the totals at /api/x/worktime/totals.

A plugin runs inside the daemon. It defines setup(grovy) and uses only `grovy` (grovy's own plugins, in plugins/ in the
grovy folder, are written the same way: read plugins/git.py for a longer example):

    grovy.on("event", fn)    fn(session, record): each event the conversation records (a copy)
    grovy.on("state", fn)    fn(session, old, new): a session's state changed (idle, working, needs_input, ...)
    grovy.on("notify", fn)   fn(session, kind, text): the moments GROVY_NOTIFY_CMD runs (kind: input, done, error)
    grovy.route(method, path, fn)   GET or POST /api/x/PLUGIN/PATH, behind grovy's login; fn(request) returns
                             something JSON-able, or (status, value). request.query (GET), request.body (POST).
                             Call it with grovy.api() from custom.js (a GET needs its X-Grovy header, so a link
                             can't trigger it); keep GET routes read-only all the same. 30s deadline.
    grovy.command(fn)        fn(cmd) answers `grovy x PLUGIN ...` run inside a session (one handler per plugin): give
                             the session's Claude a verb. cmd.args (the words after the plugin name), cmd.session,
                             cmd.cwd (for a relative path). Return the text to print, or (status, text). 30s deadline.
    grovy.download(path, name=None, session=None)   offer a file on disk to the phone; returns a "/xfile/.." URL to put
                             in a widget or a page. The file is NOT copied: grovy serves it from `path` (absolute),
                             behind its login, always as a download. (grovy's built-in share plugin uses both of these.)
    grovy.sessions(), grovy.session(name)   the session list / one session's detail (copies)
    grovy.send(session, text)               send a message, with the phone's rules (never forced)
    grovy.log(text)                          a line in grovy's log (journalctl --user -u grovy)

The on() callbacks run one at a time on a thread of their own; each route call on a thread of its own. on() and route()
only work inside setup(). If setup() fails or takes over 10s, the plugin is skipped; if a callback fails, the error is
logged and grovy carries on. GROVY_PLUGINS=0 turns plugins off.

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
