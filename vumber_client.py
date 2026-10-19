"""Small client for the endpoints used by Vumber's own web dashboard."""

from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken


AUTH_URL = "https://api.vumber.telephony.meteormobile.net/authentication/v1/token"
API_URL = "https://api.vumber.telephony.meteormobile.net"
CLIENT_ID = "ngAuthProd"
FERNET_PREFIX = b"FOP1"


class VumberError(RuntimeError):
    """A safe, user-facing Vumber error."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes) -> bytes:
    configured_key = os.environ.get("CRM_DATA_ENCRYPTION_KEY", "").strip()
    if configured_key:
        key = base64.urlsafe_b64encode(hashlib.sha256(configured_key.encode("utf-8")).digest())
        return FERNET_PREFIX + Fernet(key).encrypt(data)
    if os.name != "nt":
        raise VumberError("Set CRM_DATA_ENCRYPTION_KEY before connecting Vumber on this host.")
    source = ctypes.create_string_buffer(data)
    source_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(source_blob), "Vumber CRM", None, None, None, 0,
        ctypes.byref(output_blob),
    ):
        raise VumberError("Windows could not protect the Vumber session.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if data.startswith(FERNET_PREFIX):
        configured_key = os.environ.get("CRM_DATA_ENCRYPTION_KEY", "").strip()
        if not configured_key:
            raise VumberError("CRM_DATA_ENCRYPTION_KEY is required to open the saved Vumber session.")
        key = base64.urlsafe_b64encode(hashlib.sha256(configured_key.encode("utf-8")).digest())
        try:
            return Fernet(key).decrypt(data[len(FERNET_PREFIX):])
        except InvalidToken as exc:
            raise VumberError("CRM_DATA_ENCRYPTION_KEY does not match the saved Vumber session.") from exc
    if os.name != "nt":
        raise VumberError("This Windows-encrypted Vumber session cannot be moved. Reconnect Vumber here.")
    source = ctypes.create_string_buffer(data)
    source_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source_blob), None, None, None, None, 0,
        ctypes.byref(output_blob),
    ):
        raise VumberError("The saved Vumber session could not be opened by this Windows user.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


class VumberSessionStore:
    """Stores tokens with a host secret, or Windows DPAPI when no secret is set."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(_unprotect(self.path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise VumberError("The saved Vumber connection is damaged. Reconnect Vumber.") from exc

    def save(self, session: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        protected = _protect(json.dumps(session, separators=(",", ":")).encode("utf-8"))
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(protected)
        temporary.replace(self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class VumberClient:
    def __init__(
        self,
        store: VumberSessionStore,
        http: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self.store = store
        self.http = http or requests.Session()
        self.timeout = timeout
        self._configuration: dict[str, Any] | None = None
        self._configuration_at = 0.0

    @property
    def connected(self) -> bool:
        try:
            session = self.store.load()
            return bool(session and session.get("access_token") and session.get("account_no"))
        except VumberError:
            return False

    def connect(self, username: str, password: str) -> dict[str, Any]:
        payload = {
            "grantType": "password", "userName": username.strip(),
            "password": password, "clientId": CLIENT_ID, "rememberMe": True,
        }
        response = self.http.post(AUTH_URL, json=payload, timeout=self.timeout)
        data = self._json(response, "Vumber sign-in failed")
        token = data.get("accessToken")
        if not token:
            raise VumberError("Vumber did not return a usable sign-in session.")
        user = self._authorized("GET", "/account-info/v1/info", token=token)
        account_no = user.get("accountNo") or user.get("accountId")
        if not account_no:
            raise VumberError("Vumber signed in, but the account number was not returned.")
        session = {
            "access_token": token,
            "refresh_token": data.get("refreshToken", ""),
            "expires_at": time.time() + int(data.get("expiresIn") or 3600),
            "account_no": str(account_no),
            "username": user.get("userName") or username.strip(),
        }
        self.store.save(session)
        self._configuration = None
        return {"account_no": session["account_no"], "username": session["username"]}

    def disconnect(self) -> None:
        self.store.clear()
        self._configuration = None

    def status(self) -> dict[str, Any]:
        session = self.store.load()
        if not session:
            return {"connected": False}
        return {
            "connected": bool(session.get("access_token") and session.get("account_no")),
            "account_no": session.get("account_no", ""),
            "username": session.get("username", ""),
        }

    def configuration(self, force: bool = False) -> dict[str, Any]:
        if not force and self._configuration and time.time() - self._configuration_at < 60:
            return self._configuration
        session = self._session()
        account = session["account_no"]
        sms = self._authorized(
            "GET", f"/message/v1/account/{account}/virtual-numbers",
            params={"accountNo": account, "virtualNumber": "All Vumbers"},
        )
        calls = self._authorized(
            "GET", f"/call/v1/account/{account}/make-call",
            params={"accountno": account, "publicnumber": "All Vumbers"},
        )
        result = {
            "sms_numbers": self._options(sms.get("smsEnabledVirtualNumbers", [])),
            "call_numbers": self._options(calls.get("availableVirtualNumbers", [])),
            "callback_numbers": self._options(calls.get("availableForwardingNumbers", [])),
        }
        self._configuration, self._configuration_at = result, time.time()
        return result

    def upload_sms_image(self, virtual_number: str, image_path: Path | str) -> str:
        """Upload MMS media using the dashboard's multipart contract."""
        import mimetypes
        image_path = Path(image_path)
        mime = mimetypes.guess_type(image_path.name)[0] or ''
        if mime not in {'image/jpeg', 'image/png', 'image/gif'}:
            raise VumberError('Use a JPEG, PNG, or GIF image.')
        if not image_path.is_file() or image_path.stat().st_size > 498000:
            raise VumberError('The SMS image must exist and be smaller than 498 KB.')
        session = self._session()
        account = session['account_no']
        with image_path.open('rb') as image:
            response = self.http.post(
                API_URL + f'/virtual-number/v1/account/{account}/upload',
                headers={'Authorization': f"Bearer {session['access_token']}"},
                data={'virtualNumber': self._digits(virtual_number), 'fileType': '10'},
                files={'FileName[]': (image_path.name, image, mime)}, timeout=self.timeout,
            )
        result = self._json(response, 'Vumber image upload failed')
        upload_id = result.get('uploadId')
        if not upload_id:
            raise VumberError('Vumber did not return an image upload ID.')
        return str(upload_id)

    def send_sms(self, recipient: str, virtual_number: str, message: str,
                 upload_ids: list[str] | None = None) -> dict[str, Any]:
        session = self._session()
        account = session["account_no"]
        payload = {
            "accountNo": account, "recipient": self._digits(recipient),
            "virtualNumber": self._digits(virtual_number), "message": message.strip(),
            "uploadIds": ','.join(str(value) for value in (upload_ids or [])),
        }
        result = self._authorized("POST", f"/message/v1/account/{account}/sms", json=payload)
        if str(result.get("errorName", "0")) not in {"0", "None", ""}:
            raise VumberError(result.get("errorMessage") or "Vumber could not send the text.")
        return result

    def make_call(
        self, recipient: str, virtual_number: str, callback_number: str,
    ) -> dict[str, Any]:
        session = self._session()
        account = session["account_no"]
        payload = {
            "accountNo": account, "personalNumber": self._digits(recipient),
            "virtualNumber": self._digits(virtual_number),
            "callbacknumber": self._digits(callback_number),
        }
        return self._authorized("POST", f"/call/v1/account/{account}/make-call", json=payload)

    def _session(self) -> dict[str, Any]:
        session = self.store.load()
        if not session:
            raise VumberError("Connect Vumber first.")
        if float(session.get("expires_at") or 0) <= time.time() + 60:
            session = self._refresh(session)
        return session

    def _refresh(self, session: dict[str, Any]) -> dict[str, Any]:
        if not session.get("refresh_token"):
            raise VumberError("The Vumber session expired. Reconnect Vumber.")
        response = self.http.post(
            AUTH_URL, data=json.dumps({"grantType": "refresh_token"}),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {session['access_token']}",
            },
            timeout=self.timeout,
        )
        data = self._json(response, "The Vumber session expired. Reconnect Vumber")
        session.update({
            "access_token": data.get("accessToken", session["access_token"]),
            "refresh_token": data.get("refreshToken", session["refresh_token"]),
            "expires_at": time.time() + int(data.get("expiresIn") or 3600),
        })
        self.store.save(session)
        return session

    def _authorized(self, method: str, path: str, token: str | None = None, **kwargs) -> dict[str, Any]:
        if token is None:
            token = self._session()["access_token"]
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Content-Type", "application/json")
        response = self.http.request(method, API_URL + path, headers=headers, timeout=self.timeout, **kwargs)
        return self._json(response, "Vumber request failed")

    @staticmethod
    def _json(response: requests.Response, message: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise VumberError(f"{message} (HTTP {response.status_code}).") from exc
        if not response.ok:
            detail = data.get("error_description") or data.get("message") or data.get("errorMessage")
            raise VumberError(str(detail or f"{message} (HTTP {response.status_code})."))
        if not isinstance(data, dict):
            raise VumberError(f"{message}: unexpected response.")
        return data

    @staticmethod
    def _options(values: Any) -> list[dict[str, str]]:
        result = []
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict):
                value = str(item.get("value") or item.get("number") or "")
                label = str(item.get("text") or item.get("label") or value)
            else:
                value = label = str(item)
            if value:
                result.append({"value": value, "label": label})
        return result

    @staticmethod
    def _digits(value: str) -> str:
        digits = "".join(character for character in str(value) if character.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise VumberError("Use a valid 10-digit US phone number.")
        return digits
