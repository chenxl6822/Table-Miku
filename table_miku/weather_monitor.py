from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .storage import load_settings
from .weather import evaluate_weather_alerts, fetch_open_meteo, resolve_location

logger = logging.getLogger(__name__)


class WeatherMonitor(QObject):
    """主动天气监测服务，发现恶劣天气时通过 notice 信号提醒"""

    notice = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = True
        self._interval_minutes = 20
        self._cooldown_minutes = 60  # 同类提醒冷却
        self._last_alert: dict[str, datetime] = {}  # type → datetime 去重
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check)
        self._timer.setSingleShot(False)

    def start(self) -> None:
        """启动定时检查"""
        self._apply_settings(load_settings().get("weather_alerts") or {})
        # 启动后 5 秒首次检查
        QTimer.singleShot(5000, self._check)
        self._timer.start(self._interval_minutes * 60 * 1000)

    def stop(self) -> None:
        self._timer.stop()

    def check_now(self) -> None:
        """手动触发一次检查"""
        self._check()

    def _check(self) -> None:
        try:
            settings = load_settings()
            weather_alerts = settings.get("weather_alerts", {})
            if not weather_alerts.get("enabled", True):
                return
            self._apply_settings(weather_alerts)
            city = settings.get("city", "北京")
            location = resolve_location(city)
            if location.get("latitude") is None:
                return
            data = fetch_open_meteo(location["latitude"], location["longitude"], include_hourly=True)
            if data is None:
                return
            lead_minutes = int(weather_alerts.get("lead_minutes", 30))
            lead_hours = max(1, (max(lead_minutes, 0) + 59) // 60)
            self._evaluate(data, lead_hours=lead_hours)
        except Exception as e:
            logger.error(f"WeatherMonitor check failed: {e}")

    def _evaluate(self, data: dict[str, Any], lead_hours: int = 0) -> None:
        now = datetime.now()
        for alert in evaluate_weather_alerts(data, lead_hours=lead_hours):
            alert_type = str(alert.get("type") or "weather")
            if not self._can_alert(alert_type, now):
                continue
            self._last_alert[alert_type] = now
            self.notice.emit("surprised", str(alert.get("message") or "天气可能有变化，请留意出行。"))

    def _can_alert(self, alert_type: str, now: datetime) -> bool:
        """冷却期内不重复提醒"""
        last = self._last_alert.get(alert_type)
        if last is None:
            return True
        return (now - last).total_seconds() > self._cooldown_minutes * 60

    def _apply_settings(self, weather_alerts: dict[str, Any]) -> None:
        self._interval_minutes = max(int(weather_alerts.get("interval_minutes", self._interval_minutes)), 1)
        self._cooldown_minutes = max(int(weather_alerts.get("cooldown_minutes", self._cooldown_minutes)), 1)
        interval_ms = self._interval_minutes * 60 * 1000
        if self._timer.interval() != interval_ms:
            self._timer.setInterval(interval_ms)
