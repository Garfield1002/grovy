"""Talking to the daemon over its Unix socket."""
import json
import socket

from . import core


def sock_request(msg, timeout=2.0, wait_reply=True):
    """Send one JSON line to grovyd; optionally read one JSON line back. Raises OSError if unreachable."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(core.sock_path())
        s.sendall(json.dumps(msg).encode() + b"\n")
        if not wait_reply:
            return None
        # No half-close: the daemon treats EOF as "this hook process is gone".
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data) if data.strip() else None
    finally:
        s.close()


def notify_daemon(msg):
    try:
        sock_request(msg, timeout=1.0, wait_reply=False)
    except OSError:
        pass
