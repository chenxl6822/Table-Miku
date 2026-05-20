from __future__ import annotations

import json
import re
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


def _get_json(url: str, timeout: float = 8.0) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Table-Miku/0.4 (desktop pet weather lookup)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_weather(location_text: str = "湘潭,湖南") -> str:
    requested = (location_text or "湘潭,湖南").strip()
    location = resolve_location(requested)
    weather = fetch_open_meteo(location["latitude"], location["longitude"])
    current = weather.get("current") or {}

    temperature = current.get("temperature_2m")
    wind = current.get("wind_speed_10m")
    humidity = current.get("relative_humidity_2m")
    apparent = current.get("apparent_temperature")
    code = current.get("weather_code")
    description = WMO_DESCRIPTIONS.get(code, "天气情况未知")

    place = location["display_name"]
    source_note = "IP 自动定位可能受 VPN/代理影响，建议在设置里填写“城市,省份”。" if location.get("source") == "ip" else "位置由地理库解析。"
    return (
        f"{place}：现在{description}，{temperature}°C，体感 {apparent}°C，"
        f"湿度 {humidity}%，风速 {wind} km/h。\n{source_note}"
    )


def resolve_location(location_text: str) -> dict[str, Any]:
    requested = location_text.strip()
    if requested.lower() in {"auto", "定位", "自动定位", "当前位置"}:
        return detect_ip_location()

    city, province = parse_china_location(requested)
    if city in CITY_PROVINCE_HINTS and not province:
        province = CITY_PROVINCE_HINTS[city]

    location = _try_geocoder(geocode_with_nominatim, city, province)
    if location is not None:
        return location

    location = _try_geocoder(geocode_with_open_meteo, city, province)
    if location is not None:
        return location

    hint = f"{city},{province}" if province else city
    raise RuntimeError(f"没有找到「{hint}」的地理位置，请尝试输入“城市,省份”，例如“湘潭,湖南”。")


def _try_geocoder(func, city: str, province: str | None) -> dict[str, Any] | None:
    try:
        return func(city, province)
    except OSError:
        return None


def parse_china_location(text: str) -> tuple[str, str | None]:
    cleaned = re.sub(r"\s+", "", text)
    parts = [part for part in re.split(r"[,，、/|]+", cleaned) if part]
    if len(parts) >= 2:
        city = _strip_city_suffix(parts[0])
        province = normalize_province(parts[1])
        if parts[0] in PROVINCE_ALIASES and parts[1] not in PROVINCE_ALIASES:
            city = _strip_city_suffix(parts[1])
            province = normalize_province(parts[0])
        return city, province

    for short, full in PROVINCE_ALIASES.items():
        if cleaned.startswith(short) and len(cleaned) > len(short):
            return _strip_city_suffix(cleaned[len(short):]), full
        if cleaned.endswith(short) and len(cleaned) > len(short):
            return _strip_city_suffix(cleaned[:-len(short)]), full

    return _strip_city_suffix(cleaned), None


def normalize_province(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.replace("省", "").replace("市", "").strip()
    return PROVINCE_ALIASES.get(cleaned, text)


def geocode_with_nominatim(city: str, province: str | None) -> dict[str, Any] | None:
    params = {
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 8,
        "countrycodes": "cn",
        "accept-language": "zh-CN",
        "city": city,
        "country": "中国",
    }
    if province:
        params["state"] = province

    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    results = _get_json(url)
    if not isinstance(results, list):
        return None

    ranked = sorted(results, key=lambda item: _location_score(item, city, province), reverse=True)
    for item in ranked:
        if _location_score(item, city, province) <= 0:
            continue
        address = item.get("address") or {}
        display = _display_name(city, province, address, item)
        return {
            "name": city,
            "region": province or address.get("state") or "",
            "country": "中国",
            "latitude": float(item["lat"]),
            "longitude": float(item["lon"]),
            "display_name": display,
            "source": "nominatim",
        }
    return None


def geocode_with_open_meteo(city: str, province: str | None) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"name": city, "count": 10, "language": "zh", "countryCode": "CN"})
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?{query}"
    geo = _get_json(geo_url)
    results = geo.get("results") or []
    if not results:
        return None

    def score(item: dict[str, Any]) -> int:
        value = 0
        if item.get("country_code") == "CN":
            value += 2
        if province and province.replace("省", "") in str(item.get("admin1", "")):
            value += 10
        if city in str(item.get("name", "")):
            value += 5
        return value

    best = max(results, key=score)
    if score(best) <= 0:
        return None
    return {
        "name": best.get("name", city),
        "region": best.get("admin1", province or ""),
        "country": best.get("country", "中国"),
        "latitude": float(best["latitude"]),
        "longitude": float(best["longitude"]),
        "display_name": "，".join([part for part in [best.get("name", city), best.get("admin1", province), "中国"] if part]),
        "source": "open-meteo-geocoding",
    }


def fetch_open_meteo(latitude: float, longitude: float) -> dict[str, Any]:
    weather_query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "timezone": "auto",
        }
    )
    return _get_json(f"https://api.open-meteo.com/v1/forecast?{weather_query}")


def detect_ip_location() -> dict[str, Any]:
    data = _get_json("http://ip-api.com/json/?lang=zh-CN")
    if data.get("status") != "success":
        raise RuntimeError("IP 自动定位失败，请手动填写城市和省份。")
    city = data.get("city") or data.get("regionName") or "当前位置"
    region = data.get("regionName") or ""
    return {
        "name": city,
        "region": region,
        "country": data.get("country") or "",
        "latitude": float(data["lat"]),
        "longitude": float(data["lon"]),
        "display_name": "，".join([part for part in [city, region, data.get("country")] if part]),
        "source": "ip",
    }


def _location_score(item: dict[str, Any], city: str, province: str | None) -> int:
    address = item.get("address") or {}
    haystack = " ".join(str(value) for value in [item.get("display_name", ""), *address.values()])
    score = 0
    if city and city in haystack:
        score += 6
    if province and province.replace("省", "") in haystack:
        score += 12
    if address.get("country_code") == "cn":
        score += 2
    if item.get("class") == "boundary":
        score += 1
    return score


def _display_name(city: str, province: str | None, address: dict[str, Any], item: dict[str, Any]) -> str:
    state = province or address.get("state") or address.get("province")
    county = address.get("city") or address.get("town") or address.get("county") or city
    country = address.get("country") or "中国"
    return "，".join([part for part in [county, state, country] if part])


def _strip_city_suffix(text: str) -> str:
    return re.sub(r"(市|县|区)$", "", text.strip())
