// New project: ＋ → New project (home screen) makes a blank git repository, or clones one, in the projects folder
// (~/dev), and starts a session in it. A new repository is a trust boundary of its own, so Claude asks whether to
// trust it: grovy answers yes for you, at once for a blank one, and for a clone only after you've seen what in it
// Claude would load (its own settings can run commands).
//
// A built-in extension that uses only window.grovy; the work is grovy's own (grovylib/newproject.py,
// /api/projects/new). To turn it off: grovy.disable("projects") in custom.js.
grovy.extension("projects", grovy => {
  const {h} = grovy;
  grovy.css(`
.np-kind { display:flex; gap:8px; margin:0 0 14px; }
.np-kind .opt { flex:1; text-align:center; margin:0; }
.np-kind .opt.sel::before { content:none; }
.np-files { font-size:13px; background:var(--bg); border-radius:8px; padding:8px 10px; margin:0 0 12px; max-height:16em;
  overflow:auto; }
.np-files div { margin:2px 0; overflow-wrap:anywhere; }
.np-files b { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-weight:600; }
.np-files span { color:var(--mut); }
.np-files .np-run b { color:var(--bad, #c33); }
.np-files code { display:block; margin:2px 0 4px 12px; font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap; overflow-wrap:anywhere; }
.np-note:empty { display:none; }`);

  // Open the session once it's there; the first message waits in the message box if it couldn't be queued.
  function open(r, words) {
    grovy.closeSheet();
    if (words && !r.queued) addEventListener("hashchange", () => grovy.draft(words), {once: true});
    if (r.warning) addEventListener("hashchange", () => grovy.say(r.warning), {once: true});
    location.hash = "#/s/" + encodeURIComponent(r.name);
  }

  grovy.menu("New project", async () => {
    let dir = "~/dev";
    try { dir = (await grovy.api("/api/projects/new")).dir; } catch (e) { /* shown as ~/dev */ }
    let kind = "blank", busy = false;
    const name = h("input", {class: "field", placeholder: "Name", autocapitalize: "off", autocorrect: "off",
                             spellcheck: "false", style: "margin-bottom:12px"});
    const url = h("input", {class: "field", placeholder: "https://github.com/owner/repo.git or git@host:owner/repo",
                            autocapitalize: "off", autocorrect: "off", spellcheck: "false", inputmode: "url",
                            style: "margin-bottom:12px"});
    const text = h("textarea", {class: "field", rows: 3, placeholder: "First message for Claude (optional)"});
    const ask = h("input", {type: "checkbox"});
    const note = h("div", {class: "note np-note"});
    const where = h("p", {class: "note", style: "margin:-6px 0 12px"});
    const go = h("button", {class: "btn allow"});
    const kinds = h("div", {class: "np-kind"});
    const urlBox = h("div", {}, url);
    const valid = () => {
      const n = name.value.trim() || (kind === "clone" ? url.value.trim().replace(/\/+$/, "").split(/[\/:]/).pop()
                                                         .replace(/\.git$/, "") : "");
      where.textContent = n ? `In ${dir}/${n}` : `In ${dir}`;
      go.textContent = kind === "clone" ? "Clone" : "Create";
      go.disabled = busy || (kind === "clone" ? !url.value.trim() : !name.value.trim());
    };
    const draw = () => {
      kinds.replaceChildren(...[["blank", "Blank"], ["clone", "From a remote"]].map(([k, label]) =>
        h("button", {class: "opt" + (k === kind ? " sel" : ""), onclick: () => { kind = k; draw(); }}, label)));
      urlBox.hidden = kind !== "clone";
      name.placeholder = kind === "clone" ? "Name (optional: the repository's)" : "Name";
      valid();
    };
    name.oninput = url.oninput = valid;
    const fail = e => {
      if (!note.isConnected) return grovy.say(e.message);   // the sheet was closed meanwhile
      note.className = "errline np-note"; note.textContent = e.message;
    };
    go.onclick = async () => {
      busy = true; valid(); note.className = "note np-note";
      note.textContent = kind === "clone" ? "Cloning…" : "Creating, starting Claude…";
      const words = text.value.trim(), asking = ask.checked;
      try {
        const made = await grovy.api("/api/projects/new", {kind, name: name.value.trim(), url: url.value.trim()});
        if (made.kind === "clone") return review(made, words, asking);
        open(await grovy.api("/api/projects/start", {path: made.path, token: made.token, text: words, ask: asking}),
             words);
      } catch (e) { fail(e); } finally { busy = false; valid(); }
    };
    draw();
    grovy.sheet(h("h2", {}, "New project"), kinds, urlBox, name, where,
      text, h("label", {class: "toggle"}, ask, "Ask before running tools"), note, h("div", {class: "row"}, go));
    name.focus();
  }, {where: "new", sub: "A blank repository, or a clone"});

  // A clone is only trusted once you've seen what Claude would load from it; closing this sheet deletes the clone.
  function review(made, words, asking) {
    let decided = false;
    const files = made.review.length
      ? h("div", {class: "np-files"}, ...made.review.map(f =>
          h("div", {class: f.runs ? "np-run" : ""}, h("b", {}, f.file), h("span", {}, " · " + f.what),
            ...(f.commands || []).map(c => h("code", {}, c)))))
      : h("p", {class: "note"}, "It has no Claude settings or instructions of its own.");
    const note = h("div", {class: "note np-note"});
    const trust = h("button", {class: "btn allow"}, "Trust and start");
    const drop = h("button", {class: "btn deny"}, "Delete the clone");
    const all = [trust, drop];
    trust.onclick = async () => {
      decided = true; all.forEach(b => { b.disabled = true; });
      note.className = "note np-note"; note.textContent = "Starting Claude, answering its trust question…";
      try {
        open(await grovy.api("/api/projects/start", {path: made.path, token: made.token, text: words, ask: asking,
                                                     trust: true}), words);
      } catch (e) {
        if (!note.isConnected) { grovy.say("Claude didn't start, so the clone is deleted: " + e.message); return discard(); }
        decided = false; all.forEach(b => { b.disabled = false; });
        note.className = "errline np-note"; note.textContent = e.message;
      }
    };
    const discard = async () => {
      try {
        const r = await grovy.api("/api/projects/discard", {path: made.path, token: made.token});
        if (r.message !== "deleted") grovy.say(r.message);
      } catch (e) { grovy.say("The clone wasn't deleted: " + e.message); }
    };
    drop.onclick = () => { decided = true; grovy.closeSheet(); discard(); };
    grovy.sheet(h("h2", {}, "Trust this repository?"),
      h("p", {style: "margin:0 0 10px;color:var(--mut);font-size:13px;overflow-wrap:anywhere"},
        `Cloned into ${made.path}. Once trusted, Claude loads these files from it; settings and MCP servers can ` +
        "run commands on this computer (the ones they name are shown)."),
      files, note, h("div", {style: "display:flex;gap:10px;flex-wrap:wrap"}, trust, drop));
    // Closed another way (Cancel, a tap outside, another page): the clone goes, untrusted.
    const t = setInterval(() => {
      if (note.isConnected) return;
      clearInterval(t);
      if (!decided) discard();
    }, 500);
  }
}, {about: "＋ → New project"});
