"""The CLI on sessions of another machine: `grovy attach pc:NAME`, `grovy ls`, `grovy kill pc:NAME`, ...

`MACHINE:NAME` (a session name never holds a colon) picks the machine:
  * on a trunk, MACHINE is one of its grafts (peers.json): the command goes straight to that graft's grovy through
    its tunnel socket, with the token from peers.json (the daemon isn't involved). attach is relayed by the graft's
    grovy (grovylib/terminal.py), when it allows it.
  * on a graft, MACHINE is `trunk`: the command runs on the trunk over your usual ssh (`ssh -t TRUNK grovy ...`).
`grovy ls` on a trunk lists its grafts' sessions after its own; `grovy ls trunk` on a graft lists the trunk's.
"""
import os
import shlex
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
    os.execvp("ssh", ["ssh", *(["-t"] if tty else []), "--", host, cmd])


def _call(peer, method, path, body=None):
    try:
        code, obj = peer.call(method, path, body)
    except peers.PeerError as e:
        core.die(str(e))
    if code != 200:
        core.die(f"{peer.name}: {obj.get('error') or f'HTTP {code}'}")
    return obj


def run(a):
    """A COMMANDS command whose session is MACHINE:NAME."""
    machine, name = split(a.name)
    if not core.valid_name(name):
        core.die(f"bad session name {name!r}")
    kind, where = resolve(machine)
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
    if a.cmd == "logs":
        sys.stdout.write(_call(peer, "GET", f"{q}/screen?lines={int(a.lines)}").get("text", ""))
        return 0
    if a.cmd in ("kill", "forget"):
        _call(peer, "POST", f"{q}/{a.cmd}", {})
        print(f"{'killed' if a.cmd == 'kill' else 'forgot'} {machine}:{name}")
        return 0
    if a.cmd == "resume":
        if a.fresh:
            core.die("--fresh works on the machine itself only (the API resumes the conversation)")
        print(_call(peer, "POST", f"{q}/resume", {}).get("ok") or f"resumed {machine}:{name}")
        return 0
    if a.cmd == "rename":
        _, new = split(a.new)
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


def peer_lines():
    """Rows for `grovy ls` on a trunk: each graft's sessions, or why it can't be listed."""
    conf, _ = peers.read_config()
    rows, notes = [], []
    for name in sorted(conf):
        p = peers.Peer(name, conf[name], None)
        try:
            code, obj = p.call("GET", "/api/sessions")
        except peers.PeerError as e:
            notes.append(f"{name}: {e}")
            continue
        if code != 200 or not isinstance(obj.get("sessions"), list):
            notes.append(f"{name}: {obj.get('error') or f'HTTP {code}'}")
            continue
        for s in obj["sessions"]:
            if isinstance(s, dict) and core.valid_name(s.get("name")):
                rows.append((f"{name}:{s['name']}", str(s.get("state", "unknown"))[:12], str(s.get("cwd", "?"))))
    return rows, notes
