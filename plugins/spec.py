"""Spec mode: a project's living specs (roadmap, documentation and decisions) in markdown files of its own git repo.

A daemon plugin and a command at once:
  * as a plugin, it tells a starting session which specs its repo has and how to use them (a few lines: the
    conventions are read on demand, with `grovy spec guide`), checks a spec right after Claude edits one by hand and
    tells Claude what that edit broke, and serves the specs to the page (extensions/spec.js draws them);
  * as a command (`grovy spec ...`, which runs this file), it reads one item with what relates to it (`show R7`) and
    writes items, claims, decisions and terms in place, so Claude never reads a spec file just to change a line.

A spec is a folder whose index.md starts with a YAML header holding `spec: 1` (the format version), plus the markdown
files its `parts:` lists. specs/spec-mode/ in the grovy folder is the design of this very plugin. Turn it off with an
empty ~/.config/grovy/plugins/spec.off, or copy this file there and make it your own.
"""
import datetime
import fcntl
import os
import re
import shutil
import subprocess
import sys
import threading
import time

FORMAT_VERSION = 1
SCAN_TTL = 5              # seconds a repo's list of specs is reused
FILE_MAX = 512 * 1024     # bigger markdown files are not read
FILE_WARN = 8 * 1024      # a spec file past this is worth splitting (each Read of it costs that much context)
NOTE_SHOWN = 8            # problems listed in one after-edit note

GUIDE = """\
grovy specs: a spec is a folder (specs/NAME/) of markdown files: index.md (a YAML header with `spec: 1`, `title`, \
`status`, `parts: [a.md, ...]`; then a short map: overview, Now, the roadmap, a Glossary) plus its parts (the \
documentation, by topic, and decisions.md). It is the project's roadmap, documentation and decision ledger.

Read little: `grovy spec status` (open items), `grovy spec show R7` (one item, decision or term, with where it is \
and the live decisions that mention it). Open a file only for its prose, and only the lines you need.

Write with commands, not by hand (no need to read the file first; each prints one line):
  grovy spec add "text" [--under HEADING]     a roadmap item; prints its new ID
  grovy spec claim R7 | tick R7 | todo R7 | drop R7 "why"
  grovy spec decide "the decision" --why "reason" [--supersedes D2]
  grovy spec term "Term" "definition"
Add --spec NAME when the repo has several specs. Each ID is stable and never reused; `NAME#R7` refers to another \
spec's.
Rules: claim an item before working on it and leave items others claimed. Record every choice the user settles at \
once with `decide`, then mention it in one line; never edit an old decision, supersede it. When an item is done, \
tick it; when its phase is done, write what it built into the topic's part as documentation (present tense), keeping \
the item's {#R7} there, and take the item out of the roadmap. Keep the index short.
grovy checks a spec after you edit one by hand and tells you what the edit broke."""

HEADER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
ID_RE = re.compile(r"\{#([A-Z][A-Za-z]{0,3})(\d{1,5})\}")
LIST = r"(?:[-*+]|\d{1,3}[.)])"
ITEM_RE = re.compile(r"^(\s*)" + LIST + r" \[([ ~xX-])\][ \t]+(.*)$")
MARK_RE = re.compile(r"^(\s*(?:>\s*)*" + LIST + r" \[)([ ~xX-])(\][ \t]+)(@[A-Za-z0-9_./-]+[ \t]*)?")
DECISION_RE = re.compile(r"^\s*" + LIST + r" \{#(D)(\d{1,5})\}[ \t]*(\d{4}-\d{2}-\d{2})?:?[ \t]*(.*)$")
TERM_RE = re.compile(r"^\*\*(.+?)\*\*[ \t]*\{#([A-Z][A-Za-z]{0,3})(\d{1,5})\}:?[ \t]*(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
QUOTE_RE = re.compile(r"^\s*(?:>\s?)+")
SUPERSEDES_RE = re.compile(r"\bSupersedes\s+((?:D\d+(?:\s*(?:,|and)\s*)?)+)", re.I)
CLAIM_RE = re.compile(r"^@([A-Za-z0-9_./-]+)\s*")
CODE_RE = re.compile(r"(`+).*?\1")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
STATES = {" ": "todo", "~": "doing", "x": "done", "X": "done", "-": "dropped"}
MARKS = {"todo": " ", "doing": "~", "done": "x", "dropped": "-"}


def ids_in(text):
    """The IDs in a line of markdown, leaving out code spans (`{#R17}` there is an example, not an ID)."""
    return [pre + num for pre, num in ID_RE.findall(CODE_RE.sub("", text))]


def prefix(id_):
    return re.match(r"[A-Z][A-Za-z]*", id_).group(0)


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
    """The YAML header's top-level `key: value` lines (values: scalars, flow lists/maps, `- x` block lists, or an
    indented `k: v` block map). Returns (dict, body line offset) or (None, 0) when there is no header."""
    m = HEADER_RE.match(text)
    if not m:
        return None, 0
    out, key = {}, None
    for line in m.group(1).split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(_scalar(item.group(1)))
            continue
        sub = re.match(r"^\s+([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if sub and key is not None and (out.get(key) is None or isinstance(out.get(key), dict)):
            out[key] = out.get(key) or {}
            out[key][sub.group(1)] = _flow(re.sub(r"\s+#.*$", "", sub.group(2)))
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
    root = r.stdout.strip() if r.returncode == 0 else ""
    return os.path.realpath(root) if root else None


def candidate_indexes(root):
    """index.md files git knows of (tracked, or new and not ignored): only these can be specs."""
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                            "--", "index.md", "*/index.md"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return sorted({os.path.join(root, p) for p in r.stdout.decode("utf-8", "replace").split("\0")
                   if p and os.path.basename(p) == "index.md"})


def read(path):
    try:
        if os.path.getsize(path) > FILE_MAX:
            return None
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except OSError:
        return None


def is_index(path):
    """A spec's index: a file named index.md whose YAML header has `spec: N` (the format version, a number)."""
    if os.path.basename(path) != "index.md":
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096).replace("\r\n", "\n")
    except OSError:
        return False
    meta, _ = parse_header(head)
    return bool(meta) and isinstance(meta.get("spec"), int) and not isinstance(meta.get("spec"), bool)


_scans, _scan_lock = {}, threading.Lock()


def find_specs(root):
    """Paths of the spec indexes in a repo, cached for SCAN_TTL seconds."""
    with _scan_lock:
        hit = _scans.get(root)
        if hit and time.monotonic() - hit[0] < SCAN_TTL:
            return hit[1]
    found = [p for p in candidate_indexes(root) if is_index(p)]
    with _scan_lock:
        _scans[root] = (time.monotonic(), found)
    return found


def forget(root):
    with _scan_lock:
        _scans.pop(root, None)


def part_path(folder, root, part):
    """Where a `parts:` entry points, or None if that is outside the repo (no absolute paths, no ../ or symlink
    escapes: a cloned repo must not make grovy read, or send to the page, files from elsewhere)."""
    if not isinstance(part, str) or not part.endswith(".md") or os.path.isabs(part):
        return None
    path = os.path.realpath(os.path.join(folder, part))
    return path if root and path.startswith(os.path.realpath(root) + os.sep) else None


def parts_of(meta):
    parts = (meta or {}).get("parts") or []
    return [p for p in parts if isinstance(p, str)] if isinstance(parts, list) else []


def spec_of(path):
    """(index path, repo root) of the spec a markdown file belongs to, or (None, None)."""
    path = os.path.realpath(path)
    root = git_root(os.path.dirname(path))
    if not root:
        return None, None
    if os.path.basename(path) == "index.md":
        forget(root)   # it may have just become a spec
    for index in find_specs(root):
        if path == os.path.realpath(index):
            return index, root
        folder = os.path.dirname(index)
        meta, _ = parse_header(read(index) or "")
        if any(part_path(folder, root, p) == path for p in parts_of(meta)):
            return index, root
    return None, None


# ---------------------------------------------------------------- parsing one spec
def load(index, root):
    """The whole spec: meta, its files' items, decisions, terms, anchors and headings, and the problems found."""
    text = read(index) or ""
    meta, _ = parse_header(text)
    meta = meta or {}
    folder = os.path.dirname(index)
    spec = {"name": os.path.basename(folder), "index": index, "folder": folder, "root": root,
            "rel": os.path.relpath(folder, root), "meta": meta,
            "title": str(meta.get("title") or os.path.basename(folder)), "status": str(meta.get("status") or "draft"),
            "files": [], "paths": {}, "sizes": {}, "items": [], "decisions": [], "terms": [], "anchors": [],
            "headings": [], "problems": []}
    for rel in ["index.md"] + parts_of(meta):
        path = index if rel == "index.md" else part_path(folder, root, rel)
        if path is None:
            spec["problems"].append(f"index.md: part {rel!r} is not a .md file inside the repo: left out")
            continue
        body = text if path == index else read(path)
        if body is None:
            spec["problems"].append(f"index.md: part {rel} is missing (or over {FILE_MAX // 1024} KB)")
            continue
        spec["files"].append(rel)
        spec["paths"][rel] = path
        spec["sizes"][rel] = len(body.encode())
        parse_file(spec, rel, body)
    for d in spec["decisions"]:
        d["supersedes"] = []
        for m in SUPERSEDES_RE.finditer(d["text"]):
            d["supersedes"] += re.findall(r"D\d+", m.group(1))
    for d in spec["decisions"]:
        for old in spec["decisions"]:
            if old["id"] in d["supersedes"]:
                old["superseded_by"] = d["id"]
    lint(spec)
    return spec


def parse_file(spec, rel, text):
    _, skip = parse_header(text)
    text = COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)   # comments out, line numbers kept
    lines = text.split("\n")
    section, fence, item, decision = [], False, None, None

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

    for n, raw in enumerate(lines[skip:], start=skip + 1):
        if FENCE_RE.match(raw):
            close()
            decision = None
            fence = not fence
            continue
        if fence:
            continue
        line = QUOTE_RE.sub("", raw)   # a quoted line reads like any other
        indent = len(line) - len(line.lstrip())
        if item and line.strip() and indent > item["indent"] and not ITEM_RE.match(line):
            item["lines"].append(line.strip())   # the item goes on, indented
            continue
        if decision and line.strip() and indent > 0 and not ITEM_RE.match(line) and not DECISION_RE.match(line):
            decision["text"] += " " + line.strip()
            continue
        close()
        decision = None
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
            decision = {"file": rel, "line": n, "id": m.group(1) + m.group(2), "date": m.group(3),
                        "text": m.group(4).strip(), "section": path}
            spec["decisions"].append(decision)
            continue
        m = TERM_RE.match(line.strip())
        if m:
            spec["terms"].append({"file": rel, "line": n, "term": m.group(1), "id": m.group(2) + m.group(3),
                                  "text": ID_RE.sub("", m.group(4)).strip(), "section": path})
            continue
        for id_ in ids_in(line):   # an ID left in prose: a done item, written up
            spec["anchors"].append({"file": rel, "line": n, "id": id_, "section": path, "text": line.strip()[:200]})
    close()


def all_ids(spec):
    out = []
    for kind in ("items", "decisions", "terms", "anchors", "headings"):
        for x in spec[kind]:
            if x.get("id"):
                out.append((x["id"], kind, x))
    return out


def item_prefixes(spec):
    return {prefix(it["id"]) for it in spec["items"] if it["id"]} or {"R"}


def lint(spec):
    p = spec["problems"]
    if spec["meta"].get("spec") != FORMAT_VERSION:
        p.append(f"index.md: header says spec: {spec['meta'].get('spec')!r}; this grovy reads format {FORMAT_VERSION}")
    seen = {}
    for id_, kind, x in all_ids(spec):
        if id_ in seen and not (kind == "anchors" and seen[id_][0] in ("anchors", "headings")) \
                and not (kind == "headings" and seen[id_][0] == "anchors"):
            first = seen[id_][1]
            p.append(f"{x['file']}:{x['line']}: {id_} is already used at {first['file']}:{first['line']}")
        seen.setdefault(id_, (kind, x))
    for it in spec["items"]:
        if not it["id"]:
            p.append(f"{it['file']}:{it['line']}: item has no ID ({it['text'][:40]!r}): "
                     "add it with `grovy spec add`, or write the next free ID")
        if it["state"] == "doing" and not it["claim"]:
            p.append(f"{it['file']}:{it['line']}: {it['id'] or 'item'} is marked doing but not claimed "
                     "(`grovy spec claim`)")
    for d in spec["decisions"]:
        if not d["date"]:
            p.append(f"{d['file']}:{d['line']}: decision {d['id']} has no date (YYYY-MM-DD)")
    for rel, size in spec["sizes"].items():
        if size > FILE_WARN:
            p.append(f"{rel}: {size // 1024} KB; every Read of it costs that much context: split it into parts")


def next_id(spec, pre):
    nums = [int(i[len(pre):]) for i, _, _ in all_ids(spec) if prefix(i) == pre]
    return f"{pre}{max(nums, default=0) + 1}"


def summary(spec):
    """Counts: done, todo, doing, dropped (done counts items written up as prose, their ID left as an anchor)."""
    states = {"todo": 0, "doing": 0, "done": 0, "dropped": 0}
    item_ids = {it["id"] for it in spec["items"] if it["id"]}
    for it in spec["items"]:
        states[it["state"]] += 1
    pres = item_prefixes(spec)
    states["done"] += len({a["id"] for a in spec["anchors"] if prefix(a["id"]) in pres and a["id"] not in item_ids})
    states["total"] = sum(states[k] for k in ("todo", "doing", "done"))
    return states


def sections(spec):
    """The roadmap by section, in reading order: done/total and the item IDs (written-up anchors count as done)."""
    out, by = [], {}

    def sec(path, file):
        key = (file, tuple(path))
        if key not in by:
            by[key] = {"file": file, "path": path, "done": 0, "total": 0, "items": []}
            out.append(by[key])
        return by[key]
    item_ids = {it["id"] for it in spec["items"] if it["id"]}
    pres = item_prefixes(spec)
    events = [(x["file"], x["line"], "item", x) for x in spec["items"]] + \
             [(a["file"], a["line"], "anchor", a) for a in spec["anchors"]
              if prefix(a["id"]) in pres and a["id"] not in item_ids]
    order = {f: n for n, f in enumerate(spec["files"])}
    for file, line, kind, x in sorted(events, key=lambda e: (order.get(e[0], 99), e[1])):
        s = sec(x["section"], file)
        if kind == "anchor":
            s["done"] += 1
            s["total"] += 1
            continue
        if x["state"] != "dropped":
            s["total"] += 1
            s["done"] += x["state"] == "done"
        s["items"].append(x["id"])
    return out


def stamp(spec):
    """Changes whenever one of the spec's files does: the page reloads only then."""
    out = []
    for rel in spec["files"]:
        try:
            st = os.stat(spec["paths"][rel])
            out.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            out.append(rel)
    return "|".join(out)


# ---------------------------------------------------------------- what Claude is told
def pointer(cwd, grovy_bin="grovy"):
    """At session start: which specs the repo has and how to use them, in a few lines (the rest on demand)."""
    root = git_root(cwd)
    indexes = find_specs(root) if root else []
    if not indexes:
        return None
    lines = ["This repo keeps specs (living roadmap, documentation and decisions) under grovy spec mode:"]
    for index in indexes:
        spec = load(index, root)
        s = summary(spec)
        doing = [f"{it['id']} @{it['claim']}" for it in spec["items"] if it["state"] == "doing" and it["id"]]
        lines.append(f"- {spec['rel']}: {spec['title']} [{spec['status']}], {s['done']}/{s['total']} done"
                     + (f", in progress: {', '.join(doing[:6])}" if doing else ""))
    where = "" if shutil.which("grovy") and os.path.realpath(shutil.which("grovy")) == os.path.realpath(grovy_bin) \
        else f" (grovy is {grovy_bin})"
    lines.append(f"Before you read or change a spec, run `grovy spec guide` once{where}: its conventions. Then "
                 "`grovy spec status` and `grovy spec show R7` beat reading files, and writes go through `grovy spec` "
                 "commands. Record every choice the user settles with `grovy spec decide`.")
    return "\n".join(lines)


_last = {}            # spec index -> the problems last reported (or seen), keyed without line numbers
_last_lock = threading.Lock()


def problem_key(p):
    return re.sub(r"^([^:]+):\d+:", r"\1:", p)


def note_for(path, subagent=False, grovy_bin="grovy"):
    """After a hand edit to a spec file: only the problems this edit introduced (and how many older ones remain)."""
    index, root = spec_of(path)
    if not index:
        return None
    spec = load(index, root)
    now = {problem_key(p): p for p in spec["problems"]}
    with _last_lock:
        before = _last.get(index)
        _last[index] = set(now)
    new = [p for k, p in now.items() if before is None or k not in before]
    if not new:
        return None
    old = len(now) - len(new)
    shown = new[:NOTE_SHOWN]
    return (f"grovy spec check ({spec['rel']}): your edit left this to fix:\n" + "\n".join("- " + x for x in shown)
            + (f"\n(and {len(new) - len(shown)} more)" if len(new) > len(shown) else "")
            + (f"\n({old} older problem(s): `{grovy_bin} spec check`)" if old else "")
            + (f"\nSpec conventions: `{grovy_bin} spec guide`." if subagent else ""))


# ---------------------------------------------------------------- the page
def public(spec):
    """What the page gets: the parsed spec, its files' text, counts; paths relative to the repo."""
    texts = {rel: read(spec["paths"][rel]) or "" for rel in spec["files"]}
    return {"rel": spec["rel"], "title": spec["title"], "status": spec["status"], "files": spec["files"],
            "texts": texts, "items": spec["items"], "decisions": spec["decisions"], "terms": spec["terms"],
            "headings": spec["headings"], "problems": spec["problems"], "summary": summary(spec),
            "sections": sections(spec), "stamp": stamp(spec)}


def setup(grovy):
    grovy_bin = getattr(grovy, "bin", "grovy")

    def repo_of(request):
        name = request.query.get("session")
        detail = grovy.session(name) if isinstance(name, str) and name else None
        return git_root(detail["session"].get("cwd")) if detail else None

    @grovy.route("GET", "list")
    def list_specs(request):   # the specs of the session's repo, with their counts
        root = repo_of(request)
        out = []
        for index in find_specs(root) if root else []:
            spec = load(index, root)
            out.append({"rel": spec["rel"], "title": spec["title"], "status": spec["status"],
                        "summary": summary(spec), "problems": len(spec["problems"])})
        return {"root": root, "specs": out}

    @grovy.route("GET", "ids")
    def ids(request):   # every ID of the repo's specs, for links in the conversation: "R7" and "spec-mode#R7"
        root = repo_of(request)
        out, seen = {}, {}
        for index in find_specs(root) if root else []:
            spec = load(index, root)
            for id_, kind, x in all_ids(spec):
                what = {"rel": spec["rel"], "kind": kind,
                        "text": str(x.get("text") or x.get("title") or x.get("term") or "")[:160]}
                out.setdefault(f"{spec['name']}#{id_}", what)
                seen.setdefault(id_, []).append(what)
        for id_, whats in seen.items():   # a bare ID only when one spec has it
            if len({w["rel"] for w in whats}) == 1:
                out[id_] = whats[0]
        return {"ids": out}

    @grovy.route("GET", "spec")
    def one_spec(request):   # chosen among those found: no path from the page is ever opened
        root = repo_of(request)
        rel = request.query.get("rel")
        index = next((i for i in find_specs(root) if os.path.relpath(os.path.dirname(i), root) == rel), None) \
            if root else None
        if not index:
            return 404, {"error": "no such spec in this session's repo"}
        spec = load(index, root)
        if request.query.get("if") and request.query.get("if") == stamp(spec):
            return {"same": True}
        return public(spec)

    @grovy.context
    def context(session, info):
        return pointer(info.get("cwd"), grovy_bin)

    @grovy.after_edit(glob="*.md")
    def after_edit(session, info):
        return note_for(info["path"], subagent=bool(info.get("agent_id")), grovy_bin=grovy_bin)


# ---------------------------------------------------------------- the command: grovy spec ...
class Fail(Exception):
    pass


def pick(root, name=None, id_=None):
    """The spec a command is about: --spec NAME, else the one holding the ID, else the only (or only active) one."""
    specs = [load(i, root) for i in find_specs(root)]
    if not specs:
        raise Fail("this repo has no spec yet: create specs/NAME/index.md with a `spec: 1` header (`grovy spec guide`)")
    if name:
        hit = [s for s in specs if name in (s["name"], s["rel"])]
        if not hit:
            raise Fail(f"no spec {name!r} here (there are: {', '.join(s['name'] for s in specs)})")
        return hit[0]
    if id_:
        hit = [s for s in specs if any(i == id_ for i, _, _ in all_ids(s))]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            raise Fail(f"{id_} is in several specs ({', '.join(s['name'] for s in hit)}): add --spec NAME")
        raise Fail(f"no {id_} in this repo's specs")
    active = [s for s in specs if s["status"] == "active"]
    if len(specs) == 1 or len(active) == 1:
        return specs[0] if len(specs) == 1 else active[0]
    raise Fail(f"several specs here ({', '.join(s['name'] for s in specs)}): add --spec NAME")


def find(spec, id_):
    for i, kind, x in all_ids(spec):
        if i == id_ and kind != "headings":
            return kind, x
    for i, kind, x in all_ids(spec):
        if i == id_:
            return kind, x
    return None, None


def edit(spec, rel, change):
    """Change one file of the spec in place: change(lines) edits the list. Locked against other commands."""
    path = spec["paths"][rel]
    with open(spec["index"]) as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        with open(path, encoding="utf-8", newline="") as f:
            raw = f.read()
        crlf = "\r\n" in raw
        lines = raw.replace("\r\n", "\n").split("\n")
        change(lines)
        out = "\n".join(lines)
        tmp = f"{path}.grovy-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(out.replace("\n", "\r\n") if crlf else out)
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)


def append_under(lines, heading, new):
    """Insert `new` lines after the last list line of the section titled `heading` (or at the end of the file)."""
    start = None
    if heading:
        for n, line in enumerate(lines):
            h = HEADING_RE.match(line)
            if h and heading.lower() in ID_RE.sub("", h.group(2)).lower():
                start, level = n, len(h.group(1))
        if start is None:
            raise Fail(f"no heading matching {heading!r}")
        end = len(lines)
        for n in range(start + 1, len(lines)):
            h = HEADING_RE.match(lines[n])
            if h and len(h.group(1)) <= level:
                end = n
                break
    else:
        start, end = 0, len(lines)
    last, fence = None, False
    for n in range(start, end):
        if FENCE_RE.match(lines[n]):
            fence = not fence
            continue
        if fence:
            continue
        if re.match(r"^\s*" + LIST + r" ", lines[n]) or (last == n - 1 and lines[n].startswith("  ") and lines[n].strip()):
            last = n
    at = last + 1 if last is not None else end
    while at == end and at > 0 and not lines[at - 1].strip():
        at -= 1
    if last is None and at > 0 and lines[at - 1].strip():
        new = [""] + new
    lines[at:at] = new


def ensure_section(lines, title):
    if not any((h := HEADING_RE.match(l)) and h.group(2).strip().lower() == title.lower() for l in lines):
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"## {title}", "", ""]


def report(before, after):
    """The problems a command's change introduced (normally none)."""
    old = {problem_key(p) for p in before["problems"]}
    new = [p for p in after["problems"] if problem_key(p) not in old]
    return "\n".join("problem: " + p for p in new)


def cmd(argv):
    import argparse
    p = argparse.ArgumentParser(prog="grovy spec", description="Read and write this repo's specs (grovy spec mode).")
    p.add_argument("--spec", help="which spec (its folder name), when the repo has several")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("guide", help="the conventions")
    sub.add_parser("status", help="each spec's open items, one line each")
    sub.add_parser("check", help="all of a spec's problems")
    s = sub.add_parser("show", help="one item, decision or term, with what relates to it")
    s.add_argument("id")
    s = sub.add_parser("add", help="add a roadmap item; prints its ID")
    s.add_argument("text"), s.add_argument("--under", help="the heading to put it under (default: the last item's)")
    for name in ("claim", "tick", "todo"):
        s = sub.add_parser(name, help={"claim": "mark it in progress, by you", "tick": "mark it done",
                                       "todo": "back to todo"}[name])
        s.add_argument("id")
        if name == "claim":
            s.add_argument("--as", dest="who", help="who works on it (default: $GROVY_SESSION)")
    s = sub.add_parser("drop", help="drop an item, saying why")
    s.add_argument("id"), s.add_argument("why")
    s = sub.add_parser("decide", help="record a decision; prints its ID")
    s.add_argument("text"), s.add_argument("--why"), s.add_argument("--supersedes", help="e.g. D2 or D2,D5")
    s = sub.add_parser("term", help="add a glossary term")
    s.add_argument("term"), s.add_argument("definition")
    a = p.parse_args(argv)
    if a.cmd == "guide":
        print(GUIDE)
        return 0
    root = git_root(os.getcwd())
    if not root:
        raise Fail("not in a git repository (specs live in git repos)")
    if a.cmd == "status":
        specs = [load(i, root) for i in find_specs(root)]
        if a.spec:
            specs = [pick(root, a.spec)]
        if not specs:
            raise Fail("this repo has no spec yet (`grovy spec guide`)")
        for spec in specs:
            s = summary(spec)
            print(f"{spec['rel']}: {spec['title']} [{spec['status']}], {s['done']}/{s['total']} done"
                  + (f", {len(spec['problems'])} problem(s): `grovy spec check`" if spec["problems"] else ""))
            for it in sorted((it for it in spec["items"] if it["state"] in ("doing", "todo")),
                             key=lambda it: it["state"] != "doing"):
                mark = f"@{it['claim']}" if it["state"] == "doing" else "todo"
                print(f"  {it['id'] or '(no id)'} {mark}: {it['text'][:110]}" + ("…" if len(it["text"]) > 110 else ""))
        return 0
    if a.cmd == "check":
        spec = pick(root, a.spec)
        print("\n".join(spec["problems"]) or f"{spec['rel']}: no problems")
        return 0
    if a.cmd == "show":
        spec = pick(root, a.spec, a.id)
        kind, x = find(spec, a.id)
        if not x:
            raise Fail(f"no {a.id} in {spec['rel']}")
        where = f"{spec['rel']}/{x['file']}:{x['line']}"
        if kind == "items":
            print(f"{a.id} [{x['state']}{' @' + x['claim'] if x['claim'] else ''}] {where}, under "
                  f"{' > '.join(x['section']) or '(top)'}\n{x['text']}")
        elif kind == "decisions":
            print(f"{a.id} {x['date'] or '(no date)'} {where}"
                  + (f" (superseded by {x['superseded_by']})" if x.get("superseded_by") else "") + f"\n{x['text']}")
        elif kind == "terms":
            print(f"{x['term']} ({a.id}) {where}: {x['text']}")
        else:
            print(f"{a.id} (written up) {where}, under {' > '.join(x['section']) or '(top)'}\n{x.get('text', '')}")
        pat = re.compile(r"(?<![\w#])" + re.escape(a.id) + r"\b")
        related = [d for d in spec["decisions"] if d["id"] != a.id and not d.get("superseded_by")
                   and pat.search(CODE_RE.sub("", d["text"]))]   # `show R7` in backticks is an example
        for d in related[:8]:
            print(f"- {d['id']} {d['date'] or ''}: {d['text'][:300]}")
        if kind == "decisions" and x.get("supersedes"):
            print("supersedes: " + ", ".join(x["supersedes"]))
        return 0
    # writes
    if a.cmd in ("claim", "tick", "todo", "drop"):
        spec = pick(root, a.spec, a.id)
        kind, x = find(spec, a.id)
        if kind != "items":
            raise Fail(f"{a.id} is not a roadmap item in {spec['rel']}" + (f" (it's a {kind[:-1]})" if kind else ""))
        state = {"claim": "doing", "tick": "done", "todo": "todo", "drop": "dropped"}[a.cmd]
        who = None
        if a.cmd == "claim":
            who = a.who or os.environ.get("GROVY_SESSION") or os.environ.get("CCS_SESSION")
            if not who:
                raise Fail("say who works on it: --as NAME")
            if x["claim"] and x["claim"] != who and x["claim"].split("/")[0] != who.split("/")[0]:
                raise Fail(f"{a.id} is claimed by @{x['claim']}: leave it, or ask the user")

        def change(lines):
            n = x["line"] - 1
            m = MARK_RE.match(lines[n])
            if not m:
                raise Fail(f"can't find {a.id}'s checkbox at {x['file']}:{x['line']}")
            rest = lines[n][m.end():]
            lines[n] = m.group(1) + MARKS[state] + m.group(3) + (f"@{who} " if who else "") + rest
            if a.cmd == "drop":
                at = n
                while at + 1 < len(lines) and lines[at + 1].strip() and lines[at + 1][:1] in " \t" \
                        and not ITEM_RE.match(QUOTE_RE.sub("", lines[at + 1])):
                    at += 1
                lines[at] = re.sub(r"(\s*\{#" + re.escape(a.id) + r"\})?\s*$",
                                   lambda m: f" (dropped: {a.why.strip()})" + (m.group(1) or ""), lines[at], count=1)
        edit(spec, x["file"], change)
        after = load(spec["index"], root)
        print(f"{a.id} {state}" + (f" @{who}" if who else "") + (("\n" + report(spec, after)) if report(spec, after) else ""))
        return 0
    if a.cmd == "add":
        spec = pick(root, a.spec)
        pre = sorted(item_prefixes(spec))[0]
        new = next_id(spec, pre)
        text = " ".join(a.text.split())
        under = a.under
        rel = "index.md"
        if not under and spec["items"]:
            last = spec["items"][-1]
            rel, under = last["file"], (last["section"][-1] if last["section"] else None)
        elif under:
            rel = next((h["file"] for h in spec["headings"] if under.lower() in h["title"].lower()), "index.md")
        edit(spec, rel, lambda lines: append_under(lines, under, [f"- [ ] {text} {{#{new}}}"]))
        after = load(spec["index"], root)
        r = report(spec, after)
        print(f"added {new} to {spec['rel']}" + (f" under {under}" if under else "") + (f"\n{r}" if r else ""))
        return 0
    if a.cmd == "decide":
        spec = pick(root, a.spec)
        new = next_id(spec, "D")
        sup = [s.strip().upper() for s in (a.supersedes or "").split(",") if s.strip()]
        known = {d["id"] for d in spec["decisions"]}
        if any(s not in known for s in sup):
            raise Fail(f"no decision {', '.join(s for s in sup if s not in known)} in {spec['rel']}")
        text = " ".join(a.text.split()).rstrip(".") + "."
        if a.why:
            text += " Why: " + " ".join(a.why.split()).rstrip(".") + "."
        if sup:
            text += " Supersedes " + ", ".join(sup) + "."
        line = f"- {{#{new}}} {datetime.date.today().isoformat()}: {text}"
        if spec["decisions"]:
            rel = spec["decisions"][-1]["file"]
            edit(spec, rel, lambda lines: append_under(lines, None, [line]))
        elif "decisions.md" in spec["files"]:
            rel = "decisions.md"
            edit(spec, rel, lambda lines: append_under(lines, None, [line]))
        else:
            rel = "index.md"
            edit(spec, rel, lambda lines: (ensure_section(lines, "Decisions"), append_under(lines, "Decisions", [line])))
        after = load(spec["index"], root)
        r = report(spec, after)
        print(f"recorded {new} in {spec['rel']}/{rel}" + (f"\n{r}" if r else ""))
        return 0
    if a.cmd == "term":
        spec = pick(root, a.spec)
        new = next_id(spec, "G")
        line = f"**{' '.join(a.term.split())}** {{#{new}}}: {' '.join(a.definition.split())}"

        def change(lines):
            ensure_section(lines, "Glossary")
            append_under(lines, "Glossary", [line])
        edit(spec, "index.md", change)
        print(f"added {new} ({a.term}) to {spec['rel']}")
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(cmd(sys.argv[1:]))
    except Fail as e:
        print(f"grovy spec: {e}", file=sys.stderr)
        sys.exit(1)
