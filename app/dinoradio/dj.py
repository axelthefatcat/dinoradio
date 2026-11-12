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
            timeout = timeout or (300 if first else 180)
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
        self.piper = Piper()
        self.failures = 0
        self.message = "Ready"
        os.makedirs(VOICE_DIR, exist_ok=True)

    @property
    def available(self) -> bool:
        return self.s.dj_enabled and os.path.exists(PIPER) and os.path.exists(self.model)

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

    def script(self, song, previous=None, back_on_air=False) -> str:
        r = self.rng
        station = self.s.name
        artist = spoken(re.sub(r"\s+(feat\.?|ft\.)\s+", " featuring ", song["artist"] or "", flags=re.I))
        title = spoken(names.base_title(song["title"]))
        parts = []

        if back_on_air:
            parts.append(r.choice([
                f"Good news! We're back on air, and the record library is open again.",
                f"And we're back! Thanks for bearing with us. This is {station}.",
            ]))
        else:
            if previous is not None and r.random() * 100 < self.s.back_announce_chance:
                p_artist = spoken(previous["artist"])
                p_title = spoken(names.base_title(previous["title"]))
                parts.append(r.choice([
                    f"That was {p_artist}, with {p_title}.",
                    f"{p_artist} there, with {p_title}.",
                    f"Before that, {p_title}, from {p_artist}.",
                ]))
            if r.random() * 100 < self.s.station_ident_chance:
                parts.append(r.choice([f"You're listening to {station}.", f"This is {station}.",
                                       f"More music on {station}."]))

        pos, country, year = song["position"], song["country"], song["year"]
        yr = year_words(year) if year else None
        back_in = f" back in {yr}" if yr else ""
        from_yr = f" from {yr}" if yr else ""
        if pos is not None and pos == 1:
            parts.append(r.choice([
                f"Here's {a_country(country)} number one{from_yr}. It's {artist}, with {title}.",
                f"A chart-topper now. {artist} went all the way to number one in {country_name(country)}{back_in}, with {title}.",
                f"Straight to the top of {chart_name(country)}{back_in}. This is {title}, by {artist}.",
            ]))
        elif pos is not None and pos <= 10:
            n = number_words(pos)
            parts.append(r.choice([
                f"This one reached number {n} in {country_name(country)}{back_in}. It's {artist}, with {title}.",
                f"A top ten hit{from_yr}. Number {n} in {country_name(country)} for {artist}. This is {title}.",
                f"Here's {artist}, and {a_country(country)} number {n} hit. {title}.",
            ]))
        elif pos is not None and pos <= 100:
            n = number_words(pos)
            parts.append(r.choice([
                f"This got to number {n} in {chart_name(country)}{back_in}. Here's {artist}, with {title}.",
                f"{artist} now. {title} made number {n} in {country_name(country)}.",
                f"A chart hit{from_yr} for {artist}. This is {title}.",
            ]))
        else:
            parts.append(r.choice([
                f"Next up, {artist}, with {title}.",
                f"Here's {artist}. This is {title}.",
                f"Now, {title}, from {artist}.",
                f"Something from {artist} now. {title}.",
            ]))
        return " ".join(parts)

    # ------------------------------------------------------------------ speaking

    def speak(self, text: str, name: str = None):
        """A WAV file of the DJ saying this, or None if the voice isn't available."""
        if not self.available:
            return None
        voice = self.voice_name
        name = name or hashlib.sha1((voice + "|" + text).encode("utf-8")).hexdigest()[:16]
        out = os.path.join(VOICE_DIR, f"{name}.wav")
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            return out
        started = time.time()
        if self.piper.say(self.model, text, out):
            self.failures = 0
            self.message = f"Ready - {voice} (last line took {time.time() - started:.0f}s)"
            return out
        self.failures += 1
        self.message = f"Voice failed ({self.piper.last_error})"
        log.warning("DJ voice %s failed: %s", voice, self.piper.last_error)
        # A voice that keeps failing (usually too slow for this Pi): use the standard one instead
        if self.failures >= 2 and self.model != self.fallback and os.path.exists(self.fallback):
            log.warning("the %s voice is too slow or not working on this Pi - using %s instead", voice, FALLBACK_VOICE)
            self.model = self.fallback
            self.failures = 0
            self.message = f"Using {FALLBACK_VOICE} - {voice} was too slow on this Pi"
        return None

    @property
    def voice_name(self) -> str:
        return os.path.basename(self.model)[:-len(".onnx")]

    def standby_messages(self):
        """The 'please hold' messages - made once and kept, so they work without the NAS."""
        files = []
        for i, line in enumerate(STANDBY_LINES):
            text = line.format(station=self.s.name)
            tag = hashlib.sha1((self.voice_name + text).encode()).hexdigest()[:8]
            f = self.speak(text, name=f"standby-{i}-{tag}")
            if f:
                files.append(f)
        return files

    def tidy(self, keep_hours: int = 48):
        cutoff = time.time() - keep_hours * 3600
        for f in glob.glob(os.path.join(VOICE_DIR, "*.wav")):
            if os.path.basename(f).startswith("standby-"):
                continue
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except OSError:
                pass
