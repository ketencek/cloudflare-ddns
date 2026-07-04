#!/usr/bin/env python3
"""Cloudflare DDNS updater for a single public IPv4 A record."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


IPIFY_URL = "https://api.ipify.org"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_SECONDS = 15


class ExitCode(IntEnum):
    """Process exit codes used by systemd and CI diagnostics."""

    SUCCESS = 0
    CONFIG_ERROR = 1
    NETWORK_ERROR = 2
    CLOUDFLARE_ERROR = 3
    UNEXPECTED_ERROR = 4


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class CloudflareError(RuntimeError):
    """Raised when Cloudflare returns an API-level error."""


class NetworkError(RuntimeError):
    """Raised when an external network request fails after retries."""


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from the environment."""

    api_token: str
    zone_id: str
    record_name: str


@dataclass(frozen=True)
class DnsRecord:
    """Subset of Cloudflare DNS record fields needed by this updater."""

    record_id: str
    name: str
    record_type: str
    content: str
    proxied: bool | None


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line for journalctl."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        for key, value in record.__dict__.items():
            if key.startswith("_") and key != "_extra":
                continue
            if key in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
                "taskName",
            }:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging() -> logging.Logger:
    """Configure application logging for stdout/systemd capture."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("cloudflare_ddns")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    return logger


def load_config() -> Config:
    """Load required settings from .env and the process environment."""

    load_dotenv()

    api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    zone_id = os.getenv("CLOUDFLARE_ZONE_ID", "").strip()
    record_name = os.getenv("DNS_RECORD_NAME", "").strip()

    missing = [
        name
        for name, value in {
            "CLOUDFLARE_API_TOKEN": api_token,
            "CLOUDFLARE_ZONE_ID": zone_id,
            "DNS_RECORD_NAME": record_name,
        }.items()
        if not value
    ]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

    return Config(
        api_token=api_token,
        zone_id=zone_id,
        record_name=record_name.rstrip("."),
    )


def build_http_session(api_token: str | None = None) -> requests.Session:
    """Build a retrying HTTP session for transient failures."""

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "PATCH"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    if api_token:
        session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    return session


def parse_public_ipv4(raw_ip: str) -> str:
    """Validate and normalize an IPv4 address, rejecting IPv6 and private ranges."""

    try:
        ip_address = ipaddress.ip_address(raw_ip.strip())
    except ValueError as exc:
        raise NetworkError(f"Public IP service returned an invalid IP address: {raw_ip}") from exc

    if not isinstance(ip_address, ipaddress.IPv4Address):
        raise NetworkError(f"Public IP service returned a non-IPv4 address: {raw_ip}")

    if not ip_address.is_global:
        raise NetworkError(f"Public IP service returned a non-public IPv4 address: {raw_ip}")

    return str(ip_address)


def get_public_ipv4(session: requests.Session) -> str:
    """Fetch the current public IPv4 address from api.ipify.org."""

    try:
        response = session.get(IPIFY_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NetworkError("Unable to detect public IPv4 address") from exc

    return parse_public_ipv4(response.text)


def parse_cloudflare_response(response: requests.Response) -> dict[str, Any]:
    """Decode a Cloudflare JSON response and raise for API errors."""

    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudflareError("Cloudflare returned a non-JSON response") from exc

    if not response.ok or payload.get("success") is not True:
        errors = payload.get("errors") or []
        messages = "; ".join(
            str(error.get("message", error)) for error in errors if isinstance(error, dict)
        )
        if not messages:
            messages = response.text[:500]
        raise CloudflareError(f"Cloudflare API error ({response.status_code}): {messages}")

    return payload


def find_a_record(session: requests.Session, config: Config) -> DnsRecord:
    """Find exactly one Cloudflare A record matching DNS_RECORD_NAME."""

    url = f"{CLOUDFLARE_API_BASE}/zones/{config.zone_id}/dns_records"
    params = {
        "type": "A",
        "name": config.record_name,
        "per_page": 100,
    }

    try:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise NetworkError("Unable to query Cloudflare DNS records") from exc

    payload = parse_cloudflare_response(response)
    records = payload.get("result", [])

    if not records:
        raise CloudflareError(f"No A record found for {config.record_name}")

    exact_records = [
        record
        for record in records
        if record.get("type") == "A" and record.get("name") == config.record_name
    ]

    if len(exact_records) != 1:
        raise CloudflareError(
            f"Expected exactly one A record for {config.record_name}, "
            f"found {len(exact_records)}"
        )

    record = exact_records[0]
    content = str(record.get("content", "")).strip()
    parse_public_ipv4(content)

    return DnsRecord(
        record_id=str(record["id"]),
        name=str(record["name"]),
        record_type=str(record["type"]),
        content=content,
        proxied=record.get("proxied"),
    )


def update_a_record(
    session: requests.Session,
    config: Config,
    record: DnsRecord,
    new_ip: str,
) -> None:
    """Update only the matched Cloudflare A record content."""

    url = f"{CLOUDFLARE_API_BASE}/zones/{config.zone_id}/dns_records/{record.record_id}"
    payload: dict[str, Any] = {
        "type": "A",
        "name": record.name,
        "content": new_ip,
    }

    if record.proxied is not None:
        payload["proxied"] = record.proxied

    try:
        response = session.patch(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise NetworkError("Unable to update Cloudflare DNS record") from exc

    parse_cloudflare_response(response)


def run(logger: logging.Logger) -> ExitCode:
    """Execute one DDNS check/update cycle."""

    try:
        config = load_config()
        ip_session = build_http_session()
        cloudflare_session = build_http_session(config.api_token)

        public_ip = get_public_ipv4(ip_session)
        logger.info("Current public IPv4 detected", public_ip=public_ip)

        record = find_a_record(cloudflare_session, config)
        logger.info(
            "Existing Cloudflare A record inspected",
            dns_record_name=record.name,
            dns_record_id=record.record_id,
            cloudflare_ip=record.content,
        )

        if record.content == public_ip:
            logger.info(
                "Cloudflare A record already matches public IPv4",
                dns_record_name=record.name,
                public_ip=public_ip,
                cloudflare_ip=record.content,
                update_performed=False,
            )
            return ExitCode.SUCCESS

        update_a_record(cloudflare_session, config, record, public_ip)
        logger.info(
            "Cloudflare A record updated",
            dns_record_name=record.name,
            dns_record_id=record.record_id,
            previous_ip=record.content,
            new_ip=public_ip,
            update_performed=True,
        )
        return ExitCode.SUCCESS

    except ConfigError as exc:
        logger.error("Configuration error", error=str(exc))
        return ExitCode.CONFIG_ERROR
    except NetworkError as exc:
        logger.error("Network error", error=str(exc), exc_info=True)
        return ExitCode.NETWORK_ERROR
    except CloudflareError as exc:
        logger.error("Cloudflare error", error=str(exc), exc_info=True)
        return ExitCode.CLOUDFLARE_ERROR
    except Exception as exc:  # pragma: no cover - defensive final boundary.
        logger.error("Unexpected error", error=str(exc), exc_info=True)
        return ExitCode.UNEXPECTED_ERROR


def main() -> int:
    """CLI entry point."""

    logger = configure_logging()
    return int(run(logger))


if __name__ == "__main__":
    sys.exit(main())
