from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .knowledge_assistant.documents import MAX_DOCUMENT_BYTES


_SCHEMA_VERSION = 2
_ALLOWED_OPERATION = "create_ingestion_job"
_ENTRY_ID = re.compile(r"^outbox-[a-f0-9]{32}$")
_TEMPORARY_ENTRY = re.compile(r"^\.(outbox-[a-f0-9]{32})\.[a-f0-9]{32}\.tmp$")
_ENTROPY = b"TableMiku.KnowledgeAssistant.IngestionOutbox.v1"
_WINDOWS_DPAPI_AVAILABLE = os.name == "nt"
DEFAULT_MAX_OUTBOX_ENTRIES = 200
DEFAULT_MAX_OUTBOX_DISK_BYTES = 256 * 1024 * 1024


def _replace_with_write_through(source: Path, destination: Path) -> None:
    """Replace one outbox record and request write-through where Windows supports it."""

    if os.name != "nt":
        os.replace(source, destination)
        return
    move_file_ex = ctypes.windll.kernel32.MoveFileExW
    move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong)
    move_file_ex.restype = ctypes.c_int
    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    if not move_file_ex(
        str(source),
        str(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError()


class DataProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte)))


class WindowsCurrentUserProtector:
    """Protect data with Windows DPAPI bound to the current OS user."""

    _UI_FORBIDDEN = 0x1

    @staticmethod
    def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(value)
        blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    @staticmethod
    def _crypt(value: bytes, *, protect: bool) -> bytes:
        if not _WINDOWS_DPAPI_AVAILABLE:
            raise OSError("Windows DPAPI is unavailable; ingestion recovery fails closed")
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        )
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        )
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p
        source, source_buffer = WindowsCurrentUserProtector._blob(value)
        entropy, entropy_buffer = WindowsCurrentUserProtector._blob(_ENTROPY)
        output = _DataBlob()
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        description = ctypes.c_wchar_p()
        if protect:
            arguments = (
                ctypes.byref(source),
                "Table Miku ingestion outbox",
                ctypes.byref(entropy),
                None,
                None,
                WindowsCurrentUserProtector._UI_FORBIDDEN,
                ctypes.byref(output),
            )
        else:
            arguments = (
                ctypes.byref(source),
                ctypes.byref(description),
                ctypes.byref(entropy),
                None,
                None,
                WindowsCurrentUserProtector._UI_FORBIDDEN,
                ctypes.byref(output),
            )
        if not function(*arguments):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)
            if description.value:
                kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
            del source_buffer, entropy_buffer

    def protect(self, value: bytes) -> bytes:
        return self._crypt(value, protect=True)

    def unprotect(self, value: bytes) -> bytes:
        return self._crypt(value, protect=False)


@dataclass(frozen=True)
class OutboxEntry:
    entry_id: str
    created_at: str
    path: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class OutboxScanResult:
    entry_id: str
    created_at: str
    path: Path
    payload: dict[str, Any] | None
    error: str = ""


class SecureIngestionOutbox:
    """Durably encrypt exact ingestion requests before any network write."""

    def __init__(
        self,
        directory: Path,
        *,
        protector: DataProtector | None = None,
        max_content_bytes: int = MAX_DOCUMENT_BYTES,
        max_entries: int = DEFAULT_MAX_OUTBOX_ENTRIES,
        max_disk_bytes: int = DEFAULT_MAX_OUTBOX_DISK_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self.protector = protector or WindowsCurrentUserProtector()
        self.max_content_bytes = int(max_content_bytes)
        self.max_entries = int(max_entries)
        self.max_disk_bytes = int(max_disk_bytes)
        if self.max_entries < 1 or self.max_disk_bytes < 1:
            raise ValueError("outbox limits must be positive")

    def enqueue(self, request: dict[str, object]) -> OutboxEntry:
        payload = self._validated_request(request)
        entry_id = f"outbox-{uuid.uuid4().hex}"
        created_at = self._now()
        payload.update(
            {
                "schema_version": _SCHEMA_VERSION,
                "entry_id": entry_id,
                "created_at": created_at,
                "state": "pending",
                "cancel_after_submit": False,
                "cancel_delivery_state": "none",
            }
        )
        self._write(entry_id, created_at, payload, is_new=True)
        return self.load(entry_id)

    def load(self, entry_id: str) -> OutboxEntry:
        return self._load(entry_id, decode_content=True)

    def _load(self, entry_id: str, *, decode_content: bool) -> OutboxEntry:
        path = self._path(entry_id)
        envelope = self._read_envelope(path, expected_id=entry_id)
        protected = base64.b64decode(envelope["ciphertext"], validate=True)
        raw = self.protector.unprotect(protected)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("outbox payload must be an object")
        self._validate_stored_payload(payload, entry_id)
        if payload.get("created_at") != envelope.get("created_at"):
            raise ValueError("outbox creation time mismatch")
        public_payload = dict(payload)
        if decode_content and "content_base64" in public_payload:
            public_payload["content"] = self._decode_content(
                str(public_payload.pop("content_base64"))
            )
        return OutboxEntry(
            entry_id=entry_id,
            created_at=str(envelope["created_at"]),
            path=path,
            payload=public_payload,
        )

    def load_for_replay(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        entry = self.load_for_binding(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
        )
        if entry.payload.get("state") != "pending":
            raise ValueError("only pending outbox requests can be replayed")
        return entry

    def mark_submitted(self, entry_id: str, *, job_id: str) -> OutboxEntry:
        current = self.load(entry_id)
        safe_job_id = str(job_id).strip()
        if not safe_job_id:
            raise ValueError("job_id is required")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "entry_id": entry_id,
            "created_at": current.created_at,
            "state": "tracking",
            "service_instance_id": current.payload["service_instance_id"],
            "principal": current.payload["principal"],
            "filename": current.payload["filename"],
            "collection_id": current.payload["collection_id"],
            "job_id": safe_job_id,
            "payload_sha256": current.payload["payload_sha256"],
            "cancel_after_submit": bool(current.payload["cancel_after_submit"]),
            "cancel_delivery_state": str(current.payload["cancel_delivery_state"]),
        }
        self._write(entry_id, current.created_at, payload, is_new=False)
        return self.load(entry_id)

    def create_cancel_tracking(
        self,
        *,
        service_instance_id: str,
        principal: object,
        job_id: str,
        filename: str,
        collection_id: str,
    ) -> OutboxEntry:
        binding_id = str(service_instance_id).strip()
        safe_job_id = str(job_id).strip()
        safe_filename = str(filename).strip()
        safe_collection_id = str(collection_id).strip()
        if not binding_id or not safe_job_id:
            raise ValueError("service_instance_id and job_id are required")
        if not safe_filename or not safe_collection_id:
            raise ValueError("filename and collection_id are required for cancel tracking")
        normalized_principal = self._normalized_principal(principal)
        digest_fields = {
            "purpose": "cancel_ingestion_job",
            "service_instance_id": binding_id,
            "principal": normalized_principal,
            "filename": safe_filename,
            "collection_id": safe_collection_id,
            "job_id": safe_job_id,
        }
        entry_id = f"outbox-{uuid.uuid4().hex}"
        created_at = self._now()
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "entry_id": entry_id,
            "created_at": created_at,
            "state": "tracking",
            "service_instance_id": binding_id,
            "principal": normalized_principal,
            "filename": safe_filename,
            "collection_id": safe_collection_id,
            "job_id": safe_job_id,
            "payload_sha256": self._digest(digest_fields),
            "cancel_after_submit": True,
            "cancel_delivery_state": "requested",
        }
        self._write(entry_id, created_at, payload, is_new=True)
        return self.load(entry_id)

    def mark_cancel_requested(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        current = self.load_for_binding(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
        )
        if current.payload.get("state") not in {"pending", "tracking"}:
            raise ValueError("cancel intent requires a pending or tracking record")
        payload = dict(current.payload)
        payload.pop("content", None)
        if current.payload.get("state") == "pending":
            payload["content_base64"] = base64.b64encode(
                bytes(current.payload["content"])
            ).decode("ascii")
        payload["cancel_after_submit"] = True
        payload["cancel_delivery_state"] = "requested"
        self._write(entry_id, current.created_at, payload, is_new=False)
        return self.load(entry_id)

    def mark_cancel_delivering(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        return self._mark_cancel_delivery_state(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
            delivery_state="delivering",
        )

    def mark_cancel_rejected(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        return self._mark_cancel_delivery_state(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
            delivery_state="rejected",
        )

    def mark_cancel_delivered(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        return self._mark_cancel_delivery_state(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
            delivery_state="delivered",
        )

    def mark_cancel_unknown(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        return self._mark_cancel_delivery_state(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
            delivery_state="unknown",
        )

    def _mark_cancel_delivery_state(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
        delivery_state: str,
    ) -> OutboxEntry:
        current = self.load_for_binding(
            entry_id,
            service_instance_id=service_instance_id,
            principal=principal,
        )
        if current.payload.get("state") != "tracking":
            raise ValueError("cancel rejection requires a tracking record")
        payload = dict(current.payload)
        payload["cancel_after_submit"] = True
        payload["cancel_delivery_state"] = delivery_state
        self._write(entry_id, current.created_at, payload, is_new=False)
        return self.load(entry_id)

    def load_for_binding(
        self,
        entry_id: str,
        *,
        service_instance_id: str,
        principal: object,
    ) -> OutboxEntry:
        entry = self.load(entry_id)
        if entry.payload.get("service_instance_id") != str(service_instance_id).strip():
            raise PermissionError("outbox request belongs to a different service instance")
        if entry.payload.get("principal") != self._normalized_principal(principal):
            raise PermissionError("outbox request belongs to a different applied identity")
        return entry

    def delete(self, entry_id: str) -> None:
        path = self._path(entry_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            self._sync_directory_best_effort()

    def scan(self) -> list[OutboxScanResult]:
        if not self.directory.exists():
            return []
        results: list[OutboxScanResult] = []
        files = self._storage_files()
        inspected_bytes = 0
        for index, path in enumerate(files):
            temporary_match = _TEMPORARY_ENTRY.fullmatch(path.name)
            entry_id = temporary_match.group(1) if temporary_match else path.stem
            created_at = ""
            try:
                inspected_bytes += path.stat().st_size
            except OSError:
                pass
            if index >= self.max_entries or inspected_bytes > self.max_disk_bytes:
                results.append(
                    OutboxScanResult(
                        entry_id=entry_id,
                        created_at="",
                        path=path,
                        payload=None,
                        error="outbox safety limit exceeded",
                    )
                )
                continue
            if temporary_match:
                results.append(
                    OutboxScanResult(
                        entry_id=entry_id,
                        created_at=self._safe_created_at(path),
                        path=path,
                        payload=None,
                        error="interrupted outbox replacement retained",
                    )
                )
                continue
            try:
                entry = self._load(entry_id, decode_content=False)
            except Exception as exc:
                created_at = self._safe_created_at(path)
                results.append(
                    OutboxScanResult(
                        entry_id=entry_id,
                        created_at=created_at,
                        path=path,
                        payload=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                payload = entry.payload
                results.append(
                    OutboxScanResult(
                        entry_id=entry.entry_id,
                        created_at=entry.created_at,
                        path=entry.path,
                        payload={
                            "state": str(payload.get("state") or ""),
                            "service_instance_id": str(
                                payload.get("service_instance_id") or ""
                            ),
                            "principal": payload.get("principal"),
                            "filename": str(payload.get("filename") or ""),
                            "collection_id": str(payload.get("collection_id") or ""),
                            "job_id": str(payload.get("job_id") or ""),
                            "cancel_after_submit": bool(
                                payload.get("cancel_after_submit", False)
                            ),
                            "cancel_delivery_state": (
                                "unknown"
                                if payload.get("cancel_delivery_state") == "delivering"
                                else str(payload.get("cancel_delivery_state") or "none")
                            ),
                        },
                    )
                )
        return results

    def _validated_request(self, request: dict[str, object]) -> dict[str, Any]:
        allowed = {
            "operation",
            "service_instance_id",
            "principal",
            "filename",
            "collection_id",
            "content",
            "idempotency_key",
        }
        if set(request) != allowed:
            raise ValueError("outbox request contains missing or unsupported fields")
        if request.get("operation") != _ALLOWED_OPERATION:
            raise ValueError("unsupported outbox operation")
        service_instance_id = str(request.get("service_instance_id") or "").strip()
        filename = str(request.get("filename") or "").strip()
        collection_id = str(request.get("collection_id") or "").strip()
        idempotency_key = str(request.get("idempotency_key") or "").strip()
        content = request.get("content")
        if not service_instance_id:
            raise ValueError("service_instance_id is required")
        if not filename or not collection_id or not idempotency_key:
            raise ValueError("filename, collection_id, and idempotency_key are required")
        if not isinstance(content, bytes):
            raise TypeError("outbox content must be bytes")
        if len(content) > self.max_content_bytes:
            raise ValueError(f"document exceeds the {self.max_content_bytes} byte limit")
        principal = self._normalized_principal(request.get("principal"))
        digest_input = {
            "operation": _ALLOWED_OPERATION,
            "service_instance_id": service_instance_id,
            "principal": principal,
            "filename": filename,
            "collection_id": collection_id,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "idempotency_key": idempotency_key,
        }
        payload_sha256 = self._digest(digest_input)
        return {**digest_input, "payload_sha256": payload_sha256}

    @staticmethod
    def _normalized_principal(value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            tenant_id = str(value.get("tenant_id") or "").strip()
            user_id = str(value.get("user_id") or "").strip()
            roles_value = value.get("roles")
            collections_value = value.get("collection_ids")
        else:
            tenant_id = str(getattr(value, "tenant_id", "") or "").strip()
            user_id = str(getattr(value, "user_id", "") or "").strip()
            roles_value = getattr(value, "roles", None)
            collections_value = getattr(value, "collection_ids", None)
        if not tenant_id or not user_id:
            raise ValueError("outbox principal requires tenant_id and user_id")
        if not isinstance(roles_value, (list, tuple, set, frozenset)) or not roles_value:
            raise ValueError("outbox principal requires at least one role")
        roles = sorted({str(item).strip().lower() for item in roles_value if str(item).strip()})
        if not roles:
            raise ValueError("outbox principal requires at least one role")
        if collections_value is None:
            collections = None
        elif isinstance(collections_value, (list, tuple, set, frozenset)):
            collections = sorted(
                {str(item).strip() for item in collections_value if str(item).strip()}
            )
        else:
            raise ValueError("outbox principal collection_ids must be a collection or null")
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "roles": roles,
            "collection_ids": collections,
        }

    def _write(
        self,
        entry_id: str,
        created_at: str,
        payload: dict[str, Any],
        *,
        is_new: bool,
    ) -> None:
        plaintext = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        protected = self.protector.protect(plaintext)
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "entry_id": entry_id,
            "created_at": created_at,
            "ciphertext": base64.b64encode(protected).decode("ascii"),
        }
        encoded = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self._path(entry_id)
        self._check_capacity(destination, len(encoded), is_new=is_new)
        temporary = self.directory / f".{entry_id}.{uuid.uuid4().hex}.tmp"
        replaced = False
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_with_write_through(temporary, destination)
            replaced = True
            self._sync_directory_best_effort()
        finally:
            if replaced:
                temporary.unlink(missing_ok=True)

    def _check_capacity(self, destination: Path, encoded_size: int, *, is_new: bool) -> None:
        files = self._storage_files()
        if is_new and len(files) >= self.max_entries:
            raise OSError("secure ingestion outbox entry limit reached")
        total = 0
        for path in files:
            try:
                if path == destination:
                    continue
                total += path.stat().st_size
            except OSError:
                continue
        if total + encoded_size > self.max_disk_bytes:
            raise OSError("secure ingestion outbox disk byte limit reached")

    def _storage_files(self) -> list[Path]:
        if not self.directory.exists():
            return []
        completed = list(self.directory.glob("outbox-*.json"))
        temporary = [
            path
            for path in self.directory.glob(".*.tmp")
            if _TEMPORARY_ENTRY.fullmatch(path.name)
        ]
        return sorted({*completed, *temporary}, key=lambda path: path.name)

    @staticmethod
    def _safe_created_at(path: Path) -> str:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(envelope, dict):
            return ""
        return str(envelope.get("created_at") or "")

    def _sync_directory_best_effort(self) -> None:
        flags = getattr(os, "O_RDONLY", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(self.directory, flags | directory_flag)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _read_envelope(self, path: Path, *, expected_id: str) -> dict[str, Any]:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema_version",
            "entry_id",
            "created_at",
            "ciphertext",
        }:
            raise ValueError("invalid outbox envelope")
        if envelope.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported outbox schema")
        if envelope.get("entry_id") != expected_id:
            raise ValueError("outbox entry identity mismatch")
        if not isinstance(envelope.get("ciphertext"), str):
            raise ValueError("invalid outbox ciphertext")
        return envelope

    def _validate_stored_payload(self, payload: dict[str, Any], entry_id: str) -> None:
        if payload.get("schema_version") != _SCHEMA_VERSION or payload.get("entry_id") != entry_id:
            raise ValueError("outbox payload identity mismatch")
        state = payload.get("state")
        if state == "pending":
            required = {
                "schema_version",
                "entry_id",
                "created_at",
                "state",
                "operation",
                "service_instance_id",
                "principal",
                "filename",
                "collection_id",
                "content_base64",
                "idempotency_key",
                "payload_sha256",
                "cancel_after_submit",
                "cancel_delivery_state",
            }
            if set(payload) != required:
                raise ValueError("invalid pending outbox record")
            if payload.get("operation") != _ALLOWED_OPERATION:
                raise ValueError("unsupported stored outbox operation")
            self._normalized_principal(payload.get("principal"))
            if not isinstance(payload.get("cancel_after_submit"), bool):
                raise ValueError("invalid pending cancellation intent")
            if payload.get("cancel_delivery_state") not in {"none", "requested"}:
                raise ValueError("invalid pending cancellation delivery state")
            self._verify_payload_hash(payload)
        elif state == "tracking":
            required = {
                "schema_version",
                "entry_id",
                "created_at",
                "state",
                "service_instance_id",
                "principal",
                "filename",
                "collection_id",
                "job_id",
                "payload_sha256",
                "cancel_after_submit",
                "cancel_delivery_state",
            }
            if set(payload) != required:
                raise ValueError("invalid tracking record")
            self._normalized_principal(payload.get("principal"))
            if not str(payload.get("filename") or "").strip() or not str(
                payload.get("collection_id") or ""
            ).strip():
                raise ValueError("invalid tracking recovery metadata")
            if not isinstance(payload.get("cancel_after_submit"), bool):
                raise ValueError("invalid tracking cancellation intent")
            if payload.get("cancel_delivery_state") not in {
                "none",
                "requested",
                "delivering",
                "rejected",
                "delivered",
                "unknown",
            }:
                raise ValueError("invalid tracking cancellation delivery state")
        else:
            raise ValueError("unsupported outbox state")

    def _verify_payload_hash(self, payload: dict[str, Any]) -> None:
        fields = {
            key: payload[key]
            for key in (
                "operation",
                "service_instance_id",
                "principal",
                "filename",
                "collection_id",
                "content_base64",
                "idempotency_key",
            )
        }
        if payload.get("payload_sha256") != self._digest(fields):
            raise ValueError("outbox payload hash mismatch")

    def _path(self, entry_id: str) -> Path:
        if not _ENTRY_ID.fullmatch(str(entry_id)):
            raise ValueError("invalid outbox entry id")
        return self.directory / f"{entry_id}.json"

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _decode_content(value: str) -> bytes:
        return base64.b64decode(value, validate=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
