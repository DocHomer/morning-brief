"""Fetch the National Weather Service forecast for the phone's location and save it
to weather/latest.json for the morning episode.

Runs in GitHub Actions on a repository_dispatch event of type "location", sent by
an iPhone Shortcuts automation. Payload: {"lat": .., "lon": .., "city": .., "state": ..}.
Exact coordinates are NOT saved; only the place name and the forecast are.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "weather" / "latest.json"
UA = {"User-Agent": "morning-brief (github.com/DocHomer/morning-brief)", "Accept": "application/geo+json"}


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_coord(value) -> float | None:
    """Accept numbers or Shortcuts-formatted text like "38.25", "38,25", "38.25°", "38.25 N"."""
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\u2212", "-")
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    num = float(m.group(0).replace(",", "."))
    if re.search(r"\b[SW]\b", text.upper()) and num > 0:
        num = -num
    return round(num, 4)


def mask(obj):
    if isinstance(obj, dict):
        return {k: mask(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return re.sub(r"\d", "9", repr(obj))


def flatten(obj, out=None):
    """Collect key -> value pairs from any nesting (keys lowercased), plus loose strings."""
    out = out if out is not None else {"_strings": []}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                flatten(v, out)
            else:
                out.setdefault(str(k).strip().lower(), v)
                if isinstance(v, str):
                    out["_strings"].append(v)
    elif isinstance(obj, list):
        for v in obj:
            flatten(v, out)
    elif isinstance(obj, str):
        out["_strings"].append(obj)
    return out


def extract(payload):
    flat = flatten(payload)
    lat = parse_coord(flat.get("lat", flat.get("latitude")))
    lon = parse_coord(flat.get("lon", flat.get("lng", flat.get("longitude"))))
    if lat is None or lon is None:
        # Fallback: any text holding two coordinate-like numbers, e.g. "38.25, -85.75".
        for text in flat["_strings"]:
            nums = re.findall(r"-?\d{1,3}\.\d+", text)
            if len(nums) >= 2:
                lat, lon = round(float(nums[0]), 4), round(float(nums[1]), 4)
                break
    city = str(flat.get("city", "") or "").strip()
    state = str(flat.get("state", "") or "").strip()
    return lat, lon, city, state


def main() -> None:
    # Read the location from the event file rather than an env var, because
    # GitHub prints step env vars in the (public) run log.
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    payload = event.get("client_payload") or event.get("inputs") or {}
    lat, lon, city, state = extract(payload)
    if lat is None or lon is None:
        # Log only the layout of the payload, digits masked, so coordinates stay private.
        sys.exit(f"Could not read lat/lon. Payload layout: {mask(payload)}")
    record = {
        "received_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "city": city,
        "state": state,
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
