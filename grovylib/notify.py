"""GROVY_NOTIFY_CMD: tell the user when a session needs them, finished or failed."""
import os
import shlex
import subprocess
import sys
import threading

from . import core


class Notifier:
    """Runs GROVY_NOTIFY_CMD (argv, shlex-split) with the message on stdin. Never blocks callers."""

    def __init__(self, cmd, url=None, min_gap=60):
        self.cmd, self.url, self.min_gap = shlex.split(cmd), url, min_gap
        self.last = {}

    def send(self, name, kind, text):
        key = (name, kind)
        if core.now() - self.last.get(key, 0) < self.min_gap:
            return
        self.last[key] = core.now()
        msg = f"[{name}] {text}" + (f"\n{self.url}#/s/{name}" if self.url else "")
        threading.Thread(target=self._run, args=(msg, kind, name), daemon=True).start()

    def _run(self, msg, kind, name):
        try:
            subprocess.run(self.cmd, input=msg, text=True, timeout=15, capture_output=True,
                           env={**os.environ, "GROVY_NOTIFY_KIND": kind, "GROVY_NOTIFY_SESSION": name,
                                "CCS_NOTIFY_KIND": kind, "CCS_NOTIFY_SESSION": name})
        except (OSError, subprocess.SubprocessError) as e:
            print(f"grovyd: notify failed: {e}", file=sys.stderr)
