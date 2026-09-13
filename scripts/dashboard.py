#!/usr/bin/env python3
"""Build or serve a private application dashboard using only the standard library.

The server is bound to 127.0.0.1 and exposes only the rendered page and a narrow
display projection. It never serves a directory or the underlying profile file.
"""
import argparse
import base64
import copy
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


DEFAULT_FILE = "~/Documents/Codex/job-applications/profile.json"
TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "dashboard.html"
MAX_PROFILE_BYTES = 20 * 1024 * 1024
STATUS_LABELS = {
    "draft": "填写中", "ready": "待确认", "awaiting_confirmation": "待确认",
    "submitting": "提交中", "submitted": "已提交", "uncertain": "待核实", "blocked": "需处理",
}
SECRET_WORDS = (
    "password", "passwd", "passphrase", "token", "cookie", "otp", "secret",
    "verificationcode", "onetimecode", "smscode", "authcode", "apikey",
    "privatekey", "recoverycode", "验证码", "密码", "口令", "密钥",
)


class DashboardError(Exception):
    """A user-facing error that does not include profile contents."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def credential_name(value):
    normalized = re.sub(r"[^\w]", "", str(value).casefold()).replace("_", "")
    return any(word in normalized for word in SECRET_WORDS)


def public_value(value):
    """Suppress credential-shaped fields even in older or hand-edited profiles.

    This is defense in depth for field names, not secret detection in prose.
    The skill must never place authentication information in review text.
    """
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items() if not credential_name(key)}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def display_text(value):
    value = public_value(value)
    if isinstance(value, str):
        return value
    if value is None:
        return "未填写"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return str(value)


def safe_url(value):
    """Keep only ordinary HTTP(S) recruiting links without embedded credentials."""
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return ""
    if "\\" in value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        # Accessing port also validates malformed/non-numeric ports.
        parsed.port
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                 if not credential_name(key)]
        # Some recruiting sites use #/jobs/... routes; retain those working links.
        fragment = "" if credential_name(unquote(parsed.fragment)) else parsed.fragment
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, urlencode(query), fragment))
    except (ValueError, UnicodeError):
        return ""


def require(condition, message="资料格式不正确，请检查资料文件；现有记录没有被覆盖。"):
    if not condition:
        raise DashboardError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def confirmation_state(application):
    review = application.get("review")
    confirmation = application.get("confirmation")
    if application.get("status") == "submitted" and not isinstance(review, dict):
        return {"state": "not_recorded", "label": "此记录未保存填写确认信息", "confirmed_at": ""}
    if not isinstance(review, dict) or not isinstance(confirmation, dict):
        return {"state": "pending", "label": "尚未确认", "confirmed_at": ""}
    digest = hashlib.sha256(canonical(review).encode("utf-8")).hexdigest()
    confirmed = (digest == application.get("review_hash") == confirmation.get("review_hash")
                 and nonempty(confirmation.get("confirmed_at"))
                 and nonempty(confirmation.get("reference")))
    if confirmed:
        return {"state": "confirmed", "label": "已确认当前填写内容",
                "confirmed_at": confirmation["confirmed_at"]}
    return {"state": "outdated", "label": "填写内容已变更或确认记录不完整，需重新确认", "confirmed_at": ""}


def application_projection(application):
    require(isinstance(application, dict))
    require(nonempty(application.get("company")) and nonempty(application.get("job_id")))
    require(isinstance(application.get("status"), str) and application["status"] in STATUS_LABELS)
    if application["status"] == "submitted":
        require(nonempty(application.get("evidence")), "已提交的申请缺少成功证据，请先核实网站状态并补充回执摘要。")
    for key in ("job_title", "updated_at", "recruiting_url", "url", "application_id", "evidence"):
        require(key not in application or isinstance(application[key], str))
    review = application.get("review", {})
    require(isinstance(review, dict))
    require(isinstance(review.get("fields", []), list))
    require("notes" not in review or isinstance(review["notes"], str))
    fields = []
    hidden_fields = 0
    for field in review.get("fields", []):
        require(isinstance(field, dict) and nonempty(field.get("label")) and "value" in field)
        require("adaptation" not in field or isinstance(field["adaptation"], str))
        if credential_name(field["label"]):
            hidden_fields += 1
            continue
        fields.append({"label": field["label"], "value": display_text(field["value"]),
                       "adaptation": field.get("adaptation", "")})
    confirmation = confirmation_state(application)
    status = application["status"]
    label = STATUS_LABELS[status]
    if status == "submitted":
        group = "submitted"
    elif status == "awaiting_confirmation" or (status == "ready" and confirmation["state"] != "confirmed"):
        group = "awaiting_confirmation"
    elif status in ("blocked", "uncertain"):
        group = "needs_attention"
    else:
        group = "in_progress"
        if status == "ready":
            label = "已确认，待提交"
    return {
        "company": application["company"], "job_id": application["job_id"],
        "job_title": application.get("job_title") or application["job_id"],
        "status": status, "status_label": label, "status_group": group,
        "updated_at": application.get("updated_at", ""),
        "recruiting_url": safe_url(application.get("recruiting_url")),
        "url": safe_url(application.get("url")),
        "application_id": application.get("application_id", ""),
        "evidence": application.get("evidence", ""), "notes": review.get("notes", ""),
        "fields": fields, "hidden_fields": hidden_fields, "confirmation": confirmation,
    }


def empty_stats():
    return {"submitted": 0, "awaiting_confirmation": 0, "in_progress": 0, "needs_attention": 0}


def reject_constant(_):
    raise DashboardError("资料包含无效的 JSON 数值，请修复后重试。")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "资料包含重复的 JSON 字段，请修复后重试。")
        result[key] = value
    return result


def read_projection(path):
    """Read without changing the profile and return only fields intended for display."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {"state": "uninitialized", "message": "资料尚未初始化。开始第一次申请并保存记录后，这里会显示投递进度。",
                "applications": [], "stats": empty_stats()}
    except OSError:
        raise DashboardError("无法打开资料文件，请检查文件权限和路径；不支持符号链接。") from None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode), "资料路径必须是普通文件。")
            require(info.st_size <= MAX_PROFILE_BYTES, "资料文件过大，暂时无法加载。")
            raw = stream.read(MAX_PROFILE_BYTES + 1)
            require(len(raw.encode("utf-8")) <= MAX_PROFILE_BYTES, "资料文件过大，暂时无法加载。")
        data = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
        require(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1)
        require(isinstance(data.get("facts"), list) and isinstance(data.get("applications"), list))
        applications = [application_projection(app) for app in data["applications"]]
    except (OSError, UnicodeError):
        raise DashboardError("读取资料失败，请检查文件权限和 UTF-8 编码。") from None
    except (ValueError, RecursionError, TypeError):
        raise DashboardError("资料不是有效的 JSON 或结构不正确，请修复后重试。") from None
    applications.sort(key=lambda app: app["updated_at"], reverse=True)
    stats = empty_stats()
    for application in applications:
        stats[application["status_group"]] += 1
    return {"state": "ok", "message": "", "applications": applications, "stats": stats}


def safe_json(value):
    # JSON inside a non-executable script element must still escape its HTML end tag.
    return canonical(value).replace("&", "\\u0026").replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_page(payload):
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise DashboardError("找不到网页模板，请检查 Skill 安装是否完整。") from None
    require(template.count("__DASHBOARD_DATA__") == 1 and template.count("__DASHBOARD_CSP__") == 1,
            "网页模板不完整。")
    page = template.replace("__DASHBOARD_DATA__", safe_json(payload))
    def hashes(tag):
        return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii") + "'"
                        for content in re.findall(r"<" + tag + r"\b[^>]*>(.*?)</" + tag + r">", page, re.DOTALL))
    policy = ("default-src 'none'; script-src " + hashes("script") + "; style-src " + hashes("style")
              + "; connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'")
    return page.replace("__DASHBOARD_CSP__", policy)


def private_directory(directory):
    """Create a dedicated private output directory; do not chmod existing folders."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(),
            "网页输出目录必须是当前用户拥有的独立目录，且不能是符号链接。")
    require(stat.S_IMODE(info.st_mode) == 0o700,
            "网页输出目录需要 0700 权限。请使用专用的私有目录；现有目录权限未被修改。")


def write_page(path, page):
    private_directory(path.parent)
    require(not path.is_symlink(), "网页输出文件不能是符号链接。")
    if path.exists():
        require(path.is_file(), "网页输出路径必须是普通文件。")
    fd, name = tempfile.mkstemp(prefix=".dashboard-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(page)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class DashboardState:
    def __init__(self, profile_path, output_path, refresh_seconds=5):
        self.profile_path = Path(profile_path)
        self.output_path = Path(output_path)
        require(self.profile_path.resolve() != self.output_path.resolve(),
                "网页输出不能覆盖资料文件，请使用不同的 --output 路径。")
        self.refresh_seconds = refresh_seconds
        self.payload = None
        self.page = ""
        self.last_success = None
        self.next_refresh = 0
        self.last_error = ""

    def refresh(self, force=False, strict=False):
        if not force and time.monotonic() < self.next_refresh:
            return
        self.next_refresh = time.monotonic() + self.refresh_seconds
        try:
            projected = read_projection(self.profile_path)
            if projected["state"] == "uninitialized" and self.last_success is not None:
                raise DashboardError("资料文件暂时不存在，请检查它是否被移动或删除。")
        except DashboardError as exc:
            self.last_error = str(exc)
            if strict:
                raise
            projected = copy.deepcopy(self.last_success) if self.last_success is not None else {
                "applications": [], "stats": empty_stats(), "generated_at": ""}
            projected.update(state="error", stale=self.last_success is not None, message=str(exc))
        else:
            self.last_error = ""
            projected["stale"] = False
            previous = self.payload or {}
            unchanged = all(projected.get(key) == previous.get(key)
                            for key in ("state", "message", "applications", "stats", "stale"))
            projected["generated_at"] = previous.get("generated_at", now()) if unchanged else now()
            if projected["state"] == "ok":
                self.last_success = copy.deepcopy(projected)
        if projected != self.payload:
            page = render_page(projected)
            # A failed disk write does not replace the last good file or cached page.
            write_page(self.output_path, page)
            self.payload, self.page = projected, page


class DashboardServer(HTTPServer):
    def __init__(self, address, state):
        require(address[0] == "127.0.0.1", "网页服务只能绑定 127.0.0.1。")
        self.dashboard = state
        super().__init__(address, DashboardHandler)

    def service_actions(self):
        try:
            self.dashboard.refresh()
        except (DashboardError, OSError) as exc:
            # Disk failures must be visible, while the last usable page stays intact.
            message = str(exc) if isinstance(exc, DashboardError) else "无法保存网页，请检查输出目录权限。"
            if message != self.dashboard.last_error:
                print(message, file=sys.stderr, flush=True)
            self.dashboard.last_error = message


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "LocalDashboard"
    sys_version = ""

    def log_message(self, format, *args):
        # Request paths may contain personal information; do not log them.
        pass

    def do_GET(self):
        self.respond(False)

    def do_HEAD(self):
        self.respond(True)

    def respond(self, head_only):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed_hosts.update(("127.0.0.1", "localhost"))
        if self.headers.get("Host", "").lower() not in allowed_hosts:
            self.send_payload(403, "text/plain; charset=utf-8", "此服务仅供本机访问。".encode("utf-8"), head_only)
            return
        try:
            path = urlsplit(self.path).path
        except ValueError:
            path = ""
        state = self.server.dashboard
        if path not in ("/", "/dashboard.html", "/api/applications"):
            self.send_payload(404, "text/plain; charset=utf-8", b"Not found", head_only)
            return
        try:
            state.refresh()
        except (DashboardError, OSError) as exc:
            message = str(exc) if isinstance(exc, DashboardError) else "无法保存网页，请检查输出目录权限。"
            state.last_error = message
        payload = state.payload
        if state.last_error and payload is not None and payload.get("state") != "error":
            payload = copy.deepcopy(payload)
            payload.update(state="error", stale=True, message=state.last_error)
        if payload is None:
            self.send_payload(503, "text/plain; charset=utf-8", "网页暂时无法加载，请查看终端错误。".encode("utf-8"), head_only)
        elif path == "/api/applications":
            self.send_payload(200, "application/json; charset=utf-8", canonical(payload).encode("utf-8"), head_only)
        else:
            page = render_page(payload) if payload is not state.payload else state.page
            self.send_payload(200, "text/html; charset=utf-8", page.encode("utf-8"), head_only)

    def send_payload(self, code, content_type, body, head_only):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


def parser():
    result = argparse.ArgumentParser(description="生成或打开仅在本机运行的投递记录网页。")
    # Options are accepted before or after the command, for convenient copy/paste.
    result.add_argument("command", nargs="?", choices=("build", "serve"), default="build")
    result.add_argument("--file", default=DEFAULT_FILE, help="本地资料 JSON 文件")
    result.add_argument("--output", help="网页输出文件；默认与资料同目录的 dashboard.html")
    result.add_argument("--serve", action="store_true", help="等同于 serve 子命令")
    result.add_argument("--port", type=int, default=8765, help="本机服务端口，默认 8765")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    profile_path = Path(args.file).expanduser().absolute()
    output_path = Path(args.output).expanduser().absolute() if args.output else profile_path.with_name("dashboard.html")
    try:
        require(0 <= args.port <= 65535, "端口必须在 0 到 65535 之间。")
        state = DashboardState(profile_path, output_path)
        serving = args.command == "serve" or args.serve
        state.refresh(force=True, strict=not serving)
        if serving:
            with DashboardServer(("127.0.0.1", args.port), state) as server:
                port = server.server_address[1]
                print(json.dumps({"status": "serving", "url": f"http://127.0.0.1:{port}/",
                                  "output": str(output_path), "state": state.payload["state"]}, ensure_ascii=False), flush=True)
                if state.last_error:
                    print(state.last_error, file=sys.stderr, flush=True)
                server.serve_forever(poll_interval=1)
        else:
            print(json.dumps({"status": "built", "state": state.payload["state"], "output": str(output_path)}, ensure_ascii=False))
    except KeyboardInterrupt:
        return 0
    except DashboardError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"status": "error", "message": "无法写入网页或启动本机服务，请检查目录权限及端口占用。"}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
