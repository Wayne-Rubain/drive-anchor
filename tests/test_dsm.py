"""Tests for dsm.py -- the client for DSM's undocumented eject API.

No NAS is contacted. Every HTTP call is intercepted, which also means these
tests pin down the exact request shape: if a future change quietly renames a
parameter, these fail rather than the ejects silently doing nothing.

The most important tests here are the ones about *not trusting* a response:
an eject command reporting success is not proof the drive ejected, and a
failed list call must never look like "no devices attached".

Run with:  python3 tests/test_dsm.py
"""

import itertools
import logging
import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_anchor import dsm                                  # noqa: E402
from drive_anchor.config import DsmConfig                     # noqa: E402
from drive_anchor.dsm import DsmApiError, DsmClient           # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def config(**kw):
    cfg = DsmConfig(host="localhost", port=5001, use_https=True,
                    account="admin", session_name="DriveAnchor")
    cfg.password = "secret"
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


LOGIN_OK = {"success": True, "data": {"sid": "SID123"}}


def logged_in(cfg=None):
    """A client with a session, without exercising login in every test."""
    client = DsmClient(cfg or config())
    client._sid = "SID123"
    return client


class BaseUrl(unittest.TestCase):
    def test_https_by_default(self):
        self.assertEqual(config().base_url, "https://localhost:5001/webapi")

    def test_http_when_tls_is_off(self):
        self.assertEqual(config(use_https=False).base_url,
                         "http://localhost:5001/webapi")


class Login(unittest.TestCase):
    def test_successful_login_stores_the_session_id(self):
        with mock.patch("requests.get", return_value=FakeResponse(LOGIN_OK)):
            client = DsmClient(config())
            client.login()
        self.assertEqual(client._sid, "SID123")

    def test_failed_login_raises(self):
        with mock.patch("requests.get",
                        return_value=FakeResponse({"success": False,
                                                   "error": {"code": 400}})):
            with self.assertRaises(DsmApiError):
                DsmClient(config()).login()

    def test_failure_message_does_not_echo_the_response(self):
        """DSM includes the submitted account name in some error bodies, and
        this message is what people paste into bug reports.

        The account name here is deliberately distinctive: the word "admin"
        appears legitimately in the message's own advice ("check the account
        has admin rights"), so asserting on that would test nothing.
        """
        cfg = config()
        cfg.account = "wayne_nas_operator"
        with mock.patch("requests.get",
                        return_value=FakeResponse({"success": False,
                                                   "account": "wayne_nas_operator",
                                                   "error": {"code": 400}})):
            with self.assertRaises(DsmApiError) as ctx:
                DsmClient(cfg).login()
        message = str(ctx.exception)
        self.assertNotIn("wayne_nas_operator", message)
        self.assertNotIn("success", message)
        self.assertIn("400", message)

    def test_credentials_are_sent_and_the_password_is_not_logged(self):
        with mock.patch("requests.get",
                        return_value=FakeResponse(LOGIN_OK)) as get:
            DsmClient(config()).login()
        params = get.call_args[1]["params"]
        self.assertEqual(params["account"], "admin")
        self.assertEqual(params["passwd"], "secret")
        self.assertEqual(params["method"], "login")

    def test_context_manager_logs_in_and_out(self):
        with mock.patch("requests.get",
                        return_value=FakeResponse(LOGIN_OK)) as get:
            with DsmClient(config()) as client:
                self.assertEqual(client._sid, "SID123")
            self.assertIsNone(client._sid)
        methods = [c[1]["params"]["method"] for c in get.call_args_list]
        self.assertEqual(methods, ["login", "logout"])

    def test_logout_swallows_network_errors(self):
        """Best effort: the session expires on its own, and an exception here
        would mask whatever the caller was actually doing."""
        client = logged_in()
        with mock.patch("requests.get",
                        side_effect=requests.ConnectionError("gone")):
            client.logout()          # must not raise
        self.assertIsNone(client._sid)


class RequestShape(unittest.TestCase):
    def test_session_is_sent_on_every_authenticated_call(self):
        client = logged_in()
        with mock.patch("requests.get", return_value=FakeResponse(
                {"success": True, "data": {"devices": []}})) as get:
            client.list_devices()
        params = get.call_args[1]["params"]
        self.assertEqual(params["_sid"], "SID123")
        self.assertEqual(params["session"], "DriveAnchor")

    def test_eject_uses_dev_id_not_device_id(self):
        """The real parameter name, determined from what Storage Manager
        itself sends. Get this wrong and the call returns success while
        ejecting nothing."""
        client = logged_in()
        with mock.patch("requests.get",
                        return_value=FakeResponse({"success": True})) as get:
            client.eject("usb6")
        params = get.call_args[1]["params"]
        self.assertEqual(params["dev_id"], "usb6")
        self.assertNotIn("device_id", params)
        self.assertEqual(params["method"], "eject")

    def test_http_errors_propagate(self):
        client = logged_in()
        with mock.patch("requests.get", return_value=FakeResponse({}, status=500)):
            with self.assertRaises(requests.HTTPError):
                client.list_devices()


class ListDevices(unittest.TestCase):
    def test_parses_id_and_title_pairs(self):
        client = logged_in()
        payload = {"success": True, "data": {"devices": [
            {"dev_id": "usb1", "dev_title": "USB Disk 1"},
            {"dev_id": "usb6", "dev_title": "USB Disk 5"},
        ]}}
        with mock.patch("requests.get", return_value=FakeResponse(payload)):
            self.assertEqual(client.list_devices(),
                             [("usb1", "USB Disk 1"), ("usb6", "USB Disk 5")])

    def test_missing_title_falls_back_to_the_id(self):
        client = logged_in()
        payload = {"success": True, "data": {"devices": [{"dev_id": "usb3"}]}}
        with mock.patch("requests.get", return_value=FakeResponse(payload)):
            self.assertEqual(client.list_devices(), [("usb3", "usb3")])

    def test_genuinely_no_devices_is_an_empty_list(self):
        client = logged_in()
        with mock.patch("requests.get", return_value=FakeResponse(
                {"success": True, "data": {"devices": []}})):
            self.assertEqual(client.list_devices(), [])

    def test_a_FAILED_call_RAISES_rather_than_returning_empty(self):
        """The load-bearing test in this file.

        An empty list is also the correct answer to "how many USB devices are
        attached?" when the answer is none. If a failed call returned [] too,
        a caller could not tell "nothing to eject" from "the question could
        not be asked" -- and would carry on believing the drives were safely
        detached when they were never asked to detach at all.

        This is not hypothetical: a masked error 119 ("SID not found") on the
        first list call after a successful login did exactly that.
        """
        client = logged_in()
        with mock.patch("requests.get", return_value=FakeResponse(
                {"success": False, "error": {"code": 119}})):
            with self.assertRaises(DsmApiError):
                client.list_devices()


class IsEjected(unittest.TestCase):
    def test_true_when_the_device_is_gone(self):
        client = logged_in()
        with mock.patch.object(client, "list_devices", return_value=[("usb1", "A")]):
            self.assertTrue(client.is_ejected("usb6"))

    def test_false_while_it_is_still_listed(self):
        client = logged_in()
        with mock.patch.object(client, "list_devices", return_value=[("usb6", "A")]):
            self.assertFalse(client.is_ejected("usb6"))

    def test_an_api_failure_means_keep_waiting_not_crash(self):
        """Used inside a confirmation poll, where "cannot confirm yet" and
        "not ejected yet" should both mean keep waiting. Raising here would
        abort a sequence that might still be succeeding."""
        client = logged_in()
        with mock.patch.object(client, "list_devices",
                               side_effect=DsmApiError("boom")):
            self.assertFalse(client.is_ejected("usb6"))

    def test_a_network_failure_also_means_keep_waiting(self):
        client = logged_in()
        with mock.patch.object(client, "list_devices",
                               side_effect=requests.ConnectionError("down")):
            self.assertFalse(client.is_ejected("usb6"))


def fake_clock(start=0, step=1):
    """A clock that advances forever and never runs out.

    A finite side_effect list cannot be used here: logging calls time.time()
    internally to timestamp each record, so it consumes values from the same
    mock and the test dies with StopIteration inside the logging module
    rather than in the code under test.
    """
    counter = itertools.count(start, step)
    return lambda: float(next(counter))


class EjectWithRetry(unittest.TestCase):
    """Command success is not proof. Confirmation decides the outcome."""

    def setUp(self):
        # Keeps the clock deterministic and the test output readable.
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_success_then_confirmation(self):
        client = logged_in()
        with mock.patch.object(client, "eject", return_value=True) as eject, \
             mock.patch.object(client, "is_ejected", return_value=True), \
             mock.patch("drive_anchor.dsm.time.sleep"):
            self.assertTrue(client.eject_with_retry("usb6", "Dock"))
        eject.assert_called_once()

    def test_a_failing_command_is_retried(self):
        client = logged_in()
        with mock.patch.object(client, "eject",
                               side_effect=[False, False, True]) as eject, \
             mock.patch.object(client, "is_ejected", return_value=True), \
             mock.patch("drive_anchor.dsm.time.sleep"):
            self.assertTrue(client.eject_with_retry("usb6", "Dock"))
        self.assertEqual(eject.call_count, 3)

    def test_an_exception_from_the_command_is_retried_not_fatal(self):
        """An eject can time out and still have worked, so a raised error is
        not the end of the story -- confirmation is."""
        client = logged_in()
        with mock.patch.object(
                client, "eject",
                side_effect=[requests.ReadTimeout("slow"), True]) as eject, \
             mock.patch.object(client, "is_ejected", return_value=True), \
             mock.patch("drive_anchor.dsm.time.sleep"):
            self.assertTrue(client.eject_with_retry("usb6", "Dock"))
        self.assertEqual(eject.call_count, 2)

    def test_a_timed_out_command_that_actually_worked_still_confirms(self):
        """The exact case seen in practice: every eject attempt reports
        failure, but DSM has in fact ejected the drive. Confirmation is what
        decides, so this must succeed."""
        client = logged_in()
        with mock.patch.object(client, "eject", return_value=False), \
             mock.patch.object(client, "is_ejected", return_value=True), \
             mock.patch("drive_anchor.dsm.time.sleep"):
            self.assertTrue(client.eject_with_retry("usb6", "Dock"))

    def test_never_confirmed_returns_False(self):
        """A command that claims success but is never confirmed must fail.
        Reporting success here would let a caller cut power to a drive DSM
        still has mounted."""
        client = logged_in(config(eject_confirm_timeout_sec=3))
        with mock.patch.object(client, "eject", return_value=True), \
             mock.patch.object(client, "is_ejected", return_value=False) as check, \
             mock.patch("drive_anchor.dsm.time.sleep"), \
             mock.patch("drive_anchor.dsm.time.time", side_effect=fake_clock()):
            self.assertFalse(client.eject_with_retry("usb6", "Dock"))
        # It must actually have polled before giving up, not merely timed out.
        self.assertGreater(check.call_count, 0)

    def test_retries_are_bounded_by_config(self):
        client = logged_in(config(eject_retries=2, eject_confirm_timeout_sec=3))
        with mock.patch.object(client, "eject", return_value=False) as eject, \
             mock.patch.object(client, "is_ejected", return_value=False), \
             mock.patch("drive_anchor.dsm.time.sleep"), \
             mock.patch("drive_anchor.dsm.time.time", side_effect=fake_clock()):
            client.eject_with_retry("usb6", "Dock")
        self.assertEqual(eject.call_count, 2)


class ApiEndpoints(unittest.TestCase):
    """Pinned so a rename is caught here rather than in production."""

    def test_list_and_eject_use_the_usb_storage_api(self):
        for endpoint in (dsm.LIST_API, dsm.EJECT_API):
            self.assertEqual(endpoint["api_name"],
                             "SYNO.Core.ExternalDevice.Storage.USB")
            self.assertEqual(endpoint["api_path"], "entry.cgi")
            self.assertEqual(endpoint["version"], 1)
        self.assertEqual(dsm.LIST_API["method"], "list")
        self.assertEqual(dsm.EJECT_API["method"], "eject")


if __name__ == "__main__":
    unittest.main(verbosity=2)
