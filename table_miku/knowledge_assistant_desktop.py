from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .knowledge_assistant import KnowledgeAssistantService, Principal
from .knowledge_assistant.api import KnowledgeAssistantApi, ThreadingWSGIServer
from .knowledge_assistant.client import KnowledgeAssistantApiClient, KnowledgeAssistantApiError
from .knowledge_assistant.documents import MAX_DOCUMENT_BYTES
from .knowledge_assistant_outbox import DataProtector, SecureIngestionOutbox
from .paths import runtime_path


MAX_BATCH_FILES = 20
_TERMINAL_INGESTION_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _recovery_binding_id(mode: str, base_url: str, service_instance_id: str) -> str:
    instance_id = str(service_instance_id).strip()
    if not instance_id:
        raise ValueError("service_instance_id is required")
    if mode == "embedded":
        return instance_id
    if mode != "external":
        raise ValueError("unsupported endpoint mode")
    parsed = urlsplit(str(base_url).strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError("external recovery binding requires an HTTP(S) origin")
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    origin = f"{scheme}://{host}{'' if port is None or default_port else f':{port}'}"
    digest = hashlib.sha256(f"{origin}\0{instance_id}".encode()).hexdigest()
    return f"external-{digest}"


def _principal_payload(principal: Principal) -> dict[str, Any]:
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "roles": sorted(principal.roles),
        "collection_ids": (
            sorted(principal.collection_ids) if principal.collection_ids is not None else None
        ),
    }


def _principal_signature(principal: Principal) -> tuple[object, ...]:
    payload = _principal_payload(principal)
    collections = payload["collection_ids"]
    return (
        payload["tenant_id"],
        payload["user_id"],
        tuple(payload["roles"]),
        tuple(collections) if collections is not None else None,
    )


class _IngestionCancellationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, object]] = {}

    def begin(self, local_id: str) -> None:
        with self._lock:
            self._entries.setdefault(local_id, {"cancel": False, "phase": "queued"})

    def set_phase(self, local_id: str, phase: str) -> None:
        with self._lock:
            self._entries.setdefault(local_id, {"cancel": False})["phase"] = phase

    def request_cancel(self, local_id: str) -> str:
        with self._lock:
            entry = self._entries.setdefault(local_id, {"phase": "queued"})
            entry["cancel"] = True
            return str(entry.get("phase") or "queued")

    def cancelled(self, local_id: str) -> bool:
        with self._lock:
            return bool(self._entries.get(local_id, {}).get("cancel"))

    def mark_handled(self, local_id: str) -> None:
        with self._lock:
            entry = self._entries.get(local_id)
            if entry is not None:
                entry["cancel_handled"] = True

    def has_unhandled_cancel(self, local_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(local_id, {})
            return (
                bool(entry.get("worker_finished"))
                and bool(entry.get("cancel"))
                and not bool(entry.get("cancel_handled"))
            )

    def complete(self, local_id: str) -> None:
        with self._lock:
            entry = self._entries.setdefault(local_id, {"cancel": False})
            entry["worker_finished"] = True

    def finish(self, local_id: str) -> None:
        with self._lock:
            self._entries.pop(local_id, None)


class _IngestionWorker(QObject):
    update = Signal(object)
    recovery = Signal(object)
    item_finished = Signal()
    control_finished = Signal()

    def __init__(
        self,
        controller: object,
        outbox: SecureIngestionOutbox,
        recovery_binding_id: str,
        cancellations: _IngestionCancellationRegistry,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.outbox = outbox
        self.recovery_binding_id = recovery_binding_id
        self.cancellations = cancellations

    @Slot(object)
    def process_batch(self, command: dict[str, Any]) -> None:
        principal = command["principal"]
        generation = int(command["generation"])
        items = list(command["items"])
        for item in items:
            if QThread.currentThread().isInterruptionRequested():
                break
            self._process_item(principal, generation, dict(item))

    @Slot(object)
    def process_item(self, command: dict[str, Any]) -> None:
        try:
            if QThread.currentThread().isInterruptionRequested():
                return
            self._process_item(
                command["principal"],
                int(command["generation"]),
                dict(command["item"]),
            )
        finally:
            self.item_finished.emit()

    def _process_item(self, principal: Principal, generation: int, item: dict[str, Any]) -> None:
        local_id = str(item["local_id"])
        common = {
            "local_id": local_id,
            "filename": str(item["filename"]),
            "collection_id": str(item["collection_id"]),
            "generation": generation,
            "principal_signature": _principal_signature(principal),
        }
        self.cancellations.begin(local_id)
        entry_id = ""
        try:
            if self.cancellations.cancelled(local_id):
                self.cancellations.mark_handled(local_id)
                self.update.emit({**common, "status": "cancelled", "message": "已在发送前取消。"})
                return
            self.cancellations.set_phase(local_id, "reading")
            self.update.emit({**common, "status": "reading", "message": "正在读取文件。"})
            content = self._read_file(
                Path(item["path"]),
                local_id,
                dict(item["expected_snapshot"]),
            )
            if not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(),
                str(item["expected_snapshot"]["sha256"]),
            ):
                raise ValueError("selected file content changed before ingestion")
            if self._interrupted():
                raise _CancelledBeforeSend
            if self.cancellations.cancelled(local_id):
                self.cancellations.mark_handled(local_id)
                self.update.emit({**common, "status": "cancelled", "message": "已在发送前取消。"})
                return
            request = {
                "operation": "create_ingestion_job",
                "service_instance_id": self.recovery_binding_id,
                "principal": _principal_payload(principal),
                "filename": str(item["filename"]),
                "collection_id": str(item["collection_id"]),
                "content": content,
                "idempotency_key": str(item["idempotency_key"]),
            }
            entry = self.outbox.enqueue(request)
            entry_id = entry.entry_id
            self.cancellations.set_phase(local_id, "persisted")
            self.update.emit(
                {
                    **common,
                    "status": "persisted",
                    "entry_id": entry_id,
                    "message": "原请求已加密持久化，尚未发送。",
                }
            )
            if self._interrupted():
                self.update.emit(
                    {
                        **common,
                        "status": "pending",
                        "entry_id": entry_id,
                        "message": "应用正在关闭；加密请求已保留且尚未发送。",
                    }
                )
                return
            if self.cancellations.cancelled(local_id):
                self.cancellations.mark_handled(local_id)
                cleanup_ok = self._delete_known(entry_id)
                self.update.emit(
                    {
                        **common,
                        "status": "cancelled",
                        "message": self._with_cleanup_notice("已在发送前取消。", cleanup_ok),
                    }
                )
                return
            self.cancellations.set_phase(local_id, "sending")
            self.update.emit(
                {
                    **common,
                    "status": "sending",
                    "entry_id": entry_id,
                    "message": "请求已发送；此阶段只能请求服务端取消。",
                }
            )
            result = self.controller.create_ingestion_job(
                principal,
                filename=str(item["filename"]),
                content=content,
                collection_id=str(item["collection_id"]),
                idempotency_key=str(item["idempotency_key"]),
            )
            job_id = str(result.get("id") or result.get("job_id") or "").strip()
            status = str(result.get("status") or "queued").strip()
            if not job_id:
                raise ValueError("ingestion service returned no job id")
            if (
                self.cancellations.cancelled(local_id)
                and status not in _TERMINAL_INGESTION_STATES
            ):
                self.cancellations.mark_handled(local_id)
                try:
                    self.outbox.mark_cancel_requested(
                        entry_id,
                        service_instance_id=self.recovery_binding_id,
                        principal=_principal_payload(principal),
                    )
                except Exception:
                    self.update.emit(
                        {
                            **common,
                            "job_id": job_id,
                            "entry_id": entry_id,
                            "status": status,
                            "message": (
                                f"{self._terminal_message(status)} "
                                "取消意图未能持久化；加密请求仍保留，请人工核查。"
                            ),
                        }
                    )
                    return
            try:
                self.outbox.mark_submitted(entry_id, job_id=job_id)
            except Exception:
                self.update.emit(
                    {
                        **common,
                        "job_id": job_id,
                        "entry_id": entry_id,
                        "status": status,
                        "message": (
                            f"{self._terminal_message(status)} "
                            "本地跟踪记录转换失败；加密原请求仍保留，请人工对账。"
                        ),
                    }
                )
                return
            response = {**common, "job_id": job_id, "entry_id": entry_id, "status": status}
            if status in _TERMINAL_INGESTION_STATES:
                cleanup_ok = self._delete_known(entry_id)
                response["message"] = self._with_cleanup_notice(
                    self._terminal_message(status), cleanup_ok
                )
                self.update.emit(response)
                return
            if self.cancellations.cancelled(local_id):
                self.cancellations.mark_handled(local_id)
                if not self._mark_cancel_delivery_state(
                    entry_id,
                    principal,
                    "delivering",
                ):
                    self.update.emit(
                        {
                            **response,
                            "status": "outcome_unknown",
                            "message": (
                                "任务已创建且取消意图已保留；本地投递状态无法持久化，"
                                "尚未发送取消请求。"
                            ),
                        }
                    )
                    return
                if self._interrupted():
                    return
                self.update.emit(
                    {
                        **response,
                        "status": "cancelling",
                        "message": "取消请求中；尚不能断言任务已取消。",
                    }
                )
                try:
                    cancelled = self.controller.cancel_ingestion_job(principal, job_id)
                except Exception as exc:
                    definite = self._definite_cancel_rejection(exc)
                    if definite:
                        try:
                            self.outbox.mark_cancel_rejected(
                                entry_id,
                                service_instance_id=self.recovery_binding_id,
                                principal=_principal_payload(principal),
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            self.outbox.mark_cancel_unknown(
                                entry_id,
                                service_instance_id=self.recovery_binding_id,
                                principal=_principal_payload(principal),
                            )
                        except Exception:
                            pass
                    self.update.emit(
                        {
                            **response,
                            "status": "cancel_rejected" if definite else "outcome_unknown",
                            "message": (
                                "服务端已明确拒绝取消请求；任务已创建且取消意图仍保留，请核查。"
                                if definite
                                else "任务已创建且取消意图已保留；取消结果仍待确认。"
                            ),
                        }
                    )
                    return
                final_status = str(cancelled.get("status") or "cancelling")
                if final_status in _TERMINAL_INGESTION_STATES:
                    cleanup_ok = self._delete_known(entry_id)
                else:
                    try:
                        self.outbox.mark_cancel_delivered(
                            entry_id,
                            service_instance_id=self.recovery_binding_id,
                            principal=_principal_payload(principal),
                        )
                    except Exception:
                        pass
                    cleanup_ok = True
                self.update.emit(
                    {
                        **response,
                        **self._safe_job_fields(cancelled),
                        "status": final_status,
                        "message": self._with_cleanup_notice(
                            self._terminal_message(final_status), cleanup_ok
                        ),
                    }
                )
                return
            response["message"] = "服务端已接收，正在后台处理。"
            self.update.emit(response)
        except _CancelledBeforeSend:
            self.cancellations.mark_handled(local_id)
            if entry_id:
                cleanup_ok = self._delete_known(entry_id)
            else:
                cleanup_ok = True
            self.update.emit(
                {
                    **common,
                    "status": "cancelled",
                    "message": self._with_cleanup_notice("已在发送前取消。", cleanup_ok),
                }
            )
        except Exception as exc:
            if entry_id:
                if self._definite_create_rejection(exc):
                    cleanup_ok = self._delete_known(entry_id)
                    self.update.emit(
                        {
                            **common,
                            "status": "failed",
                            "entry_id": entry_id,
                            "message": self._with_cleanup_notice(
                                "服务端已明确拒绝该请求；未创建摄取任务。",
                                cleanup_ok,
                            ),
                        }
                    )
                else:
                    cancel_intent_persisted = True
                    if self.cancellations.cancelled(local_id):
                        self.cancellations.mark_handled(local_id)
                        try:
                            self.outbox.mark_cancel_requested(
                                entry_id,
                                service_instance_id=self.recovery_binding_id,
                                principal=_principal_payload(principal),
                            )
                        except Exception:
                            cancel_intent_persisted = False
                    if self.cancellations.cancelled(local_id) and not cancel_intent_persisted:
                        message = (
                            "结果待确认；取消意图未能持久化，恢复记录已保留，请人工核查。"
                        )
                    elif self.cancellations.cancelled(local_id):
                        message = "取消意图已保留，提交结果仍待确认。"
                    else:
                        message = "结果待确认；加密原请求已保留，可由原身份人工安全重放。"
                    self.update.emit(
                        {
                            **common,
                            "status": "outcome_unknown",
                            "entry_id": entry_id,
                            "message": message,
                        }
                    )
            else:
                self.update.emit(
                    {
                        **common,
                        "status": "failed",
                        "message": "文件未提交。请检查文件或本机安全存储后重试。",
                    }
                )
        finally:
            self.cancellations.complete(local_id)

    def _read_file(
        self,
        path: Path,
        local_id: str,
        expected_snapshot: dict[str, Any],
    ) -> bytes:
        source = Path(path)
        try:
            with source.open("rb") as stream:
                source_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(source_stat.st_mode):
                    raise ValueError("请选择一个可读取的文档文件")
                self._verify_snapshot(source, source_stat, expected_snapshot)
                if source_stat.st_size > MAX_DOCUMENT_BYTES:
                    raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
                chunks: list[bytes] = []
                total = 0
                while True:
                    if self._interrupted() or self.cancellations.cancelled(local_id):
                        raise _CancelledBeforeSend
                    chunk = stream.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOCUMENT_BYTES:
                        raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
                    chunks.append(chunk)
        except OSError as exc:
            raise ValueError("请选择一个可读取的文档文件") from exc
        return b"".join(chunks)

    @staticmethod
    def _interrupted() -> bool:
        return QThread.currentThread().isInterruptionRequested()

    @staticmethod
    def _verify_snapshot(
        source: Path,
        source_stat: os.stat_result,
        expected: dict[str, Any],
    ) -> None:
        required = {"canonical_path", "size", "mtime_ns", "device", "inode", "sha256"}
        if set(expected) != required:
            raise ValueError("invalid file snapshot")
        if os.path.normcase(os.path.abspath(str(source))) != os.path.normcase(
            os.path.abspath(str(expected["canonical_path"]))
        ):
            raise ValueError("selected file path changed")
        if source_stat.st_size != int(expected["size"]) or source_stat.st_mtime_ns != int(
            expected["mtime_ns"]
        ):
            raise ValueError("selected file changed before ingestion")
        expected_device = int(expected["device"])
        expected_inode = int(expected["inode"])
        if expected_device and source_stat.st_dev != expected_device:
            raise ValueError("selected file changed before ingestion")
        if expected_inode and source_stat.st_ino != expected_inode:
            raise ValueError("selected file changed before ingestion")

    @Slot(object)
    def poll(self, command: dict[str, Any]) -> None:
        if self._interrupted():
            return
        principal = command["principal"]
        generation = int(command["generation"])
        signature = _principal_signature(principal)
        try:
            jobs = [dict(job) for job in self.controller.list_ingestion_jobs(principal)]
        except Exception:
            self.update.emit(
                {
                    "status": "poll_failed",
                    "generation": generation,
                    "principal_signature": signature,
                    "message": "暂时无法刷新摄取任务；现有任务不会自动重试。",
                }
            )
            return
        if self._interrupted():
            return
        self._reconcile_tracking(principal, jobs, generation)
        self.update.emit(
            {
                "status": "snapshot",
                "jobs": jobs,
                "generation": generation,
                "principal_signature": signature,
            }
        )

    def _reconcile_tracking(
        self,
        principal: Principal,
        jobs: list[dict[str, Any]],
        generation: int,
    ) -> None:
        expected_principal = _principal_payload(principal)
        by_id = {str(job.get("id") or ""): job for job in jobs}
        for scanned in self.outbox.scan():
            if self._interrupted():
                return
            payload = scanned.payload or {}
            if (
                scanned.error
                or payload.get("state") != "tracking"
                or payload.get("service_instance_id") != self.recovery_binding_id
                or payload.get("principal") != expected_principal
            ):
                continue
            job_id = str(payload.get("job_id") or "")
            job = by_id.get(job_id)
            if job is None:
                try:
                    fetched = self.controller.get_ingestion_job(principal, job_id)
                except Exception as exc:
                    definite = self._definite_cancel_rejection(exc)
                    self.update.emit(
                        {
                            "entry_id": scanned.entry_id,
                            "job_id": job_id,
                            "status": "reconciliation_required",
                            "generation": generation,
                            "principal_signature": _principal_signature(principal),
                            "message": (
                                "服务端未返回该跟踪任务；本地记录已保留，请人工核查。"
                                if definite
                                else "暂时无法对账该跟踪任务；本地记录已保留。"
                            ),
                        }
                    )
                    continue
                if self._interrupted():
                    return
                job = dict(fetched)
                jobs.append(job)
                by_id[job_id] = job
            status = str(job.get("status") or "")
            if status in _TERMINAL_INGESTION_STATES:
                self._delete_known(scanned.entry_id)
                continue
            if payload.get("cancel_delivery_state") != "requested":
                continue
            if self._interrupted():
                return
            if not self._mark_cancel_delivery_state(
                scanned.entry_id,
                principal,
                "delivering",
            ):
                self.update.emit(
                    {
                        "entry_id": scanned.entry_id,
                        "job_id": job_id,
                        "status": "outcome_unknown",
                        "generation": generation,
                        "principal_signature": _principal_signature(principal),
                        "message": (
                            "取消意图已保留；本地投递状态无法持久化，尚未发送取消请求。"
                        ),
                    }
                )
                continue
            if self._interrupted():
                return
            try:
                cancelled = self.controller.cancel_ingestion_job(principal, job_id)
            except Exception as exc:
                definite = self._definite_cancel_rejection(exc)
                if definite:
                    try:
                        self.outbox.mark_cancel_rejected(
                            scanned.entry_id,
                            service_instance_id=self.recovery_binding_id,
                            principal=expected_principal,
                        )
                    except Exception:
                        pass
                else:
                    try:
                        self.outbox.mark_cancel_unknown(
                            scanned.entry_id,
                            service_instance_id=self.recovery_binding_id,
                            principal=expected_principal,
                        )
                    except Exception:
                        pass
                self.update.emit(
                    {
                        "entry_id": scanned.entry_id,
                        "job_id": job_id,
                        "status": "cancel_rejected" if definite else "outcome_unknown",
                        "generation": generation,
                        "principal_signature": _principal_signature(principal),
                        "message": (
                            "服务端已明确拒绝取消请求；取消意图仍保留，请核查权限与任务状态。"
                            if definite
                            else "取消意图已保留；取消结果仍待确认。"
                        ),
                    }
                )
                continue
            job.update(cancelled)
            status = str(job.get("status") or "cancelling")
            if status in _TERMINAL_INGESTION_STATES:
                self._delete_known(scanned.entry_id)
            else:
                try:
                    self.outbox.mark_cancel_delivered(
                        scanned.entry_id,
                        service_instance_id=self.recovery_binding_id,
                        principal=expected_principal,
                    )
                except Exception:
                    pass

    @Slot(object)
    def cancel_job(self, command: dict[str, Any]) -> None:
        try:
            self._cancel_job(command)
        finally:
            self.control_finished.emit()

    def _cancel_job(self, command: dict[str, Any]) -> None:
        if self._interrupted():
            return
        principal = command["principal"]
        generation = int(command["generation"])
        job_id = str(command["job_id"])
        common = {
            "job_id": job_id,
            "generation": generation,
            "principal_signature": _principal_signature(principal),
        }
        if command.get("local_id"):
            common["local_id"] = str(command["local_id"])
        cancel_entry_id, terminal_job = self._persist_tracking_cancel_intent(
            principal,
            job_id,
            filename=str(command.get("filename") or ""),
            collection_id=str(command.get("collection_id") or ""),
        )
        if terminal_job is not None:
            status = str(terminal_job.get("status") or "")
            self.update.emit(
                {
                    **common,
                    **self._safe_job_fields(terminal_job),
                    "status": status,
                    "message": self._terminal_message(status),
                }
            )
            return
        if not cancel_entry_id:
            self.update.emit(
                {
                    **common,
                    "status": "outcome_unknown",
                    "message": "无法持久化取消意图；尚未发送取消请求，请人工核查。",
                }
            )
            return
        if self._interrupted():
            return
        if not self._mark_cancel_delivery_state(
            cancel_entry_id,
            principal,
            "delivering",
        ):
            self.update.emit(
                {
                    **common,
                    "status": "outcome_unknown",
                    "message": (
                        "取消意图已保留；本地投递状态无法持久化，尚未发送取消请求。"
                    ),
                }
            )
            return
        if self._interrupted():
            return
        self.update.emit(
            {**common, "status": "cancelling", "message": "取消请求中；尚不能断言任务已取消。"}
        )
        try:
            result = self.controller.cancel_ingestion_job(principal, job_id)
            status = str(result.get("status") or "cancelling")
        except Exception as exc:
            if self._definite_cancel_rejection(exc):
                self._mark_cancel_delivery_state(cancel_entry_id, principal, "rejected")
                status = "cancel_rejected"
                message = "服务端已明确拒绝取消请求；任务状态未由本次操作改变。"
            else:
                self._mark_cancel_delivery_state(cancel_entry_id, principal, "unknown")
                status = "outcome_unknown"
                message = "取消意图已保留；取消结果仍待确认。"
            self.update.emit({**common, "status": status, "message": message})
            return
        if status not in _TERMINAL_INGESTION_STATES:
            self._mark_cancel_delivery_state(cancel_entry_id, principal, "delivered")
            cleanup_ok = True
        else:
            cleanup_ok = self._delete_known(cancel_entry_id)
        self.update.emit(
            {
                **common,
                **self._safe_job_fields(result),
                "status": status,
                "message": self._with_cleanup_notice(
                    self._terminal_message(status), cleanup_ok
                ),
            }
        )

    def _persist_tracking_cancel_intent(
        self,
        principal: Principal,
        job_id: str,
        *,
        filename: str = "",
        collection_id: str = "",
    ) -> tuple[str | None, dict[str, Any] | None]:
        expected_principal = _principal_payload(principal)
        for scanned in self.outbox.scan():
            payload = scanned.payload or {}
            if (
                scanned.error
                or payload.get("state") != "tracking"
                or payload.get("service_instance_id") != self.recovery_binding_id
                or payload.get("principal") != expected_principal
                or str(payload.get("job_id") or "") != job_id
            ):
                continue
            try:
                self.outbox.mark_cancel_requested(
                    scanned.entry_id,
                    service_instance_id=self.recovery_binding_id,
                    principal=expected_principal,
                )
            except Exception:
                return None, None
            return scanned.entry_id, None
        safe_filename = str(filename).strip()
        safe_collection_id = str(collection_id).strip()
        if not safe_filename or not safe_collection_id:
            try:
                job = dict(self.controller.get_ingestion_job(principal, job_id))
            except Exception:
                return None, None
            if self._interrupted():
                return None, None
            if str(job.get("id") or "") != job_id:
                return None, None
            status = str(job.get("status") or "")
            if status in _TERMINAL_INGESTION_STATES:
                return None, job
            safe_filename = str(job.get("filename") or "").strip()
            safe_collection_id = str(job.get("collection_id") or "").strip()
        try:
            tracked = self.outbox.create_cancel_tracking(
                service_instance_id=self.recovery_binding_id,
                principal=expected_principal,
                job_id=job_id,
                filename=safe_filename,
                collection_id=safe_collection_id,
            )
        except Exception:
            return None, None
        return tracked.entry_id, None

    def _mark_cancel_delivery_state(
        self,
        entry_id: str,
        principal: Principal,
        delivery_state: str,
    ) -> bool:
        expected_principal = _principal_payload(principal)
        try:
            marker = {
                "delivering": self.outbox.mark_cancel_delivering,
                "rejected": self.outbox.mark_cancel_rejected,
                "delivered": self.outbox.mark_cancel_delivered,
                "unknown": self.outbox.mark_cancel_unknown,
            }[delivery_state]
            marker(
                entry_id,
                service_instance_id=self.recovery_binding_id,
                principal=expected_principal,
            )
        except Exception:
            return False
        return True

    @Slot(object)
    def cancel_recovery(self, command: dict[str, Any]) -> None:
        try:
            self._cancel_recovery(command)
        finally:
            self.control_finished.emit()

    def _cancel_recovery(self, command: dict[str, Any]) -> None:
        if self._interrupted():
            return
        principal = command["principal"]
        generation = int(command["generation"])
        entry_id = str(command["entry_id"])
        common = {
            "entry_id": entry_id,
            "generation": generation,
            "principal_signature": _principal_signature(principal),
        }
        try:
            self.outbox.mark_cancel_requested(
                entry_id,
                service_instance_id=self.recovery_binding_id,
                principal=_principal_payload(principal),
            )
        except Exception:
            self.update.emit(
                {
                    **common,
                    "status": "outcome_unknown",
                    "message": "无法持久化取消意图；恢复记录已保留，请人工核查。",
                }
            )
            return
        self.update.emit(
            {
                **common,
                "status": "outcome_unknown",
                "message": "取消意图已保留，结果仍待确认。",
            }
        )

    @Slot(object)
    def replay(self, command: dict[str, Any]) -> None:
        try:
            self._replay(command)
        finally:
            self.control_finished.emit()

    def _replay(self, command: dict[str, Any]) -> None:
        if self._interrupted():
            return
        principal = command["principal"]
        generation = int(command["generation"])
        entry_id = str(command["entry_id"])
        common = {
            "entry_id": entry_id,
            "generation": generation,
            "principal_signature": _principal_signature(principal),
        }
        try:
            entry = self.outbox.load_for_replay(
                entry_id,
                service_instance_id=self.recovery_binding_id,
                principal=_principal_payload(principal),
            )
            payload = entry.payload
            self.update.emit(
                {
                    **common,
                    "status": "sending",
                    "filename": payload["filename"],
                    "collection_id": payload["collection_id"],
                    "message": "正在使用原内容和原幂等键安全重试。",
                }
            )
        except Exception:
            self.update.emit(
                {
                    **common,
                    "status": "outcome_unknown",
                    "message": "恢复记录暂时无法读取；加密原请求继续保留。",
                }
            )
            return
        if self._interrupted():
            return
        try:
            result = self.controller.create_ingestion_job(
                principal,
                filename=str(payload["filename"]),
                content=bytes(payload["content"]),
                collection_id=str(payload["collection_id"]),
                idempotency_key=str(payload["idempotency_key"]),
            )
            job_id = str(result.get("id") or result.get("job_id") or "").strip()
            if not job_id:
                raise ValueError("ingestion service returned no job id")
            status = str(result.get("status") or "queued")
        except Exception as exc:
            if self._definite_create_rejection(exc):
                cleanup_ok = self._delete_known(entry_id)
                self.update.emit(
                    {
                        **common,
                        "status": "failed",
                        "message": self._with_cleanup_notice(
                            "服务端已明确拒绝该请求；未创建摄取任务。", cleanup_ok
                        ),
                    }
                )
            else:
                self.update.emit(
                    {
                        **common,
                        "status": "outcome_unknown",
                        "message": "安全重放结果仍待确认；加密原请求继续保留。",
                    }
                )
            return
        try:
            self.outbox.mark_submitted(entry_id, job_id=job_id)
        except Exception:
            self.update.emit(
                {
                    **common,
                    "job_id": job_id,
                    "status": status,
                    "filename": payload["filename"],
                    "collection_id": payload["collection_id"],
                    "message": (
                        f"{self._terminal_message(status)} "
                        "本地跟踪记录转换失败；加密原请求仍保留，请人工对账。"
                    ),
                }
            )
            return
        cancel_result: dict[str, Any] | None = None
        if bool(payload.get("cancel_after_submit")) and status not in _TERMINAL_INGESTION_STATES:
            if not self._mark_cancel_delivery_state(
                entry_id,
                principal,
                "delivering",
            ):
                self.update.emit(
                    {
                        **common,
                        "job_id": job_id,
                        "status": "outcome_unknown",
                        "filename": payload["filename"],
                        "collection_id": payload["collection_id"],
                        "message": (
                            "任务已创建且取消意图已保留；本地投递状态无法持久化，"
                            "尚未发送取消请求。"
                        ),
                    }
                )
                return
            if self._interrupted():
                return
            self.update.emit(
                {
                    **common,
                    "job_id": job_id,
                    "status": "cancelling",
                    "filename": payload["filename"],
                    "collection_id": payload["collection_id"],
                    "message": "已确认任务编号，正在继续执行已保留的取消意图。",
                }
            )
            try:
                cancel_result = self.controller.cancel_ingestion_job(principal, job_id)
                status = str(cancel_result.get("status") or "cancelling")
            except Exception as exc:
                definite = self._definite_cancel_rejection(exc)
                if definite:
                    try:
                        self.outbox.mark_cancel_rejected(
                            entry_id,
                            service_instance_id=self.recovery_binding_id,
                            principal=_principal_payload(principal),
                        )
                    except Exception:
                        pass
                else:
                    try:
                        self.outbox.mark_cancel_unknown(
                            entry_id,
                            service_instance_id=self.recovery_binding_id,
                            principal=_principal_payload(principal),
                        )
                    except Exception:
                        pass
                self.update.emit(
                    {
                        **common,
                        "job_id": job_id,
                        "status": "cancel_rejected" if definite else "outcome_unknown",
                        "filename": payload["filename"],
                        "collection_id": payload["collection_id"],
                        "message": (
                            "服务端已明确拒绝取消请求；取消意图仍保留，请核查。"
                            if definite
                            else "取消意图已保留；取消结果仍待确认。"
                        ),
                    }
                )
                return
            if status not in _TERMINAL_INGESTION_STATES:
                try:
                    self.outbox.mark_cancel_delivered(
                        entry_id,
                        service_instance_id=self.recovery_binding_id,
                        principal=_principal_payload(principal),
                    )
                except Exception:
                    pass
        cleanup_ok = True
        if status in _TERMINAL_INGESTION_STATES:
            cleanup_ok = self._delete_known(entry_id)
        self.update.emit(
            {
                **common,
                **self._safe_job_fields(cancel_result),
                "job_id": job_id,
                "status": status,
                "filename": payload["filename"],
                "collection_id": payload["collection_id"],
                "message": self._with_cleanup_notice(
                    self._terminal_message(status), cleanup_ok
                ),
            }
        )

    @Slot(object)
    def abandon(self, command: dict[str, Any]) -> None:
        try:
            self._abandon(command)
        finally:
            self.control_finished.emit()

    def _abandon(self, command: dict[str, Any]) -> None:
        if self._interrupted():
            return
        principal = command["principal"]
        generation = int(command["generation"])
        entry_id = str(command["entry_id"])
        common = {
            "entry_id": entry_id,
            "generation": generation,
            "principal_signature": _principal_signature(principal),
        }
        try:
            self.outbox.load_for_replay(
                entry_id,
                service_instance_id=self.recovery_binding_id,
                principal=_principal_payload(principal),
            )
            if self._interrupted():
                return
            self.outbox.delete(entry_id)
        except Exception:
            self.update.emit(
                {
                    **common,
                    "status": "outcome_unknown",
                    "message": "无法放弃该恢复记录；记录已保留，请人工核查。",
                }
            )
            return
        self.update.emit(
            {
                **common,
                "status": "abandoned",
                "message": "已明确放弃本地恢复记录；不会自动重新提交。",
            }
        )

    @Slot()
    def scan_recovery(self) -> None:
        if self._interrupted():
            return
        results: list[dict[str, Any]] = []
        for item in self.outbox.scan():
            if item.error:
                # A corrupt record cannot be safely attributed to this service or identity.
                # Retain it on disk but do not expose its existence or metadata to the UI.
                continue
            payload = item.payload or {}
            if payload.get("service_instance_id") != self.recovery_binding_id:
                continue
            results.append(
                {
                    "entry_id": item.entry_id,
                    "status": str(payload.get("state") or "unavailable"),
                    "service_instance_id": str(payload.get("service_instance_id") or ""),
                    "principal": payload.get("principal"),
                    "requested_by": str(
                        (payload.get("principal") or {}).get("user_id") or ""
                    ),
                    "filename": str(payload.get("filename") or ""),
                    "collection_id": str(payload.get("collection_id") or ""),
                    "job_id": str(payload.get("job_id") or ""),
                    "cancel_after_submit": bool(
                        payload.get("cancel_after_submit", False)
                    ),
                    "cancel_delivery_state": str(
                        payload.get("cancel_delivery_state") or "none"
                    ),
                }
            )
        self.recovery.emit(results)

    def _delete_known(self, entry_id: str) -> bool:
        try:
            self.outbox.delete(entry_id)
        except Exception:
            return False
        return True

    @staticmethod
    def _safe_job_fields(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed = {
            "cancel_outcome",
            "document_id",
            "requested_by",
            "progress",
            "error_code",
            "error_message",
        }
        return {key: value[key] for key in allowed if key in value}

    @staticmethod
    def _with_cleanup_notice(message: str, cleanup_ok: bool) -> str:
        if cleanup_ok:
            return message
        return f"{message} 本地恢复记录暂未清理，请稍后刷新对账。"

    @staticmethod
    def _definite_create_rejection(exc: Exception) -> bool:
        return (
            isinstance(exc, KnowledgeAssistantApiError)
            and 400 <= int(exc.status_code) < 500
            and int(exc.status_code) != 408
        )

    @staticmethod
    def _definite_cancel_rejection(exc: Exception) -> bool:
        return (
            isinstance(exc, KnowledgeAssistantApiError)
            and 400 <= int(exc.status_code) < 500
            and int(exc.status_code) != 408
        )

    @staticmethod
    def _terminal_message(status: str) -> str:
        return {
            "succeeded": "摄取完成。",
            "failed": "摄取失败；核查原因后请重新选择文件创建新任务。",
            "cancelled": "服务端已确认取消。",
            "cancelling": "取消请求中；尚不能断言任务已取消。",
            "queued": "服务端已接收，等待处理。",
            "running": "服务端正在处理。",
        }.get(status, "任务状态已更新。")


class _CancelledBeforeSend(Exception):
    pass


class IngestionCoordinator(QObject):
    """Serialize recoverable ingestion work on one background Qt thread."""

    updated = Signal(object)
    recovery_updated = Signal(object)
    _submit = Signal(object)
    _poll = Signal(object)
    _cancel_job = Signal(object)
    _cancel_recovery = Signal(object)
    _replay = Signal(object)
    _abandon = Signal(object)
    _scan = Signal()

    def __init__(
        self,
        controller: object,
        outbox: SecureIngestionOutbox,
        recovery_binding_id: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not str(recovery_binding_id).strip():
            raise OSError("stable recovery_binding_id is required for recoverable ingestion")
        self.recovery_binding_id = str(recovery_binding_id).strip()
        self._cancellations = _IngestionCancellationRegistry()
        self._thread = QThread(self)
        self._poll_pending = False
        self._pending_items: list[dict[str, Any]] = []
        self._item_active = False
        self._active_item_command: dict[str, Any] | None = None
        self._latest_local_updates: dict[str, dict[str, Any]] = {}
        self._control_outstanding = 0
        self._shutting_down = False
        self._worker = _IngestionWorker(
            controller,
            outbox,
            self.recovery_binding_id,
            self._cancellations,
        )
        self._worker.moveToThread(self._thread)
        self._submit.connect(self._worker.process_item)
        self._poll.connect(self._worker.poll)
        self._cancel_job.connect(self._worker.cancel_job)
        self._cancel_recovery.connect(self._worker.cancel_recovery)
        self._replay.connect(self._worker.replay)
        self._abandon.connect(self._worker.abandon)
        self._scan.connect(self._worker.scan_recovery)
        self._worker.update.connect(self._forward_update)
        self._worker.recovery.connect(self.recovery_updated)
        self._worker.item_finished.connect(self._on_item_finished)
        self._worker.control_finished.connect(self._on_control_finished)
        self._thread.start()

    def submit_files(
        self,
        principal: Principal,
        paths: list[Path],
        *,
        collection_id: str,
        generation: int,
        expected_snapshots: list[dict[str, object]],
    ) -> list[str]:
        if self._shutting_down:
            raise RuntimeError("ingestion coordinator is shutting down")
        if not paths or len(paths) > MAX_BATCH_FILES:
            raise ValueError(f"batch must contain between 1 and {MAX_BATCH_FILES} files")
        if len(expected_snapshots) != len(paths):
            raise ValueError("one expected snapshot is required for each selected file")
        collection = str(collection_id).strip()
        if not collection:
            raise ValueError("collection_id is required")
        items: list[dict[str, Any]] = []
        local_ids: list[str] = []
        for path, supplied_snapshot in zip(paths, expected_snapshots, strict=True):
            local_id = f"local-{uuid.uuid4().hex}"
            local_ids.append(local_id)
            snapshot = self._validated_snapshot(Path(path), supplied_snapshot)
            source = Path(str(snapshot["canonical_path"]))
            items.append(
                {
                    "local_id": local_id,
                    "path": str(source),
                    "expected_snapshot": snapshot,
                    "filename": source.name,
                    "collection_id": collection,
                    "idempotency_key": f"desktop-ingestion-{uuid.uuid4().hex}",
                }
            )
            self.updated.emit(
                {
                    "local_id": local_id,
                    "filename": source.name,
                    "collection_id": collection,
                    "status": "queued",
                    "message": "已加入本地队列，尚未发送。",
                    "generation": generation,
                    "principal_signature": _principal_signature(principal),
                }
            )
        self._pending_items.extend(
            {"principal": principal, "generation": generation, "item": item}
            for item in items
        )
        self._dispatch_next()
        return local_ids

    @staticmethod
    def _validated_snapshot(path: Path, supplied: dict[str, object]) -> dict[str, object]:
        required = {"canonical_path", "size", "mtime_ns", "device", "inode", "sha256"}
        if not isinstance(supplied, dict) or set(supplied) != required:
            raise ValueError("invalid selected-file snapshot")
        canonical = Path(str(supplied["canonical_path"]))
        if not canonical.is_absolute():
            raise ValueError("canonical_path must be absolute")
        supplied_path = Path(os.path.abspath(str(path)))
        if os.path.normcase(str(supplied_path)) != os.path.normcase(str(canonical)):
            raise ValueError("selected path does not match its canonical snapshot")
        snapshot_sha256 = str(supplied["sha256"]).strip().lower()
        if len(snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in snapshot_sha256
        ):
            raise ValueError("selected-file snapshot requires a valid SHA-256 digest")
        return {
            "canonical_path": str(canonical),
            "size": int(supplied["size"]),
            "mtime_ns": int(supplied["mtime_ns"]),
            "device": int(supplied["device"]),
            "inode": int(supplied["inode"]),
            "sha256": snapshot_sha256,
        }

    def _dispatch_next(self) -> None:
        if self._shutting_down or self._item_active or not self._pending_items:
            return
        self._item_active = True
        self._active_item_command = self._pending_items.pop(0)
        self._submit.emit(self._active_item_command)

    @Slot()
    def _on_item_finished(self) -> None:
        command = self._active_item_command
        local_id = ""
        try:
            if command is not None:
                item = dict(command["item"])
                local_id = str(item["local_id"])
                update = self._latest_local_updates.get(local_id, {})
                if self._cancellations.has_unhandled_cancel(local_id):
                    job_id = str(update.get("job_id") or "")
                    entry_id = str(update.get("entry_id") or "")
                    if job_id:
                        self._emit_control(
                            self._cancel_job,
                            {
                                "principal": command["principal"],
                                "local_id": local_id,
                                "job_id": job_id,
                                "generation": int(command["generation"]),
                                "filename": str(update.get("filename") or item["filename"]),
                                "collection_id": str(
                                    update.get("collection_id") or item["collection_id"]
                                ),
                            },
                        )
                        self._cancellations.mark_handled(local_id)
                    elif entry_id and str(update.get("status") or "") in {
                        "outcome_unknown",
                        "pending",
                    }:
                        self._emit_control(
                            self._cancel_recovery,
                            {
                                "principal": command["principal"],
                                "entry_id": entry_id,
                                "generation": int(command["generation"]),
                            },
                        )
                        self._cancellations.mark_handled(local_id)
        finally:
            if local_id:
                self._cancellations.finish(local_id)
                self._latest_local_updates.pop(local_id, None)
            self._active_item_command = None
            self._item_active = False
            self._dispatch_next()

    def request_cancel_local(self, local_id: str) -> str:
        phase = self._cancellations.request_cancel(str(local_id))
        return "cancelled" if phase == "queued" else "cancelling"

    def request_cancel_job(self, principal: Principal, job_id: str, *, generation: int) -> None:
        self._emit_control(
            self._cancel_job,
            {"principal": principal, "job_id": str(job_id), "generation": generation},
        )

    def request_cancel_recovery(
        self,
        principal: Principal,
        entry_id: str,
        *,
        generation: int,
    ) -> None:
        self._emit_control(
            self._cancel_recovery,
            {"principal": principal, "entry_id": str(entry_id), "generation": generation},
        )

    def refresh(self, principal: Principal, *, generation: int) -> None:
        if self._poll_pending:
            return
        self._poll_pending = True
        self._poll.emit({"principal": principal, "generation": generation})

    @Slot(object)
    def _forward_update(self, update: object) -> None:
        if isinstance(update, dict) and update.get("local_id"):
            self._latest_local_updates[str(update["local_id"])] = dict(update)
        if isinstance(update, dict) and update.get("status") in {"snapshot", "poll_failed"}:
            self._poll_pending = False
        self.updated.emit(update)

    def safe_replay(
        self,
        principal: Principal,
        entry_id: str,
        *,
        generation: int,
    ) -> None:
        self._emit_control(
            self._replay,
            {"principal": principal, "entry_id": str(entry_id), "generation": generation},
        )

    def abandon_recovery(
        self,
        principal: Principal,
        entry_id: str,
        *,
        generation: int,
    ) -> None:
        self._emit_control(
            self._abandon,
            {"principal": principal, "entry_id": str(entry_id), "generation": generation},
        )

    def _emit_control(self, signal: Signal, command: dict[str, Any]) -> None:
        if self._shutting_down or not self._thread.isRunning():
            raise RuntimeError("ingestion coordinator is shutting down")
        self._control_outstanding += 1
        try:
            signal.emit(command)
        except Exception:
            self._control_outstanding -= 1
            raise

    @Slot()
    def _on_control_finished(self) -> None:
        self._control_outstanding = max(0, self._control_outstanding - 1)

    def scan_recovery(self) -> None:
        self._scan.emit()

    def shutdown(self, timeout_ms: int = 2000) -> bool:
        if not self._thread.isRunning():
            return True
        if self._item_active or self._pending_items or self._control_outstanding:
            # These entries exist only in memory and have not frozen/persisted bytes yet.
            # Keep the coordinator alive so they can drain instead of silently dropping them.
            return False
        self._shutting_down = True
        self._thread.requestInterruption()
        self._thread.quit()
        return self._thread.wait(max(0, int(timeout_ms)))


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        del format, args


class ManagedKnowledgeAssistantEndpoint:
    """Own a private loopback API, or connect to an explicitly configured one."""

    def __init__(self, service: KnowledgeAssistantService | None = None) -> None:
        self.mode = "embedded"
        self.database_path: Path | None = None
        self.service_instance_id = ""
        self.recovery_binding_id = ""
        self._server = None
        self._thread: threading.Thread | None = None
        self._service: KnowledgeAssistantService | None = None
        configured_url = os.getenv("KNOWLEDGE_ASSISTANT_DESKTOP_URL", "").strip()
        configured_token = os.getenv("KNOWLEDGE_ASSISTANT_API_TOKEN", "")
        if service is None and configured_url:
            if not configured_token:
                raise PermissionError(
                    "KNOWLEDGE_ASSISTANT_API_TOKEN is required for an external desktop connection"
                )
            health_client = KnowledgeAssistantApiClient(
                configured_url,
                configured_token,
                timeout_seconds=5.0,
            )
            health = health_client.health()
            if health.get("status") != "ok":
                raise ConnectionError("configured Knowledge Assistant service is not healthy")
            self.service_instance_id = str(health.get("service_instance_id") or "").strip()
            self.client = KnowledgeAssistantApiClient(configured_url, configured_token)
            self.mode = "external"
            self.recovery_binding_id = _recovery_binding_id(
                self.mode, self.client.base_url, self.service_instance_id
            )
            return

        if service is None:
            default_client = KnowledgeAssistantApiClient(
                "http://127.0.0.1:8080",
                "",
                timeout_seconds=1.0,
            )
            try:
                health = default_client.health()
            except (ConnectionError, KnowledgeAssistantApiError):
                health = {}
            if health.get("status") == "ok" and health.get("schema_version") is not None:
                raise PermissionError(
                    "检测到 127.0.0.1:8080 Knowledge Assistant；为避免连接错误实例或同时打开同一数据库，"
                    "请显式设置 KNOWLEDGE_ASSISTANT_DESKTOP_URL 与同一 API Token，或先关闭外部服务"
                )

        embedded_service = service or KnowledgeAssistantService()
        embedded_service.start()
        self._service = embedded_service
        token = secrets.token_urlsafe(32)
        application = KnowledgeAssistantApi(embedded_service, api_token=token)
        try:
            server = make_server(
                "127.0.0.1",
                0,
                application,
                server_class=ThreadingWSGIServer,
                handler_class=_QuietRequestHandler,
            )
        except Exception:
            self._service = None
            embedded_service.close()
            raise
        self._server = server
        self.database_path = embedded_service.database.path
        self.client = KnowledgeAssistantApiClient(
            f"http://127.0.0.1:{server.server_port}",
            token,
        )
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="knowledge-assistant-private-api",
            daemon=True,
        )
        self._thread.start()
        try:
            health = self.client.health()
        except Exception:
            self.close()
            raise
        if health.get("status") != "ok":
            self.close()
            raise ConnectionError("private Knowledge Assistant service failed to start")
        self.service_instance_id = str(health.get("service_instance_id") or "").strip()
        self.recovery_binding_id = _recovery_binding_id(
            self.mode, self.client.base_url, self.service_instance_id
        )

    @property
    def base_url(self) -> str:
        return self.client.base_url

    def close(self) -> bool:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if thread is not None and thread.is_alive():
            return False
        self._server = None
        self._thread = None
        service = self._service
        if service is not None:
            if service.close() is False:
                return False
            self._service = None
        return True


class KnowledgeAssistantDesktopController:
    """Small, testable adapter used by the local visual administration console."""

    def __init__(self, service: KnowledgeAssistantService | None = None) -> None:
        self.endpoint = ManagedKnowledgeAssistantEndpoint(service)
        self.client = self.endpoint.client
        self._ingestion_coordinator: IngestionCoordinator | None = None

    @property
    def database_path(self) -> Path | None:
        return self.endpoint.database_path

    @property
    def connection_label(self) -> str:
        return f"{self.endpoint.mode} · {self.endpoint.base_url}"

    def close(self) -> bool:
        coordinator = self._ingestion_coordinator
        if coordinator is not None:
            if not coordinator.shutdown(2000):
                return False
            self._ingestion_coordinator = None
        if not self.endpoint.close():
            return False
        return True

    @staticmethod
    def principal(
        tenant_id: str,
        user_id: str,
        roles: str | Iterable[str],
        collection_ids: str | Iterable[str] = "",
    ) -> Principal:
        role_values = roles.split(",") if isinstance(roles, str) else roles
        collection_input_is_text = isinstance(collection_ids, str)
        collection_values = (
            collection_ids.split(",") if collection_input_is_text else collection_ids
        )
        normalized_roles = frozenset(
            str(item).strip().lower() for item in role_values if str(item).strip()
        )
        normalized_collections = frozenset(
            str(item).strip() for item in collection_values if str(item).strip()
        )
        return Principal(
            tenant_id=tenant_id.strip(),
            user_id=user_id.strip(),
            roles=normalized_roles,
            collection_ids=(
                None
                if collection_input_is_text and not normalized_collections
                else normalized_collections
            ),
        )

    @staticmethod
    def collection_list(value: str) -> list[str] | None:
        collections = [item.strip() for item in value.split(",") if item.strip()]
        return collections or None

    @staticmethod
    def new_idempotency_key(prefix: str) -> str:
        safe_prefix = "".join(
            character
            for character in prefix.lower()
            if character.isalnum() or character == "-"
        )
        return f"{safe_prefix or 'desktop'}-{uuid.uuid4().hex}"

    def list_documents(self, principal: Principal, *, limit: int = 200) -> list[dict]:
        del limit
        return self.client.list_documents(principal)

    def lookup_documents_by_checksum(
        self,
        principal: Principal,
        *,
        collection_id: str,
        checksums: list[str],
    ) -> list[dict]:
        return self.client.lookup_documents_by_checksum(
            principal,
            collection_id=collection_id,
            checksums=checksums,
        )

    def upload_file(
        self,
        principal: Principal,
        *,
        path: Path,
        collection_id: str,
        idempotency_key: str,
    ) -> dict:
        filename, content = self.prepare_upload(path)
        return self.upload_content(
            principal,
            filename=filename,
            content=content,
            collection_id=collection_id,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def prepare_upload(path: Path) -> tuple[str, bytes]:
        source = Path(path)
        try:
            with source.open("rb") as stream:
                source_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(source_stat.st_mode):
                    raise ValueError("请选择一个可读取的文档文件")
                if source_stat.st_size > MAX_DOCUMENT_BYTES:
                    raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
                content = stream.read(MAX_DOCUMENT_BYTES + 1)
        except OSError as exc:
            raise ValueError("请选择一个可读取的文档文件") from exc
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        return source.name, content

    def upload_content(
        self,
        principal: Principal,
        *,
        filename: str,
        content: bytes,
        collection_id: str,
        idempotency_key: str,
    ) -> dict:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        return self.client.upload_document(
            principal,
            filename=filename,
            content=content,
            collection_id=collection_id,
            idempotency_key=idempotency_key,
        )

    def query(
        self,
        principal: Principal,
        *,
        query: str,
        collection_ids: str = "",
        top_k: int = 5,
    ) -> dict:
        return self.client.query(
            principal,
            query=query,
            collection_ids=self.collection_list(collection_ids),
            top_k=top_k,
        )

    def list_tasks(self, principal: Principal, *, limit: int = 200) -> list[dict]:
        del limit
        return self.client.list_tasks(principal)

    def create_ingest_task(
        self,
        principal: Principal,
        *,
        filename: str,
        collection_id: str,
        content: str,
        idempotency_key: str,
    ) -> dict:
        return self.client.create_task(
            principal,
            tool_name="ingest_text",
            arguments={
                "filename": filename,
                "collection_id": collection_id,
                "content": content,
            },
            idempotency_key=idempotency_key,
        )

    def create_archive_task(
        self,
        principal: Principal,
        *,
        document_id: str,
        idempotency_key: str,
    ) -> dict:
        return self.client.create_task(
            principal,
            tool_name="archive_document",
            arguments={"document_id": document_id},
            idempotency_key=idempotency_key,
        )

    def approval_preview(self, principal: Principal, task_id: str) -> dict:
        return self.client.approval_preview(principal, task_id)

    def approve_task(self, principal: Principal, task_id: str, preview_hash: str) -> dict:
        return self.client.approve_task(principal, task_id, preview_hash)

    def reject_task(self, principal: Principal, task_id: str, reason: str = "") -> dict:
        return self.client.reject_task(principal, task_id, reason)

    def metrics(self, principal: Principal, *, limit: int = 1000) -> dict:
        del limit
        return self.client.metrics(principal)

    def get_trace(self, principal: Principal, trace_id: str) -> dict:
        return self.client.get_trace(principal, trace_id.strip())

    @property
    def service_instance_id(self) -> str:
        return self.endpoint.service_instance_id

    @property
    def recovery_binding_id(self) -> str:
        return self.endpoint.recovery_binding_id

    def create_ingestion_coordinator(
        self,
        *,
        protector: DataProtector | None = None,
    ) -> IngestionCoordinator:
        if self._ingestion_coordinator is not None:
            return self._ingestion_coordinator
        outbox = SecureIngestionOutbox(
            runtime_path("knowledge_assistant_ingestion_outbox", migrate_legacy=False),
            protector=protector,
        )
        self._ingestion_coordinator = IngestionCoordinator(
            self, outbox, self.recovery_binding_id
        )
        return self._ingestion_coordinator

    def create_ingestion_job(
        self,
        principal: Principal,
        *,
        filename: str,
        content: bytes,
        collection_id: str,
        idempotency_key: str,
    ) -> dict:
        return self.client.create_ingestion_job(
            principal,
            filename=filename,
            content=content,
            collection_id=collection_id,
            idempotency_key=idempotency_key,
        )

    def list_ingestion_jobs(self, principal: Principal) -> list[dict]:
        return self.client.list_ingestion_jobs(principal)

    def get_ingestion_job(self, principal: Principal, job_id: str) -> dict:
        return self.client.get_ingestion_job(principal, job_id)

    def cancel_ingestion_job(self, principal: Principal, job_id: str) -> dict:
        return self.client.cancel_ingestion_job(principal, job_id)
