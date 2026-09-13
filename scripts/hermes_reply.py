#!/usr/bin/env python3
"""Read narrowly scoped information replies from a local Hermes Telegram DM.

This module never sends, resolves a question, confirms an application, or writes
Hermes data. Ordinary replies are untrusted answers, not approval evidence.
Hermes' native schema identifies the user at session level; it does not retain
per-message Telegram sender, reply-to, or internal-event provenance. The caller
must have verified the numeric private-chat route before configuring this reader.
"""
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import stat
import time


MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_REPLY_CHARS = 32768
MAX_REPLIES = 20
ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}\Z")
QUESTION_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class ReplyReadError(ValueError):
    """An expected failure with a safe message and a machine-readable reason."""

    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or reason)


def require(condition, reason, message):
    if not condition:
        raise ReplyReadError(reason, message)


def _route(config):
    require(isinstance(config, dict), "invalid_config", "Reply configuration must be an object.")
    require(config.get("backend") == "hermes" and config.get("platform") == "telegram",
            "unsupported_route", "Only a configured Hermes Telegram private chat is supported.")
    chat_id, user_id = config.get("chat_id"), config.get("user_id")
    require(isinstance(chat_id, str) and isinstance(user_id, str)
            and ID_PATTERN.fullmatch(chat_id) and ID_PATTERN.fullmatch(user_id) and chat_id == user_id,
            "invalid_private_route", "The verified private chat and user must have the same positive numeric ID.")
    require(config.get("thread_id") in (None, ""), "unsupported_thread", "Threaded private chats are unsupported.")
    home = config.get("hermes_home")
    require(isinstance(home, str) and Path(home).is_absolute(),
            "invalid_home", "Hermes home must be an absolute local path.")
    route_hash = hashlib.sha256(("telegram:" + chat_id + ":" + user_id).encode("utf-8")).hexdigest()
    return {
        "chat_id": chat_id, "user_id": user_id, "target_hash": route_hash,
        "session_key": "agent:main:telegram:dm:" + chat_id,
        "index_path": Path(home) / "sessions" / "sessions.json",
        "db_path": Path(home) / "state.db",
    }


def _regular_file(path, reason):
    try:
        info = path.lstat()
    except OSError:
        raise ReplyReadError(reason, "Required local Hermes metadata is unavailable.") from None
    require(stat.S_ISREG(info.st_mode), reason, "Hermes data paths must be regular files, not symbolic links.")
    return info


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "invalid_index", "The session index contains duplicate fields.")
        result[key] = value
    return result


def _load_entry(route):
    path = route["index_path"]
    info = _regular_file(path, "index_unavailable")
    require(info.st_size <= MAX_INDEX_BYTES, "index_too_large", "The session index is too large for this reader.")
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
        require(len(raw.encode("utf-8")) <= MAX_INDEX_BYTES, "index_too_large", "The session index is too large for this reader.")
        index = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ReplyReadError("invalid_index", "The session index cannot be read safely.") from None
    require(isinstance(index, dict), "invalid_index", "The session index must be an object.")
    # Look up only this verified route, without enumerating other contact names.
    entry = index.get(route["session_key"])
    require(isinstance(entry, dict), "session_missing", "No indexed session exists for this verified private chat.")
    origin = entry.get("origin")
    require(isinstance(origin, dict), "sender_unverified", "The session has no sender metadata.")
    require(entry.get("session_key") == route["session_key"]
            and entry.get("platform") == origin.get("platform") == "telegram"
            and entry.get("chat_type") == origin.get("chat_type") == "dm"
            and origin.get("chat_id") == route["chat_id"]
            and origin.get("user_id") == route["user_id"]
            and origin.get("thread_id") in (None, ""),
            "sender_unverified", "Session metadata does not match the configured private-chat sender.")
    require(not origin.get("is_bot") and not origin.get("internal"),
            "sender_unverified", "The indexed origin is marked as an internal or bot event.")
    require(not entry.get("suspended"), "session_suspended", "This session is suspended; no replies were read.")
    session_id = entry.get("session_id")
    require(isinstance(session_id, str) and QUESTION_PATTERN.fullmatch(session_id),
            "invalid_session", "The indexed session ID is invalid.")
    return {"session_id": session_id, "session_key": route["session_key"]}


def _open_readonly(route):
    info = _regular_file(route["db_path"], "database_unavailable")
    connection = None
    try:
        # Do not use immutable=1: the gateway's live WAL contains recent replies.
        connection = sqlite3.connect(route["db_path"].absolute().as_uri() + "?mode=ro",
                                     uri=True, timeout=1, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
    except sqlite3.Error:
        if connection is not None:
            connection.close()
        raise ReplyReadError("database_unavailable", "The Hermes database could not be opened read-only.") from None
    return connection, [info.st_dev, info.st_ino]


def _verify_schema_and_sender(connection, entry, route):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
    session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
    require({"id", "session_id", "role", "content", "timestamp"} <= columns
            and {"id", "source", "user_id"} <= session_columns,
            "unsupported_schema", "This Hermes database lacks the fields required to verify a reply.")
    index_columns = [row["name"] for row in connection.execute("PRAGMA index_info(idx_messages_session)")]
    require(index_columns[:2] == ["session_id", "timestamp"],
            "unsupported_schema", "The scoped session/time index is unavailable; a history scan is not allowed.")
    row = connection.execute("SELECT id, source, user_id FROM sessions WHERE id = ?",
                             (entry["session_id"],)).fetchone()
    require(row is not None and row["source"] == "telegram" and row["user_id"] == route["user_id"],
            "sender_unverified", "The database session does not verify the configured Telegram user.")
    return columns


def _same_entry(route, entry):
    require(_load_entry(route) == entry, "session_changed",
            "The private-chat session changed. No other session was searched and no notification was resent.")


def _same_database(route, fingerprint):
    info = _regular_file(route["db_path"], "database_unavailable")
    require([info.st_dev, info.st_ino] == fingerprint, "database_changed",
            "The Hermes database was replaced; the previous reply cursor cannot be reused.")


def prepare_cursor(config):
    """Before sending, capture one verified session and its message-ID watermark.

    Reads only routing/schema metadata and MAX(id), never message content.
    Expected failures raise ReplyReadError. The caller stores this cursor privately.
    """
    connection = None
    try:
        route = _route(config)
        entry = _load_entry(route)
        connection, fingerprint = _open_readonly(route)
        _verify_schema_and_sender(connection, entry, route)
        after_id = connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages INDEXED BY idx_messages_session WHERE session_id = ?",
            (entry["session_id"],)).fetchone()[0]
        _same_entry(route, entry)
        _same_database(route, fingerprint)
        return {"session_id": entry["session_id"], "session_key": entry["session_key"],
                "after_id": after_id, "started_at": time.time(), "target_hash": route["target_hash"],
                "db_fingerprint": fingerprint, "identity_scope": "session"}
    except (sqlite3.Error, OSError, OverflowError):
        raise ReplyReadError("metadata_unavailable", "Could not prepare the scoped reply cursor without modifying Hermes.") from None
    finally:
        if connection is not None:
            connection.close()


def _epoch(value):
    require(isinstance(value, str), "invalid_question_time", "The notification timestamps must include a timezone.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "invalid_question_time", "The notification timestamps must include a timezone.")
        result = parsed.timestamp()
        require(math.isfinite(result), "invalid_question_time", "The notification timestamp is invalid.")
        return result
    except (ValueError, OverflowError):
        raise ReplyReadError("invalid_question_time", "The notification timestamp is invalid.") from None


def _extra_message_constraints(columns):
    """If a newer schema carries provenance, never ignore contradictory fields."""
    clauses = []
    for name in ("sender_id", "user_id"):
        if name in columns:
            clauses.append('CAST(m."' + name + '" AS TEXT) = :uid')
    if "chat_id" in columns:
        clauses.append('CAST(m."chat_id" AS TEXT) = :uid')
    for name in ("source", "platform"):
        if name in columns:
            clauses.append('m."' + name + '" = \'telegram\'')
    if "chat_type" in columns:
        clauses.append('m."chat_type" IN (\'dm\', \'private\')')
    for name in ("internal", "is_internal", "is_bot", "outgoing", "is_outgoing"):
        if name in columns:
            clauses.append('m."' + name + '" = 0')
    return "".join(" AND " + clause for clause in clauses)


def read_reply(config, question, cursor):
    """Return matching ordinary answers transiently, with no writes or side effects.

    All review/verification questions are rejected before touching local files.
    The body itself must begin exactly ``[JOB-APPLY:<question_id>]`` followed by
    whitespace and a nonempty answer. A marker in quoted context is insufficient.
    Only the original indexed session is allowed; rotation fails closed.
    """
    connection = None
    try:
        require(isinstance(question, dict), "invalid_question", "Question metadata is required.")
        require(question.get("kind") == "information", "unsupported_question_kind",
                "Verification codes and application reviews must be handled in the current task; no messages were read.")
        require(question.get("state") == "pending" and question.get("notification") == "sent",
                "question_not_waiting", "Only a pending question with a confirmed sent notification may receive replies.")
        question_id = question.get("question_id")
        require(isinstance(question_id, str) and QUESTION_PATTERN.fullmatch(question_id),
                "invalid_question_id", "The question marker is invalid.")
        claimed_at, sent_at = _epoch(question.get("claimed_at")), _epoch(question.get("sent_at"))
        require(sent_at >= claimed_at, "invalid_question_time", "The notification timestamps are out of order.")
        require(isinstance(cursor, dict), "cursor_missing", "No private send-time reply cursor is available.")
        after_id, started_at = cursor.get("after_id"), cursor.get("started_at")
        require(type(after_id) is int and after_id >= 0
                and type(started_at) in (int, float) and math.isfinite(started_at),
                "invalid_cursor", "The private reply cursor is invalid.")
        route = _route(config)
        for key, expected in (("backend", "hermes"), ("platform", "telegram"), ("target_hash", route["target_hash"])):
            require(key not in question or question[key] == expected,
                    "question_route_mismatch", "The sent question receipt belongs to a different route.")
        require(cursor.get("target_hash") == route["target_hash"] and cursor.get("session_key") == route["session_key"],
                "route_changed", "The reply cursor belongs to a different route.")
        entry = _load_entry(route)
        require(entry["session_id"] == cursor.get("session_id"), "session_changed",
                "The private-chat session changed. No new or ancestor session was searched and no notification was resent.")
        connection, fingerprint = _open_readonly(route)
        require(fingerprint == cursor.get("db_fingerprint"), "database_changed",
                "The Hermes database was replaced; the reply cursor cannot be reused.")
        columns = _verify_schema_and_sender(connection, entry, route)
        marker = "[JOB-APPLY:" + question_id + "]"
        sql = """
            SELECT m.id, m.timestamp,
                   CASE WHEN length(m.content) <= :max_chars THEN m.content ELSE NULL END AS content
            FROM messages AS m INDEXED BY idx_messages_session
            JOIN sessions AS s ON s.id = m.session_id
            WHERE m.session_id = :sid
              AND s.source = 'telegram' AND s.user_id = :uid
              AND m.role = 'user' COLLATE BINARY AND m.timestamp >= :since AND m.timestamp <= :until
              AND m.id > :after_id AND typeof(m.content) = 'text'
              AND substr(m.content, 1, length(:marker)) = :marker COLLATE BINARY
              AND substr(m.content, length(:marker) + 1, 1) IN (' ', char(9), char(10), char(13))
              AND length(trim(substr(m.content, length(:marker) + 1), ' ' || char(9) || char(10) || char(13))) > 0
        """ + _extra_message_constraints(columns) + " ORDER BY m.timestamp, m.id LIMIT :limit"
        rows = connection.execute(sql, {
            "sid": entry["session_id"], "uid": route["user_id"], "after_id": after_id,
            # Telegram can deliver before the send call returns. The cursor and
            # exact question marker exclude old replies without losing a fast
            # answer that predates the local mark-sent acknowledgement.
            "since": max(claimed_at, started_at), "until": time.time(),
            "marker": marker, "max_chars": MAX_REPLY_CHARS, "limit": MAX_REPLIES + 1,
        }).fetchall()
        # Discard results if routing changed during the read.
        _same_entry(route, entry)
        _same_database(route, fingerprint)
        require(len(rows) <= MAX_REPLIES, "too_many_replies", "Too many matching replies require review in the current task.")
        require(all(row["content"] is not None for row in rows), "reply_too_large", "A matching reply is too long for automatic retrieval.")
        if not rows:
            return {"status": "waiting", "identity_scope": "session"}
        return {"status": "replies", "identity_scope": "session",
                "replies": [{"source": "telegram", "id": row["id"], "timestamp": row["timestamp"],
                             "content": row["content"]} for row in rows]}
    except ReplyReadError as exc:
        return {"status": "blocked", "identity_scope": "session", "reason": exc.reason, "message": str(exc)}
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return {"status": "blocked", "identity_scope": "session", "reason": "read_unavailable",
                "message": "The scoped read-only reply query failed. No broader search was attempted."}
    finally:
        if connection is not None:
            connection.close()
