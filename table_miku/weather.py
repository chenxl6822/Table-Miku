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


def _get_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Table-Miku/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_weather(city: str) -> str:
    city = city.strip() or "Shanghai"
    query = urllib.parse.urlencode({"name": city, "count": 1, "language": "zh"})
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?{query}"
    geo = _get_json(geo_url)
    results = geo.get("results") or []
    if not results:
        return f"没有找到「{city}」的天气位置，换个城市名试试吧。"

    location = results[0]
    latitude = location["latitude"]
    longitude = location["longitude"]
    display_name = location.get("name", city)
    weather_query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
        }
    )
    weather_url = f"https://api.open-meteo.com/v1/forecast?{weather_query}"
    weather = _get_json(weather_url)
    current = weather.get("current") or {}
    temperature = current.get("temperature_2m")
    wind = current.get("wind_speed_10m")
    code = current.get("weather_code")
    description = WMO_DESCRIPTIONS.get(code, "天气情况未知")
    return f"{display_name}现在{description}，{temperature}°C，风速 {wind} km/h。出门记得看天色，学习也别忘了喝水。"
