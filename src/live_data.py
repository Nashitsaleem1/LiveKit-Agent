"""Real-time public data for Ava: live weather and local time.

Weather comes from Open-Meteo (https://open-meteo.com), a free API that needs no key.
Only city names and coordinates are sent; no company data leaves the agent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from livekit.agents import utils
from livekit.agents.llm import ToolError

logger = logging.getLogger("live_data")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = aiohttp.ClientTimeout(total=6)

# Warehouse sites: coordinates are fixed, so no geocoding round trip is needed
WAREHOUSES = {
    "Dallas": (32.7767, -96.7970),
    "Houston": (29.7604, -95.3698),
    "Phoenix": (33.4484, -112.0740),
}
HQ_TIMEZONE = "America/Chicago"  # Austin headquarters

# WMO weather codes -> plain words
_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}  # fmt: skip
_ICE_SNOW = {56, 57, 66, 67, 71, 73, 75, 77, 85, 86}

_geo_cache: dict[str, dict[str, Any]] = {}


async def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    session = utils.http_context.http_session()
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise ToolError("The weather service returned an error.")
            return await resp.json()
    except ToolError:
        raise
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("live data request failed: %r", e)
        raise ToolError("I couldn't reach the weather service right now.") from e


async def _geocode(city: str) -> dict[str, Any]:
    key = city.strip().lower()
    if key not in _geo_cache:
        data = await _get_json(
            GEOCODE_URL, {"name": city.strip(), "count": 1, "language": "en"}
        )
        if not data.get("results"):
            raise ToolError(f"I couldn't find a place called {city}.")
        r = data["results"][0]
        _geo_cache[key] = {
            "name": ", ".join(
                p for p in (r.get("name"), r.get("admin1"), r.get("country")) if p
            ),
            "lat": r["latitude"],
            "lon": r["longitude"],
            "timezone": r.get("timezone") or "UTC",
        }
    return _geo_cache[key]


def _risks(cur: dict[str, Any], day: dict[str, Any]) -> list[str]:
    """Things that could slow down trucks today."""
    risks = []
    codes = {cur["weather_code"], day["weather_code"][0]}
    if any(c >= 95 for c in codes):
        risks.append("thunderstorms")
    if codes & _ICE_SNOW:
        risks.append("ice or snow on roads")
    if codes & {45, 48}:
        risks.append("fog and low visibility")
    if codes & {65, 81, 82}:
        risks.append("heavy rain")
    if (day.get("wind_gusts_10m_max") or [0])[0] >= 40:
        risks.append(
            f"strong wind gusts up to {round(day['wind_gusts_10m_max'][0])} miles per hour"
        )
    if (day.get("precipitation_probability_max") or [0])[
        0
    ] >= 70 and "heavy rain" not in risks:
        risks.append(
            f"{day['precipitation_probability_max'][0]} percent chance of rain"
        )
    return risks


async def _forecast(lat: float, lon: float) -> dict[str, Any]:
    data = await _get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,wind_gusts_10m_max",
            "timezone": "auto",
            "forecast_days": 1,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        },
    )
    cur, day = data["current"], data["daily"]
    return {
        "observed_at_local_time": cur["time"].replace("T", " "),
        "now": {
            "conditions": _CODES.get(cur["weather_code"], "unknown"),
            "temperature_f": round(cur["temperature_2m"]),
            "feels_like_f": round(cur["apparent_temperature"]),
            "wind_mph": round(cur["wind_speed_10m"]),
            "gusts_mph": round(cur["wind_gusts_10m"]),
        },
        "today": {
            "conditions": _CODES.get(day["weather_code"][0], "unknown"),
            "high_f": round(day["temperature_2m_max"][0]),
            "low_f": round(day["temperature_2m_min"][0]),
            "rain_chance_percent": (day.get("precipitation_probability_max") or [None])[
                0
            ],
        },
        "travel_risks": _risks(cur, day) or ["none"],
    }


async def get_weather(city: str) -> dict[str, Any]:
    place = await _geocode(city)
    return {"location": place["name"], **await _forecast(place["lat"], place["lon"])}


async def get_warehouse_weather() -> dict[str, Any]:
    names = list(WAREHOUSES)
    # All three sites are fetched in parallel, so this takes about as long as one lookup
    results = await asyncio.gather(
        *(_forecast(*WAREHOUSES[n]) for n in names), return_exceptions=True
    )
    sites = {}
    for name, res in zip(names, results, strict=True):
        if isinstance(res, Exception):
            sites[name] = {"error": "weather unavailable"}
        else:
            sites[name] = {
                "now": res["now"],
                "today": res["today"],
                "travel_risks": res["travel_risks"],
            }
    return {"warehouses": sites}


async def warehouse_weather_for_brief() -> dict[str, Any]:
    """Compact warehouse weather for the morning brief. Never raises: the brief must still
    work if the weather service is down."""
    try:
        data = await get_warehouse_weather()
    except Exception as e:
        logger.warning("brief weather unavailable: %r", e)
        return {"status": "unavailable"}
    summary = {}
    for site, w in data["warehouses"].items():
        if "error" in w:
            summary[site] = "unavailable"
            continue
        risks = [r for r in w["travel_risks"] if r != "none"]
        summary[site] = {
            "conditions": f"{w['now']['conditions']}, {w['now']['temperature_f']}F now, high {w['today']['high_f']}F",
            "travel_risks": risks or "none",
        }
    return summary


async def get_local_time(city: str | None = None) -> dict[str, Any]:
    if city:
        place = await _geocode(city)
        name, tz = place["name"], place["timezone"]
    else:
        name, tz = "Austin headquarters", HQ_TIMEZONE
    now = dt.datetime.now(ZoneInfo(tz))
    return {
        "location": name,
        "local_time": now.strftime("%I:%M %p").lstrip("0"),
        "date": now.strftime("%A, %B %d, %Y").replace(" 0", " "),
        "timezone": now.strftime("%Z"),
    }
