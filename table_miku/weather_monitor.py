from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from .paths import PROJECT_ROOT
from .storage import load_settings, save_settings
from .weather import fetch_open_meteo, resolve_location


WMO_RAIN_CODES = {51, 53, 55, 61, 63, 65, 71, 73, 75, 80, 81, 82, 95}

SEASONAL_TIPS = {
    "rain": "预计会下雨，出门记得带伞 🌂",
    "snow": "预计有雪，出行注意防滑 ❄️",
    "heavy_rain": "预计有大雨，非必要不出门 ☔",
    "hot": "今天 {temp}°C，注意防暑 🥵",
    "cold": "今天 {temp}°C，注意保暖 🧣",
    "wind": "风有点大（{wind}km/h），出门注意安全 🍃",
}


class WeatherMonitor(QObject):
    """主动天气监测服务，发现恶劣天气时通过 notice 信号提醒"""

    notice = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(20 * 60 * 1000)  # 20 分钟
        self._timer.timeout.connect(self._check)
        self._last_alert_at: datetime | None = None
        self._last_weather: str = ""

    def start(self) -> None:
        """启动定时检查"""
        self._timer.start()
        # 启动后 5 秒首检
        QTimer.singleShot(5000, self._check)

    def stop(self) -> None:
        self._timer.stop()

    def check_now(self) -> None:
        """手动触发一次检查"""
        self._check()

    def _check(self) -> None:
        settings = load_settings()
        city = settings.get("city", "auto")
        try:
            location = resolve_location(city)
            weather = fetch_open_meteo(location["latitude"], location["longitude"])
            current = weather.get("current") or {}
            alerts = self._evaluate(current)

            if alerts:
                self._last_weather = alerts[0]
                self.notice.emit("surprised", alerts[0])
        except Exception:
            pass  # 静默失败，不打断用户

    def _evaluate(self, current: dict) -> list[str]:
        """评估当前天气，返回需要提醒的消息列表"""
        alerts: list[str] = []
        code = current.get("weather_code", 0)
        temp = current.get("temperature_2m")
        wind = current.get("wind_speed_10m")

        if code in WMO_RAIN_CODES:
            if code in {65, 82, 95}:
                alerts.append(SEASONAL_TIPS["heavy_rain"])
            elif code >= 70:
                alerts.append(SEASONAL_TIPS["snow"])
            else:
                alerts.append(SEASONAL_TIPS["rain"])

        if temp is not None:
            if temp >= 35:
                alerts.append(SEASONAL_TIPS["hot"].format(temp=int(temp)))
            elif temp <= 0:
                alerts.append(SEASONAL_TIPS["cold"].format(temp=int(temp)))

        if wind is not None and wind >= 30:
            alerts.append(SEASONAL_TIPS["wind"].format(wind=int(wind)))

        return alerts
