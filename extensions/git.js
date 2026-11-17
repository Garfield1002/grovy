// Git: ⋯ → Git shows the session's repository as a graph (branches, recent commits; tap one for its files and diff),
// and branch names, tags and commit hashes Claude writes as `code` become links to them.
//
// A built-in extension that uses only window.grovy (see context-bar.js). Its data comes from the built-in git plugin
// (plugins/git.py in the grovy folder), which answers only about the folder the session runs in. Without that plugin
// (GROVY_PLUGINS=0, or a daemon started before it existed), the links never appear and ⋯ → Git says why.
// To turn it off: grovy.disable("git") in custom.js.
grovy.extension("git", grovy => {
  const {h} = grovy;
  const ROW = 46, LANE = 14, PAD = 9;   // graph geometry, px
  grovy.css(`
code.gitref { cursor:pointer; text-decoration:underline dotted; text-underline-offset:3px; color:var(--acc);
  white-space:nowrap; }
code.gitref svg.gi { width:.95em; height:.95em; vertical-align:-.12em; margin-right:.25em; fill:none; stroke:currentColor;
  stroke-width:1.6; stroke-linecap:round; stroke-linejoin:round; }
.gg-head { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; margin:2px 0 10px; font-size:13px; color:var(--mut); }
.gg-head b { color:var(--fg); font-family:ui-monospace,monospace; font-weight:600; }
.gg-head select { font:inherit; color:var(--fg); background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:4px 6px; max-width:100%; }
.gg { position:relative; }
.gg svg { position:absolute; left:0; top:0; overflow:visible; }
.gg-row { display:block; width:100%; height:${ROW}px; padding:5px 4px 5px 0; border:0; background:none; text-align:left;
  color:inherit; font:inherit; cursor:pointer; border-radius:8px; overflow:hidden; }
.gg-row:hover, .gg-row:focus-visible { background:var(--code); }
.gg-row .s { display:flex; gap:5px; align-items:center; white-space:nowrap; overflow:hidden; font-size:14px; line-height:19px; }
.gg-row .s > span:last-child { overflow:hidden; text-overflow:ellipsis; }
.gg-row .m { font-size:12px; color:var(--mut); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; line-height:17px; }
.gg-row .m code { font-size:11.5px; }
.gref { flex:none; font-size:11px; line-height:16px; padding:0 6px; border-radius:99px; border:1px solid var(--line);
  font-family:ui-monospace,monospace; max-width:46%; overflow:hidden; text-overflow:ellipsis; }
.gref.branch { border-color:var(--ok); color:var(--ok); } .gref.head { background:var(--ok); color:var(--card); }
.gref.tag { border-color:var(--warn); color:var(--warn); }
button.gref { cursor:pointer; background:none; }
.gc-meta { font-size:13px; color:var(--mut); margin:4px 0 10px; display:flex; flex-wrap:wrap; gap:4px 8px; align-items:center; }
.gc-meta code { cursor:pointer; }
.gc-body { font-size:14px; margin:0 0 12px; overflow-wrap:anywhere; }
.gc-files { font-size:13px; margin:0 0 12px; }
.gc-files div { display:flex; gap:8px; padding:3px 0; border-top:1px solid var(--line); }
.gc-files span:first-child { flex:1; font-family:ui-monospace,monospace; overflow-wrap:anywhere; }
.gc-files .a { color:var(--ok); } .gc-files .d { color:var(--bad); }
.gc-diff { font:12px/1.45 ui-monospace,monospace; background:var(--code); border-radius:8px; padding:8px; overflow-x:auto;
  white-space:pre; margin:6px 0 0; }
.gc-diff .a { color:var(--ok); } .gc-diff .d { color:var(--bad); } .gc-diff .h { color:var(--busy); }
.gc-diff .f { color:var(--fg); font-weight:700; }
.gg-note { color:var(--mut); font-size:13px; margin:10px 0; }`);

  // ---- data
  let plugin = true;   // false once the daemon says the git plugin isn't there: stop asking
  const enc = encodeURIComponent;
  async function call(path, body) {
    try { return await grovy.api("/api/x/git/" + path, body); }
    catch (e) { if (/not found/i.test(e.message) && !/no such/i.test(e.message)) plugin = false; throw e; }
  }
  const short = x => String(x).slice(0, 8);
  function age(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m";
    if (s < 86400) return Math.round(s / 3600) + "h";
    if (s < 86400 * 60) return Math.round(s / 86400) + "d";
    return new Date(t * 1000).toISOString().slice(0, 10);
  }

  // ---- links: inline `code` that names a branch, tag or commit of the session's repository
  // Looked up in batches; answers are kept per session (a miss only for a minute: Claude may be about to make it).
  const known = new Map();          // session -> Map(name -> {info, at})
  let waiting = new Map(), timer = 0;   // session -> Map(name -> [code elements])
  const NAME = /^[A-Za-z0-9][\w./+-]{0,99}$/;
  const MISS_TTL = 60000;
  grovy.on("render", node => {
    const s = grovy.session;
    if (!plugin || !s) return;
    for (const c of node.querySelectorAll("code")) {
      if (c.closest("pre, a, button, .gitref") || c.children.length) continue;
      const name = c.textContent.trim();
      if (!NAME.test(name) || /^\d+$/.test(name) || name.includes("..")) continue;
      const hit = known.get(s) && known.get(s).get(name);
      if (hit && (hit.info || Date.now() - hit.at < MISS_TTL)) { if (hit.info) link(c, s, name, hit.info); continue; }
      if (!waiting.has(s)) waiting.set(s, new Map());
      const w = waiting.get(s);
      if (!w.has(name)) w.set(name, []);
      w.get(name).push(c);
    }
    if (waiting.size && !timer) timer = setTimeout(flush, 40);
  });
  async function flush() {
    timer = 0;
    const batch = waiting;
    waiting = new Map();
    for (const [s, names] of batch) {
      const list = [...names.keys()].slice(0, 200);
      let res;
      try { res = await call("resolve", {session: s, names: list}); } catch (e) { continue; }
      if (!known.has(s)) known.set(s, new Map());
      const k = known.get(s), found = res.found || {};
      for (const n of list) {
        k.set(n, {info: found[n] || null, at: Date.now()});
        if (found[n]) for (const c of names.get(n)) link(c, s, n, found[n]);
      }
    }
  }
  // Small type icons (16×16, drawn with the text colour): a commit on its line, a branch forking, a tag.
  const ICONS = {
    commit: [["circle", {cx: 8, cy: 8, r: 3}], ["path", {d: "M1 8h4M11 8h4"}]],
    branch: [["circle", {cx: 5, cy: 3.5, r: 1.8}], ["circle", {cx: 5, cy: 12.5, r: 1.8}], ["circle", {cx: 11, cy: 5, r: 1.8}],
      ["path", {d: "M5 5.3v5.4M11 6.8c0 3-6 2-6 3.9"}]],
    tag: [["path", {d: "M2 2.5v5l6.5 6.5 5-5L7 2.5z"}], ["circle", {cx: 5, cy: 5.5, r: 1}]],
  };
  ICONS.remote = ICONS.branch;
  function icon(kind) {
    const g = svg("svg", {class: "gi", viewBox: "0 0 16 16", "aria-hidden": "true"});
    for (const [tag, attrs] of ICONS[kind] || ICONS.commit) g.append(svg(tag, attrs));
    return g;
  }
  function link(c, session, name, info) {
    c.classList.add("gitref");
    c.prepend(icon(info.kind));
    c.setAttribute("role", "link");
    c.setAttribute("tabindex", "0");
    c.title = info.kind === "commit" ? "Commit " + short(info.hash) : `${info.kind === "tag" ? "Tag" : "Branch"} ${name}`;
    const open = e => {
      e.preventDefault(); e.stopPropagation();
      if (info.kind === "commit") showCommit(session, info.hash); else showGraph(session, info.ref);
    };
    c.addEventListener("click", open);
    c.addEventListener("keydown", e => { if (e.key === "Enter") open(e); });
  }
  // A session's branches change: forget its answers when Claude finishes a turn.
  let lastState = null;
  grovy.on("route", () => { lastState = null; });
  grovy.on("update", st => {
    const s = st && st.state;
    if (lastState === "working" && s !== "working" && grovy.session) known.delete(grovy.session);
    lastState = s;
  });

  grovy.menu("Git", () => showGraph(grovy.session), {sub: "Branches and recent commits"});

  // ---- the graph
  // Lanes: each lane waits for one commit (the parent of the commit above). A commit takes the first lane waiting for
  // it (others waiting for it end there: a branch point); its first parent then takes over its lane, other parents
  // (a merge) get a lane each. Lanes keep their column, so a line is straight unless it joins or leaves a commit.
  function layout(commits) {
    const lanes = [], rows = [];
    let width = 1;
    for (const c of commits) {
      let col = lanes.indexOf(c.h);
      if (col < 0) { col = lanes.indexOf(null); if (col < 0) col = lanes.length; }
      const before = lanes.slice();
      for (let i = 0; i < lanes.length; i++) if (lanes[i] === c.h) lanes[i] = null;
      const to = [];
      c.p.forEach((p, i) => {
        let pc = i === 0 ? col : lanes.indexOf(p);
        if (pc < 0) { pc = lanes.indexOf(null); if (pc < 0) pc = lanes.length; }
        if (i > 0 && lanes[pc] === p) { to.push(pc); return; }   // already awaited: join that lane
        lanes[pc] = p; to.push(pc);
      });
      while (lanes.length && lanes[lanes.length - 1] == null) lanes.pop();
      width = Math.max(width, before.length, lanes.length, col + 1);
      rows.push({c, col, before, to});
    }
    return {rows, width};
  }
  const SVG = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }
  const laneColor = i => `var(--c${i % 6 + 1})`;
  function drawGraph({rows, width}) {
    const x = i => PAD + i * LANE, w = PAD * 2 + (width - 1) * LANE;
    const g = svg("svg", {width: w, height: rows.length * ROW, "aria-hidden": "true"});
    const line = (x1, y1, x2, y2, color) => g.append(svg("path", {fill: "none", stroke: color, "stroke-width": 2,
      d: x1 === x2 ? `M${x1} ${y1}V${y2}` : `M${x1} ${y1}C${x1} ${(y1 + y2) / 2} ${x2} ${(y1 + y2) / 2} ${x2} ${y2}`}));
    rows.forEach((r, i) => {
      const top = i * ROW, mid = top + ROW / 2, bot = top + ROW;
      r.before.forEach((want, j) => {
        if (want == null) return;
        if (want === r.c.h) line(x(j), top, x(r.col), mid, laneColor(j));   // into this commit
        else line(x(j), top, x(j), bot, laneColor(j));                      // passing by
      });
      r.to.forEach(pc => line(x(r.col), mid, x(pc), bot, laneColor(pc)));
    });
    rows.forEach((r, i) => g.append(svg("circle", {cx: x(r.col), cy: i * ROW + ROW / 2, r: r.c.p.length > 1 ? 3.5 : 4.5,
      fill: r.c.p.length > 1 ? "var(--card)" : laneColor(r.col), stroke: laneColor(r.col), "stroke-width": 2})));
    return {node: g, width: w};
  }
  function refChip(r, head, session) {
    const cls = "gref " + r.kind + (head && r.kind === "branch" && r.name === head ? " head" : "");
    return session ? h("button", {class: cls, title: r.name, onclick: e => { e.stopPropagation(); showGraph(session, r.ref); }}, r.name)
      : h("span", {class: cls, title: r.name}, r.name);
  }

  async function showGraph(session, ref = "", limit = 80) {
    if (!session) return grovy.say("Open a session first: Git shows the repository it works in.");
    const loading = h("p", {class: "gg-note"}, "Loading…");
    grovy.sheet(h("h2", {}, "Git"), loading);
    let d;
    try { d = await call(`graph?session=${enc(session)}&limit=${limit}${ref ? "&ref=" + enc(ref) : ""}`); }
    catch (e) {
      if (!loading.isConnected) return;
      return grovy.sheet(h("h2", {}, "Git"), h("p", {class: "gg-note"}, plugin ? "Couldn't read the repository: " + e.message
        : "The git plugin isn't running. Restart grovy (systemctl --user restart grovy), and check GROVY_PLUGINS isn't 0."));
    }
    if (grovy.session !== session || !loading.isConnected) return;   // moved on, or closed it, while it loaded
    if (!d.repo) return grovy.sheet(h("h2", {}, "Git"), h("p", {class: "gg-note"}, "This session's folder isn't in a git repository."));
    const pick = h("select", {"aria-label": "Show", onchange: () => showGraph(session, pick.value, limit)},
      h("option", {value: ""}, "All branches"),
      ...d.branches.map(b => {
        const o = h("option", {value: b.ref}, b.name);
        if (b.ref === d.ref) o.selected = true;
        return o;
      }));
    const head = h("div", {class: "gg-head"},
      d.branch ? h("span", {}, "On ", h("b", {}, d.branch)) : d.head ? h("span", {}, "Detached at ", h("b", {}, short(d.head))) : null,
      d.upstream ? h("span", {}, d.track ? `${d.track} ${d.upstream}` : `up to date with ${d.upstream}`) : null,
      d.changes ? h("span", {}, `${d.changes} changed file${d.changes > 1 ? "s" : ""}`) : d.changes === 0 ? h("span", {}, "clean") : null,
      pick);
    if (!d.commits.length) return grovy.sheet(h("h2", {}, "Git"), head, h("p", {class: "gg-note"}, "No commits yet."));
    const lay = layout(d.commits), g = drawGraph(lay);
    const rows = lay.rows.map(({c}) => h("button", {class: "gg-row", style: `padding-left:${g.width + 4}px`,
      onclick: () => showCommit(session, c.h, () => showGraph(session, ref, limit))},
      h("div", {class: "s"}, ...(d.refs[c.h] || []).map(r => refChip(r, d.branch)), h("span", {}, c.s)),
      h("div", {class: "m"}, h("code", {}, short(c.h)), " · ", c.a, " · ", age(c.t))));
    const more = d.commits.length >= d.limit && limit < 400
      ? h("button", {class: "chip", style: "margin-top:10px", onclick: () => showGraph(session, ref, limit * 2)}, "Show more") : null;
    grovy.sheet(h("h2", {}, "Git"), head, h("div", {class: "gg"}, g.node, ...rows), more);
  }

  // ---- one commit
  function diffNode(text) {
    const pre = h("pre", {class: "gc-diff"});
    for (const l of text.split("\n")) {
      const cls = /^(diff --git|\+\+\+ |--- )/.test(l) ? "f" : l.startsWith("@@") ? "h" : l.startsWith("+") ? "a" : l.startsWith("-") ? "d" : null;
      pre.append(cls ? h("span", {class: cls}, l) : l, "\n");
    }
    return pre;
  }
  // Commit messages are wrapped by hand at ~72 columns, and grovy.md keeps line breaks: on a phone every line would
  // break twice. Rejoin each paragraph's lines, but keep list items, code blocks, headings and trailers
  // ("Co-Authored-By: ...") on lines of their own.
  function reflow(text) {
    const out = [];
    let fence = false;
    const own = l => /^\s*([-*+]|\d+[.)])\s|^\s*(```|#|>)|^[A-Z][\w-]*: \S/.test(l);
    for (const l of text.split("\n")) {
      const prev = out.length ? out[out.length - 1] : "";
      if (/^\s*```/.test(l)) fence = !fence;
      else if (!fence && l.trim() && prev.trim() && !own(l) && !/^\s*(```|#)/.test(prev)) {
        out[out.length - 1] = prev.replace(/\s+$/, "") + " " + l.trim();
        continue;
      }
      out.push(l);
    }
    return out.join("\n");
  }
  async function showCommit(session, hash, back) {
    const top = back ? h("button", {class: "chip", style: "margin-bottom:8px", onclick: back}, "‹ Graph") : null;
    const loading = h("p", {class: "gg-note"}, "Loading…");
    grovy.sheet(top, h("h2", {}, "Commit " + short(hash)), loading);
    let c;
    try { c = await call(`commit?session=${enc(session)}&hash=${enc(hash)}`); }
    catch (e) { return loading.isConnected && grovy.sheet(top, h("h2", {}, "Commit " + short(hash)), h("p", {class: "gg-note"}, e.message)); }
    if (grovy.session !== session || !loading.isConnected) return;
    if (!c.repo) return grovy.sheet(top, h("p", {class: "gg-note"}, "This session's folder isn't in a git repository."));
    const parent = p => h("code", {class: "gitref", role: "link", tabindex: "0", onclick: () => showCommit(session, p, back),
      onkeydown: e => { if (e.key === "Enter") showCommit(session, p, back); }}, icon("commit"), short(p));
    const files = c.files.length ? h("div", {class: "gc-files"}, ...c.files.map(f => h("div", {},
      h("span", {}, f.from ? `${f.from} → ${f.path}` : f.path),
      f.add == null ? h("span", {}, "binary") : h("span", {}, h("span", {class: "a"}, "+" + f.add), " ", h("span", {class: "d"}, "−" + f.del)))),
      c.more_files ? h("div", {}, h("span", {}, `and ${c.more_files} more`)) : null)
      : h("p", {class: "gg-note"}, c.p.length > 1 ? "No changes besides the merge itself." : "No file changes.");
    const diff = h("details", {}, h("summary", {}, "Diff" + (c.truncated ? " (cut short: too long)" : "")));
    diff.addEventListener("toggle", () => { if (diff.open && diff.children.length < 2) diff.append(diffNode(c.diff)); });
    diff.open = c.diff.length < 20000;   // a short diff is shown at once
    grovy.sheet(top, h("h2", {style: "overflow-wrap:anywhere"}, c.s || "(no message)"),
      h("div", {class: "gc-meta"}, ...c.refs.map(r => refChip(r, null, session)), h("code", {title: c.h}, short(c.h)),
        h("span", {}, c.a), h("span", {}, new Date(c.t * 1000).toLocaleString()),
        c.p.length ? h("span", {}, c.p.length > 1 ? "parents" : "parent", " ", ...c.p.flatMap((p, i) => i ? [" ", parent(p)] : [parent(p)])) : null),
      c.body ? h("div", {class: "gc-body"}, grovy.md(reflow(c.body))) : null, files, c.diff ? diff : null,
      h("button", {class: "chip", style: "margin-top:12px", onclick: () => { grovy.closeSheet(); grovy.draft(`Commit ${short(c.h)} (${c.s}): `); }},
        "Ask Claude about it"));
  }
});
