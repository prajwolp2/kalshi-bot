"""Kalshi request signing (needs the `cryptography` package; deploy/setup.sh installs it).

Each request carries three headers: the key ID, a timestamp in milliseconds, and a signature of
timestamp + METHOD + path (path from the root, without the query string). RSA keys sign with
RSA-PSS / SHA-256 (salt = digest length); Ed25519 keys sign the message directly.
"""

from __future__ import annotations

import base64
import stat
from pathlib import Path


def message(ts_ms: int, method: str, path: str) -> bytes:
    return f"{ts_ms}{method.upper()}{path.split('?')[0]}".encode()


class Signer:
    def __init__(self, key_id: str, key_path: str):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

        if not key_id:
            raise ValueError("KALSHI_KEY_ID is empty")
        path = Path(key_path).expanduser()
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"{path} can be read by other users on this machine; run: chmod 600 {path}")
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        self.key_id = key_id
        if isinstance(key, rsa.RSAPrivateKey):
            pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
            self._sign = lambda m: key.sign(m, pss, hashes.SHA256())
            self.kind = "RSA"
        elif isinstance(key, ed25519.Ed25519PrivateKey):
            self._sign = key.sign
            self.kind = "Ed25519"
        else:
            raise ValueError(f"unsupported key type {type(key).__name__}: Kalshi keys are RSA or Ed25519")

    def headers(self, method: str, path: str, ts_ms: int) -> dict:
        sig = self._sign(message(ts_ms, method, path))
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
