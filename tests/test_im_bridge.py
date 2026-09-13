"""Exercise the conditional IM bridge without real adapters or private data."""
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import im_bridge

followups = im_bridge.followup_store
BASE = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
TARGET = "4242424242"
OTHER_TARGET = "4343434343"


class ImBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "private" / "notifications.json"
        self.followups_path = self.root / "private" / "followups.json"
        self.cursors_path = self.root / "private" / "reply-cursors.json"
        self.route = {
            "backend": "hermes", "platform": "telegram", "chat_id": TARGET,
            "user_id": TARGET, "hermes_home": str(self.root / "fixture-hermes-home"),
            "hermes_root": str(self.root / "fixture-hermes-root"),
            "binding_reference": "synthetic-private-dm-verification",
        }
        im_bridge.configure(self.config_path, self.route)
        self.send_response = {
            "status": "sent", "platform": "telegram", "chat_id": TARGET, "message_id": "123",
        }
        self.real_run_adapter = im_bridge.run_adapter
        self.adapter = self.patch("run_adapter", side_effect=self.dispatch)
        self.prepare = self.patch("prepare_cursor", return_value=self.fixture_cursor())
        self.reader = self.patch("read_reply", return_value={"status": "waiting"})

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(im_bridge, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def dispatch(self, config, mode, message=None):
        if mode == "doctor":
            return {"adapter_available": True, "platform_configured": True}
        self.assertEqual(mode, "send")
        return copy.deepcopy(self.send_response)

    def at(self, seconds):
        return mock.patch.object(followups, "utc_now", return_value=BASE + dt.timedelta(seconds=seconds))

    def fixture_cursor(self):
        config = {**self.route}
        return {
            "session_id": "fixture-session", "session_key": "agent:main:telegram:dm:" + TARGET,
            "after_id": 7, "started_at": BASE.timestamp() + 180,
            "target_hash": im_bridge.target_hash(config), "db_fingerprint": [1, 2],
            "identity_scope": "session",
        }

    def question(self, **extra):
        return {
            "question_id": "fixture-question", "company": "Example Company", "job_id": "J123",
            "thread_id": "fixture-codex-task", "kind": "information", "fields": [
                {"key": "notice_period", "label": "Notice period", "prompt": "What is your current notice period?"},
            ], **extra,
        }

    def ask(self, **extra):
        payload = self.question(**extra)
        with self.at(0):
            result, code = followups.execute(self.followups_path, "ask", payload=payload)
        self.assertEqual(code, 0, result)
        return payload["question_id"]

    def send(self, seconds=180, question_id="fixture-question"):
        with self.at(seconds):
            return im_bridge.send_question(self.config_path, self.followups_path, question_id, self.cursors_path)

    def resolve(self, seconds=179, question_id="fixture-question"):
        with self.at(seconds):
            result, code = followups.execute(self.followups_path, "resolve", payload={
                "question_id": question_id, "source": "codex", "reference": "fixture-answer-turn",
            })
        self.assertEqual(code, 0, result)

    def stored(self, question_id="fixture-question"):
        data = json.loads(self.followups_path.read_text())
        return next(q for q in data["questions"] if q["question_id"] == question_id)

    def send_calls(self):
        return [call for call in self.adapter.call_args_list if call.args[1] == "send"]

    def test_179_seconds_does_nothing_and_180_seconds_sends(self):
        self.ask()
        result, code = self.send(179)
        self.assertEqual(code, 3, result)
        self.assertEqual(result["status"], "not_due")
        self.adapter.assert_not_called()
        self.prepare.assert_not_called()
        self.assertFalse(self.cursors_path.exists())
        result, code = self.send(180)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(self.send_calls()), 1)
        self.assertEqual(self.stored()["notification"], "sent")

    def test_answered_or_cancelled_question_never_sends(self):
        for state in ("answered", "cancelled"):
            with self.subTest(state=state):
                qid = self.ask(question_id=state, job_id=state)
                if state == "answered":
                    self.resolve(question_id=qid)
                else:
                    with self.at(179):
                        followups.execute(self.followups_path, "cancel", question_id=qid)
                result, code = self.send(600, qid)
                self.assertEqual(code, 3, result)
                self.assertEqual(self.stored(qid)["notification"], "unsent")
        self.adapter.assert_not_called()
        self.prepare.assert_not_called()

    def test_success_is_at_most_once_and_public_result_hides_target(self):
        self.ask()
        result, code = self.send()
        self.assertEqual(code, 0, result)
        self.assertNotIn(TARGET, json.dumps(result))
        self.assertNotIn("chat_id", result)
        self.assertNotIn("user_id", result)
        self.assertIn("[JOB-APPLY:fixture-question]", self.send_calls()[0].kwargs["message"])
        first = self.followups_path.read_bytes()
        result, code = self.send(600)
        self.assertEqual(code, 3, result)
        self.assertEqual(len(self.send_calls()), 1)
        self.assertEqual(self.followups_path.read_bytes(), first)
        stored = self.stored()
        self.assertEqual(stored["message_id"], "123")
        self.assertEqual(stored["target_hash"], im_bridge.target_hash(self.route))
        self.assertNotIn(TARGET, json.dumps(stored))

    def test_uncertain_delivery_is_not_retried_and_error_body_is_not_echoed(self):
        self.ask()
        self.send_response = {"status": "send_uncertain", "error": "SYNTHETIC_SECRET_DO_NOT_ECHO", "chat_id": TARGET}
        result, code = self.send()
        self.assertEqual(code, 2, result)
        self.assertEqual(self.stored()["notification"], "uncertain")
        self.assertNotIn("SYNTHETIC_SECRET_DO_NOT_ECHO", json.dumps(result))
        self.assertNotIn(TARGET, json.dumps(result))
        result, code = self.send(9999)
        self.assertEqual(code, 3, result)
        self.assertEqual(len(self.send_calls()), 1)

    def test_incorrect_or_skipped_receipts_never_mark_sent(self):
        mutations = [
            {"chat_id": OTHER_TARGET}, {"platform": "discord"}, {"message_id": ""},
            {"message_id": "not-a-telegram-id"}, {"skipped": True},
            {"error": "SYNTHETIC_ERROR_BODY"}, {"status": "queued"},
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                qid = self.ask(question_id="receipt-" + str(index), job_id="receipt-job-" + str(index))
                self.send_response = {"status": "sent", "platform": "telegram", "chat_id": TARGET,
                                      "message_id": "123", **mutation}
                result, code = self.send(question_id=qid)
                self.assertEqual(code, 2, result)
                self.assertEqual(self.stored(qid)["notification"], "uncertain")
                self.assertNotIn(TARGET, json.dumps(result))
                self.assertNotIn(OTHER_TARGET, json.dumps(result))
                self.assertNotIn("SYNTHETIC_ERROR_BODY", json.dumps(result))

    def test_media_directives_and_oversized_questions_are_not_dispatched(self):
        prompts = [
            "Please inspect MEDIA:/tmp/fictional-private.pdf",
            "Example: `MEDIA:/tmp/fictional-private.pdf`",
            "Example: MEDIA:\n/tmp/fictional-private.pdf",
            "[[audio_as_voice]] This must remain a question.",
            "x" * 2001,
        ]
        for index, prompt in enumerate(prompts):
            with self.subTest(index=index):
                qid = self.ask(question_id="unsafe-" + str(index), job_id="unsafe-job-" + str(index),
                               fields=[{"key": "field", "label": "Question", "prompt": prompt}])
                with self.assertRaises(im_bridge.StoreError):
                    self.send(question_id=qid)
                self.assertEqual(self.stored(qid)["notification"], "unsent")
        self.adapter.assert_not_called()
        self.prepare.assert_not_called()

    def test_answer_arriving_after_claim_prevents_dispatch(self):
        self.ask()
        original = im_bridge.save_cursor

        def save_then_resolve(*args, **kwargs):
            original(*args, **kwargs)
            self.resolve(seconds=180)

        with mock.patch.object(im_bridge, "save_cursor", side_effect=save_then_resolve):
            result, code = self.send()
        self.assertEqual(code, 3, result)
        self.assertEqual(self.send_calls(), [])
        self.assertEqual(self.stored()["state"], "answered")

    def test_answer_arriving_during_dispatch_closes_question_without_resend(self):
        self.ask()

        def dispatch(config, mode, message=None):
            if mode == "send":
                self.resolve(seconds=180)
            return self.dispatch(config, mode, message)

        self.adapter.side_effect = dispatch
        result, code = self.send()
        self.assertEqual(code, 0, result)
        self.assertEqual(self.stored()["state"], "answered")
        self.assertEqual(self.stored()["notification"], "sent")
        result, code = self.send(600)
        self.assertEqual(code, 3, result)
        self.assertEqual(len(self.send_calls()), 1)

    def test_changing_target_after_claim_prevents_dispatch(self):
        self.ask()
        original = im_bridge.save_cursor

        def save_then_reconfigure(*args, **kwargs):
            original(*args, **kwargs)
            im_bridge.configure(self.config_path, {**self.route, "chat_id": OTHER_TARGET, "user_id": OTHER_TARGET})

        with mock.patch.object(im_bridge, "save_cursor", side_effect=save_then_reconfigure):
            result, code = self.send()
        self.assertEqual(code, 2, result)
        self.assertEqual(self.send_calls(), [])
        self.assertEqual(self.stored()["notification"], "uncertain")

    def test_configuration_is_private_and_does_not_accept_credentials_or_ambiguous_targets(self):
        self.assertEqual(stat.S_IMODE(self.config_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.config_path) + ".lock").stat().st_mode), 0o600)
        original = self.config_path.read_bytes()
        invalid = [
            {"token": "SYNTHETIC_SECRET"}, {"chat_id": "@fixture_username"},
            {"chat_id": "-100123456"}, {"user_id": OTHER_TARGET}, {"binding_reference": ""},
            {"backend": "unconfigured_backend"}, {"platform": "group_webhook"},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(im_bridge.StoreError):
                im_bridge.configure(self.config_path, {**self.route, **change})
            self.assertEqual(self.config_path.read_bytes(), original)
        result = im_bridge.doctor(self.config_path)
        self.assertNotIn(TARGET, json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.send_calls(), [])

    def test_corrupt_configuration_is_not_replaced_or_printed(self):
        corrupt = b'{broken SYNTHETIC_SECRET_DO_NOT_ECHO'
        self.config_path.write_bytes(corrupt)
        with self.assertRaises(im_bridge.StoreError):
            im_bridge.configure(self.config_path, self.route)
        self.assertEqual(self.config_path.read_bytes(), corrupt)
        result = im_bridge.doctor(self.config_path)
        self.assertFalse(result["route_configured"])
        self.assertNotIn("SYNTHETIC_SECRET_DO_NOT_ECHO", json.dumps(result))
        self.adapter.assert_not_called()

    def test_review_and_verification_do_not_read_external_answers(self):
        for kind in ("review", "verification"):
            with self.subTest(kind=kind):
                qid = self.ask(question_id=kind, job_id=kind, kind=kind)
                result, code = self.send(question_id=qid)
                self.assertEqual(code, 0, result)
                with mock.patch.object(im_bridge, "load_config", side_effect=AssertionError("Must not load a route for this peek")):
                    result, code = im_bridge.peek_reply(self.config_path, self.followups_path, qid, self.cursors_path)
                self.assertEqual(code, 3, result)
                self.assertEqual(result["reason"], "return_to_codex")
        self.prepare.assert_not_called()
        self.reader.assert_not_called()

    def test_peek_requires_matching_route_and_receipt(self):
        self.ask()
        result, code = self.send()
        self.assertEqual(code, 0, result)
        im_bridge.configure(self.config_path, {**self.route, "chat_id": OTHER_TARGET, "user_id": OTHER_TARGET})
        result, code = im_bridge.peek_reply(self.config_path, self.followups_path, "fixture-question", self.cursors_path)
        self.assertEqual(code, 3, result)
        self.assertEqual(result["reason"], "route_or_cursor_mismatch")
        self.reader.assert_not_called()

    def test_peek_returns_answers_without_persisting_resolving_or_confirming(self):
        self.ask()
        self.assertEqual(self.send()[1], 0)
        before_questions = self.followups_path.read_bytes()
        before_cursors = self.cursors_path.read_bytes()
        profile_path = self.config_path.with_name("profile.json")
        original_profile = b'{"applications":[{"status":"awaiting_confirmation"}]}'
        profile_path.write_bytes(original_profile)
        reply = {"status": "replies", "identity_scope": "session", "replies": [
            {"source": "telegram", "id": 8, "timestamp": BASE.timestamp() + 181,
             "content": "[JOB-APPLY:fixture-question] SYNTHETIC_PRIVATE_ANSWER two months"},
        ]}
        self.reader.return_value = reply
        result, code = im_bridge.peek_reply(self.config_path, self.followups_path, "fixture-question", self.cursors_path)
        self.assertEqual(code, 0, result)
        self.assertEqual(result, reply)
        self.assertEqual(self.followups_path.read_bytes(), before_questions)
        self.assertEqual(self.cursors_path.read_bytes(), before_cursors)
        self.assertEqual(profile_path.read_bytes(), original_profile)
        self.assertEqual(self.stored()["state"], "pending")
        self.assertNotIn("SYNTHETIC_PRIVATE_ANSWER", self.cursors_path.read_text())
        self.resolve(seconds=182)
        self.reader.reset_mock()
        im_bridge.peek_reply(self.config_path, self.followups_path, "fixture-question", self.cursors_path)
        self.reader.assert_not_called()

    def create_worker_stubs(self):
        """Create isolated imports so even the real worker cannot reach Hermes."""
        root = Path(self.route["hermes_root"])
        home = Path(self.route["hermes_home"])
        (root / "venv" / "bin").mkdir(parents=True)
        (root / "venv" / "bin" / "python").symlink_to(sys.executable)
        home.mkdir()
        (home / ".env").write_text("FIXTURE_ONLY=true\n")
        for directory in (root / "gateway", root / "gateway" / "platforms", root / "tools"):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "__init__.py").write_text("")
        (root / "dotenv.py").write_text(textwrap.dedent('''
            from pathlib import Path
            import os
            def load_dotenv(dotenv_path, override):
                assert Path(dotenv_path) == Path(os.environ["HERMES_HOME"]) / ".env"
                assert override is True
                assert Path(dotenv_path).read_text() == "FIXTURE_ONLY=true\\n"
        '''))
        (root / "gateway" / "config.py").write_text(textwrap.dedent('''
            from types import SimpleNamespace
            class Platform:
                TELEGRAM = "telegram"
            def load_gateway_config():
                return SimpleNamespace(platforms={"telegram": SimpleNamespace(
                    enabled=True, token="SYNTHETIC_NOT_A_CREDENTIAL", extra={})})
        '''))
        (root / "gateway" / "platforms" / "telegram.py").write_text("class TelegramAdapter: pass\n")
        (root / "tools" / "send_message_tool.py").write_text(textwrap.dedent('''
            import json
            from pathlib import Path
            AUDIT = Path(__file__).resolve().parents[1] / "worker-audit.json"
            async def _send_telegram_message_with_retry(bot, *, attempts=3, **kwargs):
                AUDIT.write_text(json.dumps({"attempts": attempts, "kwargs": kwargs}))
                assert attempts == 1, "A conditional notification must not retry a failed send"
                return {"success": True, "platform": "telegram", "chat_id": kwargs["chat_id"], "message_id": "321"}
            async def _send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None):
                assert platform == "telegram"
                assert media_files == []
                assert thread_id is None
                assert pconfig.extra["disable_link_previews"] is True
                return await _send_telegram_message_with_retry(None, chat_id=chat_id, text=message)
        '''))
        return root, home

    def test_real_worker_uses_explicit_venv_no_attachments_and_one_attempt(self):
        root, home = self.create_worker_stubs()
        original_env = (home / ".env").read_bytes()
        config = im_bridge.load_config(self.config_path)
        health = self.real_run_adapter(config, "doctor")
        self.assertTrue(health["adapter_available"], health)
        self.assertTrue(health["platform_configured"], health)
        self.assertFalse((root / "worker-audit.json").exists())
        message = "[JOB-APPLY:fixture-question]\nExample Company J123: current notice period?"
        result = self.real_run_adapter(config, "send", message)
        self.assertEqual(result["status"], "sent", result)
        self.assertEqual(result["message_id"], "321")
        audit = json.loads((root / "worker-audit.json").read_text())
        self.assertEqual(audit["attempts"], 1)
        self.assertEqual(audit["kwargs"], {"chat_id": TARGET, "text": message})
        self.assertEqual((home / ".env").read_bytes(), original_env)

    def test_real_worker_rejects_media_and_oversized_body_before_dispatch(self):
        root, _ = self.create_worker_stubs()
        config = im_bridge.load_config(self.config_path)
        for message in ("MEDIA:/tmp/fictional-private.pdf", "[[audio_as_voice]] example", "x" * 2001):
            with self.subTest(message=message[:30]):
                result = self.real_run_adapter(config, "send", message)
                self.assertEqual(result["status"], "send_uncertain", result)
                self.assertFalse((root / "worker-audit.json").exists())

    def test_adapter_timeout_suppresses_child_output_and_credentials(self):
        self.create_worker_stubs()
        config = im_bridge.load_config(self.config_path)
        stdout, stderr = io.StringIO(), io.StringIO()
        timeout = subprocess.TimeoutExpired("SYNTHETIC_SECRET_COMMAND", 45,
                                             output="SYNTHETIC_SECRET_STDOUT", stderr="SYNTHETIC_SECRET_STDERR")
        with mock.patch.object(im_bridge.subprocess, "run", side_effect=timeout), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.real_run_adapter(config, "send", "fixture notification")
        self.assertEqual(result, {"status": "send_uncertain"})
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(TARGET, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
