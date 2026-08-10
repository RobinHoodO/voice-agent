"""Tailscale, as much of it as the phone surface needs: an address, and a TLS front door.

WHY `tailscale serve` AND NOT A CERTIFICATE
    `getUserMedia` and `AudioWorklet` are secure-context-only. Safari on Robin's phone
    will not hand over the microphone to `http://100.96.126.87:8767` — no prompt, no
    warning he can click through, the call just rejects. So HTTPS is not a hardening
    step here, it is the difference between a working microphone and a dead one.

    `tailscale serve` terminates TLS with a real Let's Encrypt certificate for this
    machine's `*.ts.net` name, which tailscaled fetches and renews on its own. No cert
    files on disk for this app to read, chmod or forget to renew, no port forwarding,
    no LAN exposure — the listener stays on loopback and tailscaled is the only thing
    that talks to it.

THE FLAGS ARE VERIFIED, NOT GUESSED
    Against the installed binary, Tailscale 1.98.10:

        $ /Applications/Tailscale.app/Contents/MacOS/Tailscale \\
              serve --bg --yes --https=8443 http://127.0.0.1:8767
        Available within your tailnet:
        https://robins-macbook-pro.tail9908c7.ts.net:8443/
        |-- proxy http://127.0.0.1:8767
        To disable the proxy, run: tailscale serve --https=8443 off

    `--bg` (background, else it blocks the foreground), `--yes` (no interactive prompt —
    this runs from a menubar click, there is no terminal to answer one), `--https=PORT`,
    and the target as a full URL. Teardown is the `off` target the tool itself names;
    it exits 1 with "handler does not exist" when there is nothing to remove, which is
    a success for our purposes and is treated as one.

NOT PORT 443, AND THIS IS LOAD-BEARING
    443 on this machine is already served: `robins-macbook-pro.tail9908c7.ts.net:443`
    proxies to `127.0.0.1:7432` (trustmux, PID 3154 on 2026-08-10). `serve --https=443`
    would REPLACE that `/` handler and silently break it. The phone surface takes its
    own port and never touches 443 — `serve status` is read before and after, and
    `tests/test_phone_surface.py` holds that line.

DUPLICATION NOTE
    `mac/reverse_channel.py` has its own copy of the binary list and the CGNAT check.
    That file is committed-but-off for a later Thrivbe-1 project and is explicitly not
    to be built on, so this module does not import from it. Twelve duplicated lines is
    the cheaper of the two mistakes.
"""
from __future__ import annotations

import ipaddress
import subprocess
from typing import Callable

# Tailscale hands out CGNAT space. Anything outside it is not the tailnet.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")

# Where the binary lives, in the order worth trying. PATH is consulted LAST so a
# shadowed `tailscale` in a user directory cannot decide what counts as the tailnet.
TAILSCALE_BINARIES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
)

# The one port the phone surface must never serve on. See the module docstring.
RESERVED_TLS_PORTS = frozenset({443})


class TailnetError(RuntimeError):
    """Tailscale could not do what was asked. Carries a sentence Robin can act on."""


def _run(runner: Callable | None, argv: list, timeout: float = 20.0):
    return (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=timeout)


def binary(runner: Callable | None = None) -> str | None:
    """The first Tailscale binary that answers `version`. None if there is none."""
    for path in (*TAILSCALE_BINARIES, "tailscale"):
        try:
            result = _run(runner, [path, "version"], timeout=5)
        except Exception:
            continue
        if getattr(result, "returncode", 1) == 0:
            return path
    return None


def is_tailnet_address(candidate: str) -> bool:
    try:
        return ipaddress.ip_address(str(candidate)) in TAILNET_V4
    except ValueError:
        return False


def addresses(runner: Callable | None = None) -> list:
    """Every IPv4 address `tailscale ip -4` reports for this machine.

    Errors collapse to an empty list: "no tailnet address" is the fail-closed answer for
    both "Tailscale is down" and "Tailscale is not installed".
    """
    path = binary(runner)
    if not path:
        return []
    try:
        result = _run(runner, [path, "ip", "-4"], timeout=5)
    except Exception:
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def dns_name(runner: Callable | None = None) -> str | None:
    """This machine's MagicDNS name (`robins-macbook-pro.tail9908c7.ts.net`).

    Read from `tailscale status --json`, which reports it with a trailing dot.
    """
    path = binary(runner)
    if not path:
        return None
    try:
        result = _run(runner, [path, "status", "--json"], timeout=10)
        if getattr(result, "returncode", 1) != 0:
            return None
        import json
        name = (json.loads(result.stdout or "{}").get("Self") or {}).get("DNSName") or ""
    except Exception:
        return None
    return name.rstrip(".") or None


def serve_status(runner: Callable | None = None) -> dict:
    """The current `tailscale serve` config, or {} if it cannot be read."""
    path = binary(runner)
    if not path:
        return {}
    try:
        result = _run(runner, [path, "serve", "status", "--json"], timeout=10)
        if getattr(result, "returncode", 1) != 0:
            return {}
        import json
        return json.loads(result.stdout or "{}") or {}
    except Exception:
        return {}


def serve_ports(runner: Callable | None = None) -> set:
    """Which TCP ports `tailscale serve` currently answers on, as ints."""
    out = set()
    for port in (serve_status(runner).get("TCP") or {}):
        try:
            out.add(int(port))
        except (TypeError, ValueError):
            continue
    return out


def serve_url(tls_port: int, runner: Callable | None = None) -> str | None:
    """The https:// URL a phone should open, or None if the name is unknown."""
    name = dns_name(runner)
    if not name:
        return None
    return f"https://{name}:{tls_port}/"


def serve_start(tls_port: int, target_port: int, runner: Callable | None = None) -> str:
    """Publish `http://127.0.0.1:<target_port>` at `https://<this machine>:<tls_port>/`.

    Returns the URL. Raises `TailnetError` with something Robin can act on — this is
    called from a menubar click and the failure has to arrive as a sentence, not a
    traceback in a log file he is not reading.
    """
    if int(tls_port) in RESERVED_TLS_PORTS:
        raise TailnetError(
            f"port {tls_port} is already this machine's main tailnet front door "
            f"(trustmux). Serving the phone surface there would replace it — pick "
            f"another port in config `phone_surface.tls_port`.")
    path = binary(runner)
    if not path:
        raise TailnetError("Tailscale is not installed where this app can find it, so "
                           "there is no way to get HTTPS — and without HTTPS the phone "
                           "will not give up its microphone.")
    if not addresses(runner):
        raise TailnetError("Tailscale is not up on this Mac. Bring it up, then turn the "
                           "phone surface on again.")
    argv = [path, "serve", "--bg", "--yes", f"--https={int(tls_port)}",
            f"http://127.0.0.1:{int(target_port)}"]
    try:
        result = _run(runner, argv)
    except Exception as e:
        raise TailnetError(f"could not run tailscale serve: {e!r}") from e
    if getattr(result, "returncode", 1) != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip().splitlines()
        raise TailnetError(f"tailscale serve failed: {detail[0] if detail else 'unknown error'}")
    return serve_url(int(tls_port), runner) or f"https://<this machine>:{tls_port}/"


def serve_stop(tls_port: int, runner: Callable | None = None) -> bool:
    """Take the phone surface off the tailnet. True if there is nothing there now.

    "handler does not exist" is the tool's way of saying it was already off; that is
    the desired end state, so it counts as success rather than an error Robin has to
    read on his way to lunch.
    """
    path = binary(runner)
    if not path:
        return True
    try:
        result = _run(runner, [path, "serve", f"--https={int(tls_port)}", "off"])
    except Exception:
        return False
    if getattr(result, "returncode", 1) == 0:
        return True
    return "does not exist" in ((result.stderr or "") + (result.stdout or ""))


def resolve_bind_host(candidate: str | None = None, runner: Callable | None = None) -> str:
    """The address the phone surface's HTTP listener may bind. Loopback by default.

    Loopback IS the right default and not a weaker one: `tailscale serve` runs inside
    tailscaled on this machine and connects to the target over loopback, so binding
    anything wider only adds reachability nobody needs. A tailnet address is allowed
    for the case where Robin wants to hit the plain port directly while debugging.

    Everything else is REFUSED, including `0.0.0.0` and `::` — refused by the rule
    ("loopback or a tailnet address this machine actually holds"), not by a special
    case that a later edit could delete.
    """
    if candidate in (None, "", "loopback"):
        return "127.0.0.1"
    candidate = str(candidate)
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        raise TailnetError(
            f"{candidate!r} is not an IP address. The phone surface binds loopback or a "
            f"Tailscale address, nothing else.") from None
    if parsed.is_loopback:
        return candidate
    if not is_tailnet_address(candidate):
        raise TailnetError(
            f"refusing to bind {candidate!r}: the phone surface binds loopback or a "
            f"Tailscale address (100.64.0.0/10). 0.0.0.0 would put Robin's agent on "
            f"every network this laptop joins.")
    held = addresses(runner)
    if held and candidate not in held:
        raise TailnetError(f"{candidate!r} is not an address this Mac holds on the "
                           f"tailnet ({', '.join(held)}).")
    return candidate
