"""Spec mode: a project's living spec (roadmap, documentation and decisions) in markdown files of its own git repo.

grovy only reads specs; Claude writes them. This plugin:
  * tells Claude the conventions, and each spec's index, when a session starts in a repo that has specs;
  * checks a spec right after Claude (or a subagent) edits it, and tells Claude at once what's wrong;
  * serves the parsed specs to the page (extensions/spec.js draws them).

A spec is a folder whose index.md starts with a YAML header holding `spec: 1` (the format version), plus the files its
`parts:` lists. The format is in FORMAT below; specs/spec-mode/ in the grovy folder is the design of this very plugin.
Turn it off with an empty ~/.config/grovy/plugins/spec.off, or copy this file there and make it your own.
"""
import os
import re
import subprocess
import threading
import time

FORMAT_VERSION = 1
SCAN_TTL = 5            # seconds a repo's list of specs is reused
FILE_MAX = 512 * 1024   # bigger markdown files are not read
INDEX_CONTEXT_MAX = 6000  # characters of one spec's index added to Claude's context

FORMAT = """\
grovy spec mode: this project keeps specs, its living roadmap, documentation and decision ledger. A spec is a folder \
(usually specs/NAME/) whose index.md starts with a YAML header (`spec: 1`, `title`, `status`: draft|active|done|\
archived, `next`: the next free number per ID prefix such as `{R: 17, D: 30, G: 8}`, `parts`: the spec's other \
files). Read only the parts you need: grep for an ID such as `{#R17}`.
- IDs: end every roadmap item, decision and glossary term with a stable ID: `{#R17}` (item), `{#D5}` (decision), \
`{#G3}` (term). Take the number from `next` and bump it in the same edit; never reuse one. From another spec: \
`NAME#R17`.
- Items: `- [ ]` todo, `- [~] @SESSION` doing (claim it before you start; leave items others claimed; a subagent \
writes `@SESSION/AGENT`), `- [x]` done, `- [-]` dropped (say why). You choose how fine-grained items are.
- When an item is done, fold it into its section's prose, in the present tense (how it works now), keeping its \
`{#R17}` there as an anchor. Never delete an ID: drop the item instead.
- Decisions: whenever the user settles a choice, append to the spec's decisions at once, then mention it in one \
line: `- {#D5} YYYY-MM-DD: the decision. Why: the reason. Supersedes D2.` Never edit an old entry: supersede it.
- Glossary: `**Term** {#G3}: definition`, one per paragraph, in the index's `## Glossary`.
grovy checks a spec after each edit and tells you right away what to fix."""

HEADER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
ID_RE = re.compile(r"\{#([A-Z][A-Za-z]{0,3})(\d{1,5})\}")
ITEM_RE = re.compile(r"^(\s*)[-*+] \[([ ~xX-])\][ \t]+(.*)$")
DECISION_RE = re.compile(r"^\s*[-*+] \{#([A-Z][A-Za-z]{0,3})(\d{1,5})\}[ \t]*(\d{4}-\d{2}-\d{2})?:?[ \t]*(.*)$")
TERM_RE = re.compile(r"^\*\*(.+?)\*\*[ \t]*\{#([A-Z][A-Za-z]{0,3})(\d{1,5})\}:?[ \t]*(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
SUPERSEDES_RE = re.compile(r"\bSupersedes\s+((?:[A-Z][A-Za-z]{0,3}\d+(?:\s*(?:,|and)\s*)?)+)", re.I)
CLAIM_RE = re.compile(r"^@([A-Za-z0-9_./-]+)\s*")
CODE_RE = re.compile(r"(`+).*?\1")
STATES = {" ": "todo", "~": "doing", "x": "done", "X": "done", "-": "dropped"}


def ids_in(text):
    """The IDs in a line of markdown, leaving out code spans (`{#R17}` there is an example, not an ID)."""
    return [pre + num for pre, num in ID_RE.findall(CODE_RE.sub("", text))]


# ---------------------------------------------------------------- a tiny YAML subset (stdlib only)
def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return {"true": True, "false": False, "null": None, "~": None, "": None}.get(v.lower(), v)


def _flow(v):
    """`[a, b]`, `{k: v, ...}` (no nesting), or a scalar."""
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v.startswith("{") and v.endswith("}"):
        out = {}
        for pair in v[1:-1].split(","):
            if ":" in pair:
                k, _, x = pair.partition(":")
                out[str(_scalar(k))] = _scalar(x)
        return out
    return _scalar(v)


def parse_header(text):
    """The YAML header's top-level `key: value` lines (values: scalars, flow lists/maps, or `- x` block lists).
    Returns (dict, body line offset) or (None, 0) when there is no header."""
    m = HEADER_RE.match(text)
    if not m:
        return None, 0
    out, key = {}, None
    for line in m.group(1).split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", line) or re.match(r"^-\s+(.*)$", line)
        if item and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(_scalar(item.group(1)))
            continue
        k = re.match(r"^([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if k:
            key = k.group(1)
            rest = re.sub(r"\s+#.*$", "", k.group(2))
            out[key] = _flow(rest) if rest.strip() else None
    return out, m.group(0).count("\n")


# ---------------------------------------------------------------- finding specs
def git_root(cwd):
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def tracked_markdown(root):
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                            "--", "*.md"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [os.path.join(root, p) for p in r.stdout.decode("utf-8", "replace").split("\0") if p]


def read(path):
    try:
        if os.path.getsize(path) > FILE_MAX:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def is_index(path):
    """A spec's index: its YAML header has `spec:` (the format version)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    meta, _ = parse_header(head) if head.startswith("---") else (None, 0)
    return bool(meta) and meta.get("spec") not in (None, False)


_scans, _scan_lock = {}, threading.Lock()


def find_specs(root):
    """Paths of the spec indexes in a repo (git-tracked or new, not ignored), cached for SCAN_TTL seconds."""
    with _scan_lock:
        hit = _scans.get(root)
        if hit and time.monotonic() - hit[0] < SCAN_TTL:
            return hit[1]
    found = sorted(p for p in tracked_markdown(root) if is_index(p))
    with _scan_lock:
        _scans[root] = (time.monotonic(), found)
    return found


def spec_of(path):
    """(index path, repo root) of the spec a markdown file belongs to, or (None, None)."""
    root = git_root(os.path.dirname(path))
    if not root:
        return None, None
    path = os.path.normpath(path)
    for index in find_specs(root):
        if path == index:
            return index, root
        folder = os.path.dirname(index)
        meta, _ = parse_header(read(index) or "")
        for part in parts_of(meta):
            if os.path.normpath(os.path.join(folder, part)) == path:
                return index, root
    return None, None


def parts_of(meta):
    parts = (meta or {}).get("parts") or []
    return [p for p in parts if isinstance(p, str) and p.endswith(".md")] if isinstance(parts, list) else []


# ---------------------------------------------------------------- parsing one spec
def load(index, root=None):
    """The whole spec: meta, its files' sections, items, decisions, terms, anchors, and problems found."""
    text = read(index) or ""
    meta, _ = parse_header(text)
    meta = meta or {}
    folder = os.path.dirname(index)
    spec = {"name": os.path.basename(folder), "index": index, "folder": folder,
            "rel": os.path.relpath(folder, root) if root else folder, "meta": meta,
            "title": str(meta.get("title") or os.path.basename(folder)), "status": str(meta.get("status") or "draft"),
            "files": [], "items": [], "decisions": [], "terms": [], "anchors": [], "headings": [], "problems": []}
    for rel in ["index.md"] + parts_of(meta):
        path = os.path.normpath(os.path.join(folder, rel))
        body = text if path == index else read(path)
        if body is None:
            spec["problems"].append(f"{rel}: listed in parts but missing (or over {FILE_MAX // 1024} KB)")
            continue
        spec["files"].append(rel)
        parse_file(spec, rel, body)
    for d in spec["decisions"]:
        for m in SUPERSEDES_RE.finditer(d["text"]):
            for ref in re.findall(r"[A-Z][A-Za-z]{0,3}\d+", m.group(1)):
                for old in spec["decisions"]:
                    if old["id"] == ref:
                        old["superseded_by"] = d["id"]
    lint(spec)
    return spec


def parse_file(spec, rel, text):
    _, skip = parse_header(text)
    lines = text.split("\n")
    section, fence, item = [], False, None

    def close():
        nonlocal item
        if item:
            item["text"] = " ".join(item.pop("lines")).strip()
            ids = ids_in(item["text"])
            item["id"] = ids[-1] if ids else None
            if ids:
                cut = item["text"].rfind("{#" + ids[-1] + "}")
                item["text"] = (item["text"][:cut] + item["text"][cut + len(ids[-1]) + 3:]).strip()
            claim = CLAIM_RE.match(item["text"])
            item["claim"] = claim.group(1) if claim and item["state"] == "doing" else None
            spec["items"].append(item)
        item = None

    for n, line in enumerate(lines[skip:], start=skip + 1):
        if FENCE_RE.match(line):
            close()
            fence = not fence
            continue
        if fence:
            continue
        if item and line.strip() and len(line) - len(line.lstrip()) > item["indent"] and not ITEM_RE.match(line):
            item["lines"].append(line.strip())   # the item goes on, indented
            continue
        close()
        h = HEADING_RE.match(line)
        if h:
            level, title = len(h.group(1)), h.group(2)
            ids = ids_in(title)
            title = ID_RE.sub(lambda m: "" if m.group(1) + m.group(2) in ids else m.group(0), title).strip()
            section = section[:level - 1] + [""] * max(0, level - 1 - len(section)) + [title]
            spec["headings"].append({"file": rel, "line": n, "level": level, "title": title,
                                     "id": ids[0] if ids else None})
            continue
        path = [s for s in section if s]
        m = ITEM_RE.match(line)
        if m:
            item = {"file": rel, "line": n, "indent": len(m.group(1)), "state": STATES[m.group(2)],
                    "section": path, "lines": [m.group(3)]}
            continue
        m = DECISION_RE.match(line)
        if m:
            spec["decisions"].append({"file": rel, "line": n, "id": m.group(1) + m.group(2), "date": m.group(3),
                                      "text": m.group(4).strip(), "section": path})
            continue
        m = TERM_RE.match(line.strip())
        if m:
            spec["terms"].append({"file": rel, "line": n, "term": m.group(1), "id": m.group(2) + m.group(3),
                                  "text": ID_RE.sub("", m.group(4)).strip()})
            continue
        for id_ in ids_in(line):   # an ID left in prose: a done item, written up
            spec["anchors"].append({"file": rel, "line": n, "id": id_, "section": path})
    close()
    # decisions continue on indented lines too
    for d in spec["decisions"]:
        if d["file"] != rel:
            continue
        for more in lines[d["line"]:]:
            if more.strip() and more[:1] in " \t" and not ITEM_RE.match(more) and not DECISION_RE.match(more):
                d["text"] += " " + more.strip()
            else:
                break


def all_ids(spec):
    out = []
    for kind in ("items", "decisions", "terms", "anchors", "headings"):
        for x in spec[kind]:
            if x.get("id"):
                out.append((x["id"], kind, x))
    return out


def lint(spec):
    p = spec["problems"]
    meta = spec["meta"]
    if meta.get("spec") != FORMAT_VERSION:
        p.append(f"index.md: header says spec: {meta.get('spec')!r}; this grovy reads format {FORMAT_VERSION}")
    seen = {}
    for id_, kind, x in all_ids(spec):
        if id_ in seen and not (kind == "anchors" and seen[id_][0] == "anchors"):
            first = seen[id_][1]
            p.append(f"{x['file']}:{x['line']}: {id_} is already used at {first['file']}:{first['line']}")
        seen.setdefault(id_, (kind, x))
    nxt = meta.get("next") if isinstance(meta.get("next"), dict) else {}
    if not nxt:
        p.append("index.md: the header has no `next:` counters (e.g. next: {R: 1, D: 1, G: 1})")
    top = {}
    for id_ in seen:
        pre, num = re.match(r"([A-Z][A-Za-z]*)(\d+)", id_).groups()
        top[pre] = max(top.get(pre, 0), int(num))
    for pre, n in sorted(top.items()):
        if pre not in nxt:
            if nxt:
                p.append(f"index.md: IDs use prefix {pre} but `next:` has no counter for it")
        elif not isinstance(nxt[pre], int) or nxt[pre] <= n:
            p.append(f"index.md: next {pre} is {nxt[pre]!r} but {pre}{n} exists: set it to {n + 1}")
    for it in spec["items"]:
        if not it["id"]:
            p.append(f"{it['file']}:{it['line']}: item has no ID ({it['text'][:40]!r})")
        if it["state"] == "doing" and not it["claim"]:
            p.append(f"{it['file']}:{it['line']}: {it['id'] or 'item'} is marked doing but not claimed (`[~] @SESSION`)")
    for d in spec["decisions"]:
        if not d["date"]:
            p.append(f"{d['file']}:{d['line']}: decision {d['id']} has no date (YYYY-MM-DD)")


def summary(spec):
    """Counts for the page and for snapshots: done, todo, doing, dropped (done counts written-up anchors)."""
    states = {"todo": 0, "doing": 0, "done": 0, "dropped": 0}
    item_ids = set()
    for it in spec["items"]:
        states[it["state"]] += 1
        if it["id"]:
            item_ids.add(it["id"])
    prefixes = {re.match(r"[A-Z][A-Za-z]*", i).group(0) for i in item_ids} or {"R"}
    written = {a["id"] for a in spec["anchors"]
               if re.match(r"[A-Z][A-Za-z]*", a["id"]).group(0) in prefixes and a["id"] not in item_ids}
    states["done"] += len(written)
    states["total"] = sum(states[k] for k in ("todo", "doing", "done"))
    return states


# ---------------------------------------------------------------- what Claude is told
def context_for(cwd):
    root = git_root(cwd)
    if not root:
        return None
    indexes = find_specs(root)
    if not indexes:
        return None
    parts = [FORMAT, f"Specs in this repo ({root}):"]
    for index in indexes:
        spec = load(index, root)
        s = summary(spec)
        doing = [f"{it['id']} ({it['claim']}): {it['text'][:80]}" for it in spec["items"] if it["state"] == "doing"]
        parts.append(f"- {spec['rel']}/index.md: {spec['title']} [{spec['status']}], {s['done']}/{s['total']} done"
                     + (f"; in progress: {'; '.join(doing)}" if doing else "")
                     + (f"; {len(spec['problems'])} problem(s) to fix" if spec["problems"] else ""))
    active = [i for i in indexes if str(parse_header(read(i) or "")[0].get("status")) == "active"]
    for index in active[:2]:   # the index of the active spec(s): the rest is read on demand
        text = read(index) or ""
        cut = text[:INDEX_CONTEXT_MAX]
        parts.append(f"<spec-index path=\"{index}\">\n{cut}{'…(cut: read the file)' if len(text) > len(cut) else ''}\n</spec-index>")
    return "\n".join(parts)


def note_for(path):
    """After an edit to a spec file: its problems, for Claude to fix now, or None."""
    index, root = spec_of(path)
    if not index:
        return None
    spec = load(index, root)
    if not spec["problems"]:
        return None
    shown = spec["problems"][:12]
    more = len(spec["problems"]) - len(shown)
    return (f"grovy spec check ({spec['rel']}): fix these now:\n" + "\n".join("- " + x for x in shown)
            + (f"\n(and {more} more)" if more else ""))


# ---------------------------------------------------------------- the plugin
def setup(grovy):
    @grovy.context
    def context(session, info):
        return context_for(info.get("cwd"))

    @grovy.after_edit(glob="*.md")
    def after_edit(session, info):
        return note_for(info["path"])
