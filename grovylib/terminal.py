"""`grovy attach pc:NAME`: a terminal on another machine's session, relayed by its grovy over the peer tunnel.

The graft's grovyd answers `POST /api/sessions/NAME/terminal` (token, `X-Grovy: 1`, `Upgrade: grovy-terminal`; a
browser can't send that header) with `101 Switching Protocols` when `GROVY_TERMINAL=on` (grovy setup asks, for a
graft), then runs `tmux attach-session` for that session in a pseudo-terminal and relays it:
  * graft -> trunk: the terminal's bytes, as they come;
  * trunk -> graft: frames of 1 type byte + 2 length bytes (big endian) + payload: type 0 is keyboard input,
    type 1 a window size (rows, cols: 2 bytes each).
Either side closing ends it; detaching from tmux (C-b d) or the session ending closes it from the graft's side. On
the trunk, `~.` typed at the start of a line ends it from there (as in ssh), whatever the graft does.

It is a full terminal: tmux can run commands (`:run-shell`, new windows), so whoever holds this grovy's token can
run anything as you here. That is why it is off unless this computer's grovy is told otherwise.
"""
import json
import os
import re
import select
import signal
import struct
import sys
import time

from . import core, tmuxctl

UPGRADE = "grovy-terminal"
TERM_RE = re.compile(r"[A-Za-z0-9._+-]{1,40}")
DATA, SIZE = 0, 1
TMUX = ["tmux"]   # the tmux command (tests point it at a private server)
FALLBACK_TERM = "xterm-256color"
# What a program (tmux) turns on and must turn off; sent when a link drops before it could: alternate screen, mouse
# modes, bracketed paste, focus events off; cursor shown; attributes reset.
RESET = b"\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?1004l\x1b[?25h\x1b[0m"


def enabled():
    return core.env("TERMINAL", "off") == "on"


def frame(kind, payload):
    return struct.pack(">BH", kind, len(payload)) + payload


def size_frame(rows, cols):
    return frame(SIZE, struct.pack(">HH", rows, cols))


def has_terminfo(term):
    """Whether this machine can drive `term` (tmux refuses a TERM it has no terminfo entry for)."""
    dirs = [os.environ.get("TERMINFO"), os.path.expanduser("~/.terminfo"),
            *(os.environ.get("TERMINFO_DIRS") or "").split(":"),
            "/etc/terminfo", "/lib/terminfo", "/usr/share/terminfo", "/usr/lib/terminfo", "/usr/local/share/terminfo"]
    if not term[:1].isalnum():   # "." or "..": a folder, not an entry
        return False
    for d in filter(None, dirs):
        for sub in (term[0], f"{ord(term[0]):x}"):   # Linux uses the first letter, macOS its hex code
            if os.path.isfile(os.path.join(d, sub, term)):
                return True
    return False


def pick_term(term):
    return term if term and TERM_RE.fullmatch(term) and has_terminfo(term) else FALLBACK_TERM


def _set_size(fd, rows, cols):
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _pending(sock):
    return sock.pending() if hasattr(sock, "pending") else 0   # TLS keeps decrypted bytes select() can't see


def serve(sock, name, rows=24, cols=80, term=FALLBACK_TERM):
    """The graft's side, on the request's socket after the 101: attach to the session in a pty and relay."""
    import pty
    argv = [*TMUX, "attach-session", "-t", "=" + tmuxctl.tmux_name(name)]
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["TERM"] = pick_term(term)
    pid, fd = pty.fork()   # the child only execs: everything it needs was built above, before the fork
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    try:
        _set_size(fd, rows, cols)
        os.set_blocking(fd, False)   # a tmux client that stops reading must not block this thread forever
        sock.settimeout(None)
        buf = b""
        while True:
            ready = [sock] if _pending(sock) else select.select([sock, fd], [], [], 60)[0]
            if fd in ready:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    data = None
                except OSError:   # EIO: the child closed the terminal (detached, or the session ended)
                    return
                if data is None:
                    continue
                if not data:
                    return
                sock.sendall(data)
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return
                buf += data
                while len(buf) >= 3:
                    kind, n = struct.unpack(">BH", buf[:3])
                    if len(buf) < 3 + n:
                        break
                    payload, buf = buf[3:3 + n], buf[3 + n:]
                    if kind == DATA:
                        while payload:
                            try:
                                payload = payload[os.write(fd, payload):]
                            except BlockingIOError:   # tmux isn't reading: wait a little, then give up
                                if not select.select([], [fd], [], 10)[1]:
                                    return
                    elif kind == SIZE and n == 4:
                        r, c = struct.unpack(">HH", payload)
                        if 1 <= r <= 1000 and 1 <= c <= 1000:
                            _set_size(fd, r, c)
                            os.kill(pid, signal.SIGWINCH)
    finally:
        os.close(fd)   # first: a tmux client stuck on its server gets EIO/SIGHUP from the terminal going away
        _reap(pid)


def _reap(pid, wait=3.0):
    """End the attach child: SIGHUP, then SIGKILL if it hasn't gone after `wait` seconds (a hung tmux server must
    not keep this thread, its connection slot and the pty forever)."""
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    return
            except ChildProcessError:
                return
            time.sleep(0.05)


def clean(text, line=False):
    """Text from another machine made safe to print: no escape sequences, other control characters or lone
    surrogates (print would fail on them); `line`: one line, so no newline or tab either (a field of `grovy ls`
    must not start a made-up row)."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]" if line else r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]",
                  "?", str(text))


def _request(peer, name, rows, cols, term):
    """Open the terminal: (connection, socket, first bytes of the stream). RuntimeError with a readable reason."""
    from . import peers
    c = peer.connection(timeout=20)
    try:
        c.connect()
        sock = c.sock
        sock.sendall((f"POST /api/sessions/{name}/terminal HTTP/1.1\r\nHost: localhost\r\n"
                      f"Authorization: Bearer {peer.conf['token']}\r\nX-Grovy: 1\r\nConnection: Upgrade\r\n"
                      f"Upgrade: {UPGRADE}\r\nX-Term: {term}\r\nX-Rows: {rows}\r\nX-Cols: {cols}\r\n"
                      "Content-Length: 0\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            data = sock.recv(4096)
            if not data:
                raise RuntimeError(f"{peer.name} closed the connection")
            resp += data
            if len(resp) > 65536:
                raise RuntimeError(f"{peer.name} sent an unexpected answer")
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
            want = min(int(m.group(1)), 65536) if m else 0
            while len(rest) < want:   # the error's body may come after the headers
                data = sock.recv(4096)
                if not data:
                    break
                rest += data
            try:
                err = json.loads(rest[:want or None]).get("error")
            except (ValueError, AttributeError):
                err = None
            raise RuntimeError(f"{peer.name}: {clean(err or status)}")
    except OSError as e:
        c.close()
        raise RuntimeError(f"{peer.name}: {peers.reason(e, peer.name)}") from None
    except BaseException:
        c.close()
        raise
    return c, sock, rest


def attach(peer, name, stdin=None, stdout=None):
    """The trunk's side: open the terminal on `peer` (a peers.Peer) and relay this terminal to it until it ends.
    Returns an exit code; raises RuntimeError with a readable reason when it can't start."""
    import termios
    import tty
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if not stdin.isatty():
        raise RuntimeError("grovy attach needs a terminal")
    fd = stdin.fileno()

    def size():
        try:
            s = os.get_terminal_size(fd)   # the terminal itself, not $COLUMNS/$LINES or a redirected stdout
            return s.lines, s.columns
        except OSError:
            return 24, 80
    term = os.environ.get("TERM", FALLBACK_TERM)
    term = term if TERM_RE.fullmatch(term) else FALLBACK_TERM
    c, sock, rest = _request(peer, name, *size(), term)
    sock.settimeout(None)
    out = stdout.buffer if hasattr(stdout, "buffer") else stdout
    if rest:
        out.write(rest)
        out.flush()
    old = termios.tcgetattr(fd)
    resized, why = [], None

    def on_term(*_):
        raise KeyboardInterrupt
    prev_winch = signal.signal(signal.SIGWINCH, lambda *_: resized.append(1))
    prev_term = signal.signal(signal.SIGTERM, on_term)   # `kill` must still put the terminal back
    line_start, tilde = True, False   # for ~. (ssh's escape): only right after Enter
    try:
        tty.setraw(fd)
        while True:
            if resized:
                resized.clear()
                sock.sendall(size_frame(*size()))
            try:
                ready = [sock] if _pending(sock) else select.select([sock, fd], [], [], 1)[0]
            except InterruptedError:
                continue
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return 0   # the graft closed it: detached, or the session ended (tmux has cleaned up)
                out.write(data)
                out.flush()
            if fd in ready:
                data = os.read(fd, 4096)
                if not data:
                    return 0
                send = bytearray()
                for b in data:
                    if tilde:
                        tilde = False
                        if b == ord("."):
                            if send:   # what was typed before it still goes (as ssh does)
                                sock.sendall(frame(DATA, bytes(send)))
                            why = "closed with ~."
                            return 0
                        send += b"~"
                        if b == ord("~"):   # ~~ sends one ~
                            line_start = False
                            continue
                    if line_start and b == ord("~"):
                        tilde = True
                        continue
                    send.append(b)
                    line_start = b in (13, 10)
                if send:
                    sock.sendall(frame(DATA, bytes(send)))
    except KeyboardInterrupt:
        why = "stopped"
        return 130
    except OSError as e:
        why = f"the link to {peer.name} dropped ({e})"
        return 1
    finally:
        if why:   # an end tmux didn't get to clean up after: put the terminal back as it was
            out.write(RESET)
            out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        signal.signal(signal.SIGWINCH, prev_winch)
        signal.signal(signal.SIGTERM, prev_term)
        c.close()
        if why:
            print(f"\r\ngrovy attach: {why}", file=sys.stderr)
