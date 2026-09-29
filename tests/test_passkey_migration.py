import importlib.util
import os
import sys
import tempfile
import types
import unittest

from cryptography.fernet import Fernet


class _FakeClient:
    def auth_test(self):
        return {"user_id": "UBOT", "user": "prox5", "team": "test", "team_id": "T1"}


class _FakeApp:
    def __init__(self, **_kwargs):
        self.client = _FakeClient()

    @staticmethod
    def _decorator(*_args, **_kwargs):
        return lambda function: function

    event = command = action = view = _decorator


class _FakeSlackApiError(Exception):
    pass


class PasskeyMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ.update({
            "SLACK_BOT_TOKEN": "xoxb-test",
            "CONFESSIONS_CHANNEL_ID": "C_PUBLIC",
            "REVIEW_CHANNEL_ID": "C_REVIEW",
            "CONFESSIONS_KEY": Fernet.generate_key().decode(),
            "DB_PATH": os.path.join(cls.tempdir.name, "test.db"),
        })

        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda: None
        slack_bolt = types.ModuleType("slack_bolt")
        slack_bolt.App = _FakeApp
        socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
        socket_mode.SocketModeHandler = object
        slack_errors = types.ModuleType("slack_sdk.errors")
        slack_errors.SlackApiError = _FakeSlackApiError
        sys.modules.update({
            "dotenv": dotenv,
            "slack_bolt": slack_bolt,
            "slack_bolt.adapter": types.ModuleType("slack_bolt.adapter"),
            "slack_bolt.adapter.socket_mode": socket_mode,
            "slack_sdk": types.ModuleType("slack_sdk"),
            "slack_sdk.errors": slack_errors,
        })

        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        sys.path.insert(0, app_dir)
        spec = importlib.util.spec_from_file_location("prox5_app_test", os.path.join(app_dir, "app.py"))
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.module.db.close()
        cls.tempdir.cleanup()

    def setUp(self):
        self.module.db.execute("DELETE FROM relays")
        self.module.db.execute("DELETE FROM confessions")
        self.module.db.execute("DELETE FROM passkey_accounts")
        self.module.db.commit()
        self.module._account_cipher_cache.clear()

    def _insert_legacy_confession(self, channel="D_USER", text="legacy secret"):
        module = self.module
        module.db.execute(
            """INSERT INTO confessions
                   (user_enc, dm_channel_enc, dm_index, dm_ts, text_enc)
               VALUES (?,?,?,?,?)""",
            (
                "",
                module.legacy_enc(channel),
                module.dm_index(channel),
                "123.456",
                module.legacy_enc(text),
            ),
        )
        module.db.commit()

    def test_registration_migrates_legacy_ciphertext(self):
        self._insert_legacy_confession()
        created = self.module.set_user_passkey("D_USER", "my personally supplied passkey")

        self.assertTrue(created)
        row = self.module.q1("SELECT * FROM confessions")
        account = self.module.account_for_dm("D_USER")
        self.assertTrue(row["dm_channel_enc"].startswith("pk1:"))
        self.assertTrue(row["text_enc"].startswith("pk1:"))
        self.assertNotIn("my personally supplied passkey", account["passkey_enc"])
        self.assertEqual("D_USER", self.module.decrypt_for_confession(row, "dm_channel_enc"))
        self.assertEqual("legacy secret", self.module.decrypt_for_confession(row, "text_enc"))

    def test_rotation_is_atomic_and_requires_current_passkey(self):
        self._insert_legacy_confession()
        first = "the first user supplied passkey"
        second = "the replacement user supplied passkey"
        self.module.set_user_passkey("D_USER", first)
        before = self.module.q1("SELECT * FROM confessions")["text_enc"]

        with self.assertRaises(self.module.WrongCurrentPasskey):
            self.module.set_user_passkey("D_USER", second, "not the current passkey")
        self.assertEqual(before, self.module.q1("SELECT * FROM confessions")["text_enc"])

        created = self.module.set_user_passkey("D_USER", second, first)
        row = self.module.q1("SELECT * FROM confessions")
        self.assertFalse(created)
        self.assertNotEqual(before, row["text_enc"])
        self.assertEqual("legacy secret", self.module.decrypt_for_confession(row, "text_enc"))

    def test_recovery_rotation_does_not_require_current_passkey(self):
        self._insert_legacy_confession()
        first = "the original user supplied passkey"
        replacement = "a recovered user supplied passkey"
        self.module.set_user_passkey("D_USER", first)
        before = self.module.q1("SELECT * FROM confessions")["text_enc"]

        created = self.module.set_user_passkey(
            "D_USER",
            replacement,
            allow_without_current=True,
        )

        row = self.module.q1("SELECT * FROM confessions")
        self.assertFalse(created)
        self.assertNotEqual(before, row["text_enc"])
        self.assertEqual("legacy secret", self.module.decrypt_for_confession(row, "text_enc"))


if __name__ == "__main__":
    unittest.main()
