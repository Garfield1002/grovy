"""Tab completion for bash: `grovy completion bash` prints a small function that asks `grovy __complete LINE` what
fits at the cursor, so it follows the CLI as it changes. Commands and options come from cli.build_parser() itself;
session names from this computer (registry and daemon) and, after `pc:` or `trunk:`, from that machine (as
`grovy ls` asks it, bounded);
a path argument gets nothing back, and bash then completes file names."""
import argparse
import os
import shlex
import sys

from . import core

MARK = "# grovy tab completion for bash"
SCRIPT = MARK + r""" (grovy completion bash). It asks grovy what fits, so it never goes stale.
_grovy() {
    local IFS=$'\n'
    COMPREPLY=($(GROVY_WORDBREAKS="$COMP_WORDBREAKS" "${COMP_WORDS[0]}" __complete "${COMP_LINE:0:COMP_POINT}" \
        2>/dev/null))
    if [[ ${#COMPREPLY[@]} -eq 1 && ${COMPREPLY[0]} == *: ]]; then
        compopt -o nospace   # pc: is half a name
    fi
}
complete -o default -F _grovy grovy
"""
HIDDEN = {"hook", "statusline"}   # called by Claude Code, not by you
SESSION_ARGS = {("attach", "name"), ("kill", "name"), ("forget", "name"), ("resume", "name"), ("logs", "name"),
                ("rename", "name")}
PEER_TIMEOUT = 1.5


def path():
    """Where bash-completion looks for grovy's completion when you first press tab after `grovy`."""
    base = os.environ.get("BASH_COMPLETION_USER_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "bash-completion")
    return os.path.join(base, "completions", "grovy")


def bash_completion_installed():
    return any(os.path.exists(p) for p in ("/usr/share/bash-completion/bash_completion", "/etc/bash_completion",
                                           "/opt/homebrew/etc/profile.d/bash_completion.sh",
                                           "/usr/local/etc/profile.d/bash_completion.sh"))


def split(line):
    """The words before the cursor, the last being the one under it ("" after a space)."""
    for extra in ("", '"', "'"):   # an unfinished quote
        try:
            words = shlex.split(line + extra)
            break
        except ValueError:
            continue
    else:
        words = line.split()
    if not line or line[-1].isspace():
        words.append("")
    return words


def machines():
    """{name: 'peer'|'trunk'} that can go before ':' here."""
    from . import peers, remote
    try:
        conf, _ = peers.read_config()
    except Exception:
        conf = {}
    out = {n: "peer" for n in conf}
    try:
        if remote.trunk_host():
            out["trunk"] = "trunk"
    except Exception:
        pass
    return out


def local_sessions():
    from . import client, sessions
    names = set()
    try:
        names |= set(sessions.registry())
    except Exception:
        pass
    try:
        names |= set((client.sock_request({"type": "status"}, timeout=0.5) or {}).get("sessions", {}))
    except (OSError, ValueError):
        pass
    return sorted(n for n in names if core.valid_name(n))


def session_names(cur):
    where = machines()
    if ":" in cur:
        m = cur.partition(":")[0]
        from . import remote
        if where.get(m) == "trunk":
            return [r[0] for r in remote.trunk_lines(remote.trunk_host())[0]]
        if where.get(m) != "peer":
            return []
        old = remote.LS_TIMEOUT
        remote.LS_TIMEOUT = PEER_TIMEOUT
        try:
            rows, _ = remote.peer_lines(m)
        finally:
            remote.LS_TIMEOUT = old
        return [r[0] for r in rows]
    return local_sessions() + [m + ":" for m in sorted(where)]


def candidates(parser, words):
    cur, done = words[-1], words[:-1]
    p, cmds, pos, i = parser, [], 0, 0
    while i < len(done):
        w = done[i]
        i += 1
        if w.startswith("-") and len(w) > 1:
            act = p._option_string_actions.get(w.partition("=")[0])
            if act is not None and act.nargs is None and "=" not in w:
                i += 1   # its value
            continue
        pos_actions = [a for a in p._actions if not a.option_strings]
        if pos >= len(pos_actions):
            continue
        a = pos_actions[pos]
        if isinstance(a, argparse._SubParsersAction):
            if w not in a.choices:
                return []
            p, pos = a.choices[w], 0
            cmds.append(w)
        elif a.nargs == argparse.REMAINDER:
            return []   # someone else's arguments (claude's, a plugin's)
        elif a.nargs not in ("*", "+"):
            pos += 1
    if done and done[-1].startswith("-") and "=" not in done[-1]:
        act = p._option_string_actions.get(done[-1])
        if act is not None and act.nargs is None:
            return [str(c) for c in act.choices or ()]
    if cur.startswith("-"):
        return sorted({o for a in p._actions for o in a.option_strings if a.help != argparse.SUPPRESS})
    pos_actions = [a for a in p._actions if not a.option_strings]
    if pos >= len(pos_actions):
        return []
    a = pos_actions[pos]
    if isinstance(a, argparse._SubParsersAction):
        return [c for c in a.choices if c not in HIDDEN]
    if a.choices:
        return [str(c) for c in a.choices]
    top = cmds[0] if cmds else None
    if (top, a.dest) in SESSION_ARGS:
        return session_names(cur)
    if top == "ls" and a.dest == "machine":
        return sorted(machines())
    if top == "new" and a.dest == "name":
        return [m + ":" for m in sorted(machines()) if machines()[m] == "peer"] if cur else []
    if cmds[:2] == ["peer", "rm"]:
        return [m for m, k in machines().items() if k == "peer"]
    return []   # a path, a new name, free text: bash completes file names


def complete(argv):
    """`grovy __complete LINE`: print what fits at the end of LINE, one per line."""
    from . import cli
    words = split(argv[0] if argv else "")
    if len(words) < 2:
        return 0
    cur = words[-1]
    try:
        out = [c for c in candidates(cli.build_parser(), words[1:]) if c.startswith(cur)]
    except Exception:
        return 0   # never a traceback in the middle of your command line
    if ":" in cur and ":" in os.environ.get("GROVY_WORDBREAKS", ":"):
        cut = cur.rindex(":") + 1   # bash replaces only what follows the last ':'
        out = [c[cut:] for c in out]
    sys.stdout.write("".join(c + "\n" for c in out))
    return 0


def cmd_completion(a):
    sys.stdout.write(SCRIPT)
    return 0


def add_parser(sub):
    s = sub.add_parser("completion", help="print the tab completion for your shell: grovy completion bash "
                       "(grovy setup installs it)")
    s.add_argument("shell", choices=("bash",))
    s.set_defaults(fn=cmd_completion)
