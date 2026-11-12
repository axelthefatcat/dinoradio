"""The DJ: decides when to talk, writes what to say, and turns it into speech with Piper."""
import glob
import hashlib
import json
import logging
import os
import queue
import random
import re
import subprocess
import threading
import time

from . import names
from .config import APP_DIR, DATA_DIR

log = logging.getLogger("dj")

VOICE_DIR = os.path.join(DATA_DIR, "voice")
PIPER = os.path.join(APP_DIR, "piper", "piper")

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + number_words(rest))
    return str(n)


def year_words(y: int) -> str:
    """1984 -> nineteen eighty-four, 2005 -> two thousand and five, 2016 -> twenty sixteen."""
    if 2000 <= y <= 2009:
        return "two thousand" + ("" if y == 2000 else " and " + _ONES[y - 2000])
    hi, lo = divmod(y, 100)
    if lo == 0:
        return number_words(hi) + " hundred"
    return number_words(hi) + " " + ("oh-" + _ONES[lo] if lo < 10 else number_words(lo))


SPOKEN_FIXES = {"AC/DC": "A C D C", "UB40": "U B forty", "UFO": "U F O", "ELO": "E L O", "INXS": "in excess",
                "T. Rex": "T Rex", "R.E.M.": "R E M", "ABC": "A B C", "KC and the Sunshine Band": "K C and the Sunshine Band"}


def spoken(text: str) -> str:
    t = text or ""
    for a, b in SPOKEN_FIXES.items():
        if t.strip().lower() == a.lower():
            return b
    t = t.replace("&", " and ").replace("/", " ").replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def country_name(c: str) -> str:
    return "the UK" if c == "UK" else "America"


def a_country(c: str) -> str:
    """'a UK' / 'an American' - for 'a UK number one'."""
    return "a UK" if c == "UK" else "an American"


def chart_name(c: str) -> str:
    return "the UK singles chart" if c == "UK" else "the Billboard Hot 100"


FALLBACK_VOICE = "en_GB-cori-medium"


class Piper:
    """One Piper process kept running, so the voice is loaded once rather than for every line
    (loading a voice is most of the work on a small Pi)."""

    def __init__(self):
        self.proc = None
        self.model = None
        self.lock = threading.Lock()
        self.lines = queue.Queue()
        self.last_error = ""

    def _start(self, model):
        self.stop()
        env = dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(PIPER))
        self.proc = subprocess.Popen(
            ["nice", "-n", "10", PIPER, "--model", model, "--json-input", "--sentence_silence", "0.3", "--quiet"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.model = model
        self.lines = queue.Queue()
        out, q = self.proc.stdout, self.lines

        def read():
            for line in iter(out.readline, b""):
                q.put(line.decode("utf-8", "replace").strip())
            q.put(None)
        threading.Thread(target=read, daemon=True, name="piper-out").start()

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def say(self, model, text, out, timeout=None):
        with self.lock:
            first = self.proc is None or self.proc.poll() is not None or self.model != model
            if first:
                self._start(model)
            # the first line also loads the voice, so it gets longer
            timeout = timeout or (420 if first else 300)
            tmp = out + ".part.wav"
            try:
                self.proc.stdin.write((json.dumps({"text": text, "output_file": tmp}) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                deadline = time.time() + timeout
                while True:
                    line = self.lines.get(timeout=max(0.1, deadline - time.time()))
                    if line is None:
                        raise OSError("Piper stopped")
                    if line.endswith(".part.wav"):
                        break
            except queue.Empty:
                self.last_error = f"took longer than {timeout} seconds"
                self.stop()
                return False
            except OSError as e:
                self.last_error = str(e)
                self.stop()
                return False
            if not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
                self.last_error = "no audio came back"
                return False
            os.replace(tmp, out)
            return True


STANDBY_LINES = [
    "Sorry about this. {station} is having a little trouble reaching the record library at the moment. "
    "Please hold, and we'll be back as soon as we can.",
    "Apologies, the station is offline at the moment. Bear with us, normal service will resume shortly. "
    "Here's a little something while you wait.",
    "Still here! We're working on getting the record library back. Thanks for holding.",
]


BACK_LINES = [
    "Good news! We're back on air, and the record library is open again.",
    "And we're back! Thanks for bearing with us. This is {station}.",
]


def _pick(options, key: str):
    """The same choice every time for the same key, so the line (and its voice clip) can be reused."""
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16)
    return options[h % len(options)]


class DJ:
    def __init__(self, settings, rng=random):
        self.s = settings
        self.rng = rng
        self.songs_since_talk = 99         # talk early on
        self.talked_in_a_row = 0
        self.model = os.path.join(APP_DIR, "voices", f"{settings.voice}.onnx")
        self.fallback = os.path.join(APP_DIR, "voices", f"{FALLBACK_VOICE}.onnx")
        if not os.path.exists(self.model) and os.path.exists(self.fallback):
            self.model = self.fallback
        os.makedirs(VOICE_DIR, exist_ok=True)

    @property
    def available(self) -> bool:
        return self.s.dj_enabled and os.path.exists(PIPER) and os.path.exists(self.model)

    @property
    def voice_name(self) -> str:
        return os.path.basename(self.model)[:-len(".onnx")]

    # ------------------------------------------------------------------ deciding

    def should_talk(self, group: str, forced: bool = False) -> bool:
        if forced:
            return True
        if self.talked_in_a_row >= max(1, self.s.max_in_a_row):
            return False
        if self.songs_since_talk + 1 >= max(1, self.s.at_least_every):
            return True
        chance = {"top10": self.s.top10_chance, "chart": self.s.chart_chance}.get(group, self.s.other_chance)
        return self.rng.random() * 100 < chance

    def note(self, talked: bool):
        if talked:
            self.songs_since_talk = 0
            self.talked_in_a_row += 1
        else:
            self.songs_since_talk += 1
            self.talked_in_a_row = 0

    # ------------------------------------------------------------------ writing

    @staticmethod
    def _names(song):
        artist = spoken(re.sub(r"\s+(feat\.?|ft\.)\s+", " featuring ", song["artist"] or "", flags=re.I))
        title = spoken(names.base_title(song["title"]))
        return artist, title

    def intro(self, song) -> str:
        """The introduction for a song - always worded the same way for the same song."""
        artist, title = self._names(song)
        key = "intro|" + (song["chart_key"] or f"{artist}|{title}")
        pos, country, year = song["position"], song["country"], song["year"]
        yr = year_words(year) if year else None
        back_in = f" back in {yr}" if yr else ""
        from_yr = f" from {yr}" if yr else ""
        if pos is not None and pos == 1:
            return _pick([
                f"Here's {a_country(country)} number one{from_yr}. It's {artist}, with {title}.",
                f"A chart-topper now. {artist} went all the way to number one in {country_name(country)}{back_in}, with {title}.",
                f"Straight to the top of {chart_name(country)}{back_in}. This is {title}, by {artist}.",
            ], key)
        if pos is not None and pos <= 10:
            n = number_words(pos)
            return _pick([
                f"This one reached number {n} in {country_name(country)}{back_in}. It's {artist}, with {title}.",
                f"A top ten hit{from_yr}. Number {n} in {country_name(country)} for {artist}. This is {title}.",
                f"Here's {artist}, and {a_country(country)} number {n} hit. {title}.",
            ], key)
        if pos is not None and pos <= 100:
            n = number_words(pos)
            return _pick([
                f"This got to number {n} in {chart_name(country)}{back_in}. Here's {artist}, with {title}.",
                f"{artist} now. {title} made number {n} in {country_name(country)}.",
                f"A chart hit{from_yr} for {artist}. This is {title}.",
            ], key)
        return _pick([
            f"Next up, {artist}, with {title}.",
            f"Here's {artist}. This is {title}.",
            f"Now, {title}, from {artist}.",
            f"Something from {artist} now. {title}.",
        ], key)

    def back_announce(self, previous) -> str:
        """'That was...' for the song that just played - also always the same for the same song."""
        artist, title = self._names(previous)
        return _pick([
            f"That was {artist}, with {title}.",
            f"{artist} there, with {title}.",
            f"Before that, {title}, from {artist}.",
        ], "back|" + (previous["chart_key"] or f"{artist}|{title}"))

    def idents(self):
        st = self.s.name
        return [f"You're listening to {st}.", f"This is {st}.", f"More music on {st}."]

    def back_lines(self):
        return [line.format(station=self.s.name) for line in BACK_LINES]

    def standby_lines(self):
        return [line.format(station=self.s.name) for line in STANDBY_LINES]
