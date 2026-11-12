// THROWAWAY push proof for the PAM Inbox map (voice-agent issue 12). Never merge.
// Question it answers: does web push reach Robin's phone from the Thrivbe-1 tailnet origin?
// Run: oprun --env-file op.env -- node server.mjs   (behind `tailscale serve --https=8445`)
import http from "node:http";
import { readFileSync, writeFileSync, renameSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert";

const DIR = path.dirname(fileURLToPath(import.meta.url));
const SUBS = path.join(DIR, "subs.json");
const STATIC = {
  "/": ["index.html", "text/html; charset=utf-8"],
  "/sw.js": ["sw.js", "text/javascript"],
  "/manifest.json": ["manifest.json", "application/manifest+json"],
  "/icon-192.png": ["icon-192.png", "image/png"],
  "/icon-512.png": ["icon-512.png", "image/png"],
};

// Only real push services: stops the server from POSTing to any URL a client sends (SSRF).
export function validSubscription(s) {
  try {
    const u = new URL(s.endpoint);
    const h = u.hostname;
    const hostOk = h === "fcm.googleapis.com" || h === "updates.push.services.mozilla.com" ||
      h.endsWith(".push.apple.com") || h.endsWith(".notify.windows.com");
    const key = (k) => typeof k === "string" && /^[A-Za-z0-9_-]{8,512}$/.test(k);
    return u.protocol === "https:" && hostOk && key(s.keys?.p256dh) && key(s.keys?.auth);
  } catch {
    return false;
  }
}

if (process.argv.includes("--selftest")) {
  const keys = { p256dh: "a".repeat(87), auth: "b".repeat(22) };
  assert(validSubscription({ endpoint: "https://web.push.apple.com/x", keys }));
  assert(validSubscription({ endpoint: "https://fcm.googleapis.com/fcm/send/x", keys }));
  assert(!validSubscription({ endpoint: "https://evil.example/x", keys }));
  assert(!validSubscription({ endpoint: "http://web.push.apple.com/x", keys }));
  assert(!validSubscription({ endpoint: "https://push.apple.com.evil.example/x", keys }));
  assert(!validSubscription({ endpoint: "https://web.push.apple.com/x", keys: { p256dh: "a" } }));
  assert(!validSubscription(null));
  console.log("selftest ok");
  process.exit(0);
}

const { VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT, PORT = "8795" } = process.env;
assert(VAPID_PUBLIC_KEY && VAPID_PRIVATE_KEY && VAPID_SUBJECT, "VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT must be set");
const webpush = (await import("web-push")).default;
const vapidDetails = { subject: VAPID_SUBJECT, publicKey: VAPID_PUBLIC_KEY, privateKey: VAPID_PRIVATE_KEY };

const loadSubs = () => (existsSync(SUBS) ? JSON.parse(readFileSync(SUBS, "utf8")) : []);
const saveSubs = (subs) => {
  writeFileSync(SUBS + ".tmp", JSON.stringify(subs, null, 2));
  renameSync(SUBS + ".tmp", SUBS);
};

async function readJson(req) {
  let body = "";
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 4096) throw new Error("body too large");
  }
  return JSON.parse(body);
}

async function sendAll() {
  // One payload shape for the service worker and for Declarative Web Push (iOS 18.4+).
  const payload = JSON.stringify({
    web_push: 8030,
    notification: {
      title: "PAM Inbox test",
      body: `Push from Thrivbe-1 at ${new Date().toLocaleTimeString("nb-NO", { timeZone: "Europe/Oslo" })}`,
      navigate: `${VAPID_SUBJECT}/`,
    },
  });
  const results = [];
  let subs = loadSubs();
  for (const sub of subs) {
    const host = new URL(sub.endpoint).hostname;
    try {
      const r = await webpush.sendNotification(sub, payload, { vapidDetails, TTL: 3600, timeout: 5000 });
      results.push({ host, status: r.statusCode });
    } catch (e) {
      results.push({ host, status: e.statusCode ?? 0, error: String(e.body || e.message).slice(0, 200) });
      // 404/410 = the subscription is dead: drop it.
      if (e.statusCode === 404 || e.statusCode === 410) subs = subs.filter((s) => s.endpoint !== sub.endpoint);
    }
  }
  saveSubs(subs);
  return results;
}

// shortcut: no auth, the tailnet is the only gate; the real build puts these routes behind the kernel token.
http.createServer(async (req, res) => {
  const json = (code, obj) => {
    res.writeHead(code, { "content-type": "application/json" });
    res.end(JSON.stringify(obj));
  };
  try {
    const url = new URL(req.url, "http://x").pathname;
    if (req.method === "GET" && STATIC[url]) {
      const [file, type] = STATIC[url];
      res.writeHead(200, { "content-type": type, "cache-control": "no-cache" });
      return res.end(readFileSync(path.join(DIR, file)));
    }
    if (req.method === "GET" && url === "/vapid-public-key") return json(200, { key: VAPID_PUBLIC_KEY });
    if (req.method === "POST" && url === "/subscribe") {
      const sub = await readJson(req);
      if (!validSubscription(sub)) return json(400, { error: "bad subscription" });
      const clean = { endpoint: sub.endpoint, keys: { p256dh: sub.keys.p256dh, auth: sub.keys.auth } };
      saveSubs([...loadSubs().filter((s) => s.endpoint !== clean.endpoint), clean]);
      console.log("subscribed", new URL(clean.endpoint).hostname);
      return json(200, { ok: true });
    }
    if (req.method === "POST" && url === "/test") {
      const results = await sendAll();
      console.log("test", JSON.stringify(results));
      return json(200, { results });
    }
    json(404, { error: "not found" });
  } catch (e) {
    console.error(e);
    json(500, { error: "server error" });
  }
}).listen(Number(PORT), "127.0.0.1", () => console.log(`push proof on 127.0.0.1:${PORT}`));
