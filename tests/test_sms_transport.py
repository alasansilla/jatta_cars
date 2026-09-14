"""The SMS transports: what is sent, what is refused, and what is never logged.

No test here reaches a network. The Twilio adapter is exercised against a
mocked urlopen, which proves the request it would make — not that a text would
arrive in The Gambia. Delivery can only be proved with a real account.
"""
import io
import json
import logging
import unittest
import urllib.error
from unittest.mock import patch

from app import create_app, sms
from tests.test_marketplace import TestConfig


class TwilioConfig(TestConfig):
    SMS_BACKEND = "twilio"
    TWILIO_ACCOUNT_SID = "AC" + "0" * 32
    TWILIO_AUTH_TOKEN = "test-token-not-real"
    TWILIO_MESSAGING_SERVICE_SID = "MG" + "1" * 32
    TWILIO_FROM = ""


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TransportTests(unittest.TestCase):
    def app_for(self, config):
        app = create_app(config)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        return app

    def test_twilio_request_shape(self):
        self.app_for(TwilioConfig)
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode()
            captured["auth"] = request.headers.get("Authorization")
            return _Response(json.dumps({"sid": "SM123", "status": "queued"}).encode())

        with patch("app.sms.urllib.request.urlopen", side_effect=fake_urlopen):
            sms.send("+220877701234", "Your code is 123456.")
        self.assertIn("/Accounts/AC", captured["url"])
        self.assertIn("MessagingServiceSid=MG", captured["body"])
        self.assertIn("To=%2B220877701234", captured["body"])
        self.assertTrue(captured["auth"].startswith("Basic "))

    def test_twilio_failure_raises_and_logs_no_number_or_body(self):
        app = self.app_for(TwilioConfig)
        error_body = json.dumps({"code": 21408, "message": "Permission to send an SMS has not "
                                 "been enabled for the region indicated by the 'To' number: "
                                 "+220877701234"}).encode()
        failure = urllib.error.HTTPError("https://api.twilio.com", 400, "Bad Request", {},
                                         io.BytesIO(error_body))
        with patch("app.sms.urllib.request.urlopen", side_effect=failure), \
                self.assertLogs(app.logger, level=logging.DEBUG) as logs:
            with self.assertRaises(sms.SmsUnavailable):
                sms.send("+220877701234", "Your code is 123456.")
        output = "\n".join(logs.output)
        self.assertNotIn("7701234", output)
        self.assertNotIn("123456", output)
        self.assertIn("21408", output)

    def test_unconfigured_twilio_refuses_to_send(self):
        class Missing(TwilioConfig):
            TWILIO_AUTH_TOKEN = ""
        app = self.app_for(Missing)
        self.assertTrue(sms.configuration_problems(app))
        with patch("app.sms.urllib.request.urlopen") as network:
            with self.assertRaises(sms.SmsUnavailable):
                sms.send("+220877701234", "x")
        network.assert_not_called()

    def test_no_backend_means_unavailable(self):
        class Nothing(TestConfig):
            SMS_BACKEND = ""
        self.app_for(Nothing)
        self.assertFalse(sms.available())

    def test_fake_backend_is_refused_in_production(self):
        class Production(TestConfig):
            ENV_NAME = "production"
            SMS_BACKEND = "fake"
        app = create_app(TestConfig)
        app.config.update(ENV_NAME="production", SMS_BACKEND="fake")
        self.assertTrue(sms.configuration_problems(app))
        from config import check_production_config
        self.assertTrue(any("fake" in problem for problem in check_production_config(app)))


if __name__ == "__main__":
    unittest.main()
