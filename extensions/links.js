// Links: web links in replies look like git's links to branches and commits (git.js). Each gets a small link icon; a bare address is shortened to its
// site and its path, faint (drawn by CSS: the link's text stays the full address, so Quote and copying get it; it
// also shows on hover); and an address written as `code` becomes a link.
//
// A built-in extension that uses only window.grovy. Only http(s) addresses are touched, read with URL() (never as
// markup), and the link opens in a new tab with no referrer, as grovy's own do. Code blocks are left alone.
// To turn it off: grovy.disable("links") in custom.js, or ⋯ → Extensions.
grovy.extension("links", grovy => {
  const {h} = grovy;
  grovy.css(`
a.lk { color:var(--acc); text-decoration:underline dotted; text-underline-offset:3px; overflow-wrap:anywhere; }
a.lk.bare { font:.88em/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; background:var(--code); padding:.1em .35em;
  border-radius:5px; }   /* an address, shown like code; a named link keeps the sentence's font */
a.lk svg { width:.95em; height:.95em; position:relative; top:.12em; margin-right:.25em; fill:none; stroke:currentColor;
  stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }   /* as git's links (git.js): the same size and look */
/* a bare address: its short form is drawn from data- attributes, so Quote and copying a selection still get the
   real address, kept as the link's (visually hidden) text */
a.lk .ls::before { content:attr(data-host); } a.lk .ls::after { content:attr(data-path); opacity:.65; }
/* inline, not position:absolute: innerText puts line breaks around a block, and Quote split the sentence there */
a.lk .lf { display:inline-block; vertical-align:bottom; width:1px; height:1px; overflow:hidden; clip-path:inset(50%);
  white-space:nowrap; }`);
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
  const short = u => {   // the site, then the path, faint (query and fragment included), its middle cut if long
    let rest = (u.pathname === "/" ? "" : u.pathname) + u.search + u.hash;
    try { rest = decodeURIComponent(rest); } catch (e) {}   // "/wiki/Été", not "/wiki/%C3%89t%C3%A9" (display only)
    if (rest.length > 42) rest = [...rest].slice(0, 24).join("") + "…" + [...rest].slice(-16).join("");
    return {host: u.host.replace(/^www\.(?=[^.]+\.)/, ""), path: rest};
  };
  grovy.on("render", node => {
    for (const c of node.querySelectorAll("code")) {   // `https://…` in a reply: a link
      if (c.closest("pre, a, button") || c.children.length) continue;
      const t = c.textContent.trim(), u = /^https?:\/\/\S+$/.test(t) && web(t);
      if (u) c.replaceWith(h("a", {href: u.href, target: "_blank", rel: "noopener noreferrer"}, u.href));
    }
    for (const a of node.querySelectorAll("a[href]")) {
      if (a.closest("pre") || a.classList.contains("lk") || !a.closest(".md")) continue;
      const u = web(a.getAttribute("href"));
      if (!u) continue;
      if (a.textContent === a.getAttribute("href") || a.textContent === u.href) {
        const s = short(u);
        a.replaceChildren(h("span", {class: "ls", "data-host": s.host, "data-path": s.path, "aria-hidden": "true"}),
                          h("span", {class: "lf"}, a.textContent));
        a.classList.add("bare");
      }
      a.title = u.href;
      a.classList.add("lk");
      a.prepend(icon());
    }
  });
}, {about: "Web links stand out, bare addresses shortened to their site"});
