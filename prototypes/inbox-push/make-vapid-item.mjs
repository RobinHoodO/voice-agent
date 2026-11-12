// Makes a new VAPID key pair and prints a 1Password item as JSON. The private key goes
// only to stdout, so always pipe this straight into `op item create` (see README.md).
const { publicKey, privateKey } = await crypto.subtle.generateKey(
  { name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"],
);
const pub = Buffer.from(await crypto.subtle.exportKey("raw", publicKey)).toString("base64url");
const { d } = await crypto.subtle.exportKey("jwk", privateKey);
const origin = "https://hetzner.tail9908c7.ts.net:8445";
const notes = `WHAT: Web Push VAPID key pair (P-256) for the PAM Inbox. Fields: public_key (base64url raw point, not secret), private_key (base64url d, secret), subject (VAPID contact).
USED BY: Thrivbe-1. Now: throwaway push proof /opt/Thrivbe-AI/.tmp/inbox-push-proof (op.env: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT). Later: kernel voice-api push sender.
SCOPE: signs pushes to devices that subscribed on ${origin}. No other access.
CREATED: 2026-11-12 by Robin with make-vapid-item.mjs (Claude Code session).
EXPIRES / ROTATE: never expires / make a new pair, update this item, restart the sender. Every device must subscribe again.
REVOKE: delete this item and the subscriptions store. There is no provider dashboard.
CONTEXT: https://github.com/RobinHoodO/voice-agent/issues/12 (map: issues/8)
RELATED: atomic-habits VAPID (another app, do not share)
DELETE WHEN: the PAM Inbox is retired`;
console.log(JSON.stringify({
  title: "Web Push VAPID key pair - PAM Inbox push sender - Thrivbe-1",
  category: "API_CREDENTIAL",
  tags: ["web-push", "pam-inbox", "thrivbe-1"],
  fields: [
    { id: "public_key", label: "public_key", type: "STRING", value: pub },
    { id: "private_key", label: "private_key", type: "CONCEALED", value: d },
    { id: "subject", label: "subject", type: "STRING", value: origin },
    { id: "notesPlain", label: "notesPlain", type: "STRING", purpose: "NOTES", value: notes },
  ],
}));
