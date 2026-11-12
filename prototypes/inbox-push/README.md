# Throwaway: web push proof for the PAM Inbox

Never merge. Ticket: https://github.com/RobinHoodO/voice-agent/issues/12

Question: does a web push reach Robin's phone from the Thrivbe-1 tailnet origin
`https://hetzner.tail9908c7.ts.net:8445`?

- Keys: 1Password, vault `Thrivbe-Core`, item `Web Push VAPID key pair - PAM Inbox push sender - Thrivbe-1`.
  Made once with:
  `node make-vapid-item.mjs | op item create --account thrivbeas.1password.eu --vault Thrivbe-Core --format json | python3 -c "import sys,json; d=json.load(sys.stdin); print('item', d['title'], d['id'])"`
- Run on Thrivbe-1: `oprun --env-file op.env -- node server.mjs`, behind
  `tailscale serve --bg --https=8445 http://127.0.0.1:8795`.
- Self-check: `node server.mjs --selftest`.
- Devices are stored in `subs.json` next to the server.
