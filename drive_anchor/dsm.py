"""Talking to DSM's web API to eject USB devices.

A warning about this interface
------------------------------
`SYNO.Core.ExternalDevice.Storage.USB` is **not a documented or supported
API**. The parameter names here were determined by watching what Storage
Manager itself sends when you click Eject. Synology can change it in any DSM
release without notice.

That is the single largest maintenance risk in this project, and it is why
every call is checked explicitly rather than trusted: when this breaks, it
must break loudly and stop, not fail quietly and let a caller proceed as
though drives were safely ejected.
"""

from __future__ import annotations

import logging
import time
from typing import List, Tuple

import requests
import urllib3

from .config import DsmConfig

log = logging.getLogger(__name__)

# DSM ships a self-signed certificate on its own HTTPS port. Verification is
# off by default for localhost calls, so silence the resulting per-call
# warning rather than printing it dozens of times per run.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LIST_API = {"api_name": "SYNO.Core.ExternalDevice.Storage.USB",
            "api_path": "entry.cgi", "version": 1, "method": "list"}
EJECT_API = {"api_name": "SYNO.Core.ExternalDevice.Storage.USB",
             "api_path": "entry.cgi", "version": 1, "method": "eject"}


class DsmApiError(RuntimeError):
    """DSM answered, but said the call failed.

    Kept separate from requests' own network errors for one specific reason.
    An empty device list is *also* the correct answer to "how many USB
    devices are attached?" when the answer is genuinely none. If a failed
    API call were allowed to return an empty list too, a caller could not
    tell "nothing to eject" from "the question could not be asked" -- and
    would carry on believing the drives were safely detached when they were
    never asked to detach at all.

    This is not hypothetical. It is exactly what a masked error 119
    ("SID not found"), returned on the first list call immediately after a
    successful login, did to an earlier version of this code.
    """


class DsmClient:
    """A logged-in DSM session. Use as a context manager to log out cleanly."""

    def __init__(self, cfg: DsmConfig):
        self._cfg = cfg
        self._sid = None

    def __enter__(self) -> "DsmClient":
        self.login()
        return self

    def __exit__(self, *exc_info) -> None:
        self.logout()

    def login(self) -> None:
        r = requests.get(f"{self._cfg.base_url}/auth.cgi", params={
            "api": "SYNO.API.Auth", "version": 6, "method": "login",
            "account": self._cfg.account, "passwd": self._cfg.password,
            "session": self._cfg.session_name, "format": "sid",
        }, timeout=self._cfg.login_timeout_sec, verify=self._cfg.verify_tls)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            # Deliberately does not echo the response: DSM includes the
            # submitted account name in some error bodies, and this message
            # is what people paste into bug reports.
            code = (data.get("error") or {}).get("code", "unknown")
            raise DsmApiError(
                f"DSM login failed (error {code}). Check the account has "
                f"admin rights and that DRIVE_ANCHOR_DSM_PASSWORD is correct.")
        self._sid = data["data"]["sid"]
        log.info("  DSM login OK")

    def logout(self) -> None:
        if not self._sid:
            return
        try:
            requests.get(f"{self._cfg.base_url}/auth.cgi", params={
                "api": "SYNO.API.Auth", "version": 6, "method": "logout",
                "session": self._cfg.session_name, "_sid": self._sid,
            }, timeout=10, verify=self._cfg.verify_tls)
        except requests.RequestException:
            pass  # Best effort; the session expires on its own.
        self._sid = None

    def _call(self, endpoint: dict, extra: dict = None) -> dict:
        # "session" is sent on every authenticated call, not just at login.
        # Synology's own published guides for other API families do this,
        # and it appears to reduce how often DSM fails to resolve the _sid.
        params = {"api": endpoint["api_name"], "version": endpoint["version"],
                  "method": endpoint["method"], "_sid": self._sid,
                  "session": self._cfg.session_name}
        params.update(extra or {})
        r = requests.get(f"{self._cfg.base_url}/{endpoint['api_path']}",
                         params=params, timeout=self._cfg.call_timeout_sec,
                         verify=self._cfg.verify_tls)
        r.raise_for_status()
        return r.json()

    def list_devices(self) -> List[Tuple[str, str]]:
        """Every USB device DSM currently sees, as (dev_id, title) pairs.

        Asked fresh each time rather than read from config, so any number of
        drives is handled without a fixed list -- including ones the user
        plugged in and never told Drive Anchor about. Anything DSM reports
        gets ejected.

        Raises DsmApiError rather than returning [] on failure. See the
        DsmApiError docstring for why that distinction is load-bearing.
        """
        resp = self._call(LIST_API)
        if not resp.get("success"):
            raise DsmApiError(f"DSM device list call failed: {resp}")
        devices = resp.get("data", {}).get("devices", [])
        return [(d.get("dev_id"), d.get("dev_title") or d.get("dev_id"))
                for d in devices]

    def eject(self, dev_id: str) -> bool:
        """Ask DSM to eject one device. The real parameter name is `dev_id`."""
        resp = self._call(EJECT_API, {"dev_id": dev_id})
        ok = bool(resp.get("success"))
        log.info("  eject %s -> %s", dev_id, "ok" if ok else resp)
        return ok

    def is_ejected(self, dev_id: str) -> bool:
        """True when the device no longer appears in DSM's device list.

        Returns False -- not an exception -- if the list call fails, because
        this is used inside a confirmation poll where "cannot confirm yet"
        and "not yet ejected" should both mean "keep waiting".
        """
        try:
            return not any(d == dev_id for d, _ in self.list_devices())
        except (DsmApiError, requests.RequestException) as exc:
            log.warning("  could not confirm ejection of %s: %s", dev_id, exc)
            return False

    def eject_with_retry(self, dev_id: str, title: str) -> bool:
        """Eject one device, retrying, then confirm DSM agrees it is gone.

        Both halves matter. The eject command can time out and still have
        worked, so a failed call is retried rather than treated as fatal;
        and the command returning success is not proof, so the device list
        is polled until DSM stops reporting the device.
        """
        for attempt in range(1, self._cfg.eject_retries + 1):
            try:
                if self.eject(dev_id):
                    break
            except (requests.RequestException, DsmApiError) as exc:
                log.warning("  eject attempt %d/%d for %s failed: %s",
                            attempt, self._cfg.eject_retries, title, exc)
            time.sleep(2)

        deadline = time.time() + self._cfg.eject_confirm_timeout_sec
        while time.time() < deadline:
            if self.is_ejected(dev_id):
                log.info("  DSM confirmed ejected: %s", title)
                return True
            time.sleep(2)
        log.error("  DSM never confirmed %s was ejected", title)
        return False
