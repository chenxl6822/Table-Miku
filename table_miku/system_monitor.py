from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
import urllib.error
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
        if used_percent >= threshold:
            self._memory_high_count += 1
        else:
            self._memory_high_count = 0

        if force_report:
            level = "surprised" if used_percent >= threshold else "smile"
            self.notice.emit(level, f"内存状态：已用 {used_percent:.0f}%，可用约 {available_mb} MB。")
            return

        if self._memory_high_count >= max(required_checks, 1) and not self._memory_alerting:
            self._memory_alerting = True
            self.notice.emit("surprised", f"内存压力偏高：已用约 {used_percent:.0f}%，可用 {available_mb} MB。可以先关掉浏览器重标签或大程序。")
        elif self._memory_alerting and used_percent < max(threshold - 12, 55):
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
            state = "|".join(f"{result.name}:{int(result.ok)}" for result in results)
            now = datetime.now()
            healthy_minutes = int(monitor.get("network_healthy_report_minutes", 30))
            report_healthy_after = timedelta(minutes=max(healthy_minutes, 5))
            should_report = (
                force_report
                or state != self._last_network_state
                or self._last_network_report_at is None
                or now - self._last_network_report_at >= report_healthy_after
            )
            if should_report:
                self._last_network_state = state
                self._last_network_report_at = now
                level, message = format_network_notice(results)
                self.notice.emit(level, message)
        finally:
            self._network_running = False


def probe_network_target(name: str, url: str, timeout: float) -> ProbeResult:
    start = time.perf_counter()
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
            return ProbeResult(name=name, url=url, ok=ok, elapsed_ms=elapsed_ms)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ProbeResult(name=name, url=url, ok=False, elapsed_ms=elapsed_ms, error=str(exc))


def format_network_notice(results: list[ProbeResult]) -> tuple[str, str]:
    by_name = {result.name.lower(): result for result in results}
    baidu = by_name.get("百度") or by_name.get("baidu")
    google = by_name.get("google") or by_name.get("谷歌")

    if baidu and google:
        if baidu.ok and google.ok:
            return "smile", f"网络正常：百度 {baidu.elapsed_ms}ms，Google {google.elapsed_ms}ms，都连得上。"
        if baidu.ok and not google.ok:
            return "surprised", "国内网络正常，但 Google 连不上。开了 VPN 的话，可能是代理或出口有问题。"
        if not baidu.ok and google.ok:
            return "surprised", "Google 能连上，但百度连不上。可能是 DNS、国内链路或代理规则异常。"
        return "surprised", "百度和 Google 都连不上，可能断网了，或者 VPN/代理配置把流量卡住了。"

    ok_results = [result for result in results if result.ok]
    if len(ok_results) == len(results):
        details = "，".join(f"{result.name} {result.elapsed_ms}ms" for result in results)
        return "smile", f"网络正常：{details}。"
    failed = "、".join(result.name for result in results if not result.ok)
    return "surprised", f"网络异常：{failed} 暂时连不上。"
