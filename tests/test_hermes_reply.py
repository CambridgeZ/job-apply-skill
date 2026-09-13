import contextlib
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes_reply.py"
SPEC = importlib.util.spec_from_file_location("job_apply_hermes_reply", SCRIPT)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class HermesReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = self.home / "state.db"
        self.index = self.home / "sessions" / "sessions.json"
        self.index.parent.mkdir()
        self.config = {"backend": "hermes", "platform": "telegram", "chat_id": "12345", "user_id": "12345",
                       "hermes_home": str(self.home), "hermes_root": str(self.home / "source"), "thread_id": None}
        self.key = "agent:main:telegram:dm:12345"
        self.entry = {"session_key": self.key, "session_id": "fixture-session", "platform": "telegram", "chat_type": "dm",
                      "origin": {"platform": "telegram", "chat_type": "dm", "chat_id": "12345", "user_id": "12345", "thread_id": None}}
        self.save_index()
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL);
                CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
                INSERT INTO sessions VALUES ('fixture-session', 'telegram', '12345');
                INSERT INTO sessions VALUES ('other-session', 'telegram', '99999');
            """)
        self.cursor = reader.prepare_cursor(self.config)
        current = time.time()
        self.question = {"question_id": "fixture-q1", "kind": "information", "state": "pending", "notification": "sent",
                         "claimed_at": iso(current - 1), "sent_at": iso(current), "target_hash": self.cursor["target_hash"]}
        self.marker = "[JOB-APPLY:fixture-q1]"

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_index(self):
        self.index.write_text(json.dumps({self.key: self.entry}), encoding="utf-8")

    def add(self, content, session_id="fixture-session", role="user", timestamp=None, **extra):
        values = {"session_id": session_id, "role": role, "content": content,
                  "timestamp": time.time() if timestamp is None else timestamp, **extra}
        with self.connection() as connection:
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            return connection.execute("INSERT INTO messages (" + columns + ") VALUES (" + placeholders + ")", list(values.values())).lastrowid

    def read(self, **question_changes):
        return reader.read_reply(self.config, {**self.question, **question_changes}, self.cursor)

    def test_cursor_reads_only_metadata_even_when_content_access_is_denied(self):
        self.add("unrelated private fixture")
        original = reader._open_readonly

        def protected(route):
            connection, fingerprint = original(route)
            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and table == "messages" and column == "content":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            return connection, fingerprint

        with mock.patch.object(reader, "_open_readonly", side_effect=protected):
            cursor = reader.prepare_cursor(self.config)
        self.assertEqual(cursor["after_id"], 1)
        self.assertEqual(cursor["identity_scope"], "session")

    def test_exact_new_information_reply_is_returned_without_persistence(self):
        message_id = self.add(self.marker + " 我的通知期为一个月。")
        before = self.db.read_bytes()
        result = self.read()
        self.assertEqual(result["status"], "replies")
        self.assertEqual(result["identity_scope"], "session")
        self.assertEqual(result["replies"][0]["id"], message_id)
        self.assertEqual(result["replies"][0]["content"], self.marker + " 我的通知期为一个月。")
        self.assertEqual(set(result["replies"][0]), {"source", "id", "timestamp", "content"})
        self.assertEqual(self.db.read_bytes(), before)

    def test_other_session_role_old_time_and_future_time_do_not_match(self):
        content = self.marker + " excluded fixture"
        self.add(content, session_id="other-session")
        self.add(content, role="assistant")
        self.add(content, role="tool")
        self.add(content, role="USER")
        self.add(content, timestamp=time.time() - 3600)
        self.add(content, timestamp=time.time() + 3600)
        self.assertEqual(self.read(), {"status": "waiting", "identity_scope": "session"})

    def test_cursor_excludes_preexisting_message_even_if_timestamp_is_later(self):
        self.add(self.marker + " historical fixture")
        self.cursor = reader.prepare_cursor(self.config)
        with self.connection() as connection:
            connection.execute("UPDATE messages SET timestamp=?", (time.time(),))
        self.assertEqual(self.read()["status"], "waiting")

    def test_fast_reply_before_local_mark_sent_is_not_lost(self):
        current = time.time()
        self.cursor["started_at"] = current - 10
        self.question["claimed_at"] = iso(current - 20)
        self.question["sent_at"] = iso(current - 1)
        self.add(self.marker + " reply while send acknowledgement was in flight", timestamp=current - 5)
        self.assertEqual(self.read()["status"], "replies")

    def test_marker_must_be_body_prefix_followed_by_whitespace_and_answer(self):
        rejected = ("Reply: " + self.marker + " yes", "> " + self.marker + " yes",
                    '[Replying to: "' + self.marker + ' question"]\n\nyes',
                    "\n" + self.marker + " yes", "[job-apply:fixture-q1] yes", "[JOB-APPLY:fixture-q10] yes",
                    self.marker + "yes", self.marker, self.marker + " \t\r\n")
        for value in rejected:
            self.add(value)
        self.assertEqual(self.read()["status"], "waiting")
        self.add(self.marker + "\n合法的独立回复")
        self.assertEqual(len(self.read()["replies"]), 1)

    def test_review_and_verification_never_open_files_or_database(self):
        with mock.patch.object(reader, "_load_entry", side_effect=AssertionError("must not read files")), \
             mock.patch.object(reader, "_open_readonly", side_effect=AssertionError("must not open database")):
            for kind in ("verification", "review"):
                result = self.read(kind=kind)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["reason"], "unsupported_question_kind")

    def test_question_must_still_be_pending_and_notification_sent(self):
        for changes in ({"state": "resolved"}, {"state": "cancelled"}, {"notification": "unsent"}, {"notification": "uncertain"}):
            with mock.patch.object(reader, "_load_entry", side_effect=AssertionError("must not read files")):
                self.assertEqual(self.read(**changes)["reason"], "question_not_waiting")

    def test_bad_question_times_or_marker_fail_before_reading_messages(self):
        for changes in ({"sent_at": "invalid"}, {"sent_at": "2020-01-01T00:00:00"},
                        {"sent_at": iso(time.time() - 100)}, {"question_id": "bad] marker"}):
            with mock.patch.object(reader, "_open_readonly", side_effect=AssertionError("must not open database")):
                self.assertEqual(self.read(**changes)["status"], "blocked")

    def test_wrong_or_missing_database_sender_fails_closed(self):
        for user_id in ("99999", None):
            with self.connection() as connection:
                connection.execute("UPDATE sessions SET user_id=? WHERE id='fixture-session'", (user_id,))
            self.assertEqual(self.read()["reason"], "sender_unverified")
            with self.assertRaises(reader.ReplyReadError):
                reader.prepare_cursor(self.config)

    def test_wrong_index_sender_platform_or_thread_fails_closed(self):
        original = copy.deepcopy(self.entry)
        for key, value in (("user_id", "99999"), ("chat_id", "99999"), ("platform", "discord"),
                           ("chat_type", "group"), ("thread_id", "42"), ("is_bot", True)):
            self.entry = copy.deepcopy(original)
            self.entry["origin"][key] = value
            self.save_index()
            self.assertEqual(self.read()["reason"], "sender_unverified")

    def test_session_rotation_does_not_search_any_new_or_ancestor_messages(self):
        self.add(self.marker + " must not read")
        self.entry["session_id"] = "other-session"
        self.save_index()
        with mock.patch.object(reader, "_open_readonly", side_effect=AssertionError("must not open database")):
            self.assertEqual(self.read()["reason"], "session_changed")

    def test_index_rotation_during_query_discards_matches(self):
        self.add(self.marker + " must be discarded")
        original = reader._load_entry
        calls = 0
        def changing(route):
            nonlocal calls
            calls += 1
            value = original(route)
            if calls == 2:
                value["session_id"] = "other-session"
            return value
        with mock.patch.object(reader, "_load_entry", side_effect=changing):
            result = self.read()
        self.assertEqual(result["reason"], "session_changed")
        self.assertNotIn("replies", result)

    def test_route_cursor_and_receipt_must_agree(self):
        old_hash = self.cursor["target_hash"]
        self.cursor["target_hash"] = "different-route"
        self.assertEqual(self.read()["reason"], "route_changed")
        self.cursor["target_hash"] = old_hash
        self.assertEqual(self.read(target_hash="different-route")["reason"], "question_route_mismatch")

    def test_missing_index_prevents_history_scan(self):
        with self.connection() as connection:
            connection.execute("DROP INDEX idx_messages_session")
        self.assertEqual(self.read()["reason"], "unsupported_schema")

    def test_optional_per_message_identity_and_internal_flags_cannot_be_ignored(self):
        with self.connection() as connection:
            connection.execute("ALTER TABLE messages ADD COLUMN sender_id TEXT")
            connection.execute("ALTER TABLE messages ADD COLUMN internal INTEGER")
        self.add(self.marker + " wrong sender", sender_id="99999", internal=0)
        self.add(self.marker + " synthetic event", sender_id="12345", internal=1)
        self.add(self.marker + " missing provenance")
        self.assertEqual(self.read()["status"], "waiting")
        self.add(self.marker + " valid fixture", sender_id="12345", internal=0)
        self.assertEqual(len(self.read()["replies"]), 1)

    def test_large_or_many_matches_fail_closed_without_returning_bodies(self):
        self.add(self.marker + " " + "x" * reader.MAX_REPLY_CHARS)
        result = self.read()
        self.assertEqual(result["reason"], "reply_too_large")
        self.assertNotIn("replies", result)
        with self.connection() as connection:
            connection.execute("DELETE FROM messages")
        for _ in range(reader.MAX_REPLIES + 1):
            self.add(self.marker + " repeated fixture")
        result = self.read()
        self.assertEqual(result["reason"], "too_many_replies")
        self.assertNotIn("replies", result)


if __name__ == "__main__":
    unittest.main()
