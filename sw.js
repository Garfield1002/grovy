// grovy's service worker, served at /sw.js (grovyd.py) so it covers the whole app. It does two things:
//   * Web Push: the daemon's push plugin (plugins/push.py) sends an *empty* push through the browser's push service,
//     so that service never sees what happened. Here the worker asks grovy itself for the messages and shows them.
//   * One notification stacks every session's news; tapping it opens grovy (or the session, if only one).
// It caches nothing and never answers a page request: the app always comes from grovy, as it is now.
// Turning notifications on and off is extensions/push.js.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

async function messages() {
  const sub = await self.registration.pushManager.getSubscription();
  if (!sub) return null;
  const r = await fetch("/api/x/push/pending", {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Grovy": "1"},
    body: JSON.stringify({endpoint: sub.endpoint})});
  if (!r.ok) throw new Error("grovy answered " + r.status);
  return r.json();   // {messages: [...], latest: {...} | null}
}

// All of grovy is one notification, a stack: a line per session (its latest news, newest first), kept in the
// notification's own data, so dismissing it starts a new stack. It buzzes when it appears, and again only for news
// that needs you (a question, a permission, a failure); a turn that finished just updates it quietly.
const STACK = "grovy", SEEN = "grovy-seen", LINES = 6;
const ICON = "/assets/icon-192.png", BADGE = "/assets/badge-96.png";   // the badge: white on transparent (Android)
const TITLES = {input: "needs you", done: "finished", error: "failed"};
const URGENT = new Set(["input", "error"]);

function line(m) {
  const t = String(m.text || "").replace(/^finished: /, "");
  return `${m.session || "grovy"}: ${t.length > 90 ? t.slice(0, 89) + "…" : t}`;
}

function title(items) {
  if (items.length === 1) { const m = items[0]; return m.title || `${m.session} ${TITLES[m.kind] || ""}`.trim(); }
  const n = k => items.filter(m => m.kind === k).length, parts = [];
  if (n("input")) parts.push(`${n("input")} need${n("input") === 1 ? "s" : ""} you`);
  if (n("error")) parts.push(`${n("error")} failed`);
  if (n("done")) parts.push(`${n("done")} finished`);
  return `${items.length} sessions: ${parts.join(", ")}`;
}

// items: the stack, newest first. alert: buzz (a new stack always does: it's a new notification).
function draw(items, alert) {
  const url = items.length === 1 && items[0].session ? "/#/s/" + encodeURIComponent(items[0].session) : "/";
  return self.registration.showNotification(title(items), {
    body: items.map(line).join("\n"), tag: STACK, icon: ICON, badge: BADGE, data: {url, items},
    renotify: alert, silent: !alert});
}

self.addEventListener("push", e => e.waitUntil((async () => {
  let got = null;
  try { got = await messages(); } catch (err) { /* grovy unreachable or logged out: say so, without details */ }
  const shown = await self.registration.getNotifications();
  for (const n of shown) if (n.tag !== STACK) n.close();   // older per-session ones, and the "can't reach" one
  const old = shown.find(n => n.tag === STACK);
  const stack = (old && old.data && Array.isArray(old.data.items)) ? old.data.items : [];
  // Every push must show something (browsers count silent ones against the site). Each message has its own push, but
  // one fetch can take several: a push that finds none re-shows the stack (or the latest message), quietly.
  const fresh = got ? got.messages : [];
  if (!got || (!fresh.length && !stack.length && !got.latest))
    return self.registration.showNotification("grovy", {body: "Something happened: open grovy to see.", tag: SEEN,
      icon: ICON, badge: BADGE});
  if (!fresh.length) return draw(stack.length ? stack : [got.latest], false);
  // grovy is open and in front: it shows all this itself. Shown (browsers require it), then gone at once.
  const wins = await self.clients.matchAll({type: "window", includeUncontrolled: true});
  if (wins.some(c => c.focused && c.visibilityState === "visible")) {
    await self.registration.showNotification("grovy", {tag: SEEN, silent: true, icon: ICON, badge: BADGE});
    for (const n of await self.registration.getNotifications({tag: SEEN})) n.close();
    return;
  }
  let items = stack;
  for (const m of fresh) items = [m, ...items.filter(x => x.session !== m.session)];
  return draw(items.slice(0, LINES), !old || fresh.some(m => URGENT.has(m.kind)));
})()));

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const c of wins) {
      if (c.frameType !== "top-level" || new URL(c.url).origin !== self.location.origin) continue;
      try { await c.focus(); } catch (err) { continue; }
      try { await c.navigate(url); } catch (err) { /* not ours to steer: focusing it is enough */ }
      return;
    }
    await self.clients.openWindow(url);
  })());
});
