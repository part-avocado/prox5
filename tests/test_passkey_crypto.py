import os
import sys
import unittest

from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from passkey_crypto import (  # noqa: E402
    decrypt_content,
    encrypt_content,
    new_salt,
    unwrap_passkey,
    validate_passkey,
    wrap_passkey,
)


class PasskeyCryptoTests(unittest.TestCase):
    def test_round_trip_and_wrapped_passkey(self):
        deployment_cipher = Fernet(Fernet.generate_key())
        passkey = "correct horse battery staple"
        salt = new_salt()

        wrapped = wrap_passkey(passkey, deployment_cipher)
        self.assertNotIn(passkey, wrapped)
        self.assertEqual(passkey, unwrap_passkey(wrapped, deployment_cipher))

        encrypted = encrypt_content("private submission", passkey, salt)
        self.assertNotIn("private submission", encrypted)
        self.assertEqual("private submission", decrypt_content(encrypted, passkey, salt))

    def test_users_with_different_passkeys_are_isolated(self):
        salt = new_salt()
        encrypted = encrypt_content("user one", "this is user one's passkey", salt)
        with self.assertRaises(InvalidToken):
            decrypt_content(encrypted, "this is user two's passkey", salt)

    def test_salts_isolate_users_with_same_passkey(self):
        passkey = "a shared but valid passkey"
        first = encrypt_content("same", passkey, new_salt())
        second = encrypt_content("same", passkey, new_salt())
        self.assertNotEqual(first, second)

    def test_short_passkey_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least 12"):
            validate_passkey("too short")


if __name__ == "__main__":
    unittest.main()
