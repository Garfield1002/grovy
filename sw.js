// grovy's service worker, served at /sw.js (grovyd.py) so it covers the whole app. It does two things:
//   * Web Push: the daemon's push plugin (plugins/push.py) sends an *empty* push through the browser's push service,
//     so that service never sees what happened. Here the worker asks grovy itself for the messages and shows them.
//   * Tapping a notification opens that session.
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

const TITLES = {input: "needs you", done: "finished", error: "failed"};

async function show(m, watching, quiet) {
  const tag = "grovy-" + m.session;   // one per session: the newest replaces the previous
  await self.registration.showNotification(m.title || `${m.session} ${TITLES[m.kind] || ""}`.trim(), {
    body: m.text, tag, icon: "/assets/icon-192.png", badge: "/assets/icon-64.png",
    data: {url: m.session ? "/#/s/" + encodeURIComponent(m.session) : "/"}, renotify: !watching && !quiet,
    silent: quiet});
  if (watching)   // the user is looking at this session right now: shown (browsers require it), then gone at once
    for (const n of await self.registration.getNotifications({tag})) n.close();
}

self.addEventListener("push", e => e.waitUntil((async () => {
  let got = null;
  try { got = await messages(); } catch (err) { /* grovy unreachable or logged out: say so, without details */ }
  // Every push must show something (browsers count silent ones against the site). Each message has its own push, but
  // one fetch can take several: a push that finds none re-shows the latest, quietly (no sound, no new alert).
  const quiet = !!got && !got.messages.length;
  const list = got ? (got.messages.length ? got.messages : got.latest ? [got.latest] : []) : [];
  if (!list.length)
    return self.registration.showNotification("grovy", {body: "Something happened: open grovy to see.", tag: "grovy",
      icon: "/assets/icon-192.png"});
  const wins = await self.clients.matchAll({type: "window", includeUncontrolled: true});
  const watching = s => wins.some(c => c.focused && c.visibilityState === "visible" &&
                                       new URL(c.url).hash === "#/s/" + encodeURIComponent(s));
  for (const m of list) await show(m, !!m.session && watching(m.session), quiet);
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
