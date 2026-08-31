"""Asking DSM to eject USB devices.

There are two ways to reach the same DSM API, and this module prefers the one
that needs no password.

**synowebapi** (default). `/usr/syno/bin/synowebapi` is a root-only local
command that invokes DSM's internal API directly, reporting itself as
`runner=SYSTEM_ADMIN`. Because it runs locally as root there is no login, no
account, and no password. Drive Anchor already needs root to create mounts, so
this asks for nothing it did not already have.

**HTTP** (fallback). The original path: an authenticated HTTPS session against
DSM on localhost, using an admin account. Kept because synowebapi is
undocumented and root-only, and there is no guarantee every DSM version ships
it in the same place.

A warning that applies to both
------------------------------
`SYNO.Core.ExternalDevice.Storage.USB` is **not a documented or supported
API**. The parameter names here were determined by watching what Storage
Manager sends when you click Eject. Synology can change it in any release.

That is the largest maintenance risk in this project, and it is why every
response is checked explicitly. When this breaks it must break loudly and
stop, not fail quietly and let a caller believe drives were safely ejected.

Credit: the synowebapi invocation came from a Synology user solving the same
problem on r/synology, who found it after `synousbdisk -umount` failed on a
busy filesystem.
"""

from __future__ import annotations

import json
import logging
import time
from typing import List, Optional, Tuple

import requests
import urllib3

from . import host
from .config import DsmConfig

log = logging.getLogger(__name__)

# DSM ships a self-signed certificate on its own HTTPS port. Verification is
# off by default for localhost calls, so silence the resulting per-call
# warning rather than printing it dozens of times per run.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SYNOWEBAPI = "/usr/syno/bin/synowebapi"
USB_API = "SYNO.Core.ExternalDevice.Storage.USB"
API_VERSION = 1

LIST_API = {"api_name": USB_API, "api_path": "entry.cgi",
            "version": API_VERSION, "method": "list"}
EJECT_API = {"api_name": USB_API, "api_path": "entry.cgi",
             "version": API_VERSION, "method": "eject"}


class DsmApiError(RuntimeError):
    """DSM answered, but said the call failed.

    Kept separate from network errors for one specific reason. An empty device
    list is *also* the correct answer to "how many USB devices are attached?"
    when the answer is none. If a failed call were allowed to return an empty
    list too, a caller could not tell "nothing to eject" from "the question
    could not be asked" -- and would carry on believing the drives were safely
    detached when they were never asked to detach at all.

    This is not hypothetical. A masked error 119 ("SID not found"), returned
    on the first list call immediately after a successful login, did exactly
    that to an earlier version of this code.
    """


class _DsmBase:
    """Behaviour shared by both transports.

    Subclasses provide `_list_raw()` and `_eject_raw()`; everything about
    retrying and confirming is identical either way, because the thing that
    decides the outcome is DSM's device list, not how the call was made.
    """

    def __init__(self, cfg: DsmConfig):
        self._cfg = cfg

    def __enter__(self) -> "_DsmBase":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> None:
        """Nothing to do by default."""

    def close(self) -> None:
        """Nothing to do by default."""

    # -- to be provided by the transport ------------------------------------

    def _list_raw(self) -> dict:
        raise NotImplementedError

    def _eject_raw(self, dev_id: str) -> dict:
        raise NotImplementedError

    # -- shared behaviour ---------------------------------------------------

    def list_devices(self) -> List[Tuple[str, str]]:
        """Every USB device DSM currently sees, as (dev_id, title) pairs.

        Asked fresh each time rather than read from config, so any number of
        drives is handled without a fixed list -- including ones the user
        plugged in and never told Drive Anchor about.

        Raises DsmApiError rather than returning [] on failure. See the
        DsmApiError docstring for why that distinction is load-bearing.
        """
        resp = self._list_raw()
        if not resp.get("success"):
            raise DsmApiError(f"DSM device list call failed: {resp}")
        devices = resp.get("data", {}).get("devices", [])
        return [(d.get("dev_id"), d.get("dev_title") or d.get("dev_id"))
                for d in devices]

    def eject(self, dev_id: str) -> bool:
        """Ask DSM to eject one device. The real parameter name is `dev_id`."""
        resp = self._eject_raw(dev_id)
        ok = bool(resp.get("success"))
        log.info("  eject %s -> %s", dev_id, "ok" if ok else resp)
        return ok

    def is_ejected(self, dev_id: str) -> bool:
        """True when the device no longer appears in DSM's device list.

        Returns False -- not an exception -- if the list call fails, because
        this runs inside a confirmation poll where "cannot confirm yet" and
        "not yet ejected" should both mean "keep waiting".
        """
        try:
            return not any(d == dev_id for d, _ in self.list_devices())
        except (DsmApiError, host.HostError, requests.RequestException) as exc:
            log.warning("  could not confirm ejection of %s: %s", dev_id, exc)
            return False

    def eject_with_retry(self, dev_id: str, title: str) -> bool:
        """Eject one device, retrying, then confirm DSM agrees it is gone.

        Both halves matter. The eject command can time out and still have
        worked, so a failed call is retried rather than treated as fatal; and
        the command returning success is not proof, so the device list is
        polled until DSM stops reporting the device.
        """
        for attempt in range(1, self._cfg.eject_retries + 1):
            try:
                if self.eject(dev_id):
                    break
            except (requests.RequestException, host.HostError, DsmApiError) as exc:
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


class SynoWebApiClient(_DsmBase):
    """Calls DSM's API through the local synowebapi command. No credentials.

    Two things about this command's behaviour matter:

    * Its progress chatter goes to **stderr**, so stdout is clean JSON.
    * **It exits 0 even when the call failed**, reporting the problem as
      `success: false` in the body. So the exit code says only "the command
      ran", never "the request worked", and it must not be trusted as though
      it did.
    """

    @staticmethod
    def available() -> bool:
        try:
            return host.run_on_host(["test", "-x", SYNOWEBAPI],
                                    timeout=15).returncode == 0
        except host.HostError:
            return False

    def _call(self, method: str, extra: dict = None) -> dict:
        # Built as a literal list in the call itself, not assembled into a
        # variable first. tests/test_docs.py reads the command name out of the
        # syntax tree to check it is documented in docs/privileges.md, and a
        # command hidden behind a variable would slip past that check.
        args = [f"api={USB_API}", f"method={method}", f"version={API_VERSION}"]
        args += [f"{key}={value}" for key, value in (extra or {}).items()]

        result = host.run_on_host([SYNOWEBAPI, "--exec", *args],
                                  timeout=self._cfg.call_timeout_sec)
        text = (result.stdout or "").strip()
        if not text:
            raise DsmApiError(
                f"synowebapi returned nothing for {method}. "
                f"stderr: {(result.stderr or '').strip()[:300]}")
        try:
            return json.loads(text)
        except ValueError:
            # Not JSON at all means the command shape changed, which is a
            # different failure from "DSM said no" and worth saying so.
            raise DsmApiError(
                f"synowebapi produced output that is not JSON for {method}: "
                f"{text[:300]}")

    def _list_raw(self) -> dict:
        return self._call("list")

    def _eject_raw(self, dev_id: str) -> dict:
        return self._call("eject", {"dev_id": dev_id})


class HttpDsmClient(_DsmBase):
    """The original transport: an authenticated HTTPS session on localhost."""

    def __init__(self, cfg: DsmConfig):
        super().__init__(cfg)
        self._sid: Optional[str] = None

    def open(self) -> None:
        self.login()

    def close(self) -> None:
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
            # submitted account name in some error bodies, and this message is
            # what people paste into bug reports.
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
        # Synology's own published guides for other API families do this, and
        # it appears to reduce how often DSM fails to resolve the _sid.
        params = {"api": endpoint["api_name"], "version": endpoint["version"],
                  "method": endpoint["method"], "_sid": self._sid,
                  "session": self._cfg.session_name}
        params.update(extra or {})
        r = requests.get(f"{self._cfg.base_url}/{endpoint['api_path']}",
                         params=params, timeout=self._cfg.call_timeout_sec,
                         verify=self._cfg.verify_tls)
        r.raise_for_status()
        return r.json()

    def _list_raw(self) -> dict:
        return self._call(LIST_API)

    def _eject_raw(self, dev_id: str) -> dict:
        return self._call(EJECT_API, {"dev_id": dev_id})


def connect(cfg: DsmConfig) -> _DsmBase:
    """Pick a transport. Prefers the one that needs no password.

    `transport: auto` uses synowebapi when the command is present and falls
    back to HTTP otherwise. Setting it explicitly to `synowebapi` or `http`
    skips the detection, which is what you want when diagnosing a problem and
    need to know which path is actually in use.
    """
    choice = (cfg.transport or "auto").lower()
    if choice not in ("auto", "synowebapi", "http"):
        raise DsmApiError(
            f"dsm.transport must be auto, synowebapi or http, not {choice!r}")

    if choice == "http":
        log.info("  using the DSM HTTP API (transport: http)")
        return HttpDsmClient(cfg)

    if choice == "synowebapi" or SynoWebApiClient.available():
        if choice == "auto":
            log.info("  using synowebapi, no DSM credentials needed")
        return SynoWebApiClient(cfg)

    log.info("  synowebapi not found, falling back to the DSM HTTP API")
    return HttpDsmClient(cfg)


def needs_credentials(cfg: DsmConfig) -> bool:
    """True when the chosen transport will require an account and password."""
    choice = (cfg.transport or "auto").lower()
    if choice == "synowebapi":
        return False
    if choice == "http":
        return True
    return not SynoWebApiClient.available()


# Kept so existing imports and any external references keep working.
DsmClient = HttpDsmClient
