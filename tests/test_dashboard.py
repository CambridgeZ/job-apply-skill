import base64
import hashlib
from html.parser import HTMLParser
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dashboard.py"
SPEC = importlib.util.spec_from_file_location("job_apply_dashboard", SCRIPT)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class PageParser(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.initial = ""
        self.in_initial = False
        self.tags = []
        self.csp = ""
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script" and attrs.get("id") == "initial-data":
            self.in_initial = True
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy":
            self.csp = attrs["content"]

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_initial = False

    def handle_data(self, data):
        if self.in_initial:
            self.initial += data


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "private"
        self.file = self.directory / "profile.json"
        self.output = self.directory / "dashboard.html"

    def tearDown(self):
        self.temp.cleanup()

    def application(self, status="draft", **extra):
        return {"company": "示例公司", "job_id": "J123", "job_title": "后端工程师",
                "account": "fixture-private-account", "status": status,
                "url": "https://example.com/jobs/J123", "recruiting_url": "https://example.com/careers",
                "resume_ref": "fixture-private-resume-path.pdf", "created_at": "2026-09-12T01:00:00Z",
                "updated_at": "2026-09-13T01:00:00Z", "evidence": "示例确认页显示已接收" if status == "submitted" else "", **extra}

    def write_profile(self, applications=None):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = {"schema_version": 1, "facts": [{"never_display": "fixture-private-facts"}],
                "applications": applications or []}
        self.file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stderr if code else result.stdout)

    def parsed_page(self, path=None):
        return PageParser((path or self.output).read_text(encoding="utf-8"))

    def confirmed(self, app):
        digest = hashlib.sha256(dashboard.canonical(app["review"]).encode("utf-8")).hexdigest()
        app["review_hash"] = digest
        app["confirmation"] = {"review_hash": digest, "reference": "fixture-private-chat-reference",
                               "confirmed_at": "2026-09-13T01:05:00Z"}
        return app


class DashboardTests(DashboardFixture):
    def test_build_missing_profile_makes_private_uninitialized_page(self):
        result = self.cli("build", "--file", str(self.file))
        self.assertEqual(result["state"], "uninitialized")
        self.assertFalse(self.file.exists())
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        payload = json.loads(self.parsed_page().initial)
        self.assertEqual(payload["applications"], [])
        self.assertIn("资料尚未初始化", payload["message"])
        self.assertEqual(payload["stats"], dashboard.empty_stats())

    def test_initialized_empty_page_is_not_an_error_or_demo_data(self):
        self.write_profile()
        self.cli("--file", str(self.file), "build")
        page = self.output.read_text(encoding="utf-8")
        payload = json.loads(self.parsed_page().initial)
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["applications"], [])
        self.assertIn("还没有投递记录", page)
        self.assertNotIn("示例公司", page)

    def test_display_projection_preserves_chinese_fields_and_hides_other_profile_data(self):
        app = self.application(review={"fields": [{"label": "项目经历", "value": "开发任务系统\n支持失败恢复",
                                                   "source": "fixture-private-source",
                                                   "adaptation": "突出与岗位相关的服务端实现。"}],
                                       "notes": "选填的个人主页留空"},
                               evidence="确认页显示申请编号 A123", application_id="A123")
        self.write_profile([self.confirmed(app)])
        payload = dashboard.read_projection(self.file)
        public = payload["applications"][0]
        self.assertEqual(public["fields"][0]["value"], "开发任务系统\n支持失败恢复")
        self.assertIn("服务端", public["fields"][0]["adaptation"])
        self.assertEqual(public["confirmation"]["state"], "confirmed")
        self.assertEqual(public["application_id"], "A123")
        self.assertEqual(public["evidence"], "确认页显示申请编号 A123")
        self.assertEqual(public["notes"], "选填的个人主页留空")
        self.assertEqual(public["recruiting_url"], "https://example.com/careers")
        self.assertNotIn("fixture-private-", json.dumps(payload))
        self.assertNotIn("source", public["fields"][0])

    def test_all_status_groups_and_role_fallback(self):
        review = {"fields": [{"label": "姓名", "value": "测试用户"}]}
        applications = [self.application(status=status, job_id=status) for status in dashboard.STATUS_LABELS]
        applications.append(self.confirmed(self.application(status="ready", job_id="confirmed-ready", review=review)))
        del applications[0]["job_title"]
        self.write_profile(applications)
        payload = dashboard.read_projection(self.file)
        self.assertEqual(payload["stats"], {"submitted": 1, "awaiting_confirmation": 2,
                                           "in_progress": 3, "needs_attention": 2})
        by_id = {app["job_id"]: app for app in payload["applications"]}
        self.assertEqual(by_id["draft"]["job_title"], "draft")
        self.assertEqual(by_id["submitting"]["status_label"], "提交中")
        self.assertEqual(by_id["uncertain"]["status_label"], "待核实")
        self.assertEqual(by_id["confirmed-ready"]["status_label"], "已确认，待提交")
        self.assertEqual(by_id["submitted"]["confirmation"]["state"], "not_recorded")
        page = dashboard.render_page({**payload, "generated_at": "", "stale": False})
        parsed = PageParser(page)
        filters = {attrs["data-filter"] for _, attrs in parsed.tags if "data-filter" in attrs}
        self.assertEqual(filters, {"all", *payload["stats"]})
        # The browser searches only the three display fields; no hidden facts are exported for search.
        self.assertIn("[app.company, app.job_title, app.job_id]", page)
        self.assertIn('app.status_group === activeFilter', page)

    def test_confirmation_digest_covers_raw_review_including_source_and_notes(self):
        app = self.confirmed(self.application("ready", review={"fields": [{"label": "经历", "value": "原文",
                                                                             "source": "真实简历"}], "notes": "已核对"}))
        self.assertEqual(dashboard.application_projection(app)["confirmation"]["state"], "confirmed")
        app["review"]["fields"][0]["source"] = "更改后的来源"
        public = dashboard.application_projection(app)
        self.assertEqual(public["confirmation"]["state"], "outdated")
        self.assertEqual(public["status_group"], "awaiting_confirmation")
        app["review_hash"] = app["confirmation"]["review_hash"] = "identical-but-invalid"
        self.assertEqual(dashboard.application_projection(app)["confirmation"]["state"], "outdated")

    def test_submitted_without_success_evidence_is_an_explicit_error(self):
        self.write_profile([self.application("submitted", evidence="")])
        with self.assertRaisesRegex(dashboard.DashboardError, "缺少成功证据"):
            dashboard.read_projection(self.file)

    def test_html_and_script_injection_remain_inert_text(self):
        attack = '</script><img src=x onerror="alert(1)"><script>alert("x")</script>&\u2028\u2029'
        self.write_profile([self.application(company=attack, evidence=attack,
                                            review={"fields": [{"label": "<b>字段</b>", "value": attack,
                                                               "adaptation": attack}], "notes": attack})])
        self.cli("build", "--file", str(self.file))
        page = self.output.read_text(encoding="utf-8")
        parsed = self.parsed_page()
        payload = json.loads(parsed.initial)
        self.assertEqual(payload["applications"][0]["company"], attack)
        self.assertEqual(payload["applications"][0]["fields"][0]["value"], attack)
        self.assertEqual(payload["applications"][0]["evidence"], attack)
        self.assertEqual(payload["applications"][0]["notes"], attack)
        self.assertNotIn(attack, page)
        self.assertEqual(sum(tag == "script" for tag, _ in parsed.tags), 2)
        self.assertFalse(any(tag in ("img", "iframe") for tag, _ in parsed.tags))
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("insertAdjacentHTML", page)
        self.assertNotIn("eval(", page)
        self.assertIn("textContent", page)
        self.assertIn("\\u003c/script\\u003e", parsed.initial)

    def test_csp_hashes_cover_inline_resources_without_external_dependencies(self):
        self.write_profile()
        self.cli("build", "--file", str(self.file))
        page = self.output.read_text(encoding="utf-8")
        parsed = self.parsed_page()
        self.assertIn("default-src 'none'", parsed.csp)
        self.assertNotIn("'unsafe-inline'", parsed.csp)
        for tag in ("script", "style"):
            for content in re.findall(r"<" + tag + r"\b[^>]*>(.*?)</" + tag + r">", page, re.DOTALL):
                digest = base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii")
                self.assertIn("'sha256-" + digest + "'", parsed.csp)
        self.assertFalse(any("src" in attrs or tag == "link" for tag, attrs in parsed.tags))

    def test_urls_reject_script_schemes_credentials_and_remove_secret_queries(self):
        bad = ("javascript:alert(1)", "data:text/html,attack", "file:///tmp/profile.json", "//example.com/jobs",
               "https://user:password@example.com/jobs", "https://example.com\\@evil.test", "https://example.com\n/jobs",
               "https://[broken", "https://example.com:bad/jobs")
        for value in bad:
            self.assertEqual(dashboard.safe_url(value), "", value)
        url = "https://example.com/jobs?id=J123&access_token=fixture-secret&verification_code=123456#access_token=fixture-secret"
        self.assertEqual(dashboard.safe_url(url), "https://example.com/jobs?id=J123")
        self.assertEqual(dashboard.safe_url("https://example.com/#/jobs/J123"), "https://example.com/#/jobs/J123")
        self.write_profile([self.application(url="javascript:alert(1)", recruiting_url="https://example.com/careers")])
        public = dashboard.read_projection(self.file)["applications"][0]
        self.assertEqual(public["url"], "")
        self.assertEqual(public["recruiting_url"], "https://example.com/careers")

    def test_credential_fields_are_omitted_and_nested_secret_names_are_removed(self):
        fields = [{"label": label, "value": "fixture-secret-code"} for label in ("OTP", "登录验证码", "password")]
        fields.append({"label": "其他信息", "value": {"answer": "保留", "password": "fixture-secret-code",
                                                      "nested": [{"refresh_token": "fixture-secret-code", "safe": False}]}})
        self.write_profile([self.application(review={"fields": fields})])
        payload = dashboard.read_projection(self.file)
        public = payload["applications"][0]
        self.assertEqual(public["hidden_fields"], 3)
        self.assertEqual(len(public["fields"]), 1)
        self.assertNotIn("fixture-secret-code", json.dumps(payload))
        self.assertIn("保留", public["fields"][0]["value"])

    def test_custom_output_and_existing_insecure_directory(self):
        self.write_profile([self.application()])
        output = Path(self.temp.name) / "snapshot" / "my-applications.html"
        self.cli("--output", str(output), "build", "--file", str(self.file))
        self.assertTrue(output.exists())
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        unsafe = Path(self.temp.name) / "shared"
        unsafe.mkdir(mode=0o755)
        os.chmod(unsafe, 0o755)
        error = self.cli("build", "--file", str(self.file), "--output", str(unsafe / "dashboard.html"), code=2)
        self.assertIn("0700", error["message"])
        self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o755)

    def test_corrupt_build_preserves_last_page_and_source(self):
        self.write_profile([self.application()])
        self.cli("build", "--file", str(self.file))
        previous = self.output.read_bytes()
        bad_inputs = ('{broken', '{"schema_version":true,"facts":[],"applications":[]}',
                      '{"schema_version":1,"facts":[],"applications":[],"x":NaN}',
                      '{"schema_version":1,"facts":[],"applications":[],"facts":[]}',
                      '{"schema_version":1,"facts":[],"applications":[null]}')
        for raw in bad_inputs:
            self.file.write_text(raw, encoding="utf-8")
            result = self.cli("build", "--file", str(self.file), code=2)
            self.assertEqual(result["status"], "error")
            self.assertEqual(self.output.read_bytes(), previous)
            self.assertEqual(self.file.read_text(encoding="utf-8"), raw)

    def test_profile_and_output_symlinks_are_not_followed_or_overwritten(self):
        self.write_profile([self.application()])
        original = self.file.read_bytes()
        self.cli("build", "--file", str(self.file), "--output", str(self.file), code=2)
        self.assertEqual(self.file.read_bytes(), original)
        self.output.symlink_to(self.file)
        self.cli("build", "--file", str(self.file), code=2)
        self.assertEqual(self.file.read_bytes(), original)
        self.output.unlink()
        symlink = self.directory / "linked-profile.json"
        symlink.symlink_to(self.file)
        self.cli("build", "--file", str(symlink), code=2)
        self.assertEqual(self.file.read_bytes(), original)

    def test_state_keeps_last_good_records_on_corruption_deletion_and_recovers(self):
        self.write_profile([self.application()])
        state = dashboard.DashboardState(self.file, self.output, refresh_seconds=0)
        state.refresh()
        self.file.write_text("{broken", encoding="utf-8")
        state.refresh()
        self.assertEqual(state.payload["state"], "error")
        self.assertTrue(state.payload["stale"])
        self.assertEqual(state.payload["applications"][0]["company"], "示例公司")
        self.assertIn("JSON", state.payload["message"])
        self.file.unlink()
        state.refresh()
        self.assertEqual(state.payload["state"], "error")
        self.assertEqual(len(state.payload["applications"]), 1)
        self.write_profile([self.application(company="更新公司")])
        state.refresh()
        self.assertEqual(state.payload["state"], "ok")
        self.assertFalse(state.payload["stale"])
        self.assertEqual(state.payload["applications"][0]["company"], "更新公司")


class DashboardHTTPTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        self.write_profile([self.application()])
        self.state = dashboard.DashboardState(self.file, self.output, refresh_seconds=0.02)
        self.state.refresh(force=True)
        self.server = dashboard.DashboardServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_http_only_serves_page_and_safe_projection(self):
        for path in ("/", "/dashboard.html"):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("我的投递记录".encode(), body)
            self.assertNotIn(b"fixture-private-", body)
        status, headers, body = self.request("/api/applications")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["applications"]), 1)
        self.assertNotIn(b"fixture-private-", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_http_rejects_arbitrary_files_traversal_and_foreign_host(self):
        (self.directory / "private.txt").write_text("fixture-private-arbitrary-file")
        for path in ("/profile.json", "/private.txt", "/../profile.json", "/%2e%2e/profile.json",
                     "/api/../profile.json", "/assets/dashboard.html", "/scripts/dashboard.py"):
            status, _, body = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"fixture-private", body)
        self.assertEqual(self.request("/", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("/api/applications", method="POST")[0], 501)
        status, _, body = self.request("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        with self.assertRaises(dashboard.DashboardError):
            dashboard.DashboardServer(("0.0.0.0", 0), self.state)

    def test_background_refresh_updates_output_without_browser_request(self):
        self.write_profile([self.application(company="后台更新公司")])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if "后台更新公司" in self.output.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        self.assertIn("后台更新公司", self.output.read_text(encoding="utf-8"))
        payload = json.loads(self.request("/api/applications")[2])
        self.assertEqual(payload["applications"][0]["company"], "后台更新公司")

    def test_http_failure_is_explicit_and_retains_previous_records(self):
        self.file.write_text("{broken", encoding="utf-8")
        self.state.next_refresh = 0
        status, _, body = self.request("/api/applications")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "error")
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["applications"][0]["company"], "示例公司")
        self.assertIn("JSON", payload["message"])


if __name__ == "__main__":
    unittest.main()
