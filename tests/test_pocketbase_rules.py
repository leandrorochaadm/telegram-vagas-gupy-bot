import os
import secrets
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pocketbase_server import PocketBaseServer, pocketbase_bin  # noqa: E402

DATE = "2026-10-24 12:00:00.000Z"
LONG_TEXT = "x" * 6000
LONG_DESCRIPTION = "x" * 50_000
BLOCKED_LIST_STATUSES = (200, 403)
BLOCKED_STATUSES = (404, 403)
BLOCKED_CREATE_STATUSES = (400, 403)

# One valid body per data collection; the factory takes a unique tag to avoid unique indexes.
VALID_BODIES: dict[str, dict] = {
    "sent_jobs": {"link": "https://example.com/{tag}", "published": "2026-10-24", "title": "Title {tag}"},
    "queued_jobs": {
        "link": "https://example.com/{tag}",
        "title": "Title {tag}",
        "company": "Company",
        "published": "2026-10-24",
        "message": "Message {tag}",
        "source": "gupy",
    },
    "alerts": {"label": "gupy", "text": "Text {tag}"},
    "alert_sends": {"sent_at": DATE},
    "brave_searches": {"label": "label-{tag}", "searched_at": DATE},
    "linkedin_sessions": {"cookie_hash": "hash-{tag}", "searched_at": DATE},
    "inhire_tenants": {"slug": "slug-{tag}", "found_at": DATE},
    "inhire_discoveries": {"last_run": DATE, "found": 3},
    "job_descriptions": {
        "link": "https://example.com/{tag}",
        "source": "gupy",
        "title": "Title {tag}",
        "company": "Company",
        "published": "2026-10-24",
        "status": "pending",
        "attempts": 0,
    },
}
# Field changed by the update probe, per collection.
UPDATE_FIELDS: dict[str, tuple[str, object]] = {
    "sent_jobs": ("title", "Changed"),
    "queued_jobs": ("title", "Changed"),
    "alerts": ("text", "Changed"),
    "alert_sends": ("sent_at", "2026-10-25 12:00:00.000Z"),
    "brave_searches": ("searched_at", "2026-10-25 12:00:00.000Z"),
    "linkedin_sessions": ("expired_at", "2026-10-25 12:00:00.000Z"),
    "inhire_tenants": ("found_at", "2026-10-25 12:00:00.000Z"),
    "inhire_discoveries": ("found", 7),
    "job_descriptions": ("status", "saved"),
}


def body_for(collection: str, tag: str) -> dict:
    return {
        key: value.format(tag=tag) if isinstance(value, str) else value
        for key, value in VALID_BODIES[collection].items()
    }


def records_path(collection: str, record_id: str | None = None) -> str:
    base = f"/api/collections/{collection}/records"
    return f"{base}/{record_id}" if record_id else base


@unittest.skipUnless(pocketbase_bin(), "POCKETBASE_BIN not set")
class PocketBaseRulesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = PocketBaseServer(pocketbase_bin())
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        cls.bot_a_password = secrets.token_urlsafe(24)
        cls.bot_b_password = secrets.token_urlsafe(24)
        cls.user_password = secrets.token_urlsafe(24)
        cls.bot_a_id = cls.server.create_auth_account("bot_accounts", "bot-a@test.local", cls.bot_a_password)
        cls.bot_b_id = cls.server.create_auth_account("bot_accounts", "bot-b@test.local", cls.bot_b_password)
        cls.user_id = cls.server.create_auth_account("users", "user@test.local", cls.user_password)
        cls.bot_token = cls.server.login("bot_accounts", "bot-a@test.local", cls.bot_a_password)
        cls.user_token = cls.server.login("users", "user@test.local", cls.user_password)
        cls.seeds = {
            name: cls.server.create_record(name, body_for(name, "seed")) for name in VALID_BODIES
        }

    def req(self, method: str, path: str, token: str | None = None, **kwargs):
        return self.server.request(method, path, token=token, **kwargs)


class BotOnlyRulesTest(PocketBaseRulesTestCase):
    def assert_blocked(self, token: str | None, label: str) -> None:
        for collection, seed in self.seeds.items():
            seed_id = seed["id"]
            with self.subTest(actor=label, collection=collection, op="list"):
                response = self.req("GET", records_path(collection), token)
                self.assertIn(response.status_code, BLOCKED_LIST_STATUSES)
                if response.status_code == 200:
                    self.assertEqual(response.json()["items"], [])
                    self.assertEqual(response.json()["totalItems"], 0)
            with self.subTest(actor=label, collection=collection, op="view"):
                response = self.req("GET", records_path(collection, seed_id), token)
                self.assertIn(response.status_code, BLOCKED_STATUSES)
            with self.subTest(actor=label, collection=collection, op="create"):
                before = self.server.count(collection)
                response = self.req(
                    "POST", records_path(collection), token, json=body_for(collection, f"{label}-new")
                )
                self.assertIn(response.status_code, BLOCKED_CREATE_STATUSES)
                self.assertEqual(self.server.count(collection), before)
            with self.subTest(actor=label, collection=collection, op="update"):
                field, value = UPDATE_FIELDS[collection]
                response = self.req("PATCH", records_path(collection, seed_id), token, json={field: value})
                self.assertIn(response.status_code, BLOCKED_STATUSES)
                self.assertEqual(self.server.get_record(collection, seed_id), seed)
            with self.subTest(actor=label, collection=collection, op="delete"):
                response = self.req("DELETE", records_path(collection, seed_id), token)
                self.assertIn(response.status_code, BLOCKED_STATUSES)
                self.assertIsNotNone(self.server.get_record(collection, seed_id))

    def test_anonymous_is_blocked(self) -> None:
        self.assert_blocked(None, "anonymous")

    def test_users_record_is_blocked(self) -> None:
        self.assert_blocked(self.user_token, "users")

    def test_bot_has_all_operations(self) -> None:
        for collection, seed in self.seeds.items():
            with self.subTest(collection=collection, op="list"):
                response = self.req("GET", records_path(collection), self.bot_token)
                self.assertEqual(response.status_code, 200)
                self.assertIn(seed["id"], [item["id"] for item in response.json()["items"]])
            with self.subTest(collection=collection, op="view"):
                response = self.req("GET", records_path(collection, seed["id"]), self.bot_token)
                self.assertEqual(response.status_code, 200)
            with self.subTest(collection=collection, op="create"):
                response = self.req(
                    "POST", records_path(collection), self.bot_token, json=body_for(collection, "bot-own")
                )
                self.assertEqual(response.status_code, 200, response.text)
                own_id = response.json()["id"]
                field, value = UPDATE_FIELDS[collection]
                with self.subTest(collection=collection, op="update"):
                    response = self.req(
                        "PATCH", records_path(collection, own_id), self.bot_token, json={field: value}
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                with self.subTest(collection=collection, op="delete"):
                    response = self.req("DELETE", records_path(collection, own_id), self.bot_token)
                    self.assertEqual(response.status_code, 204)
                    self.assertIsNone(self.server.get_record(collection, own_id))


class AccountCollectionsTest(PocketBaseRulesTestCase):
    def test_anonymous_cannot_register_bot_account(self) -> None:
        before = self.server.count("bot_accounts")
        password = secrets.token_urlsafe(24)
        response = self.req(
            "POST",
            records_path("bot_accounts"),
            json={"email": "intruder@test.local", "password": password, "passwordConfirm": password},
        )
        self.assertIn(response.status_code, BLOCKED_CREATE_STATUSES)
        self.assertEqual(self.server.count("bot_accounts"), before)

    def test_bot_sees_only_its_own_account(self) -> None:
        response = self.req("GET", records_path("bot_accounts"), self.bot_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["items"]], [self.bot_a_id])
        other_before = self.server.get_record("bot_accounts", self.bot_b_id)
        path = records_path("bot_accounts", self.bot_b_id)
        self.assertIn(self.req("GET", path, self.bot_token).status_code, BLOCKED_STATUSES)
        self.assertIn(
            self.req("PATCH", path, self.bot_token, json={"name": "hacked"}).status_code, BLOCKED_STATUSES
        )
        self.assertIn(self.req("DELETE", path, self.bot_token).status_code, BLOCKED_STATUSES)
        self.assertEqual(self.server.get_record("bot_accounts", self.bot_b_id), other_before)

    def test_users_record_cannot_read_bot_accounts(self) -> None:
        response = self.req("GET", records_path("bot_accounts"), self.user_token)
        self.assertIn(response.status_code, BLOCKED_LIST_STATUSES)
        if response.status_code == 200:
            self.assertEqual(response.json()["items"], [])
        response = self.req("GET", records_path("bot_accounts", self.bot_a_id), self.user_token)
        self.assertIn(response.status_code, BLOCKED_STATUSES)

    def test_users_collection_is_locked(self) -> None:
        before = self.server.count("users")
        password = secrets.token_urlsafe(24)
        response = self.req(
            "POST",
            records_path("users"),
            json={"email": "intruder@test.local", "password": password, "passwordConfirm": password},
        )
        self.assertIn(response.status_code, BLOCKED_CREATE_STATUSES)
        self.assertEqual(self.server.count("users"), before)
        response = self.req("GET", records_path("users"), self.user_token)
        self.assertEqual(response.status_code, 403)


class UniqueIndexTest(PocketBaseRulesTestCase):
    def post(self, collection: str, body: dict):
        return self.req("POST", records_path(collection), self.bot_token, json=body)

    def assert_not_unique(self, response, fields: tuple[str, ...]) -> None:
        self.assertEqual(response.status_code, 400, response.text)
        data = response.json()["data"]
        found = [field for field in fields if data.get(field, {}).get("code") == "validation_not_unique"]
        self.assertTrue(found, response.text)

    def test_single_field_indexes(self) -> None:
        for collection, field in (
            ("sent_jobs", "link"),
            ("brave_searches", "label"),
            ("linkedin_sessions", "cookie_hash"),
            ("inhire_tenants", "slug"),
            ("job_descriptions", "link"),
        ):
            with self.subTest(collection=collection):
                body = body_for(collection, "dup")
                self.assertEqual(self.post(collection, body).status_code, 200)
                self.assert_not_unique(self.post(collection, body), (field,))

    def test_queued_jobs_unique_by_link_and_source(self) -> None:
        body = body_for("queued_jobs", "dup")
        self.assertEqual(self.post("queued_jobs", body).status_code, 200)
        self.assertEqual(self.post("queued_jobs", {**body, "source": "web"}).status_code, 200)
        response = self.post("queued_jobs", body)
        self.assert_not_unique(response, ("link", "source"))
        print("queued_jobs duplicate error data:", response.json()["data"])

    def test_inhire_discoveries_accepts_zero(self) -> None:
        body = {**body_for("inhire_discoveries", "zero"), "found": 0}
        response = self.post("inhire_discoveries", body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["found"], 0)


class LongTextTest(PocketBaseRulesTestCase):
    def test_long_text_is_accepted(self) -> None:
        for collection, field in (
            ("sent_jobs", "title"),
            ("queued_jobs", "message"),
            ("alerts", "text"),
        ):
            with self.subTest(collection=collection, field=field):
                body = {**body_for(collection, "long"), field: LONG_TEXT}
                response = self.req("POST", records_path(collection), self.bot_token, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                stored = self.server.get_record(collection, response.json()["id"])
                self.assertEqual(len(stored[field]), len(LONG_TEXT))

    def test_long_description_is_accepted(self) -> None:
        body = {
            **body_for("job_descriptions", "long"),
            "description": LONG_DESCRIPTION,
            "status": "saved",
            "finished_at": DATE,
        }
        response = self.req("POST", records_path("job_descriptions"), self.bot_token, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.server.get_record("job_descriptions", response.json()["id"])
        self.assertEqual(len(stored["description"]), len(LONG_DESCRIPTION))


if __name__ == "__main__":
    unittest.main()
