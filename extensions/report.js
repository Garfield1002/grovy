// Report: point at what you don't like in the grovy UI and fire it off to an agent, made fast.
//
// A key binding (Alt+R) or a two-finger press-and-hold opens a paint overlay ON TOP of whatever you're doing. It
// never navigates or closes anything, so cancelling drops you right back where you were. You paint over the region(s)
// you don't like (it can span several things); grovy works out which on-screen elements that covers and describes
// *those* to the agent, so you never have to see or name the DOM. Add a line about what's wrong, and grovy files it
// as an issue in your grovy repo (one worktree, one session) — the same machinery as ⋯ → New issue, just aimed at
// grovy itself and started from a picture of the screen.
//
// A built-in extension that uses only window.grovy; the work is grovy's own (grovylib/issues.py, /api/report).
// To turn it off: grovy.disable("report") in custom.js.
grovy.extension("report", grovy => {
  const {h} = grovy;
  grovy.css(`
#grovy-report { position:fixed; inset:0; z-index:2147483000; touch-action:none; -webkit-user-select:none; user-select:none; }
#grovy-report canvas { position:absolute; inset:0; width:100%; height:100%; cursor:crosshair; }
#grovy-report .rep-top, #grovy-report .rep-bot { position:absolute; left:0; right:0; background:var(--card); border-color:var(--line);
  display:flex; align-items:center; gap:8px; padding:8px 10px; box-shadow:0 0 16px rgba(0,0,0,.25); }
#grovy-report .rep-top { top:0; border-bottom:1px solid var(--line); padding-top:calc(8px + env(safe-area-inset-top)); }
#grovy-report .rep-bot { bottom:0; border-top:1px solid var(--line); flex-wrap:wrap; padding-bottom:calc(8px + env(safe-area-inset-bottom)); }
#grovy-report .rep-hint { color:var(--fg); font-size:14px; font-weight:600; flex:1; }
#grovy-report .rep-sub { color:var(--mut); font-size:12px; width:100%; margin:-2px 0 2px; }
#grovy-report .rep-err { color:var(--bad); font-size:13px; width:100%; }
#grovy-report .rep-err:empty { display:none; }
#grovy-report textarea { width:100%; box-sizing:border-box; }
#grovy-report .rep-spacer { flex:1; }
#grovy-report .rep-min { padding:4px 10px; font-size:13px; }`);

  const metaTheme = () => { try { return JSON.parse(document.querySelector('meta[name="grovy-theme"]').content).theme || "default"; }
                            catch (e) { return "default"; } };
  const scheme = () => matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const clip = (s, n) => s.length > n ? s.slice(0, n) + "…" : s;

  let overlay = null;   // the one overlay, if open

  function describe(el) {   // a short, agent-readable line for one element the user painted over (never the DOM to them)
    const tag = el.tagName.toLowerCase();
    const id = el.id ? "#" + el.id : "";
    const cls = el.classList.length ? "." + [...el.classList].slice(0, 4).join(".") : "";
    const label = el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("placeholder") ||
                  el.getAttribute("alt") || "";
    const text = (el.textContent || "").replace(/\s+/g, " ").trim();
    const r = el.getBoundingClientRect();
    const rect = `${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}×${Math.round(r.height)}`;
    const quote = clip(label || text, 80);
    return `<${tag}${id}${cls}>` + (quote ? ` “${quote}”` : "") + `  [${rect}]`;
  }

  function collect(strokes) {   // the distinct on-screen elements under the paint, topmost first, overlay excluded
    const seen = new Map();   // element -> its line
    overlay.style.visibility = "hidden";   // so elementFromPoint sees the page, not us
    try {
      for (const s of strokes) for (let i = 0; i < s.length; i += 3) {
        const el = document.elementFromPoint(s[i].x, s[i].y);
        if (!el || el === document.body || el === document.documentElement || overlay.contains(el)) continue;
        if (!seen.has(el)) seen.set(el, describe(el));
        if (seen.size >= 15) break;
      }
    } finally { overlay.style.visibility = ""; }
    return [...seen.values()];
  }

  function context(strokes) {
    const dpr = Math.round((window.devicePixelRatio || 1) * 10) / 10;
    const lines = [`Screen: ${location.hash || "#/"}  (${clip(document.title, 60)})`,
                   `Viewport: ${innerWidth}×${innerHeight} @${dpr}x · theme ${metaTheme()} · ${scheme()}`];
    const els = strokes.length ? collect(strokes) : [];
    if (els.length) {
      lines.push(`Painted over ${strokes.length} region(s); the on-screen elements under the paint, topmost first:`);
      for (const e of els) lines.push("  - " + e);
    } else {
      lines.push("(nothing painted — see the note)");
    }
    return lines.join("\n");
  }

  function start() {
    if (overlay) return;
    try { open_(); } catch (e) { close(); grovy.say("Report: " + (e && e.message || e)); }
  }

  function open_() {
    const canvas = h("canvas", {});
    const ctx = canvas.getContext("2d");
    const strokes = [];   // each is an array of {x, y} in viewport (CSS) pixels
    let busy = false;     // a filing POST is in flight: Esc and ✕ wait for it (it files what you asked for)
    const fit = () => {
      const dpr = window.devicePixelRatio || 1;   // read each time: browser zoom / a move between monitors can change it
      canvas.width = Math.round(innerWidth * dpr); canvas.height = Math.round(innerHeight * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); redraw();
    };
    const redraw = () => {
      ctx.clearRect(0, 0, innerWidth, innerHeight);
      ctx.lineWidth = 26; ctx.lineCap = ctx.lineJoin = "round"; ctx.strokeStyle = "rgba(255,92,60,.4)";
      for (const s of strokes) {
        if (!s.length) continue;
        ctx.beginPath(); ctx.moveTo(s[0].x, s[0].y);
        for (const p of s) ctx.lineTo(p.x, p.y);
        if (s.length === 1) ctx.lineTo(s[0].x + .01, s[0].y);   // a dot
        ctx.stroke();
      }
    };

    const active = new Map();   // pointerId -> its stroke, so several fingers paint separate strokes, not one tangle
    canvas.addEventListener("pointerdown", e => {
      e.preventDefault(); canvas.setPointerCapture(e.pointerId);
      const stroke = [{x: e.clientX, y: e.clientY}];
      active.set(e.pointerId, stroke); strokes.push(stroke); redraw(); sync();
    });
    canvas.addEventListener("pointermove", e => {
      const stroke = active.get(e.pointerId);
      if (!stroke) return;
      e.preventDefault(); stroke.push({x: e.clientX, y: e.clientY}); redraw();
    });
    const end = e => active.delete(e.pointerId);
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);

    const hint = h("div", {class: "rep-hint"}, "Paint over what's wrong");
    const undo = h("button", {class: "btn rep-min", onclick: () => { strokes.pop(); redraw(); sync(); }}, "Undo");
    const clear = h("button", {class: "btn rep-min", onclick: () => { strokes.length = 0; redraw(); sync(); }}, "Clear");
    const cancel = h("button", {class: "btn rep-min", "aria-label": "Cancel", onclick: () => { if (!busy) close(); }}, "✕");
    const top = h("div", {class: "rep-top"}, hint, undo, clear, cancel);

    const note = h("textarea", {class: "field", rows: 2, placeholder: "What don't you like? (e.g. “move this button, it's too close to Discard”)"});
    const err = h("div", {class: "rep-err"});   // shown IN the overlay: grovy.say() would paint behind it (higher z-index)
    const file = h("button", {class: "btn allow", disabled: ""}, "File grovy issue");
    const bot = h("div", {class: "rep-bot"},
      h("div", {class: "rep-sub"}, "Filed as an issue in your grovy repo — a worktree and a session of its own."),
      err, note, h("div", {class: "rep-spacer"}), file);

    overlay = h("div", {id: "grovy-report"}, canvas, top, bot);
    document.body.appendChild(overlay);

    function sync() { file.disabled = busy || !note.value.trim(); }
    note.oninput = () => { err.textContent = ""; sync(); };
    file.onclick = async () => {
      const words = note.value.trim();
      if (!words) return;
      if (/^[/!]/.test(words)) { err.textContent = "Start the note with words: a leading / or ! would look like a command."; return; }
      busy = true; sync(); err.textContent = ""; file.textContent = "Filing…";
      const ctxText = context(strokes);
      try {
        const r = await grovy.api("/api/report", {note: words, context: ctxText});
        close();
        if (await grovy.ask(`Filed grovy issue ${r.name}` + (r.queued ? "." : " (open it to send your words).") +
                            " Open it now?", "Open", {cancel: "Stay here"})) {
          if (!r.queued) addEventListener("hashchange", () => grovy.draft(words), {once: true});
          location.hash = "#/s/" + encodeURIComponent(r.name);
        }
      } catch (e) {
        busy = false; file.textContent = "File grovy issue"; sync();
        err.textContent = "Not filed: " + e.message;   // e.g. the graft / untrusted-repo refusal — must be readable here
      }
    };

    onkey = e => { if (e.key === "Escape" && !busy) { e.preventDefault(); close(); } };
    addEventListener("keydown", onkey, true);
    onresize_ = () => fit();
    addEventListener("resize", onresize_);
    fit(); sync();
  }

  let onkey = null, onresize_ = null;
  function close() {
    if (onkey) { removeEventListener("keydown", onkey, true); onkey = null; }
    if (onresize_) { removeEventListener("resize", onresize_); onresize_ = null; }
    if (overlay) { overlay.remove(); overlay = null; }
  }

  grovy.key("Alt+R", start);

  // Always available, no button: press and hold two fingers for ~half a second, anywhere. (A button might not be
  // reachable in every state; this gesture is, and it leaves what you're doing untouched.)
  let hold = null;
  addEventListener("touchstart", e => {
    clearTimeout(hold);
    if (overlay || e.touches.length !== 2) return;
    hold = setTimeout(start, 550);
  }, {passive: true});
  const drop = () => { clearTimeout(hold); hold = null; };
  addEventListener("touchend", drop, {passive: true});
  addEventListener("touchmove", drop, {passive: true});   // a hold is still; any move (e.g. a pinch) cancels it
}, {about: "Alt+R / two-finger hold: paint over what's wrong and file a grovy issue"});
