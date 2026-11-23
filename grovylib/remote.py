"""The CLI on sessions of another machine: `grovy attach pc:NAME`, `grovy ls`, `grovy kill pc:NAME`, ...

`MACHINE:NAME` (a session name never holds a colon) picks the machine:
  * on a trunk, MACHINE is one of its grafts (peers.json): the command goes straight to that graft's grovy through
    its tunnel socket, with the token from peers.json (the daemon isn't involved). attach is relayed by the graft's
    grovy (grovylib/terminal.py), when it allows it.
  * on a graft, MACHINE is `trunk`: the command runs on the trunk over your usual ssh (`ssh -t TRUNK grovy ...`).
`grovy ls` on a trunk lists its grafts' sessions after its own; `grovy ls trunk` on a graft lists the trunk's.
"""
import http.client
import json
import os
import re
import shlex
import subprocess
import sys

from . import core, install, peers, terminal

COMMANDS = ("attach", "logs", "kill", "resume", "forget", "rename", "new")
FIND_GROVY = 'g="$HOME/.local/bin/grovy"; [ -x "$g" ] || g=grovy; exec "$g" '


def split(target):
    """('MACHINE', 'NAME') for MACHINE:NAME, else (None, target)."""
    if isinstance(target, str) and ":" in target:
        m, _, n = target.partition(":")
        return m, n
    return None, target


def trunk_host():
    g = install.read_record().get("graft")
    return g.get("trunk") if isinstance(g, dict) else None


def resolve(machine):
    """('peer', Peer) or ('trunk', host); dies with what exists otherwise."""
    conf, _ = peers.read_config()
    if machine in conf:
        return "peer", peers.Peer(machine, conf[machine], None)
    if machine == "trunk" and trunk_host():
        return "trunk", trunk_host()
    known = sorted(conf) + (["trunk"] if trunk_host() else [])
    core.die(f"no machine named {machine!r}" + (f" (known: {', '.join(known)})" if known else
                                                " (this computer has no grafts, and isn't grafted onto a trunk)"))


def on_trunk(host, argv, tty=False):
    """Run `grovy ARGV` on the trunk over your usual ssh, taking this terminal over (exec)."""
    if not peers.HOST_RE.fullmatch(host) or host.startswith("-"):
        core.die(f"bad trunk address {host!r} in the setup record")
    cmd = "sh -c " + shlex.quote(FIND_GROVY + " ".join(shlex.quote(a) for a in argv))
    os.execvp("ssh", ["ssh", *(["-t"] if tty else []), *peers.COMMAND_OPTS, "--", host, cmd])


def _call(peer, method, path, body=None):
    try:
        code, obj = peer.call(method, path, body)
    except peers.PeerError as e:
        core.die(terminal.clean(e, line=True))
    if code != 200:
        core.die(f"{peer.name}: {terminal.clean(obj.get('error') or f'HTTP {code}', line=True)}")
    return obj


def run(a):
    try:
        return _run(a)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def _run(a):
    """A COMMANDS command whose session is MACHINE:NAME."""
    machine, name = split(a.name)
    if not core.valid_name(name):
        core.die(f"bad session name {name!r}")
    kind, where = resolve(machine)
    if a.cmd == "rename":
        nm, new = split(a.new)
        if nm not in (None, machine):
            core.die(f"a session can't move between machines ({machine} -> {nm})")
        a.new = new
    if a.cmd == "new" and not a.path.startswith("/"):
        core.die(f"give the folder on {machine} as an absolute path (it is resolved there, not here)")
    if kind == "trunk":
        argv = {"attach": ["attach", name], "logs": ["logs", name, "-n", str(getattr(a, "lines", 200))],
                "kill": ["kill", name], "forget": ["forget", name],
                "resume": ["resume", name, *(["--fresh"] if getattr(a, "fresh", False) else [])],
                "rename": ["rename", name, getattr(a, "new", "")],
                "new": ["new", name, getattr(a, "path", ""), *getattr(a, "claude_args", [])]}[a.cmd]
        return on_trunk(where, argv, tty=a.cmd == "attach")
    peer = where
    q = f"/api/sessions/{name}"
    if a.cmd == "attach":
        try:
            return terminal.attach(peer, name)
        except (RuntimeError, OSError) as e:
            core.die(str(e))
    if a.cmd == "logs":   # the pane's text, cleaned of escape sequences (untrusted: it comes from another machine)
        text = str(_call(peer, "GET", f"{q}/screen?lines={int(a.lines)}").get("text", ""))
        sys.stdout.write(terminal.clean(text))
        return 0
    if a.cmd in ("kill", "forget"):
        _call(peer, "POST", f"{q}/{a.cmd}", {})
        print(f"{'killed' if a.cmd == 'kill' else 'forgot'} {machine}:{name}")
        return 0
    if a.cmd == "resume":
        if a.fresh:
            core.die("--fresh works on the machine itself only (the API resumes the conversation)")
        print(terminal.clean(_call(peer, "POST", f"{q}/resume", {}).get("ok") or f"resumed {machine}:{name}", line=True))
        return 0
    if a.cmd == "rename":
        new = a.new
        if not core.valid_name(new):
            core.die(f"bad session name {a.new!r}")
        _call(peer, "POST", f"{q}/rename", {"name": new})
        print(f"renamed {machine}:{name} -> {machine}:{new}")
        return 0
    if a.cmd == "new":
        if [x for x in a.claude_args if x != "--"]:
            core.die("extra claude arguments work on the machine itself only")
        _call(peer, "POST", "/api/sessions", {"name": name, "path": a.path})
        print(f"started {machine}:{name} in {a.path} on {machine}  (grovy attach {machine}:{name})")
        return 0
    core.die(f"{a.cmd} doesn't work on another machine")


LS_TIMEOUT = 4   # seconds a graft gets to answer `grovy ls` (one that's asleep must not stall it)


def _read_all(r, deadline, limit):
    """A response body, but within an overall deadline: a graft trickling bytes can't hold `grovy ls`."""
    import time
    data = b""
    while len(data) <= limit:
        if time.monotonic() > deadline:
            raise TimeoutError("it took too long to answer")
        chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
        if not chunk:
            return data
        data += chunk
    raise ValueError("its answer is too long")


def trunk_lines(host, run=subprocess.run):
    """Rows for `grovy ls` on a graft: the trunk's own sessions as trunk:NAME, from `grovy ls --here` there over your
    usual ssh (BatchMode: never a password prompt in the middle of a list; LS_TIMEOUT or so in all)."""
    if not peers.HOST_RE.fullmatch(host) or host.startswith("-"):
        return [], [f"trunk: bad address {host!r} in the setup record"]
    script = FIND_GROVY.replace('exec "$g" ', '"$g" ls --here')
    try:
        r = run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={LS_TIMEOUT}", *peers.COMMAND_OPTS, "--", host,
                 "sh -c " + shlex.quote(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=LS_TIMEOUT + 1)
    except (OSError, subprocess.SubprocessError) as e:
        return [], [f"trunk: {terminal.clean(e, line=True) or type(e).__name__} (grovy ls trunk)"]
    if r.returncode:
        err = terminal.clean(((r.stderr or "").strip().splitlines() or [""])[-1], line=True)
        return [], [f"trunk: couldn't list its sessions over ssh: {err[:200] or f'exit {r.returncode}'}"
                     " (grovy ls trunk logs in the usual way)"]
    rows = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or parts[0] == "NAME" or not core.valid_name(parts[0]):
            continue   # the header, "no sessions", or a graft's row (pc:NAME): only the trunk's own
        rows.append((f"trunk:{parts[0]}", terminal.clean(parts[1], line=True)[:12],
                     terminal.clean(parts[2] if len(parts) > 2 else "?", line=True)))
    return rows, []


def peer_lines(only=None):
    """Rows for `grovy ls` on a trunk: each graft's sessions, or why it can't be listed. The grafts are asked at
    once and given LS_TIMEOUT in all, the whole answer included; a graft still talking then is left behind (its
    thread is a daemon thread: Ctrl-C and the end of the command don't wait for it). What they say is cleaned
    before it is printed: it is untrusted."""
    import threading
    import time
    conf, problems = peers.read_config()
    notes = [f"peers.json: {terminal.clean(p, line=True)}" for p in problems]
    names = [n for n in sorted(conf) if only in (None, n)]
    deadline = time.monotonic() + LS_TIMEOUT
    got = {}

    def one(name):
        try:
            p = peers.Peer(name, conf[name], None)
            c, r = p.open("GET", "/api/sessions", timeout=LS_TIMEOUT)
            try:
                obj = json.loads(_read_all(r, deadline, peers.ANSWER_MAX))
            finally:
                c.close()
            if r.status != 200 or not isinstance(obj, dict) or not isinstance(obj.get("sessions"), list):
                err = obj.get("error") if isinstance(obj, dict) else None
                got[name] = ([], [f"{name}: {terminal.clean(err or f'HTTP {r.status}', line=True)}"])
                return
            got[name] = ([(f"{name}:{s['name']}", terminal.clean(s.get("state", "unknown"), line=True)[:12],
                           terminal.clean(s.get("cwd", "?"), line=True))
                          for s in obj["sessions"] if isinstance(s, dict) and core.valid_name(s.get("name"))], [])
        except Exception as e:   # whatever one graft does, the others' rows stay
            got[name] = ([], [f"{name}: {terminal.clean(e, line=True) or type(e).__name__}"])
    threads = [threading.Thread(target=one, args=(n,), daemon=True) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0, deadline - time.monotonic()) + 0.5)
    rows = []
    for n in names:
        r, nn = got.get(n, ([], [f"{n}: it took too long to answer"]))
        rows += r
        notes += nn
    return rows, notes
