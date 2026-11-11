"""Dino's Radio audio engine.

Plays the queued songs one after another with crossfades, mixes the DJ over the music with the
music dipped underneath, and sends it all, as MP3, to Icecast through FFmpeg. Each song is decoded
by its own FFmpeg process on a reader thread with several seconds read ahead, so a slow NAS never
stalls the stream; if a song runs dry it plays silence rather than dropping the listeners. If nothing
is queued it plays the standby tracks, so the stream never goes off air."""
import base64
import collections
import itertools
import logging
import os
import random
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

from . import config

log = logging.getLogger("engine")

RATE = 44100
CHUNK = 4410                      # 0.1 s
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma")


class Track:
    """One song (or DJ clip), decoded to 16-bit stereo PCM on a background thread."""

    def __init__(self, path, gain_db=0.0, tag="", title="", artist="", read_ahead_s=20):
        self.path, self.gain_db, self.tag, self.title, self.artist = path, gain_db or 0.0, tag, title, artist
        self.id = None
        self.max_frames = int(read_ahead_s * RATE)
        self.chunks = collections.deque()
        self.buffered = 0
        self.eof = False
        self.failed = False
        self.produced = 0
        self.dry_since = None
        self.cond = threading.Condition()
        self.proc = None
        self.stopped = False
        self.thread = threading.Thread(target=self._read, daemon=True, name="decode")
        self.thread.start()

    def _read(self):
        af = f"volume={self.gain_db:.2f}dB" if self.gain_db else "anull"
        try:
            self.proc = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-i", self.path, "-vn", "-af", af,
                 "-f", "s16le", "-ar", str(RATE), "-ac", "2", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            leftover = b""
            while not self.stopped:
                data = self.proc.stdout.read(CHUNK * 4 * 5)
                if not data:
                    break
                data = leftover + data
                usable = len(data) - len(data) % 4
                leftover = data[usable:]
                frames = np.frombuffer(data[:usable], dtype=np.int16).reshape(-1, 2)
                with self.cond:
                    self.chunks.append(frames)
                    self.buffered += len(frames)
                    self.produced += len(frames)
                    while self.buffered > self.max_frames and not self.stopped:
                        self.cond.wait(1)
            self.proc.wait(timeout=10)
            if self.produced == 0 and not self.stopped:
                self.failed = True
        except Exception as e:
            log.warning("can't decode %s: %s", self.path, e)
            self.failed = True
        finally:
            with self.cond:
                self.eof = True
                self.cond.notify_all()

    def read(self, n):
        """n frames as float32 (silence where the decoder hasn't caught up)."""
        out = np.zeros((n, 2), dtype=np.float32)
        got = 0
        with self.cond:
            while got < n and self.chunks:
                c = self.chunks[0]
                take = min(n - got, len(c))
                out[got:got + take] = c[:take]
                if take == len(c):
                    self.chunks.popleft()
                else:
                    self.chunks[0] = c[take:]
                got += take
                self.buffered -= take
            self.cond.notify_all()
        if got < n and not self.eof:
            self.dry_since = self.dry_since or time.monotonic()
        elif got:
            self.dry_since = None
        return out

    @property
    def finished(self):
        return self.eof and self.buffered == 0

    @property
    def ending(self):
        """Fully read, with only this many frames left to play."""
        return self.buffered if self.eof else None

    def stop(self):
        self.stopped = True
        with self.cond:
            self.cond.notify_all()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except OSError:
            pass


class Engine:
    def __init__(self, settings):
        self.s = settings
        self.queue = collections.deque()      # Tracks waiting
        self.current = None
        self.incoming = None                  # the next Track, while crossfading
        self.xf_total = 0
        self.xf_pos = 0
        self.skip_left = None                 # frames left of a skip fade-out
        self.voices = collections.deque()
        self.voice = None
        self.duck = 1.0
        self.on_air_tag = ""
        self.ids = itertools.count(1)
        self.lock = threading.RLock()
        self.encoder = None
        self.encoder_ok = False
        self.encoder_started = 0.0
        self.stop_event = threading.Event()
        self.xfade = int(max(0.0, min(settings.crossfade_seconds, 10.0)) * RATE)
        self.duck_level = max(0.0, min(1.0, settings.duck_percent / 100))
        self.voice_gain = float(settings.voice_volume)
        self.last_standby = None

    # ------------------------------------------------------------------ control (from the brain)

    def push(self, path, gain_db=0.0, tag="", title="", artist=""):
        t = Track(path, gain_db, tag, title, artist)
        with self.lock:
            t.id = next(self.ids)
            self.queue.append(t)
        return t.id

    def say(self, path):
        with self.lock:
            self.voices.append(Track(path, read_ahead_s=120))

    def alive(self):
        with self.lock:
            ids = {t.id for t in self.queue if not t.failed}
            for t in (self.current, self.incoming):
                if t is not None:
                    ids.add(t.id)
            return ids

    def skip(self):
        with self.lock:
            if self.current is not None and self.skip_left is None:
                self.skip_left = RATE          # fade out over a second

    def flush_and_skip(self):
        with self.lock:
            for t in self.queue:
                t.stop()
            self.queue.clear()
        self.skip()

    def remaining(self):
        t = self.current
        return None if t is None else t.ending

    # ------------------------------------------------------------------ the stream

    def _encoder_cmd(self):
        s = self.s
        url = (f"icecast://source:{urllib.parse.quote(s.icecast_password, safe='')}@"
               f"{s.icecast_host}:{s.icecast_port}/{s.stream_mount}")
        return ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
                "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
                "-c:a", "libmp3lame", "-b:a", f"{int(s.bitrate)}k", "-content_type", "audio/mpeg",
                "-ice_name", s.name, "-ice_description", f"{s.name} - hits, album tracks and a DJ",
                "-ice_genre", "Various", "-ice_public", "0", "-f", "mp3", url]

    def _start_encoder(self):
        try:
            self.encoder = subprocess.Popen(self._encoder_cmd(), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            self.encoder_started = time.monotonic()
            log.info("streaming to Icecast as /%s", self.s.stream_mount)
        except OSError as e:
            log.error("can't start the encoder: %s", e)
            self.encoder = None
            self.encoder_ok = False

    def _write(self, pcm):
        if self.encoder is None or self.encoder.poll() is not None:
            self.encoder_ok = False
            if self.encoder is not None:
                err = b""
                try:
                    err = self.encoder.stderr.read() or b""
                except Exception:
                    pass
                log.warning("encoder stopped (%s) - restarting", err.decode(errors="replace").strip()[-200:])
                time.sleep(2)
            self._start_encoder()
            if self.encoder is None:
                time.sleep(5)
                return False
        try:
            self.encoder.stdin.write(pcm)
            # on air once the encoder has stayed connected to Icecast for a few seconds
            self.encoder_ok = time.monotonic() - self.encoder_started > 3
            return True
        except (BrokenPipeError, OSError):
            self.encoder_ok = False
            try:
                self.encoder.kill()
            except OSError:
                pass
            return False

    def _metadata(self, track):
        """Tells Icecast the song title (players show it)."""
        s = self.s
        text = f"{track.artist} - {track.title}" if track.artist else track.title
        url = (f"http://{s.icecast_host}:{s.icecast_port}/admin/metadata?mount=/{urllib.parse.quote(s.stream_mount)}"
               f"&mode=updinfo&song={urllib.parse.quote(text)}")
        auth = base64.b64encode(f"source:{s.icecast_password}".encode()).decode()

        def send():
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                log.debug("metadata not sent: %s", e)
        threading.Thread(target=send, daemon=True).start()

    # ------------------------------------------------------------------ choosing what plays

    def _standby_track(self):
        files = config.standby_tracks(self.s)
        if not files:
            return None
        path = random.choice([f for f in files if f != self.last_standby] or files)
        self.last_standby = path
        t = Track(path, tag="", title=os.path.splitext(os.path.basename(path))[0], artist=self.s.name)
        t.id = next(self.ids)
        return t

    def _next_track(self):
        """The next playable Track from the queue (skipping any that failed), else a standby track."""
        with self.lock:
            while self.queue:
                t = self.queue[0]
                if t.failed:
                    self.queue.popleft()
                    log.warning("skipping unplayable %s", t.path)
                    continue
                if t.produced == 0 and not t.eof:
                    # still opening - give it a moment (a slow NAS), unless it's taking far too long
                    return None
                return self.queue.popleft()
        return "standby"

    def _start(self, track):
        self.on_air_tag = track.tag
        self._metadata(track)

    # ------------------------------------------------------------------ the mixing loop

    def run(self):
        threading.Thread(target=self._loop, daemon=True, name="engine").start()

    def _loop(self):
        start = time.monotonic()
        sent = 0
        waiting_since = None
        while not self.stop_event.is_set():
            # keep about two seconds ahead of real time
            ahead = sent / RATE - (time.monotonic() - start)
            if ahead > 2.0:
                time.sleep(min(0.05, ahead - 2.0))
                continue
            if ahead < -5:                          # fell well behind (busy Pi) - don't rush to catch up
                start = time.monotonic() - sent / RATE
            pcm = self._mix_chunk(CHUNK, lambda: waiting_since)
            if self.current is None and not self.queue:
                waiting_since = waiting_since or time.monotonic()
            else:
                waiting_since = None
            if self._write(pcm.tobytes()):
                sent += CHUNK
            else:
                start = time.monotonic()
                sent = 0

    def _mix_chunk(self, n, waiting):
        with self.lock:
            # Nothing playing: start the next song, or standby if nothing comes for a few seconds
            if self.current is None:
                nxt = self._next_track()
                if nxt == "standby":
                    w = waiting()
                    if w is not None and time.monotonic() - w > 4:
                        nxt = self._standby_track()
                    else:
                        nxt = None
                if nxt is not None:
                    self.current = nxt
                    self._start(nxt)

            music = np.zeros((n, 2), dtype=np.float32)
            cur = self.current
            if cur is not None:
                # Begin the crossfade into the next song as the current one nears its end
                if self.incoming is None and self.skip_left is None:
                    left = cur.ending
                    if left is not None and left <= max(self.xfade, 1):
                        nxt = self._next_track()
                        if nxt == "standby":
                            nxt = None          # nothing queued yet - the brain will add something
                        if nxt is not None:
                            self.incoming = nxt
                            self.xf_total = max(left, 1)
                            self.xf_pos = 0
                            self._start(nxt)
                a = cur.read(n)
                if self.skip_left is not None:
                    ramp = np.clip((self.skip_left - np.arange(n)) / RATE, 0, 1).astype(np.float32)
                    a *= ramp[:, None]
                    self.skip_left -= n
                if self.incoming is not None:
                    pos = (self.xf_pos + np.arange(n)) / self.xf_total
                    g_in = np.clip(pos, 0, 1).astype(np.float32)
                    g_out = np.sqrt(1 - g_in)
                    g_in = np.sqrt(g_in)
                    music = a * g_out[:, None] + self.incoming.read(n) * g_in[:, None]
                    self.xf_pos += n
                else:
                    music = a
                # Done with this song?
                dry = cur.dry_since is not None and time.monotonic() - cur.dry_since > 15
                if cur.finished or dry or (self.skip_left is not None and self.skip_left <= 0):
                    if dry:
                        log.warning("music stopped arriving for %s - moving on", cur.path)
                    cur.stop()
                    self.current = self.incoming
                    self.incoming = None
                    self.skip_left = None

            # The DJ, with the music dipped underneath
            if self.voice is None and self.voices:
                self.voice = self.voices.popleft()
            target = self.duck_level if self.voice is not None else 1.0
            step = n / (RATE * (0.4 if target < self.duck else 1.2))
            new_duck = max(target, self.duck - step) if target < self.duck else min(target, self.duck + step)
            env = np.linspace(self.duck, new_duck, n, dtype=np.float32)
            self.duck = new_duck
            out = music * env[:, None]
            if self.voice is not None:
                out += self.voice.read(n) * self.voice_gain
                if self.voice.finished or self.voice.failed or (
                        self.voice.dry_since and time.monotonic() - self.voice.dry_since > 10):
                    self.voice.stop()
                    self.voice = None
            return np.clip(out, -32768, 32767).astype(np.int16)
