from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx

from papertrader.weather.http import WeatherHttp
from papertrader.weather.openmeteo import (
    _ENSEMBLE_MAX_RETRIES,
    fetch_openmeteo_ensemble_detail,
)


def _city():
    return SimpleNamespace(
        lat=41.87,
        lon=-87.62,
        tz="America/Chicago",
    )


def _ensemble_payload(event_date: date) -> dict:
    return {
        "daily": {
            "time": [event_date.isoformat()],
            "temperature_2m_max_member01_gfs_seamless": [80.0],
            "temperature_2m_max_member01_ecmwf_ifs025": [81.0],
        }
    }


def test_ensemble_cache_avoids_duplicate_requests(monkeypatch):
    http = WeatherHttp("test-agent")
    city = _city()
    event_date = date(2026, 8, 17)
    calls = {"n": 0}

    def fake_get(url, params=None):
        calls["n"] += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = _ensemble_payload(event_date)
        return resp

    monkeypatch.setattr(http.client, "get", fake_get)
    monkeypatch.setattr("papertrader.weather.openmeteo.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("papertrader.weather.openmeteo._throttle_ensemble", lambda _http: None)

    first = fetch_openmeteo_ensemble_detail(http, city, event_date, models="gfs_seamless,ecmwf_ifs025")
    second = fetch_openmeteo_ensemble_detail(http, city, event_date, models="gfs_seamless,ecmwf_ifs025")

    assert calls["n"] == 1
    assert len(first[0]) == 2
    assert first == second
    http.close()


def test_ensemble_retries_on_rate_limit(monkeypatch):
    http = WeatherHttp("test-agent")
    city = _city()
    event_date = date(2026, 8, 17)
    calls = {"n": 0}

    def fake_get(url, params=None):
        calls["n"] += 1
        resp = MagicMock()
        if calls["n"] == 1:
            resp.status_code = 429
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "rate limited",
                request=MagicMock(),
                response=resp,
            )
            return resp
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = _ensemble_payload(event_date)
        return resp

    monkeypatch.setattr(http.client, "get", fake_get)
    monkeypatch.setattr("papertrader.weather.openmeteo.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("papertrader.weather.openmeteo._throttle_ensemble", lambda _http: None)

    members, gfs, ecmwf, err = fetch_openmeteo_ensemble_detail(
        http, city, event_date, models="gfs_seamless,ecmwf_ifs025"
    )

    assert calls["n"] == 2
    assert err is None
    assert len(members) == 2
    assert gfs == 1 and ecmwf == 1
    http.close()


def test_ensemble_does_not_cache_api_failures(monkeypatch):
    http = WeatherHttp("test-agent")
    city = _city()
    event_date = date(2026, 8, 17)
    calls = {"n": 0}

    def fake_get(url, params=None):
        calls["n"] += 1
        resp = MagicMock()
        resp.status_code = 429
        return resp

    monkeypatch.setattr(http.client, "get", fake_get)
    monkeypatch.setattr("papertrader.weather.openmeteo.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("papertrader.weather.openmeteo._throttle_ensemble", lambda _http: None)

    first = fetch_openmeteo_ensemble_detail(http, city, event_date)
    second = fetch_openmeteo_ensemble_detail(http, city, event_date)

    assert first[3] == "rate_limited"
    assert second[3] == "rate_limited"
    assert calls["n"] == _ENSEMBLE_MAX_RETRIES * 2
    http.close()
