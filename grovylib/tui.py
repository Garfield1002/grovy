"""A small terminal UI for `grovy setup` and `grovy cleanup`: colours, step lines, arrow-key menus. Stdlib only.

Everything degrades: colours are off when stdout isn't a terminal, `NO_COLOR` is set or `TERM=dumb`; without a
terminal on stdin (a pipe, `-y`), every question returns its default without asking, so a scripted setup takes the
recommended answers. Menus redraw in place with plain ANSI cursor movement (no curses); lines are cut to the
terminal's width so a redraw never miscounts wrapped lines.
"""
import itertools
import os
import re
import select as _select
import shutil
import sys

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def color_on(stream=None):
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def paint(text, *styles, stream=None):
    if not styles or not color_on(stream):
        return text
    return "\x1b[" + ";".join(_CODES[s] for s in styles) + "m" + text + "\x1b[0m"


def visible_len(s):
    return len(_ANSI.sub("", s))


AUTO = False   # -y: take every default even on a terminal


def interactive():
    """A person can answer: both ends are a terminal (and -y wasn't given)."""
    if AUTO:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def width():
    return max(10, shutil.get_terminal_size((80, 24)).columns)


def short(path):
    """A path as people write it: ~/... under the home folder."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home.rstrip("/") + "/") else path


def say(text=""):
    print(text, flush=True)


def ok(text):
    say(f"  {paint('✓', 'green', 'bold')} {text}")


def warn(text):
    say(f"  {paint('⚠', 'yellow', 'bold')} {text}")


def fail(text):
    say(f"  {paint('✗', 'red', 'bold')} {text}")


def note(text):
    say(paint(text, "dim"))


def step(n, total, title):
    say()
    say(paint(f"[{n}/{total}] ", "dim") + paint(title, "bold"))


TREE = [  # drawn for grovy, not copied from anywhere; plain, in the terminal's own colour
    "            ▄▄▄▄▄▄",
    "         ▄██████████▄        ▄▄▄",
    "        ████████████████    ▄█████▄",
    "        ▀██████████████▀    ▀█████▀",
    "           ▀▀▀▐██▌▀▀▀         ▐█▌",
    "              ▐██▌            ▐█▌",
    "      ▁▁▁▁▁▁▁▁▟██▙▁▁▁▁▁▁▁▁▁▁▁▁▟█▙▁▁▁▁▁▁",
]


def tree(side=()):
    """The welcome tree, with optional lines of text beside it (from the second line down)."""
    side = list(side)
    for i, line in enumerate(TREE):
        text = side[i - 1] if 1 <= i <= len(side) else ""
        say(line + (" " * max(42 - len(line), 2) + (paint(text, "bold") if i == 1 else text) if text else ""))


# --- questions ------------------------------------------------------------------------------------------------

def _read_key(fd):
    """One key press from a terminal in cbreak mode: 'up', 'down', 'enter', 'ctrl-c', a digit or a character."""
    ch = os.read(fd, 1)
    if ch == b"\x1b":
        seq = b""
        while len(seq) < 8 and _select.select([fd], [], [], 0.05)[0]:
            seq += os.read(fd, 1)
            if len(seq) >= 2 and 0x40 <= seq[-1] <= 0x7e:   # the final byte of an escape sequence
                break
        if seq[:1] in (b"[", b"O") and seq[-1:] in (b"A", b"B"):   # ↑/↓, with or without modifiers
            return "up" if seq[-1:] == b"A" else "down"
        return "esc"
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b"\x03":
        return "ctrl-c"
    if ch == b"\x04":
        return "ctrl-d"
    return ch.decode(errors="replace")


def _terminal_keys():
    """(key iterator, restore()): the terminal goes to cbreak mode right away, before anything is drawn, so a key
    pressed while the menu appears is neither echoed nor lost."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def keys():
        while True:
            yield _read_key(fd)
    return keys(), lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _cut(s, n):
    """`s` cut to n visible characters (ANSI codes kept, a reset added if anything was cut)."""
    if visible_len(s) <= n:
        return s
    out, seen, i = [], 0, 0
    while i < len(s) and seen < n - 1:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        seen += 1
        i += 1
    return "".join(out) + "…" + ("\x1b[0m" if "\x1b[" in s else "")


def _menu_lines(question, options, cur):
    lines = [paint("? ", "cyan", "bold") + paint(question, "bold")]
    col = max(len(o["label"]) for o in options) + 2   # hints line up in one column
    for i, o in enumerate(options):
        here = i == cur
        mark = paint("❯ ", "cyan", "bold") if here else "  "
        label = paint(o["label"], "cyan", "bold") if here else o["label"]
        hint = " " * (col - len(o["label"])) + paint(o["hint"], "dim") if o.get("hint") else ""
        lines.append(mark + label + hint)
        for d in o.get("detail", ()):
            lines.append("    " + (d if here else paint(d, "dim")))
    lines.append(paint("  ↑/↓ to move, enter to choose", "dim"))
    return lines


def select(question, options, default=0, keys=None, out=None):
    """Ask to pick one of `options` (dicts: label, value, optional hint and detail lines). Returns its value.
    Without a terminal, returns the default's value at once. `keys`/`out` are for tests."""
    options = [o if isinstance(o, dict) else {"label": str(o), "value": o} for o in options]
    if not options:
        raise ValueError("no options")
    cur = max(0, min(default, len(options) - 1))
    if keys is None and not interactive():
        return options[cur]["value"]
    out = out or sys.stdout
    restore = None
    if keys is None:
        keys, restore = _terminal_keys()
    w, drawn, chosen = width() - 1, 0, False
    try:
        for key in itertools.chain([None], keys):   # None: draw it once before the first key
            if key == "up":
                cur = (cur - 1) % len(options)
            elif key == "down":
                cur = (cur + 1) % len(options)
            elif key in ("k", "j"):
                cur = (cur + (1 if key == "j" else -1)) % len(options)
            elif isinstance(key, str) and key.isdigit() and 1 <= int(key) <= len(options):
                cur = int(key) - 1
            elif key == "ctrl-c":
                raise KeyboardInterrupt
            elif key == "ctrl-d":
                raise EOFError
            elif key == "enter":
                chosen = True
                break
            elif key is not None:
                continue
            lines = [_cut(x, w) for x in _menu_lines(question, options, cur)]
            if drawn:
                out.write(f"\x1b[{drawn}A\x1b[J")
            out.write("\n".join(lines) + "\n")
            out.flush()
            drawn = len(lines)
    finally:
        if drawn:   # collapse the menu into one line saying what was chosen
            out.write(f"\x1b[{drawn}A\x1b[J")
            out.write(_cut((paint("✓ ", "green") + question + " " + paint(options[cur]["label"], "cyan")) if chosen
                           else paint("✗ ", "red") + question + " " + paint("(stopped)", "dim"), w) + "\n")
            out.flush()
        if restore:
            restore()
    return options[cur]["value"]


def text(question, default="", check=None, ask=None):
    """Ask for a line of text; enter keeps the default. `check(answer)` returns an error message or None. Without a
    terminal, returns the default. `ask` replaces input() in tests."""
    if ask is None and not interactive():
        return default
    real = ask is None
    ask = ask or input
    tries = 0
    while True:
        shown = f" {paint('(' + str(default) + ')', 'dim')}" if default != "" else ""
        try:
            got = ask(paint("? ", "cyan", "bold") + paint(question, "bold") + shown + " ").strip()
        except EOFError:
            got = ""
        got = got or str(default)
        why = check(got) if check else None
        if not why:
            if real and tries == 0 and color_on():   # like a menu: the question becomes one line with the answer
                sys.stdout.write("\x1b[1A\x1b[J" + paint("✓ ", "green") + question + " " + paint(got, "cyan") + "\n")
                sys.stdout.flush()
            return got
        tries += 1
        warn(why)


def confirm(question, default=True, ask=None):
    if ask is None and not interactive():
        return default
    got = text(question + (" (Y/n)" if default else " (y/N)"), "", ask=ask).lower()
    return default if not got else got in ("y", "yes")
