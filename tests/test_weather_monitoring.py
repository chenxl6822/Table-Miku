import urllib.parse
from datetime import datetime, timedelta

from table_miku import weather, weather_monitor
from table_miku.weather_monitor import WeatherMonitor


def test_parse_coordinate_location_accepts_plain_pair():
    location = weather.parse_coordinate_location("27.8563,112.9000")

    assert location is not None
    assert location["latitude"] == 27.8563
    assert location["longitude"] == 112.9
    assert location["source"] == "manual-coordinates"
    assert location["confidence"] == "high"


def test_parse_coordinate_location_accepts_labeled_pair():
    location = weather.parse_coordinate_location("lat=27.8563; lon=112.9000")

    assert location is not None
    assert location["latitude"] == 27.8563
    assert location["longitude"] == 112.9


def test_resolve_location_uses_cache(monkeypatch):
    cached_location = {
        "display_name": "湖南省，湘潭，雨湖区，中国",
        "latitude": 27.8563,
        "longitude": 112.9,
        "source": "nominatim",
        "confidence": "high",
    }
    monkeypatch.setattr(
        weather,
        "read_json",
        lambda filename, default: {
            weather._location_cache_key("雨湖区,湘潭,湖南"): {
                "cached_at": (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
                "location": cached_location,
            }
        },
    )

    location = weather.resolve_location("雨湖区,湘潭,湖南")

    assert location["cached"] is True
    assert location["cache_stale"] is False
    assert location["latitude"] == 27.8563


def test_resolve_location_marks_ip_as_low_confidence(monkeypatch):
    monkeypatch.setattr(
        weather,
        "detect_ip_location",
        lambda: {
            "display_name": "湖南省，湘潭，中国",
            "latitude": 27.8,
            "longitude": 112.9,
            "source": "ip",
        },
    )

    location = weather.resolve_location("auto")

    assert location["source"] == "ip"
    assert location["confidence"] == "low"


def test_fetch_open_meteo_requests_hourly_and_ms_wind(monkeypatch):
    captured = {}

    def fake_get_json(url, timeout=8.0):
        captured["url"] = url
        return {"current": {}}

    monkeypatch.setattr(weather, "_get_json", fake_get_json)

    weather.fetch_open_meteo(27.8, 112.9, include_daily=True, include_hourly=True)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
    assert query["wind_speed_unit"] == ["ms"]
    assert "weather_code" in query["hourly"][0]
    assert "weather_code" in query["daily"][0]


def test_detect_ip_location_uses_https(monkeypatch):
    captured = {}

    def fake_get_json(url, timeout=8.0):
        captured["url"] = url
        return {
            "success": True,
            "city": "湘潭",
            "region": "湖南",
            "country": "中国",
            "latitude": 27.8,
            "longitude": 112.9,
        }

    monkeypatch.setattr(weather, "_get_json", fake_get_json)

    location = weather.detect_ip_location()

    assert captured["url"].startswith("https://")
    assert location["latitude"] == 27.8


def test_evaluate_weather_alerts_includes_hourly_forecast():
    data = {
        "current": {"weather_code": 0, "temperature_2m": 24, "wind_speed_10m": 2},
        "hourly": {
            "time": ["2026-06-28T10:00", "2026-06-28T11:00"],
            "weather_code": [0, 95],
            "temperature_2m": [24, 25],
            "wind_speed_10m": [2, 3],
        },
    }

    alerts = weather.evaluate_weather_alerts(data, lead_hours=2)

    assert any(alert["type"] == "thunderstorm" and alert["source"] == "hourly" for alert in alerts)
    assert any("可能有雷暴" in alert["message"] for alert in alerts)


def test_evaluate_weather_alerts_skips_past_hourly_samples():
    times = [f"2026-07-31T{hour:02d}:00" for hour in range(24)]
    codes = [0] * 24
    codes[2] = 95
    codes[18] = 65
    data = {
        "current": {
            "time": "2026-07-31T17:15",
            "weather_code": 0,
            "temperature_2m": 24,
            "wind_speed_10m": 2,
        },
        "hourly": {
            "time": times,
            "weather_code": codes,
            "temperature_2m": [24] * 24,
            "wind_speed_10m": [2] * 24,
        },
    }

    alerts = weather.evaluate_weather_alerts(data, lead_hours=3)

    assert not any(alert["type"] == "thunderstorm" for alert in alerts)
    assert any(alert["type"] == "rain" and alert["time"] == times[18] for alert in alerts)


def test_weather_monitor_cools_down_duplicate_alerts():
    monitor = WeatherMonitor()
    messages: list[str] = []
    monitor.notice.connect(lambda _expression, message: messages.append(message))
    data = {"current": {"weather_code": 95, "temperature_2m": 24, "wind_speed_10m": 2}}

    monitor._evaluate(data)
    monitor._evaluate(data)

    assert len(messages) == 1


def test_weather_monitor_dispatches_network_work_to_background(monkeypatch):
    monitor = WeatherMonitor()
    started: dict[str, object] = {}
    monkeypatch.setattr(
        weather_monitor,
        "load_settings",
        lambda: {"city": "北京", "weather_alerts": {"enabled": True}},
    )

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            started.update(target=target, args=args, daemon=daemon)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(weather_monitor.threading, "Thread", FakeThread)

    monitor.check_now()

    assert started["target"] == monitor._fetch_worker
    assert started["args"] == ("北京", 1)
    assert started["daemon"] is True
    assert started["started"] is True
    monitor._running = False
