"""grovy's built-in file-sharing plugin: a session's Claude can hand the user a file to download on the phone.

    grovy x share FILE [--caption TEXT]

prints a ```widget block (a "file" card) for Claude to put in its reply; tapping it downloads the file. The file is
NOT copied: grovy serves it straight from its path on disk (grovy.download), behind grovy's login, always as an
attachment and never rendered (so an arbitrary file can't run as a page on grovy's origin). The offer points at a
path, so the file must still be there when the user taps; a session's own files are the point (a report, a CSV, a
zip, a log it just made). A plugin of your own named share.py (~/.config/grovy/plugins/) replaces this one.
"""
import json
import os
import stat

NAME_MAX = 120
CAPTION_MAX = 300


def setup(grovy):
    @grovy.command
    def share(cmd):
        file, caption, args = None, None, list(cmd.args)
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--caption", "-c"):
                caption = args[i + 1] if i + 1 < len(args) else ""
                i += 2
            elif a == "--" and file is None and i + 1 < len(args):
                file = args[i + 1]   # everything after -- is the file name, even if it starts with -
                i += 2
            elif file is None and not a.startswith("-"):
                file = a
                i += 1
            else:
                return 2, f"share: unexpected argument {a!r}\nusage: grovy x share FILE [--caption TEXT]"
        if not file:
            return 2, "usage: grovy x share FILE [--caption TEXT]"
        base = cmd.cwd if cmd.cwd and os.path.isabs(cmd.cwd) else os.getcwd()
        path = os.path.abspath(file if os.path.isabs(file) else os.path.join(base, file))
        try:
            st = os.stat(path)
        except OSError as e:
            return 1, f"share: can't read {file}: {e}"
        if not stat.S_ISREG(st.st_mode):
            return 2, f"share: not a regular file: {file}"
        name = os.path.basename(path)
        url = grovy.download(path, name=name, session=cmd.session)
        spec = {"type": "file", "name": name[:NAME_MAX], "size": st.st_size, "url": url}
        if caption:
            spec["caption"] = caption[:CAPTION_MAX]
        return "```widget\n" + json.dumps(spec, ensure_ascii=False) + "\n```"
