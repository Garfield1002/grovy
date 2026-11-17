// Spec mode: the project's specs (roadmap, documentation, decisions), drawn from what plugins/spec.py reads in the
// session's git repo. ⋯ → Specs opens them. grovy never writes a spec: tapping an item or a decision only puts a
// message to Claude in the box, for you to send.
//
// A built-in extension, written only against window.grovy like your own custom.js. Turn it off with
// grovy.disable("spec") in custom.js (and the daemon side with an empty ~/.config/grovy/plugins/spec.off).
grovy.extension("spec", grovy => {
  const {h, md, api} = grovy;
  grovy.css(`
.spec-top { display:flex; align-items:center; gap:10px; margin:2px 0 14px; flex-wrap:wrap; }
.spec-status { font-size:12.5px; font-weight:650; text-transform:uppercase; letter-spacing:.04em; color:var(--mut);
  border:1px solid var(--line); border-radius:99px; padding:2px 10px; }
.spec-status.active { color:var(--ok); border-color:var(--ok); }
.spec-meter { flex:1; min-width:140px; } .spec-meter .n { font-size:13px; color:var(--mut); margin-bottom:4px; }
.spec-bar { height:6px; border-radius:3px; background:var(--line); overflow:hidden; }
.spec-bar b { display:block; height:100%; background:var(--ok); transition:width .4s ease; }
.spec-problems { border:1px solid var(--warn); border-radius:12px; padding:10px 12px; margin:0 0 14px; font-size:14px; }
.spec-problems b { color:var(--warn); } .spec-problems ul { margin:6px 0 0; padding-left:18px; }
.spec-sec { margin:0 0 18px; } .spec-sec h3 { display:flex; align-items:baseline; gap:8px; margin:0 0 6px; font-size:16px; }
.spec-sec h3 span { font-weight:400; font-size:13px; color:var(--mut); margin-left:auto; }
.spec-sec .spec-bar { height:4px; margin-bottom:6px; }
.spec-item { display:flex; gap:10px; align-items:flex-start; width:100%; text-align:left; border:0; background:none;
  padding:9px 2px; border-bottom:1px solid var(--line); font:inherit; color:inherit; }
.spec-item i { flex:none; width:18px; text-align:center; font-style:normal; color:var(--mut); }
.spec-item.done i { color:var(--ok); } .spec-item.doing i { color:var(--warn); }
.spec-item.done .t, .spec-item.dropped .t { color:var(--mut); } .spec-item.dropped .t { text-decoration:line-through; }
.spec-item .t { flex:1; min-width:0; } .spec-item .t > code:last-child { font-size:12px; color:var(--mut); margin-left:6px; background:none; }
.spec-claim { font-size:12px; font-weight:650; color:var(--warn); margin-right:6px; }
.spec-more { margin:0 0 12px; border:1px solid var(--line); border-radius:12px; padding:0 12px; background:var(--card); }
.spec-more summary { padding:12px 0; font-weight:650; cursor:pointer; }
.spec-dec { padding:8px 0; border-top:1px solid var(--line); font-size:14.5px; }
.spec-dec.old { color:var(--mut); } .spec-dec .d { font-size:12.5px; color:var(--mut); }
.spec-term { padding:6px 0; font-size:14.5px; }`);

  const ID_TOKEN = /\s*\{#[A-Z][A-Za-z]{0,3}\d{1,5}\}/g;
  const HEADER = /^---[ \t]*\n[\s\S]*?\n---[ \t]*(\n|$)/;
  const ICON = {todo: "○", doing: "◐", done: "●", dropped: "✕"};
  const get = (path, params) => api(`/api/x/spec/${path}?` + new URLSearchParams(params));
  const bar = (done, total) => h("div", {class: "spec-bar"}, h("b", {style: `width:${total ? Math.round(done / total * 100) : 0}%`}));
  let open = null;   // {panel, session, rel, loaded}: the spec on screen

  grovy.menu("Specs", async session => {
    let r;
    try { r = await get("list", {session}); } catch (e) { return grovy.say("Specs: " + e.message); }
    if (!r.root) return grovy.say("This session's folder isn't in a git repository: specs live in git repos.");
    if (!r.specs.length) return grovy.say("No spec in this project yet. Ask Claude to start one: it knows the format.");
    if (r.specs.length === 1) return grovy.open("spec", {session, rel: r.specs[0].rel});
    grovy.sheet(h("h2", {}, "Specs"), ...r.specs.map(s => h("button", {class: "item",
      onclick: () => { grovy.closeSheet(); grovy.open("spec", {session, rel: s.rel}); }}, s.title,
      h("div", {class: "sub"}, `${s.rel} · ${s.status} · ${s.summary.done}/${s.summary.total} done`
        + (s.problems ? ` · ${s.problems} to fix` : "")))));
  }, {sub: "Roadmap, docs and decisions of this project"});

  grovy.view("spec", (panel, arg) => {
    open = {panel, session: arg.session, rel: arg.rel, loaded: 0};
    panel.title(arg.rel);
    panel.actions(h("button", {class: "chip", onclick: () => load(true)}, "Refresh"));
    panel.onclose = () => { if (open && open.panel === panel) open = null; };
    panel.body.append(h("div", {class: "sys"}, "Loading…"));
    load(true);
  });

  grovy.on("update", () => { if (open && Date.now() - open.loaded > 5000) load(false); });   // Claude may be editing it

  async function load(shout) {
    const o = open; if (!o) return;
    o.loaded = Date.now();
    let s;
    try { s = await get("spec", {session: o.session, rel: o.rel}); }
    catch (e) { if (shout && !o.panel.closed) o.panel.body.replaceChildren(h("div", {class: "sys"}, e.message)); return; }
    if (o.panel.closed) return;
    o.panel.title(s.title);
    o.panel.body.replaceChildren(...draw(s, o));
  }

  function draw(s, o) {
    const items = Object.fromEntries(s.items.filter(it => it.id).map(it => [it.id, it]));
    const sum = s.summary;
    const top = h("div", {class: "spec-top"}, h("span", {class: "spec-status " + s.status}, s.status),
      h("div", {class: "spec-meter"}, h("div", {class: "n"}, `${sum.done} of ${sum.total} done`
        + (sum.doing ? ` · ${sum.doing} in progress` : "") + (sum.dropped ? ` · ${sum.dropped} dropped` : "")), bar(sum.done, sum.total)));
    const problems = s.problems.length ? h("div", {class: "spec-problems"}, h("b", {}, `${s.problems.length} to fix`),
      h("ul", {}, ...s.problems.slice(0, 20).map(p => h("li", {}, p)))) : null;
    const doing = s.items.filter(it => it.state === "doing");
    const now = doing.length ? h("div", {class: "spec-sec"}, h("h3", {}, "In progress"), ...doing.map(it => row(it, s, o))) : null;
    const secs = s.sections.filter(x => x.items.length).map(x => h("div", {class: "spec-sec"},
      h("h3", {}, x.path[x.path.length - 1] || x.file, h("span", {}, `${x.done}/${x.total}`)), bar(x.done, x.total),
      ...x.items.map(id => items[id]).filter(Boolean).map(it => row(it, s, o))));
    const loose = s.items.filter(it => !it.id).map(it => row(it, s, o));
    const decisions = s.decisions.length ? h("details", {class: "spec-more"}, h("summary", {}, `Decisions (${s.decisions.length})`),
      ...s.decisions.slice().reverse().map(d => h("div", {class: "spec-dec" + (d.superseded_by ? " old" : ""), role: "button", tabindex: "0",
        onclick: () => ask(o, d.id, d.text, [["Revisit it", `Let's revisit decision ${d.id} of spec ${o.rel}: `],
                                             ["Explain it", `Explain decision ${d.id} of spec ${o.rel}.`]])},
        h("div", {class: "d"}, d.id, d.date ? " · " + d.date : "", d.superseded_by ? " · superseded by " + d.superseded_by : ""),
        ...inline(d.text)))) : null;
    const terms = s.terms.length ? h("details", {class: "spec-more"}, h("summary", {}, `Glossary (${s.terms.length})`),
      ...s.terms.map(t => h("div", {class: "spec-term"}, h("b", {}, t.term), ": ", ...inline(t.text)))) : null;
    const files = s.files.map(f => h("details", {class: "spec-more"}, h("summary", {}, f),
      md(String(s.texts[f] || "").replace(HEADER, "").replace(ID_TOKEN, ""))));
    return [top, problems, now, ...secs, ...loose, decisions, terms, ...files].filter(Boolean);
  }

  function row(it, s, o) {
    const claim = it.claim ? h("span", {class: "spec-claim"}, "@" + it.claim) : null;
    const text = it.claim ? it.text.replace(/^@\S+\s*/, "") : it.text;
    const name = it.id || "this item";
    return h("button", {class: "spec-item " + it.state, onclick: () => ask(o, name, it.text, [
        ["Work on it", `Work on ${name} of spec ${o.rel}.`],
        ["Explain it", `Explain ${name} of spec ${o.rel}: where it stands and what's left.`],
        ["Drop it", `Drop ${name} from spec ${o.rel}: `]])},
      h("i", {"aria-hidden": "true"}, ICON[it.state] || "○"), h("span", {class: "t"}, claim, inline(text), it.id ? h("code", {}, it.id) : null));
  }

  function inline(text) {   // markdown for one line: its paragraph's contents, so code spans and links show as such
    const m = md(text), p = m.querySelector("p");
    return p && m.childElementCount === 1 ? [...p.childNodes] : [m];
  }

  function ask(o, id, text, actions) {   // a message to Claude, put in the box: never sent from here
    grovy.sheet(h("h2", {}, id), h("p", {style: "margin:0 0 14px;color:var(--mut)"}, text),
      ...actions.map(([label, msg]) => h("button", {class: "item", onclick: () => {
        grovy.closeSheet(); o.panel.close();
        setTimeout(() => { if (grovy.session === o.session) grovy.draft(msg); }, 50);
      }}, label)));
  }
});
