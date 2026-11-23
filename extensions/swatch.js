// Swatches: a colour Claude writes as `code` (`#3e5482`, `#fff`, `#3e548280`) gets a small dot of that colour in front.
//
// A built-in extension that uses only window.grovy. Only text that is exactly a hex colour counts, so the colour put
// on the page is always one of those (checked here, never taken as markup). Code blocks are left alone.
// To turn it off: grovy.disable("swatch") in custom.js, or ⋯ → Extensions.
grovy.extension("swatch", grovy => {
  const {h} = grovy;
  grovy.css(`
code.swatch { white-space:nowrap; }
code.swatch .sw { display:inline-block; width:.8em; height:.8em; border-radius:50%; margin-right:.35em; vertical-align:-.06em;
  box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--fg) 25%, transparent); }`);
  const HEX = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
  grovy.on("render", node => {
    for (const c of node.querySelectorAll("code")) {
      if (c.closest("pre") || c.children.length || c.classList.contains("swatch")) continue;
      const t = c.textContent.trim();
      if (!HEX.test(t)) continue;
      const dot = h("span", {class: "sw", "aria-hidden": "true"});
      dot.style.background = t;
      c.classList.add("swatch");
      c.prepend(dot);
    }
  });
}, {about: "A dot of the colour next to hex colour codes"});
