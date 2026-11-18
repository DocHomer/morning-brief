"""Fetch the National Weather Service forecast for the phone's location and save it
to weather/latest.json for the morning episode.

Runs in GitHub Actions on a repository_dispatch event of type "location", sent by
an iPhone Shortcuts automation. Payload: {"lat": .., "lon": .., "city": .., "state": ..}.
Exact coordinates are NOT saved; only the place name and the forecast are.
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "weather" / "latest.json"
UA = {"User-Agent": "morning-brief (github.com/DocHomer/morning-brief)", "Accept": "application/geo+json"}


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    payload = json.loads(os.environ.get("PAYLOAD") or "{}")
    try:
        lat = round(float(str(payload["lat"]).strip()), 4)
        lon = round(float(str(payload["lon"]).strip()), 4)
    except (KeyError, ValueError):
        sys.exit(f"Payload missing lat/lon: {sorted(payload)}")

    record = {
        "received_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "city": str(payload.get("city", "")).strip(),
        "state": str(payload.get("state", "")).strip(),
        "source": "National Weather Service",
        "periods": [],
    }
    try:
        point = get(f"https://api.weather.gov/points/{lat},{lon}")["properties"]
        rel = point.get("relativeLocation", {}).get("properties", {})
        record["nws_place"] = f"{rel.get('city', '')}, {rel.get('state', '')}".strip(", ")
        record["source_url"] = f"https://forecast.weather.gov/MapClick.php?lat={lat:.2f}&lon={lon:.2f}"
        fc = get(point["forecast"])["properties"]
        record["forecast_updated"] = fc.get("updateTime") or fc.get("generatedAt")
        record["periods"] = [
            {"name": p["name"], "temp": p["temperature"], "unit": p["temperatureUnit"],
             "wind": f"{p.get('windSpeed', '')} {p.get('windDirection', '')}".strip(),
             "precip_pct": (p.get("probabilityOfPrecipitation") or {}).get("value"),
             "detail": p["detailedForecast"]}
            for p in fc["periods"][:8]
        ]
        try:
            alerts = get(f"https://api.weather.gov/alerts/active?point={lat},{lon}")["features"]
            record["alerts"] = [a["properties"]["headline"] for a in alerts]
        except Exception as exc:  # noqa: BLE001
            record["alerts_error"] = repr(exc)
    except Exception as exc:  # noqa: BLE001  (e.g. outside the U.S.)
        record["error"] = f"NWS lookup failed: {exc!r}"

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Saved forecast for {record.get('nws_place') or record['city']}: {len(record['periods'])} periods")


if __name__ == "__main__":
    main()
