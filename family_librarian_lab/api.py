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

    def admin_request(self, request_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/api/v1/admin/requests/{request_id}")
        _require_status(response, 200, "admin request")
        return _object(response.body, "admin request")

    def list_assets(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/admin/media-assets/")
        _require_status(response, 200, "media assets")
        return _list_field(response.body, "assets", "media assets")

    def publishing_queue(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/admin/publishing/queue")
        _require_status(response, 200, "publishing queue")
        return _object(response.body, "publishing queue")

    def recheck_delivery(self, delivery_id: str) -> None:
        """Audiobookshelf deliveries have no background verification hosted
        service (unlike CWA's CwaVerificationHostedService) -- a delivery
        left Verifying after its one synchronous check stays there until
        this is called explicitly."""
        response = self._request("POST", f"/api/v1/admin/publishing/deliveries/{delivery_id}/recheck")
        _require_status(response, 204, "delivery recheck")

    def recheck_library_import(self, library_import_id: str) -> None:
        response = self._request("POST", f"/api/v1/admin/publishing/library-imports/{library_import_id}/recheck")
        _require_status(response, 204, "library-import recheck")

    def cwa_settings(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/admin/publishing/cwa/")
        _require_status(response, 200, "CWA settings")
        return _object(response.body, "CWA settings")

    def test_cwa_ingest(self, request: dict[str, object]) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/admin/publishing/cwa/test-ingest", json_body=request)
        _require_status(response, 200, "CWA ingest probe")
        return _object(response.body, "CWA ingest probe")

    def set_cwa_sftp_private_key(self, private_key: str) -> dict[str, Any]:
        response = self._request(
            "PUT", "/api/v1/admin/publishing/cwa/sftp-key", json_body={"value": private_key}
        )
        _require_status(response, 200, "CWA SFTP private key")
        return _object(response.body, "CWA settings")

    def set_cwa_sftp_password(self, password: str) -> dict[str, Any]:
        response = self._request(
            "PUT", "/api/v1/admin/publishing/cwa/sftp-password", json_body={"value": password}
        )
        _require_status(response, 200, "CWA SFTP password")
        return _object(response.body, "CWA settings")

    def test_cwa_opds(self, request: dict[str, object]) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/admin/publishing/cwa/test-opds", json_body=request)
        _require_status(response, 200, "CWA OPDS probe")
        return _object(response.body, "CWA OPDS probe")

    def test_cwa(self) -> dict[str, Any]:
        """Tests the *saved* CWA configuration (ingest + OPDS combined) and
        records the result on the settings row -- the only probe that feeds
        the enablement invariant (docs/01 §12.1.1): enabling CWA requires
        LastTestSucceeded to be true for the currently saved configuration.
        Unlike test_cwa_ingest()/test_cwa_opds(), this takes no request body --
        it always tests what's already saved, never a draft/candidate config."""
        response = self._request("POST", "/api/v1/admin/publishing/cwa/test", json_body={})
        _require_status(response, 200, "CWA connection test")
        return _object(response.body, "CWA connection test")

    def audiobookshelf_settings(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/admin/publishing/audiobookshelf/")
        _require_status(response, 200, "Audiobookshelf settings")
        return _object(response.body, "Audiobookshelf settings")

    def test_audiobookshelf(self, request: dict[str, object]) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/admin/publishing/audiobookshelf/test", json_body=request)
        _require_status(response, 200, "Audiobookshelf connection probe")
        return _object(response.body, "Audiobookshelf connection probe")

    def discover_audiobookshelf_libraries(self, request: dict[str, object]) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/admin/publishing/audiobookshelf/libraries", json_body=request)
        _require_status(response, 200, "Audiobookshelf library discovery")
        return _object(response.body, "Audiobookshelf library discovery")

    def create_demo_ebook_request(self) -> tuple[str, str]:
        return self.create_demo_request("Ebook")

    def create_demo_audiobook_request(self) -> tuple[str, str]:
        return self.create_demo_request("Audiobook")

    def create_demo_request(self, media_type: str) -> tuple[str, str]:
        work_id = self.resolve_demo_work()
        created = self._request(
            "POST",
            "/api/v1/requests/",
            json_body={"workId": work_id, "formats": [media_type], "note": None, "confirmDuplicate": True},
        )
        _require_status(created, 201, f"{media_type.lower()} request creation")
        request = _object(created.body, "created request")
        formats = request.get("formats")
        if not isinstance(formats, list):
            raise AssertionError("Created request did not contain formats.")
        format_id = next(
            (item.get("formatId") for item in formats if isinstance(item, dict) and item.get("mediaType") == media_type),
            None,
        )
        if not isinstance(format_id, str):
            raise AssertionError(f"Created request did not contain a {media_type} format.")
        return _required_string(request, "id", "created request"), format_id

    def resolve_demo_work(self, slug: str = "the-hobbit") -> str:
        """Resolve one of DemoBookMetadataProvider's fixed catalog entries
        into a real Work. `slug` defaults to "the-hobbit" (every other
        existing suite's target) -- the gutenberg suite uses
        "project-hail-mary" instead so its Gutenberg-catalog fixtures (built
        with a matching title/author) don't share a Work with unrelated
        suites."""
        work = self._request("POST", f"/api/v1/catalog/candidates/demo/{slug}/resolve", data=b"")
        _require_status(work, (200, 201), "demo catalog work resolution")
        return _required_string(_object(work.body, "catalog work"), "id", "catalog work")

    def fulfillment_options(self, work_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/api/v1/catalog/works/{work_id}/fulfillment-options")
        _require_status(response, 200, "work fulfillment options")
        return _object(response.body, "work fulfillment options")

    def gutenberg_status(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/admin/gutenberg/status")
        _require_status(response, 200, "gutenberg catalog status")
        return _object(response.body, "gutenberg catalog status")

    def gutenberg_sync(self) -> ApiResponse:
        """Trigger a Gutenberg catalog sync (incremental if the catalog is
        already ready, full otherwise -- GutenbergCatalogEndpoints' own
        rule). 409 if one is already in progress; 502 on failure -- callers
        decide what each status means for their scenario."""
        return self._request("POST", "/api/v1/admin/gutenberg/sync", data=b"")

    def gutenberg_purge(self) -> ApiResponse:
        return self._request("DELETE", "/api/v1/admin/gutenberg/catalog")

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> ApiResponse:
        """GutenbergProvider.FindDirectAcquisitionsAsync (and every other
        IAutomaticDirectAcquisitionProvider) checks `ProviderState.IsUsable`
        before ever searching -- a synced catalog with real matching data
        still returns zero fulfillment options until this is called, same
        registered/enabled gate CWA/ABS destinations already need
        (confirmed against real code: GutenbergProvider.cs's
        FindDirectAcquisitionsAsync)."""
        return self._request(
            "PUT", f"/api/v1/admin/integrations/metadata/{provider_id}/enabled", json_body={"enabled": enabled}
        )

    def upload_manual_epub(self, request_id: str, format_id: str, content: bytes, filename: str) -> ApiResponse:
        return self._upload_manual_file(request_id, format_id, content, filename)

    def upload_manual_audio(self, request_id: str, format_id: str, content: bytes, filename: str) -> ApiResponse:
        return self._upload_manual_file(request_id, format_id, content, filename)

    def _upload_manual_file(self, request_id: str, format_id: str, content: bytes, filename: str) -> ApiResponse:
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

    def configure_cwa_local(
        self,
        *,
        local_ingest_path: str,
        opds_base_url: str,
        opds_username: str,
        opds_password: str,
    ) -> dict[str, Any]:
        settings = self._request(
            "PUT",
            "/api/v1/admin/publishing/cwa/",
            json_body={
                "transportMode": "Local",
                "localIngestPath": local_ingest_path,
                "sftpHost": None,
                "sftpPort": None,
                "sftpUsername": None,
                "sftpIngestPath": None,
                "sftpAuthenticationMode": "PrivateKey",
                "opdsBaseUrl": opds_base_url,
                "opdsUsername": opds_username,
            },
        )
        _require_status(settings, 200, "CWA settings")
        password = self._request(
            "PUT", "/api/v1/admin/publishing/cwa/opds-password", json_body={"value": opds_password}
        )
        _require_status(password, 200, "CWA OPDS password")

        # Enabling requires a passing test for this exact saved configuration
        # (docs/01 §12.1.1's enablement invariant) -- test_cwa_ingest()/
        # test_cwa_opds() above don't count, they only probe drafts.
        probe = self.test_cwa()
        if not probe.get("succeeded"):
            raise AssertionError(f"CWA connection test did not succeed before enabling: {probe!r}")

        enabled = self._request(
            "PUT", "/api/v1/admin/publishing/cwa/enabled", json_body={"enabled": True}
        )
        _require_status(enabled, 200, "CWA enable")
        return _object(enabled.body, "CWA settings")

    def configure_cwa_sftp(
        self,
        *,
        sftp_host: str,
        sftp_port: int,
        sftp_username: str,
        sftp_ingest_path: str,
        auth_mode: str,
        credential: str,
        opds_base_url: str,
        opds_username: str,
        opds_password: str,
    ) -> dict[str, Any]:
        """Configures CWA's SFTP transport and drives the same trust-on-
        first-test flow an administrator would (design doc CWA-S-01): probe
        with no trusted fingerprint yet (expect a rejection carrying the
        server's observed fingerprint, no file transferred), trust it, then
        probe again (expect a real connect plus a temporary write-and-remove
        probe on the remote ingest path). Returns both probe responses plus
        the final enabled settings so a case can assert on the trust flow
        itself, not just the end-to-end publish it enables."""
        settings = self._request(
            "PUT",
            "/api/v1/admin/publishing/cwa/",
            json_body={
                "transportMode": "Sftp",
                "localIngestPath": None,
                "sftpHost": sftp_host,
                "sftpPort": sftp_port,
                "sftpUsername": sftp_username,
                "sftpIngestPath": sftp_ingest_path,
                "sftpAuthenticationMode": auth_mode,
                "opdsBaseUrl": opds_base_url,
                "opdsUsername": opds_username,
            },
        )
        _require_status(settings, 200, "CWA SFTP settings")

        secret_path = "sftp-key" if auth_mode == "PrivateKey" else "sftp-password"
        secret = self._request(
            "PUT", f"/api/v1/admin/publishing/cwa/{secret_path}", json_body={"value": credential}
        )
        _require_status(secret, 200, "CWA SFTP credential")

        opds_password_response = self._request(
            "PUT", "/api/v1/admin/publishing/cwa/opds-password", json_body={"value": opds_password}
        )
        _require_status(opds_password_response, 200, "CWA OPDS password")

        def probe(trusted_fingerprint: str | None) -> dict[str, Any]:
            response = self._request(
                "POST",
                "/api/v1/admin/publishing/cwa/test-ingest",
                json_body={
                    "transportMode": "Sftp",
                    "localIngestPath": None,
                    "sftpHost": sftp_host,
                    "sftpPort": sftp_port,
                    "sftpUsername": sftp_username,
                    "sftpIngestPath": sftp_ingest_path,
                    "sftpAuthenticationMode": auth_mode,
                    "sftpPrivateKey": credential if auth_mode == "PrivateKey" else None,
                    "sftpPassphrase": None,
                    "sftpPassword": credential if auth_mode == "Password" else None,
                    "trustedSftpHostKeyFingerprint": trusted_fingerprint,
                },
            )
            _require_status(response, 200, "CWA SFTP ingest probe")
            return _object(response.body, "CWA SFTP ingest probe")

        untrusted_probe = probe(None)
        fingerprint = untrusted_probe.get("sftpHostKeyFingerprint")
        if not untrusted_probe.get("requiresSftpHostKeyTrust") or not isinstance(fingerprint, str):
            raise AssertionError(
                "Expected the first SFTP probe to reject an untrusted host key and report its "
                f"fingerprint; got {untrusted_probe!r}."
            )

        trust = self._request(
            "PUT", "/api/v1/admin/publishing/cwa/sftp-host-key", json_body={"fingerprint": fingerprint}
        )
        _require_status(trust, 200, "CWA SFTP host-key trust")

        trusted_probe = probe(fingerprint)
        if not trusted_probe.get("succeeded"):
            raise AssertionError(f"SFTP ingest probe failed after trusting its host key: {trusted_probe!r}")

        # Enabling requires a passing test for this exact saved configuration
        # (docs/01 §12.1.1's enablement invariant) -- the ingest probes above
        # test drafts and don't count, and the OPDS side has never been probed
        # against the saved settings yet either.
        connection_test = self.test_cwa()
        if not connection_test.get("succeeded"):
            raise AssertionError(f"CWA connection test did not succeed before enabling: {connection_test!r}")

        enabled = self._request("PUT", "/api/v1/admin/publishing/cwa/enabled", json_body={"enabled": True})
        _require_status(enabled, 200, "CWA enable")

        return {
            "settings": _object(enabled.body, "CWA settings"),
            "untrusted_probe": untrusted_probe,
            "trusted_probe": trusted_probe,
            "connection_test": connection_test,
        }

    def configure_audiobookshelf(
        self,
        *,
        base_url: str,
        library_id: str,
        folder_id: str,
        api_token: str,
    ) -> dict[str, Any]:
        settings = self._request(
            "PUT",
            "/api/v1/admin/publishing/audiobookshelf/",
            json_body={"baseUrl": base_url, "libraryId": library_id, "folderId": folder_id},
        )
        _require_status(settings, 200, "Audiobookshelf settings")
        token = self._request(
            "PUT", "/api/v1/admin/publishing/audiobookshelf/api-token", json_body={"value": api_token}
        )
        _require_status(token, 200, "Audiobookshelf API token")

        # Unlike CWA, AudiobookshelfSettingsService.SetEnabledAsync does not
        # itself require a passing test, so skipping this would still "enable"
        # successfully -- but FormatReadinessService's request-creation gate
        # requires LastTestSucceeded regardless, so a request would then be
        # permanently rejected ("Test the connection...") despite IsEnabled
        # already being true. Test the just-saved config for real before
        # enabling, same as configure_cwa_local().
        tested = self._request("POST", "/api/v1/admin/publishing/audiobookshelf/test", json_body={})
        _require_status(tested, 200, "Audiobookshelf connection test")
        probe = _object(tested.body, "Audiobookshelf connection test")
        if not probe.get("succeeded"):
            raise AssertionError(f"Audiobookshelf connection test did not succeed before enabling: {probe!r}")

        enabled = self._request(
            "PUT", "/api/v1/admin/publishing/audiobookshelf/enabled", json_body={"enabled": True}
        )
        _require_status(enabled, 200, "Audiobookshelf enable")
        return _object(enabled.body, "Audiobookshelf settings")

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
