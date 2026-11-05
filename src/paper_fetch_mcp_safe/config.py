"""TOML configuration loaded at startup; credentials never enter status output."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .compliance import LICENSE_POLICIES

DEFAULT_CONFIG_NAME = "paper-fetch.toml"
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+")
_ALLOWED_KEYS = {
    "mailto", "openalex_api_key", "s2_api_key", "core_api_key", "allow_scihub",
    "scihub_mirrors", "license_policy", "timeout_seconds", "host_rates", "download_directory",
}


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class Config:
    mailto: str
    openalex_api_key: str | None = None
    s2_api_key: str | None = None
    core_api_key: str | None = None
    allow_scihub: bool = False
    scihub_mirrors: list[str] = field(default_factory=lambda: ["https://sci-hub.se"])
    license_policy: str = "open"
    timeout_seconds: float = 45.0
    host_rates: dict[str, float] = field(default_factory=dict)
    download_directory: str | None = None

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "Config":
        config_path = Path(path) if path is not None else Path.cwd() / DEFAULT_CONFIG_NAME
        config_path = config_path.expanduser().resolve()
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"configuration file not found: {config_path}; create it from paper-fetch.toml.example") from exc
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read configuration file {config_path}: {exc}") from exc
        if unknown := set(data) - _ALLOWED_KEYS:
            raise ConfigError(f"unknown configuration keys in {config_path}: {', '.join(sorted(unknown))}")

        mailto = data.get("mailto")
        if not isinstance(mailto, str) or not _EMAIL_RE.fullmatch(mailto.strip()):
            raise ConfigError(f"mailto in {config_path} must be a valid contact email")

        def optional_key(name: str) -> str | None:
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ConfigError(f"{name} in {config_path} must be a string")
            return value.strip() or None

        allow_scihub = data.get("allow_scihub", False)
        if not isinstance(allow_scihub, bool):
            raise ConfigError(f"allow_scihub in {config_path} must be true or false")
        mirrors = data.get("scihub_mirrors", ["https://sci-hub.se"])
        if not isinstance(mirrors, list) or not mirrors or any(
            not isinstance(item, str) or urlparse(item).scheme != "https"
            or not urlparse(item).hostname for item in mirrors
        ):
            raise ConfigError(f"scihub_mirrors in {config_path} must be nonempty HTTPS URLs")
        mirrors = [item.rstrip("/") for item in mirrors]

        license_policy = data.get("license_policy", "open")
        if license_policy not in LICENSE_POLICIES:
            raise ConfigError(f"license_policy in {config_path} must be one of {', '.join(LICENSE_POLICIES)}")

        timeout = data.get("timeout_seconds", 45.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"timeout_seconds in {config_path} must be positive")

        rates = data.get("host_rates", {})
        if not isinstance(rates, dict) or any(
            not isinstance(host, str) or not _HOST_RE.fullmatch(host)
            or isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0
            for host, rate in rates.items()
        ):
            raise ConfigError(f"host_rates in {config_path} must map hostnames to positive numbers")

        download_directory = data.get("download_directory")
        if download_directory is not None:
            if not isinstance(download_directory, str) or not download_directory.strip():
                raise ConfigError(f"download_directory in {config_path} must be a nonempty path")
            target = Path(download_directory.strip()).expanduser()
            if not target.is_absolute():
                target = config_path.parent / target
            download_directory = str(target.resolve())

        return cls(
            mailto=mailto.strip(),
            openalex_api_key=optional_key("openalex_api_key"),
            s2_api_key=optional_key("s2_api_key"),
            core_api_key=optional_key("core_api_key"),
            allow_scihub=allow_scihub,
            scihub_mirrors=mirrors,
            license_policy=license_policy,
            timeout_seconds=float(timeout),
            host_rates={host.lower(): float(rate) for host, rate in rates.items()},
            download_directory=download_directory,
        )
