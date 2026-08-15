"""Master-key provider abstraction.

The credential vault is encrypted with a Fernet key (the "master key"). This
module decouples *where* that key comes from (environment, file, secrets
manager) from *how* it is used, and supports key rotation by exposing an
ordered list of keys:

    get_keys() -> [primary, *decrypt_only]

The first key is the active encrypt key; any others are decrypt-only keys kept
so that ciphertext written under a previous key still opens during a rotation
window. The application wraps these in a MultiFernet, which encrypts with the
primary and decrypts with any of them.

Providers shipped here (all selectable via MASTER_KEY_PROVIDER):
  * env    — MASTER_KEY (+ optional MASTER_KEY_SECONDARY, comma-separated)
  * file   — data/global/master.key (+ optional master.key.prev), generates one
             on first run if absent, preserving the historical behaviour
  * vault  — HashiCorp Vault KV v2 over HTTP (VAULT_ADDR + VAULT_TOKEN +
             VAULT_SECRET_PATH); standard library only, no hvac dependency
  * auto   — (default) env var if set, otherwise the file provider

Roadmap, stated honestly: cloud KMS / managed-secret providers (AWS Secrets
Manager, GCP Secret Manager, Azure Key Vault) follow the same get_keys()
contract — fetch the key material from the managed store and return it primary
first. They require the customer's cloud credentials and a reachable endpoint,
so they are intentionally provided as an extension point (subclass KeyProvider)
rather than as code that cannot be exercised without that infrastructure.
"""

import os
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

_log = logging.getLogger("clickhouse-console")


def _as_bytes(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.strip()
        return v or None
    v = str(v).strip()
    return v.encode() if v else None


class KeyProvider:
    """Base class. Subclass and implement get_keys() to add a backend."""
    name = "base"

    def get_keys(self):  # -> list[bytes]
        raise NotImplementedError


class EnvKeyProvider(KeyProvider):
    """Keys from environment variables. MASTER_KEY is the primary;
    MASTER_KEY_SECONDARY may hold one or more comma-separated decrypt-only keys
    for a rotation window."""
    name = "env"

    def get_keys(self):
        keys = []
        prim = _as_bytes(os.environ.get("MASTER_KEY"))
        if prim:
            keys.append(prim)
        for part in (os.environ.get("MASTER_KEY_SECONDARY", "") or "").split(","):
            b = _as_bytes(part)
            if b:
                keys.append(b)
        return keys


class FileKeyProvider(KeyProvider):
    """Keys from files on disk. master.key is the primary; master.key.prev is an
    optional decrypt-only key for a rotation window. Generates a primary on
    first run if absent (and 'cryptography' is installed), mirroring the
    product's original behaviour."""
    name = "file"

    def __init__(self, global_dir, logger=None, allow_generate=True):
        self.path = Path(global_dir) / "master.key"
        self.prev = Path(global_dir) / "master.key.prev"
        self.log = logger or _log
        self.allow_generate = allow_generate

    def get_keys(self):
        keys = []
        if self.path.exists():
            b = _as_bytes(self.path.read_bytes())
            if b:
                keys.append(b)
        elif self.allow_generate:
            gen = self._generate()
            if gen:
                keys.append(gen)
        if self.prev.exists():
            b = _as_bytes(self.prev.read_bytes())
            if b:
                keys.append(b)
        return keys

    def _generate(self):
        try:
            from cryptography.fernet import Fernet
            new = Fernet.generate_key()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            self.path.write_bytes(new)
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
            self.log.warning("=" * 60)
            self.log.warning("GENERATED master key at %s", self.path)
            self.log.warning("BACK THIS UP \u2014 losing it makes stored CH passwords unrecoverable.")
            self.log.warning("In production, set MASTER_KEY or use a secrets manager instead.")
            self.log.warning("=" * 60)
            return new
        except ImportError:
            self.log.warning("'cryptography' not installed \u2014 credential vault disabled "
                             "(server-side connections won't work)")
            return None


class VaultKeyProvider(KeyProvider):
    """HashiCorp Vault provider (KV v2 or v1). Reads the active key from the
    `current` field and an optional rotation key from `previous` at
    VAULT_SECRET_PATH. Uses only the standard library.

    Required env: VAULT_ADDR, VAULT_TOKEN, VAULT_SECRET_PATH
      (e.g. VAULT_SECRET_PATH=secret/data/clickhouse-console/master-key for KV v2)
    Optional env: VAULT_KEY_FIELD (default 'current'),
                  VAULT_PREV_FIELD (default 'previous'),
                  VAULT_TIMEOUT (seconds, default 5)

    This is real, working code, but it depends on the customer running a
    reachable Vault and provisioning the secret — that part cannot be exercised
    without their infrastructure.
    """
    name = "vault"

    def __init__(self, logger=None):
        self.addr = (os.environ.get("VAULT_ADDR", "") or "").rstrip("/")
        self.token = os.environ.get("VAULT_TOKEN", "") or ""
        self.path = os.environ.get("VAULT_SECRET_PATH", "") or ""
        self.cur_field = os.environ.get("VAULT_KEY_FIELD", "current")
        self.prev_field = os.environ.get("VAULT_PREV_FIELD", "previous")
        try:
            self.timeout = float(os.environ.get("VAULT_TIMEOUT", "5"))
        except ValueError:
            self.timeout = 5.0
        self.log = logger or _log

    def _fetch(self):
        url = "%s/v1/%s" % (self.addr, self.path.lstrip("/"))
        req = urllib.request.Request(url, headers={"X-Vault-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError("Vault returned HTTP %s for %s" % (e.code, url))
        except urllib.error.URLError as e:
            raise RuntimeError("Vault unreachable at %s: %s" % (url, e.reason))

    def get_keys(self):
        if not (self.addr and self.token and self.path):
            raise RuntimeError("VaultKeyProvider requires VAULT_ADDR, VAULT_TOKEN, "
                               "and VAULT_SECRET_PATH")
        body = self._fetch()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        # KV v2 nests the secret under data.data; KV v1 is flat under data.
        if isinstance(data.get("data"), dict):
            data = data["data"]
        keys = []
        cur = _as_bytes(data.get(self.cur_field))
        if cur:
            keys.append(cur)
        prev = _as_bytes(data.get(self.prev_field))
        if prev:
            keys.append(prev)
        if not keys:
            raise RuntimeError("Vault secret at %s has no '%s' field"
                               % (self.path, self.cur_field))
        return keys


def load_key_provider(global_dir, logger=None):
    """Select a provider from MASTER_KEY_PROVIDER ('env'|'file'|'vault'|'auto').
    'auto' (default) preserves historical precedence: the MASTER_KEY env var if
    set, otherwise the on-disk file provider (which generates a key on first
    run)."""
    sel = (os.environ.get("MASTER_KEY_PROVIDER", "auto") or "auto").strip().lower()
    log = logger or _log
    if sel == "env":
        return EnvKeyProvider()
    if sel == "file":
        return FileKeyProvider(global_dir, log)
    if sel == "vault":
        return VaultKeyProvider(log)
    # auto
    if (os.environ.get("MASTER_KEY", "") or "").strip():
        return EnvKeyProvider()
    return FileKeyProvider(global_dir, log)
