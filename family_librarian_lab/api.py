"""Thin cookie-authenticated HTTP client for Family Librarian's public API."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: Any


class FamilyLibrarianApi:
    """Exercise the deployed host exactly as a cookie-authenticated browser does."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._csrf_token: str | None = None
        self.trace: list[dict[str, object]] = []

    def authenticate(self, email: str, password: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/auth/login",
            json_body={"email": email, "password": password, "rememberMe": False},
        )
        _require_status(response, 204, "bootstrap administrator login")
        token = self._request("GET", "/api/v1/antiforgery/token")
        _require_status(token, 200, "anti-forgery token")
        self._csrf_token = _object(token.body, "anti-forgery token")["token"]
        return self.me()

    def me(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/me")
        _require_status(response, 200, "current user")
        return _object(response.body, "current user")

    def list_accounts(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/admin/accounts/")
        _require_status(response, 200, "accounts")
        return _list_field(response.body, "accounts", "accounts")

    def list_requests(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/admin/requests/")
        _require_status(response, 200, "admin requests")
        return _list_field(response.body, "requests", "admin requests")

    def list_assets(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/admin/media-assets/")
        _require_status(response, 200, "media assets")
        return _list_field(response.body, "assets", "media assets")

    def publishing_queue(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/admin/publishing/queue")
        _require_status(response, 200, "publishing queue")
        return _object(response.body, "publishing queue")

    def create_demo_ebook_request(self) -> tuple[str, str]:
        work = self._request("POST", "/api/v1/catalog/candidates/demo/the-hobbit/resolve", data=b"")
        _require_status(work, (200, 201), "demo catalog work resolution")
        work_id = _object(work.body, "catalog work")["id"]
        created = self._request(
            "POST",
            "/api/v1/requests/",
            json_body={"workId": work_id, "formats": ["Ebook"], "note": None, "confirmDuplicate": True},
        )
        _require_status(created, 201, "ebook request creation")
        request = _object(created.body, "created request")
        formats = request.get("formats")
        if not isinstance(formats, list):
            raise AssertionError("Created request did not contain formats.")
        format_id = next(
            (item.get("formatId") for item in formats if isinstance(item, dict) and item.get("mediaType") == "Ebook"),
            None,
        )
        if not isinstance(format_id, str):
            raise AssertionError("Created request did not contain an ebook format.")
        return _required_string(request, "id", "created request"), format_id

    def upload_manual_epub(self, request_id: str, format_id: str, content: bytes, filename: str) -> ApiResponse:
        boundary = f"----family-librarian-lab-{uuid.uuid4().hex}"
        payload = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        return self._request(
            "POST",
            f"/api/v1/admin/requests/{request_id}/formats/{format_id}/manual-import",
            data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        if json_body is not None:
            if data is not None:
                raise ValueError("Specify either json_body or data, not both.")
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers = {**(headers or {}), "Content-Type": "application/json"}
        request_headers = {"Accept": "application/json", **(headers or {})}
        if method not in {"GET", "HEAD", "OPTIONS", "TRACE"} and self._csrf_token is not None:
            request_headers["X-CSRF-TOKEN"] = self._csrf_token
        request = Request(self._base_url + path, data=data, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=30) as response:  # local Compose URL supplied by the lab
                status = response.status
                raw = response.read()
        except HTTPError as error:
            status = error.code
            raw = error.read()
        body = _decode_body(raw)
        self.trace.append({"method": method, "path": path, "status": status})
        return ApiResponse(status=status, body=body)


def _decode_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"non_json_bytes": len(raw)}


def _require_status(response: ApiResponse, expected: int | tuple[int, ...], action: str) -> None:
    statuses = (expected,) if isinstance(expected, int) else expected
    if response.status not in statuses:
        raise AssertionError(f"{action} returned HTTP {response.status}; expected {statuses}.")


def _object(body: Any, label: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AssertionError(f"{label} response was not a JSON object.")
    return body


def _list_field(body: Any, field: str, label: str) -> list[dict[str, Any]]:
    value = _object(body, label).get(field)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AssertionError(f"{label} response did not contain a {field} array.")
    return value


def _required_string(body: dict[str, Any], key: str, label: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise AssertionError(f"{label} did not contain a string {key}.")
    return value
