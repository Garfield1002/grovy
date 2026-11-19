// The context bar: a thin line just above the message box showing how much of Claude's context window is used
// (green, amber past 25%, red past 75%). Tap it for the session's status: model, effort, fast mode.
//
// A built-in extension: grovy loads every extensions/*.js before your ~/.config/grovy/custom.js, and they use only
// window.grovy, exactly like your own code can. To turn this one off, put grovy.disable("context-bar") in custom.js;
// to change it, copy it there under another name.
grovy.extension("context-bar", grovy => {
  const {h} = grovy;
  // In place of the composer's top border; the tap area is taller than the line. The line is positioned against the
  // composer (left/right 0) but keeps its place in the flow (no top), so it spans the whole width whatever the column's
  // max-width.
  grovy.css(`
.ctxbar { display:block; width:100%; height:12px; margin:-2px 0 2px; padding:0; border:0; background:none; cursor:pointer; }
.ctxbar[hidden] { display:none; }
#composer:has(.ctxbar:not([hidden])) { border-top-color:transparent; }
.ctxbar i { position:absolute; left:0; right:0; display:block; height:3px; background:var(--line); }
.ctxbar b { display:block; height:100%; background:var(--ok); transition:width .4s ease; }
.ctxbar.warn b { background:var(--warn); } .ctxbar.bad b { background:var(--bad); }`);
  const level = c => c > 75 ? "bad" : c > 25 ? "warn" : "ok";
  const fill = h("b");
  const bar = h("button", {class: "ctxbar", hidden: "", onclick: () => details()}, h("i", {}, fill));
  let meta = null;   // the status line Claude Code last reported (model, effort, fast, context)

  grovy.slot("composer").append(bar);
  grovy.on("route", () => { meta = null; bar.hidden = true; });
  grovy.on("update", state => {
    meta = state.meta || null;
    if (!meta || meta.context == null) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.className = "ctxbar " + level(meta.context);
    fill.style.width = meta.context + "%";
    bar.setAttribute("aria-label", meta.context + "% of the context window used: tap for details");
  });

  function details() {
    const m = meta || {};
    const row = (k, v) => v == null || v === "" ? null : h("div", {class: "kv2"}, h("span", {}, k), h("span", {}, v));
    const big = m.context == null ? null : h("div", {style: "margin:4px 0 14px"},
      h("div", {style: "height:6px;border-radius:3px;background:var(--line);overflow:hidden"},
        h("div", {style: `height:100%;width:${m.context}%;background:var(--${level(m.context)})`})));
    grovy.sheet(h("h2", {}, "Session status"), big,
      row("Context used", m.context == null ? null : m.context + "%"), row("Model", m.model), row("Effort", m.effort),
      row("Fast mode", m.fast ? "on" : "off"),
      h("div", {class: "note", style: "margin-top:10px;color:var(--mut);font-size:13px"},
        "Near 100% Claude compacts the conversation to free up context."));
  }
}, {about: "The context-use bar above the message box (tap it for the model)"});
