// The command palette (index.html palette/currentCmds), driven in a real browser: tests/test_grovy.py
// TestPaletteBrowser adds this to a copy of index.html (fetch/EventSource stubbed) and screenshots it in headless
// Firefox. The top-left pixel says the verdict: green when every check passed; red otherwise, with the failures
// listed (open the page to read them). Only synchronous behaviour is checked (the command rows, filtering, the
// keyboard) — the session/page jumps are fetched and added async, which a screenshot wouldn't wait for.
addEventListener("DOMContentLoaded", () => {
 const R = [], ok = (name, c, extra = "") => R.push((c ? "PASS " : "FAIL ") + name + (c ? "" : " " + extra));
 const q = sel => document.querySelector(sel);
 const rows = () => [...document.querySelectorAll("#sheetbody .cmdrow")];
 const labels = () => rows().map(r => r.querySelector("div > :first-child") ? r.textContent : r.textContent);
 const key = (el, k, opts = {}) => el.dispatchEvent(new KeyboardEvent("keydown", {key: k, bubbles: true, cancelable: true, ...opts}));

 // Ctrl+K opens the palette
 key(document.body, "k", {ctrlKey: true});
 ok("ctrl+k opens palette", !!q("#sheetbody .cmdp"), document.getElementById("sheet").className);
 ok("has commands", rows().length >= 3, rows().length + "");
 const has = t => rows().some(r => r.textContent.includes(t));
 ok("lists New session", has("New session"));
 ok("lists Extensions", has("Extensions"));
 ok("lists Login link", has("Login link"));

 // a second Ctrl+K doesn't stack a second palette
 key(document.body, "k", {ctrlKey: true});
 ok("no double palette", document.querySelectorAll("#sheetbody .cmdp").length === 1,
    document.querySelectorAll("#sheetbody .cmdp").length + "");

 const inp = q("#sheetbody input.field");
 ok("search box focused-able", !!inp);

 // typing filters; "ext" keeps Extensions and drops the rest, first row highlighted
 inp.value = "ext"; inp.dispatchEvent(new Event("input", {bubbles: true}));
 ok("filter narrows to one", rows().length === 1, labels().join(" | "));
 ok("filter keeps Extensions", rows()[0] && rows()[0].textContent.includes("Extensions"));
 ok("first row highlighted", rows()[0] && rows()[0].classList.contains("on"));

 // cleared query shows all again; arrow keys move the highlight
 inp.value = ""; inp.dispatchEvent(new Event("input", {bubbles: true}));
 ok("cleared shows all", rows().length >= 3, rows().length + "");
 ok("first on by default", rows()[0].classList.contains("on"));
 key(inp, "ArrowDown");
 ok("arrow down moves highlight", !rows()[0].classList.contains("on") && rows()[1].classList.contains("on"),
    rows().map(r => r.classList.contains("on")).join(","));

 // Enter on a filtered-to-Extensions list runs it (opens the Extensions sheet)
 inp.value = "extensi"; inp.dispatchEvent(new Event("input", {bubbles: true}));
 key(inp, "Enter");
 ok("enter runs the command", (q("#sheetbody h2") || {}).textContent === "Extensions",
    (q("#sheetbody h2") || {}).textContent);

 const bad = R.filter(r => !r.startsWith("PASS"));
 const v = document.createElement("pre"); v.id = "verdict";
 v.style.cssText = "position:fixed;inset:0;z-index:2147483647;margin:0;padding:8px;font-size:12px;background:" + (bad.length ? "#ff0000" : "#00ff00");
 v.textContent = bad.length ? bad.join("\n") : "ok"; document.body.append(v);
});
