import unittest
from unittest import mock

from backend.x_account_registration import flow


class XAccountRegistrationTests(unittest.TestCase):
    def test_uses_shared_grok_registration_engine(self):
        with mock.patch.object(flow.engine, "run_registration") as run:
            flow.run_registration(3)

        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args, (3,))
        self.assertEqual(kwargs["flow_adapter"].result_platform, "x")
        self.assertEqual(kwargs["flow_adapter"].force_workers, 1)

    def test_optional_click_does_not_swallow_manual_challenge(self):
        with mock.patch.object(
            flow,
            "_wait_for",
            side_effect=flow.ManualChallengeRequired("challenge"),
        ):
            with self.assertRaises(flow.ManualChallengeRequired):
                flow._click(object(), ("button",), required=False)

    def test_challenge_detection_recognizes_arkose_frame(self):
        page = mock.Mock()
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.first.is_visible.return_value = True
        page.locator.return_value = locator

        self.assertIn("人工安全验证", flow._challenge_reason(page))
        page.locator.assert_called_with("iframe[src*='arkose']")

    def test_platform_failures_keep_specific_categories(self):
        self.assertEqual(
            flow.classify_failure(flow.ManualChallengeRequired("challenge")),
            "manual_challenge",
        )
        self.assertEqual(
            flow.classify_failure(flow.PhoneVerificationRequired("phone")),
            "phone_required",
        )


if __name__ == "__main__":
    unittest.main()
