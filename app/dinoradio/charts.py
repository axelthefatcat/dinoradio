"""Chart positions from Wikipedia.

For each song it finds the song's own Wikipedia page (checking the page's song box names this artist,
so a cover version or a different song with the same title isn't used), reads the chart table, and
keeps the better of its peak in the UK Singles Chart and the US Billboard Hot 100. The release year
is taken from the song box too, for the DJ. Each answer is remembered; songs that weren't found are
tried again after 90 days."""
import json
import logging
import re
import time
import urllib.parse
import urllib.error
import urllib.request

from . import names
from .db import connect

log = logging.getLogger("charts")
RETRY_NOT_FOUND = 90 * 86400


class Unreachable(Exception):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def api(params: dict, contact: str) -> dict:
    query = {"format": "json", "formatversion": "2", **params}
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(query)
    # Wikipedia asks every app to say who it is, with a way to get in touch - without that it limits us hard
    agent = f"DinosRadio/1.0 (+https://github.com/axelthefatcat/dinoradio{'; ' + contact if contact else ''}) python-urllib"
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        retry = e.headers.get("Retry-After") if e.headers else None
        raise Unreachable(f"HTTP {e.code}", int(retry) if retry and retry.isdigit() else None)
    except (OSError, ValueError) as e:
        raise Unreachable(str(e))


def find(title: str, artist: str, contact: str = "", pause=lambda: time.sleep(2)):
    """(country, position, year) - country None if the song has no chart peak.
    Raises Unreachable if Wikipedia couldn't be reached."""
    base = names.base_title(title)
    who = names.primary_artist(artist)
    wanted_title, wanted_artist = names.normal(base), names.normal(who)
    if not wanted_title or not wanted_artist:
        return None, None, None
    search = api({"action": "query", "list": "search", "srlimit": "6",
                  "srsearch": f'intitle:"{base}" {who} song'}, contact)
    titles = [r.get("title", "") for r in search.get("query", {}).get("search", [])]
    titles = [t for t in titles if names.normal(names.base_title(t)) == wanted_title]
    for page in titles[:3]:
        pause()
        parsed = api({"action": "parse", "prop": "wikitext", "redirects": "1", "page": page}, contact)
        text = parsed.get("parse", {}).get("wikitext")
        if not isinstance(text, str) or not page_is_by(wanted_artist, text):
            continue
        country, position = peak(text)
        return country, position, release_year(text)
    return None, None, None


def _infobox(text: str):
    m = re.search(r"\{\{\s*Infobox (song|single)", text, re.I)
    return text[m.end():m.end() + 4000] if m else None


def _clean(value: str) -> str:
    value = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", value)
    return re.sub(r"\{\{[^}]*\}\}", " ", value)


def page_is_by(wanted_artist: str, text: str) -> bool:
    """Whether the page's song box names this artist."""
    box = _infobox(text)
    if box is None:
        return False
    m = re.search(r"\|\s*artist\s*=\s*([^\n]+)", box, re.I)
    if not m:
        return False
    named = names.normal(_clean(m.group(1)))
    return bool(named) and (wanted_artist in named or named in wanted_artist)


def release_year(text: str):
    box = _infobox(text)
    if box is None:
        return None
    m = re.search(r"\|\s*released\s*=\s*([^\n]+)", box, re.I)
    if not m:
        return None
    y = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", m.group(1))
    return int(y.group(1)) if y else None


def _section(text: str) -> str:
    """Only the original's chart table, not a later cover version's."""
    m = re.search(r"(?mi)^==+\s*(Weekly charts|Charts|Chart performance|Chart positions|Chart history)\s*==+", text)
    if not m:
        return text
    after = text[m.end():]
    e = re.search(r"(?m)^==[^=]", after)
    return after[:e.start()] if e else after


def peak(text: str):
    """The best UK Singles Chart / Billboard Hot 100 position in the page's chart table: (country, position)."""
    sec = _section(text)
    best = {"UK": None, "US": None}

    def note(country, position):
        if 1 <= position <= 200:
            best[country] = position if best[country] is None else min(best[country], position)

    # {{single chart|UK|3|...}} and {{single chart|Billboard100|1|...}}
    for m in re.finditer(r"\{\{\s*single ?chart\s*\|\s*([^|}]+?)\s*\|\s*(\d{1,3})", sec, re.I):
        ident = m.group(1).lower().replace(" ", "")
        if ident in ("uk", "uksingles"):
            note("UK", int(m.group(2)))
        if ident in ("billboard100", "billboardhot100"):
            note("US", int(m.group(2)))

    # Older pages: plain table rows - "UK Singles (OCC)" / "US Billboard Hot 100" then "| 3"
    lines = sec.split("\n")
    for i, line in enumerate(lines):
        lower = line.lower()
        if "uk singles" in lower or "official singles chart" in lower:
            country = "UK"
        elif "billboard hot 100" in lower or "billboard]] hot 100" in lower:
            country = "US"
        else:
            continue
        if "single chart" in lower:
            continue
        candidates = []
        if "||" in line:
            candidates.append(line.split("||", 1)[1])
        for j in range(i + 1, min(len(lines), i + 3)):
            nxt = lines[j].strip()
            if nxt.startswith("|-") or nxt.startswith("!"):
                break
            candidates.append(nxt)
        for c in candidates:
            c = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>|\{\{[^}]*\}\}", "", c).replace("|", " ")
            c = re.sub(r'align\s*=\s*"?\w+"?|style\s*=\s*"[^"]*"', "", c).strip()
            if c and c[0].isdigit():
                note(country, int(re.match(r"\d+", c).group()))
                break

    uk, us = best["UK"], best["US"]
    if uk is not None and us is not None:
        return ("US", us) if us < uk else ("UK", uk)
    if uk is not None:
        return "UK", uk
    if us is not None:
        return "US", us
    return None, None


class Charts:
    """Works through the library in the background, about one Wikipedia request a second."""

    def __init__(self, settings):
        self.s = settings
        self.message = "Waiting"
        self.offline_until = 0.0
        self.backoff = 0

    def next_unchecked(self):
        """A track whose song hasn't been looked up (or was not found more than 90 days ago)."""
        return connect().execute(
            """SELECT t.chart_key, t.artist, t.title FROM tracks t LEFT JOIN charts c ON c.key = t.chart_key
               WHERE t.chart_key NOT LIKE '|%' AND t.chart_key NOT LIKE '%|'
                 AND (c.key IS NULL OR (c.country IS NULL AND c.checked < ?))
               ORDER BY t.added DESC, random() LIMIT 1""", (time.time() - RETRY_NOT_FOUND,)).fetchone()

    def look_up_one(self) -> bool:
        """Looks up one song. False when there's nothing to do (or Wikipedia is unreachable)."""
        if not self.s.charts_enabled or time.time() < self.offline_until:
            return False
        row = self.next_unchecked()
        if row is None:
            self.message = "All songs looked up"
            return False
        try:
            country, position, year = find(row["title"], row["artist"], self.s.contact_email)
        except Unreachable as e:
            # Wait longer each time it happens (10 min, 20, 40... up to 6 hours), or as long as Wikipedia asks
            self.backoff = min(self.backoff * 2 or 600, 6 * 3600)
            wait = max(self.backoff, e.retry_after or 0)
            self.offline_until = time.time() + wait
            self.message = f"Wikipedia said {e} - trying again in {wait // 60} minutes"
            log.warning(self.message)
            return False
        self.backoff = 0
        connect().execute("INSERT OR REPLACE INTO charts(key, country, position, year, checked) VALUES (?,?,?,?,?)",
                          (row["chart_key"], country, position, year, time.time()))
        if country:
            log.info("chart: %s - %s: %s %s%s", row["artist"], row["title"], country, position,
                     f" ({year})" if year else "")
        self.message = "Looking up"
        return True

    def counts(self) -> dict:
        con = connect()
        songs = con.execute("SELECT COUNT(DISTINCT chart_key) FROM tracks").fetchone()[0]
        checked = con.execute("SELECT COUNT(*) FROM charts WHERE key IN (SELECT chart_key FROM tracks)").fetchone()[0]
        top10 = con.execute("SELECT COUNT(*) FROM charts WHERE position <= 10").fetchone()[0]
        hits = con.execute("SELECT COUNT(*) FROM charts WHERE position <= 100").fetchone()[0]
        return dict(songs=songs, checked=checked, top10=top10, hits=hits)
