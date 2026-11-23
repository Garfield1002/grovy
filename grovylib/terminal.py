"""`grovy attach pc:NAME`: a terminal on another machine's session, relayed by its grovy over the peer tunnel.

The graft's grovyd answers `POST /api/sessions/NAME/terminal` (token, `X-Grovy: 1`, `Upgrade: grovy-terminal`; a
browser can't send that header) with `101 Switching Protocols` when `GROVY_TERMINAL=on` (grovy setup asks, for a
graft), then runs `tmux attach-session` for that session in a pseudo-terminal and relays it:
  * graft -> trunk: the terminal's bytes, as they come;
  * trunk -> graft: frames of 1 type byte + 2 length bytes (big endian) + payload: type 0 is keyboard input,
    type 1 a window size (rows, cols: 2 bytes each).
Either side closing ends it; detaching from tmux (C-b d) or the session ending closes it from the graft's side.

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

from . import core, tmuxctl

UPGRADE = "grovy-terminal"
TERM_RE = re.compile(r"[A-Za-z0-9._+-]{1,40}")
DATA, SIZE = 0, 1
TMUX = ["tmux"]   # the tmux command (tests point it at a private server)


def enabled():
    return core.env("TERMINAL", "off") == "on"


def frame(kind, payload):
    return struct.pack(">BH", kind, len(payload)) + payload


def size_frame(rows, cols):
    return frame(SIZE, struct.pack(">HH", rows, cols))


def _set_size(fd, rows, cols):
    import fcntl
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _pending(sock):
    return sock.pending() if hasattr(sock, "pending") else 0   # TLS keeps decrypted bytes select() can't see


def serve(sock, name, rows=24, cols=80, term="xterm-256color"):
    """The graft's side, on the request's socket after the 101: attach to the session in a pty and relay."""
    import pty
    pid, fd = pty.fork()
    if pid == 0:   # the child: only exec (no Python state shared with the daemon is touched)
        try:
            os.environ["TERM"] = term
            os.environ.pop("TMUX", None)
            os.execvp(TMUX[0], [*TMUX, "attach-session", "-t", "=" + tmuxctl.tmux_name(name)])
        finally:
            os._exit(127)
    try:
        _set_size(fd, rows, cols)
        sock.settimeout(None)
        buf = b""
        while True:
            ready = [sock] if _pending(sock) else select.select([sock, fd], [], [], 60)[0]
            if fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:   # EIO: the child closed the terminal (detached, or the session ended)
                    return
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
                        os.write(fd, payload)
                    elif kind == SIZE and n == 4:
                        r, c = struct.unpack(">HH", payload)
                        if 1 <= r <= 1000 and 1 <= c <= 1000:
                            _set_size(fd, r, c)
                            os.kill(pid, signal.SIGWINCH)
                if len(buf) > 1 << 20:   # a client that never finishes a frame
                    return
    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)


def attach(peer, name, stdin=None, stdout=None):
    """The trunk's side: open the terminal on `peer` (a peers.Peer) and relay this terminal to it until it ends.
    Returns an exit code; raises RuntimeError with a readable reason when the graft refuses."""
    import shutil
    import termios
    import tty
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if not stdin.isatty():
        raise RuntimeError("grovy attach needs a terminal")
    size = shutil.get_terminal_size((80, 24))
    term = os.environ.get("TERM", "xterm-256color")
    term = term if TERM_RE.fullmatch(term) else "xterm-256color"
    c = peer.connection(timeout=20)
    c.connect()
    sock = c.sock
    head = (f"POST /api/sessions/{name}/terminal HTTP/1.1\r\nHost: localhost\r\n"
            f"Authorization: Bearer {peer.conf['token']}\r\nX-Grovy: 1\r\nConnection: Upgrade\r\nUpgrade: {UPGRADE}\r\n"
            f"X-Term: {term}\r\nX-Rows: {size.lines}\r\nX-Cols: {size.columns}\r\nContent-Length: 0\r\n\r\n")
    sock.sendall(head.encode())
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
        c.close()
        try:
            err = json.loads(rest[:want or None]).get("error")
        except (ValueError, AttributeError):
            err = None
        raise RuntimeError(f"{peer.name}: {err or status}")
    sock.settimeout(None)
    out = stdout.buffer if hasattr(stdout, "buffer") else stdout
    if rest:
        out.write(rest)
        out.flush()
    fd = stdin.fileno()
    old = termios.tcgetattr(fd)
    resized = []
    prev = signal.signal(signal.SIGWINCH, lambda *_: resized.append(1))
    try:
        tty.setraw(fd)
        while True:
            if resized:
                resized.clear()
                s = shutil.get_terminal_size((80, 24))
                sock.sendall(size_frame(s.lines, s.columns))
            try:
                ready = [sock] if _pending(sock) else select.select([sock, fd], [], [], 1)[0]
            except InterruptedError:
                continue
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    return 0
                out.write(data)
                out.flush()
            if fd in ready:
                data = os.read(fd, 4096)
                if not data:
                    return 0
                sock.sendall(frame(DATA, data))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        signal.signal(signal.SIGWINCH, prev)
        c.close()
