// Push handlers copied from .tmp/web-push-kit/reference/public/sw.js. No caching in this proof.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  event.waitUntil((async () => {
    let notification = {};
    try {
      const data = event.data ? event.data.json() : null;
      if (data && typeof data === "object" && data.notification && typeof data.notification === "object") {
        notification = data.notification;
      }
    } catch {
      // The notification below is still shown: Safari revokes subscriptions that push silently.
    }
    const title = typeof notification.title === "string" && notification.title ? notification.title : "PAM Inbox";
    const body = typeof notification.body === "string" && notification.body
      ? notification.body
      : "An agent waits for you.";
    const navigate = typeof notification.navigate === "string" && notification.navigate
      ? notification.navigate
      : `${self.location.origin}/`;
    const options = { body, icon: "/icon-192.png", data: { navigate } };
    if (typeof notification.tag === "string" && notification.tag) options.tag = notification.tag;
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  let url;
  try {
    url = new URL(event.notification.data?.navigate || "/", self.location.origin);
    if (url.origin !== self.location.origin) url = new URL("/", self.location.origin);
  } catch {
    url = new URL("/", self.location.origin);
  }
  event.waitUntil((async () => {
    try {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const existing = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (existing) {
        await existing.focus();
        if (typeof existing.navigate === "function") {
          await existing.navigate(url.toString());
          return;
        }
      }
    } catch {
      // Fall through and open a fresh window.
    }
    try {
      await self.clients.openWindow(url.toString());
    } catch {
      // The browser refused both paths.
    }
  })());
});
