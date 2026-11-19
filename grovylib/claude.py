"""How a session's Claude is launched: command line, --settings file (hooks, status line), instructions."""
import hashlib
import json
import os
import shlex
import sys

from . import core, projects


# Events the adapter forwards. Anything else is ignored (exit 0, no output).
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PermissionRequest",
               "Notification", "Stop", "StopFailure", "PostToolUse", "SubagentStart", "SubagentStop")
# The ones still forwarded when fired inside a subagent (GROVY_SUBAGENT_HOOKS=1: all): requests waiting on the
# user, and what the phone's list of background work needs. The daemon never takes session state from them.
SUBAGENT_EVENTS = ("PermissionRequest", "Notification", "SubagentStart", "SubagentStop", "PostToolUse")


def claude_cmd():
    return core.env("CLAUDE", "claude")


def grovy_cmd():
    """argv prefix that runs this CLI regardless of PATH or cwd."""
    return [sys.executable, os.path.join(core.HERE, "grovy.py")]


SESSION_ENV_VERSION = 1   # bump when start_session's environment changes (1: PYTHONPATH), so sessions show Update


def auto_mode_hints():
    """Auto mode's classifier can't know that grovy is local and blocked `grovy page publish` as "unsanctioned
    communication". These entries describe it (prose; the classifier reads them) and say that showing work to the
    user through grovy is fine. They cover grovy's own invocation only. "$defaults" keeps every built-in rule."""
    bin_ = GROVY_BIN
    return {
        "environment": ["$defaults",
                        f"**Org-specific CLIs**: grovy (`{bin_}`, also run as `python3 {core.HERE}/grovy.py`, and "
                        "`import grovy` in Python scripts) is the user's own tool that mirrors this Claude Code "
                        "session to the user's own phone. Its `page`, `note`, `progress`, `show` and `tail` "
                        "subcommands, and the Python functions grovy.note/progress/track/log/show, deliver only to "
                        "the user's grovy daemon on this machine over a private Unix socket, and the daemon shows the "
                        "content to the user alone. Nothing leaves the machine unless the user, on the phone, chooses "
                        "to share a page."],
        "allow": ["$defaults",
                  "grovy Display: showing work to the user through grovy (`grovy page publish|ls|rm`, `grovy note`, "
                  "`grovy progress`, `grovy show`, piping output into `grovy tail`, or the grovy Python functions) "
                  "is answering the user, not communication with others: not Unsanctioned Communication, External "
                  "System Writes, Unrequested Artifact Publish or Data Exfiltration. It covers only the grovy "
                  "invocation itself; judge any other command on the same line, including what is piped into "
                  "`grovy tail`, on its own."],
    }


def session_settings(name):
    """What a session started now gets via --settings: hooks, status line, auto-mode hints."""
    cmd = " ".join(_shquote(a) for a in grovy_cmd() + ["hook", name])

    def entry(timeout):
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}]

    hooks = {ev: entry(10) for ev in HOOK_EVENTS}
    # The hook gives up on the daemon at PERMISSION_TIMEOUT+10, well before Claude kills it.
    hooks["PermissionRequest"] = [{"matcher": "*", **entry(core.PERMISSION_TIMEOUT + 30)[0]}]
    hooks["PostToolUse"] = [{"matcher": "*", **entry(10)[0]}]
    # The status line feeds the phone (model, effort, context) and then shows the user's own status line.
    status = {"type": "command", "command": " ".join(_shquote(a) for a in grovy_cmd() + ["statusline", name])}
    own = user_statusline()
    if own and isinstance(own.get("padding"), int):
        status["padding"] = own["padding"]
    return {"hooks": hooks, "statusLine": status, "autoMode": auto_mode_hints(),
            "permissions": {"additionalDirectories": [core.attachments_dir()]}}   # files sent from the phone


def session_fingerprint(name):
    """Identifies what a session started now would run with. Claude reads its settings only at start, so a
    session whose recorded fingerprint differs is out of date (offer ⋯ → Update session)."""
    import hashlib
    blob = {"settings": session_settings(name), "env": SESSION_ENV_VERSION}
    if core.env("CONFIG_DIR"):   # passed to the session's environment (only when set, so default setups don't change)
        blob["config"] = core.env("CONFIG_DIR")
    blob = json.dumps(blob, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def hook_settings(name, file_name=None):
    """Settings file passed to claude via --settings; registers `grovy hook NAME` for our events.
    file_name lets a rename rewrite the file a running Claude already points at."""
    path = os.path.join(core.state_dir(), "hooks", f"{file_name or name}.json")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    core.atomic_write_json(path, session_settings(name))
    return path


def user_statusline(project=None):
    """The status line the user configured (local > project > user settings), unless it is ours.

    `project` must be the session's registered folder, never the cwd from Claude's status JSON: that follows
    Claude's Bash `cd`, so a cloned repo's .claude/settings.json would get its command run without the trust
    dialog (Claude Code itself only applies project settings from the trusted project root)."""
    paths = []
    if project:
        paths += [os.path.join(project, ".claude", "settings.local.json"), os.path.join(project, ".claude", "settings.json")]
    paths.append(os.path.join(projects.claude_home(), "settings.json"))
    for p in paths:
        sl = core.read_json(p, {}).get("statusLine") if isinstance(core.read_json(p, {}), dict) else None
        if isinstance(sl, dict) and sl.get("type") == "command" and isinstance(sl.get("command"), str) \
                and " statusline " not in sl["command"]:
            return sl
    return None


def _shquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def session_pythonpath():
    """So that `import grovy` works in scripts Claude runs (live widgets). Appended, so a project's own
    PYTHONPATH entries win."""
    have = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    return os.pathsep.join(have + ([core.HERE] if core.HERE not in have else []))


WIDGET_GUIDE = """\
grovy: the user may read your final message of each turn on a phone, which renders markdown, LaTeX math \
($..$ inline, $$..$$ on lines of its own) and ```widget blocks (one JSON object each; grovy draws them, no HTML). The terminal shows the raw JSON, so state key facts in \
text too. Use a widget only when it clearly beats text, usually at most one per reply.
- chart: {"type":"chart","kind":"bar|line|pie","title":"..","labels":["a","b"],"series":[{"name":"..","values":[1,2]}],"unit":"ms"}
- stats: {"type":"stats","title":"..","items":[{"label":"Tests","value":"88/88","note":"..","tone":"ok|warn|bad"}]}
- progress: {"type":"progress","title":"..","items":[{"label":"..","value":3,"max":10}]}
- checklist: {"type":"checklist","title":"..","items":[{"label":"..","status":"done|todo|running|failed|skipped","note":".."}]}
- choices: {"type":"choices","question":"..","multi":false,"options":[{"label":"..","description":"..","message":"text to send if picked"}]}
- form: {"type":"form","title":"..","submit":"..","fields":[{"name":"k","label":"..","kind":"text|textarea|number|select|toggle","options":[".."],"default":".."}]}
- image: run `GROVY image FILE [--caption T]` (png/jpg/gif/webp, e.g. a plot or screenshot you made) and put the ```widget block it prints in your reply.
- diagram: {"type":"diagram","direction":"down|right","nodes":[{"id":"a","label":"..","tone":"ok|warn|bad"}],"edges":[{"from":"a","to":"b","label":".."}]}
choices/form only prefill the user's message box; they review and send it. Limits: ~12 bars, 8 pie slices, 30 nodes.
Only when the user asks for a page (they may say "local page": that means this), or the result is too big for \
widgets, make one: a self-contained HTML file \
(phone-first, light/dark; inline JS/CSS, pinned libraries from cdnjs/jsdelivr/unpkg, Google Fonts, data: images; \
no fetch, storage or cookies, so embed the data; `grovy.send(text)` prefills the message box), then run \
`GROVY page publish FILE --title T` as a command of its own (same file name updates it; `page ls`, `page rm NAME`). Mention it in your reply.
Commands you run that take more than ~30s (training, builds, long test runs, batch jobs) can show live cards on the \
phone as they go. Python: `import grovy` (on PYTHONPATH; does nothing outside grovy): `for x in grovy.track(xs, "T")`; \
`p = grovy.progress("T", total=N)` then `p.set(n, note="..")` or `p.advance()`; `grovy.note("markdown")`; \
`lg = grovy.log("T")` then `print(.., file=lg)`; `w = grovy.show(widget_dict)` then `w.update(d)`, `w.done()`. \
Shell: `cmd | GROVY tail --title T`, `GROVY progress KEY N --max M [--done]`, `GROVY note TEXT`. Use these in \
scripts you write to run, not in the user's code unless asked."""

GROVY_BIN = os.path.join(core.HERE, "grovy")
WIDGET_GUIDE = WIDGET_GUIDE.replace("GROVY ", shlex.quote(GROVY_BIN) + " ")


INSTRUCTIONS_MAX = 16 * 1024


def user_instructions():
    """The user's ~/.config/grovy/instructions.md: their own notes for Claude in every grovy session (for example
    the widgets their custom.js adds). Read at each SessionStart, so an edit reaches a session after /clear."""
    path = os.path.join(core.config_dir(), "instructions.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(INSTRUCTIONS_MAX + 1)
    except OSError:
        return ""
    if len(text) > INSTRUCTIONS_MAX:
        print(f"grovy hook: ignoring {path}: over {INSTRUCTIONS_MAX // 1024} KB", file=sys.stderr)
        return ""
    return text.strip()


def widget_guide():
    """Hooks inherit Claude's environment: a session started before PYTHONPATH was set needs the path spelled out."""
    if core.HERE in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        return WIDGET_GUIDE
    return WIDGET_GUIDE.replace("(on PYTHONPATH; ", f"(first `sys.path.insert(0, {core.HERE!r})`; ")
