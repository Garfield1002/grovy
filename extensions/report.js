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
#grovy-report textarea { width:100%; flex:1 1 100%; box-sizing:border-box; }
#grovy-report .rep-spacer { flex:1; }
#grovy-report .rep-min { padding:4px 10px; font-size:13px; flex:0 0 auto; width:auto; }`);

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

  // While the overlay is open the page is shrunk into the space between its two bars, so nothing is hidden behind
  // them. Each child of <body> gets the same viewport mapping v -> (x0, y0) + k*v (its transform-origin is the
  // viewport's corner in its own coordinates), so fixed sheets and the sticky header shrink with the rest; the overlay
  // itself is a body child too and is left alone. Strokes are kept in page coordinates (v), so a resize (the keyboard
  // opening for the note) rescales them with the page.
  let shrunk = null;   // [{el, style: what it had}], and the mapping
  let fitTo = {k: 1, x0: 0, y0: 0};
  function shrink(topH, botH) {
    unshrink();
    const W = innerWidth, H = innerHeight, pad = 8;
    const k = Math.max(.3, Math.min((W - 2 * pad) / W, (H - topH - botH - 2 * pad) / H));
    fitTo = {k, x0: (W - k * W) / 2, y0: topH + pad + Math.max(0, (H - topH - botH - 2 * pad - k * H) / 2)};
    const kids = [...document.body.children].filter(el => el !== overlay && !/^(SCRIPT|STYLE|LINK|TEMPLATE)$/.test(el.tagName));
    const rects = kids.map(el => el.getBoundingClientRect());   // all read before any is moved
    shrunk = kids.map((el, i) => {
      const was = {transform: el.style.transform, transformOrigin: el.style.transformOrigin, transition: el.style.transition};
      el.style.transition = "none";
      el.style.transformOrigin = `${-rects[i].left}px ${-rects[i].top}px`;
      el.style.transform = `translate(${fitTo.x0}px, ${fitTo.y0}px) scale(${k})`;
      return {el, was};
    });
  }
  function unshrink() {
    if (shrunk) for (const {el, was} of shrunk) Object.assign(el.style, was);
    shrunk = null; fitTo = {k: 1, x0: 0, y0: 0};
  }
  const toPage = (x, y) => ({x: (x - fitTo.x0) / fitTo.k, y: (y - fitTo.y0) / fitTo.k});
  const toScreen = p => ({x: fitTo.x0 + fitTo.k * p.x, y: fitTo.y0 + fitTo.k * p.y});

  function collect(strokes) {   // the distinct on-screen elements under the paint, topmost first, overlay excluded
    const seen = new Map();   // element -> its line
    overlay.style.visibility = "hidden";   // so elementFromPoint sees the page, not us
    const again = shrunk && (() => shrink(...overlay._bars()));
    unshrink();   // the real layout: real elements under the points, real rects in what the agent is told
    try {
      for (const s of strokes) for (let i = 0; i < s.length; i += 3) {
        if (s[i].x < 0 || s[i].y < 0 || s[i].x >= innerWidth || s[i].y >= innerHeight) continue;   // off the page
        const el = document.elementFromPoint(s[i].x, s[i].y);
        if (!el || el === document.body || el === document.documentElement || overlay.contains(el)) continue;
        if (!seen.has(el)) seen.set(el, describe(el));
        if (seen.size >= 15) break;
      }
    } finally { if (again) again(); overlay.style.visibility = ""; }
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
    const strokes = [];   // each is an array of {x, y} in page coordinates: where they are on the unshrunk page
    let busy = false;     // a filing POST is in flight: Esc and ✕ wait for it (it files what you asked for)
    const fit = () => {
      const dpr = window.devicePixelRatio || 1;   // read each time: browser zoom / a move between monitors can change it
      canvas.width = Math.round(innerWidth * dpr); canvas.height = Math.round(innerHeight * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      shrink(...overlay._bars()); redraw();
    };
    const redraw = () => {
      ctx.clearRect(0, 0, innerWidth, innerHeight);
      const {k, x0, y0} = fitTo, w = innerWidth * k, hh = innerHeight * k;
      ctx.fillStyle = "rgba(0,0,0,.45)";   // dim around the shrunk page, and frame it
      ctx.fillRect(0, 0, innerWidth, y0); ctx.fillRect(0, y0 + hh, innerWidth, innerHeight - y0 - hh);
      ctx.fillRect(0, y0, x0, hh); ctx.fillRect(x0 + w, y0, innerWidth - x0 - w, hh);
      ctx.lineWidth = 1; ctx.strokeStyle = "rgba(128,128,128,.8)"; ctx.strokeRect(x0 - .5, y0 - .5, w + 1, hh + 1);
      ctx.lineWidth = 26 * k; ctx.lineCap = ctx.lineJoin = "round"; ctx.strokeStyle = "rgba(255,92,60,.4)";
      for (const s of strokes) {
        if (!s.length) continue;
        const q = s.map(toScreen);
        ctx.beginPath(); ctx.moveTo(q[0].x, q[0].y);
        for (const p of q) ctx.lineTo(p.x, p.y);
        if (q.length === 1) ctx.lineTo(q[0].x + .01, q[0].y);   // a dot
        ctx.stroke();
      }
    };

    const active = new Map();   // pointerId -> its stroke, so several fingers paint separate strokes, not one tangle
    canvas.addEventListener("pointerdown", e => {
      e.preventDefault(); canvas.setPointerCapture(e.pointerId);
      const stroke = [toPage(e.clientX, e.clientY)];
      active.set(e.pointerId, stroke); strokes.push(stroke); redraw(); sync();
    });
    canvas.addEventListener("pointermove", e => {
      const stroke = active.get(e.pointerId);
      if (!stroke) return;
      e.preventDefault(); stroke.push(toPage(e.clientX, e.clientY)); redraw();
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
    overlay._bars = () => [top.getBoundingClientRect().bottom, innerHeight - bot.getBoundingClientRect().top];
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
    if (window.ResizeObserver) { bars = new ResizeObserver(() => fit()); bars.observe(top); bars.observe(bot); }   // e.g. an error line
    fit(); sync();
  }

  let onkey = null, onresize_ = null, bars = null;
  function close() {
    if (bars) { bars.disconnect(); bars = null; }
    if (onkey) { removeEventListener("keydown", onkey, true); onkey = null; }
    if (onresize_) { removeEventListener("resize", onresize_); onresize_ = null; }
    unshrink();
    if (overlay) { overlay.remove(); overlay = null; }
  }

  grovy.key("Alt+R", start);

  // Always available, no button: press and hold two fingers for ~half a second, anywhere. (A button might not be
  // reachable in every state; this gesture is, and it leaves what you're doing untouched.)
  // Real fingers never stay perfectly still: touchmove fires for a pixel of jitter, so only a real move (a pinch, a
  // two-finger scroll) cancels the hold.
  let hold = null, from = null;
  const pts = e => [...e.touches].map(t => [t.clientX, t.clientY]);
  const drop = () => { clearTimeout(hold); hold = null; from = null; };
  addEventListener("touchstart", e => {
    drop();
    if (overlay || e.touches.length !== 2) return;
    from = pts(e);
    hold = setTimeout(() => { drop(); start(); }, 550);
  }, {passive: true});
  addEventListener("touchmove", e => {
    if (!from) return;
    const now = pts(e);
    if (now.length !== 2 || now.some(([x, y], i) => Math.hypot(x - from[i][0], y - from[i][1]) > 12)) drop();
  }, {passive: true});
  addEventListener("touchend", drop, {passive: true});
  addEventListener("touchcancel", drop, {passive: true});   // the browser took the touch over (e.g. its own zoom)
}, {about: "Alt+R / two-finger hold: paint over what's wrong and file a grovy issue"});
