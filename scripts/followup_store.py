#!/usr/bin/env python3
"""Private pending-question metadata; no timers, messaging, or answer storage.

Call check with the proposed question metadata before showing a question. It is
read-only; previously asked information fields block the whole proposed batch,
including across tasks and closed records. Reuse stable field keys for synonymous
questions. A check does not reserve a question; ask checks again under its lock.
Call ask only after showing an allowed question. Its fixed 180-second delay uses
the real clock, never caller-supplied time. Call resolve only after the entire batch
has been answered; partial replies leave it pending. Resolving a reminder is not
permission to fill fields, confirm a review, or submit an application.
Verification prompts are replaced with a neutral description. Other prompts and
references must describe questions or identify messages, never contain answers
or credentials. A schema cannot detect every secret embedded in free text.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
import uuid

from profile_store import StoreError, locked, nonempty, parse_json, require, timestamp

DEFAULT_FILE = "~/Documents/Codex/job-applications/followups.json"
DELAY_SECONDS = 180
KINDS = ("information", "verification", "review")
ASK_KEYS = {"question_id", "company", "job_id", "fields", "thread_id", "kind"}
RECEIPT_KEYS = {"message_id", "backend", "platform", "target_hash"}
STORED_KEYS = ASK_KEYS | RECEIPT_KEYS | {
    "asked_at", "deadline", "state", "notification", "claimed_at", "sent_at",
    "uncertain_at", "resolved_at", "cancelled_at", "resolution"}
VERIFICATION_FIELDS = [{"key": "verification_action", "label": "登录验证",
                        "prompt": "请回到当前任务完成登录验证，不要将验证码写入提醒或记录。"}]


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def keys_only(value, allowed):
    require(isinstance(value, dict) and set(value) <= allowed,
            "Unexpected fields are forbidden; store question metadata, never answers or credentials.")


def valid_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is not None,
            "Question ID must be a short opaque identifier.")


def field_key(value):
    # Semantic aliases must use one stable key; do not guess equivalence from prose.
    return unicodedata.normalize("NFKC", value).strip().casefold()


def ask_valid(value):
    keys_only(value, ASK_KEYS)
    require(all(nonempty(value.get(k)) for k in ("company", "job_id")), "Company and job_id are required.")
    require(value.get("kind") in KINDS, "Question kind must be information, verification, or review.")
    if "question_id" in value:
        valid_id(value["question_id"])
    if "thread_id" in value:
        require(nonempty(value["thread_id"]), "thread_id must be nonempty text.")
    fields = value.get("fields")
    require(isinstance(fields, list) and bool(fields), "A question needs a nonempty fields array.")
    seen = set()
    for field in fields:
        keys_only(field, {"key", "label", "prompt"})
        require(all(nonempty(field.get(k)) for k in ("key", "label", "prompt")),
                "Each requested field needs key, label, and prompt text; no answer/value is accepted.")
        require(field["key"] not in seen, "Each requested field key must be unique.")
        seen.add(field["key"])


def receipt_valid(value):
    require(all(nonempty(value.get(k)) for k in RECEIPT_KEYS), "A send receipt needs message_id, backend, platform, and target_hash.")
    require(re.fullmatch(r"[0-9a-f]{64}", value["target_hash"]) is not None,
            "target_hash must be a lowercase SHA-256 digest, never the recipient address.")


def validate(data):
    keys_only(data, {"schema_version", "questions"})
    require(type(data.get("schema_version")) is int and data["schema_version"] == 1
            and isinstance(data.get("questions"), list), "Invalid follow-up storage schema; existing data was not overwritten.")
    seen = set()
    for question in data["questions"]:
        keys_only(question, STORED_KEYS)
        ask_valid({k: v for k, v in question.items() if k in ASK_KEYS})
        require("question_id" in question, "Stored question ID is missing.")
        require(question["question_id"] not in seen, "Duplicate question IDs; existing data was not overwritten.")
        seen.add(question["question_id"])
        require(timestamp(question.get("deadline")) - timestamp(question.get("asked_at"))
                == dt.timedelta(seconds=DELAY_SECONDS), "Stored reminder delay must be exactly 180 seconds.")
        require(question.get("state") in ("pending", "answered", "cancelled"), "Invalid question state.")
        require(question.get("notification") in ("unsent", "sending", "sent", "uncertain"), "Invalid notification state.")
        if question["kind"] == "verification":
            require(question["fields"] == VERIFICATION_FIELDS, "Verification records may contain only neutral prompt metadata.")
        for key in ("claimed_at", "sent_at", "uncertain_at", "resolved_at", "cancelled_at"):
            if key in question:
                timestamp(question[key])
        if question["notification"] != "unsent":
            require("claimed_at" in question and timestamp(question["claimed_at"]) >= timestamp(question["deadline"]),
                    "A notification cannot be claimed before its deadline.")
        if question["notification"] == "sent":
            receipt_valid(question)
            require("sent_at" in question, "Sent notification requires a timestamp.")
        if question["notification"] == "uncertain":
            require("uncertain_at" in question, "Uncertain notification requires a timestamp.")
        if question["state"] == "answered":
            resolution = question.get("resolution")
            keys_only(resolution, {"source", "reference"})
            require(resolution.get("source") in ("codex", "im") and nonempty(resolution.get("reference"))
                    and "resolved_at" in question, "Answered questions require source, reference, and resolved_at.")
        if question["state"] == "cancelled":
            require("cancelled_at" in question, "Cancelled question requires a timestamp.")


def read_store(path, harden_permissions=True):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"schema_version": 1, "questions": []}
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "Follow-up path must be a regular file.")
        if harden_permissions:
            os.fchmod(stream.fileno(), 0o600)
        data = parse_json(stream.read())
    validate(data)
    return data


def write_store(path, data):
    validate(data)
    fd, name = tempfile.mkstemp(prefix=".followups-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def is_due(question, current):
    return question["state"] == "pending" and question["notification"] == "unsent" and timestamp(question["deadline"]) <= current


def check_question(data, question):
    """Return matching IDs and requested keys, never old prompts or answers."""
    requested = {field_key(field["key"]): field["key"] for field in question["fields"]}
    require(len(requested) == len(question["fields"]), "Each requested field key must be unique after normalization.")
    already_asked = set()
    matches = []
    reason = None

    def signature(item):
        return (item["company"], item["job_id"], item.get("thread_id"), item["kind"],
                tuple(sorted(field_key(field["key"]) for field in item["fields"])))

    for existing in data["questions"]:
        same_id = existing["question_id"] == question.get("question_id")
        overlap = set(requested) & {field_key(field["key"]) for field in existing["fields"]}
        same_information_scope = (question["kind"] == existing["kind"] == "information"
                                  and question["company"] == existing["company"]
                                  and question["job_id"] == existing["job_id"])
        previous_information = same_information_scope and bool(overlap)
        protected_batch = (question["kind"] != "information" and signature(existing) == signature(question)
                           and (existing["state"] == "pending"
                                or existing["notification"] in ("sending", "sent", "uncertain")))
        if previous_information or protected_batch or same_id:
            matches.append(existing["question_id"])
            if previous_information or protected_batch:
                already_asked.update(overlap)
            reason = reason or ("already_asked_fields" if previous_information
                                else "protected_batch" if protected_batch else "question_id_conflict")
    result = {"status": "conflict" if matches else "ready", "can_ask": not matches,
              "question_ids": matches,
              "asked_fields": [original for key, original in requested.items() if key in already_asked],
              "new_fields": [original for key, original in requested.items() if key not in already_asked]}
    if matches:
        result.update(question_id=matches[0], reason=reason)
    return result


def run(data, command, current, payload=None, question_id=None, company=None, job_id=None, thread_id=None, state=None):
    if command in ("check", "preflight", "ask"):
        ask_valid(payload)
        question = dict(payload)
        if command == "ask":
            question.setdefault("question_id", "q-" + uuid.uuid4().hex[:10])
        if question["kind"] == "verification":
            question["fields"] = VERIFICATION_FIELDS
        checked = check_question(data, question)
        if not checked["can_ask"] or command != "ask":
            return checked, 0 if checked["can_ask"] else 3, False
        question.update(asked_at=iso(current), deadline=iso(current + dt.timedelta(seconds=DELAY_SECONDS)),
                        state="pending", notification="unsent")
        data["questions"].append(question)
        return {k: question[k] for k in ("question_id", "asked_at", "deadline", "state", "notification")}, 0, True
    if command in ("due", "list"):
        questions = [q for q in data["questions"] if (company is None or q["company"] == company)
                     and (job_id is None or q["job_id"] == job_id) and (thread_id is None or q.get("thread_id") == thread_id)
                     and (state is None or q["state"] == state) and (command != "due" or is_due(q, current))]
        return {"status": "ok", "questions": questions}, 0, False
    if command in ("resolve", "mark-sent"):
        allowed = {"question_id", "source", "reference", "answer"} if command == "resolve" else {"question_id"} | RECEIPT_KEYS
        keys_only(payload, allowed)
        # Never inspect, echo, or persist a supplied answer body, including codes.
        payload = {k: v for k, v in payload.items() if k != "answer"}
        question_id = payload.get("question_id")
    valid_id(question_id)
    question = next((q for q in data["questions"] if q["question_id"] == question_id), None)
    if question is None:
        return {"status": "missing", "question_id": question_id}, 4, False
    if command == "get":
        return {"status": "ok", "question": question}, 0, False
    if command == "claim":
        if not is_due(question, current):
            status = "not_due" if question["state"] == "pending" and question["notification"] == "unsent" else "not_claimable"
            return {"status": status, "question_id": question_id}, 3, False
        question.update(notification="sending", claimed_at=iso(current))
    elif command == "mark-sent":
        receipt_valid(payload)
        require(question["notification"] == "sending", "Only a claimed sending notification may be marked sent.")
        question.update({k: payload[k] for k in RECEIPT_KEYS})
        question.update(notification="sent", sent_at=iso(current))
    elif command == "mark-uncertain":
        require(question["notification"] == "sending", "Only a claimed sending notification may become uncertain; do not retry uncertain sends.")
        question.update(notification="uncertain", uncertain_at=iso(current))
    elif command == "resolve":
        require(payload.get("source") in ("codex", "im") and nonempty(payload.get("reference")),
                "Resolution requires source codex/im and a reference, never answer text.")
        if question["state"] != "pending":
            return {"status": "already_closed", "question_id": question_id}, 3, False
        question.update(state="answered", resolved_at=iso(current),
                        resolution={k: payload[k] for k in ("source", "reference")})
    elif command == "cancel":
        if question["state"] != "pending":
            return {"status": "already_closed", "question_id": question_id}, 3, False
        question.update(state="cancelled", cancelled_at=iso(current))
    else:
        raise StoreError("Unsupported follow-up command.")
    return {"status": "ok", "question_id": question_id, "state": question["state"], "notification": question["notification"]}, 0, True


def execute(path, command, **kwargs):
    path = Path(path).expanduser().absolute()
    if command in ("check", "preflight"):
        # Writers replace the data file atomically, so a read gets one complete
        # version. Do not create a parent/lock or chmod data for an advisory check.
        data = read_store(path, harden_permissions=False)
        result, code, _ = run(data, command, utc_now(), **kwargs)
        return result, code
    with locked(path):
        data = read_store(path)
        result, code, changed = run(data, command, utc_now(), **kwargs)
        if changed:
            write_store(path, data)
    return result, code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=DEFAULT_FILE, help="Dedicated private storage path, separate from profile.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "preflight", "ask", "resolve", "mark-sent"):
        commands.add_parser(name, help="Read a JSON object from stdin")
    for name in ("get", "claim", "mark-uncertain", "cancel"):
        commands.add_parser(name).add_argument("question_id")
    for name in ("due", "list"):
        listing = commands.add_parser(name)
        for option in ("company", "job-id", "thread-id"):
            listing.add_argument("--" + option)
        listing.add_argument("--state", choices=("pending", "answered", "cancelled"))
    args = vars(parser.parse_args())
    path, command = args.pop("file"), args.pop("command")
    try:
        if command in ("check", "preflight", "ask", "resolve", "mark-sent"):
            args["payload"] = parse_json(sys.stdin.read())
        result, code = execute(path, command, **args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except (StoreError, OSError, UnicodeError, RecursionError) as error:
        message = str(error) if isinstance(error, StoreError) else "Follow-up I/O failed; no successful update was confirmed."
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
