"""Dino's Radio - the brain. Chooses the music, prepares the DJ, watches the NAS and feeds the audio engine."""
import argparse
import collections
import datetime
import itertools
import json
import logging
import os
import random
import signal
import socket
import socketserver
import sys
import subprocess
import threading
import time

from . import backup, config, names, voices, webui
from .charts import Charts
from .db import connect
from .dj import DJ
from .engine import Engine
from .library import Library
from .picker import Picker, _CHRISTMAS
from .weather import Weather

log = logging.getLogger("brain")
STATUS_FILE = os.path.join(config.RUN_DIR, "status.json")
SOCKET_FILE = os.path.join(config.RUN_DIR, "brain.sock")
REMOUNT = "/usr/local/sbin/dinoradio-remount"
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac")


def picker_christmas(title):
    return bool(_CHRISTMAS.search(title or ""))


class Item:
    def __init__(self, kind, path, row=None, voice=None, title="", artist="", script=""):
        self.kind, self.path, self.row, self.voice = kind, path, row, list(voice or [])
        self.title, self.artist, self.script = title, artist, script
        self.rid = None
        self.tag = None
        self.pushed = 0.0
        self.started = None
        self.voice_sent = False


class Brain:
    def __init__(self, settings):
        self.s = settings
        self.stop = threading.Event()
        self.lib = Library(settings)
        self.charts = Charts(settings)
        self.picker = Picker(settings)
        self.dj = DJ(settings)
        self.voices = voices.VoiceCache(settings, self.dj.model, self.stop)
        self.voices.filler = self._lines_to_voice_ahead
        self.plan = collections.deque()  # songs chosen in advance: {"row", "talk", "back", "ident"}
        self.silent_for = 0             # songs since the DJ last actually spoke
        self.weather_line = None        # (text, when) - waiting to be voiced / said
        self.say_at = 0
        self.ahead = {"top10": [0, 0], "chart": [0, 0]}     # intros voiced ahead: [ready, total]
        self.ahead_checked = 0
        self.ahead_idle_until = 0
        self.weather = Weather(settings)
        self.engine = Engine(settings)
        self.items = {}                 # rid -> Item, pushed and not finished
        self.now = None                 # the Item on air
        self.preparing = False
        self.standby = False
        self.standby_since = None
        self.standby_test = False
        self.back_on_air = False
        self.ok_in_a_row = 0
        self.fail_in_a_row = 0
        self.nas_message = "Not checked yet"
        self.standby_voice = []
        self.recent_standby = []
        self.say_next = None            # text to say over the next song once it's voiced (dinoradio say)
        self.load_failures = 0
        self.started = time.time()
        self.history = []               # recent (time, artist, title, kind)
        self.counter = itertools.count(1)

    # ================================================================== threads

    def run(self):
        os.makedirs(config.RUN_DIR, exist_ok=True)
        os.makedirs(config.SD_STANDBY, exist_ok=True)
        self.standby_count = 0
        connect()
        self.engine.run()
        threading.Thread(target=self._prepare_standby_voice, daemon=True, name="standby-voice").start()
        if self.dj.available:
            threading.Thread(target=self._keep_running, args=(self.voices.run,), daemon=True, name="voices").start()
        for target in (self._dj_loop, self._health_loop, self._scan_loop, self._charts_loop, self._levelling_loop,
                       self._backup_loop, self._control_server):
            threading.Thread(target=self._keep_running, args=(target,), daemon=True, name=target.__name__).start()
        if self.s.web_port:
            threading.Thread(target=webui.serve, args=(self, self.s.web_port), daemon=True, name="web").start()
        log.info("Dino's Radio brain started - music folder %s", self.s.music_folder)
        last_status = 0
        while not self.stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("tick failed")
            if time.time() - last_status >= 2:
                self._write_status()
                last_status = time.time()
            self.stop.wait(1)

    def _tick(self):
        alive = self.engine.alive()
        on_air = self.engine.on_air_tag
        newest_started = None
        for rid, item in list(self.items.items()):
            if item.started is None and on_air == item.tag:
                self._started(item)
            if item.started is not None:
                newest_started = item if newest_started is None or item.started > newest_started.started \
                    else newest_started
        for rid, item in list(self.items.items()):
            if item.started is None:
                failed = (alive is not None and rid not in alive) or time.time() - item.pushed > 1800
                if failed:
                    log.warning("couldn't play %s", item.path)
                    if item.kind == "song":
                        self.load_failures += 1
                    del self.items[rid]
            elif item is not newest_started and time.time() - item.started > 60:
                del self.items[rid]       # finished
            # the DJ talks a moment after the song starts
            if item.started and item.voice and not item.voice_sent \
                    and time.time() - item.started >= self.s.talk_delay_seconds:
                item.voice_sent = True
                for clip in item.voice:
                    self.engine.say(clip)
        pending = [i for i in self.items.values() if i.started is None]
        if not pending and not self.preparing:
            self.preparing = True
            threading.Thread(target=self._prepare, daemon=True, name="prepare").start()

    def _started(self, item):
        item.started = time.time()
        self.now = item
        if item.kind == "song":
            self.picker.played(item.row)
            self.load_failures = 0
        self.history.insert(0, (time.strftime("%H:%M"), item.artist, item.title, item.kind))
        del self.history[20:]
        log.info("on air: %s - %s%s", item.artist, item.title,
                 f'  [DJ: "{item.script}"]' if item.voice and item.kind == "song" else ("  [DJ]" if item.voice else ""))

    def _keep_running(self, job):
        """Runs a background job, and starts it again if it ever stops with an error."""
        while not self.stop.is_set():
            try:
                job()
                return
            except Exception:
                log.exception("%s stopped with an error - restarting it in 30 seconds", job.__name__.strip("_"))
                self.stop.wait(30)

    # ================================================================== preparing the next item

    def _prepare(self):
        try:
            item = self._standby_item() if self.standby else self._song_item()
            if item is None:
                self.stop.wait(5)
                return
            item.tag = f"{os.getpid()}-{next(self.counter)}"
            gain = item.row["gain"] if item.row is not None and item.row["gain"] is not None \
                and self.s.levelling else 0.0
            rid = self.engine.push(item.path, gain_db=gain, tag=item.tag, title=item.title, artist=item.artist)
            item.rid = rid
            item.pushed = time.time()
            self.items[rid] = item
        except OSError as e:
            log.warning("prepare: %s", e)
            self.stop.wait(3)
        except Exception:
            log.exception("prepare failed")
            self.stop.wait(5)
        finally:
            self.preparing = False

    def _plan_songs(self):
        """Keeps the next few songs chosen in advance, and asks for their DJ lines to be voiced -
        the nearest first."""
        while len(self.plan) < max(1, self.s.plan_ahead):
            taken = [i.row for i in self.items.values() if i.row is not None] + [p["row"] for p in self.plan]
            row = self.picker.pick(exclude_ids={r["id"] for r in taken},
                                   exclude_artists={names.artist_key(r["artist"]) for r in taken},
                                   exclude_keys={r["chart_key"] for r in taken})
            if row is None:
                break
            row = dict(row)
            prev = self.plan[-1]["row"] if self.plan else (
                self.now.row if self.now is not None and self.now.kind == "song" else None)
            entry = {"row": row, "talk": False, "back": None, "ident": None}
            if self.dj.available:
                entry["talk"] = self.dj.should_talk(Picker.group_of(row))
                self.dj.note(entry["talk"])
                if entry["talk"] and prev is not None and random.random() * 100 < self.s.back_announce_chance:
                    entry["back"] = self.dj.back_announce(prev)
                if entry["talk"] and random.random() * 100 < self.s.station_ident_chance:
                    entry["ident"] = random.choice(self.dj.idents())
            self.plan.append(entry)
        if self.dj.available:
            for n, e in enumerate(self.plan):
                if e["talk"]:
                    self.voices.want(self.dj.intro(e["row"]), 10 + n)
                    for extra in (e["back"], e["ident"]):
                        self.voices.want(extra, 40 + n)

    def _song_item(self):
        self._plan_songs()
        if not self.plan:
            self.nas_message = "No music in the library yet"
            return None
        entry = self.plan.popleft()
        row = entry["row"]
        # Measure its loudness now if there's time (a song is playing); otherwise it plays unlevelled this once
        if row["gain"] is None and self.s.levelling and not self.standby and self.now is not None:
            row["gain"] = self.lib.measure(row["id"], row["path"], timeout=120)
        clips, words = self._link(entry)
        self._plan_songs()
        return Item("song", row["path"], row=row, voice=clips, title=row["title"], artist=row["artist"],
                    script=" ".join(words))

    def _link(self, entry):
        """What the DJ says over this song: only lines whose voice clips are ready.
        Anything not voiced in time is simply left out - the song plays without a link."""
        if not self.dj.available:
            return [], []
        v = self.voices
        clips, words = [], []

        def add(text, kind=voices.KEEP):
            p = v.ready(text, kind)
            if p:
                clips.append(p)
                words.append(text)
            return p

        special = False
        if self.back_on_air:
            self.back_on_air = False
            special = any(add(t) for t in random.sample(self.dj.back_lines(), 2))
        if self.say_next:
            if add(self.say_next, voices.SHORT):
                self.say_next, special = None, True
            elif time.time() - self.say_at > 3600:
                log.info("gave up waiting for the voice for: %s", self.say_next)
                self.say_next = None
        if self.weather_line and not special:
            text, when = self.weather_line
            if add(text, voices.SHORT):
                self.weather_line, special = None, True
            elif time.time() - when > 45 * 60:
                log.info("the weather wasn't voiced in time - skipped this hour")
                self.weather_line = None
        intro = self.dj.intro(entry["row"])
        if v.ready(intro) and (entry["talk"] or special or self.silent_for + 1 >= max(1, self.s.at_least_every)):
            if not special:
                for extra in (entry["back"], entry["ident"]):
                    if extra:
                        add(extra)
            add(intro)
        self.silent_for = 0 if clips else self.silent_for + 1
        return clips, words

    def _lines_to_voice_ahead(self):
        """For quiet moments: introductions for the top 10 hits, then the other chart hits,
        that haven't been voiced yet. Also keeps count for the status page."""
        if not self.dj.available or time.time() < self.ahead_idle_until:
            return []
        rows = connect().execute(
            """SELECT t.artist, t.title, t.chart_key, c.country, c.position, c.year
               FROM tracks t JOIN charts c ON c.key = t.chart_key
               WHERE c.position BETWEEN 1 AND 100 GROUP BY t.chart_key ORDER BY c.position, t.chart_key""").fetchall()
        todo = {"top10": [], "chart": []}
        counts = {"top10": [0, 0], "chart": [0, 0]}
        christmas_ok = self.picker._christmas_allowed()
        for r in rows:
            if not christmas_ok and picker_christmas(r["title"]):
                continue
            g = "top10" if r["position"] <= 10 else "chart"
            text = self.dj.intro(r)
            counts[g][1] += 1
            if self.voices.ready(text):
                counts[g][0] += 1
            else:
                todo[g].append(text)
        self.ahead = counts
        out = todo["top10"][:5] + (todo["chart"][:5] if self.s.chart_chance > 0 else [])
        if not out:
            self.ahead_idle_until = time.time() + 600     # all done - look again in a while (new music)
        return out

    def _standby_item(self):
        files = config.standby_tracks(self.s)
        if not files:
            self.stop.wait(5)
            return None
        recent = self.recent_standby[-min(len(files) - 1, 20):] if len(files) > 1 else []
        path = random.choice([f for f in files if f not in recent] or files)
        self.recent_standby.append(path)
        del self.recent_standby[:-20]
        # the "please hold" message on the first standby track, then every few tracks
        voice = []
        if self.standby_voice and self.standby_count % max(1, self.s.message_every) == 0:
            voice = [random.choice(self.standby_voice)]
        self.standby_count += 1
        title, artist = os.path.splitext(os.path.basename(path))[0], self.s.name
        row = connect().execute("SELECT artist, title FROM tracks WHERE path LIKE ? LIMIT 1",
                                ("%/" + os.path.basename(path),)).fetchone()
        if row:
            artist, title = row["artist"], row["title"]
        return Item("standby", path, voice=voice, title=title, artist=artist,
                    script="(standby message)" if voice else "")

    def _prepare_standby_voice(self):
        """The 'please hold' messages are voiced first and kept, so they work without the NAS."""
        if not self.dj.available:
            return
        lines = self.dj.standby_lines()
        # the first apology straight away; the rest after the next songs' introductions
        for n, t in enumerate(lines):
            self.voices.want(t, 0 if n == 0 else 45, voices.STANDBY)
        for t in self.dj.back_lines():
            self.voices.want(t, 45)
        for t in self.dj.idents():
            self.voices.want(t, 50)
        files = []
        for t in lines:
            p = self.voices.wait_for(t, voices.STANDBY, 3 * 3600)
            if p:
                files.append(p)
                self.standby_voice = list(files)
        if files:
            log.info("standby messages ready (%d)", len(files))

    def _dj_loop(self):
        """Fetches the weather when it's due, so it can be voiced before it's read out."""
        while not self.stop.is_set():
            if self.dj.available and not self.standby and self.weather_line is None:
                line = self.weather.take()
                if line:
                    self.weather_line = (line, time.time())
                    self.voices.want(line, 1, voices.SHORT)
            self.stop.wait(30)

    # ================================================================== the NAS

    def _nas_ok(self) -> bool:
        root = self.s.music_folder
        con = connect()
        samples = [r[0] for r in con.execute("SELECT path FROM tracks ORDER BY random() LIMIT 2")]
        script = 'ls -d "$1" >/dev/null || exit 1; shift; [ $# -eq 0 ] && exit 0; ' \
                 'for f in "$@"; do head -c 65536 "$f" >/dev/null 2>&1 && exit 0; done; exit 1'
        try:
            r = subprocess.run(["timeout", "20", "sh", "-c", script, "check", root] + samples,
                               capture_output=True, timeout=30)
            return r.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    def _health_loop(self):
        while not self.stop.is_set():
            ok = self._nas_ok() and not self.standby_test
            if self.load_failures >= 2:
                ok = False
            if ok:
                self.ok_in_a_row += 1
                self.fail_in_a_row = 0
                self.nas_message = "Connected"
            else:
                self.fail_in_a_row += 1
                self.ok_in_a_row = 0
                self.nas_message = "Test standby" if self.standby_test else "Can't read the music folder"
                if not self.standby_test and self.fail_in_a_row % 3 == 2 and os.path.exists(REMOUNT):
                    log.info("trying to reconnect the music share")
                    subprocess.run(["sudo", "-n", REMOUNT], capture_output=True, timeout=60)
            if not self.standby and (self.fail_in_a_row >= 2 or self.standby_test):
                self._enter_standby()
            elif self.standby and self.ok_in_a_row >= 2:
                self._leave_standby()
            self.stop.wait(5 if self.standby_test or self.standby else self.s.check_seconds)

    def _enter_standby(self):
        log.warning("music library unavailable - going to standby")
        self.standby = True
        self.standby_since = time.time()
        self.standby_count = 1          # the apology plays now, so the next message comes a few tracks later
        self.load_failures = 0
        # Apologise straight away, then the standby loop takes over
        if self.standby_voice:
            try:
                self.engine.say(random.choice(self.standby_voice[:2]))
            except OSError:
                pass
        # Whatever is playing or queued from the NAS won't play now: clear it and start the standby loop
        for _ in range(30):
            if not self.preparing:
                break
            time.sleep(1)
        self.preparing = True
        self.engine.flush_and_skip()
        for rid, item in list(self.items.items()):
            if item.started is None:
                del self.items[rid]
        self._prepare()

    def _leave_standby(self):
        log.info("music library is back - back on air after %.0f minutes", (time.time() - self.standby_since) / 60)
        self.standby = False
        self.standby_since = None
        self.back_on_air = True
        self.load_failures = 0

    # ================================================================== background work

    def _scan_loop(self):
        last_quick = 0
        last_full_day = None
        while not self.stop.is_set():
            now = time.localtime()
            need_full = connect().execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
            due_full = now.tm_hour == self.s.full_rescan_hour and last_full_day != now.tm_yday
            due_quick = time.time() - last_quick >= self.s.rescan_minutes * 60
            if not self.standby and (need_full or due_full or due_quick or self.rescan_requested):
                full = due_full or self.rescan_requested == "full"
                self.rescan_requested = None
                try:
                    self.lib.scan(full=full, stop=self.stop)
                    if full:
                        last_full_day = now.tm_yday
                except OSError as e:
                    self.lib.message = f"Scan stopped: {e}"
                    log.warning(self.lib.message)
                last_quick = time.time()
                self.voices.tidy()
            self.stop.wait(20)

    rescan_requested = None
    backup_requested = False
    backup_message = "None yet"

    def _backup_loop(self):
        last_day = None
        latest = backup.latest(self.s) if not backup.drive_missing(self.s) else None
        if latest:
            self.backup_message = "Last: " + os.path.basename(latest)[len(backup.PREFIX):-7]
        while not self.stop.is_set():
            now = time.localtime()
            due = now.tm_hour == self.s.backup_hour and last_day != now.tm_yday
            if due or self.backup_requested:
                self.backup_requested = False
                try:
                    out = backup.make(self.s)
                    self.backup_message = "Last: " + os.path.basename(out)[len(backup.PREFIX):-7]
                    last_day = now.tm_yday
                except Exception as e:
                    self.backup_message = f"Failed {time.strftime('%d %b %H:%M')}: {e}"
                    log.warning("backup failed: %s", e)
                    if due:
                        last_day = now.tm_yday      # try again tomorrow (or: dinoradio backup)
            self.stop.wait(20)

    def _charts_loop(self):
        while not self.stop.is_set():
            worked = False
            if not self.standby:
                try:
                    worked = self.charts.look_up_one()
                except Exception:
                    log.exception("chart lookup failed")
            self.stop.wait(3 if worked else 60)        # gently - about one song every few seconds

    def _levelling_loop(self):
        while not self.stop.is_set():
            row = None
            if self.s.levelling and not self.standby and not self.lib.scanning:
                row = self.lib.next_unmeasured()
                if row is not None:
                    self.lib.measure(row["id"], row["path"])
            self.stop.wait(2 if row is not None else 300)

    # ================================================================== status and control

    def status(self) -> dict:
        def item_info(i):
            if i is None:
                return None
            d = {"kind": i.kind, "artist": i.artist, "title": i.title, "dj": i.script or None}
            if i.row is not None and i.row.get("position"):
                d["chart"] = f'{i.row["country"]} {i.row["position"]}' + (f' ({i.row["year"]})' if i.row.get("year") else "")
            return d
        pending = [i for i in self.items.values() if i.started is None]
        now = item_info(self.now)
        cur = self.engine.current
        if cur is not None and (self.now is None or not cur.tag):      # nothing queued yet, or a fallback track
            now = {"kind": "standby" if not cur.tag else "song", "artist": cur.artist, "title": cur.title, "dj": None}
        return {
            "station": self.s.name,
            "state": "STANDBY" if self.standby else ("ON AIR" if self.engine.encoder_ok else "NOT STREAMING"),
            "standby_minutes": round((time.time() - self.standby_since) / 60) if self.standby_since else None,
            "now": now,
            "next": item_info(pending[0]) if pending else None,
            "nas": self.nas_message,
            "library": self.lib.message,
            "levelling": self.lib.counts(),
            "charts": {**self.charts.counts(), "message": self.charts.message},
            "dj": ("on - " + self.voices.status()) if self.dj.available else
                  ("off" if not self.s.dj_enabled else "voice not installed"),
            "voiced_ahead": self._ahead_text() if self.dj.available else None,
            "coming_up": [{"artist": p["row"]["artist"], "title": p["row"]["title"],
                           "dj": "ready" if p["talk"] and self.voices.ready(self.dj.intro(p["row"]))
                           else ("waiting" if p["talk"] else None)} for p in list(self.plan)],
            "standby_tracks": len(config.standby_tracks(self.s)),
            "standby_messages": len(self.standby_voice),
            "backup": self.backup_message,
            "weather": self.weather.message,
            "history": self.history[:10],
            "stream": f"http://<this pi>:{self.s.icecast_port}/{self.s.stream_mount}",
            "uptime_minutes": round((time.time() - self.started) / 60),
        }

    def _ahead_text(self):
        t, c = self.ahead["top10"], self.ahead["chart"]
        if not t[1] and not c[1]:
            return "Counting..."
        return f"Top 10 hits {t[0]} of {t[1]}, other chart hits {c[0]} of {c[1]}"

    def _write_status(self):
        try:
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.status(), f, indent=1)
            os.replace(tmp, STATUS_FILE)
        except Exception as e:
            log.debug("status not written: %s", e)

    def handle(self, line: str) -> str:
        cmd, _, arg = line.strip().partition(" ")
        if cmd == "status":
            return json.dumps(self.status())
        if cmd == "skip":
            self.engine.skip()
            return "Skipped."
        if cmd == "rescan":
            self.rescan_requested = "full" if arg == "full" else "quick"
            return "Rescan started." if not self.standby else "On standby - will rescan when the NAS is back."
        if cmd == "backup":
            self.backup_requested = True
            return f"Backing up to {backup.folder_for(self.s)} - check with: dinoradio status"
        if cmd == "weather":
            if not self.weather.enabled:
                return "No weather location is set - add one with: dinoradio settings  ([weather] location)"
            self.weather.next_due = datetime.datetime.now()
            return ("Getting the weather now. The DJ reads it over the first song after it's been voiced "
                    "(a few minutes - longer with a high-quality voice).")
        if cmd == "say":
            if not self.dj.available:
                return "The DJ voice isn't available."
            self.say_next = arg.strip()
            self.say_at = time.time()
            self.voices.want(self.say_next, 0, voices.SHORT)
            return ("The DJ will say that over the first song after it's been voiced "
                    "(a few minutes - longer with a high-quality voice).")
        if cmd == "standby":
            self.standby_test = arg.strip() == "on"
            return "Standby test on - it switches over within a few seconds." if self.standby_test \
                else "Standby test off - back on air within a few seconds."
        return "Unknown command"

    def _control_server(self):
        brain = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline().decode("utf-8", "replace")
                try:
                    reply = brain.handle(line)
                except Exception as e:
                    reply = f"Error: {e}"
                self.wfile.write(reply.encode("utf-8") + b"\n")

        try:
            os.remove(SOCKET_FILE)
        except OSError:
            pass
        server = socketserver.ThreadingUnixStreamServer(SOCKET_FILE, Handler)
        os.chmod(SOCKET_FILE, 0o666)
        server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Dino's Radio brain")
    ap.add_argument("--scan", action="store_true", help="scan the music folder and exit")
    ap.add_argument("--fill-standby", type=int, metavar="N", help="copy N random tracks to the standby folder and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().addHandler(webui.LOG)
    s = config.load()
    if args.scan:
        print(Library(s).scan(full=True))
        return
    if args.fill_standby:
        fill_standby(s, args.fill_standby)
        return
    brain = Brain(s)
    signal.signal(signal.SIGTERM, lambda *a: brain.stop.set())
    brain.run()


def random_files(root: str, n: int, tries: int = 400):
    """n random songs without reading the whole library: wander down random folders."""
    found = set()
    for _ in range(tries):
        if len(found) >= n:
            break
        folder = root
        for _depth in range(6):
            try:
                entries = [e for e in os.scandir(folder) if not e.name.startswith((".", "@", "#"))]
            except OSError:
                break
            songs = [e.path for e in entries if e.is_file() and e.name.lower().endswith(AUDIO_EXT)
                     and 2_000_000 < e.stat().st_size < 60_000_000]
            dirs = [e.path for e in entries if e.is_dir()]
            if songs and (not dirs or random.random() < 0.6):
                found.add(random.choice(songs))
                break
            if not dirs:
                break
            folder = random.choice(dirs)
    return list(found)


def fill_standby(s, n: int):
    """Copies n more songs from the library to the standby folder (normally on the USB drive):
    top 10 hits first, one per artist, leaving at least 500 MB free on the drive."""
    import shutil
    folder = s.standby_folder
    if folder.startswith(config.USB_MOUNT + "/") and not os.path.ismount(config.USB_MOUNT):
        sys.exit("The USB drive isn't plugged in.")
    os.makedirs(folder, exist_ok=True)
    have = set(os.listdir(folder))
    rows = connect().execute(
        """SELECT t.path, t.artist FROM tracks t LEFT JOIN charts c ON c.key = t.chart_key
           WHERE t.duration BETWEEN 120 AND 420
           ORDER BY CASE WHEN c.position <= 10 THEN 0 WHEN c.position <= 40 THEN 1 ELSE 2 END, random()
           LIMIT ?""", (n * 20,)).fetchall()
    candidates, artists = [], set()
    for path, artist in rows:
        a = (artist or "").lower()
        if os.path.basename(path) in have or a in artists:
            continue
        artists.add(a)
        candidates.append(path)
    if not candidates:
        candidates = [p for p in random_files(s.music_folder, n) if os.path.basename(p) not in have]
    copied = 0
    for path in candidates[:n]:
        if shutil.disk_usage(folder).free < 500_000_000:
            print("Stopping - the drive is nearly full")
            break
        dest = os.path.join(folder, os.path.basename(path))
        try:
            shutil.copyfile(path, dest + ".part")
            os.replace(dest + ".part", dest)
            copied += 1
            print(f"  {copied:3}  {os.path.basename(path)}")
        except OSError as e:
            print(f"  skipped {os.path.basename(path)}: {e}")
    total = len([f for f in os.listdir(folder) if f.lower().endswith(AUDIO_EXT)])
    print(f"Copied {copied} - the standby folder now has {total} tracks "
          f"({shutil.disk_usage(folder).free / 1e9:.1f} GB free)")


if __name__ == "__main__":
    main()
