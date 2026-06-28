from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from .storage import read_json, write_json


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
    56: "冻毛毛雨（小）",
    57: "冻毛毛雨（较强）",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨（小）",
    67: "冻雨（较强）",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    85: "小雪阵雨",
    86: "大雪阵雨",
    95: "雷雨",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def _weather_severity(code: int) -> str:
    """Return severity label for a WMO weather code."""
    light = {51, 56, 61, 66, 71, 77, 80, 85}
    moderate = {53, 63, 73, 81}
    heavy = {55, 57, 65, 67, 75, 82, 86, 95, 96, 99}
    if code in light:
        return "轻度"
    if code in moderate:
        return "中等"
    if code in heavy:
        return "较强"
    # Freezing drizzle codes
    if code == 56:
        return "轻度"
    if code == 57:
        return "较强"
    return ""

PROVINCE_ALIASES = {
    "安徽": "安徽省",
    "湖南": "湖南省",
    "湖北": "湖北省",
    "广东": "广东省",
    "广西": "广西壮族自治区",
    "江西": "江西省",
    "江苏": "江苏省",
    "浙江": "浙江省",
    "福建": "福建省",
    "河南": "河南省",
    "河北": "河北省",
    "山东": "山东省",
    "山西": "山西省",
    "四川": "四川省",
    "重庆": "重庆市",
    "北京": "北京市",
    "上海": "上海市",
    "天津": "天津市",
}

CITY_PROVINCE_HINTS = {
    "湘潭": "湖南省",
    "长沙": "湖南省",
    "株洲": "湖南省",
    "衡阳": "湖南省",
    "岳阳": "湖南省",
}

LOCATION_CACHE_FILE = "weather_location_cache.json"
LOCATION_CACHE_TTL_DAYS = 14
AUTO_LOCATION_WORDS = {"auto", "定位", "自动定位", "当前位置"}


def _get_json(url: str, timeout: float = 8.0) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Table-Miku/0.5 (desktop pet weather lookup)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"天气服务暂时不可用 (HTTP {ex.code})") from ex
    except urllib.error.URLError as ex:
        raise RuntimeError("天气服务连接失败，请检查网络") from ex
    except json.JSONDecodeError as ex:
        raise RuntimeError("天气数据解析异常") from ex
    except OSError as ex:
        raise RuntimeError(f"网络请求异常: {ex}") from ex


def get_weather(location_text: str = "雨湖区,湘潭,湖南") -> str:
    requested = (location_text or "雨湖区,湘潭,湖南").strip()
    location = resolve_location(requested)
    weather = fetch_open_meteo(location["latitude"], location["longitude"], include_daily=True, include_hourly=True)
    current = weather.get("current") or {}
    daily = weather.get("daily") or {}

    code = current.get("weather_code")
    severity = _weather_severity(code) if code is not None else ""
    description = WMO_DESCRIPTIONS.get(code, "天气情况未知")
    if severity:
        description = severity + description
    source_note = format_location_source_note(location)

    trend = analyze_weather_trend(daily) if daily else ""
    upcoming = summarize_hourly_alerts(weather.get("hourly") or {}, lead_hours=6)

    lines: list[str] = []
    lines.append(
        f"{location['display_name']}：现在{description}，{current.get('temperature_2m')}°C，"
        f"体感 {current.get('apparent_temperature')}°C，湿度 {current.get('relative_humidity_2m')}%，"
        f"风速 {current.get('wind_speed_10m')} m/s。"
    )

    # Add daily temperature range if available
    daily_temp_max = daily.get("temperature_2m_max", [])
    daily_temp_min = daily.get("temperature_2m_min", [])
    if len(daily_temp_max) > 0 and len(daily_temp_min) > 0:
        lines.append(f"今日温度范围 {daily_temp_min[0]}°C ~ {daily_temp_max[0]}°C。")

    if trend:
        lines.append(trend)
    if upcoming:
        lines.append(upcoming)

    lines.append(source_note)
    return "\n".join(lines)


def resolve_location(location_text: str) -> dict[str, Any]:
    requested = (location_text or "雨湖区,湘潭,湖南").strip()
    coordinate_location = parse_coordinate_location(requested)
    if coordinate_location is not None:
        return coordinate_location

    if requested.lower() in AUTO_LOCATION_WORDS:
        location = detect_ip_location()
        return _normalize_location_result(location, requested, confidence="low")

    cached = _cached_location(requested, allow_stale=False)
    if cached is not None:
        return cached

    components = parse_china_location(requested)
    if components["city"] in CITY_PROVINCE_HINTS and not components["province"]:
        components["province"] = CITY_PROVINCE_HINTS[components["city"]]

    location = _try_geocoder(geocode_with_nominatim, components)
    if location is not None:
        location = _normalize_location_result(location, requested)
        _save_cached_location(requested, location)
        return location

    location = _try_geocoder(geocode_with_open_meteo, components)
    if location is not None:
        location = _normalize_location_result(location, requested)
        _save_cached_location(requested, location)
        return location

    stale = _cached_location(requested, allow_stale=True)
    if stale is not None:
        stale["cache_stale"] = True
        return stale

    hint = format_components(components)
    raise RuntimeError(f"没有找到「{hint}」的地理位置，请尝试输入'区县,城市,省份'，例如'雨湖区,湘潭,湖南'。")


def parse_coordinate_location(text: str) -> dict[str, Any] | None:
    """Parse manual coordinates from ``lat,lon`` or ``lat=..., lon=...`` text."""
    raw = text.strip()
    if not raw:
        return None

    labeled = re.search(
        r"(?:lat|latitude|纬度)\s*[:=]\s*(-?\d+(?:\.\d+)?)"
        r".*?(?:lon|lng|longitude|经度)\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        raw,
        re.IGNORECASE,
    )
    if labeled:
        latitude = float(labeled.group(1))
        longitude = float(labeled.group(2))
    else:
        normalized = raw.replace("，", ",").replace("、", ",")
        parts = [part.strip() for part in normalized.split(",")]
        if len(parts) != 2:
            return None
        if not all(re.fullmatch(r"-?\d+(?:\.\d+)?", part) for part in parts):
            return None
        latitude = float(parts[0])
        longitude = float(parts[1])

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise RuntimeError("坐标超出范围：纬度必须在 -90~90，经度必须在 -180~180。")

    return {
        "district": "",
        "city": "",
        "region": "",
        "country": "",
        "latitude": latitude,
        "longitude": longitude,
        "display_name": f"手动坐标 {latitude:.4f}, {longitude:.4f}",
        "source": "manual-coordinates",
        "confidence": "high",
        "query": raw,
    }


def parse_china_location(text: str) -> dict[str, str | None]:
    cleaned = re.sub(r"\s+", "", text)
    parts = [part for part in re.split(r"[,，、/|]+", cleaned) if part]
    district: str | None = None
    city: str | None = None
    province: str | None = None

    if len(parts) >= 3:
        district = _strip_area_suffix(parts[0])
        city = _strip_city_suffix(parts[1])
        province = normalize_province(parts[2])
    elif len(parts) == 2:
        first, second = parts
        if first in PROVINCE_ALIASES:
            province = normalize_province(first)
            city = _strip_city_suffix(second)
        elif second in PROVINCE_ALIASES:
            city = _strip_city_suffix(first)
            province = normalize_province(second)
        elif _looks_like_district(first):
            district = _strip_area_suffix(first)
            city = _strip_city_suffix(second)
        else:
            city = _strip_city_suffix(first)
            district = _strip_area_suffix(second) if _looks_like_district(second) else None
    else:
        city = _strip_city_suffix(cleaned)
        for short, full in PROVINCE_ALIASES.items():
            if cleaned.startswith(short) and len(cleaned) > len(short):
                province = full
                city = _strip_city_suffix(cleaned[len(short):])
                break
            if cleaned.endswith(short) and len(cleaned) > len(short):
                province = full
                city = _strip_city_suffix(cleaned[:-len(short)])
                break

    if city in CITY_PROVINCE_HINTS and province is None:
        province = CITY_PROVINCE_HINTS[city]

    city, district = _split_known_city_district(city, district)
    if city in CITY_PROVINCE_HINTS and province is None:
        province = CITY_PROVINCE_HINTS[city]

    return {"district": district, "city": city or cleaned, "province": province}


def normalize_province(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.replace("省", "").replace("市", "").strip()
    return PROVINCE_ALIASES.get(cleaned, text)


def geocode_with_nominatim(components: dict[str, str | None]) -> dict[str, Any] | None:
    query = format_components(components, include_country=True)
    params = {
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 10,
        "countrycodes": "cn",
        "accept-language": "zh-CN",
        "q": query,
    }

    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    results = _get_json(url)
    if not isinstance(results, list):
        return None

    ranked = sorted(results, key=lambda item: _location_score(item, components), reverse=True)
    for item in ranked:
        if _location_score(item, components) <= 0:
            continue
        address = item.get("address") or {}
        return {
            "district": _address_district(address) or components.get("district") or "",
            "city": _address_city(address) or components.get("city") or "",
            "region": components.get("province") or address.get("state") or "",
            "country": address.get("country") or "中国",
            "latitude": float(item["lat"]),
            "longitude": float(item["lon"]),
            "display_name": _display_name(components, address),
            "source": "nominatim",
        }
    return None


def geocode_with_open_meteo(components: dict[str, str | None]) -> dict[str, Any] | None:
    # Open-Meteo geocoding is city-level for many China entries, so it is a fallback
    # when Nominatim is unavailable. District text is preserved in display output.
    city = components.get("city") or components.get("district") or ""
    query = urllib.parse.urlencode({"name": city, "count": 10, "language": "zh", "countryCode": "CN"})
    geo = _get_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
    results = geo.get("results") or []
    if not results:
        return None

    best = max(results, key=lambda item: _open_meteo_score(item, components))
    if _open_meteo_score(best, components) <= 0:
        return None
    return {
        "district": components.get("district") or "",
        "city": best.get("name", city),
        "region": best.get("admin1", components.get("province") or ""),
        "country": best.get("country", "中国"),
        "latitude": float(best["latitude"]),
        "longitude": float(best["longitude"]),
        "display_name": format_components(
            {
                "district": components.get("district"),
                "city": best.get("name", city),
                "province": best.get("admin1", components.get("province")),
            },
            include_country=True,
        ),
        "source": "open-meteo-geocoding",
    }


def fetch_open_meteo(
    latitude: float,
    longitude: float,
    include_daily: bool = False,
    include_hourly: bool = False,
) -> dict[str, Any]:
    current_params = "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m"
    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "current": current_params,
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    if include_daily:
        params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        params["forecast_days"] = 3
    if include_hourly:
        params["hourly"] = "weather_code,temperature_2m,precipitation_probability,wind_speed_10m"
        params["forecast_days"] = max(int(params.get("forecast_days", 1)), 2)
    weather_query = urllib.parse.urlencode(params)
    return _get_json(f"https://api.open-meteo.com/v1/forecast?{weather_query}")


def evaluate_weather_alerts(data: dict[str, Any], lead_hours: int = 0) -> list[dict[str, Any]]:
    """Return current and near-future weather alerts from Open-Meteo data.

    Wind speed is expected in m/s because ``fetch_open_meteo`` requests
    ``wind_speed_unit=ms``.
    """
    alerts: list[dict[str, Any]] = []
    current = data.get("current") or {}
    alerts.extend(
        _alerts_from_sample(
            current.get("weather_code"),
            current.get("temperature_2m"),
            current.get("wind_speed_10m"),
            source="current",
            time_label="当前",
        )
    )

    if lead_hours <= 0:
        return alerts

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    codes = hourly.get("weather_code") or []
    temps = hourly.get("temperature_2m") or []
    winds = hourly.get("wind_speed_10m") or []
    limit = min(len(codes), max(int(lead_hours), 1) + 1)
    current_types = {alert["type"] for alert in alerts}
    future_types: set[str] = set()

    for index in range(limit):
        time_label = _format_hour_label(times[index] if index < len(times) else "")
        sample_alerts = _alerts_from_sample(
            codes[index],
            temps[index] if index < len(temps) else None,
            winds[index] if index < len(winds) else None,
            source="hourly",
            time_label=time_label,
        )
        for alert in sample_alerts:
            alert_type = str(alert.get("type", ""))
            if alert_type in current_types or alert_type in future_types:
                continue
            alert["time"] = times[index] if index < len(times) else ""
            alert["message"] = _future_alert_message(alert)
            alerts.append(alert)
            future_types.add(alert_type)
    return alerts


def summarize_hourly_alerts(hourly_data: dict[str, Any], lead_hours: int = 6) -> str:
    if not hourly_data:
        return ""
    alerts = evaluate_weather_alerts({"hourly": hourly_data}, lead_hours=lead_hours)
    future_messages = [
        str(alert.get("message", ""))
        for alert in alerts
        if alert.get("source") == "hourly" and alert.get("message")
    ]
    if not future_messages:
        return ""
    return "未来几小时提醒：" + "；".join(future_messages[:3])


def analyze_weather_trend(daily_data: dict[str, Any]) -> str:
    """Return a Chinese-language trend summary based on daily forecast data."""
    if not daily_data:
        return ""
    time_list = daily_data.get("time", [])
    code_list = daily_data.get("weather_code", [])
    precip_list = daily_data.get("precipitation_probability_max", [])
    temp_max_list = daily_data.get("temperature_2m_max", [])
    temp_min_list = daily_data.get("temperature_2m_min", [])

    if len(time_list) < 2 or len(code_list) < 2:
        return ""

    parts: list[str] = []

    # Today vs tomorrow weather code change
    today_code = code_list[0] if isinstance(code_list[0], int) else 0
    tomorrow_code = code_list[1] if isinstance(code_list[1], int) else 0
    today_precip = precip_list[0] if len(precip_list) > 0 else 0
    tomorrow_precip = precip_list[1] if len(precip_list) > 1 else 0

    # Rain coming or going
    if tomorrow_code in range(51, 100) and today_code not in range(51, 100):
        parts.append("明天可能有降水，记得带伞")
    if tomorrow_precip >= 50 and today_precip < 30:
        parts.append("明天降雨概率较高")
    if today_code in range(51, 100) and tomorrow_code not in range(51, 100):
        parts.append("今天降水预计明天转好")

    # Thunderstorm warning
    if tomorrow_code in (95, 96, 99):
        parts.append("明天可能有雷暴天气")

    # Temperature trend
    if len(temp_max_list) >= 2 and len(temp_min_list) >= 2:
        today_high = temp_max_list[0]
        tomorrow_high = temp_max_list[1]
        today_low = temp_min_list[0]
        tomorrow_low = temp_min_list[1]
        if tomorrow_high - today_high >= 5:
            parts.append(f"明天明显升温，最高 {tomorrow_high}°C")
        elif today_high - tomorrow_high >= 5:
            parts.append(f"明天明显降温，最高 {tomorrow_high}°C")
        if tomorrow_low <= 0 and today_low > 0:
            parts.append("明天最低温降至零度以下，注意防冻")

    # Day-after-tomorrow glance
    if len(code_list) >= 3:
        day3_code = code_list[2] if isinstance(code_list[2], int) else 0
        day3_desc = WMO_DESCRIPTIONS.get(day3_code, "")
        if day3_desc and day3_code != tomorrow_code:
            parts.append(f"后天（{time_list[2]}）：{day3_desc}")

    return "；".join(parts) if parts else ""


def format_location_source_note(location: dict[str, Any]) -> str:
    source = str(location.get("source") or "")
    confidence = str(location.get("confidence") or "medium")
    cached = bool(location.get("cached"))
    stale = bool(location.get("cache_stale"))
    if source == "ip":
        return "位置来自 IP 自动定位，置信度较低，可能受 VPN/代理/运营商出口影响；建议填写“区县,城市,省份”或手动坐标。"
    if source == "manual-coordinates":
        return "位置来自手动坐标，精度取决于你输入的坐标。"
    if stale:
        return "天气位置来自旧缓存，因为实时地理解析失败；如位置不准，请重新设置城市或坐标。"
    if cached:
        return "天气位置来自本地地理编码缓存，避免重复联网解析。"
    if confidence == "high":
        return "位置由地理库解析，匹配到区县级信息。"
    return "位置由地理库解析，可能是城市级匹配；如需更准可填写“区县,城市,省份”或坐标。"


def detect_ip_location() -> dict[str, Any]:
    data = _get_json("http://ip-api.com/json/?lang=zh-CN")
    if data.get("status") != "success":
        raise RuntimeError("IP 自动定位失败，请手动填写区县、城市和省份。")
    city = data.get("city") or data.get("regionName") or "当前位置"
    region = data.get("regionName") or ""
    return {
        "district": "",
        "city": city,
        "region": region,
        "country": data.get("country") or "",
        "latitude": float(data["lat"]),
        "longitude": float(data["lon"]),
        "display_name": "，".join([part for part in [region, city, data.get("country")] if part]),
        "source": "ip",
        "confidence": "low",
    }


def format_components(components: dict[str, str | None], include_country: bool = False) -> str:
    parts = [components.get("province"), components.get("city"), components.get("district")]
    if include_country:
        parts.append("中国")
    return "，".join(str(part) for part in parts if part)


def _try_geocoder(func, components: dict[str, str | None]) -> dict[str, Any] | None:
    try:
        return func(components)
    except (OSError, RuntimeError, urllib.error.URLError, TimeoutError):
        return None


def _alerts_from_sample(
    weather_code: Any,
    temperature: Any,
    wind_speed: Any,
    *,
    source: str,
    time_label: str,
) -> list[dict[str, Any]]:
    code = _as_int(weather_code)
    temp = _as_float(temperature)
    wind = _as_float(wind_speed)
    alerts: list[dict[str, Any]] = []

    if code in (95, 96, 99):
        message = "当前有雷暴并伴有冰雹，请尽量避免外出！" if code in (96, 99) else "当前有雷暴天气，注意安全。"
        alerts.append({"type": "thunderstorm", "source": source, "code": code, "time_label": time_label, "message": message})
    elif code in (45, 48):
        desc = "雾凇" if code == 48 else "雾"
        alerts.append({"type": "fog", "source": source, "code": code, "time_label": time_label, "message": f"当前有{desc}，能见度较低，出行注意安全。"})
    elif code in (56, 57):
        severity = _weather_severity(code)
        alerts.append({"type": "freeze", "source": source, "code": code, "time_label": time_label, "message": f"当前有{severity}冻毛毛雨，路面可能结冰，注意防滑。"})
    elif code in (66, 67):
        severity = _weather_severity(code)
        alerts.append({"type": "freeze", "source": source, "code": code, "time_label": time_label, "message": f"当前有{severity}冻雨，路面可能结冰，注意防滑。"})
    elif code is not None and 61 <= code <= 65:
        severity = _weather_severity(code)
        alerts.append({"type": "rain", "source": source, "code": code, "time_label": time_label, "message": f"当前正在下{severity}雨，出门记得带伞。"})
    elif code is not None and 71 <= code <= 77:
        severity = _weather_severity(code)
        alerts.append({"type": "snow", "source": source, "code": code, "time_label": time_label, "message": f"当前正在下{severity}雪，注意保暖。"})
    elif code is not None and 80 <= code <= 82:
        severity = _weather_severity(code)
        alerts.append({"type": "rain", "source": source, "code": code, "time_label": time_label, "message": f"当前有{severity}阵雨，出门记得带伞。"})
    elif code is not None and 85 <= code <= 86:
        severity = _weather_severity(code)
        alerts.append({"type": "snow", "source": source, "code": code, "time_label": time_label, "message": f"当前有{severity}雪阵雨，注意保暖。"})

    if temp is not None and temp >= 35:
        alerts.append({"type": "heat", "source": source, "temperature": temp, "time_label": time_label, "message": f"当前 {temp:g}°C，注意防暑。"})
    if temp is not None and temp <= -5:
        alerts.append({"type": "cold", "source": source, "temperature": temp, "time_label": time_label, "message": f"当前 {temp:g}°C，注意保暖。"})
    if wind is not None and wind > 12.5:
        alerts.append({"type": "wind", "source": source, "wind_speed": wind, "time_label": time_label, "message": f"当前风力较大 ({wind:g}m/s)，注意安全。"})

    return alerts


def _future_alert_message(alert: dict[str, Any]) -> str:
    when = str(alert.get("time_label") or "稍后")
    alert_type = str(alert.get("type") or "")
    if alert_type == "thunderstorm":
        return f"{when}可能有雷暴，提前减少外出安排。"
    if alert_type == "fog":
        return f"{when}可能有雾，出行注意能见度。"
    if alert_type == "freeze":
        return f"{when}可能有冻雨或结冰风险，注意防滑。"
    if alert_type == "rain":
        return f"{when}可能有降雨，出门记得带伞。"
    if alert_type == "snow":
        return f"{when}可能有降雪，注意保暖和路面湿滑。"
    if alert_type == "heat":
        return f"{when}可能高温，注意补水和防晒。"
    if alert_type == "cold":
        return f"{when}可能低温，注意保暖。"
    if alert_type == "wind":
        return f"{when}可能有大风，注意收好窗边物品。"
    return str(alert.get("message") or "")


def _format_hour_label(raw_time: Any) -> str:
    text = str(raw_time or "")
    if "T" not in text:
        return "未来几小时"
    try:
        return datetime.fromisoformat(text).strftime("%H:%M")
    except ValueError:
        return text.split("T", 1)[-1]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_location_result(
    location: dict[str, Any],
    query: str,
    confidence: str | None = None,
) -> dict[str, Any]:
    result = dict(location)
    result["query"] = query
    if confidence is not None:
        result["confidence"] = confidence
    elif not result.get("confidence"):
        result["confidence"] = "high" if result.get("district") else "medium"
    result.setdefault("cached", False)
    result.setdefault("cache_stale", False)
    return result


def _location_cache_key(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _load_location_cache() -> dict[str, Any]:
    payload = read_json(LOCATION_CACHE_FILE, {})
    return payload if isinstance(payload, dict) else {}


def _cached_location(requested: str, allow_stale: bool) -> dict[str, Any] | None:
    cache = _load_location_cache()
    entry = cache.get(_location_cache_key(requested))
    if not isinstance(entry, dict):
        return None
    location = entry.get("location")
    if not isinstance(location, dict):
        return None
    cached_at = _parse_cache_time(str(entry.get("cached_at") or ""))
    stale = cached_at is None or datetime.now() - cached_at > timedelta(days=LOCATION_CACHE_TTL_DAYS)
    if stale and not allow_stale:
        return None
    result = dict(location)
    result["cached"] = True
    result["cache_stale"] = stale
    return result


def _save_cached_location(requested: str, location: dict[str, Any]) -> None:
    if location.get("source") in {"ip", "manual-coordinates"}:
        return
    cache = _load_location_cache()
    clean_location = dict(location)
    clean_location.pop("cached", None)
    clean_location.pop("cache_stale", None)
    cache[_location_cache_key(requested)] = {
        "cached_at": datetime.now().isoformat(timespec="seconds"),
        "location": clean_location,
    }
    write_json(LOCATION_CACHE_FILE, cache)


def _parse_cache_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _location_score(item: dict[str, Any], components: dict[str, str | None]) -> int:
    address = item.get("address") or {}
    haystack = " ".join(str(value) for value in [item.get("display_name", ""), *address.values()])
    score = 0
    for key, weight in [("province", 12), ("city", 8), ("district", 16)]:
        value = components.get(key)
        if value and value.replace("省", "").replace("市", "") in haystack:
            score += weight
    if address.get("country_code") == "cn":
        score += 2
    return score


def _open_meteo_score(item: dict[str, Any], components: dict[str, str | None]) -> int:
    score = 0
    province = components.get("province")
    city = components.get("city")
    if item.get("country_code") == "CN":
        score += 2
    if province and province.replace("省", "") in str(item.get("admin1", "")):
        score += 10
    if city and city in str(item.get("name", "")):
        score += 5
    return score


def _display_name(components: dict[str, str | None], address: dict[str, Any]) -> str:
    province = components.get("province") or address.get("state") or address.get("province")
    city = components.get("city") or _address_city(address)
    district = components.get("district") or _address_district(address)
    country = address.get("country") or "中国"
    return "，".join([part for part in [province, city, district, country] if part])


def _address_city(address: dict[str, Any]) -> str:
    return address.get("city") or address.get("town") or address.get("municipality") or address.get("county") or ""


def _address_district(address: dict[str, Any]) -> str:
    return address.get("suburb") or address.get("city_district") or address.get("district") or address.get("county") or ""


def _looks_like_district(text: str) -> bool:
    return text.endswith(("区", "县", "旗", "市辖区"))


def _split_known_city_district(city: str | None, district: str | None) -> tuple[str | None, str | None]:
    if not city or district:
        return city, district
    for known_city in sorted(CITY_PROVINCE_HINTS, key=len, reverse=True):
        if city.startswith(known_city) and len(city) > len(known_city):
            tail = city[len(known_city):]
            if _looks_like_district(tail):
                return known_city, tail
    return city, district


def _strip_city_suffix(text: str) -> str:
    return re.sub(r"(市)$", "", text.strip())


def _strip_area_suffix(text: str) -> str:
    return text.strip()
