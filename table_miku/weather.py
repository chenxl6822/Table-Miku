from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


WMO_DESCRIPTIONS = {
    0: "晴朗",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴天",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    95: "雷雨",
}


def _get_json(url: str, timeout: float = 6.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Table-Miku/0.2"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def detect_location() -> dict[str, Any]:
    """Approximate the current location by public IP."""
    data = _get_json("http://ip-api.com/json/?lang=zh-CN")
    if data.get("status") != "success":
        raise RuntimeError("IP 定位失败")
    return {
        "name": data.get("city") or data.get("regionName") or "当前位置",
        "region": data.get("regionName") or "",
        "country": data.get("country") or "",
        "latitude": data["lat"],
        "longitude": data["lon"],
    }


def geocode_city(city: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"name": city, "count": 1, "language": "zh"})
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?{query}"
    geo = _get_json(geo_url)
    results = geo.get("results") or []
    if not results:
        return None
    location = results[0]
    return {
        "name": location.get("name", city),
        "region": location.get("admin1", ""),
        "country": location.get("country", ""),
        "latitude": location["latitude"],
        "longitude": location["longitude"],
    }


def get_weather(city: str = "auto") -> str:
    location: dict[str, Any] | None = None
    requested = (city or "auto").strip()

    if requested.lower() in {"auto", "定位", "自动定位", "当前位置"}:
        location = detect_location()
    else:
        location = geocode_city(requested)
        if location is None:
            return f"没有找到「{requested}」的天气位置。可以把城市设置为 auto，让 Miku 自动定位。"

    weather_query = urllib.parse.urlencode(
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "timezone": "auto",
        }
    )
    weather_url = f"https://api.open-meteo.com/v1/forecast?{weather_query}"
    weather = _get_json(weather_url)
    current = weather.get("current") or {}
    temperature = current.get("temperature_2m")
    wind = current.get("wind_speed_10m")
    humidity = current.get("relative_humidity_2m")
    code = current.get("weather_code")
    description = WMO_DESCRIPTIONS.get(code, "天气情况未知")

    place_bits = [location.get("name", "当前位置"), location.get("region"), location.get("country")]
    place = "，".join([str(bit) for bit in place_bits if bit])
    return f"{place}：现在{description}，{temperature}°C，湿度 {humidity}%，风速 {wind} km/h。"
