// Is grovy up to date? grovyd asks GitHub once a day for the newest tagged release of grovy's repo (see
// grovylib/update.py) and this shows it under ⚙ Settings: "Update available" when there's a newer tag, else
// "Check for updates". Tap it for the versions, when it last checked, a link to the release, and a "Check now" button.
//
// A built-in extension (window.grovy only, like your own custom.js). Turn it off with grovy.disable("updates") in
// custom.js, or in ⚙ → Extensions. Opt out of the check itself (no GitHub request at all) with GROVY_UPDATE_CHECK=off;
// check a different repo's tags with GROVY_UPDATE_REPO=owner/repo.
grovy.extension("updates", grovy => {
  const {h} = grovy;
  let info = null;   // the last /api/update result, or null until the first load / after a failure

  async function load() {
    try { info = await grovy.api("/api/update"); }
    catch (e) { info = null; }
  }
  load();
  grovy.on("route", session => { if (!session) load(); });   // back on the home screen: refresh the status

  // Shown in ⚙ Settings only when the check is on (GROVY_UPDATE_CHECK). Two labels so an available update is visible
  // at a glance; both open the same sheet. `when` runs each time the sheet opens, so it reflects the latest load().
  const on = () => info && info.enabled;
  grovy.menu("Update available", () => sheet(), {where: "home", section: "grovy", when: () => !!(on() && info.behind)});
  grovy.menu("Check for updates", () => sheet(), {where: "home", section: "grovy", when: () => !!(on() && !info.behind)});

  function ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }

  function body() {
    const i = info || {};
    const out = [];
    if (i.behind) {
      out.push(h("p", {class: "upd-lead"}, h("b", {}, i.latest), " is available — you have ", h("b", {}, i.current), "."));
      out.push(grovy.md("Update your grovy folder with `grovy update` (fast-forwards the git upstream), or see the "
        + "release on GitHub below. Sessions already running keep their settings until ⋯ → Update session."));
    } else if (i.current && i.latest) {
      out.push(h("p", {class: "upd-lead"}, "grovy is up to date ", h("span", {class: "upd-mut"}, "(" + i.current + ")"), "."));
    } else if (i.error) {
      out.push(h("p", {class: "upd-lead"}, "Couldn't check for updates."), h("p", {class: "upd-mut"}, String(i.error)));
    } else {
      out.push(h("p", {class: "upd-lead"}, "Haven't checked yet."));
    }
    const kv = (k, v) => v == null || v === "" ? null : h("div", {class: "upd-kv"}, h("span", {}, k), h("span", {}, v));
    out.push(h("div", {class: "upd-rows"},
      kv("Installed", i.current || "unknown (not a tagged checkout)"),
      kv("Latest", i.latest || (i.error ? "—" : "not checked")),
      kv("Repo", i.repo || "—"),
      kv("Last checked", ago(i.checked))));
    if (i.url) out.push(h("a", {class: "chip", href: i.url, target: "_blank", rel: "noopener",
      style: "margin-top:12px"}, i.behind ? "View release" : "Releases on GitHub"));
    const check = h("button", {class: "btn allow", onclick: async () => {
      check.disabled = true; check.textContent = "Checking…";
      try { info = await grovy.api("/api/update", {}); } catch (e) { grovy.say("Couldn't check: " + e.message); }
      sheet();
    }}, "Check now");
    out.push(h("div", {style: "display:flex;gap:10px;margin-top:12px"}, check));
    return out;
  }

  function sheet() { grovy.sheet(h("h2", {}, "Updates"), ...body()); }

  grovy.css(`
.upd-lead { font-size:15px; margin:2px 0 8px; }
.upd-mut { color:var(--mut); }
.upd-rows { margin:10px 0; }
.upd-kv { display:flex; justify-content:space-between; gap:12px; padding:5px 0; border-top:1px solid var(--line); font-size:14px; }
.upd-kv span:last-child { color:var(--mut); overflow-wrap:anywhere; text-align:right; }`);
}, {about: "Check GitHub for a newer grovy release"});
