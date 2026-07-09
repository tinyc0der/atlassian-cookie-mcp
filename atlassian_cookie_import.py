"""Shared cookie-import logic for CLI and Chrome native-messaging host.

Splits an extension-style cookie list into per-service jars and probes each
service for liveness. No I/O of the export file, no process exit — callers
decide how to present results and whether to delete a source file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from atlassian_browser_auth import (
    BrowserAuthConfig,
    _cookie_matches_base_url,
    probe_live,
    write_storage_state,
)

ServiceName = Literal["jira", "confluence"]
ALL_SERVICES: tuple[ServiceName, ...] = ("jira", "confluence")


@dataclass
class ServiceImportResult:
    service: str
    matched: int = 0
    status: int | None = None
    jar: str | None = None
    skipped: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImportResult:
    services: list[ServiceImportResult] = field(default_factory=list)
    any_matched: bool = False
    any_live: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.error is None and self.any_matched and self.any_live,
            "any_matched": self.any_matched,
            "any_live": self.any_live,
            "error": self.error,
            "services": {
                s.service: s.to_dict() for s in self.services
            },
        }


def import_cookies(
    cookies: list[dict[str, Any]],
    service: ServiceName | None = None,
) -> ImportResult:
    """Write matching cookies to jars and probe each service.

    ``service`` limits import to one jar; default is both jira and confluence.
    Requires ``JIRA_URL`` / ``CONFLUENCE_URL`` in the environment (or pre-loaded
    host env — see :mod:`atlassian_native_host`).
    """
    if not isinstance(cookies, list):
        return ImportResult(error="missing a 'cookies' list")

    services: list[ServiceName]
    if service is None:
        services = list(ALL_SERVICES)
    elif service in ALL_SERVICES:
        services = [service]
    else:
        return ImportResult(error=f"unknown service: {service}")

    result = ImportResult()
    for svc in services:
        try:
            cfg = BrowserAuthConfig.from_env(svc)
        except RuntimeError as exc:
            result.error = str(exc)
            return result
        base = cfg.service_base(svc)
        matched = [c for c in cookies if _cookie_matches_base_url(c, base)]
        if not matched:
            result.services.append(
                ServiceImportResult(
                    service=svc,
                    skipped=True,
                    message=f"no cookies match {base}",
                )
            )
            continue
        write_storage_state(matched, cfg.storage_state)
        status = probe_live(base, svc, matched, cfg.user_agent)
        live = status == 200
        result.any_matched = True
        if live:
            result.any_live = True
        msg = (
            f"imported {len(matched)} cookies -> HTTP {status} "
            f"({'live' if live else 'NOT live'})"
        )
        result.services.append(
            ServiceImportResult(
                service=svc,
                matched=len(matched),
                status=status,
                jar=str(cfg.storage_state),
                message=msg,
            )
        )

    if not result.any_matched and result.error is None:
        result.error = (
            "No cookies matched your JIRA_URL / CONFLUENCE_URL hosts. "
            "Check the export and that those env vars point at the right instance."
        )
    return result
