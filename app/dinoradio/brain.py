"""Dino's Radio - the brain. Chooses the music, prepares the DJ, watches the NAS and feeds the audio engine."""
import argparse
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

from . import backup, config, webui
from .charts import Charts
from .db import connect
from .dj import DJ
from .engine import Engine
from .library import Library
from .picker import Picker

log = logging.getLogger("brain")
STATUS_FILE = os.path.join(config.RUN_DIR, "status.json")
SOCKET_FILE = os.path.join(config.RUN_DIR, "brain.sock")
REMOUNT = "/usr/local/sbin/dinoradio-remount"
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac")


class Item:
    def __init__(self, kind, path, row=None, voice=None, title="", artist="", script=""):
        self.kind, self.path, self.row, self.voice = kind, path, row, voice
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
        self.say_next = None            # text to say over the next song (dinoradio say)
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
        for target in (self._health_loop, self._scan_loop, self._charts_loop, self._levelling_loop,
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
                self.engine.say(item.voice)
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
        log.info("on air: %s - %s%s", item.artist, item.title, "  [DJ]" if item.voice else "")

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

    def _song_item(self):
        exclude = {i.row["id"] for i in self.items.values() if i.row is not None}
        row = self.picker.pick(exclude_ids=exclude)
        if row is None:
            self.nas_message = "No music in the library yet"
            return None
        row = dict(row)
        # Measure its loudness now if there's time (a song is playing); otherwise it plays unlevelled this once
        if row["gain"] is None and self.s.levelling and not self.standby and self.now is not None:
            row["gain"] = self.lib.measure(row["id"], row["path"], timeout=120)
        group = Picker.group_of(row)
        back = self.back_on_air
        said = self.say_next
        voice, script = None, ""
        if self.dj.available and (said or self.dj.should_talk(group, forced=back)):
            previous = self.now.row if self.now is not None and self.now.kind == "song" else None
            script = said or self.dj.script(row, previous=previous, back_on_air=back)
            voice = self.dj.speak(script)
            self.dj.note(voice is not None)
        else:
            self.dj.note(False)
        if back:
            self.back_on_air = False
        if said:
            self.say_next = None
        return Item("song", row["path"], row=row, voice=voice, title=row["title"], artist=row["artist"],
                    script=script)

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
        voice = None
        if self.standby_voice and self.standby_count % max(1, self.s.message_every) == 0:
            voice = random.choice(self.standby_voice)
        self.standby_count += 1
        title, artist = os.path.splitext(os.path.basename(path))[0], self.s.name
        row = connect().execute("SELECT artist, title FROM tracks WHERE path LIKE ? LIMIT 1",
                                ("%/" + os.path.basename(path),)).fetchone()
        if row:
            artist, title = row["artist"], row["title"]
        return Item("standby", path, voice=voice, title=title, artist=artist,
                    script="(standby message)" if voice else "")

    def _prepare_standby_voice(self):
        self.standby_voice = self.dj.standby_messages()
        if self.standby_voice:
            log.info("standby messages ready (%d)", len(self.standby_voice))

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
                self.dj.tidy()
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
            "dj": ("on - " + self.dj.message) if self.dj.available else
                  ("off" if not self.s.dj_enabled else "voice not installed"),
            "standby_tracks": len(config.standby_tracks(self.s)),
            "standby_messages": len(self.standby_voice),
            "backup": self.backup_message,
            "history": self.history[:10],
            "stream": f"http://<this pi>:{self.s.icecast_port}/{self.s.stream_mount}",
            "uptime_minutes": round((time.time() - self.started) / 60),
        }

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
        if cmd == "say":
            if not self.dj.available:
                return "The DJ voice isn't available."
            self.say_next = arg.strip()
            return "The DJ will say that over the next song to be queued (within a few minutes)."
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
