"""Offline native QR lifecycle tests; no gateway or HA installation required."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest


path = Path(__file__).resolve().parents[1] / "custom_components/tuya_recordings/lib/native_auth.py"
spec = importlib.util.spec_from_file_location("native_auth_under_test", path)
auth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auth
spec.loader.exec_module(auth)


class NativeAuthTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, responses, **kwargs):
        self.calls = []

        async def call(api, version, body):
            self.calls.append((api, version, body))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        return auth.NativeQrAuthorization(
            call,
            device_fingerprint="fingerprint",
            **kwargs,
        )

    @staticmethod
    def session(**updates):
        result = {
            "sid": "private-sid",
            "ecode": "private-ecode",
            "uid": "private-uid",
            "partnerIdentity": "partner",
            "domain": {"mobileMqttsUrl": "mqtt.example.test"},
        }
        result.update(updates)
        return result

    async def test_exact_requests_and_secrets_not_in_repr(self):
        flow = self.flow(["test-token", self.session()])
        self.assertEqual(await flow.begin(), "tuyaSmart--qrLogin?token=test-token")
        self.assertEqual(len(self.calls), 1)
        result = await flow.finish()
        self.assertEqual(result.sid, "private-sid")
        self.assertEqual(result.mobile_mqtts_url, "mqtt.example.test")
        self.assertEqual(result.device_fingerprint, "fingerprint")
        self.assertNotIn("private", repr(result))
        self.assertEqual(self.calls, [
            ("thing.m.user.qr.token.create", "1.0", {}),
            ("thing.m.user.qr.token.user.get", "1.0", {"token": "test-token"}),
        ])
        for operation in (flow.begin, flow.finish):
            with self.assertRaises(auth.NativeAuthorizationError):
                await operation()
        self.assertEqual(len(self.calls), 2)

    async def test_tuya_smart_flow_uses_its_own_qr_payload_and_endpoints(self):
        flow = self.flow(
            ["test-token", self.session()],
            qr_scheme="tuyaSmart",
            qr_create_api="thing.m.user.qr.token.create",
            qr_finish_api="thing.m.user.qr.token.user.get",
        )

        self.assertEqual(await flow.begin(), "tuyaSmart--qrLogin?token=test-token")
        await flow.finish()
        self.assertEqual(
            self.calls,
            [
                ("thing.m.user.qr.token.create", "1.0", {}),
                ("thing.m.user.qr.token.user.get", "1.0", {"token": "test-token"}),
            ],
        )

    async def test_failure_has_no_retry_or_sensitive_error(self):
        flow = self.flow([RuntimeError("secret-server-response")])
        with self.assertRaises(auth.NativeAuthorizationError) as error:
            await flow.begin()
        self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(auth.NativeAuthorizationError):
            await flow.begin()
        self.assertEqual(len(self.calls), 1)

    async def test_expired_flow_never_checks_server(self):
        now = [0]
        flow = self.flow(["token"], clock=lambda: now[0])
        await flow.begin()
        now[0] = 301
        with self.assertRaises(auth.NativeAuthorizationError):
            await flow.finish()
        self.assertEqual(len(self.calls), 1)

    async def test_incomplete_session_stops(self):
        for result in (
            {"sid": "s", "uid": "u"},
            None,
            self.session(ecode=""),
            self.session(partnerIdentity=""),
            self.session(domain={}),
        ):
            flow = self.flow(["token", result])
            await flow.begin()
            with self.assertRaises(auth.NativeAuthorizationError):
                await flow.finish()
            with self.assertRaises(auth.NativeAuthorizationError):
                await flow.finish()
            self.assertEqual(len(self.calls), 2)

    async def test_malformed_token_stops(self):
        for token in (None, "", "bad&token=x", "x" * 2049):
            flow = self.flow([token])
            with self.assertRaises(auth.NativeAuthorizationError):
                await flow.begin()
            self.assertEqual(len(self.calls), 1)

    async def test_cancel_prevents_late_token_and_concurrent_begin(self):
        started = asyncio.Event()

        async def stubborn_call(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "late-token"

        flow = auth.NativeQrAuthorization(stubborn_call, device_fingerprint="fingerprint")
        task = asyncio.create_task(flow.begin())
        await started.wait()
        with self.assertRaises(auth.NativeAuthorizationError):
            await flow.begin()
        flow.cancel()
        with self.assertRaises(auth.NativeAuthorizationError):
            await task
        self.assertIsNone(flow._token)

    async def test_caller_cancel_propagates(self):
        started = asyncio.Event()

        async def blocked_call(*args):
            started.set()
            await asyncio.Event().wait()

        flow = auth.NativeQrAuthorization(blocked_call, device_fingerprint="fingerprint")
        task = asyncio.create_task(flow.begin())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(auth.NativeAuthorizationError):
            await flow.begin()

    async def test_cancelled_before_start_makes_no_request(self):
        flow = self.flow([])
        flow.cancel()
        with self.assertRaises(auth.NativeAuthorizationError):
            await flow.begin()
        self.assertEqual(self.calls, [])

    async def test_completion_refusal_is_terminal(self):
        flow = self.flow(["token", RuntimeError("secret-refusal")])
        await flow.begin()
        with self.assertRaises(auth.NativeAuthorizationError) as error:
            await flow.finish()
        self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(auth.NativeAuthorizationError):
            await flow.finish()
        self.assertEqual(len(self.calls), 2)

    async def test_caller_cancel_even_if_gateway_swallows_it(self):
        started = asyncio.Event()

        async def stubborn_call(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "late-token"

        flow = auth.NativeQrAuthorization(stubborn_call, device_fingerprint="fingerprint")
        task = asyncio.create_task(flow.begin())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(flow._token)


if __name__ == "__main__":
    unittest.main()
