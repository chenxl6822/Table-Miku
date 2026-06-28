import socket

from table_miku import system_monitor
from table_miku.system_monitor import ProbeResult, SystemMonitor


def test_memory_pressure_reason_uses_percent_and_available_mb():
    monitor = {"memory_warning_percent": 88, "memory_available_warning_mb": 1024}

    assert system_monitor.memory_pressure_reason(90, 800, monitor) == "已用约 90%，可用仅 800 MB"
    assert system_monitor.memory_pressure_reason(70, 800, monitor) == "可用仅 800 MB"
    assert system_monitor.memory_pressure_reason(70, 2048, monitor) == ""


def test_probe_network_target_classifies_dns_failure(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        raise socket.gaierror("mock dns failed")

    monkeypatch.setattr(system_monitor.socket, "getaddrinfo", fake_getaddrinfo)

    result = system_monitor.probe_network_target("Docs", "https://docs.example.test/", timeout=0.1)

    assert result.ok is False
    assert result.error_kind == "dns"
    assert result.dns_ok is False
    assert result.host == "docs.example.test"


def test_probe_network_target_rejects_invalid_url():
    result = system_monitor.probe_network_target("Bad", "not-a-url", timeout=0.1)

    assert result.ok is False
    assert result.error_kind == "invalid_url"


def test_format_network_notice_reports_partial_failure_detail():
    results = [
        ProbeResult("内网", "https://internal.example/", True, 32, http_status=204),
        ProbeResult("Docs", "https://docs.example/", False, 120, error_kind="tls", tls_ok=False),
    ]

    level, message = system_monitor.format_network_notice(results)

    assert level == "surprised"
    assert "网络部分异常" in message
    assert "TLS 握手失败" in message
    assert "可达：内网" in message


def test_network_worker_waits_for_consecutive_failures(monkeypatch):
    monitor = SystemMonitor()
    messages: list[str] = []
    monitor.notice.connect(lambda _expression, message: messages.append(message))
    fail = ProbeResult("Docs", "https://docs.example/", False, 10, error_kind="dns", dns_ok=False)

    monkeypatch.setattr(system_monitor, "probe_network_target", lambda name, url, timeout: fail)

    config = {
        "network_targets": [{"name": "Docs", "url": "https://docs.example/"}],
        "network_timeout_seconds": 1,
        "network_warning_checks": 2,
    }
    monitor._network_worker(config, force_report=False)
    monitor._network_worker(config, force_report=False)

    assert len(messages) == 1
    assert "DNS 解析失败" in messages[0]


def test_network_worker_reports_recovery_after_alert(monkeypatch):
    monitor = SystemMonitor()
    messages: list[str] = []
    monitor.notice.connect(lambda _expression, message: messages.append(message))
    fail = ProbeResult("Docs", "https://docs.example/", False, 10, error_kind="dns", dns_ok=False)
    ok = ProbeResult("Docs", "https://docs.example/", True, 20, http_status=204)
    results = [fail, fail, ok]

    monkeypatch.setattr(system_monitor, "probe_network_target", lambda name, url, timeout: results.pop(0))

    config = {
        "network_targets": [{"name": "Docs", "url": "https://docs.example/"}],
        "network_timeout_seconds": 1,
        "network_warning_checks": 2,
    }
    monitor._network_worker(config, force_report=False)
    monitor._network_worker(config, force_report=False)
    monitor._network_worker(config, force_report=False)

    assert len(messages) == 2
    assert "DNS 解析失败" in messages[0]
    assert "网络正常" in messages[1]
