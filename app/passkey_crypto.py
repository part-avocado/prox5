# salts my passkey

import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


CIPHERTEXT_PREFIX = "pk1:"
MIN_PASSKEY_LENGTH = 6
MAX_PASSKEY_LENGTH = 256


def validate_passkey(passkey: str) -> str:
    if not isinstance(passkey, str):
        raise ValueError("Passkey must be text.")
    if len(passkey) < MIN_PASSKEY_LENGTH:
        raise ValueError(f"Passkey must be at least {MIN_PASSKEY_LENGTH} characters.")
    if len(passkey) > MAX_PASSKEY_LENGTH:
        raise ValueError(f"Passkey can be no more than {MAX_PASSKEY_LENGTH} characters.")
    return passkey


def new_salt() -> bytes:
    return os.urandom(16)


def encode_salt(salt: bytes) -> str:
    return base64.urlsafe_b64encode(salt).decode("ascii")


def decode_salt(encoded: str) -> bytes:
    return base64.urlsafe_b64decode(encoded.encode("ascii"))


def content_cipher(passkey: str, salt: bytes) -> Fernet:
    validate_passkey(passkey)
    raw_key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(
        passkey.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt_content(value: str, passkey: str, salt: bytes) -> str:
    token = content_cipher(passkey, salt).encrypt(value.encode("utf-8")).decode("ascii")
    return CIPHERTEXT_PREFIX + token


def decrypt_content(value: str, passkey: str, salt: bytes) -> str:
    if not value.startswith(CIPHERTEXT_PREFIX):
        raise ValueError("Not an encrypted value! :sho:")
    token = value[len(CIPHERTEXT_PREFIX):]
    return content_cipher(passkey, salt).decrypt(token.encode("ascii")).decode("utf-8")


def wrap_passkey(passkey: str, deployment_cipher: Fernet) -> str:
    validate_passkey(passkey)
    return deployment_cipher.encrypt(passkey.encode("utf-8")).decode("ascii")


def unwrap_passkey(wrapped: str, deployment_cipher: Fernet) -> str:
    return deployment_cipher.decrypt(wrapped.encode("ascii")).decode("utf-8")
