#!/usr/bin/env python3
"""grovy - run Claude Code sessions in tmux and use them from your phone.

This file is two things:
  * the `grovy` command (the code is in grovylib/; `grovylib/__init__.py` has a map of it), and
  * the library scripts import inside a grovy session: `import grovy`, then grovy.track, progress, note, log
    and show (grovylib/scriptlib.py). It must stay cheap to import and never raise or block.

Claude Code runs `grovy.py hook NAME` for every hook event of a session (and `grovy.py statusline NAME`):
those go straight to grovylib/hook.py, so a mistake in the rest of the CLI can't take the hooks down.
"""
import os
import sys

from grovylib.scriptlib import Live, Log, Progress, active, log, note, progress, show, track  # noqa: F401

__all__ = ["active", "track", "progress", "log", "note", "show", "Live", "Progress", "Log", "main"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] in ("hook", "statusline") and not argv[1].startswith("-"):
        from grovylib import hook
        return (hook.cmd_hook if argv[0] == "hook" else hook.cmd_statusline)(argv[1])
    from grovylib import cli
    return cli.main(argv)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
