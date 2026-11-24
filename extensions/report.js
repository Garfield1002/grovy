// Report: point at what you don't like in the grovy UI and fire it off to an agent, made fast.
//
// A key binding (Alt+R) or a two-finger press-and-hold opens a paint overlay ON TOP of whatever you're doing. It
// never navigates or closes anything, so cancelling drops you right back where you were. You paint over the region(s)
// you don't like (it can span several things); grovy works out which on-screen elements that covers and describes
// *those* to the agent, so you never have to see or name the DOM. Add a line about what's wrong, and grovy files it
// as an issue in your grovy repo (one worktree, one session) — the same machinery as ＋ → New issue, just aimed at
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

  function bare(fn) {   // run fn on the page as it really is: the overlay hidden, the page unshrunk; then put both back
    overlay.style.visibility = "hidden";   // so elementFromPoint sees the page, not us
    const again = shrunk && (() => shrink(...overlay._bars()));
    unshrink();   // the real layout: real elements under the points, real rects in what the agent is told
    try { return fn(); } finally { if (again) again(); overlay.style.visibility = ""; }
  }

  function collect(strokes) {   // the distinct on-screen elements under the paint, topmost first, overlay excluded
    const seen = new Map();   // element -> its line
    {
      for (const s of strokes) for (let i = 0; i < s.length; i += 3) {
        if (s[i].x < 0 || s[i].y < 0 || s[i].x >= innerWidth || s[i].y >= innerHeight) continue;   // off the page
        const el = document.elementFromPoint(s[i].x, s[i].y);
        if (!el || el === document.body || el === document.documentElement || overlay.contains(el)) continue;
        if (!seen.has(el)) seen.set(el, describe(el));
        if (seen.size >= 15) break;
      }
    }
    return [...seen.values()];
  }

  // A picture of the page as it is, with the paint on top, for the agent to look at. No library: the live DOM is cloned
  // (the overlay left out), its state the clone can't carry is written in (form values, canvases and images as data
  // URLs, scroll offsets, where fixed and stuck elements are on screen), and the clone is drawn inside an SVG
  // <foreignObject>, which the browser rasterizes like any image. Called with the page unshrunk; null when the browser
  // won't (an older Safari taints such a canvas), and the report then goes without it.
  function clonePage() {
    const W = innerWidth, H = innerHeight, sx = scrollX, sy = scrollY;
    const root = document.documentElement, copy = root.cloneNode(true);
    const live = [root, ...root.querySelectorAll("*")], dup = [copy, ...copy.querySelectorAll("*")];
    const fixes = [];   // read every live layout first, then write to the clone
    live.forEach((el, i) => {
      const c = dup[i];
      if (!c || (overlay && (el === overlay || overlay.contains(el)))) return;
      if (/^(SCRIPT|NOSCRIPT)$/.test(el.tagName)) return fixes.push(() => c.remove());
      if (el.tagName === "TEXTAREA") fixes.push(() => { c.textContent = el.value; });
      else if (el.tagName === "INPUT") fixes.push(() => {
        if (/^(checkbox|radio)$/.test(el.type)) el.checked ? c.setAttribute("checked", "") : c.removeAttribute("checked");
        else if (el.type !== "password") c.setAttribute("value", el.value);
      });
      else if (el.tagName === "CANVAS") {
        let url = ""; try { url = el.toDataURL(); } catch (e) {}
        const r = el.getBoundingClientRect();
        fixes.push(() => { const img = document.createElement("img"); img.src = url; img.className = el.className;
          img.style.cssText = el.style.cssText + `;width:${r.width}px;height:${r.height}px`; c.replaceWith(img); });
      } else if (el.tagName === "IMG" && el.complete && el.naturalWidth && !/^data:/.test(el.currentSrc || el.src)) {
        try {
          const cv = document.createElement("canvas"); cv.width = el.naturalWidth; cv.height = el.naturalHeight;
          cv.getContext("2d").drawImage(el, 0, 0); const url = cv.toDataURL(); fixes.push(() => c.setAttribute("src", url));
        } catch (e) {}
      }
      if (el.scrollTop || el.scrollLeft) {
        if (el !== root && el !== document.body) {
          const t = `translate(${-el.scrollLeft}px, ${-el.scrollTop}px)`;
          fixes.push(() => { for (const k of c.children) k.style.transform = t + " " + (k.style.transform || ""); });
        }
      }
      const pos = el.nodeType === 1 && el !== root && el !== document.body ? getComputedStyle(el).position : "";
      if (pos === "fixed") {   // the picture is the whole document seen through a window at the scroll offset
        const r = el.getBoundingClientRect();
        fixes.push(() => Object.assign(c.style, {position: "absolute", top: r.top + sy + "px", left: r.left + sx + "px",
          width: r.width + "px", height: r.height + "px", right: "auto", bottom: "auto", margin: "0", transform: "none"}));
      } else if (pos === "sticky") {   // where it is stuck now, minus where it would sit if it scrolled away
        const stuck = el.getBoundingClientRect(), was = el.style.position;
        el.style.position = "relative"; const flow = el.getBoundingClientRect(); el.style.position = was;
        fixes.push(() => Object.assign(c.style, {position: "relative", top: stuck.top - flow.top + "px",
          left: stuck.left - flow.left + "px"}));
      }
    });
    fixes.forEach(f => f());
    const docH = Math.max(root.scrollHeight, sy + H), docW = Math.max(root.scrollWidth, sx + W);
    copy.style.width = docW + "px"; copy.style.height = docH + "px";
    const xhtml = new XMLSerializer().serializeToString(copy);
    return {W, H, svg: `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="${sx} ${sy} ${W} ${H}">` +
      `<foreignObject x="0" y="0" width="${docW}" height="${docH}">${xhtml}</foreignObject></svg>`};
  }

  async function picture(page, strokes) {   // page: clonePage()'s result → a PNG Blob with the paint drawn on, or null
    try {
      const img = new Image();
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(page.svg);
      await img.decode();
      const k = Math.min(2, window.devicePixelRatio || 1), cv = document.createElement("canvas");
      cv.width = Math.round(page.W * k); cv.height = Math.round(page.H * k);
      const g = cv.getContext("2d"); g.scale(k, k);
      g.fillStyle = getComputedStyle(document.body).backgroundColor || "#fff"; g.fillRect(0, 0, page.W, page.H);
      g.drawImage(img, 0, 0, page.W, page.H);
      g.lineWidth = 26; g.lineCap = g.lineJoin = "round"; g.strokeStyle = "rgba(255,60,30,.45)";
      for (const s of strokes) {
        if (!s.length) continue;
        g.beginPath(); g.moveTo(s[0].x, s[0].y);
        for (const p of s) g.lineTo(p.x, p.y);
        if (s.length === 1) g.lineTo(s[0].x + .01, s[0].y);
        g.stroke();
      }
      return await new Promise(res => cv.toBlob(res, "image/png"));   // throws if the browser tainted the canvas
    } catch (e) { return null; }
  }

  async function upload(png) {   // → the path the issue's Claude can read, or "" (the report still goes)
    if (!png) return "";
    try {
      const r = await fetch("/api/report/screenshot", {method: "POST", body: png,
        headers: {"Content-Type": "application/octet-stream"}});
      return r.ok ? (await r.json()).path || "" : "";
    } catch (e) { return ""; }
  }

  function where() {   // where the user is, in words: the screen, the session and what's open over it
    const out = [];
    const name = grovy.session, st = grovy.state;
    if (name) out.push(`Session "${name}"` + (st ? ` — state ${st.state || "?"}` + (st.cwd ? `, in ${st.cwd}` : "") : ""));
    else out.push("The session list (home screen)");
    const sheet = document.querySelector("#sheet:not(.hidden) #sheetbody h2");
    if (sheet) out.push(`A sheet is open over it: “${clip(sheet.textContent.trim(), 60)}”`);
    const vis = id => { const e = document.getElementById(id); return e && !e.classList.contains("hidden") && e.getClientRects().length > 0; };
    for (const [id, what] of [["viewer", "the file viewer"], ["ctx", "a right-click menu"], ["err", "an error banner"]])
      if (vis(id)) out.push(`Also open: ${what}` + (id === "err" ? ` (“${clip(document.getElementById(id).textContent.trim(), 120)}”)` : ""));
    const docH = document.documentElement.scrollHeight;
    out.push(`Scrolled ${Math.round(scrollY)}px of ${Math.max(0, docH - innerHeight)}px` +
             (docH - innerHeight - scrollY < 40 ? " (at the bottom)" : scrollY < 40 ? " (at the top)" : ""));
    const f = document.activeElement;
    if (f && f !== document.body && !(overlay && overlay.contains(f))) out.push("Focused: " + describe(f));
    out.push(`Layout: ${innerWidth >= 900 ? "wide (sidebar)" : "phone"} · ${navigator.userAgent.replace(/\s+/g, " ").slice(0, 160)}`);
    return out;
  }

  function capture(strokes) {   // everything else read from the page, in one pass over its real layout
    return bare(() => {
      const dpr = Math.round((window.devicePixelRatio || 1) * 10) / 10;
      return {els: strokes.length ? collect(strokes) : [],
              head: [`Screen: ${location.hash || "#/"}  (${clip(document.title, 60)})`, ...where(),
                     `Viewport: ${innerWidth}×${innerHeight} @${dpr}x · theme ${metaTheme()} · ${scheme()}`]};
    });
  }

  function context(strokes, {head, els}, shot) {
    const lines = [...head, shot ? `Screenshot of the screen as it was, the paint in translucent red (read it): ${shot}`
                                 : "(no screenshot: this browser wouldn't draw the page)"];
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
    // the picture is of the page as it was when Report opened: before the keyboard (for the note) squeezes it
    let page = null;
    try { page = clonePage(); } catch (e) {}
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
      try {
        const seen = capture(strokes);
        const shot = page ? await upload(await picture(page, strokes)) : "";
        const ctxText = context(strokes, seen, shot);
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
