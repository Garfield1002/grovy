"""tmux: run commands on the session panes, and type into Claude safely."""
import os
import subprocess
import time

from . import core


def tmux(*args, input=None, check=True, timeout=5):
    """Run tmux with an argument array (never a shell). Bounded: a hung tmux server raises instead of
    blocking the daemon, which calls some of these while holding its state lock."""
    try:
        r = subprocess.run(["tmux", *args], input=input, capture_output=True, timeout=timeout,
                           text=isinstance(input, str) or input is None)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tmux {args[0]} timed out after {timeout}s")
    if check and r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        raise RuntimeError(f"tmux {args[0]} failed: {err.strip()}")
    return r


def tmux_name(name):
    return core.TMUX_PREFIX + name


def tmux_managed():
    """{grovy name: pane_id} for live tmux sessions carrying our @grovy marker AND owned by this state dir
    (@grovy_state; sessions predating that tag belong to the default dir). tmux is authoritative."""
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{@grovy}\t#{pane_id}\t#{@grovy_state}", check=False)
    if r.returncode != 0:  # no server running = no sessions
        return {}
    mine = os.path.realpath(core.state_dir())
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1] and parts[0] == tmux_name(parts[1]) and \
                os.path.realpath(parts[3] or core.default_state_dir()) == mine:
            out.setdefault(parts[1], parts[2])
    return out


# systemd-oomd kills a whole cgroup when memory pressure stays high, and each tmux pane is its own cgroup, so it
# killed Claude sessions along with heavy jobs running elsewhere. The daemon moves each session's Claude into its own
# scope marked ManagedOOMPreference=avoid (oomd honours it: the scope and the monitored user@UID.service have the same
# owner), and when a session vanishes it asks the journal whether the OOM killer did it. All of it is best effort.

def pane_pids():
    """{grovy name: pid of the pane's process}. That's Claude itself: tmux execs our argv directly."""
    r = tmux("list-panes", "-a", "-F", "#{session_name}\t#{@grovy}\t#{pane_pid}\t#{@grovy_state}", check=False)
    if r.returncode != 0:
        return {}
    mine, out = os.path.realpath(core.state_dir()), {}
    for parts in (line.split("\t") for line in r.stdout.splitlines()):
        if len(parts) == 4 and parts[1] and parts[0] == tmux_name(parts[1]) and parts[2].isdigit() and \
                os.path.realpath(parts[3] or core.default_state_dir()) == mine:
            out.setdefault(parts[1], int(parts[2]))
    return out


def tmux_alive(name):
    return name in tmux_managed()


def capture(name, lines=200):
    if not tmux_alive(name):
        raise ValueError(f"no live managed session {name!r}")
    return tmux("capture-pane", "-p", "-J", "-S", f"-{int(lines)}", "-t", "=" + tmux_name(name) + ":").stdout


TYPE_MAX = 20000   # longer prompts are pasted (Claude then labels them as pasted content)
TYPE_CHUNK = 64    # chars per send-keys; small bursts are not detected as a paste by Claude Code
TYPE_PAUSE = 0.015


def _own_pane(name):
    panes = tmux_managed()
    if name not in panes:
        raise RuntimeError("session is not running")
    pane = panes[name]
    who = tmux("display-message", "-p", "-t", pane, "#{session_name}\t#{@grovy}", check=False)
    if who.returncode != 0 or who.stdout.strip() != f"{tmux_name(name)}\t{name}":
        raise RuntimeError("pane no longer belongs to session")
    return pane


def typed_mode(text):
    """Typed keystrokes reach Claude as the user's own words; a big paste arrives wrapped as
    <pasted_content>. Leading / or ! would switch the input into command/bash mode when typed,
    so those (and huge prompts) take the literal paste path."""
    return len(text) <= TYPE_MAX and not text.lstrip().startswith(("/", "!"))


def send_key(name, key):
    """Press one key (e.g. Escape) in the session's pane, after verifying the pane is ours."""
    tmux("send-keys", "-t", _own_pane(name), key)


class PasteAborted(RuntimeError):
    """Typing stopped part-way because the session stopped being safe to type into."""


def paste_prompt(name, text, command=False, abort=None):
    """Deliver text to the session's input, then Enter. No shell anywhere.

    command=True types an allowlisted slash command. It always ends in a space before Enter:
    the space closes Claude Code's fuzzy command menu, so Enter can't pick a neighbour.

    The caller decides *whether* it is safe to type. `abort()` (if given) is asked again between chunks
    and right before Enter, and returns a reason to stop: Claude can start a turn on its own while we
    type (a background task finishing), and if that turn opens a permission dialog, the final Enter
    would answer it with its highlighted option. Raises PasteAborted then, and RuntimeError if the
    session is gone or its pane changed owner.
    """
    def check():
        why = abort() if abort else None
        if why:
            raise PasteAborted(why)

    pane = _own_pane(name)
    if command:
        text = text.strip() + " "
    if command or typed_mode(text):
        clean = "".join(c for c in text.replace("\r\n", "\n").replace("\t", "    ")
                        if c == "\n" or c.isprintable() or c == " ")
        for i, line in enumerate(clean.split("\n")):
            if i:
                tmux("send-keys", "-t", pane, "C-j")  # newline without submitting
            for j in range(0, len(line), TYPE_CHUNK):
                # tmux treats an argument ending in ';' as a command separator and drops the ';',
                # so every ';' is sent as a raw byte instead.
                for k, piece in enumerate(line[j:j + TYPE_CHUNK].split(";")):
                    if k:
                        tmux("send-keys", "-t", pane, "-H", "3b")
                    if piece:
                        tmux("send-keys", "-t", pane, "-l", "--", piece)
                time.sleep(TYPE_PAUSE)
                check()
    else:
        buf = f"grovy-{name}-{os.getpid()}-{time.monotonic_ns()}"
        # Strip control characters (ESC especially): "\x1b[201~" would end the bracketed paste early
        # and turn the rest of the text into raw keystrokes.
        safe = "".join(c for c in text.replace("\r\n", "\n") if c in "\n\t" or c.isprintable())
        tmux("load-buffer", "-b", buf, "-", input=safe.encode())
        try:
            _own_pane(name)  # re-verify right before pasting
            check()
        except RuntimeError:
            tmux("delete-buffer", "-b", buf, check=False)
            raise
        tmux("paste-buffer", "-p", "-d", "-r", "-b", buf, "-t", pane)
    time.sleep(0.4)  # let the TUI ingest the input before submitting
    _own_pane(name)
    check()
    tmux("send-keys", "-t", pane, "Enter")
