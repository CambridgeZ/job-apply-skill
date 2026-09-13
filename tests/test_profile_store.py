import concurrent.futures
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "profile_store.py"


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "private"
        self.file = self.directory / "profile.json"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, value=None, code=0):
        result = subprocess.run([sys.executable, str(SCRIPT), "--file", str(self.file), *args],
                                input=json.dumps(value) if value is not None else None,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return json.loads(result.stderr if code == 2 else result.stdout)

    def fact(self, value="sample", scope=None, **extra):
        return {"key": "availability", "value": value, "scope": scope or {},
                "source": {"kind": "user", "reference": "Test fixture only"}, **extra}

    def app(self, status="draft", **extra):
        return {"company": "Example Company", "job_id": "example-job", "account": "test-account-alias",
                "status": status, "url": "https://example.com/jobs/example-job",
                "resume_ref": "fixture-resume.pdf", **extra}

    def stored(self):
        return json.loads(self.file.read_text())

    def test_init_is_private_idempotent_and_preserves_extensions(self):
        self.assertEqual(self.cli("init")["status"], "initialized")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.file) + ".lock").stat().st_mode), 0o600)
        document = self.stored()
        document["custom_metadata"] = {"format_note": "preserve me"}
        self.file.write_text(json.dumps(document))
        self.cli("put-fact", value=self.fact())
        self.assertEqual(self.stored()["custom_metadata"], document["custom_metadata"])
        before = self.file.read_bytes()
        self.assertEqual(self.cli("init")["status"], "existing")
        self.assertEqual(before, self.file.read_bytes())

    def test_scope_specificity_ambiguity_and_expiry(self):
        self.assertEqual(self.cli("lookup", "availability", code=4)["status"], "missing")
        self.cli("put-fact", value=self.fact("general"))
        self.cli("put-fact", value=self.fact("company-value", {"company": "Example"}))
        self.cli("put-fact", value=self.fact("country-value", {"country": "Example Country"}))
        self.assertEqual(self.cli("lookup", "availability")["value"], "general")
        self.assertEqual(self.cli("lookup", "availability", "--context", '{"company":"Example"}')["value"], "company-value")
        context = '{"company":"Example","country":"Example Country"}'
        ambiguous = self.cli("lookup", "availability", "--context", context, code=6)
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertNotIn("value", ambiguous)
        self.cli("put-fact", value=self.fact("specific", {"company": "Example", "country": "Example Country"}))
        self.assertEqual(self.cli("lookup", "availability", "--context", context)["value"], "specific")
        self.cli("put-fact", "--replace", value=self.fact("old", {"company": "Example", "country": "Example Country"},
                                                          valid_until="2000-01-01T00:00:00Z"))
        self.assertEqual(self.cli("lookup", "availability", "--context", context, code=5)["status"], "expired")
        self.cli("forget", "availability")
        self.cli("put-fact", value=self.fact("old", valid_until="2000-01-01T00:00:00Z"))
        self.assertEqual(self.cli("lookup", "availability", code=5)["status"], "expired")

    def test_equal_specificity_same_answer_is_known(self):
        self.cli("put-fact", value=self.fact("same", {"company": "Example"}))
        self.cli("put-fact", value=self.fact("same", {"country": "Example Country"}))
        result = self.cli("lookup", "availability", "--context", '{"company":"Example","country":"Example Country"}')
        self.assertEqual(result["status"], "known")
        self.assertEqual(len(result["matches"]), 2)

    def test_conflict_preserves_data_until_explicit_replace(self):
        result = self.cli("put-fact", value=self.fact("private-value-1"))
        self.assertNotIn("private-value-1", json.dumps(result))
        before = self.file.read_bytes()
        self.assertEqual(self.cli("put-fact", value=self.fact("private-value-2"), code=3)["status"], "conflict")
        self.assertEqual(self.file.read_bytes(), before)
        self.cli("put-fact", "--replace", value=self.fact("private-value-2"))
        self.assertEqual(self.cli("lookup", "availability")["value"], "private-value-2")
        self.assertEqual(len(self.stored()["facts"]), 1)

    def test_blank_placeholder_values_are_rejected_without_overwriting(self):
        self.cli("put-fact", value=self.fact("existing answer"))
        before = self.file.read_bytes()
        for placeholder in (None, "", "   \n\t", [], {}):
            self.cli("put-fact", "--replace", value=self.fact(placeholder), code=2)
            self.assertEqual(self.file.read_bytes(), before)

    def test_false_and_zero_are_valid_answers(self):
        for answer in (False, 0):
            self.cli("put-fact", "--replace", value=self.fact(answer))
            result = self.cli("lookup", "availability")
            self.assertEqual(result["status"], "known")
            self.assertIs(type(result["value"]), type(answer))
            self.assertEqual(result["value"], answer)

    def test_same_value_reimport_preserves_user_source_and_omitted_metadata(self):
        original = self.fact("same answer", valid_until="2030-01-01T00:00:00Z", context_note="Keep this metadata")
        self.cli("put-fact", value=original)
        imported = self.fact("same answer", source={"kind": "resume", "reference": "Fixture resume"})
        self.cli("put-fact", value=imported)
        result = self.stored()["facts"][0]
        self.assertEqual(result["source"], original["source"])
        self.assertEqual(result["valid_until"], original["valid_until"])
        self.assertEqual(result["context_note"], original["context_note"])

    def test_explicit_replace_can_change_source_and_remove_expiry(self):
        self.cli("put-fact", value=self.fact("same answer", valid_until="2030-01-01T00:00:00Z"))
        replacement = self.fact("same answer", source={"kind": "resume", "reference": "Explicit replacement fixture"})
        self.cli("put-fact", "--replace", value=replacement)
        result = self.stored()["facts"][0]
        self.assertEqual(result["source"], replacement["source"])
        self.assertNotIn("valid_until", result)

    def test_expired_job_scope_does_not_fall_back_to_live_global_answer(self):
        self.cli("put-fact", value=self.fact("global preference"))
        self.cli("put-fact", value=self.fact("old job preference", {"job_id": "example-job"},
                                             valid_until="2000-01-01T00:00:00Z"))
        result = self.cli("lookup", "availability", "--context", '{"job_id":"example-job"}', code=5)
        self.assertEqual(result["status"], "expired")
        self.assertNotIn("value", result)
        self.assertEqual(self.cli("lookup", "availability")["value"], "global preference")

    def test_credential_field_rejection_including_nested_names(self):
        for key in ("password", "otp", "access_token", "sessionCookie", "验证码", "verification_code"):
            bad = self.fact()
            bad["key"] = key
            self.assertEqual(self.cli("put-fact", value=bad, code=2)["status"], "error")
        self.cli("put-fact", value=self.fact({"safe": {"refresh_token": "not-real"}}), code=2)
        self.assertFalse(self.file.exists())

    def test_submitted_requires_evidence_and_application_updates_deduplicate(self):
        self.cli("record-application", value=self.app("submitted"), code=2)
        result = self.cli("record-application", value=self.app())
        self.assertEqual(set(result), {"status", "application_status"})
        created = self.stored()["applications"][0]["created_at"]
        self.cli("record-application", value=self.app("submitted", evidence="Fixture confirmation reference"))
        apps = self.cli("list-applications", "--company", "Example Company", "--job-id", "example-job")["applications"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["created_at"], created)
        self.assertEqual(apps[0]["status"], "submitted")
        self.assertEqual(self.cli("list-applications", "--company", "Unrelated")["applications"], [])

    def test_forget_exact_scope(self):
        self.cli("put-fact", value=self.fact("general"))
        self.cli("put-fact", value=self.fact("specific", {"company": "Example"}))
        self.assertEqual(self.cli("forget", "availability", "--scope", '{"company":"Example"}')["removed"], 1)
        self.assertEqual(self.cli("lookup", "availability", "--context", '{"company":"Example"}')["value"], "general")

    def test_corrupt_or_wrong_schema_never_overwritten(self):
        self.cli("init")
        for raw in (b'{broken', b'{"schema_version":1,"facts":{},"applications":[]}',
                    b'{"schema_version":1,"schema_version":1,"facts":[],"applications":[]}',
                    b'{"schema_version":true,"facts":[],"applications":[]}',
                    b'{"schema_version":1,"facts":[],"applications":[],"x":NaN}'):
            self.file.write_bytes(raw)
            self.cli("put-fact", value=self.fact(), code=2)
            self.assertEqual(self.file.read_bytes(), raw)

    def test_malformed_facts_and_application_are_reported_as_errors(self):
        invalid_facts = [None, [], {"key": "x"}, self.fact(source={"kind": [], "reference": "fixture"}),
                         self.fact(scope={"country": 1}), self.fact(valid_until="tomorrow"),
                         self.fact(valid_until="2030-01-01T00:00:00")]
        # JSON null through the CLI still means an explicit stdin object is absent/invalid.
        for fact in invalid_facts:
            self.cli("put-fact", value=fact, code=2)
        self.cli("record-application", value=self.app(status=[]), code=2)

    def test_concurrent_writers_keep_every_record(self):
        def write(index):
            fact = self.fact(str(index))
            fact["key"] = f"fixture_{index}"
            return subprocess.run([sys.executable, str(SCRIPT), "--file", str(self.file), "put-fact"],
                                  input=json.dumps(fact), text=True, capture_output=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write, range(20)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(len(self.stored()["facts"]), 20)
        self.assertEqual(list(self.directory.glob(".profile-*.tmp")), [])

    def test_insecure_existing_directory_is_not_chmodded(self):
        self.directory.mkdir(mode=0o755)
        os.chmod(self.directory, 0o755)
        self.cli("init", code=2)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)
        self.assertFalse(self.file.exists())

    def test_profile_symlink_is_not_followed(self):
        self.cli("init")
        outside = Path(self.temp.name) / "outside.json"
        original = self.file.read_bytes()
        outside.write_bytes(original)
        self.file.unlink()
        self.file.symlink_to(outside)
        self.cli("put-fact", value=self.fact(), code=2)
        self.assertEqual(outside.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
