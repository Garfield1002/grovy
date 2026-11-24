"""The slash commands the phone offers: skills and commands from the project and the user."""
import os
import re

from . import core


CMD_RE = re.compile(r"/([A-Za-z0-9][\w:.-]*)(?:\s|$)")
BUILTIN_COMMANDS = [{"name": "clear", "description": "Start a new conversation (clears context)", "source": "built-in"},
                    {"name": "compact", "description": "Summarize the conversation to free up context",
                     "source": "built-in"},
                    {"name": "goal", "description": "Set a goal; Claude keeps working until it's met "
                     "(/goal clear to stop, /goal alone for status)", "source": "built-in"}]
_cmd_cache = {}
# Claude Code built-ins (and common aliases) that open menus or act outside the conversation.
# Names are compared lowercased. Skills can't shadow these: the phone must never open a built-in menu.
RESERVED_COMMANDS = set("""add-dir agents allowed-tools approved-tools bashes btw bug chrome clear compact config
context continue copy cost doctor effort exit export extra-usage fast feedback fork goal help hooks ide init
install-github-app install-slack-app keybindings login logout mcp memory migrate-installer model new
output-style permissions plan plugin plugins pr-comments privacy-settings quit release-notes reload-plugins
remote-env rename reset resume review rewind sandbox security-review settings skills stats status statusline
tasks teleport terminal-setup theme todos upgrade usage vim""".split())

# Built-ins the phone is allowed to send even though they're reserved (reserved so a skill can't shadow them,
# but these produce a conversational effect that works when typed from the phone, unlike menu-openers like /model).
PHONE_SAFE = {"clear", "compact", "goal"}


def _frontmatter(path):
    meta, body = {}, []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(8192).splitlines()
    except OSError:
        return meta, ""
    if lines and lines[0].strip() == "---":
        for i, l in enumerate(lines[1:], 1):
            if l.strip() == "---":
                body = lines[i + 1:]
                break
            k, _, v = l.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    else:
        body = lines
    return meta, next((l.strip("# ").strip() for l in body if l.strip()), "")


def commands_for(cwd):
    """Skills and custom commands Claude Code would offer in this project, plus /clear and /compact.
    Cached briefly; read from the same places Claude Code reads them."""
    hit = _cmd_cache.get(cwd)
    if hit and core.now() - hit[0] < 30:
        return hit[1]
    import glob
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    found = {}

    def add(name, desc, source):
        if name.lower() in RESERVED_COMMANDS:  # a repo's SKILL.md named "model"/"Model" must not unlock /model
            return
        if name and re.fullmatch(r"[A-Za-z0-9][\w:.-]*", name) and name not in found:
            found[name] = {"name": name, "description": desc[:140], "source": source}

    roots = [(os.path.join(cwd, ".claude"), "project")] if cwd else []
    roots.append((home, "user"))
    for root, source in roots:
        for f in sorted(glob.glob(os.path.join(glob.escape(root), "skills", "*", "SKILL.md"))):
            meta, first = _frontmatter(f)
            add(meta.get("name") or os.path.basename(os.path.dirname(f)), meta.get("description") or first, source)
        cdir = os.path.join(root, "commands")
        for f in sorted(glob.glob(os.path.join(glob.escape(cdir), "**", "*.md"), recursive=True)):
            meta, first = _frontmatter(f)
            rel = os.path.relpath(f, cdir)[:-3].replace(os.sep, ":")
            add(rel, meta.get("description") or first, source)
    for f in sorted(glob.glob(os.path.join(glob.escape(home), "skills", "synced", "*", "*", "SKILL.md"))):
        meta, first = _frontmatter(f)  # claude.ai-synced skills are invoked with this prefix
        add("anthropic-skills:" + (meta.get("name") or os.path.basename(os.path.dirname(f))),
            meta.get("description") or first, "synced")
    out = BUILTIN_COMMANDS + sorted(found.values(), key=lambda c: c["name"])
    _cmd_cache[cwd] = (core.now(), out)
    return out
