"""macOS Keychain secret store — the `security` CLI, lifted verbatim out of config.py.

This is the only place in the tree that shells out to `security`. `install()` plugs it
into `core.secrets`, which is what `config.secret()` consults.
"""
import subprocess

from core import secrets
from core.config import KEYCHAIN_SERVICE


class KeychainStore(secrets.SecretStore):
    def get(self, name: str) -> str | None:
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
        return None

    def set(self, name: str, value: str) -> bool:
        """Store/overwrite a secret in the Keychain (-U updates if present)."""
        try:
            r = subprocess.run(
                ["security", "add-generic-password", "-U",
                 "-s", KEYCHAIN_SERVICE, "-a", name, "-w", value],
                capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False

    def delete(self, name: str) -> None:
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
                capture_output=True, text=True)
        except Exception:
            pass


def install() -> None:
    secrets.set_store(KeychainStore())
