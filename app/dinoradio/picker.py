"""Chooses what plays next: mostly top 10 hits, then other chart hits, then album tracks."""
import datetime
import logging
import random
import re
import time

from . import names
from .db import connect

log = logging.getLogger("picker")

GROUPS = ("top10", "chart", "other")
_CHRISTMAS = re.compile(r"christmas|xmas|santa|jingle bell|mistletoe|sleigh ride|rudolph", re.I)


class Picker:
    def __init__(self, settings, rng=random):
        self.s = settings
        self.rng = rng

    def _christmas_allowed(self) -> bool:
        mode = (self.s.christmas or "december").lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        return datetime.date.today().month == 12

    def _choose_group(self) -> str:
        weights = [max(0, self.s.top10_percent), max(0, self.s.chart_percent), max(0, self.s.other_percent)]
        if sum(weights) == 0:
            weights = [1, 1, 1]
        return self.rng.choices(GROUPS, weights=weights)[0]

    def _tracks(self):
        rows = connect().execute(
            """SELECT t.id, t.path, t.artist, t.title, t.album, t.genre, t.duration, t.chart_key, t.gain,
                      c.country, c.position, c.year
               FROM tracks t LEFT JOIN charts c ON c.key = t.chart_key""").fetchall()
        christmas_ok = self._christmas_allowed()
        excluded = set(self.s.exclude_genres)
        out = []
        for r in rows:
            d = r["duration"] or 0
            if d and (d < self.s.min_seconds or d > self.s.max_seconds):
                continue
            genre = (r["genre"] or "").lower()
            if excluded and any(g in genre for g in excluded):
                continue
            if not christmas_ok and (_CHRISTMAS.search(r["genre"] or "") or _CHRISTMAS.search(r["title"] or "")
                                     or _CHRISTMAS.search(r["album"] or "")):
                continue
            out.append(r)
        return out

    @staticmethod
    def group_of(row) -> str:
        p = row["position"]
        if p is not None and p <= 10:
            return "top10"
        if p is not None and p <= 100:
            return "chart"
        return "other"

    def pick(self, exclude_ids=()):
        """The next song (a database row), or None if the library is empty."""
        tracks = [t for t in self._tracks() if t["id"] not in exclude_ids]
        if not tracks:
            return None
        con = connect()
        since = time.time() - self.s.song_repeat_hours * 3600
        recent_songs = {r[0] for r in con.execute("SELECT chart_key FROM plays WHERE played > ?", (since,))}
        recent_artists = [r[0] for r in con.execute(
            "SELECT artist_key FROM plays ORDER BY id DESC LIMIT ?", (max(0, self.s.artist_gap),))]
        recent_artists = set(recent_artists)

        by_group = {g: [] for g in GROUPS}
        for t in tracks:
            by_group[self.group_of(t)].append(t)

        first = self._choose_group()
        order = [first] + [g for g in GROUPS if g != first]
        # Strictest rules first; relax them only if nothing at all fits
        for rules in ("both", "song", "none"):
            for g in order:
                cands = by_group[g]
                if rules in ("both", "song"):
                    cands = [t for t in cands if t["chart_key"] not in recent_songs]
                if rules == "both":
                    cands = [t for t in cands if names.artist_key(t["artist"]) not in recent_artists]
                if cands:
                    choice = self.rng.choice(cands)
                    if g != first or rules != "both":
                        log.debug("wanted %s, took %s (rules: %s)", first, g, rules)
                    return choice
        return None

    @staticmethod
    def played(row):
        connect().execute("INSERT INTO plays(track_id, artist_key, chart_key, played) VALUES (?,?,?,?)",
                          (row["id"], names.artist_key(row["artist"]), row["chart_key"], time.time()))
        connect().execute("DELETE FROM plays WHERE played < ?", (time.time() - 60 * 86400,))
