// Issues: one small fix, in a session of its own. ⋯ → New issue (home screen): pick a project, say what to fix;
// grovy makes a branch off the default branch in a git worktree of its own and starts Claude there with your words.
// ⋯ → Finish issue (in that session): see its commits, then merge them (fast-forward, done by grovy) and close it
// all (session, worktree, branch), or discard it.
//
// A built-in extension that uses only window.grovy; the work is grovy's own (grovylib/issues.py, /api/issues).
// To turn it off: grovy.disable("issues") in custom.js.
grovy.extension("issues", grovy => {
  const {h} = grovy;
  grovy.css(`
.iss-c { font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg); border-radius:8px; padding:8px 10px;
  margin:0 0 12px; white-space:pre-wrap; overflow-wrap:anywhere; max-height:14em; overflow:auto; }
.iss-meta { font-size:13px; color:var(--mut); margin:2px 0 12px; overflow-wrap:anywhere; }
.iss-meta b { color:var(--fg); font-family:ui-monospace,monospace; font-weight:600; }`);
  const base = p => p.replace(/\/+$/, "").split("/").pop() || p;
  const buttons = (...b) => h("div", {style: "display:flex;gap:10px;flex-wrap:wrap"}, ...b);
  const btn = (label, cls, run) => h("button", {class: "btn " + cls, onclick: run}, label);
  function home(msg) {   // the home screen closes any open sheet as it opens: say msg once it's there
    if (location.hash === "#/" || location.hash === "") { if (msg) grovy.say(msg); return; }
    if (msg) addEventListener("hashchange", () => grovy.say(msg), {once: true});
    location.hash = "#/";
  }

  grovy.menu("New issue", async () => {
    let projects;
    try { projects = (await grovy.api("/api/issues/projects")).projects; } catch (e) { return grovy.say(e.message); }
    if (!projects.length) return grovy.say("No project to start an issue in: an issue needs a git repository Claude " +
      "already trusts (run claude once in it on the computer, and accept its trust prompt).");
    let picked = projects.length === 1 ? projects[0] : null;
    const text = h("textarea", {class: "field", rows: 4, placeholder: "What to fix, e.g. “the time shown twice above my messages”"});
    const ask = h("input", {type: "checkbox"});
    const note = h("div", {class: "note"});
    const start = h("button", {class: "btn allow", disabled: ""}, "Start");
    let busy = false;   // one issue per tap, even if the text or project changes while it starts
    const valid = () => { start.disabled = busy || !(picked && text.value.trim()); };
    const list = grovy.picker({
      items: projects, title: p => base(p.path), sub: p => `from ${p.base} · ${p.path}`, recent: p => p.recent,
      selected: picked, placeholder: "Search projects…", onPick: p => { picked = p; valid(); }});
    text.oninput = valid;
    start.onclick = async () => {
      busy = true; valid(); note.className = "note"; note.textContent = "Making the worktree, starting Claude…";
      const words = text.value;
      try {
        const r = await grovy.api("/api/issues", {path: picked.path, text: words, ask: ask.checked});
        grovy.closeSheet();
        // couldn't be queued: once the session is open, put it in the message box to send yourself
        if (!r.queued) addEventListener("hashchange", () => grovy.draft(words), {once: true});
        location.hash = "#/s/" + encodeURIComponent(r.name);
      } catch (e) {
        if (!note.isConnected) grovy.say("The issue didn't start: " + e.message);   // the sheet was closed meanwhile
        else { note.className = "errline"; note.textContent = e.message; }
      } finally { busy = false; valid(); }
    };
    valid();
    grovy.sheet(h("h2", {}, "New issue"),
      h("p", {class: "iss-meta", style: "margin-top:0"}, "A branch in a worktree of its own, a session for it. When " +
        "Claude has committed the fix, ⋯ → Finish issue merges it and cleans up."),
      h("div", {class: "lbl"}, "Project"), list, h("div", {class: "lbl"}, "The issue"), text,
      h("label", {class: "toggle"}, ask, "Ask before running tools"), note, h("div", {class: "row"}, start));
    text.focus();
  }, {where: "home", sub: "A quick fix in a worktree of its own"});

  grovy.menu("Finish issue", async session => {
    let st;
    try { st = await grovy.api("/api/issues/" + encodeURIComponent(session)); } catch (e) { return grovy.say(e.message); }
    const what = h("p", {class: "iss-meta"}, h("b", {}, st.branch), " → ", h("b", {}, st.base), " · ",
      `${st.ahead} commit${st.ahead === 1 ? "" : "s"}`, st.behind ? ` · ${st.base} has ${st.behind} new` : "");
    const commits = st.commits.length ? h("div", {class: "iss-c"}, st.commits.join("\n")) : null;
    const draft = text => () => { grovy.closeSheet(); grovy.draft(text); };
    let why = null, fix = null;
    if (st.error) why = st.error;
    else if (st.dirty.length) {
      why = "There are uncommitted changes in the issue's worktree:";
      fix = btn("Ask Claude to commit", "allow", draft("Commit your changes on this branch (run the tests first)."));
    } else if (!st.ahead) why = "Nothing to merge yet: there are no commits on the issue's branch.";
    else if (st.behind) {
      why = `${st.base} has moved on since this issue started: Claude must rebase before it can be merged.`;
      fix = btn("Ask Claude to rebase", "allow", draft(`Rebase this branch onto ${st.base}, run the tests, and fix ` +
        "anything that breaks."));
    }
    const discard = btn("Discard", "deny", async () => {
      const sure = await grovy.ask(`Discard ${st.branch}? Its session stops, and its worktree and branch are ` +
        "deleted, commits and uncommitted changes included.", "Discard", {danger: true});
      if (!sure) return;
      try {
        const r = await grovy.api(`/api/issues/${encodeURIComponent(session)}/discard`, {});
        home(r.message === "discarded" ? "" : "Issue discarded, but " + r.message.replace(/^discarded; /, ""));
      } catch (e) { grovy.say(e.message); }
    });
    if (why) return grovy.sheet(h("h2", {}, "Finish issue"), what, commits, h("p", {style: "margin:0 0 10px"}, why),
      st.dirty.length ? h("div", {class: "iss-c"}, st.dirty.join("\n")) : null,
      buttons(...(fix ? [fix] : []), discard, btn("Close", "", grovy.closeSheet)));
    const close = btn("Close", "", grovy.closeSheet);
    const merge = btn(`Merge into ${st.base}`, "allow", async () => {
      const all = [merge, discard, close];
      all.forEach(b => { b.disabled = true; }); merge.textContent = "Merging…";
      try {
        // only the commits shown here: if Claude committed again since, grovy refuses and you look again
        const r = await grovy.api(`/api/issues/${encodeURIComponent(session)}/finish`, {tip: st.tip});
        home("Issue finished: " + r.message + ".");
      } catch (e) { grovy.say(e.message); }
    });
    grovy.sheet(h("h2", {}, "Finish issue"), what, commits,
      h("p", {class: "iss-meta"}, `Fast-forwards ${st.base} to these commits, then stops the session and deletes ` +
        "its worktree and branch. Nothing is pushed."),
      buttons(merge, discard, close));
  }, {where: "session", section: "Session", when: state => !!(state && state.issue)});
}, {about: "⋯ → New issue and Finish issue"});
