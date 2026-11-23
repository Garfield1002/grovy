// Links: web links in replies stand out a little. Each gets a small link icon; a bare address is shortened to its
// site in bold and its path, faint (the full address shows on hover); and an address written as `code` becomes a link.
//
// A built-in extension that uses only window.grovy. Only http(s) addresses are touched, read with URL() (never as
// markup), and the link opens in a new tab with no referrer, as grovy's own do. Code blocks are left alone.
// To turn it off: grovy.disable("links") in custom.js, or ⋯ → Extensions.
grovy.extension("links", grovy => {
  const {h} = grovy;
  grovy.css(`
a.lk { text-decoration:none; border-radius:5px; padding:0 3px; margin:0 -1px;
  background:color-mix(in srgb, var(--acc) 9%, transparent); overflow-wrap:anywhere; }
a.lk:hover { background:color-mix(in srgb, var(--acc) 17%, transparent); }
a.lk svg { width:.8em; height:.8em; vertical-align:-.04em; margin-right:.28em; fill:none; stroke:currentColor;
  stroke-width:2.2; stroke-linecap:round; stroke-linejoin:round; }
a.lk .lp { opacity:.7; }`);
  const NS = "http://www.w3.org/2000/svg";
  const icon = () => {   // two chain links
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    for (const d of ["M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7", "M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"]) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", d);
      s.append(p);
    }
    return s;
  };
  const web = s => { try { const u = new URL(s); return /^https?:$/.test(u.protocol) ? u : null; } catch (e) { return null; } };
  const short = u => {   // site in bold, then the path (query and fragment included), its middle cut if long
    let rest = (u.pathname === "/" ? "" : u.pathname) + u.search + u.hash;
    if (rest.length > 42) rest = rest.slice(0, 24) + "…" + rest.slice(-16);
    return [h("b", {}, u.host.replace(/^www\./, "")), rest ? h("span", {class: "lp"}, rest) : null];
  };
  grovy.on("render", node => {
    for (const c of node.querySelectorAll("code")) {   // `https://…` in a reply: a link
      if (c.closest("pre, a, button") || c.children.length) continue;
      const t = c.textContent.trim(), u = /^https?:\/\/\S+$/.test(t) && web(t);
      if (u) c.replaceWith(h("a", {href: u.href, target: "_blank", rel: "noopener noreferrer"}, t));
    }
    for (const a of node.querySelectorAll("a[href]")) {
      if (a.closest("pre") || a.classList.contains("lk") || !a.closest(".md")) continue;
      const u = web(a.getAttribute("href"));
      if (!u) continue;
      if (a.textContent === a.getAttribute("href") || a.textContent === u.href) a.replaceChildren(...short(u).filter(Boolean));
      a.title = u.href;
      a.classList.add("lk");
      a.prepend(icon());
    }
  });
}, {about: "Web links stand out, bare addresses shortened to their site"});
