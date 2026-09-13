#!/usr/bin/env python3
"""Private local facts and application history; Python 3 standard library only.

Secret-key checks prevent some mistakes, but cannot detect secrets in free text.
Send personal data through stdin, never shell arguments. All times use ISO 8601
with an explicit timezone. Scope/context values are strings and match exactly.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

DEFAULT_FILE = "~/Documents/Codex/job-applications/profile.json"
STATES = {"draft", "ready", "submitting", "submitted", "uncertain", "blocked"}
SECRET_WORDS = ("password", "passwd", "passphrase", "token", "cookie", "otp",
                "验证码", "密码", "口令", "密钥", "secret", "verificationcode",
                "onetimecode", "smscode", "authcode", "apikey", "privatekey", "recoverycode")


class StoreError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise StoreError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value):
    require(nonempty(value), "Timestamps must be ISO 8601 strings with a timezone.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StoreError("Invalid timestamp; include an explicit timezone.") from None
    require(parsed.tzinfo is not None, "Timestamps must include an explicit timezone.")
    return parsed


def no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^\w]", "", key.casefold()).replace("_", "")
            require(not any(word in normalized for word in SECRET_WORDS),
                    "Credential-like field names are forbidden; do not store passwords, codes, tokens, or cookies.")
            no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_secrets(item)


def scope_valid(scope):
    require(isinstance(scope, dict) and all(nonempty(k) and nonempty(v) for k, v in scope.items()),
            "Scope and context must be JSON objects with nonempty string keys and values.")
    no_secrets(scope)


def fact_valid(fact, stored=False):
    require(isinstance(fact, dict), "A fact must be a JSON object.")
    require(nonempty(fact.get("key")) and "value" in fact, "A fact needs a nonempty key and a value.")
    value = fact["value"]
    require(value is not None and (not isinstance(value, str) or bool(value.strip()))
            and (not isinstance(value, (list, dict)) or bool(value)),
            "A fact value cannot be null, blank text, or an empty list/object; false and 0 are valid answers.")
    no_secrets({fact["key"]: value})
    scope_valid(fact.get("scope", {}))
    source = fact.get("source")
    require(isinstance(source, dict) and source.get("kind") in ("user", "resume")
            and nonempty(source.get("reference")), "Source needs kind user/resume and a nonempty reference.")
    if "valid_until" in fact:
        timestamp(fact["valid_until"])
    if stored:
        timestamp(fact.get("updated_at"))
    no_secrets(fact)


def application_valid(app, stored=False):
    require(isinstance(app, dict), "An application must be a JSON object.")
    required = ("company", "job_id", "account", "url", "resume_ref")
    require(all(nonempty(app.get(k)) for k in required),
            "Application needs company, job_id, account (an alias is allowed), url, and resume_ref.")
    require(isinstance(app.get("status"), str) and app["status"] in STATES, "Invalid application status.")
    if app["status"] == "submitted":
        require(nonempty(app.get("evidence")), "Submitted status requires nonempty evidence.")
    if stored:
        timestamp(app.get("created_at"))
        timestamp(app.get("updated_at"))
    no_secrets(app)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def fact_id(fact):
    return fact["key"], canonical(fact.get("scope", {}))


def application_id(app):
    return tuple(app[k] for k in ("company", "job_id", "account"))


def validate(data):
    require(isinstance(data, dict) and type(data.get("schema_version")) is int
            and data["schema_version"] == 1, "Invalid profile schema_version; existing data was not overwritten.")
    no_secrets(data)
    for field, validator, identity in (("facts", fact_valid, fact_id),
                                        ("applications", application_valid, application_id)):
        require(isinstance(data.get(field), list), "Profile facts and applications must be arrays.")
        seen = set()
        for item in data[field]:
            validator(item, stored=True)
            ident = identity(item)
            require(ident not in seen, "Duplicate record identities in profile; existing data was not overwritten.")
            seen.add(ident)


def reject_constant(_):
    raise StoreError("Non-finite JSON numbers are unsupported.")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON object keys are unsupported.")
        result[key] = value
    return result


def parse_json(text):
    try:
        return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise StoreError("Invalid JSON; existing data was not overwritten.") from None


@contextlib.contextmanager
def locked(path):
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(),
            "Use a private directory owned by the current user; directory symlinks are unsupported.")
    require(stat.S_IMODE(info.st_mode) == 0o700,
            "Profile directory must have 0700 permissions; choose a dedicated private directory.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path) + ".lock", flags, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "Lock path must be a regular file.")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_profile(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"schema_version": 1, "facts": [], "applications": []}, False
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "Profile path must be a regular file.")
        os.fchmod(stream.fileno(), 0o600)
        data = parse_json(stream.read())
    validate(data)
    return data, True


def write_profile(path, data):
    validate(data)
    fd, name = tempfile.mkstemp(prefix=".profile-", suffix=".tmp", dir=path.parent)
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


def lookup(data, key, context):
    scope_valid(context)
    candidates = [f for f in data["facts"] if f["key"] == key
                  and all(context.get(k) == v for k, v in f.get("scope", {}).items())]
    if not candidates:
        return {"status": "missing", "key": key}, 4
    # An expired specific answer must not silently fall back to a broader one.
    specificity = max(len(f.get("scope", {})) for f in candidates)
    most_specific = [f for f in candidates if len(f.get("scope", {})) == specificity]
    current_time = dt.datetime.now(dt.timezone.utc)
    matches = [f for f in most_specific if "valid_until" not in f
               or timestamp(f["valid_until"]) > current_time]
    if not matches:
        return {"status": "expired", "key": key}, 5
    if len({canonical(f["value"]) for f in matches}) > 1:
        return {"status": "ambiguous", "key": key, "match_count": len(matches)}, 6
    return {"status": "known", "key": key, "value": matches[0]["value"],
            "matches": [{k: v for k, v in f.items() if k not in {"key", "value"}} for f in matches]}, 0


def run(args, data):
    command = args.command
    if command == "lookup":
        result, code = lookup(data, args.key, parse_json(args.context))
        return result, code, False
    if command == "put-fact":
        fact = parse_json(sys.stdin.read())
        fact_valid(fact)
        fact.setdefault("scope", {})
        existing = next((f for f in data["facts"] if fact_id(f) == fact_id(fact)), None)
        if existing is not None and canonical(existing["value"]) != canonical(fact["value"]) and not args.replace:
            return {"status": "conflict", "key": fact["key"], "message": "Explicit correction requires --replace."}, 3, False
        if existing is not None and not args.replace:
            incoming_source = fact["source"]
            fact = {**existing, **fact}
            if existing["source"]["kind"] == "user" and incoming_source["kind"] == "resume":
                fact["source"] = existing["source"]
        fact["updated_at"] = now()
        if existing is not None:
            data["facts"][data["facts"].index(existing)] = fact
        else:
            data["facts"].append(fact)
        return {"status": "updated" if existing else "created", "key": fact["key"]}, 0, True
    if command == "record-application":
        app = parse_json(sys.stdin.read())
        application_valid(app)
        existing = next((a for a in data["applications"] if application_id(a) == application_id(app)), None)
        app["created_at"] = existing["created_at"] if existing else now()
        app["updated_at"] = now()
        if existing is not None:
            data["applications"][data["applications"].index(existing)] = app
        else:
            data["applications"].append(app)
        return {"status": "updated" if existing else "created", "application_status": app["status"]}, 0, True
    if command == "list-applications":
        apps = [a for a in data["applications"] if (not args.company or a["company"] == args.company)
                and (not args.job_id or a["job_id"] == args.job_id)]
        return {"status": "ok", "applications": apps}, 0, False
    if command == "forget":
        scope = parse_json(args.scope) if args.scope is not None else None
        if scope is not None:
            scope_valid(scope)
        old_count = len(data["facts"])
        data["facts"] = [f for f in data["facts"] if not (f["key"] == args.key
                         and (scope is None or f.get("scope", {}) == scope))]
        removed = old_count - len(data["facts"])
        return {"status": "forgotten", "key": args.key, "removed": removed}, 0, bool(removed)
    raise StoreError("Unsupported command.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=DEFAULT_FILE, help="Profile path; its parent must be a dedicated 0700 directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    lookup_parser = commands.add_parser("lookup")
    lookup_parser.add_argument("key")
    lookup_parser.add_argument("--context", default="{}")
    put = commands.add_parser("put-fact", help="Read one fact JSON object from stdin")
    put.add_argument("--replace", action="store_true", help="Use only for an explicitly confirmed correction")
    commands.add_parser("record-application", help="Read one application JSON object from stdin")
    listing = commands.add_parser("list-applications")
    listing.add_argument("--company")
    listing.add_argument("--job-id")
    forget = commands.add_parser("forget")
    forget.add_argument("key")
    forget.add_argument("--scope", help="Exact scope JSON; omitted means all scopes for this key")
    args = parser.parse_args()
    try:
        path = Path(args.file).expanduser().absolute()
        with locked(path):
            data, exists = read_profile(path)
            if args.command == "init":
                result, code, changed = {"status": "existing" if exists else "initialized"}, 0, not exists
            else:
                result, code, changed = run(args, data)
            if changed:
                write_profile(path, data)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except (StoreError, OSError, UnicodeError, RecursionError) as error:
        message = str(error) if isinstance(error, StoreError) else "Profile I/O failed; no successful update was confirmed."
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
