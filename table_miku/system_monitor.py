from __future__ import annotations

import ctypes
import ctypes.wintypes
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .storage import load_settings


@dataclass(frozen=True)
class ProbeResult:
    name: str
    url: str
    ok: bool
    elapsed_ms: int
    error: str = ""
    error_kind: str = ""
    dns_ok: bool | None = None
    tcp_ok: bool | None = None
    tls_ok: bool | None = None
    http_status: int | None = None
    host: str = ""
    port: int | None = None


@dataclass(frozen=True)
class SystemSnapshot:
    cpu_percent: float | None
    memory_percent: float | None
    memory_available_mb: int | None
    network: list[ProbeResult]


class CpuSampler:
    """Sample total Windows CPU usage from kernel times without extra deps."""

    def __init__(self) -> None:
        self._last: tuple[int, int, int] | None = None

    def sample(self) -> float | None:
        current = self._read_system_times()
        if current is None:
            return None
        if self._last is None:
            self._last = current
            return None

        last_idle, last_kernel, last_user = self._last
        idle, kernel, user = current
        self._last = current

        idle_delta = idle - last_idle
        total_delta = (kernel - last_kernel) + (user - last_user)
        if total_delta <= 0:
            return None
        busy = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
        return busy

    @staticmethod
    def _read_system_times() -> tuple[int, int, int] | None:
        filetime = ctypes.wintypes.FILETIME  # type: ignore[attr-defined]
        idle = filetime()
        kernel = filetime()
        user = filetime()
        ok = ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return (
            CpuSampler._filetime_to_int(idle),
            CpuSampler._filetime_to_int(kernel),
            CpuSampler._filetime_to_int(user),
        )

    @staticmethod
    def _filetime_to_int(value: Any) -> int:
        return (int(value.dwHighDateTime) << 32) + int(value.dwLowDateTime)


class MemorySampler:
    def sample(self) -> tuple[float, int] | None:
        status = self._memory_status()
        if status is None or status.ullTotalPhys <= 0:
            return None
        used_percent = float(status.dwMemoryLoad)
        available_mb = int(status.ullAvailPhys / 1024 / 1024)
        return used_percent, available_mb

    @staticmethod
    def _memory_status() -> Any | None:
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.wintypes.DWORD),
                ("dwMemoryLoad", ctypes.wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

            def __init__(self) -> None:
                super().__init__()
                self.dwLength = ctypes.sizeof(self)

        status = MemoryStatusEx()
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return status if ok else None


class SystemMonitor(QObject):
    notice = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cpu = CpuSampler()
        self._memory = MemorySampler()
        self._cpu_high_count = 0
        self._memory_high_count = 0
        self._cpu_alerting = False
        self._memory_alerting = False
        self._last_network_state = ""
        self._last_network_report_at: datetime | None = None
        self._network_running = False
        self._last_network_check_at: datetime | None = None
        self._network_bad_count = 0
        self._network_alerting = False
        self.latest_snapshot = SystemSnapshot(None, None, None, [])

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start()
        QTimer.singleShot(4_000, self._first_check)

    def check_now(self) -> None:
        self._check_cpu(force_report=True)
        self._check_memory(force_report=True)
        self._check_network(force_report=True)

    def _first_check(self) -> None:
        settings = load_settings()
        monitor = settings.get("system_monitor") or {}
        if not monitor.get("enabled", True):
            return
        self._check_cpu(force_report=False)
        self._check_network(force_report=True)

    def _tick(self) -> None:
        settings = load_settings()
        monitor = settings.get("system_monitor") or {}
        if not monitor.get("enabled", True):
            return

        interval_seconds = int(monitor.get("check_interval_seconds", 30))
        self._timer.setInterval(max(interval_seconds, 10) * 1000)
        self._check_cpu(force_report=False)
        self._check_memory(force_report=False)

        network_minutes = int(monitor.get("network_check_interval_minutes", 5))
        due_at = datetime.now() - timedelta(minutes=max(network_minutes, 1))
        if self._last_network_check_at is None or self._last_network_check_at <= due_at:
            self._check_network(force_report=False)

    def _check_cpu(self, force_report: bool) -> None:
        monitor = (load_settings().get("system_monitor") or {})
        if not monitor.get("cpu_enabled", True):
            return

        usage = self._cpu.sample()
        if usage is None:
            if force_report:
                self.notice.emit("focus", "CPU 监测正在校准，再过十几秒就能给出准确占用。")
            return

        threshold = float(monitor.get("cpu_warning_percent", 85))
        required_checks = int(monitor.get("cpu_warning_checks", 3))
        if usage >= threshold:
            self._cpu_high_count += 1
        else:
            self._cpu_high_count = 0

        if force_report:
            level = "surprised" if usage >= threshold else "smile"
            self.notice.emit(level, f"电脑状态：CPU 当前 {usage:.0f}%。")
            return

        if self._cpu_high_count >= max(required_checks, 1) and not self._cpu_alerting:
            self._cpu_alerting = True
            self.notice.emit("surprised", f"CPU 连续偏高：当前约 {usage:.0f}%。如果电脑发热或卡顿，可以先关掉重任务。")
        elif self._cpu_alerting and usage < max(threshold - 15, 45):
            self._cpu_alerting = False
            self.notice.emit("smile", f"CPU 已恢复：当前约 {usage:.0f}%，电脑喘过气来了。")

        self.latest_snapshot = SystemSnapshot(
            cpu_percent=usage,
            memory_percent=self.latest_snapshot.memory_percent,
            memory_available_mb=self.latest_snapshot.memory_available_mb,
            network=self.latest_snapshot.network,
        )

    def _check_memory(self, force_report: bool) -> None:
        monitor = (load_settings().get("system_monitor") or {})
        if not monitor.get("memory_enabled", True):
            return

        sampled = self._memory.sample()
        if sampled is None:
            if force_report:
                self.notice.emit("focus", "内存监测暂时读不到数据，稍后我会再试。")
            return

        used_percent, available_mb = sampled
        self.latest_snapshot = SystemSnapshot(
            cpu_percent=self.latest_snapshot.cpu_percent,
            memory_percent=used_percent,
            memory_available_mb=available_mb,
            network=self.latest_snapshot.network,
        )
        threshold = float(monitor.get("memory_warning_percent", 88))
        required_checks = int(monitor.get("memory_warning_checks", 2))
        pressure_reason = memory_pressure_reason(used_percent, available_mb, monitor)
        if pressure_reason:
            self._memory_high_count += 1
        else:
            self._memory_high_count = 0

        if force_report:
            level = "surprised" if pressure_reason else "smile"
            self.notice.emit(level, f"内存状态：已用 {used_percent:.0f}%，可用约 {available_mb} MB。")
            return

        if self._memory_high_count >= max(required_checks, 1) and not self._memory_alerting:
            self._memory_alerting = True
            self.notice.emit("surprised", f"内存压力偏高：{pressure_reason}。可以先关掉浏览器重标签或大程序。")
        elif self._memory_alerting and not memory_pressure_reason(used_percent, available_mb, _memory_recovery_settings(monitor)):
            self._memory_alerting = False
            self.notice.emit("smile", f"内存已恢复：已用约 {used_percent:.0f}%，可用 {available_mb} MB。")

    def _check_network(self, force_report: bool) -> None:
        monitor = (load_settings().get("system_monitor") or {})
        if not monitor.get("network_enabled", True) or self._network_running:
            return

        self._network_running = True
        self._last_network_check_at = datetime.now()
        thread = threading.Thread(
            target=self._network_worker,
            args=(monitor, force_report),
            daemon=True,
        )
        thread.start()

    def _network_worker(self, monitor: dict[str, Any], force_report: bool) -> None:
        try:
            timeout = float(monitor.get("network_timeout_seconds", 4))
            targets = monitor.get("network_targets") or [
                {"name": "百度", "url": "https://www.baidu.com/"},
                {"name": "Google", "url": "https://www.google.com/generate_204"},
            ]
            results = [probe_network_target(str(item["name"]), str(item["url"]), timeout) for item in targets]
            self.latest_snapshot = SystemSnapshot(
                cpu_percent=self.latest_snapshot.cpu_percent,
                memory_percent=self.latest_snapshot.memory_percent,
                memory_available_mb=self.latest_snapshot.memory_available_mb,
                network=results,
            )
            state = network_state_key(results)
            now = datetime.now()
            healthy_minutes = int(monitor.get("network_healthy_report_minutes", 30))
            report_healthy_after = timedelta(minutes=max(healthy_minutes, 5))
            healthy = network_is_healthy(results)
            required_checks = int(monitor.get("network_warning_checks", 2))
            if healthy:
                self._network_bad_count = 0
            else:
                self._network_bad_count += 1

            should_report = (
                force_report
                or (healthy and self._network_alerting)
                or (not healthy and self._network_bad_count >= max(required_checks, 1) and state != self._last_network_state)
                or (healthy and not self._network_alerting and (
                    self._last_network_report_at is None
                    or now - self._last_network_report_at >= report_healthy_after
                ))
            )
            if should_report:
                self._last_network_state = state
                self._last_network_report_at = now
                self._network_alerting = not healthy
                level, message = format_network_notice(results)
                self.notice.emit(level, message)
        finally:
            self._network_running = False


def probe_network_target(name: str, url: str, timeout: float) -> ProbeResult:
    start = time.perf_counter()
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not parsed.scheme or not host:
        return ProbeResult(
            name=name,
            url=url,
            ok=False,
            elapsed_ms=0,
            error="invalid url",
            error_kind="invalid_url",
            dns_ok=False,
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return _probe_failure(name, url, start, "dns", exc, host=host, port=port, dns_ok=False)
    except OSError as exc:
        return _probe_failure(name, url, start, "dns", exc, host=host, port=port, dns_ok=False)

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except (socket.timeout, TimeoutError) as exc:
        return _probe_failure(name, url, start, "timeout", exc, host=host, port=port, dns_ok=True, tcp_ok=False)
    except OSError as exc:
        return _probe_failure(name, url, start, "tcp", exc, host=host, port=port, dns_ok=True, tcp_ok=False)

    tls_ok: bool | None = None
    try:
        if parsed.scheme == "https":
            try:
                context = ssl.create_default_context()
                with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                    tls_sock.settimeout(timeout)
                tls_ok = True
            except ssl.SSLError as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                return _probe_failure(name, url, start, "tls", exc, host=host, port=port, dns_ok=True, tcp_ok=True, tls_ok=False)
        else:
            sock.close()
    except OSError:
        pass

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Table-Miku/0.6 network monitor"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
            ok = 200 <= int(response.status) < 400
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return ProbeResult(
                name=name,
                url=url,
                ok=ok,
                elapsed_ms=elapsed_ms,
                error_kind="" if ok else "http_status",
                dns_ok=True,
                tcp_ok=True,
                tls_ok=tls_ok,
                http_status=int(response.status),
                host=host,
                port=port,
            )
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name=name,
            url=url,
            ok=False,
            elapsed_ms=elapsed_ms,
            error=str(exc),
            error_kind="http_status",
            dns_ok=True,
            tcp_ok=True,
            tls_ok=tls_ok,
            http_status=int(exc.code),
            host=host,
            port=port,
        )
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(
            name=name,
            url=url,
            ok=False,
            elapsed_ms=elapsed_ms,
            error=str(exc),
            error_kind=classify_network_error(exc),
            dns_ok=True,
            tcp_ok=True,
            tls_ok=tls_ok,
            host=host,
            port=port,
        )


def format_network_notice(results: list[ProbeResult]) -> tuple[str, str]:
    if not results:
        return "focus", "网络检测没有配置目标。"
    by_name = {result.name.lower(): result for result in results}
    baidu = by_name.get("百度") or by_name.get("baidu")
    google = by_name.get("google") or by_name.get("谷歌")

    if baidu and google:
        if baidu.ok and google.ok:
            return "smile", f"网络正常：百度 {baidu.elapsed_ms}ms，Google {google.elapsed_ms}ms，都连得上。"
        if baidu.ok and not google.ok:
            return "surprised", f"国内网络正常，但 Google 异常：{probe_failure_summary(google)}。开了 VPN 的话，可能是代理或出口有问题。"
        if not baidu.ok and google.ok:
            return "surprised", f"Google 能连上，但百度异常：{probe_failure_summary(baidu)}。可能是 DNS、国内链路或代理规则异常。"
        return "surprised", f"百度和 Google 都异常：百度 {probe_failure_summary(baidu)}；Google {probe_failure_summary(google)}。"

    ok_results = [result for result in results if result.ok]
    if len(ok_results) == len(results):
        details = "，".join(format_probe_success(result) for result in results)
        return "smile", f"网络正常：{details}。"
    failed = "；".join(f"{result.name} {probe_failure_summary(result)}" for result in results if not result.ok)
    reachable = "、".join(result.name for result in ok_results)
    if reachable:
        return "surprised", f"网络部分异常：{failed}。可达：{reachable}。"
    return "surprised", f"网络异常：{failed}。"


def memory_pressure_reason(used_percent: float, available_mb: int, monitor: dict[str, Any]) -> str:
    percent_threshold = float(monitor.get("memory_warning_percent", 88))
    available_threshold = int(monitor.get("memory_available_warning_mb", 1024))
    reasons: list[str] = []
    if used_percent >= percent_threshold:
        reasons.append(f"已用约 {used_percent:.0f}%")
    if available_mb <= available_threshold:
        reasons.append(f"可用仅 {available_mb} MB")
    return "，".join(reasons)


def _memory_recovery_settings(monitor: dict[str, Any]) -> dict[str, Any]:
    percent_threshold = max(float(monitor.get("memory_warning_percent", 88)) - 12, 55)
    available_threshold = int(monitor.get("memory_available_warning_mb", 1024)) + 512
    return {
        **monitor,
        "memory_warning_percent": percent_threshold,
        "memory_available_warning_mb": available_threshold,
    }


def network_is_healthy(results: list[ProbeResult]) -> bool:
    return bool(results) and all(result.ok for result in results)


def network_state_key(results: list[ProbeResult]) -> str:
    return "|".join(
        f"{result.name}:{int(result.ok)}:{result.error_kind}:{result.http_status or ''}"
        for result in results
    )


def classify_network_error(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    text = str(reason or exc).lower()
    if isinstance(reason, socket.gaierror) or "name or service" in text or "getaddrinfo" in text:
        return "dns"
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in text or "timeout" in text:
        return "timeout"
    if "connection refused" in text or "connection reset" in text:
        return "tcp"
    return "network"


def format_probe_success(result: ProbeResult) -> str:
    status = f" HTTP {result.http_status}" if result.http_status is not None else ""
    return f"{result.name} {result.elapsed_ms}ms{status}"


def probe_failure_summary(result: ProbeResult) -> str:
    kind_labels = {
        "invalid_url": "URL 无效",
        "dns": "DNS 解析失败",
        "tcp": "TCP 连接失败",
        "tls": "TLS 握手失败",
        "timeout": "连接超时",
        "http_status": f"HTTP {result.http_status}",
        "network": "网络请求失败",
    }
    label = kind_labels.get(result.error_kind, result.error_kind or "未知错误")
    if result.elapsed_ms:
        label += f"，耗时 {result.elapsed_ms}ms"
    return label


def _probe_failure(
    name: str,
    url: str,
    start: float,
    error_kind: str,
    exc: BaseException,
    *,
    host: str,
    port: int,
    dns_ok: bool | None,
    tcp_ok: bool | None = None,
    tls_ok: bool | None = None,
) -> ProbeResult:
    return ProbeResult(
        name=name,
        url=url,
        ok=False,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
        error=str(exc),
        error_kind=error_kind,
        dns_ok=dns_ok,
        tcp_ok=tcp_ok,
        tls_ok=tls_ok,
        host=host,
        port=port,
    )
