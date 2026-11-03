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

    def test_prepare_signup_form_accepts_email_first_variant(self):
        page = mock.Mock()
        email_input = mock.Mock()
        log = mock.Mock()
        cancel = mock.Mock(return_value=False)

        with (
            mock.patch.object(flow, "_find", side_effect=[None, email_input]),
            mock.patch.object(flow, "_click") as click,
            mock.patch.object(flow, "_wait_for") as wait_for,
        ):
            flow._prepare_signup_form(page, log, cancel)

        page.goto.assert_called_once_with(
            flow.SIGNUP_URL,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        click.assert_not_called()
        wait_for.assert_not_called()
        log.assert_any_call("[*] 已识别 X.com 邮箱优先注册页")

    def test_next_uses_exact_continue_selector(self):
        with mock.patch.object(flow, "_click", return_value=True) as click:
            self.assertTrue(flow._next(object(), cancel=lambda: False))

        selectors = click.call_args.args[1]
        self.assertIn("button:text-is('Continue')", selectors)
        self.assertNotIn("button:has-text('Continue')", selectors)

    def test_email_first_variant_submits_email_before_profile(self):
        page = mock.Mock()
        email_input = mock.Mock()
        log = mock.Mock()
        cancel = mock.Mock(return_value=False)

        with (
            mock.patch.object(flow, "_raw_page", return_value=page),
            mock.patch.object(
                flow,
                "_new_available_email",
                return_value=("person@example.com", "mail-token"),
            ),
            mock.patch.object(
                flow,
                "build_profile",
                return_value=("Test", "Person", "Strong-password-1"),
            ),
            mock.patch.object(
                flow,
                "_find",
                side_effect=[None, email_input],
            ),
            mock.patch.object(flow, "_fill") as fill,
            mock.patch.object(flow, "_next", return_value=True) as next_step,
            mock.patch.object(flow, "_wait_for_email_verification_step") as wait_step,
        ):
            email, token, _submitted_at = flow.fill_email_and_submit(
                log_callback=log,
                cancel_callback=cancel,
            )

        self.assertEqual((email, token), ("person@example.com", "mail-token"))
        fill.assert_called_once_with(email_input, "person@example.com")
        next_step.assert_called_once_with(page, cancel=cancel)
        wait_step.assert_called_once()
        self.assertEqual(wait_step.call_args.args[1], "person@example.com")


if __name__ == "__main__":
    unittest.main()
