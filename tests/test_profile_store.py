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

    def review(self, value="Sample final answer"):
        return {"fields": [{"label": "Availability", "value": value,
                            "source": "User answer in fixture", "adaptation": "Matched the form date format"}],
                "notes": "Actual final form contents in a fixture"}

    def confirm(self, review_hash, **extra):
        identity = {k: self.app()[k] for k in ("company", "job_id", "account")}
        return self.cli("confirm-application", value={**identity, "review_hash": review_hash,
                        "reference": "Explicit user confirmation in fixture conversation", **extra})

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
        review_hash = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))["review_hash"]
        self.confirm(review_hash)
        self.cli("record-application", value=self.app("submitted"), code=2)
        self.cli("record-application", value=self.app("submitted", evidence="Fixture confirmation reference"))
        apps = self.cli("list-applications", "--company", "Example Company", "--job-id", "example-job")["applications"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["created_at"], created)
        self.assertEqual(apps[0]["status"], "submitted")
        self.assertEqual(self.cli("list-applications", "--company", "Unrelated")["applications"], [])

    def test_submission_requires_confirmation_even_when_status_is_ready(self):
        self.cli("record-application", value=self.app("ready"))
        for status in ("submitting", "submitted"):
            self.cli("record-application", value=self.app(status, evidence="Fixture evidence"), code=2)
        result = self.cli("record-application", value=self.app("ready", review=self.review()))
        self.assertEqual(result["application_status"], "awaiting_confirmation")
        before = self.file.read_bytes()
        for status in ("submitting", "submitted"):
            self.cli("record-application", value=self.app(status, evidence="Fixture evidence"), code=2)
            self.assertEqual(self.file.read_bytes(), before)
        self.confirm(result["review_hash"])
        self.assertEqual(self.cli("record-application", value=self.app("submitting"))["application_status"], "submitting")

    def test_awaiting_confirmation_requires_actual_review_fields(self):
        for extra in ({}, {"review": {"fields": []}}, {"review": {"fields": [{"label": "Name"}]}},
                      {"review": {"fields": [{"label": "", "value": "Example"}]}}):
            self.cli("record-application", value=self.app("awaiting_confirmation", **extra), code=2)
        result = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))
        self.assertEqual(len(result["review_hash"]), 64)
        self.assertNotIn("Sample final answer", json.dumps(result))
        self.assertEqual(self.stored()["applications"][0]["review"], self.review())

    def test_record_rejects_forged_confirmation_and_input_hash(self):
        result = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))
        before = self.file.read_bytes()
        forged = {"review_hash": result["review_hash"], "reference": "Not from confirm command",
                  "confirmed_at": "2030-01-01T00:00:00Z"}
        for extra in ({"confirmation": forged}, {"review_hash": result["review_hash"]}):
            self.cli("record-application", value=self.app("ready", **extra), code=2)
            self.assertEqual(self.file.read_bytes(), before)

    def test_stale_confirmation_hash_does_not_mutate_data(self):
        old_hash = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))["review_hash"]
        new_hash = self.cli("record-application", value=self.app("draft", review=self.review("Changed final answer")))["review_hash"]
        self.assertNotEqual(new_hash, old_hash)
        before = self.file.read_bytes()
        identity = {k: self.app()[k] for k in ("company", "job_id", "account")}
        result = self.cli("confirm-application", value={**identity, "review_hash": old_hash,
                          "reference": "Confirmation of the earlier version"}, code=3)
        self.assertEqual(result["status"], "stale_review")
        self.assertEqual(self.file.read_bytes(), before)
        self.confirm(new_hash)
        confirmation = self.stored()["applications"][0]["confirmation"]
        self.assertEqual(confirmation["review_hash"], new_hash)
        self.assertTrue(confirmation["confirmed_at"].endswith("Z"))

    def test_changed_review_clears_confirmation_and_requires_another_confirmation(self):
        old_hash = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))["review_hash"]
        self.confirm(old_hash)
        before = self.file.read_bytes()
        self.cli("record-application", value=self.app("submitting", review=self.review("Changed final answer")), code=2)
        self.assertEqual(self.file.read_bytes(), before)
        result = self.cli("record-application", value=self.app("ready", review=self.review("Changed final answer")))
        self.assertEqual(result["application_status"], "awaiting_confirmation")
        self.assertNotIn("confirmation", self.stored()["applications"][0])
        self.cli("record-application", value=self.app("submitting"), code=2)
        self.confirm(result["review_hash"])
        self.cli("record-application", value=self.app("submitting"))

    def test_partial_application_updates_preserve_review_confirmation_and_metadata(self):
        result = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review(),
                          recruiting_url="https://example.com/careers", context_note="Preserve this fixture metadata"))
        self.confirm(result["review_hash"])
        original = self.stored()["applications"][0]
        identity = {k: self.app()[k] for k in ("company", "job_id", "account")}
        self.cli("record-application", value={**identity, "status": "blocked"})
        updated = self.stored()["applications"][0]
        for key in ("review", "review_hash", "confirmation", "recruiting_url", "context_note", "created_at", "resume_ref", "url"):
            self.assertEqual(updated[key], original[key])
        self.cli("record-application", value={**identity, "review": self.review(), "status": "ready"})
        self.assertEqual(self.stored()["applications"][0]["confirmation"], original["confirmation"])

    def test_changing_attachment_or_job_url_requires_a_different_review(self):
        result = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))
        self.confirm(result["review_hash"])
        identity = {k: self.app()[k] for k in ("company", "job_id", "account")}
        before = self.file.read_bytes()
        for change in ({"resume_ref": "revised-fixture-resume.pdf"}, {"url": "https://example.com/jobs/revised-target"}):
            for extra in ({}, {"review": self.review()}):
                self.cli("record-application", value={**identity, **change, **extra}, code=2)
                self.assertEqual(self.file.read_bytes(), before)

    def test_changing_attachment_with_new_review_invalidates_prior_confirmation(self):
        result = self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))
        self.confirm(result["review_hash"])
        new_review = self.review()
        new_review["fields"].append({"label": "Uploaded resume version", "value": "revised-fixture-resume.pdf"})
        updated = self.cli("record-application", value=self.app("ready", resume_ref="revised-fixture-resume.pdf", review=new_review))
        self.assertNotEqual(updated["review_hash"], result["review_hash"])
        self.assertEqual(updated["application_status"], "awaiting_confirmation")
        self.assertNotIn("confirmation", self.stored()["applications"][0])
        identity = {k: self.app()[k] for k in ("company", "job_id", "account")}
        stale = self.cli("confirm-application", value={**identity, "review_hash": result["review_hash"],
                         "reference": "Prior attachment confirmation"}, code=3)
        self.assertEqual(stale["status"], "stale_review")

    def test_legacy_submitted_records_remain_readable_and_preserved(self):
        self.cli("init")
        document = self.stored()
        legacy = self.app("submitted", evidence="Legacy fixture evidence", created_at="2020-01-01T00:00:00Z",
                          updated_at="2020-01-01T00:00:00Z")
        document["applications"].append(legacy)
        self.file.write_text(json.dumps(document))
        result = self.cli("list-applications")
        self.assertEqual(result["applications"], [legacy])
        self.assertEqual(result["warnings"][0]["code"], "legacy_submitted_without_review")
        self.cli("put-fact", value=self.fact())
        self.assertEqual(self.stored()["applications"], [legacy])
        before = self.file.read_bytes()
        self.cli("record-application", value=self.app("submitted", evidence="New evidence cannot bypass confirmation"), code=2)
        self.assertEqual(self.file.read_bytes(), before)

    def test_tampered_saved_review_hash_is_rejected_without_overwriting(self):
        self.cli("record-application", value=self.app("awaiting_confirmation", review=self.review()))
        document = self.stored()
        document["applications"][0]["review"]["fields"][0]["value"] = "Tampered answer"
        self.file.write_text(json.dumps(document))
        before = self.file.read_bytes()
        self.cli("list-applications", code=2)
        self.cli("put-fact", value=self.fact(), code=2)
        self.assertEqual(self.file.read_bytes(), before)

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
        self.cli("record-application", value=self.app(status=[], review=self.review()), code=2)

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
