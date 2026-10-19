"""Formal communication provider boundary for SMS and calls.

Providers implement the SmsProvider / CallProvider protocols. The Vumber
adapters wrap the existing Windows-encrypted VumberClient so the CRM never
calls provider internals directly.
"""

from __future__ import annotations

from typing import Any, Protocol

from vumber_client import VumberClient, VumberError


class SmsProvider(Protocol):
    name: str

    def send_sms(self, recipient: str, from_number: str, message: str,
                 upload_ids: list[str] | None = None) -> dict[str, Any]: ...

    def upload_sms_image(self, from_number: str, image_path: str) -> str: ...

    def sms_lines(self) -> list[dict[str, str]]: ...


class CallProvider(Protocol):
    name: str

    def place_callback_call(
        self, recipient: str, from_number: str, callback_number: str
    ) -> dict[str, Any]: ...

    def call_lines(self) -> list[dict[str, str]]: ...

    def callback_lines(self) -> list[dict[str, str]]: ...


class VumberSmsProvider:
    """SMS provider adapter over the Vumber session client."""

    name = "vumber"

    def __init__(self, client: VumberClient):
        self._client = client

    def send_sms(self, recipient: str, from_number: str, message: str,
                 upload_ids: list[str] | None = None) -> dict[str, Any]:
        if upload_ids:
            return self._client.send_sms(recipient, from_number, message, upload_ids)
        return self._client.send_sms(recipient, from_number, message)

    def upload_sms_image(self, from_number: str, image_path: str) -> str:
        return self._client.upload_sms_image(from_number, image_path)

    def sms_lines(self) -> list[dict[str, str]]:
        return self._client.configuration(force=True)["sms_numbers"]


class VumberCallProvider:
    """Callback-call provider adapter over the Vumber session client."""

    name = "vumber"

    def __init__(self, client: VumberClient):
        self._client = client

    def place_callback_call(
        self, recipient: str, from_number: str, callback_number: str
    ) -> dict[str, Any]:
        return self._client.make_call(recipient, from_number, callback_number)

    def call_lines(self) -> list[dict[str, str]]:
        return self._client.configuration(force=True)["call_numbers"]

    def callback_lines(self) -> list[dict[str, str]]:
        return self._client.configuration(force=True)["callback_numbers"]


def build_providers(client: VumberClient) -> tuple[VumberSmsProvider, VumberCallProvider]:
    """Return the SMS and call provider adapters for the given client."""
    return VumberSmsProvider(client), VumberCallProvider(client)


__all__ = [
    "SmsProvider",
    "CallProvider",
    "VumberSmsProvider",
    "VumberCallProvider",
    "VumberError",
    "build_providers",
]
