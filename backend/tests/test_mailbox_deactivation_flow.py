import unittest
from unittest import mock

from backend.registration import engine as gr


class OutlookWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)

    def tearDown(self):
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_feature_disabled_and_temp_source_are_recorded(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_registration_success": False,
            }
        )
        self.assertEqual(
            gr.default_email_disable_detail("outlookemail")["status"],
            "feature_disabled",
        )
        gr.config["outlookemail_disable_after_registration_success"] = True
        gr.config["outlookemail_source"] = "temp"
        self.assertEqual(
            gr.default_email_disable_detail("outlookemail")["status"],
            "unsupported_source",
        )

    def test_registration_success_can_disable_outlook_account(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_registration_success": True,
            }
        )
        with mock.patch.object(
            gr.outlookemail_provider,
            "account_for_email",
            return_value={"id": 88, "email": "person@outlook.com"},
        ), mock.patch.object(
            gr.outlookemail_provider,
            "disable_account",
            return_value={"success": True, "account_id": 88},
        ) as disable:
            detail = gr.disable_outlookemail_after_registration_success(
                "person@outlook.com"
            )
        self.assertEqual(detail["status"], "success")
        self.assertEqual(detail["account_id"], "88")
        disable.assert_called_once()
        self.assertEqual(disable.call_args.kwargs["proxies"], {})

    def test_already_registered_account_is_treated_as_consumed(self):
        gr.config["email_provider"] = "outlookemail"
        with mock.patch.object(
            gr, "disable_outlookemail_consumed", return_value={"status": "success"}
        ) as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_ALREADY_REGISTERED,
                "duplicate@outlook.com",
                reason="账号已注册",
            )
        assert detail is not None
        self.assertEqual(detail["status"], "success")
        disable.assert_called_once()

    def test_other_failures_do_not_disable_outlook_account(self):
        gr.config["email_provider"] = "outlookemail"
        with mock.patch.object(gr, "disable_outlookemail_consumed") as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_CODE, "timeout@outlook.com", reason="未收到验证码"
            )
        self.assertIsNone(detail)
        disable.assert_not_called()

    def test_wait_for_code_timeout_triggers_group_move(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_group_id": "1",
                "outlookemail_code_timeout_group_id": "14",
            }
        )
        with mock.patch.object(
            gr.outlookemail_provider,
            "wait_for_code",
            side_effect=Exception("OutlookEmail 在 60s 内未收到验证码邮件"),
        ), mock.patch.object(
            gr,
            "maybe_move_outlookemail_for_code_timeout",
            return_value={"status": "success"},
        ) as move:
            with self.assertRaisesRegex(Exception, "未收到验证码"):
                gr.outlookemail_get_oai_code("timeout@outlook.com")
        move.assert_called_once()


if __name__ == "__main__":
    unittest.main()
