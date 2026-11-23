"""The `grovy` command line."""
import argparse
import json
import os
import re
import shutil
import sys
import threading
import time

from . import cleanup, client, completion, core, hook, install, projects, remote, scriptlib, sessions, setupflow, tmuxctl, tui, update


# The STATE column on a terminal: what it means for you, at a glance. Ready (green), busy (cyan), wants you (bold
# yellow), broken (bold red), nothing known or nothing running (dim). Anything else, e.g. from a newer grovy on
# another machine, is shown plain.
STATE_LOOK = {"idle": ("●", "green"), "working": ("●", "cyan"), "starting": ("◌", "cyan"),
              "needs_input": ("▲", "yellow", "bold"), "error": ("✗", "red", "bold"),
              "unknown": ("?", "dim"), "running?": ("?", "dim"), "exited": ("○", "dim")}


def _state_cell(st):
    look = STATE_LOOK.get(st)
    label = st.replace("_", " ")
    return (f"{look[0]} {label}", *look[1:]) if look else f"  {label}"


def _name_cell(name):
    machine, short = remote.split(name)
    return [(machine + ":", "dim"), short] if machine else name


def _ls_cells(rows, local=False):
    """`grovy ls` rows (NAME, STATE, PATH) as table cells; this machine's paths with ~ for the home folder."""
    return [[_name_cell(n), _state_cell(st), tui.short(p) if local else p] for n, st, p in rows]


def _ls_table(rows, header, widths=(), local=False):
    return tui.table(_ls_cells(rows, local), header=header, widths=widths, cut_start=(2,))


def _ls_more(shown, mine, rows, head):
    """Other machines' rows, after this machine's `shown` lines are on the screen. When they need wider columns,
    the whole table is drawn again in place so it stays one table, but only if what was shown is surely still on
    the screen and took one line each (else they follow as a block of their own, lined up as far as they can be)."""
    lines, _ = tui.table(_ls_cells(mine, True) + _ls_cells(rows), header=head, cut_start=(2,))
    if lines[:len(shown)] == shown:
        return print("\n".join(lines[len(shown):]))
    size = shutil.get_terminal_size((80, 24))
    if len(shown) < size.lines - 1 and all(tui.cell_width(x) < size.columns for x in shown):
        sys.stdout.write(f"\x1b[{len(shown)}A\x1b[J")
        return print("\n".join(lines))
    print("\n".join(_ls_table(rows, None, tui.table(_ls_cells(mine, True), header=head)[1])[0]))


def _stdin_tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def cmd_ls(a):
    # On a terminal (and without NO_COLOR or --plain): a coloured table. Anywhere else the output is the plain
    # whitespace columns it always was, byte for byte: remote.trunk_lines parses `grovy ls --here` from a graft,
    # and scripts may too.
    fancy = tui.color_on(sys.stdout) and not getattr(a, "plain", False)
    head = ["NAME", "STATE", "PATH"]
    machine = getattr(a, "machine", None)
    if machine:   # another machine's sessions only
        kind, where = remote.resolve(machine.rstrip(":"))
        if kind == "trunk":   # with a terminal at both ends, the trunk's grovy gets one too (ssh -t): its colours
            return remote.on_trunk(where, ["ls"], tty=fancy and _stdin_tty())
        rows, notes = remote.peer_lines(machine.rstrip(":"))
        if fancy:
            print("\n".join(_ls_table(rows, head)[0] if rows else [tui.paint("no sessions", "dim")]))
        for r in rows if not fancy else ():
            print(f"{r[0]:<16} {r[1]:<12} {r[2]}")
        for n in notes:
            tui.caution(n)
        return 0
    reg, live = sessions.registry(), tmuxctl.tmux_managed()
    try:
        status = (client.sock_request({"type": "status"}) or {}).get("sessions", {})
    except (OSError, ValueError):
        status = None
    names = sorted(set(reg) | set(live))
    got = {}

    def others():
        rows, notes = remote.peer_lines()
        host = remote.trunk_host()
        if host:
            r, n = remote.trunk_lines(host)
            rows, notes = rows + r, notes + n
        got.update(rows=rows, notes=notes)
        completion.remember([r[0] for r in rows])   # tab's names, fresh for free
    asking = threading.Thread(target=others, daemon=True)
    if not getattr(a, "here", False):
        asking.start()   # a trunk's grafts (a graft's trunk), asked while this machine's own sessions are printed
    mine = []
    for n in names:
        if n not in live:
            st = "exited"
        elif status is None:
            st = "running?"  # daemon down: tmux says alive, Claude state unknown
        else:
            st = status.get(n, {}).get("state", "unknown")
        mine.append((n, st, reg.get(n, {}).get("cwd", "?")))
    shown = []
    if fancy and mine:   # printed at once: other machines' rows follow when they answer (_ls_more)
        shown = _ls_table(mine, head, local=True)[0]
        print("\n".join(shown))
    elif mine:
        print(f"{'NAME':<16} {'STATE':<12} PATH")
    for n, st, cwd in mine if not fancy else ():
        print(f"{n:<16} {st:<12} {cwd}")
    sys.stdout.flush()
    if asking.is_alive():
        asking.join(remote.LS_TIMEOUT + 2)   # peer_lines has its own deadline; never wait past it
    rows, notes = got.get("rows", []), got.get("notes", [])
    if fancy and rows:
        _ls_more(shown, mine, rows, head) if mine else print("\n".join(_ls_table(rows, head)[0]))
    elif rows and not names:
        print(f"{'NAME':<16} {'STATE':<12} PATH")
    for r in rows if not fancy else ():
        print(f"{r[0]:<16} {r[1]:<12} {r[2]}")
    if not names and not rows:
        print(tui.paint("no sessions", "dim") if fancy else "no sessions")
    for n in notes:
        tui.caution(n)
    if status is None:
        tui.caution("(grovyd not reachable: state shown from tmux only)")
    return 0


def cmd_new(a):
    try:
        extra = [x for x in a.claude_args if x != "--"][:20]
        sessions.start_session(a.name, a.path, extra)
        sessions.check_started(a.name, a.path, extra)
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    tui.done(f"started {a.name} in tmux session {tmuxctl.tmux_name(a.name)}", f"  (grovy attach {a.name})")
    if not projects.folder_trusted(a.path):
        tui.caution(f"note: Claude hasn't been trusted in this folder yet, so it is showing the workspace-trust dialog\n"
                    f"and grovy sees nothing until you accept it once: grovy attach {a.name}")
    return 0


def cmd_resume(a):
    try:
        tui.done(sessions.resume_session(a.name, a.fresh))
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    return 0


def cmd_attach(a):
    if not tmuxctl.tmux_alive(a.name):
        core.die(f"no live managed session {a.name!r}" +
                 (f" here (the trunk's: grovy attach trunk:{a.name})" if remote.trunk_host() else ""))
    verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
    os.execvp("tmux", ["tmux", verb, "-t", "=" + tmuxctl.tmux_name(a.name)])


def cmd_kill(a):
    try:
        sessions.kill_session(a.name)
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    tui.done(f"killed {a.name}")
    return 0


def cmd_rename(a):
    try:
        reply = client.sock_request({"type": "rename", "old": a.name, "new": a.new})
        if reply and reply.get("error"):
            core.die(reply["error"])
    except (OSError, ValueError):  # daemon not running: rename on our own
        try:
            sessions.rename_session(a.name, a.new)
        except (ValueError, RuntimeError) as e:
            core.die(str(e))
    tui.done(f"renamed {a.name} -> {a.new}")
    return 0


def cmd_forget(a):
    if tmuxctl.tmux_alive(a.name):
        core.die(f"{a.name} is running; kill it first")
    with sessions.registry_locked() as reg:
        if reg.pop(a.name, None) is None:
            core.die(f"unknown session {a.name!r}")
    client.notify_daemon({"type": "refresh"})
    if tui.color_on(sys.stdout):   # silent elsewhere, as it always was
        tui.done(f"forgot {a.name}")
    return 0


def cmd_logs(a):
    try:
        sys.stdout.write(tmuxctl.capture(a.name, a.lines))
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    return 0


def cmd_serve(a):
    import grovyd
    return grovyd.serve(a.bind, a.port)


def _page_error(msg):
    print(tui.paint("grovy page:", "red", "bold", stream=sys.stderr) + f" {msg}", file=sys.stderr)


def cmd_page(a):
    """Publish an HTML page to the phone (a sandboxed local "artifact"), or list/remove this session's pages."""
    session = a.session or core.env("SESSION")
    if a.op == "ls" and a.all:
        session = None
    elif not session:
        _page_error("no session: run this inside a grovy session or pass --session NAME")
        return 2
    msg = {"type": "page", "op": a.op, "session": session}
    if a.op == "publish":
        try:
            with open(a.file, "rb") as f:
                html = f.read().decode("utf-8", "surrogateescape")
        except OSError as e:
            _page_error(e)
            return 1
        stem = os.path.splitext(os.path.basename(a.file))[0]
        msg.update(html=html, title=a.title, name=a.name or re.sub(r"[^A-Za-z0-9_.-]", "-", stem).lstrip("-._")[:64] or "page")
    elif a.op == "rm":
        msg["name"] = a.name
    try:
        reply = client.sock_request(msg, timeout=30) or {}
    except (OSError, ValueError) as e:
        _page_error(f"daemon unreachable ({e})")
        return 1
    if "error" in reply:
        _page_error(reply["error"])
        return 1
    res = reply.get("ok")
    if a.op == "publish":
        print(f"published {res['name']} (version {res['version']}): it's in the chat on the phone. {res.get('url', '')}")
    elif a.op == "ls" and tui.color_on(sys.stdout):
        rows = [([p["session"]] if a.all else []) + [p["name"], (f"v{p['version']}", "dim"),
                                                     ("shared", "cyan") if p.get("share") else "", p["title"]]
                for p in res]
        print("\n".join(tui.table(rows, header=(["SESSION"] if a.all else []) + ["NAME", "VER", "", "TITLE"])[0]
                        if res else [tui.paint("no pages", "dim")]))
    elif a.op == "ls":
        for p in res:
            print((f"{p['session']:<16} " if a.all else "") + f"{p['name']:<24} v{p['version']:<4} "
                  + ("shared  " if p.get("share") else "        ") + p["title"])
        if not res:
            print("no pages")
    else:
        print(res)
    return 0


def cmd_x(a):
    """Run a plugin's `grovy x PLUGIN ...` command: forward it to the daemon and print what the plugin returns."""
    msg = {"type": "plugin", "plugin": a.plugin, "args": a.args, "session": core.env("SESSION"), "cwd": os.getcwd()}
    try:
        reply = client.sock_request(msg, timeout=40) or {}   # a command gets the plugin route deadline (30s) plus slack
    except (OSError, ValueError) as e:
        print(f"grovy x: daemon unreachable ({e})", file=sys.stderr)
        return 1
    if "error" in reply:
        print(f"grovy x {a.plugin}: {reply['error']}", file=sys.stderr)
        return 1
    code = reply.get("code", 200)
    out = reply.get("out", "")
    if out:
        print(out, file=sys.stdout if code == 200 else sys.stderr)
    return 0 if code == 200 else (code if 1 <= code < 256 else 1)


def _live_cli(key, spec, status="running", message=None):
    """Live widget from the command line: report problems on stderr, but never fail the caller's pipeline."""
    if not scriptlib.active():
        print("grovy: not in a grovy session; nothing shown", file=sys.stderr)
        return 0
    r = scriptlib._live_send(key, spec, status, message)
    if r is None:
        print("grovy: the grovy daemon is unreachable; nothing shown", file=sys.stderr)
    return 0


def _status(a):
    return ("failed", a.failed) if a.failed is not None else ("done", None) if a.done else ("running", None)


IMAGE_MAGIC = {"png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
               "gif": (b"GIF87a", b"GIF89a"), "webp": (b"RIFF",)}
MAX_IMAGE = 20 * 1024 * 1024


def cmd_image(a):
    """Keep a copy of an image for the phone and print the widget block that shows it in a reply."""
    session = a.session or core.env("SESSION")
    if not session or not core.valid_name(session):
        print("grovy image: no session: run this inside a grovy session or pass --session NAME", file=sys.stderr)
        return 2
    ext = a.file.rsplit(".", 1)[-1].lower() if "." in os.path.basename(a.file) else ""
    if ext not in IMAGE_MAGIC:
        print("grovy image: only .png, .jpg, .gif and .webp files can be shown", file=sys.stderr)
        return 2
    try:
        with open(a.file, "rb") as f:
            data = f.read(MAX_IMAGE + 1)
    except OSError as e:
        print(f"grovy image: {e}", file=sys.stderr)
        return 1
    if len(data) > MAX_IMAGE:
        print(f"grovy image: larger than {MAX_IMAGE // 2**20} MB; make it smaller first", file=sys.stderr)
        return 2
    if not data.startswith(IMAGE_MAGIC[ext]) or (ext == "webp" and data[8:12] != b"WEBP"):
        print(f"grovy image: {a.file} is not a .{ext} image", file=sys.stderr)
        return 2
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(a.file)).strip("._-")[:80] or "image." + ext
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}-{safe}"
    d = os.path.join(core.state_dir(), "images", session)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(d, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    spec = {"type": "image", "src": f"{session}/{name}"}
    if a.caption:
        spec["caption"] = a.caption
    print("```widget\n" + json.dumps(spec, ensure_ascii=False) + "\n```")
    return 0


def cmd_note(a):
    return _live_cli(scriptlib._live_key(), {"type": "note", "text": " ".join(a.text), "tone": a.tone, "title": a.title}, "done")


def cmd_show(a):
    src = sys.stdin.read() if a.json == "-" else a.json
    try:
        spec = json.loads(src)
    except ValueError as e:
        print(f"grovy show: not valid JSON: {e}", file=sys.stderr)
        return 2
    status, message = _status(a)
    return _live_cli(a.key or scriptlib._live_key(), spec, status if a.key else "done", message)


def cmd_progress(a):
    item = {"label": a.label or "", "value": a.value}
    if a.max is None:
        item["indeterminate"] = True
    else:
        item["max"] = a.max
    if a.note:
        item["note"] = a.note
    status, message = _status(a)
    return _live_cli(a.key, {"type": "progress", "title": a.title, "items": [item], "eta": True}, status, message)


def cmd_tail(a):
    """Copy stdin to stdout unchanged, showing its last lines live on the phone."""
    w = scriptlib.Log(a.title, a.lines, a.key)
    out = sys.stdout.buffer
    try:
        for line in iter(sys.stdin.buffer.readline, b""):
            try:
                out.write(line)
                out.flush()
            except BrokenPipeError:   # downstream closed: keep reading so the producer isn't killed by us
                out = open(os.devnull, "wb")
            w.write(line)
    except KeyboardInterrupt:
        w._close("stopped", "interrupted")
        return 130
    w.done()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="grovy", description="Minimal Claude Code session manager")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new", help="start Claude Code in a new tmux session")
    s.add_argument("name"), s.add_argument("path")
    s.add_argument("claude_args", nargs=argparse.REMAINDER, help="extra claude arguments, after --")
    s.set_defaults(fn=cmd_new)
    s = sub.add_parser("ls", help="list sessions (a trunk's grafts' or a graft's trunk's too; `ls MACHINE` for one "
                       "other machine only)")
    s.add_argument("machine", nargs="?", help="pc (a graft, on its trunk) or trunk (on a graft)")
    s.add_argument("--here", action="store_true", help="this computer's sessions only")
    s.add_argument("--plain", action="store_true", help="plain columns, as when the output isn't a terminal")
    s.set_defaults(fn=cmd_ls)
    for cmd, fn, h in (("attach", cmd_attach, "attach to the tmux session"),
                       ("kill", cmd_kill, "terminate a session"),
                       ("forget", cmd_forget, "remove an exited session from the registry")):
        s = sub.add_parser(cmd, help=h)
        s.add_argument("name")
        s.set_defaults(fn=fn)
    s = sub.add_parser("rename", help="rename a session (works while it runs)")
    s.add_argument("name"), s.add_argument("new")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("resume", help="restart an exited session, resuming its Claude conversation")
    s.add_argument("name"), s.add_argument("--fresh", action="store_true", help="start a new conversation")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("logs", help="print recent tmux scrollback")
    s.add_argument("name"), s.add_argument("-n", "--lines", type=int, default=200)
    s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("serve", help="run the daemon")
    s.add_argument("--bind", default=core.env("BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=core.env_int("PORT", 7333))
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("statusline", help="(called by Claude Code) forward status info, print the user's status line")
    s.add_argument("name")
    s.set_defaults(fn=lambda a: hook.cmd_statusline(a.name))
    s = sub.add_parser("hook", help="(called by Claude Code) process one hook event from stdin")
    s.add_argument("name")
    s.set_defaults(fn=lambda a: hook.cmd_hook(a.name))
    setupflow.add_parser(sub)
    s = sub.add_parser("page", help="publish an HTML page to the phone (sandboxed), or list/remove pages")
    ps = s.add_subparsers(dest="op", required=True)
    pp = ps.add_parser("publish", help="publish FILE; publishing the same name again updates it")
    pp.add_argument("file"), pp.add_argument("--title"), pp.add_argument("--name", help="default: the file name")
    pr = ps.add_parser("rm", help="remove a page")
    pr.add_argument("name")
    pl = ps.add_parser("ls", help="list pages")
    pl.add_argument("--all", action="store_true", help="pages of every session")
    for x in (pp, pr, pl):
        x.add_argument("--session", help="default: $GROVY_SESSION")
        x.set_defaults(fn=cmd_page)
    s = sub.add_parser("x", help="run a plugin command: grovy x PLUGIN [args...] (e.g. grovy x share FILE)")
    s.add_argument("plugin"), s.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the plugin")
    s.set_defaults(fn=cmd_x)
    s = sub.add_parser("image", help="keep a copy of an image for the phone; prints the widget block to put in a reply")
    s.add_argument("file"), s.add_argument("--caption"), s.add_argument("--session", help="default: $GROVY_SESSION")
    s.set_defaults(fn=cmd_image)
    s = sub.add_parser("note", help="show a markdown note in this session's phone chat, now")
    s.add_argument("text", nargs="+"), s.add_argument("--title"), s.add_argument("--tone", choices=("ok", "warn", "bad"))
    s.set_defaults(fn=cmd_note)
    s = sub.add_parser("show", help="show a widget (JSON, or - for stdin); with --key, later calls update it")
    s.add_argument("json"), s.add_argument("--key")
    s = [s, sub.add_parser("progress", help="show or update a live progress bar: grovy progress KEY VALUE --max N")]
    s[1].add_argument("key"), s[1].add_argument("value", type=float), s[1].add_argument("--max", type=float)
    s[1].add_argument("--title"), s[1].add_argument("--label"), s[1].add_argument("--note")
    for x, fn in zip(s, (cmd_show, cmd_progress)):
        g = x.add_mutually_exclusive_group()
        g.add_argument("--done", action="store_true", help="mark it finished")
        g.add_argument("--failed", metavar="MESSAGE", help="mark it failed")
        x.set_defaults(fn=fn)
    s = sub.add_parser("tail", help="copy stdin to stdout, showing its last lines live on the phone")
    s.add_argument("--title"), s.add_argument("--lines", type=int, default=40), s.add_argument("--key")
    s.set_defaults(fn=cmd_tail)
    s = sub.add_parser("tls", help="optional HTTPS: make a certificate for the name grovy is opened by")
    ts = s.add_subparsers(dest="op", required=True)
    ti = ts.add_parser("init", help="create (or renew) the certificate; phones install it again each time")
    ti.add_argument("--name", action="append", help="host name or IP address the phone opens grovy by (repeatable; "
                    "default: this machine's full host name)")
    ti.set_defaults(fn=install.cmd_tls)
    cleanup.add_parser(sub)
    update.add_parser(sub)
    sub.add_parser("doctor", help="check dependencies and connectivity").set_defaults(fn=install.cmd_doctor)
    sub.add_parser("url", help="print the web UI link(s), including the login token").set_defaults(fn=install.cmd_url)
    from . import peers   # imported here: hooks never load it
    peers.add_parser(sub)
    completion.add_parser(sub)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__complete"]:   # bash's tab (grovy completion bash)
        return completion.complete(argv[1:])
    a = build_parser().parse_args(argv)
    if a.cmd in remote.COMMANDS and remote.split(getattr(a, "name", None))[0]:
        return remote.run(a)   # MACHINE:NAME: another machine's session
    if a.cmd in remote.COMMANDS and a.cmd != "new" and not here(a.name):
        found, notes = remote.find(a.name)   # a plain name that's only on another machine works as well
        if len(found) == 1:
            print(f"({a.name} is {found[0]})", file=sys.stderr)
            a.name = found[0]
            return remote.run(a)
        if found:
            core.die(f"{a.name!r} is on more than one machine: say which, " + " or ".join(found))
        for n in notes:
            print(n, file=sys.stderr)
    return a.fn(a)


def here(name):
    """A session of this computer's (registered, or running in tmux)."""
    return name in sessions.registry() or tmuxctl.tmux_alive(name)
