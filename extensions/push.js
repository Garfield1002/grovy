// Notifications: ⚙ → Notifications turns push notifications on or off for this device (when a session needs you,
// finishes or fails, even with grovy closed), and sends a test.
//
// A built-in extension that uses only window.grovy. The sending is the built-in push plugin (plugins/push.py); what
// shows them is grovy's service worker (sw.js). Needs HTTPS (`grovy tls init`); on iPhone, grovy opened from the
// home screen. To turn it off: grovy.disable("push") in custom.js.
grovy.extension("push", grovy => {
  const {h} = grovy;
  const can = window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  // Registered on every start, so an updated sw.js reaches the device. It caches nothing, so it never stands in the way.
  const reg = can ? navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).catch(() => null)
                  : Promise.resolve(null);

  function why() {
    if (!window.isSecureContext) return "Notifications need grovy over HTTPS. On the computer: grovy tls init, then " +
      "install its certificate on this device (it says how).";
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));
    if (ios) return "On iPhone, notifications only work in grovy opened from the home screen: Share → Add to Home " +
      "Screen, then open it from there.";
    return "This browser can't receive push notifications.";
  }
  const key = b64 => {
    const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - b64.length % 4) % 4));
    return Uint8Array.from(s, c => c.charCodeAt(0));
  };
  const same = (a, b) => !!a && a.byteLength === b.byteLength && new Uint8Array(a).every((x, i) => x === b[i]);
  const call = (path, endpoint) => grovy.api("/api/x/push/" + path, {endpoint});

  // Ready before any tap: iPhone only lets subscribe() run inside the tap itself, so nothing may be awaited first.
  let registration = null, server = null;
  if (can) {
    reg.then(r => { registration = r; });
    grovy.api("/api/x/push/key").then(k => { server = key(k.key); }).catch(() => {});
  }

  async function turnOn(subscribing) {
    try {
      const sub = await subscribing;
      await call("subscribe", sub.endpoint);
      await call("test", sub.endpoint);
      grovy.say("Notifications are on for this device: a test is on its way. You'll hear when a session needs you, " +
                "finishes or fails.");
    } catch (e) {
      grovy.say(Notification.permission === "denied"
        ? "Notifications are blocked for grovy in this browser's settings: allow them there, then try again."
        : "Couldn't turn notifications on: " + e.message);
    }
  }

  function settings(sub) {
    const btn = (label, cls, run) => h("button", {class: "btn " + cls, onclick: run}, label);
    grovy.sheet(h("p", {style: "margin:4px 0 16px"}, "Notifications are on for this device."),
      h("div", {style: "display:flex;gap:10px;flex-wrap:wrap"},
        btn("Send a test", "", async () => {
          grovy.closeSheet();
          try { await call("test", sub.endpoint); } catch (e) { grovy.say("The test failed: " + e.message); }
        }),
        btn("Turn off", "deny", async () => {
          grovy.closeSheet();
          try { await call("unsubscribe", sub.endpoint); } catch (e) { /* the device stops listening either way */ }
          await sub.unsubscribe();
          grovy.say("Notifications are off for this device.");
        }),
        btn("Close", "", grovy.closeSheet)));
  }

  grovy.menu("Notifications", () => {
    if (!can) return grovy.say(why());
    if (!registration || !server) return grovy.say("grovy isn't ready for notifications yet (or its push plugin is " +
                                                   "off): try again in a moment.");
    // Called right in the tap, first thing: subscribe() asks for permission itself where needed.
    const subscribing = registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: server});
    subscribing.catch(() => {});
    (async () => {
      const sub = await registration.pushManager.getSubscription();
      if (sub && !same(sub.options.applicationServerKey, server)) {   // made for another grovy key: start over
        await sub.unsubscribe();
        return grovy.say("grovy's notification key has changed: tap ⚙ → Notifications again to turn them back on.");
      }
      if (sub && Notification.permission === "granted") {
        try { await call("subscribe", sub.endpoint); } catch (e) { /* settings still work; a test will tell */ }
        return settings(sub);
      }
      turnOn(subscribing);
    })();
  }, {where: "home", sub: "Push notifications on this device"});

  // Keep grovy's list right: a subscribed device tells grovy again on each start (grovy may have dropped it after a
  // failed push). Never unsubscribe here: without a tap, the device couldn't subscribe again.
  if (can && Notification.permission === "granted")
    reg.then(r => r && r.pushManager.getSubscription()).then(sub => sub && call("subscribe", sub.endpoint)).catch(() => {});
}, {about: "⚙ → Notifications"});
