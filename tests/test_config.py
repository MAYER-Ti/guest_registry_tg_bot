import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ConfigError, get_db_path, load_config


TOKEN = "123456789:" + "A" * 35


class ConfigTests(unittest.TestCase):
    def test_four_staff_share_explicit_allowlist(self):
        with patch.dict(os.environ, {"BOT_TOKEN": TOKEN, "ALLOWED_USER_IDS": "10, 20,30,40"}, clear=True):
            config = load_config()
        self.assertEqual(config.allowed_user_ids, frozenset({10, 20, 30, 40}))
        self.assertEqual(config.db_path, Path("data/guests.sqlite3"))
        self.assertEqual(config.token, TOKEN)
        self.assertNotIn(TOKEN, repr(config))

    def test_missing_or_malformed_allowlist_always_fails_closed(self):
        for value in (None, "", " ", "10,", ",10", "10,,20", "0", "-10", "+10", "01", "name", "1.0", "١"):
            with self.subTest(value=value):
                environment = {"BOT_TOKEN": TOKEN}
                if value is not None:
                    environment["ALLOWED_USER_IDS"] = value
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ConfigError):
                        load_config()

    def test_tokens_never_appear_in_validation_errors(self):
        for token in (None, "", "secret", "123:secret", TOKEN + " secret", "0:" + "A" * 35):
            with self.subTest(token=token):
                environment = {"ALLOWED_USER_IDS": "10"}
                if token is not None:
                    environment["BOT_TOKEN"] = token
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ConfigError) as raised:
                        load_config()
                if token:
                    self.assertNotIn(token, str(raised.exception))

    def test_valid_token_not_exposed_when_allowlist_fails(self):
        with patch.dict(os.environ, {"BOT_TOKEN": TOKEN}, clear=True):
            with self.assertRaises(ConfigError) as raised:
                load_config()
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_duplicates_are_deduplicated(self):
        with patch.dict(os.environ, {"BOT_TOKEN": TOKEN, "ALLOWED_USER_IDS": "10,10"}, clear=True):
            self.assertEqual(load_config().allowed_user_ids, frozenset({10}))

    def test_storage_path_precedence_and_backup_needs_no_token(self):
        with patch.dict(os.environ, {"DATA_DIR": "/app/data"}, clear=True):
            self.assertEqual(get_db_path(), Path("/app/data/guests.sqlite3"))
        with patch.dict(os.environ, {"DATA_DIR": "unused", "DB_PATH": "custom/guests.sqlite3"}, clear=True):
            self.assertEqual(get_db_path(), Path("custom/guests.sqlite3"))

    def test_empty_and_memory_storage_are_rejected(self):
        for environment in ({"DB_PATH": ""}, {"DB_PATH": " "}, {"DB_PATH": ":memory:"}, {"DATA_DIR": ""}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ConfigError):
                    get_db_path()


if __name__ == "__main__":
    unittest.main()
