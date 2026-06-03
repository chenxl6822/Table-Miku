from __future__ import annotations

import logging
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal

from .storage import load_settings
from .weather import _weather_severity, fetch_open_meteo, resolve_location

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
            city = settings.get("city", "北京")
            location = resolve_location(city)
            if location.get("latitude") is None:
                return
            data = fetch_open_meteo(location["latitude"], location["longitude"])
            if data is None:
                return
            self._evaluate(data)
        except Exception as e:
            logger.error(f"WeatherMonitor check failed: {e}")

    def _evaluate(self, data: dict) -> None:
        alerts: list[str] = []
        now = datetime.now()
        current = data.get("current", {})
        temp = current.get("temperature_2m", 20)
        weather_code = current.get("weather_code", 0)
        wind_speed = current.get("wind_speed_10m", 0)

        # 雷暴 (95, 96, 99)
        if weather_code in (95, 96, 99) and self._can_alert("thunderstorm", now):
            if weather_code in (96, 99):
                alerts.append("⛈️ 当前有雷暴并伴有冰雹，请尽量避免外出！")
            else:
                alerts.append("⛈️ 当前有雷暴天气，注意安全~")
            self._last_alert["thunderstorm"] = now

        # 雾 (45, 48)
        if weather_code in (45, 48) and self._can_alert("fog", now):
            desc = "雾凇" if weather_code == 48 else "雾"
            alerts.append(f"🌫️ 当前有{desc}，能见度较低，出行注意安全~")
            self._last_alert["fog"] = now

        # 冻毛毛雨 (56, 57)
        if 56 <= weather_code <= 57 and self._can_alert("freeze", now):
            severity = _weather_severity(weather_code)
            alerts.append(f"🌧️ 当前有{severity}冻毛毛雨，路面可能结冰，注意防滑~")
            self._last_alert["freeze"] = now

        # 冻雨 (66, 67)
        if 66 <= weather_code <= 67 and self._can_alert("freeze", now):
            severity = _weather_severity(weather_code)
            alerts.append(f"🌧️ 当前有{severity}冻雨，路面可能结冰，注意防滑~")
            self._last_alert["freeze"] = now

        # 雨 (61-67 排除冻雨已处理的 66-67)
        if 61 <= weather_code <= 65:
            if self._can_alert("rain", now):
                severity = _weather_severity(weather_code)
                alerts.append(f"🌧️ 当前正在下{severity}雨，出门记得带伞~")
                self._last_alert["rain"] = now

        # 雪 (71-77 排除冻毛毛雨 56-57 的雪粒 77)
        if 71 <= weather_code <= 77:
            if self._can_alert("snow", now):
                severity = _weather_severity(weather_code)
                alerts.append(f"❄️ 当前正在下{severity}雪，注意保暖~")
                self._last_alert["snow"] = now

        # 阵雨/阵雪 (80-86)
        if 80 <= weather_code <= 82 and self._can_alert("rain", now):
            severity = _weather_severity(weather_code)
            alerts.append(f"🌧️ 当前有{severity}阵雨，出门记得带伞~")
            self._last_alert["rain"] = now
        if 85 <= weather_code <= 86 and self._can_alert("snow", now):
            severity = _weather_severity(weather_code)
            alerts.append(f"❄️ 当前有{severity}雪阵雨，注意保暖~")
            self._last_alert["snow"] = now

        # 高温
        if temp >= 35 and self._can_alert("heat", now):
            alerts.append(f"🔥 当前 {temp}°C，注意防暑~")
            self._last_alert["heat"] = now

        # 低温
        if temp <= -5 and self._can_alert("cold", now):
            alerts.append(f"🥶 当前 {temp}°C，注意保暖~")
            self._last_alert["cold"] = now

        # 大风 (>12.5 m/s ≈ 6级)
        if wind_speed > 12.5 and self._can_alert("wind", now):
            alerts.append(f"💨 当前风力较大 ({wind_speed}m/s)，注意安全~")
            self._last_alert["wind"] = now

        for msg in alerts:
            self.notice.emit("surprised", msg)

    def _can_alert(self, alert_type: str, now: datetime) -> bool:
        """冷却期内不重复提醒"""
        last = self._last_alert.get(alert_type)
        if last is None:
            return True
        return (now - last).total_seconds() > self._cooldown_minutes * 60
