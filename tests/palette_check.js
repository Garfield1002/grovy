// The command palette and the ＋ "New" menu (index.html palette/currentCmds/newMenu), driven in a real browser:
// tests/test_grovy.py TestPaletteBrowser adds this to a copy of index.html (fetch/EventSource stubbed, plus a tiny
// injected extension registering a where:"new" item "Startable" and a where:"both" item "Dual") and screenshots it in
// headless Firefox. The top-left pixel says the verdict: green when every check passed; red otherwise, with the
// failures listed. Only synchronous behaviour is checked (the screenshot wouldn't wait for the async session/page
// jumps, which palette_check.js doesn't assert).
addEventListener("DOMContentLoaded", () => {
 const R = [], ok = (name, c, extra = "") => R.push((c ? "PASS " : "FAIL ") + name + (c ? "" : " " + extra));
 const $$ = sel => [...document.querySelectorAll(sel)];
 const rows = () => $$("#sheetbody .cmdrow");
 const texts = els => els.map(r => r.textContent);
 const key = (el, k, opts = {}) => el.dispatchEvent(new KeyboardEvent("keydown", {key: k, bubbles: true, cancelable: true, ...opts}));
 const count = (els, t) => els.filter(r => r.textContent.includes(t)).length;

 // Ctrl+K opens the palette
 key(document.body, "k", {ctrlKey: true});
 ok("ctrl+k opens palette", !!document.querySelector("#sheetbody .cmdp"), document.getElementById("sheet").className);
 ok("has commands", rows().length >= 3, rows().length + "");
 const has = t => rows().some(r => r.textContent.includes(t));
 ok("lists New session", has("New session"));
 ok("lists Extensions", has("Extensions"));
 ok("lists Login link", has("Login link"));

 // where:"new" item shows in the palette; where:"both" item shows exactly once (not duplicated across buckets)
 ok("lists where:new item", has("Startable"));
 ok("where:both item once", count(rows(), "Dual") === 1, count(rows(), "Dual") + "");

 // a second Ctrl+K doesn't stack a second palette
 key(document.body, "k", {ctrlKey: true});
 ok("no double palette", $$("#sheetbody .cmdp").length === 1, $$("#sheetbody .cmdp").length + "");

 const inp = document.querySelector("#sheetbody input.field");
 ok("search box present", !!inp);

 // typing filters; "extensi" keeps Extensions and drops the rest, first row highlighted
 inp.value = "extensi"; inp.dispatchEvent(new Event("input", {bubbles: true}));
 ok("filter narrows to one", rows().length === 1, texts(rows()).join(" | "));
 ok("filter keeps Extensions", rows()[0] && rows()[0].textContent.includes("Extensions"));
 ok("first row highlighted", rows()[0] && rows()[0].classList.contains("on"));

 // cleared query shows all again; arrow keys move the highlight (tracked by descriptor, so Enter runs the shown row)
 inp.value = ""; inp.dispatchEvent(new Event("input", {bubbles: true}));
 ok("cleared shows all", rows().length >= 3, rows().length + "");
 ok("first on by default", rows()[0].classList.contains("on"));
 key(inp, "ArrowDown");
 ok("arrow down moves highlight", !rows()[0].classList.contains("on") && rows()[1].classList.contains("on"),
    rows().map(r => r.classList.contains("on")).join(","));

 // Enter on a filtered-to-Extensions list runs it (opens the Extensions sheet)
 inp.value = "extensi"; inp.dispatchEvent(new Event("input", {bubbles: true}));
 key(inp, "Enter");
 ok("enter runs the command", (document.querySelector("#sheetbody h2") || {}).textContent === "Extensions",
    (document.querySelector("#sheetbody h2") || {}).textContent);

 // the ＋ button opens a "New" menu with the where:"new" items, but not the where:"both" one
 closeSheet();
 document.getElementById("add").click();
 const newItems = $$("#sheetbody .item");
 ok("+ opens New menu", (document.querySelector("#sheetbody h2") || {}).textContent === "New",
    (document.querySelector("#sheetbody h2") || {}).textContent);
 ok("New menu has New session", count(newItems, "New session") === 1);
 ok("New menu has where:new item", count(newItems, "Startable") === 1);
 ok("New menu lacks where:both item", count(newItems, "Dual") === 0, texts(newItems).join(" | "));

 const bad = R.filter(r => !r.startsWith("PASS"));
 const v = document.createElement("pre"); v.id = "verdict";
 v.style.cssText = "position:fixed;inset:0;z-index:2147483647;margin:0;padding:8px;font-size:12px;background:" + (bad.length ? "#ff0000" : "#00ff00");
 v.textContent = bad.length ? bad.join("\n") : "ok"; document.body.append(v);
});
