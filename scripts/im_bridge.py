#!/usr/bin/env python3
"""One conditional, text-only Hermes Telegram reminder and scoped reply reads.

Configure only a numerically verified private DM binding. No username lookup,
credential copying, arbitrary messages, attachments, or application approval.
Only information replies may be returned to the current agent for verification;
they never resolve questions, write facts, or confirm/submit applications here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

import followup_store
from profile_store import StoreError, locked, nonempty, now, parse_json, require, timestamp

DEFAULT_CONFIG = "~/Documents/Codex/job-applications/notifications.json"
CONFIG_KEYS = {"backend", "platform", "chat_id", "user_id", "hermes_home", "hermes_root", "verified_username", "binding_reference"}
MAX_MESSAGE_CHARS = 2000
ADAPTER_TIMEOUT = 45

# Constant worker code: private routing/message data travels only through stdin.
WORKER = r'''
import asyncio, contextlib, inspect, io, json, logging, os, re, sys
from pathlib import Path
payload = json.load(sys.stdin)
result = {"status": "adapter_unavailable"}
logging.disable(logging.CRITICAL)
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        home = str(Path(payload["hermes_home"]).expanduser().absolute())
        os.environ["HERMES_HOME"] = home
        sys.path.insert(0, payload["hermes_root"])
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(Path(home) / ".env"), override=True)
        os.environ["HERMES_HOME"] = home
        from gateway.config import Platform, load_gateway_config
        from gateway.platforms.telegram import TelegramAdapter
        from tools import send_message_tool as send_tool
        _send_to_platform = send_tool._send_to_platform
        original_retry = getattr(send_tool, "_send_telegram_message_with_retry", None)
        if not callable(_send_to_platform) or not callable(original_retry):
            raise ValueError("Unsupported adapter API")
        inspect.signature(_send_to_platform).bind(Platform.TELEGRAM, None, payload["chat_id"], "probe", media_files=[], thread_id=None)
        inspect.signature(original_retry).bind(None, attempts=1, chat_id=1, text="probe")
        pconfig = load_gateway_config().platforms.get(Platform.TELEGRAM)
        configured = bool(pconfig and pconfig.enabled and pconfig.token)
        if payload["mode"] == "doctor":
            result = {"status": "ok", "adapter_available": True, "platform_configured": configured}
        elif payload["mode"] == "send" and configured:
            async def single_attempt(bot, **kwargs):
                kwargs.pop("attempts", None)
                return await original_retry(bot, attempts=1, **kwargs)
            send_tool._send_telegram_message_with_retry = single_attempt
            message = payload["message"]
            if (not isinstance(message, str) or not message.strip() or len(message) > 2000
                    or re.search(r"MEDIA:|\[\[audio_as_voice\]\]", message, re.I)):
                raise ValueError("Invalid text")
            pconfig.extra = {**(getattr(pconfig, "extra", {}) or {}), "disable_link_previews": True}
            async def dispatch():
                return await asyncio.wait_for(_send_to_platform(
                    Platform.TELEGRAM, pconfig, payload["chat_id"], message,
                    media_files=[], thread_id=None), timeout=40)
            raw = asyncio.run(dispatch())
            message_id = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
            if (isinstance(raw, dict) and raw.get("success") is True and not raw.get("skipped")
                    and not raw.get("error") and raw.get("platform") == "telegram"
                    and str(raw.get("chat_id")) == payload["chat_id"]
                    and re.fullmatch(r"[1-9][0-9]*", message_id)):
                result = {"status": "sent", "platform": "telegram", "chat_id": payload["chat_id"], "message_id": message_id}
            else:
                result = {"status": "send_uncertain"}
except BaseException:
    result = {"status": "send_uncertain" if payload.get("mode") == "send" else "adapter_unavailable"}
print(json.dumps(result))
'''


def private_read(path, validator):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "Private bridge files must be regular 0600 files owned by the current user.")
        data = parse_json(stream.read())
    validator(data)
    return data


def private_write(path, data, validator):
    validator(data)
    fd, name = tempfile.mkstemp(prefix=".im-bridge-", suffix=".tmp", dir=path.parent)
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


def config_valid(config):
    require(isinstance(config, dict) and set(config) <= CONFIG_KEYS | {"schema_version", "configured_at"}, "Unexpected notification configuration fields are forbidden.")
    require(type(config.get("schema_version")) is int and config["schema_version"] == 1, "Invalid notification configuration schema.")
    require(config.get("backend") == "hermes" and config.get("platform") == "telegram", "Only Hermes Telegram private DM bindings are supported.")
    for key in ("chat_id", "user_id"):
        require(isinstance(config.get(key), str) and re.fullmatch(r"[1-9][0-9]*", config[key]), "A verified numeric private DM binding is required; usernames cannot be used as targets.")
    require(config["chat_id"] == config["user_id"], "The private DM chat and user binding must match.")
    for key in ("hermes_home", "hermes_root"):
        require(nonempty(config.get(key)) and Path(config[key]).is_absolute(), "Hermes paths must be explicit absolute paths.")
    require(nonempty(config.get("binding_reference")), "A reference to the verified binding is required.")
    if "verified_username" in config:
        require(nonempty(config["verified_username"]) and re.fullmatch(r"@?[A-Za-z0-9_]{1,64}", config["verified_username"]), "verified_username may only describe an already verified binding.")
    timestamp(config.get("configured_at"))


def configure(path, payload):
    require(isinstance(payload, dict) and set(payload) <= CONFIG_KEYS, "Only routing fields are accepted; never include credentials.")
    config = {**payload, "schema_version": 1, "configured_at": now()}
    require(nonempty(config.get("hermes_home", "~/.hermes")), "Hermes paths must be text.")
    config["hermes_home"] = str(Path(config.get("hermes_home", "~/.hermes")).expanduser().absolute())
    default_root = str(Path(config["hermes_home"]) / "hermes-agent")
    require(nonempty(config.get("hermes_root", default_root)), "Hermes paths must be text.")
    config["hermes_root"] = str(Path(config.get("hermes_root", default_root)).expanduser().absolute())
    config_valid(config)
    path = Path(path).expanduser().absolute()
    with locked(path):
        private_read(path, config_valid)  # A corrupt existing file is never replaced.
        private_write(path, config, config_valid)
    return {"status": "configured", "backend": "hermes", "platform": "telegram", "route_configured": True}


def load_config(path):
    config = private_read(Path(path).expanduser().absolute(), config_valid)
    require(config is not None, "No verified notification route is configured.")
    return config


def target_hash(config):
    return hashlib.sha256(("telegram:" + config["chat_id"] + ":" + config["user_id"]).encode("utf-8")).hexdigest()


def run_adapter(config, mode, message=None):
    root = Path(config["hermes_root"])
    python = root / "venv" / "bin" / "python"
    if not python.is_file():
        python = root / ".venv" / "bin" / "python"
    if not python.is_file() or not (root / "tools" / "send_message_tool.py").is_file():
        return {"status": "adapter_unavailable"}
    payload = {k: config[k] for k in ("hermes_home", "hermes_root", "chat_id")}
    payload.update(mode=mode)
    if message is not None:
        payload["message"] = message
    environment = os.environ.copy()
    environment["HERMES_HOME"] = config["hermes_home"]
    try:
        completed = subprocess.run([str(python), "-B", "-c", WORKER], input=json.dumps(payload, ensure_ascii=False),
                                   text=True, capture_output=True, cwd=root, env=environment,
                                   timeout=ADAPTER_TIMEOUT if mode == "send" else 15)
        if completed.returncode == 0:
            result = parse_json(completed.stdout)
            if isinstance(result, dict):
                return result
    except (OSError, subprocess.TimeoutExpired, StoreError, UnicodeError):
        pass
    return {"status": "send_uncertain" if mode == "send" else "adapter_unavailable"}


def doctor(config_path):
    try:
        config = load_config(config_path)
    except (StoreError, OSError, UnicodeError):
        return {"status": "unconfigured", "route_configured": False, "adapter_available": False, "platform_configured": False}
    result = run_adapter(config, "doctor")
    return {"status": "ok" if result.get("adapter_available") and result.get("platform_configured") else "unavailable",
            "route_configured": True, "adapter_available": result.get("adapter_available") is True,
            "platform_configured": result.get("platform_configured") is True}


def render_message(question):
    followup_store.valid_id(question.get("question_id"))
    marker = "[JOB-APPLY:" + question["question_id"] + "]"
    kind = question.get("kind")
    require(kind in ("information", "verification", "review"), "Unsupported question kind.")
    require(nonempty(question.get("company")) and nonempty(question.get("job_id")), "Question routing metadata is missing.")
    lines = [marker, question["company"] + " · " + question["job_id"]]
    if kind == "information":
        fields = question.get("fields")
        require(isinstance(fields, list) and bool(fields), "Question fields are missing.")
        for field in fields:
            require(isinstance(field, dict) and nonempty(field.get("label")) and nonempty(field.get("prompt")), "Question fields must contain text only.")
            lines.append(field["label"] + "：" + field["prompt"])
        lines.append("请单独发送一条纯文本回复，以完整编号 " + marker + " 开头，后面逐项填写答案。仅点 Telegram 的回复引用不够。")
    elif kind == "verification":
        lines.append("请回到 Codex 当前任务完成登录验证。不要通过聊天发送验证码。")
    else:
        lines.append("请回到 Codex 检查并明确确认这份网申。此处回复不作为提交确认。")
    message = "\n".join(lines)
    require(len(message) <= MAX_MESSAGE_CHARS, "Reminder exceeds the 2000-character text limit.")
    require(re.search(r"MEDIA:|\[\[audio_as_voice\]\]", message, re.I) is None, "Media and attachment directives are forbidden.")
    return message


def prepare_cursor(config):
    from hermes_reply import prepare_cursor as implementation
    return implementation(config)


def read_reply(config, question, cursor):
    from hermes_reply import read_reply as implementation
    return implementation(config, question, cursor)


def cursor_store_valid(data):
    require(isinstance(data, dict) and set(data) == {"schema_version", "cursors"}
            and type(data["schema_version"]) is int and data["schema_version"] == 1
            and isinstance(data["cursors"], dict), "Invalid reply cursor storage.")
    for question_id, entry in data["cursors"].items():
        followup_store.valid_id(question_id)
        require(isinstance(entry, dict) and set(entry) == {"target_hash", "cursor"}
                and isinstance(entry["target_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["target_hash"])
                and isinstance(entry["cursor"], dict), "Invalid reply cursor entry.")


def cursor_path(config_path, cursors_path):
    return Path(cursors_path).expanduser().absolute() if cursors_path else Path(config_path).expanduser().absolute().with_name("im-reply-cursors.json")


def save_cursor(path, question_id, config, cursor):
    with locked(path):
        data = private_read(path, cursor_store_valid) or {"schema_version": 1, "cursors": {}}
        data["cursors"][question_id] = {"target_hash": target_hash(config), "cursor": cursor}
        private_write(path, data, cursor_store_valid)


def mark_uncertain(followups_path, question_id):
    try:
        followup_store.execute(followups_path, "mark-uncertain", question_id=question_id)
    except (StoreError, OSError, UnicodeError):
        pass  # A retained 'sending' claim also prevents an automatic retry.
    return {"status": "send_uncertain", "question_id": question_id}, 2


def send_question(config_path, followups_path, question_id, cursors_path=None):
    config = load_config(config_path)
    retrieved, code = followup_store.execute(followups_path, "get", question_id=question_id)
    if code:
        return retrieved, code
    question = retrieved["question"]
    message = render_message(question)
    if question["state"] != "pending" or question["notification"] != "unsent":
        return {"status": "not_claimable", "question_id": question_id}, 3
    if not followup_store.is_due(question, followup_store.utc_now()):
        return {"status": "not_due", "question_id": question_id}, 3
    health = run_adapter(config, "doctor")
    if not (health.get("adapter_available") is True and health.get("platform_configured") is True):
        return {"status": "adapter_unavailable", "question_id": question_id}, 2
    cursor = None
    if question["kind"] == "information":
        try:
            cursor = prepare_cursor(config)
        except Exception:
            return {"status": "reply_source_unavailable", "question_id": question_id}, 2
    claimed, code = followup_store.execute(followups_path, "claim", question_id=question_id)
    if code:
        return claimed, code
    try:
        if cursor is not None:
            save_cursor(cursor_path(config_path, cursors_path), question_id, config, cursor)
        fresh, fresh_code = followup_store.execute(followups_path, "get", question_id=question_id)
        if fresh_code or fresh["question"]["state"] != "pending" or fresh["question"]["notification"] != "sending":
            return {"status": "closed_no_send", "question_id": question_id}, 3
        if target_hash(load_config(config_path)) != target_hash(config):
            return mark_uncertain(followups_path, question_id)
        result = run_adapter(config, "send", message=message)
        message_id = str(result.get("message_id", ""))
        if not (result.get("status") == "sent" and not result.get("skipped") and not result.get("error")
                and result.get("platform") == "telegram" and str(result.get("chat_id")) == config["chat_id"]
                and re.fullmatch(r"[1-9][0-9]*", message_id)):
            return mark_uncertain(followups_path, question_id)
        receipt, code = followup_store.execute(followups_path, "mark-sent", payload={
            "question_id": question_id, "message_id": message_id, "backend": "hermes",
            "platform": "telegram", "target_hash": target_hash(config)})
        if code:
            return mark_uncertain(followups_path, question_id)
        return {"status": "sent", "question_id": question_id, "platform": "telegram",
                "target_verified": True, "message_id": message_id}, 0
    except Exception:
        return mark_uncertain(followups_path, question_id)


def peek_reply(config_path, followups_path, question_id, cursors_path=None):
    retrieved, code = followup_store.execute(followups_path, "get", question_id=question_id)
    if code:
        return retrieved, code
    question = retrieved["question"]
    if question["kind"] != "information":
        return {"status": "blocked", "reason": "return_to_codex", "question_id": question_id}, 3
    if question["state"] != "pending" or question["notification"] != "sent":
        return {"status": "waiting", "question_id": question_id}, 0
    config = load_config(config_path)
    data = private_read(cursor_path(config_path, cursors_path), cursor_store_valid)
    entry = data["cursors"].get(question_id) if data else None
    digest = target_hash(config)
    if entry is None or entry["target_hash"] != digest or question.get("target_hash") != digest:
        return {"status": "blocked", "reason": "route_or_cursor_mismatch", "question_id": question_id}, 3
    try:
        result = read_reply(config, question, entry["cursor"])
    except Exception:
        return {"status": "blocked", "reason": "reply_source_unavailable", "question_id": question_id}, 2
    return result, 3 if result.get("status") == "blocked" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--followups", default=followup_store.DEFAULT_FILE)
    parser.add_argument("--cursors", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="Read verified routing metadata from stdin; never include credentials")
    commands.add_parser("doctor", help="Read-only route and adapter checks; no message is sent")
    for command in ("send", "peek", "get"):
        commands.add_parser(command).add_argument("question_id")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            result, code = configure(args.config, parse_json(sys.stdin.read())), 0
        elif args.command == "doctor":
            result = doctor(args.config)
            code = 0 if result["status"] == "ok" else 2
        elif args.command == "get":
            result, code = followup_store.execute(args.followups, "get", question_id=args.question_id)
        else:
            operation = send_question if args.command == "send" else peek_reply
            result, code = operation(args.config, args.followups, args.question_id, cursors_path=args.cursors)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except (StoreError, OSError, UnicodeError, RecursionError):
        print(json.dumps({"status": "error", "message": "Bridge operation could not be completed; no delivery was confirmed."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
