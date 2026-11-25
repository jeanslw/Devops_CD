"""通知配置、Webhook 接收转发与告警发送的回归测试（外部发送均使用 mock）。"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException  # noqa: E402

from backend.config import settings

settings.db_driver = "sqlite"

from backend.database import Database  # noqa: E402
from backend.exceptions import DatabaseError, NotFoundError, ValidationError  # noqa: E402
from backend.models import BotRequest, WebhookForwardRequest, WebhookRequest  # noqa: E402
from backend.routers import alerts as alerts_router  # noqa: E402
from backend.routers import bots as bots_router  # noqa: E402
from backend.routers import webhooks as webhooks_router  # noqa: E402
from backend.routers.bots import list_bots  # noqa: E402
from backend.routers.webhooks import (  # noqa: E402
    _MAX_BODY_BYTES,
    _format_event_message,
    create_webhook,
    forward_event,
    list_webhooks,
    receive_webhook,
)
from backend.services import alert_service  # noqa: E402
from backend.services.notification import (  # noqa: E402
    ALERT_TEMPLATE_FIELDS,
    BOT_TEMPLATE_FIELDS,
    is_valid_template,
    notify_deploy,
    render_template,
)

from cd_contract import enable_cd  # noqa: E402


class _FakeRequest:
    def __init__(self, chunks=(), headers=None, on_stream=None):
        self.headers = headers or {}
        self._chunks = chunks
        self._on_stream = on_stream

    async def stream(self):
        if self._on_stream:
            self._on_stream()
        for chunk in self._chunks:
            yield chunk


class NotificationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "notification.db")
        enable_cd(self.db_path)
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("SELECT 1")
        Database._tables_ensured = True
        alert_service._cooldowns.clear()
        alert_service._durations.clear()
        alert_service._pending_langs.clear()
        alert_service._retry_not_before.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def _add_bot(self, template="", bot_type="custom"):
        with self.db.conn() as conn:
            cur = conn.execute(
                "INSERT INTO cd_bots (name, type, webhook_url, template) VALUES (?,?,?,?)",
                ("bot", bot_type, "https://hooks.example.test/notify", template),
            )
            return cur.lastrowid

    def _add_webhook(self, bot_id=0, enabled=1, name="ci"):
        with self.db.conn() as conn:
            cur = conn.execute(
                "INSERT INTO cd_webhooks (name, token, bot_id, enabled) VALUES (?,?,?,?)",
                (name, "enc:placeholder", bot_id, enabled),
            )
            return cur.lastrowid

    def _add_event(self, webhook_id, payload='{"project":"demo"}'):
        with self.db.conn() as conn:
            cur = conn.execute(
                "INSERT INTO cd_webhook_events (webhook_id, payload) VALUES (?,?)",
                (webhook_id, payload),
            )
            return cur.lastrowid

    def _event_count(self):
        with self.db.conn() as conn:
            return conn.execute("SELECT COUNT(*) AS cnt FROM cd_webhook_events").fetchone()["cnt"]

    def _forwarded(self):
        with self.db.conn() as conn:
            return conn.execute("SELECT forwarded FROM cd_webhook_events").fetchall()

    def _bot_count(self):
        with self.db.conn() as conn:
            return conn.execute("SELECT COUNT(*) AS cnt FROM cd_bots").fetchone()["cnt"]


class TestSensitiveConfigurationReads(NotificationTestCase):
    def test_bot_list_contains_only_selector_fields(self):
        with self.db.conn() as conn:
            conn.execute(
                "INSERT INTO cd_bots (name, type, webhook_url, template) VALUES (?,?,?,?)",
                ("deploy-bot", "custom", "https://secret.example/hook?token=secret", "{project}"),
            )

        rows = list_bots(self.db, "ordinary-user")

        self.assertEqual(rows, [{"id": 1, "name": "deploy-bot", "type": "custom"}])
        self.assertNotIn("webhook_url", rows[0])

    def test_webhook_token_is_returned_only_at_creation(self):
        created = create_webhook(WebhookRequest(name="ci", bot_id=0), self.db, {"username": "admin"})
        token = created["data"]["token"]
        self.assertTrue(token)

        rows = list_webhooks(self.db, "ordinary-user")

        self.assertEqual(len(rows), 1)
        self.assertNotIn("token", rows[0])
        self.assertNotIn(token, repr(rows))
        self.assertTrue(rows[0]["has_token"])


class TestTemplateHandling(NotificationTestCase):
    def test_valid_templates(self):
        self.assertTrue(is_valid_template("", BOT_TEMPLATE_FIELDS))
        self.assertTrue(is_valid_template("{project} {tag}", BOT_TEMPLATE_FIELDS))
        self.assertTrue(is_valid_template("{value}% {server}", ALERT_TEMPLATE_FIELDS))
        self.assertTrue(is_valid_template("{value:.1f}%", ALERT_TEMPLATE_FIELDS))
        self.assertTrue(is_valid_template("{value:>10}", ALERT_TEMPLATE_FIELDS))
        self.assertTrue(is_valid_template("{project[0]}", BOT_TEMPLATE_FIELDS))

    def test_invalid_templates(self):
        for tpl in ("{", "}", "{}", "{0}", "{unknown}", "{project"):
            self.assertFalse(is_valid_template(tpl, BOT_TEMPLATE_FIELDS))
        self.assertFalse(is_valid_template("{value:{width}}", ALERT_TEMPLATE_FIELDS))

    def test_render_falls_back_on_attribute_and_index_errors(self):
        self.assertEqual(render_template("{project.foo}", {"project": "x"}, "FALLBACK"), "FALLBACK")
        self.assertEqual(render_template("{project[foo]}", {"project": "x"}, "FALLBACK"), "FALLBACK")
        self.assertEqual(render_template("{project[0]}", {"project": "demo"}, "FALLBACK"), "d")

    def test_add_bot_rejects_invalid_template(self):
        req = BotRequest(name="bot", webhook_url="https://hooks.example.test/x", template="{")
        with self.assertRaises(ValidationError):
            bots_router.add_bot(req, self.db, {"username": "admin"})
        self.assertEqual(self._bot_count(), 0)

    def test_alert_rules_reject_invalid_template(self):
        req = alerts_router.AlertRuleRequest(name="cpu", resource_type="cpu", template="{")
        with self.assertRaises(ValidationError):
            alerts_router.create_alert(req, self.db, {"username": "admin"})
        with self.assertRaises(ValidationError):
            alerts_router.update_alert(1, req, self.db, {"username": "admin"})

    def test_add_bot_accepts_valid_template(self):
        req = BotRequest(name="bot", webhook_url="https://hooks.example.test/x", template="{project}")
        bots_router.add_bot(req, self.db, {"username": "admin"})
        self.assertEqual(self._bot_count(), 1)

    def test_webhook_message_falls_back_on_invalid_template(self):
        msg = _format_event_message({"project": "demo", "tag": "v1"}, {"template": "{"})
        self.assertIn("project: demo", msg)
        self.assertIn("tag: v1", msg)

    def test_webhook_message_uses_valid_template(self):
        msg = _format_event_message({"project": "demo"}, {"template": "{project}/{tag}"})
        self.assertEqual(msg, "demo/")

    def test_webhook_message_handles_non_object_payload(self):
        self.assertEqual(_format_event_message(["a"], {"template": ""}), "{}")

    def test_deploy_notification_falls_back_on_invalid_template(self):
        bot_id = self._add_bot(template="{")
        with patch("backend.services.notification.send_webhook", return_value=True) as send:
            notify_deploy(self.db, bot_id, "v1", "proj", "image:latest", "✅ Success", "ssh", ["srv"], "en")
        self.assertIn("Deploy Notification", send.call_args.args[1])

    def test_deploy_notification_uses_valid_template(self):
        bot_id = self._add_bot(template="{project}/{tag}")
        with patch("backend.services.notification.send_webhook", return_value=True) as send:
            notify_deploy(self.db, bot_id, "v1", "proj", "image:latest", "✅ Success", "ssh", ["srv"], "en")
        self.assertEqual(send.call_args.args[1], "proj/v1")


class TestWebhookReceive(NotificationTestCase):
    def test_content_length_rejected_before_stream(self):
        wid = self._add_webhook()
        with patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}):
            req = _FakeRequest(headers={"content-length": str(_MAX_BODY_BYTES + 1)}, chunks=(b"x",))
            with self.assertRaises(ValidationError):
                asyncio.run(receive_webhook("tok", req, self.db))
        self.assertEqual(self._event_count(), 0)

    def test_huge_content_length_digit_string_does_not_error(self):
        wid = self._add_webhook()
        with patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}):
            req = _FakeRequest(headers={"content-length": "9" * 5000}, chunks=(b'{"project":"demo"}',))
            asyncio.run(receive_webhook("tok", req, self.db))
        self.assertEqual(self._event_count(), 1)

    def test_stream_limit_rejected(self):
        wid = self._add_webhook()
        with patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}):
            req = _FakeRequest(chunks=(b"a" * _MAX_BODY_BYTES, b"b"))
            with self.assertRaises(ValidationError):
                asyncio.run(receive_webhook("tok", req, self.db))
        self.assertEqual(self._event_count(), 0)

    def test_disabled_during_body_read_is_rejected(self):
        wid = self._add_webhook()

        def disable():
            with self.db.conn() as conn:
                conn.execute("UPDATE cd_webhooks SET enabled=0 WHERE id=?", (wid,))

        with patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}):
            req = _FakeRequest(chunks=(b'{"project":"demo"}',), on_stream=disable)
            with self.assertRaises(NotFoundError):
                asyncio.run(receive_webhook("tok", req, self.db))
        self.assertEqual(self._event_count(), 0)

    def test_send_happens_after_event_committed(self):
        bot_id = self._add_bot(template="{project}")
        wid = self._add_webhook(bot_id=bot_id)
        observed = {}

        def fake_send(url, message, bot_type=""):
            observed["count"] = self._event_count()
            observed["message"] = message
            observed["bot_type"] = bot_type
            return True

        with (
            patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}),
            patch.object(webhooks_router, "send_webhook", side_effect=fake_send),
        ):
            req = _FakeRequest(chunks=(b'{"project":"demo"}',))
            result = asyncio.run(receive_webhook("tok", req, self.db))

        self.assertTrue(result["success"])
        self.assertEqual(observed["count"], 1)
        self.assertEqual(observed["message"], "demo")
        self.assertEqual(observed["bot_type"], "custom")
        self.assertEqual(self._forwarded(), [{"forwarded": 1}])

    def test_send_failure_keeps_event_unforwarded(self):
        bot_id = self._add_bot()
        wid = self._add_webhook(bot_id=bot_id)
        with (
            patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}),
            patch.object(webhooks_router, "send_webhook", return_value=False),
        ):
            req = _FakeRequest(chunks=(b'{"project":"demo"}',))
            asyncio.run(receive_webhook("tok", req, self.db))
        self.assertEqual(self._forwarded(), [{"forwarded": 0}])

    def test_invalid_template_does_not_break_receive(self):
        bot_id = self._add_bot(template="{")
        wid = self._add_webhook(bot_id=bot_id)
        with (
            patch.object(webhooks_router, "_find_enabled_webhook_by_token", return_value={"id": wid}),
            patch.object(webhooks_router, "send_webhook", return_value=True) as send,
        ):
            req = _FakeRequest(chunks=(b'{"project":"demo"}',))
            asyncio.run(receive_webhook("tok", req, self.db))
        self.assertIn("project: demo", send.call_args.args[1])
        self.assertEqual(self._forwarded(), [{"forwarded": 1}])


class TestWebhookForward(NotificationTestCase):
    def test_forward_passes_bot_type(self):
        bot_id = self._add_bot(template="{project}")
        eid = self._add_event(self._add_webhook())
        with patch.object(webhooks_router, "send_webhook", return_value=True) as send:
            result = forward_event(eid, WebhookForwardRequest(bot_id=bot_id), self.db, "user")
        self.assertTrue(result["success"])
        self.assertEqual(send.call_args.args[1], "demo")
        self.assertEqual(send.call_args.args[2], "custom")
        self.assertEqual(self._forwarded(), [{"forwarded": 1}])

    def test_forward_failure_keeps_event_unforwarded(self):
        bot_id = self._add_bot()
        eid = self._add_event(self._add_webhook())
        with patch.object(webhooks_router, "send_webhook", return_value=False):
            with self.assertRaises(DatabaseError):
                forward_event(eid, WebhookForwardRequest(bot_id=bot_id), self.db, "user")
        self.assertEqual(self._forwarded(), [{"forwarded": 0}])

    def test_forward_invalid_template_uses_default(self):
        bot_id = self._add_bot(template="{")
        eid = self._add_event(self._add_webhook())
        with patch.object(webhooks_router, "send_webhook", return_value=True) as send:
            forward_event(eid, WebhookForwardRequest(bot_id=bot_id), self.db, "user")
        self.assertIn("project: demo", send.call_args.args[1])


class TestAlertDelivery(NotificationTestCase):
    def _seed_alert(self, template=""):
        bot_id = self._add_bot()
        with self.db.conn() as conn:
            conn.execute(
                "INSERT INTO cd_servers (name, host, type) VALUES (?,?,?)",
                ("srv", "127.0.0.1", "ssh"),
            )
        self.rule = {
            "id": 1,
            "resource_type": "cpu",
            "target_type": "system",
            "bot_id": bot_id,
            "template": template,
            "server_ids": "",
            "cooldown_minutes": 10,
            "duration_minutes": 0,
            "threshold": 80,
        }
        self.result = {"resource": "cpu", "value": 91, "entity_label": ""}

    def _check(self, results=None):
        payload = [self.result] if results is None else results
        with patch.object(alert_service, "collect_alert_metrics", return_value=payload):
            alert_service._check_one(self.db, self.rule)

    def test_both_languages_succeeded_sets_cooldown(self):
        self._seed_alert()
        with patch.object(alert_service, "send_webhook", return_value=True) as send:
            self._check()
        self.assertEqual(send.call_count, 2)
        key = alert_service._track_key(1, 1, "")
        self.assertIn(key, alert_service._cooldowns)
        self.assertNotIn(key, alert_service._durations)
        self.assertNotIn(key, alert_service._pending_langs)

    def test_both_languages_failed_keeps_duration_and_backs_off(self):
        self._seed_alert()
        with patch.object(alert_service, "send_webhook", return_value=False) as send:
            self._check()
            self.assertEqual(send.call_count, 2)
            key = alert_service._track_key(1, 1, "")
            self.assertNotIn(key, alert_service._cooldowns)
            self.assertIn(key, alert_service._durations)
            self.assertEqual(alert_service._pending_langs[key], {"zh", "en"})
            self._check()
            self.assertEqual(send.call_count, 2)

    def test_only_failed_language_is_retried(self):
        self._seed_alert()
        with patch.object(alert_service, "send_webhook", side_effect=lambda url, msg, bot_type="": "资源告警" in msg):
            self._check()
        key = alert_service._track_key(1, 1, "")
        self.assertEqual(alert_service._pending_langs[key], {"en"})
        self.assertNotIn(key, alert_service._cooldowns)

        alert_service._retry_not_before.clear()
        with patch.object(alert_service, "send_webhook", return_value=True) as send:
            self._check()
        self.assertEqual(send.call_count, 1)
        self.assertIn("Resource Alert", send.call_args.args[1])
        self.assertIn(key, alert_service._cooldowns)
        self.assertNotIn(key, alert_service._pending_langs)

    def test_recovery_clears_retry_state(self):
        self._seed_alert()
        with patch.object(alert_service, "send_webhook", return_value=False):
            self._check()
        key = alert_service._track_key(1, 1, "")
        self.assertIn(key, alert_service._pending_langs)

        self._check(results=[])
        self.assertNotIn(key, alert_service._pending_langs)
        self.assertNotIn(key, alert_service._durations)
        self.assertNotIn(key, alert_service._retry_not_before)

        with patch.object(alert_service, "send_webhook", return_value=True) as send:
            self._check()
        self.assertEqual(send.call_count, 2)

    def test_invalid_alert_template_falls_back_to_default(self):
        self._seed_alert(template="{")
        with patch.object(alert_service, "send_webhook", return_value=True) as send:
            self._check()
        self.assertEqual(send.call_count, 2)
        self.assertIn("资源告警", send.call_args_list[0].args[1])
        self.assertIn("Resource Alert", send.call_args_list[1].args[1])

    def test_check_all_rules_isolates_failing_rule(self):
        with self.db.conn() as conn:
            conn.execute(
                "INSERT INTO cd_alert_rules (name, resource_type, bot_id, enabled) VALUES (?,?,?,1)",
                ("r1", "cpu", 0),
            )
            conn.execute(
                "INSERT INTO cd_alert_rules (name, resource_type, bot_id, enabled) VALUES (?,?,?,1)",
                ("r2", "cpu", 0),
            )
        with (
            patch.object(alert_service, "Database", return_value=self.db),
            patch.object(alert_service, "_check_one", side_effect=[RuntimeError("boom"), None]) as check,
        ):
            alert_service.check_all_rules()
        self.assertEqual(check.call_count, 2)


class TestEventRoutePermissions(NotificationTestCase):
    """事件查询与手动转发路由的权限依赖锁定."""

    def _route_checker(self, path: str, method: str):
        route = next(
            r
            for r in webhooks_router.router.routes
            if getattr(r, "path", "") == f"/api/webhooks{path}" and method in getattr(r, "methods", set())
        )
        checkers = [
            d.call for d in getattr(route, "dependant").dependencies if getattr(d.call, "__name__", "") == "checker"
        ]
        self.assertEqual(len(checkers), 1)
        return checkers[0]

    def test_list_events_requires_notification_manage(self):
        checker = self._route_checker("/{wid}/events", "GET")
        with self.assertRaises(HTTPException) as ctx:
            checker(user={"role": "dev", "permissions": ["cd.webhook"]})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsNotNone(checker(user={"role": "dev", "permissions": ["cd.notification-manage"]}))

    def test_forward_event_requires_notification_manage(self):
        checker = self._route_checker("/events/{eid}/forward", "POST")
        with self.assertRaises(HTTPException) as ctx:
            checker(user={"role": "dev", "permissions": ["cd.webhook"]})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsNotNone(checker(user={"role": "dev", "permissions": ["*"]}))


if __name__ == "__main__":
    unittest.main()
