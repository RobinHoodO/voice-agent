"""One secret resolver, two providers.

Resolution order, unchanged from the original `config.secret()`:

  1. the registered **store** — macOS Keychain (`mac.keychain`), nothing on Linux
  2. the **environment fallback** declared per secret in `config._ENV_FALLBACK`
  3. **systemd credentials** — `$CREDENTIALS_DIRECTORY/<name>`, the Linux way to
     hand a unit a secret without putting it in the environment or on disk

Step 3 is new and deliberately LAST: `CREDENTIALS_DIRECTORY` is never set on macOS,
so a Mac resolves exactly as it did before this file existed.
"""
from __future__ import annotations

import os


class SecretStore:
    """A read/write secret store. `get` returning None means 'not here', not 'error'."""

    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> bool:
        return False

    def delete(self, name: str) -> None:
        return None


class NullStore(SecretStore):
    """No writable store — env/credentials only. The Linux default."""


_store: SecretStore = NullStore()


def set_store(store: SecretStore) -> None:
    global _store
    _store = store


def store() -> SecretStore:
    return _store


def _from_credentials_dir(name: str) -> str | None:
    """systemd `LoadCredential=`/`SetCredential=` drops each secret in its own file."""
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if not d:
        return None
    try:
        with open(os.path.join(d, name), encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except Exception:
        return None


def secret(name: str, env_fallback=()) -> str | None:
    """Store first, then env fallback (dev), then systemd credentials. None if nowhere."""
    try:
        v = _store.get(name)
    except Exception:
        v = None
    if v:
        return v
    for env in env_fallback:
        v = os.environ.get(env)
        if v:
            return v
    return _from_credentials_dir(name)


def set_secret(name: str, value: str) -> bool:
    try:
        return bool(_store.set(name, value))
    except Exception:
        return False


def delete_secret(name: str) -> None:
    try:
        _store.delete(name)
    except Exception:
        pass
