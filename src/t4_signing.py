"""Canonical Ed25519 signing helpers for public T4 records."""
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class SigningKeyError(RuntimeError):
    """Raised when persistent signing-key material is absent or invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def signing_key_path(path: Optional[os.PathLike] = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("DOGFOOD_SIGNING_KEY_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "data" / "t4_ed25519_private.pem"


def load_or_create_private_key(path: Optional[os.PathLike] = None) -> Ed25519PrivateKey:
    key_path = signing_key_path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        pem = key_path.read_bytes()
    except FileNotFoundError:
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        try:
            descriptor = os.open(
                str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(pem)
                key_file.flush()
                os.fsync(key_file.fileno())
        except FileExistsError:
            pem = key_path.read_bytes()
        except OSError as exc:
            raise SigningKeyError(f"Unable to persist signing key at {key_path}") from exc

    try:
        loaded_key = serialization.load_pem_private_key(pem, password=None)
    except (TypeError, ValueError) as exc:
        raise SigningKeyError(f"Signing key at {key_path} is invalid; it was not replaced") from exc
    if not isinstance(loaded_key, Ed25519PrivateKey):
        raise SigningKeyError(f"Signing key at {key_path} is not an Ed25519 private key")
    return loaded_key


def public_key_info(private_key: Ed25519PrivateKey) -> dict:
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "key_id": hashlib.sha256(public_bytes).hexdigest()[:24],
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }


def sign_payload(payload: dict, private_key: Optional[Ed25519PrivateKey] = None) -> dict:
    key = private_key or load_or_create_private_key()
    signature = key.sign(canonical_json_bytes(payload))
    return {
        **public_key_info(key),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_payload(payload: dict, signature: str, public_key: str) -> bool:
    try:
        public_bytes = base64.b64decode(public_key, validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes, canonical_json_bytes(payload)
        )
        return True
    except (InvalidSignature, TypeError, ValueError, UnicodeEncodeError):
        return False