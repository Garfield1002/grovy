// The built-in widgets: what draws the ```widget blocks Claude puts in its replies (chart, stats, progress, note,
// image, log, checklist, choices, form, diagram), and the cards scripts show with `import grovy` (live widgets).
// Claude learns these types from WIDGET_GUIDE in grovylib/claude.py: change both together.
//
// A built-in extension: grovy loads every extensions/*.js before your ~/.config/grovy/custom.js, and they use only
// window.grovy, exactly like your own code can. Each renderer gets the widget's JSON (untrusted: it comes from Claude)
// and returns DOM built with h(), never HTML; bad input throws an Error with a readable message, which grovy shows in
// place of the widget. To replace one type, call grovy.widget("chart", ...) in custom.js; to turn them all off,
// grovy.disable("widgets").
grovy.extension("widgets", grovy => {
  const {h, md} = grovy;
  grovy.css(`
:root { --c1:#3a6fd8; --c2:#c96442; --c3:#2f8a4c; --c4:#8a5cc7; --c5:#c27c0e; --c6:#2a9aa8; }
@media (prefers-color-scheme: dark) { :root { --c1:#7aa2ff; --c2:#e08a67; --c3:#5cc47e; --c4:#b48cf0; --c5:#f0a63a; --c6:#5cc8d4; } }
.c1 { --c:var(--c1) } .c2 { --c:var(--c2) } .c3 { --c:var(--c3) } .c4 { --c:var(--c4) } .c5 { --c:var(--c5) } .c6 { --c:var(--c6) }
.wg-image { padding:6px; } .wg-image .wt { margin:4px 6px 8px; } .wcap { margin:6px 6px 2px; }
.wimg { display:block; } .wimg img { display:block; max-width:100%; max-height:70vh; margin:0 auto; border-radius:8px; }
.lg { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:13px; color:var(--mut); margin:0 0 8px; }
.lg span { display:inline-flex; align-items:center; gap:6px; } .lg i { width:10px; height:10px; border-radius:3px; background:var(--c); flex:none; }
.lg.col { flex-direction:column; gap:6px; color:var(--fg); font-size:14px; } .lg em { font-style:normal; color:var(--mut); margin-left:auto; padding-left:10px; }
.bars { display:flex; flex-direction:column; gap:9px; }
.bl { font-size:14px; margin-bottom:2px; overflow-wrap:anywhere; }
.bv { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--mut); }
.bv b { display:block; height:12px; min-width:2px; border-radius:3px; background:var(--c); flex:none; max-width:calc(100% - 70px); }
.bv b.neg { opacity:.55; }
.pie { display:flex; align-items:center; gap:16px; flex-wrap:wrap; } .pie svg { width:120px; height:120px; flex:none; }
.pie path { fill:var(--c); } .pie .ring { stroke:var(--c); } .pie .lg { flex:1; min-width:150px; margin:0; }
svg.line { width:100%; height:auto; display:block; } svg text { fill:var(--mut); font-size:11px; font-family:inherit; }
svg.line .grid { stroke:var(--line); } svg.line .ln { fill:none; stroke:var(--c); stroke-width:2.2; stroke-linejoin:round; }
svg.line .dot { fill:var(--card); stroke:var(--c); stroke-width:2; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:8px; }
.tile { padding:10px 12px; border-radius:10px; background:var(--bg); }
.tv { font-size:22px; font-weight:700; line-height:1.2; overflow-wrap:anywhere; } .tl { font-size:14px; color:var(--mut); }
.tv.ok { color:var(--ok) } .tv.warn { color:var(--warn) } .tv.bad { color:var(--bad) }
.pg { margin:0 0 10px; } .pg:last-child { margin:0; }
.pgh { display:flex; justify-content:space-between; gap:10px; font-size:14px; margin-bottom:4px; }
.pg i { display:block; height:8px; border-radius:4px; background:var(--line); overflow:hidden; }
.pg b { display:block; height:100%; background:var(--busy); } .pg b.ok { background:var(--ok) } .pg b.warn { background:var(--warn) } .pg b.bad { background:var(--bad) }
.pg b.ind { width:30%; animation:ind 1.4s ease-in-out infinite alternate; } .pg .wn { margin-top:3px; }
@keyframes ind { from { margin-left:0 } to { margin-left:70% } }
@media (prefers-reduced-motion: reduce) { .pg b.ind { animation:none; width:100%; opacity:.5 } }
.wg-note { border-left:4px solid var(--line); } .wg-note.ok { border-left-color:var(--ok) } .wg-note.warn { border-left-color:var(--warn) } .wg-note.bad { border-left-color:var(--bad) }
.wg-note .md > :first-child { margin-top:0 } .wg-note .md > :last-child { margin-bottom:0 }
.wlog { display:flex; flex-direction:column-reverse; max-height:16em; overflow:auto; background:var(--bg); border-radius:8px; padding:8px 10px; }
.wlog pre { margin:0; font:12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
.ck { display:flex; flex-direction:column; gap:7px; } .ci { display:flex; gap:10px; align-items:flex-start; }
.ci .ic { width:20px; flex:none; text-align:center; font-weight:700; color:var(--faint); }
.ci.done .ic { color:var(--ok) } .ci.failed .ic { color:var(--bad) } .ci.running .ic { color:var(--busy) }
.ci.done > div > div:first-child, .ci.skipped > div > div:first-child { color:var(--mut); }
.opt { display:flex; flex-direction:column; align-items:flex-start; width:100%; text-align:left; padding:10px 12px; margin:0 0 8px;
  min-height:44px; border:1px solid var(--line); border-radius:10px; background:var(--bg); }
.opt span { font-size:13px; color:var(--mut); } .opt.on { border-color:var(--busy); box-shadow:inset 0 0 0 1px var(--busy); }
.wact { display:flex; margin-top:4px; }
.fr { display:flex; flex-direction:column; gap:4px; margin:0 0 10px; font-size:14px; }
.fr input, .fr select, .fr textarea { font:inherit; font-size:16px; color:inherit; background:var(--bg); border:1px solid var(--line);
  border-radius:8px; padding:9px 10px; min-height:42px; width:100%; }
.fr.tg { flex-direction:row; align-items:center; gap:10px; } .fr.tg input { width:22px; min-height:22px; height:22px; flex:none; }
.dg { overflow-x:auto; } .dg svg { display:block; width:100%; height:auto; margin:0 auto; } .dg.wide svg { width:auto; max-width:none; }
.dg .nd rect { fill:var(--bg); stroke:var(--faint); } .dg .nd text { fill:var(--fg); font-size:12.5px; }
.dg .nd.ok rect { stroke:var(--ok); stroke-width:1.6 } .dg .nd.warn rect { stroke:var(--warn); stroke-width:1.6 } .dg .nd.bad rect { stroke:var(--bad); stroke-width:1.6 }
.dg .ed { fill:none; stroke:var(--faint); stroke-width:1.4; } .dg .ah { fill:var(--faint); }
.dg .el { font-size:11px; paint-order:stroke; stroke:var(--card); stroke-width:4px; stroke-linejoin:round; }`);

  const SVGNS = "http://www.w3.org/2000/svg";
  function sv(tag, attrs, ...kids) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
    for (const k of kids.flat(Infinity)) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
    return e;
  }
  // checks: each throws a readable Error on bad input
  const wtext = (v, max = 400) => { if (v == null) return ""; if (typeof v === "object") throw new Error("expected text, got " + JSON.stringify(v).slice(0, 40)); return String(v).slice(0, max); };
  const wnum = (v, what) => { const n = typeof v === "string" && v.trim() !== "" ? Number(v) : v;
    if (typeof n !== "number" || !isFinite(n)) throw new Error(what + " must be a number"); return n; };
  const warr = (v, what, max) => { if (!Array.isArray(v) || !v.length) throw new Error(what + " must be a non-empty list");
    if (v.length > max) throw new Error(what + `: at most ${max}`); return v; };
  const wobj = (v, what) => { if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error(what + " must be an object"); return v; };
  const TONES = new Set(["ok", "warn", "bad"]);
  const tone = t => TONES.has(t) ? t : "";
  const fmt = n => Math.abs(n) >= 1e6 || (Math.abs(n) < 1e-3 && n) ? n.toPrecision(3) : String(+n.toFixed(Math.abs(n) < 10 ? 2 : Math.abs(n) < 1000 ? 1 : 0));
  const wtitle = t => t ? h("div", {class: "wt"}, wtext(t, 200)) : null;
  const dur = s => { s = Math.max(0, Math.round(s)); return s < 60 ? s + "s" : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`
    : `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m`; };
  let widgetSeq = 0;   // unique ids within the page (form fields, diagram arrowheads)

  const WIDGETS = {
    chart(s) {
      const labels = warr(s.labels, "labels", 60).map(l => wtext(l, 60));
      const series = warr(s.series, "series", 6).map((x, i) => {
        wobj(x, "series item");
        const values = warr(x.values, "series values", 60).map(v => wnum(v, "values"));
        if (values.length !== labels.length) throw new Error(`series ${i + 1} has ${values.length} values for ${labels.length} labels`);
        return {name: wtext(x.name, 60), values, c: "c" + (i % 6 + 1)};
      });
      const unit = s.unit ? " " + wtext(s.unit, 12) : "";
      const legend = series.length > 1 ? h("div", {class: "lg"}, series.map(x => h("span", {}, h("i", {class: x.c}), x.name || "series"))) : null;
      const kind = s.kind || "bar";
      if (kind === "bar") {
        const max = Math.max(...series.flatMap(x => x.values.map(Math.abs))) || 1;
        return [wtitle(s.title), legend, h("div", {class: "bars"}, labels.map((l, i) => h("div", {class: "br"},
          h("div", {class: "bl"}, l),
          series.map(x => h("div", {class: "bv"},
            h("b", {class: x.c + (x.values[i] < 0 ? " neg" : ""), style: `width:${Math.abs(x.values[i]) / max * 100}%`}),
            h("span", {}, fmt(x.values[i]) + unit))))))];
      }
      if (kind === "pie") {
        const x = series[0];
        if (x.values.some(v => v < 0)) throw new Error("pie values can't be negative");
        if (labels.length > 12) throw new Error("pie: at most 12 slices");
        const total = x.values.reduce((a, b) => a + b, 0);
        if (!total) throw new Error("pie values add up to 0");
        let a = -Math.PI / 2;
        const pt = (r, t) => `${(60 + r * Math.cos(t)).toFixed(2)} ${(60 + r * Math.sin(t)).toFixed(2)}`;
        const arcs = x.values.map((v, i) => {
          const b = a + v / total * 2 * Math.PI, big = b - a > Math.PI ? 1 : 0, c = "c" + (i % 6 + 1);
          const d = v >= total ? null
            : `M${pt(56, a)}A56 56 0 ${big} 1 ${pt(56, b)}L${pt(34, b)}A34 34 0 ${big} 0 ${pt(34, a)}Z`;
          a = b;
          return v ? (d ? sv("path", {d, class: c}) : sv("circle", {cx: 60, cy: 60, r: 45, fill: "none", "stroke-width": 22, class: "ring " + c})) : null;
        });
        return [wtitle(s.title), h("div", {class: "pie"}, sv("svg", {viewBox: "0 0 120 120", role: "img"}, arcs),
          h("div", {class: "lg col"}, labels.map((l, i) => h("span", {}, h("i", {class: "c" + (i % 6 + 1)}), l,
            h("em", {}, fmt(x.values[i]) + unit + " · " + Math.round(x.values[i] / total * 100) + "%")))))];
      }
      if (kind === "line") {
        const all = series.flatMap(x => x.values);
        let lo = Math.min(...all), hi = Math.max(...all);
        if (lo === hi) { lo -= 1; hi += 1; }
        if (lo > 0 && lo < hi / 2) lo = 0;                     // round axis: 0, 25, 50 rather than 7, 25.5, 44
        const mag = 10 ** Math.floor(Math.log10((hi - lo) / 2)), step = [1, 2, 2.5, 5, 10].map(k => k * mag).find(k => (hi - lo) / k <= 4);
        lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
        const W = 340, H = 180, L = 40, R = 10, T = 10, B = 24, n = labels.length;
        const X = i => L + (n === 1 ? (W - L - R) / 2 : i * (W - L - R) / (n - 1)), Y = v => T + (hi - v) / (hi - lo) * (H - T - B);
        const ticks = []; for (let t = lo; t <= hi + step / 2; t += step) ticks.push(+t.toPrecision(12));
        const xl = [...new Set([0, Math.floor((n - 1) / 2), n - 1])];
        return [wtitle(s.title), legend, sv("svg", {viewBox: `0 0 ${W} ${H}`, class: "line", role: "img"},
          ticks.map(t => [sv("line", {x1: L, x2: W - R, y1: Y(t), y2: Y(t), class: "grid"}),
            sv("text", {x: L - 6, y: Y(t) + 4, "text-anchor": "end"}, fmt(t))]),
          xl.map(i => sv("text", {x: X(i), y: H - 6, "text-anchor": n === 1 ? "middle" : i === 0 ? "start" : i === n - 1 ? "end" : "middle"}, labels[i].slice(0, 16))),
          series.map(x => [sv("polyline", {points: x.values.map((v, i) => X(i) + "," + Y(v)).join(" "), class: "ln " + x.c}),
            n <= 24 ? x.values.map((v, i) => sv("circle", {cx: X(i), cy: Y(v), r: 3, class: "dot " + x.c}, sv("title", {}, `${labels[i]}: ${fmt(v)}${unit}`))) : null])),
          unit ? h("div", {class: "wn"}, "values in" + unit) : null];
      }
      throw new Error(`unknown chart kind ${JSON.stringify(kind)} (bar, line, pie)`);
    },

    stats(s) {
      return [wtitle(s.title), h("div", {class: "tiles"}, warr(s.items, "items", 12).map(it => { wobj(it, "item");
        return h("div", {class: "tile"}, h("div", {class: "tv " + tone(it.tone)}, wtext(it.value, 30)),
          h("div", {class: "tl"}, wtext(it.label, 60)), it.note ? h("div", {class: "wn"}, wtext(it.note, 120)) : null); }))];
    },

    progress(s, live) {
      let eta = null;
      const items = warr(s.items, "items", 20).map(it => { wobj(it, "item");
        const v = wnum(it.value, "value"), ind = it.indeterminate === true, max = it.max == null || ind ? 100 : wnum(it.max, "max");
        const pct = max > 0 ? Math.max(0, Math.min(100, v / max * 100)) : 0;
        if (eta === null && !ind && it.max != null && max > 0) eta = [v, max];
        const running = live && live.status === "running";
        return h("div", {class: "pg"}, h("div", {class: "pgh"}, h("span", {}, wtext(it.label, 80)),
            h("span", {class: "wn"}, ind ? fmt(v) : it.max == null ? fmt(v) + "%" : fmt(v) + " / " + fmt(max))),
          h("i", {}, ind && running ? h("b", {class: "ind"})
            : h("b", {class: tone(it.tone) || (live && live.status === "failed" ? "bad" : pct >= 100 || ind ? "ok" : ""), style: `width:${ind ? 100 : pct}%`})),
          it.note != null ? h("div", {class: "wn"}, wtext(it.note, 200)) : null); });
      let time = null;
      if (s.eta === true && live && wnum(live.updated, "updated") >= wnum(live.created, "created")) {
        const took = live.updated - live.created;
        if (live.status !== "running") time = (live.status === "done" ? "took " : "ran for ") + dur(took);
        else if (eta && eta[0] > 0 && eta[0] < eta[1] && took >= 2) time = dur(took) + " elapsed · about " + dur(took * (eta[1] - eta[0]) / eta[0]) + " left";
        else time = dur(took) + " elapsed";
      }
      return [wtitle(s.title), items, time ? h("div", {class: "wn"}, time) : null];
    },

    note(s) { return [wtitle(s.title), md(wtext(s.text, 20000))]; },
    image(s) {   // src: what `grovy image FILE` printed (a copy kept by grovy); tap for full size
      const src = wtext(s.src, 300);
      if (!/^[A-Za-z0-9_-]+\/\d{8}-\d{6}-[0-9a-f]{4}-[A-Za-z0-9._-]+$/.test(src))
        throw new Error("src must be what `grovy image FILE` printed");
      const url = "/api/images/" + src.split("/").map(encodeURIComponent).join("/");
      const link = h("a", {class: "wimg", href: url, target: "_blank", rel: "noopener"});
      link.append(h("img", {src: url, alt: wtext(s.caption, 300), loading: "lazy",
        onerror: () => link.replaceWith(h("div", {class: "wn"}, "This image is no longer available."))}));
      return [wtitle(s.title), link, s.caption ? h("div", {class: "wn wcap"}, wtext(s.caption, 300)) : null];
    },

    log(s) {
      const lines = Array.isArray(s.lines) ? s.lines : (() => { throw new Error("lines must be a list"); })();
      if (lines.length > 200) throw new Error("lines: at most 200");
      return [wtitle(s.title), h("div", {class: "wlog"}, h("pre", {}, lines.map(l => wtext(l, 500)).join("\n") || "(no output yet)"))];
    },

    checklist(s) {
      const ICON = {done: "✓", todo: "○", running: "◐", failed: "✕", skipped: "–"};
      return [wtitle(s.title), h("div", {class: "ck"}, warr(s.items, "items", 60).map(it => { wobj(it, "item");
        const st = ICON[it.status] ? it.status : "todo";
        return h("div", {class: "ci " + st}, h("span", {class: "ic"}, ICON[st]),
          h("div", {}, h("div", {}, wtext(it.label, 200)), it.note ? h("div", {class: "wn"}, wtext(it.note, 300)) : null)); }))];
    },

    choices(s) {
      const opts = warr(s.options, "options", 12).map(o => { wobj(o, "option");
        const label = wtext(o.label, 120); if (!label) throw new Error("every option needs a label");
        return {label, desc: wtext(o.description, 300), msg: wtext(o.message, 4000) || label}; });
      const q = wtext(s.question, 400);
      const btn = o => h("button", {class: "opt"}, h("b", {}, o.label), o.desc ? h("span", {}, o.desc) : null);
      if (!s.multi) return [wtitle(q || s.title), opts.map(o => { const b = btn(o); b.onclick = () => grovy.draft(o.msg); return b; })];
      const picked = new Set();
      const use = h("button", {class: "btn allow", disabled: ""}, "Use selection");
      use.onclick = () => grovy.draft(opts.filter((_, i) => picked.has(i)).map(o => o.msg).join("\n"));
      return [wtitle(q || s.title), h("div", {class: "wn", style: "margin:-6px 0 8px"}, "Pick any, then use the selection."), opts.map((o, i) => { const b = btn(o);
        b.onclick = () => { picked.has(i) ? picked.delete(i) : picked.add(i); b.classList.toggle("on", picked.has(i));
          use.disabled = !picked.size; };
        return b; }), h("div", {class: "wact"}, use)];
    },

    form(s) {
      const id = "wf" + (++widgetSeq);
      const fields = warr(s.fields, "fields", 12).map((f, i) => { wobj(f, "field");
        const label = wtext(f.label || f.name, 100), kind = f.kind || "text", def = f.default;
        let el;
        if (kind === "select") {
          const opts = warr(f.options, `options of "${label}"`, 30).map(o => wtext(o, 100));
          el = h("select", {}, opts.map(o => h("option", {}, o))); if (def != null && opts.includes(String(def))) el.value = String(def);
        } else if (kind === "toggle") { el = h("input", {type: "checkbox"}); el.checked = !!def; }
        else if (kind === "textarea") { el = h("textarea", {rows: 3}); el.value = def == null ? "" : wtext(def, 4000); }
        else if (kind === "text" || kind === "number") {
          el = h("input", {type: kind === "number" ? "number" : "text", inputmode: kind === "number" ? "decimal" : null});
          el.value = def == null ? "" : wtext(def, 1000);
        } else throw new Error(`unknown field kind ${JSON.stringify(kind)} (text, textarea, number, select, toggle)`);
        el.id = id + "-" + i;
        return {label, el, kind};
      });
      const submit = h("button", {class: "btn allow"}, wtext(s.submit, 40) || "Use these answers");
      submit.onclick = () => grovy.draft([s.title ? wtext(s.title, 200) : null,
        ...fields.map(f => `${f.label}: ${f.kind === "toggle" ? (f.el.checked ? "yes" : "no") : f.el.value.trim() || "(blank)"}`)]
        .filter(Boolean).join("\n"));
      return [wtitle(s.title), fields.map(f => f.kind === "toggle"
          ? h("label", {class: "fr tg", for: f.el.id}, f.el, h("span", {}, f.label))
          : h("label", {class: "fr", for: f.el.id}, h("span", {}, f.label), f.el)),
        h("div", {class: "wact"}, submit)];
    },

    diagram(s) {
      const nodes = warr(s.nodes, "nodes", 40).map(n => { wobj(n, "node");
        return {id: wtext(n.id, 60), label: wtext(n.label == null ? n.id : n.label, 80), tone: tone(n.tone)}; });
      const byId = new Map();
      for (const n of nodes) { if (!n.id) throw new Error("every node needs an id"); if (byId.has(n.id)) throw new Error(`duplicate node id "${n.id}"`); byId.set(n.id, n); }
      const edges = (s.edges == null ? [] : Array.isArray(s.edges) ? s.edges : (() => { throw new Error("edges must be a list"); })())
        .slice(0, 80).map(e => { wobj(e, "edge");
          const a = byId.get(wtext(e.from, 60)), b = byId.get(wtext(e.to, 60));
          if (!a || !b) throw new Error(`edge ${JSON.stringify(e.from)} -> ${JSON.stringify(e.to)} names an unknown node`);
          return {a, b, label: wtext(e.label, 40)}; });
      // layers: longest path from the sources; edges that close a cycle are drawn but don't push layers
      const idx = new Map(nodes.map((n, i) => [n, i])), state = new Map(), forward = [];
      const visit = n => { state.set(n, 1);
        for (const e of edges) if (e.a === n) { const st = state.get(e.b);
          if (st === 1) continue; forward.push(e); if (!st) visit(e.b); }
        state.set(n, 2); };
      nodes.forEach(n => state.get(n) || visit(n));
      const layer = new Map(nodes.map(n => [n, 0]));
      for (let k = 0; k < nodes.length; k++) for (const e of forward) if (e.a !== e.b) layer.set(e.b, Math.max(layer.get(e.b), layer.get(e.a) + 1));
      const rows = [];
      nodes.forEach(n => (rows[layer.get(n)] ||= []).push(n));
      const pos = new Map();
      const place = () => rows.forEach(r => r.forEach((n, i) => pos.set(n, i)));
      place();
      for (let sweep = 0; sweep < 4; sweep++) {   // barycenter ordering, alternating down and up
        const down = sweep % 2 === 0;
        for (const r of down ? rows.slice(1) : rows.slice(0, -1).reverse()) {
          const bc = n => { const nb = forward.filter(e => down ? e.b === n : e.a === n).map(e => pos.get(down ? e.a : e.b));
            return nb.length ? nb.reduce((a, b) => a + b, 0) / nb.length : pos.get(n); };
          const key = new Map(r.map(n => [n, bc(n)]));
          r.sort((x, y) => key.get(x) - key.get(y) || idx.get(x) - idx.get(y));
          place();
        }
      }
      // geometry: wrap labels, size boxes, center each row
      const right = s.direction === "right";
      const wrap = t => { const out = []; let line = "";
        for (const w of t.split(/\s+/)) { if (line && (line + " " + w).length > 16) { out.push(line); line = w; } else line = line ? line + " " + w : w; }
        if (line) out.push(line); return out.slice(0, 3).map(l => l.length > 18 ? l.slice(0, 17) + "…" : l); };
      const box = new Map(nodes.map(n => { const lines = wrap(n.label);
        return [n, {lines, w: Math.max(48, Math.max(...lines.map(l => l.length)) * 7.2 + 20), h: lines.length * 16 + 14}]; }));
      const GAP = 14, LG = right ? 56 : 44;
      const span = r => r.reduce((a, n) => a + (right ? box.get(n).h : box.get(n).w), 0) + GAP * (r.length - 1);
      const thick = r => Math.max(...r.map(n => right ? box.get(n).w : box.get(n).h));
      const cross = Math.max(...rows.map(span));
      let along = 8;
      rows.forEach(r => { let c = 8 + (cross - span(r)) / 2; const t = thick(r);
        r.forEach(n => { const b = box.get(n);
          if (right) { b.x = along + (t - b.w) / 2; b.y = c; c += b.h + GAP; } else { b.x = c; b.y = along + (t - b.h) / 2; c += b.w + GAP; } });
        along += t + LG; });
      // backward edges loop out to the side: make room for them
      const reach = e => right ? Math.abs(box.get(e.b).x - box.get(e.a).x) : Math.abs(box.get(e.b).y - box.get(e.a).y);
      const pad = Math.max(0, ...edges.filter(e => e.a !== e.b && layer.get(e.b) <= layer.get(e.a))
        .map(e => 0.75 * (40 + 0.3 * reach(e)) + (e.label ? e.label.length * 3.4 + 4 : 4)));
      for (const b of box.values()) right ? b.y += pad : b.x += pad;
      const W = (right ? along - LG : cross + 2 * pad) + 16, H = (right ? cross + 2 * pad : along - LG) + 16;   // stays centred
      const mid = "wa" + (++widgetSeq);
      const port = (n, out) => { const b = box.get(n);
        return right ? [out ? b.x + b.w : b.x, b.y + b.h / 2] : [b.x + b.w / 2, out ? b.y + b.h : b.y]; };
      const edgeEls = edges.map(e => {
        if (e.a === e.b) return null;
        const back = layer.get(e.b) <= layer.get(e.a);
        const [x1, y1] = port(e.a, !back), [x2, y2] = port(e.b, back);
        let d;
        if (!back) { const m = right ? (x1 + x2) / 2 : (y1 + y2) / 2;
          d = right ? `M${x1} ${y1}C${m} ${y1} ${m} ${y2} ${x2} ${y2}` : `M${x1} ${y1}C${x1} ${m} ${x2} ${m} ${x2} ${y2}`; }
        else { const o = 40 + 0.3 * (right ? Math.abs(x2 - x1) : Math.abs(y2 - y1)); d = right ? `M${x1} ${y1}C${x1} ${y1 - o} ${x2} ${y2 - o} ${x2} ${y2}` : `M${x1} ${y1}C${x1 - o} ${y1} ${x2 - o} ${y2} ${x2} ${y2}`; }
        const bo = back ? 0.75 * (40 + 0.3 * (right ? Math.abs(x2 - x1) : Math.abs(y2 - y1))) : 0;
        const lx = (x1 + x2) / 2 - (right ? 0 : bo), ly = (y1 + y2) / 2 - (right ? bo : 0);
        return [sv("path", {d, class: "ed", "marker-end": `url(#${mid})`}),
          e.label ? sv("text", {x: lx, y: ly + 4, "text-anchor": "middle", class: "el"}, e.label) : null];
      });
      const svg = sv("svg", {viewBox: `0 0 ${W} ${H}`, width: W, role: "img", style: `max-width:${W}px`},
        sv("defs", {}, sv("marker", {id: mid, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse"},
          sv("path", {d: "M0 0L10 5L0 10z", class: "ah"}))),
        edgeEls,
        nodes.map(n => { const b = box.get(n);
          return sv("g", {class: "nd " + n.tone}, sv("rect", {x: b.x, y: b.y, width: b.w, height: b.h, rx: 8}),
            b.lines.map((l, i) => sv("text", {x: b.x + b.w / 2, y: b.y + 20 + i * 16, "text-anchor": "middle"}, l))); }));
      return [wtitle(s.title), h("div", {class: "dg" + (W > 520 ? " wide" : "")}, svg)];
    },
  };
  for (const [type, render] of Object.entries(WIDGETS)) grovy.widget(type, render);
}, {about: "Charts, checklists and other widgets in replies"});
