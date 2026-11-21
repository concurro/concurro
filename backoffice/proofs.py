"""Short-lived browser command proofs signed only by the Python session authority."""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PROOF_SECONDS = 180


def segment(value: dict) -> bytes:
    """Encode one compact JWT JSON segment without exposing key material."""
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(body).rstrip(b"=")


class CommandSigner:
    """Hold the browser command private key outside the read-only task API."""

    def __init__(self, pem: bytes):
        """Require an RSA key compatible with the Rust RS256 verifier."""
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("command proof key must be RSA")
        self.key = key

    def sign(self, actor: dict, action_id: str, content: dict) -> str:
        """Bind the actor and exact command contents to one short-lived action proof."""
        claims = {
            "actor": actor,
            "action_id": action_id,
            "content": content,
            "exp": int(time.time()) + PROOF_SECONDS,
        }
        message = segment({"alg": "RS256", "typ": "JWT"}) + b"." + segment(claims)
        signature = self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return (message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
