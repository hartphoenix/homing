import importlib.util
import os
import unittest


SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package", "scripts", "homing.py")
spec = importlib.util.spec_from_file_location("homing_source_review_client", SCRIPT)
homing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(homing)


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
REVIEW_ID = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-08-19T12:00:00+00:00"


def review(status="open"):
    return {
        "id": REVIEW_ID,
        "project_id": PROJECT_ID,
        "status": status,
        "observed_prompt_revision": 12,
        "resolved_prompt_revision": 12 if status == "resolved" else None,
        "opened_at": STAMP,
        "last_reported_at": STAMP,
        "resolved_at": STAMP if status == "resolved" else None,
    }


class SourceReviewClientValidationTests(unittest.TestCase):
    def test_revision_does_not_coerce_bool_float_or_oversized_values(self):
        for value in (True, False, 1.0, "12", 2147483648):
            with self.subTest(value=value), self.assertRaises(SystemExit) as raised:
                homing.source_review_revision(value)
            self.assertEqual(raised.exception.code, homing.EXIT_VALIDATION)

    def test_review_response_is_closed_and_bounded(self):
        self.assertEqual(homing.validate_source_review(review())["status"], "open")
        with self.assertRaises(SystemExit):
            homing.validate_source_review(dict(review(), agent_text="do something"))
        with self.assertRaises(SystemExit):
            homing.validate_source_review(dict(review(), resolved_prompt_revision=12))
        with self.assertRaises(SystemExit):
            homing.validate_source_review_list({"items": [review()] * 101})

    def test_cli_has_list_report_and_resolve_commands_without_free_text(self):
        parser = homing.build_parser()
        self.assertEqual(parser.parse_args(["source-review-list"]).command, "source-review-list")
        report = parser.parse_args(["source-review-report", "--project", PROJECT_ID, "--revision", "12"])
        self.assertEqual(report.prompt_revision, 12)
        resolved = parser.parse_args([
            "source-plan-review-resolve", "--project", PROJECT_ID,
            "--review-id", REVIEW_ID, "--prompt-revision", "12",
        ])
        self.assertEqual(resolved.review, REVIEW_ID)
