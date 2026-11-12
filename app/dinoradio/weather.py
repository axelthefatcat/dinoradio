"""The weather, read by the DJ about once an hour - from Open-Meteo (open-meteo.com, free, no account).

The town is set in the settings ([weather] location); its map position is looked up once and
remembered. Each hour a random minute is picked; the first song prepared after it gets the
weather as its introduction."""
import datetime
import json
import logging
import os
import random
import time
import urllib.parse
import urllib.request

from .config import DATA_DIR
from .dj import number_words

log = logging.getLogger("weather")
PLACE_FILE = os.path.join(DATA_DIR, "weather-place.json")

CONDITIONS = {
    0: ("clear skies", "sunshine"), 1: ("mostly clear skies", "mostly sunny"), 2: ("some cloud about", "sunny spells"),
    3: ("grey and overcast", "grey and overcast"), 45: ("foggy", "foggy"), 48: ("freezing fog", "freezing fog"),
    51: ("light drizzle", "light drizzle"), 53: ("drizzle", "drizzle"), 55: ("heavy drizzle", "heavy drizzle"),
    56: ("freezing drizzle", "freezing drizzle"), 57: ("freezing drizzle", "freezing drizzle"),
    61: ("light rain", "light rain"), 63: ("rain", "rain"), 65: ("heavy rain", "heavy rain"),
    66: ("freezing rain", "freezing rain"), 67: ("heavy freezing rain", "heavy freezing rain"),
    71: ("light snow", "light snow"), 73: ("snow", "snow"), 75: ("heavy snow", "heavy snow"), 77: ("snow grains", "snow grains"),
    80: ("a few showers", "a few showers"), 81: ("showers", "showers"), 82: ("heavy showers", "heavy showers"),
    85: ("snow showers", "snow showers"), 86: ("heavy snow showers", "heavy snow showers"),
    95: ("thunderstorms", "thunderstorms"), 96: ("thunderstorms with hail", "thunderstorms with hail"),
    99: ("thunderstorms with hail", "thunderstorms with hail"),
}
COMPASS = ["northerly", "north-easterly", "easterly", "south-easterly", "southerly", "south-westerly",
           "westerly", "north-westerly"]


def degrees(t: float) -> str:
    n = round(t)
    word = ("minus " + number_words(-n)) if n < 0 else number_words(n)
    return word + (" degree" if abs(n) == 1 else " degrees")


def wind_words(mph: float, direction: float) -> str:
    d = COMPASS[int(((direction % 360) + 22.5) // 45) % 8]
    if mph < 4:
        return "hardly a breath of wind"
    if mph < 13:
        return f"a light {d} breeze"
    if mph < 19:
        return f"a gentle {d} breeze of around {number_words(round(mph))} miles an hour"
    if mph < 25:
        return f"a fresh {d} wind of around {number_words(round(mph))} miles an hour"
    if mph < 39:
        return f"a strong {d} wind, gusting around {number_words(round(mph))} miles an hour"
    return f"gale-force {d} winds of {number_words(round(mph))} miles an hour, so hold on to your hats"


class Weather:
    def __init__(self, settings, rng=random):
        self.s = settings
        self.rng = rng
        self.next_due = None
        self.last_line = ""
        self.message = "Off" if not settings.weather_location else "Waiting"
        self._plan_next(first=True)

    @property
    def enabled(self) -> bool:
        return bool(self.s.weather_location)

    def _plan_next(self, first=False):
        """A random time in the next hour (or soon after starting)."""
        now = datetime.datetime.now()
        if first:
            self.next_due = now + datetime.timedelta(minutes=self.rng.randint(5, 25))
        else:
            hour = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
            self.next_due = hour + datetime.timedelta(minutes=self.rng.randint(0, 55))
        if self.enabled:
            self.message = f"{self.s.weather_location} - next at about {self.next_due:%H:%M}"

    def due(self) -> bool:
        if not self.enabled or self.next_due is None or datetime.datetime.now() < self.next_due:
            return False
        h = datetime.datetime.now().hour
        if not (self.s.weather_from_hour <= h <= self.s.weather_to_hour):
            self._plan_next()
            return False
        return True

    # ------------------------------------------------------------------ fetching

    @staticmethod
    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "DinosRadio/1.0 (+https://github.com/axelthefatcat/dinoradio)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def place(self):
        """(name, latitude, longitude) for the town in the settings, looked up once and remembered."""
        wanted = self.s.weather_location.strip()
        try:
            with open(PLACE_FILE) as f:
                saved = json.load(f)
            if saved.get("asked") == wanted:
                return saved["name"], saved["lat"], saved["lon"]
        except (OSError, ValueError, KeyError):
            pass
        # "Evesham" or "Evesham, GB" / "52.09,-1.94"
        parts = [p.strip() for p in wanted.split(",")]
        try:
            if len(parts) == 2 and all(p.replace(".", "", 1).replace("-", "", 1).isdigit() for p in parts):
                name, lat, lon = "here", float(parts[0]), float(parts[1])
            else:
                q = {"name": parts[0], "count": 1, "language": "en", "format": "json"}
                if len(parts) > 1 and len(parts[-1]) == 2:
                    q["countryCode"] = parts[-1].upper()
                res = self._get("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(q))
                hit = (res.get("results") or [None])[0]
                if not hit:
                    self.message = f"Couldn't find \"{wanted}\" - check [weather] location in the settings"
                    return None
                name, lat, lon = hit["name"], hit["latitude"], hit["longitude"]
            with open(PLACE_FILE, "w") as f:
                json.dump({"asked": wanted, "name": name, "lat": lat, "lon": lon}, f)
            log.info("weather location: %s (%.3f, %.3f)", name, lat, lon)
            return name, lat, lon
        except Exception as e:
            self.message = f"Couldn't look up the location ({e})"
            return None

    def forecast(self, lat, lon) -> dict:
        q = {"latitude": lat, "longitude": lon, "timezone": "auto", "wind_speed_unit": "mph", "forecast_days": 1,
             "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,is_day",
             "hourly": "precipitation_probability,weather_code",
             "daily": "temperature_2m_max,temperature_2m_min"}
        return self._get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))

    # ------------------------------------------------------------------ the DJ's words

    def script(self, data: dict, name: str) -> str:
        r = self.rng
        cur = data["current"]
        code = int(cur.get("weather_code", 3))
        day = bool(cur.get("is_day", 1))
        sky = CONDITIONS.get(code, ("cloudy", "cloudy"))[1 if day else 0]
        if sky == "sunshine" and not day:
            sky = "clear skies"
        temp = cur["temperature_2m"]
        feels = cur.get("apparent_temperature", temp)
        wind = wind_words(cur.get("wind_speed_10m", 0), cur.get("wind_direction_10m", 0))

        intro = r.choice([f"Time for the weather in {name}.", f"Here's the weather for {name}.",
                          f"A quick look at the weather in {name}."])
        now_line = r.choice([f"Right now it's {degrees(temp)} with {sky}, and {wind}.",
                             f"At the moment we've got {sky}, {degrees(temp)}, and {wind}.",
                             f"It's {degrees(temp)} out there with {sky}, and {wind}."])
        if abs(feels - temp) >= 3:
            now_line += f" It feels more like {degrees(feels)}."

        # The rest of today: the chance of rain in the hours still to come, and the high or low
        later = ""
        try:
            times = data["hourly"]["time"]
            probs = data["hourly"]["precipitation_probability"]
            now_iso = data["current"]["time"][:13]
            upcoming = [p for t, p in zip(times, probs) if t[:13] > now_iso and p is not None]
            chance = max(upcoming) if upcoming else None
            hi = data["daily"]["temperature_2m_max"][0]
            lo = data["daily"]["temperature_2m_min"][0]
            hour = int(now_iso[11:13])
            if chance is not None:
                if chance >= 70:
                    later = r.choice(["Rain looks likely later, so keep the umbrella handy.",
                                      "There's rain on the way later on."])
                elif chance >= 40:
                    later = f"There's a {number_words(round(chance / 10) * 10)} percent chance of showers later."
                elif upcoming:
                    later = r.choice(["It should stay dry for the rest of the day.", "Staying dry for now."])
            if hour < 15 and hi - temp >= 2:
                later += f" Climbing to a high of {degrees(hi)}."
            elif hour >= 18 and temp - lo >= 2:
                later += f" Dropping to {degrees(lo)} overnight."
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        return " ".join(x for x in (intro, now_line, later.strip()) if x)

    def take(self):
        """The weather line if it's due now (and plans the next one), else None."""
        if not self.due():
            return None
        self._plan_next()
        where = self.place()
        if where is None:
            return None
        name, lat, lon = where
        try:
            line = self.script(self.forecast(lat, lon), self.s.weather_name or name)
        except Exception as e:
            self.message = f"Weather unavailable ({e}) - next at about {self.next_due:%H:%M}"
            log.warning(self.message)
            return None
        self.last_line = line
        log.info("weather: %s", line)
        return line
