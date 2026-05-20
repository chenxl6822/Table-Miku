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
        headers={"User-Agent": "Table-Miku/0.5 (desktop pet weather lookup)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_weather(location_text: str = "雨湖区,湘潭,湖南") -> str:
    requested = (location_text or "雨湖区,湘潭,湖南").strip()
    location = resolve_location(requested)
    weather = fetch_open_meteo(location["latitude"], location["longitude"])
    current = weather.get("current") or {}

    description = WMO_DESCRIPTIONS.get(current.get("weather_code"), "天气情况未知")
    source_note = "IP 自动定位可能受 VPN/代理影响，建议填写“区县,城市,省份”。" if location.get("source") == "ip" else "位置由真实地理库解析。"
    return (
        f"{location['display_name']}：现在{description}，{current.get('temperature_2m')}°C，"
        f"体感 {current.get('apparent_temperature')}°C，湿度 {current.get('relative_humidity_2m')}%，"
        f"风速 {current.get('wind_speed_10m')} km/h。\n{source_note}"
    )


def resolve_location(location_text: str) -> dict[str, Any]:
    requested = location_text.strip()
    if requested.lower() in {"auto", "定位", "自动定位", "当前位置"}:
        return detect_ip_location()

    components = parse_china_location(requested)
    if components["city"] in CITY_PROVINCE_HINTS and not components["province"]:
        components["province"] = CITY_PROVINCE_HINTS[components["city"]]

    location = _try_geocoder(geocode_with_nominatim, components)
    if location is not None:
        return location

    location = _try_geocoder(geocode_with_open_meteo, components)
    if location is not None:
        return location

    hint = format_components(components)
    raise RuntimeError(f"没有找到「{hint}」的地理位置，请尝试输入“区县,城市,省份”，例如“雨湖区,湘潭,湖南”。")


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
    }


def format_components(components: dict[str, str | None], include_country: bool = False) -> str:
    parts = [components.get("province"), components.get("city"), components.get("district")]
    if include_country:
        parts.append("中国")
    return "，".join(str(part) for part in parts if part)


def _try_geocoder(func, components: dict[str, str | None]) -> dict[str, Any] | None:
    try:
        return func(components)
    except OSError:
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
