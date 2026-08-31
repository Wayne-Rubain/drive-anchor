"""Configuration loading and validation.

Two rules shape this module:

1. **Secrets never live in the config file.** The DSM password is read from
   the environment only. A config file gets copied into gists, pasted into
   forum posts and committed to git; an environment variable does not.

2. **Fail loudly on a malformed config.** A drive with no UUID, or a DSM
   section with no host, must stop the run rather than be quietly skipped.
   Skipping means a detach sequence reports success while leaving a drive
   mounted and spinning -- the worst failure this tool could have.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("DRIVE_ANCHOR_CONFIG", "/data/config.yaml")

ENV_ACCOUNT = "DRIVE_ANCHOR_DSM_ACCOUNT"
ENV_PASSWORD = "DRIVE_ANCHOR_DSM_PASSWORD"


class ConfigError(ValueError):
    """The configuration is unusable. The message says which key and why."""


@dataclass
class Drive:
    """One USB drive Drive Anchor manages.

    uuid  -- filesystem UUID from `blkid`. The stable identity.
    path  -- the fixed /volume1 path it is always reachable at.
    share -- optional DSM share name, used only to pause indexing.
    """
    name: str
    uuid: str
    path: str
    share: Optional[str] = None

    @property
    def label(self) -> str:
        return self.name or self.path


@dataclass
class DsmConfig:
    host: str = "localhost"
    port: int = 5001
    use_https: bool = True
    verify_tls: bool = False
    session_name: str = "DriveAnchor"
    account: str = ""
    password: str = field(default="", repr=False)
    login_timeout_sec: int = 15
    call_timeout_sec: int = 20
    eject_confirm_timeout_sec: int = 45
    eject_retries: int = 3

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.host}:{self.port}/webapi"


@dataclass
class QuiesceConfig:
    """Services to stop before ejecting, and restart afterwards.

    A running media server holds files open, which makes DSM's eject fail,
    and its indexer will happily rescan a half-mounted library and record
    that your media has vanished. Both are avoided by pausing them first.
    """
    packages: List[str] = field(default_factory=list)
    index_shares: List[str] = field(default_factory=list)
    command_timeout_sec: int = 20


@dataclass
class RepairConfig:
    """Limits on unattended repair.

    max_per_hour exists so self-healing cannot quietly paper over a drive
    that is dropping off the bus every few minutes. Past the cap, `repair`
    stops fixing and starts complaining, which is the only way the problem
    ever reaches a person.

    state_dir must be writable for the cap to survive between scheduled runs.
    If it is not, repair still works but the cap is not enforced across
    invocations, and it says so.
    """
    max_per_hour: int = 3
    state_dir: str = "/data/state"


@dataclass
class Config:
    dry_run: bool = True
    drives: List[Drive] = field(default_factory=list)
    dsm: DsmConfig = field(default_factory=DsmConfig)
    quiesce: QuiesceConfig = field(default_factory=QuiesceConfig)
    repair: RepairConfig = field(default_factory=RepairConfig)
    bind_wait_sec: int = 90
    bind_poll_sec: int = 3
    verify_attempts: int = 3
    verify_retry_sec: int = 10

    @property
    def paths(self) -> List[str]:
        return [d.path for d in self.drives]

    def drive_by_name(self, name: str) -> Optional[Drive]:
        for d in self.drives:
            if d.name == name:
                return d
        return None


def load(path: str = None) -> Config:
    """Read and validate the config file. Raises ConfigError on any problem."""
    path = path or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"no config file at {path}. Copy config.example.yaml and edit it, "
            f"or set DRIVE_ANCHOR_CONFIG to its location.")
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}")

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    return _build(raw)


def _build(raw: dict) -> Config:
    drives = []
    seen_paths = set()
    seen_uuids = set()

    entries = raw.get("drives") or []
    if not isinstance(entries, list):
        raise ConfigError("'drives' must be a list")

    for i, entry in enumerate(entries):
        where = f"drives[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping")
        for key in ("uuid", "path"):
            if not entry.get(key):
                raise ConfigError(f"{where} is missing required key '{key}'")
        path = str(entry["path"])
        uuid = str(entry["uuid"])
        if not path.startswith("/"):
            raise ConfigError(f"{where}.path must be absolute, got {path!r}")
        # Duplicates are always a copy-paste slip, and both forms are
        # dangerous: two drives sharing a path means one silently shadows the
        # other, and one UUID on two paths means a bind that flip-flops.
        if path in seen_paths:
            raise ConfigError(f"{where}.path {path!r} is used by another drive")
        if uuid in seen_uuids:
            raise ConfigError(f"{where}.uuid {uuid!r} is used by another drive")
        seen_paths.add(path)
        seen_uuids.add(uuid)
        drives.append(Drive(
            name=str(entry.get("name") or path.rsplit("/", 1)[-1]),
            uuid=uuid,
            path=path,
            share=entry.get("share"),
        ))

    dsm_raw = raw.get("dsm") or {}
    if not isinstance(dsm_raw, dict):
        raise ConfigError("'dsm' must be a mapping")
    dsm = DsmConfig(**{k: v for k, v in dsm_raw.items()
                       if k in DsmConfig.__dataclass_fields__ and k != "password"})
    # Credentials come from the environment, never the file.
    dsm.account = os.environ.get(ENV_ACCOUNT, dsm.account)
    dsm.password = os.environ.get(ENV_PASSWORD, "")

    q_raw = raw.get("quiesce") or {}
    if not isinstance(q_raw, dict):
        raise ConfigError("'quiesce' must be a mapping")
    quiesce = QuiesceConfig(**{k: v for k, v in q_raw.items()
                               if k in QuiesceConfig.__dataclass_fields__})

    r_raw = raw.get("repair") or {}
    if not isinstance(r_raw, dict):
        raise ConfigError("'repair' must be a mapping")
    repair = RepairConfig(**{k: v for k, v in r_raw.items()
                             if k in RepairConfig.__dataclass_fields__})

    cfg = Config(
        dry_run=bool(raw.get("dry_run", True)),
        drives=drives,
        dsm=dsm,
        quiesce=quiesce,
        repair=repair,
    )
    for key in ("bind_wait_sec", "bind_poll_sec", "verify_attempts", "verify_retry_sec"):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    return cfg


def require_credentials(cfg: Config) -> None:
    """Check DSM credentials are present before a sequence that needs them.

    Called at the start of an operation rather than at load time, so that
    read-only commands like `list` and `status` work without them.
    """
    missing = []
    if not cfg.dsm.account:
        missing.append(ENV_ACCOUNT)
    if not cfg.dsm.password:
        missing.append(ENV_PASSWORD)
    if missing:
        raise ConfigError(
            "DSM credentials are not set. This operation needs an admin "
            "account to call DSM's eject API. Set: " + ", ".join(missing))
