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
            mock.patch.object(flow, "_find", return_value=None),
            mock.patch.object(flow, "_find_email_first_input", return_value=email_input),
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
        with (
            mock.patch.object(flow, "_find_exact_text_button", return_value=None),
            mock.patch.object(flow, "_click", return_value=True) as click,
        ):
            self.assertTrue(flow._next(object(), cancel=lambda: False))

        selectors = click.call_args.args[1]
        self.assertIn("button:text-is('Continue')", selectors)
        self.assertNotIn("button:has-text('Continue')", selectors)

    def test_next_clicks_only_exact_rendered_continue_button(self):
        page = mock.Mock()
        exact_button = mock.Mock()

        with (
            mock.patch.object(
                flow, "_find_exact_text_button", return_value=exact_button
            ) as find_exact,
            mock.patch.object(flow, "_click") as fallback_click,
        ):
            self.assertTrue(flow._next(page, cancel=lambda: False))

        find_exact.assert_called_once_with(
            page,
            ("Continue", "继续", "Next", "下一步"),
        )
        exact_button.click.assert_called_once_with(timeout=10_000)
        fallback_click.assert_not_called()

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
                flow, "_find", return_value=None
            ),
            mock.patch.object(flow, "_find_email_first_input", return_value=email_input),
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

    def test_find_searches_child_frames(self):
        page = mock.Mock()
        main_frame = mock.Mock()
        child_frame = mock.Mock()
        page.main_frame = main_frame
        page.frames = [main_frame, child_frame]

        missing = mock.Mock()
        missing.count.return_value = 0
        page.locator.return_value = missing
        found = mock.Mock()
        found.count.return_value = 1
        found.first.is_visible.return_value = True
        child_frame.locator.return_value = found

        result = flow._find(page, ("input[name='text']",))

        self.assertIs(result, found.first)
        child_frame.locator.assert_called_once_with("input[name='text']")

    def test_email_first_fallback_uses_accessible_label(self):
        page = mock.Mock()
        page.frames = []
        missing = mock.Mock()
        missing.count.return_value = 0
        page.locator.return_value = missing
        accessible = mock.Mock()
        accessible.count.return_value = 1
        accessible.first.is_visible.return_value = True
        page.get_by_label.return_value = accessible

        result = flow._find_email_first_input(page)

        self.assertIs(result, accessible.first)
        page.get_by_label.assert_called_once()

    def test_timeout_diagnostics_do_not_log_input_values(self):
        page = mock.Mock()
        page.url = "https://x.com/i/flow/signup?token=secret"
        page.frames = []
        page.title.return_value = "Sign up"
        page.evaluate.return_value = "complete"
        empty = mock.Mock()
        empty.count.return_value = 0
        empty.inner_text.return_value = "Email or username person@example.com"
        page.locator.return_value = empty
        log = mock.Mock()
        flow._remember_log_callback(log)

        flow._emit_timeout_diagnostics(page, ("input[name='text']",))

        output = "\n".join(call.args[0] for call in log.call_args_list)
        self.assertIn("X.com 控件等待超时", output)
        self.assertIn("<email>", output)
        self.assertNotIn("person@example.com", output)
        self.assertNotIn("secret", output)


if __name__ == "__main__":
    unittest.main()
