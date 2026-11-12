"""The DJ's voice clips: made in the background, ahead of time, and kept.

A slow, high-quality voice on a small Pi can take several minutes to say one sentence, so nothing
waits for it. The brain asks for the lines it will need (`want`), most urgent first; one Piper
process works through them in the background; the brain uses a clip only once it's ready (`ready`).
A line that isn't ready in time is simply not said.

Song introductions are worded the same way every time, so each clip is made once and kept for good
(on the USB drive when there is one). When there's nothing urgent, the worker gets ahead by voicing
the introductions for the biggest hits in the library.
"""
import hashlib
import logging
import os
import subprocess
import threading
import time

from . import config
from .dj import PIPER, Piper, VOICE_DIR

log = logging.getLogger("voices")

SD_CACHE = os.path.join(config.DATA_DIR, "voice-cache")

# kinds of clip
KEEP = "keep"          # kept for good: song intros, back-announces, idents, back-on-air
SHORT = "short"        # weather and "dinoradio say": deleted after a couple of days
STANDBY = "standby"    # the "please hold" messages: kept, and included in backups


class VoiceCache:
    def __init__(self, settings, model: str, stop: threading.Event):
        self.s = settings
        self.model = model
        self.voice = os.path.basename(model)[:-len(".onnx")]
        self.stop = stop
        self.piper = Piper()
        self.cv = threading.Condition()
        self.wanted = {}             # text -> [priority, kind]
        self.filler = None           # a function giving more lines to make in idle time
        self.doing = None
        self.made = 0
        self.failed = 0
        self.last_seconds = None
        self.message = "Starting"
        self.bad = {}                # text -> time it failed (not tried again for a while)
        os.makedirs(VOICE_DIR, exist_ok=True)

    # ------------------------------------------------------------------ where clips live

    def _usb_cache(self):
        b = self.s.backup_folder
        if b and b.startswith(config.USB_MOUNT + "/") and os.path.ismount(config.USB_MOUNT):
            return os.path.join(b, "voice-cache")
        return None

    def _key(self, text):
        return hashlib.sha1((self.voice + "|" + text).encode("utf-8")).hexdigest()[:20]

    def _places(self, text, kind):
        k = self._key(text)
        if kind == SHORT:
            return [os.path.join(VOICE_DIR, k + ".mp3")]
        if kind == STANDBY:
            return [os.path.join(VOICE_DIR, f"standby-{k}.mp3")]
        usb = self._usb_cache()
        sub = k[:2]                 # 256 sub-folders, so no folder gets huge
        return [os.path.join(d, sub, k + ".mp3") for d in (usb, SD_CACHE) if d]

    def ready(self, text, kind=KEEP):
        """The clip's file if it has been made, else None."""
        if not text:
            return None
        for p in self._places(text, kind):
            if os.path.exists(p):
                return p
        return None

    # ------------------------------------------------------------------ asking for clips

    def want(self, text, priority, kind=KEEP):
        """Ask for a line to be voiced. Lower priority numbers are made first."""
        if not text or self.ready(text, kind):
            return
        with self.cv:
            cur = self.wanted.get(text)
            if cur is None or priority < cur[0]:
                self.wanted[text] = [priority, kind]
                self.cv.notify()

    def waiting(self):
        with self.cv:
            return len(self.wanted)

    def wait_for(self, text, kind, timeout):
        """For the few lines worth waiting for (the standby messages, at start-up)."""
        end = time.time() + timeout
        while time.time() < end and not self.stop.is_set():
            p = self.ready(text, kind)
            if p:
                return p
            self.stop.wait(5)
        return None

    # ------------------------------------------------------------------ the worker

    def run(self):
        if not os.path.exists(PIPER) or not os.path.exists(self.model):
            self.message = "voice not installed"
            return
        while not self.stop.is_set():
            job = self._next_job()
            if job is None:
                self.message = self._idle_message()
                self.stop.wait(20)
                continue
            text, kind = job
            self._make(text, kind)

    def _next_job(self):
        with self.cv:
            for text in [t for t in self.wanted if self.ready(t, self.wanted[t][1])]:
                del self.wanted[text]
            if self.wanted:
                text = min(self.wanted, key=lambda t: self.wanted[t][0])
                kind = self.wanted.pop(text)[1]
                return text, kind
        if self.filler is not None and self.s.prerender:
            try:
                for text in self.filler():
                    if not self.ready(text) and time.time() - self.bad.get(text, 0) > 86400:
                        return text, KEEP
            except Exception:
                log.exception("choosing lines to voice ahead failed")
        return None

    def _make(self, text, kind):
        out = self._places(text, kind)[0]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        wav = out[:-4] + ".wav"
        self.doing = text
        self.message = f"voicing: \"{text[:60]}{'...' if len(text) > 60 else ''}\""
        started = time.time()
        # a slow voice can take about a minute per second of speech: allow plenty
        timeout = 300 + 6 * len(text)
        ok = self.piper.say(self.model, text, wav, timeout=timeout)
        if ok:
            r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", wav,
                                "-ac", "1", "-b:a", "64k", "-f", "mp3", out + ".part"], capture_output=True)
            ok = r.returncode == 0 and os.path.exists(out + ".part")
            if ok:
                os.replace(out + ".part", out)
        for f in (wav, out + ".part"):
            try:
                os.remove(f)
            except OSError:
                pass
        self.doing = None
        if ok:
            self.made += 1
            self.last_seconds = time.time() - started
        else:
            self.failed += 1
            self.bad[text] = time.time()
            log.warning("couldn't voice a line (%s): %s", self.piper.last_error or "ffmpeg failed", text[:80])
            self.stop.wait(10)

    def _idle_message(self):
        return "all caught up"

    def status(self) -> str:
        parts = [self.voice]
        if self.doing:
            parts.append(self.message)
        with self.cv:
            n = len(self.wanted)
        if n:
            parts.append(f"{n} waiting")
        if self.last_seconds:
            parts.append(f"last line took {self.last_seconds / 60:.1f} min" if self.last_seconds >= 90
                         else f"last line took {self.last_seconds:.0f}s")
        if not self.doing and not n:
            parts.append(self.message)
        return " - ".join(parts)

    # ------------------------------------------------------------------ tidying

    def tidy(self, keep_hours=48):
        cutoff = time.time() - keep_hours * 3600
        try:
            names = os.listdir(VOICE_DIR)
        except OSError:
            return
        for n in names:
            if n.startswith("standby-"):
                continue
            p = os.path.join(VOICE_DIR, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass

    def cached_count(self):
        total = 0
        for d in (self._usb_cache(), SD_CACHE):
            if d and os.path.isdir(d):
                for sub in os.scandir(d):
                    if sub.is_dir():
                        total += sum(1 for f in os.scandir(sub.path) if f.name.endswith(".mp3"))
        return total
