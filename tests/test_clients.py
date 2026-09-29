"""Client app tokens: shown once, stored as digests, checked in memory, revocable."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from control.app import clients


class ClientTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        for p in (patch.object(clients, "CLIENTS_PATH", os.path.join(self.temp.name, "c.json")),
                  patch.object(clients.cfg, "DATA_DIR", self.temp.name)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)
        clients._load()
        self.addCleanup(clients._load)

    def stored(self):
        with open(clients.CLIENTS_PATH, encoding="utf-8") as handle:
            return json.load(handle)

    def test_register_returns_a_prefixed_token_once_and_stores_only_its_digest(self):
        client, token = clients.register("Chi's iPhone", "ios", "1.0")
        self.assertTrue(token.startswith(clients.TOKEN_PREFIX))
        self.assertNotIn("token_hash", client)
        text = json.dumps(self.stored())
        self.assertNotIn(token, text)
        self.assertIn(clients._digest(token), text)
        self.assertEqual(clients.resolve(token)["id"], client["id"])

    def test_unknown_malformed_or_missing_tokens_resolve_to_nothing(self):
        clients.register("phone", "ios")
        for token in (None, "", "mdd_c1_guess", "not-even-prefixed"):
            with self.subTest(token=token):
                self.assertIsNone(clients.resolve(token))

    def test_unknown_platform_is_refused_and_names_are_bounded(self):
        with self.assertRaises(clients.ClientError):
            clients.register("x", "symbian")
        client, _ = clients.register("n" * 500, "android")
        self.assertEqual(len(client["name"]), clients.NAME_MAX)
        self.assertEqual(clients.register("", "ios")[0]["name"], "iPhone")

    def test_checking_a_token_does_not_touch_the_disk_within_the_skew(self):
        _, token = clients.register("phone", "ios")
        with patch.object(clients, "_save") as save, patch("builtins.open") as opened:
            for _ in range(50):
                self.assertIsNotNone(clients.resolve(token))
        save.assert_not_called()
        opened.assert_not_called()

    def test_use_slides_the_expiry_forward_after_the_skew(self):
        client, token = clients.register("phone", "ios")
        later = time.time() + clients.LAST_SEEN_SKEW + 60
        with patch.object(clients.time, "time", return_value=later):
            record = clients.resolve(token)
        self.assertEqual(record["expires_at"], int(later) + clients.TOKEN_TTL)
        self.assertGreater(self.stored()["clients"][0]["last_seen"], client["last_seen"])

    def test_an_expired_token_stops_working_and_is_forgotten(self):
        _, token = clients.register("phone", "ios")
        with patch.object(clients.time, "time", return_value=time.time() + clients.TOKEN_TTL + 1):
            self.assertIsNone(clients.resolve(token))
        self.assertEqual(clients.list_clients(), [])

    def test_revoke_one_and_revoke_all(self):
        first, first_token = clients.register("one", "ios")
        _, second_token = clients.register("two", "android")
        self.assertTrue(clients.revoke(first["id"]))
        self.assertFalse(clients.revoke(first["id"]))
        self.assertIsNone(clients.resolve(first_token))
        self.assertIsNotNone(clients.resolve(second_token))
        self.assertEqual(clients.revoke_all(), 1)
        self.assertIsNone(clients.resolve(second_token))
        self.assertEqual(self.stored()["clients"], [])

    def test_tokens_survive_a_restart_and_ids_are_not_reused(self):
        first, token = clients.register("phone", "ios")
        clients.revoke(clients.register("gone", "ios")[0]["id"])
        clients._load()
        self.assertEqual(clients.resolve(token)["id"], first["id"])
        self.assertEqual(clients.register("new", "ios")[0]["id"], first["id"] + 2)

    def test_expired_records_are_dropped_when_listing(self):
        clients.register("old", "ios")
        with patch.object(clients.time, "time", return_value=time.time() + clients.TOKEN_TTL + 1):
            self.assertEqual(clients.list_clients(), [])
        self.assertEqual(self.stored()["clients"], [])

    def test_the_least_recently_used_makes_room_beyond_the_cap(self):
        with patch.object(clients, "MAX_CLIENTS", 3):
            made = [clients.register(f"phone {n}", "ios") for n in range(3)]
            later = time.time() + clients.LAST_SEEN_SKEW + 1
            with patch.object(clients.time, "time", return_value=later):
                clients.resolve(made[0][1])       # the first is still in use
                clients.resolve(made[2][1])
            self.assertEqual(clients.make_room(), [made[1][0]["id"]])
            clients.register("phone 3", "ios")
            self.assertEqual(len(clients.list_clients()), 3)
            self.assertIsNone(clients.resolve(made[1][1]))
            self.assertIsNotNone(clients.resolve(made[0][1]))

    def test_listing_never_shows_the_digest(self):
        clients.register("phone", "ios", "2.1")
        (listed,) = clients.list_clients()
        self.assertEqual(set(listed), {"id", "name", "platform", "app_version", "created_at",
                                       "last_seen", "expires_at"})


if __name__ == "__main__":
    unittest.main()
