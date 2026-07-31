from __future__ import annotations

from table_miku import storage


def test_load_settings_repairs_invalid_numeric_values(monkeypatch):
    monkeypatch.setattr(
        storage,
        "read_json",
        lambda _filename, _default: {
            "reminder_interval_minutes": "invalid",
            "bubble_seconds": -5,
            "assistant": {
                "command_max_output_chars": "huge",
                "command_timeout_seconds": 0,
            },
            "weather_alerts": {
                "interval_minutes": "bad",
                "lead_minutes": -10,
            },
            "pomodoro": "broken",
        },
    )

    settings = storage.load_settings()

    assert settings["reminder_interval_minutes"] == 60
    assert settings["bubble_seconds"] == 1
    assert settings["assistant"]["command_max_output_chars"] == 420
    assert settings["assistant"]["command_timeout_seconds"] == 5
    assert settings["weather_alerts"]["interval_minutes"] == 20
    assert settings["weather_alerts"]["lead_minutes"] == 0
    assert settings["pomodoro"]["work_minutes"] == 25
