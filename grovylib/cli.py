"""The `grovy` command line."""
import argparse
import json
import os
import re
import sys
import time

from . import client, core, hook, install, plugins, projects, scriptlib, sessions, tmuxctl


def cmd_ls(_):
    reg, live = sessions.registry(), tmuxctl.tmux_managed()
    try:
        status = (client.sock_request({"type": "status"}) or {}).get("sessions", {})
    except (OSError, ValueError):
        status = None
    names = sorted(set(reg) | set(live))
    if not names:
        print("no sessions")
        return 0
    print(f"{'NAME':<16} {'STATE':<12} PATH")
    for n in names:
        if n not in live:
            st = "exited"
        elif status is None:
            st = "running?"  # daemon down: tmux says alive, Claude state unknown
        else:
            st = status.get(n, {}).get("state", "unknown")
        print(f"{n:<16} {st:<12} {reg.get(n, {}).get('cwd', '?')}")
    if status is None:
        print("(grovyd not reachable: state shown from tmux only)", file=sys.stderr)
    return 0


def cmd_new(a):
    try:
        extra = [x for x in a.claude_args if x != "--"][:20]
        sessions.start_session(a.name, a.path, extra)
        sessions.check_started(a.name, a.path, extra)
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    print(f"started {a.name} in tmux session {tmuxctl.tmux_name(a.name)}  (grovy attach {a.name})")
    if not projects.folder_trusted(a.path):
        print(f"note: Claude hasn't been trusted in this folder yet, so it is showing the workspace-trust dialog\n"
              f"and grovy sees nothing until you accept it once: grovy attach {a.name}", file=sys.stderr)
    return 0


def cmd_resume(a):
    try:
        print(sessions.resume_session(a.name, a.fresh))
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    return 0


def cmd_attach(a):
    if not tmuxctl.tmux_alive(a.name):
        core.die(f"no live managed session {a.name!r}")
    verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
    os.execvp("tmux", ["tmux", verb, "-t", "=" + tmuxctl.tmux_name(a.name)])


def cmd_kill(a):
    try:
        sessions.kill_session(a.name)
    except (ValueError, RuntimeError) as e:
        core.die(str(e))
    print(f"killed {a.name}")
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
    print(f"renamed {a.name} -> {a.new}")
    return 0


def cmd_forget(a):
    if tmuxctl.tmux_alive(a.name):
        core.die(f"{a.name} is running; kill it first")
    with sessions.registry_locked() as reg:
        if reg.pop(a.name, None) is None:
            core.die(f"unknown session {a.name!r}")
    client.notify_daemon({"type": "refresh"})
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


def cmd_page(a):
    """Publish an HTML page to the phone (a sandboxed local "artifact"), or list/remove this session's pages."""
    session = a.session or core.env("SESSION")
    if a.op == "ls" and a.all:
        session = None
    elif not session:
        print("grovy page: no session: run this inside a grovy session or pass --session NAME", file=sys.stderr)
        return 2
    msg = {"type": "page", "op": a.op, "session": session}
    if a.op == "publish":
        try:
            with open(a.file, "rb") as f:
                html = f.read().decode("utf-8", "surrogateescape")
        except OSError as e:
            print(f"grovy page: {e}", file=sys.stderr)
            return 1
        stem = os.path.splitext(os.path.basename(a.file))[0]
        msg.update(html=html, title=a.title, name=a.name or re.sub(r"[^A-Za-z0-9_.-]", "-", stem).lstrip("-._")[:64] or "page")
    elif a.op == "rm":
        msg["name"] = a.name
    try:
        reply = client.sock_request(msg, timeout=30) or {}
    except (OSError, ValueError) as e:
        print(f"grovy page: daemon unreachable ({e})", file=sys.stderr)
        return 1
    if "error" in reply:
        print(f"grovy page: {reply['error']}", file=sys.stderr)
        return 1
    res = reply.get("ok")
    if a.op == "publish":
        print(f"published {res['name']} (version {res['version']}): it's in the chat on the phone. {res.get('url', '')}")
    elif a.op == "ls":
        for p in res:
            print((f"{p['session']:<16} " if a.all else "") + f"{p['name']:<24} v{p['version']:<4} "
                  + ("shared  " if p.get("share") else "        ") + p["title"])
        if not res:
            print("no pages")
    else:
        print(res)
    return 0


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


def cmd_plugin(name, args):
    """`grovy spec ...`: run a plugin file as a command (the user's copy if they have one)."""
    path = plugins.plugin_file(name)
    if not path:
        print(f"grovy {name}: the {name} plugin is turned off or missing", file=sys.stderr)
        return 1
    os.execv(sys.executable, [sys.executable, path, *args])


PLUGIN_COMMANDS = ("spec",)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in PLUGIN_COMMANDS:   # its own arguments, untouched by argparse
        return cmd_plugin(argv[0], argv[1:])
    p = argparse.ArgumentParser(prog="grovy", description="Minimal Claude Code session manager")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new", help="start Claude Code in a new tmux session")
    s.add_argument("name"), s.add_argument("path")
    s.add_argument("claude_args", nargs=argparse.REMAINDER, help="extra claude arguments, after --")
    s.set_defaults(fn=cmd_new)
    sub.add_parser("ls", help="list sessions").set_defaults(fn=cmd_ls)
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
    s = sub.add_parser("setup", help="one-time install: PATH link, systemd service, phone link")
    s.add_argument("--bind", help="address to listen on (asked if omitted)")
    s.add_argument("--port", type=int, default=core.env_int("PORT", 7333))
    s.add_argument("--no-service", action="store_true", help="don't install the systemd service")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask; use the suggested address")
    s.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    s.set_defaults(fn=install.cmd_setup)
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
    sub.add_parser("spec", help="read and write this repo's specs (grovy spec guide)")   # handled before argparse
    sub.add_parser("doctor", help="check dependencies and connectivity").set_defaults(fn=install.cmd_doctor)
    sub.add_parser("url", help="print the web UI link(s), including the login token").set_defaults(fn=install.cmd_url)
    a = p.parse_args(argv)
    return a.fn(a)
