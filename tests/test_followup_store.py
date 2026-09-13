import concurrent.futures
import datetime as dt
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("followup_store", SCRIPTS / "followup_store.py")
followups = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(followups)
BASE = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


class FollowupStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "followups.json"

    def tearDown(self):
        self.temp.cleanup()

    def question(self, **extra):
        return {"question_id": "fixture-question", "company": "Example Company", "job_id": "example-job",
                "thread_id": "fixture-thread", "kind": "information", "fields": [
                    {"key": "start_date", "label": "Start date", "prompt": "What start date should be used?"},
                    {"key": "location", "label": "Location", "prompt": "Which office location should be used?"}], **extra}

    def call(self, command, seconds=0, code=0, **kwargs):
        with mock.patch.object(followups, "utc_now", return_value=BASE + dt.timedelta(seconds=seconds)):
            result, actual = followups.execute(self.path, command, **kwargs)
        self.assertEqual(actual, code, result)
        return result

    def ask(self, **extra):
        return self.call("ask", payload=self.question(**extra))

    def stored(self):
        return json.loads(self.path.read_text())

    def receipt(self, **extra):
        return {"question_id": "fixture-question", "message_id": "fixture-message-id", "backend": "fixture-backend",
                "platform": "fixture-platform", "target_hash": "a" * 64, **extra}

    def test_fixed_deadline_is_not_due_at_179_and_due_at_180(self):
        asked = self.ask()
        self.assertEqual(followups.timestamp(asked["deadline"]) - followups.timestamp(asked["asked_at"]),
                         dt.timedelta(seconds=180))
        self.assertEqual(self.call("due", seconds=179)["questions"], [])
        due = self.call("due", seconds=180)["questions"]
        self.assertEqual([q["question_id"] for q in due], ["fixture-question"])
        self.assertEqual(self.call("claim", seconds=179, question_id="fixture-question", code=3)["status"], "not_due")
        self.assertEqual(self.call("claim", seconds=180, question_id="fixture-question")["notification"], "sending")

    def test_cli_uses_real_clock_and_cannot_shorten_deadline(self):
        command = [sys.executable, "-B", str(SCRIPTS / "followup_store.py"), "--file", str(self.path)]
        before = dt.datetime.now(dt.timezone.utc)
        asked = subprocess.run(command + ["ask"], input=json.dumps(self.question()), text=True, capture_output=True)
        after = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        result = json.loads(asked.stdout)
        self.assertLessEqual(before, followups.timestamp(result["asked_at"]))
        self.assertLessEqual(followups.timestamp(result["asked_at"]), after)
        claimed = subprocess.run(command + ["claim", "fixture-question"], text=True, capture_output=True)
        self.assertEqual(claimed.returncode, 3)
        self.assertEqual(json.loads(claimed.stdout)["status"], "not_due")
        override = subprocess.run(command + ["ask", "--delay", "0"], input=json.dumps(self.question()), text=True, capture_output=True)
        self.assertEqual(override.returncode, 2)
        with self.assertRaises(followups.StoreError):
            self.call("ask", payload=self.question(question_id="another-question", deadline="2000-01-01T00:00:00Z"))

    def test_answered_and_cancelled_questions_stop_reminders(self):
        self.ask()
        self.call("resolve", seconds=179, payload={"question_id": "fixture-question", "source": "codex", "reference": "fixture-turn-id"})
        self.assertEqual(self.call("due", seconds=180)["questions"], [])
        self.assertEqual(self.call("claim", seconds=180, question_id="fixture-question", code=3)["status"], "not_claimable")
        self.ask(question_id="cancelled-question")
        self.call("cancel", question_id="cancelled-question")
        self.assertEqual(self.call("due", seconds=300)["questions"], [])
        self.assertEqual(self.call("get", question_id="cancelled-question")["question"]["state"], "cancelled")

    def test_resolve_discards_answer_body_and_never_changes_applications(self):
        self.ask(kind="review")
        profile = self.path.with_name("profile.json")
        original = b'{"applications":[{"status":"awaiting_confirmation"}],"fixture":true}'
        profile.write_bytes(original)
        result = self.call("resolve", payload={"question_id": "fixture-question", "source": "im", "reference": "fixture-im-message",
                           "answer": {"otp": "123456", "password": "discard-this-secret", "text": "discard-this-answer"}})
        raw = self.path.read_text()
        for forbidden in ("123456", "discard-this-secret", "discard-this-answer", '"answer"', '"otp"', '"password"'):
            self.assertNotIn(forbidden, raw)
            self.assertNotIn(forbidden, json.dumps(result))
        self.assertEqual(self.stored()["questions"][0]["resolution"], {"source": "im", "reference": "fixture-im-message"})
        self.assertEqual(profile.read_bytes(), original)

    def test_parallel_claim_allows_only_one_sender(self):
        self.ask()
        with mock.patch.object(followups, "utc_now", return_value=BASE + dt.timedelta(seconds=180)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                outcomes = list(executor.map(lambda _: followups.execute(self.path, "claim", question_id="fixture-question"), range(20)))
        self.assertEqual(sum(code == 0 for _, code in outcomes), 1)
        self.assertTrue(all(code in (0, 3) for _, code in outcomes))
        self.assertEqual(self.stored()["questions"][0]["notification"], "sending")

    def test_uncertain_send_is_never_automatically_retried(self):
        self.ask()
        self.call("claim", seconds=180, question_id="fixture-question")
        self.call("mark-uncertain", seconds=181, question_id="fixture-question")
        self.assertEqual(self.call("due", seconds=99999)["questions"], [])
        self.call("claim", seconds=99999, question_id="fixture-question", code=3)
        with self.assertRaises(followups.StoreError):
            self.call("mark-sent", seconds=99999, payload=self.receipt())
        self.assertEqual(self.stored()["questions"][0]["notification"], "uncertain")

    def test_sent_receipt_requires_claim_and_keeps_target_hashed(self):
        self.ask()
        with self.assertRaises(followups.StoreError):
            self.call("mark-sent", payload=self.receipt())
        self.call("claim", seconds=180, question_id="fixture-question")
        with self.assertRaises(followups.StoreError):
            self.call("mark-sent", seconds=181, payload=self.receipt(target_hash="recipient@example.com"))
        result = self.call("mark-sent", seconds=181, payload=self.receipt())
        self.assertEqual(result["notification"], "sent")
        self.assertEqual(self.call("due", seconds=1000)["questions"], [])
        self.assertEqual(self.stored()["questions"][0]["message_id"], "fixture-message-id")

    def test_partial_answers_are_not_implicitly_recorded_or_resolved(self):
        self.ask()
        before = self.path.read_bytes()
        # A partial reply must stay outside this store. No resolve is called yet.
        for invalid in (self.question(answer={"start_date": "partial fixture answer"}),
                        {"question_id": "fixture-question", "source": "codex", "reference": "fixture-turn", "partial": True}):
            with self.assertRaises(followups.StoreError):
                self.call("ask" if "fields" in invalid else "resolve", payload=invalid)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.call("get", question_id="fixture-question")["question"]["state"], "pending")
        self.assertEqual(len(self.call("due", seconds=180)["questions"]), 1)

    def test_unknown_fields_rejected_and_verification_prompts_are_neutral(self):
        for extra in ({"code": "123456"}, {"answer": "fixture answer"}, {"password": "fixture secret"}):
            with self.assertRaises(followups.StoreError):
                self.ask(**extra)
        with self.assertRaises(followups.StoreError):
            self.ask(fields=[{"key": "verification", "label": "Verification", "prompt": "Question", "value": "123456"}])
        self.ask(kind="verification", fields=[{"key": "otp", "label": "Verification 123456", "prompt": "Accidentally pasted code 123456"}])
        self.assertNotIn("123456", self.path.read_text())
        self.assertEqual(self.stored()["questions"][0]["fields"], followups.VERIFICATION_FIELDS)

    def test_private_files_atomic_writes_and_corrupt_store_protection(self):
        self.ask()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.path) + ".lock").stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".followups-*.tmp")), [])
        for raw in (b'{broken', b'{"schema_version":1,"questions":{},"answer":"not-allowed"}'):
            self.path.write_bytes(raw)
            with self.assertRaises(followups.StoreError):
                self.ask(question_id="new-question")
            self.assertEqual(self.path.read_bytes(), raw)

    def test_duplicate_ask_does_not_reset_deadline_and_filters_work(self):
        first = self.ask()
        before = self.path.read_bytes()
        self.call("ask", seconds=100, payload=self.question(), code=3)
        self.assertEqual(self.path.read_bytes(), before)
        generated = self.question(company="Different Company", job_id="another-job", thread_id="another-thread")
        del generated["question_id"]
        second = self.call("ask", payload=generated)
        self.assertNotEqual(first["question_id"], second["question_id"])
        result = self.call("list", company="Example Company", job_id="example-job", thread_id="fixture-thread", state="pending")
        self.assertEqual([q["question_id"] for q in result["questions"]], ["fixture-question"])

    def test_same_field_batch_cannot_be_reasked_with_a_new_id(self):
        self.ask()
        before = self.path.read_bytes()
        same_batch = self.question(question_id="replacement-id")
        same_batch["fields"].reverse()
        same_batch["fields"][0]["prompt"] = "Rephrased question"
        result = self.call("ask", seconds=100, payload=same_batch, code=3)
        self.assertEqual(result["question_id"], "fixture-question")
        self.assertEqual(self.path.read_bytes(), before)
        self.call("resolve", payload={"question_id": "fixture-question", "source": "codex", "reference": "fixture-turn"})
        self.ask(question_id="new-task-question", thread_id="new-task")

    def test_cancel_after_notification_cannot_reset_reminder_by_reasking(self):
        self.ask()
        self.call("claim", seconds=180, question_id="fixture-question")
        self.call("mark-sent", seconds=181, payload=self.receipt())
        self.call("cancel", seconds=182, question_id="fixture-question")
        before = self.path.read_bytes()
        result = self.call("ask", seconds=183, payload=self.question(question_id="reset-attempt"), code=3)
        self.assertEqual(result["question_id"], "fixture-question")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.call("due", seconds=99999)["questions"], [])

    def test_shortened_stored_deadline_is_not_trusted(self):
        self.ask()
        document = self.stored()
        document["questions"][0]["deadline"] = followups.iso(BASE)
        self.path.write_text(json.dumps(document))
        before = self.path.read_bytes()
        with self.assertRaises(followups.StoreError):
            self.call("claim", seconds=0, question_id="fixture-question")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
