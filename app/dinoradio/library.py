"""Keeps the track list in step with the music folder, and measures loudness for volume levelling."""
import logging
import os
import re
import subprocess
import time

from . import names
from .db import connect

log = logging.getLogger("library")

try:
    import mutagen
except ImportError:            # pragma: no cover - installed by install.sh
    mutagen = None


def _first(tags, *keys):
    if not tags:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            v = v[0] if isinstance(v, (list, tuple)) else v
            v = str(v).strip()
            if v:
                return v
    return ""


def _replaygain_to_amplify(value: str, target_lufs: float):
    """ReplayGain 2 aims at -18 LUFS; shift it to our target. '−3.21 dB' -> float."""
    m = re.search(r"[-+]?\d+(\.\d+)?", value or "")
    if not m:
        return None
    return round(float(m.group()) + (target_lufs + 18.0), 2)


def read_tags(path: str, target_lufs: float):
    artist = title = album = genre = ""
    duration = 0.0
    gain = None
    if mutagen is not None:
        try:
            f = mutagen.File(path, easy=True)
            if f is not None:
                tags = f.tags
                artist = _first(tags, "artist", "albumartist")
                title = _first(tags, "title")
                album = _first(tags, "album")
                genre = _first(tags, "genre")
                rg = _first(tags, "replaygain_track_gain")
                if rg:
                    gain = _replaygain_to_amplify(rg, target_lufs)
                if getattr(f, "info", None) is not None:
                    duration = float(getattr(f.info, "length", 0) or 0)
        except Exception as e:
            log.debug("tags unreadable %s: %s", path, e)
    if not title or not artist:
        a, t = names.from_filename(path)
        artist = artist or a
        title = title or t
    return dict(artist=artist, title=title, album=album, genre=genre, duration=duration, gain=gain)


class Library:
    def __init__(self, settings):
        self.s = settings
        self.root = settings.music_folder
        self.scanning = False
        self.last_scan = 0.0
        self.last_full_scan = 0.0
        self.message = "Not scanned yet"

    # ------------------------------------------------------------------ scanning

    def _wanted(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in self.s.extensions and not name.startswith("._")

    def scan(self, full: bool = False, stop=None) -> dict:
        """Quick scan: only folders whose date changed (files added, removed or renamed).
        Full scan: every file's size and date too (catches tags edited in place)."""
        con = connect()
        self.scanning = True
        self.message = "Scanning"
        started = time.time()
        added = changed = removed = 0
        try:
            if not os.path.isdir(self.root):
                raise OSError(f"music folder {self.root} isn't available")
            known_folders = {r["path"]: r["mtime"] for r in con.execute("SELECT path, mtime FROM folders")}
            seen_folders = set()
            def _raise(e):
                raise e
            files_seen = 0
            for folder, dirs, files in os.walk(self.root, onerror=_raise):
                if stop is not None and stop.is_set():
                    break
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "@eaDir" and d != "#recycle")
                seen_folders.add(folder)
                files_seen += len(files)
                mtime = os.stat(folder).st_mtime
                if not full and known_folders.get(folder) == mtime:
                    continue
                a, c, r = self._scan_folder(con, folder, [f for f in files if self._wanted(f)])
                added, changed, removed = added + a, changed + c, removed + r
                con.execute("INSERT OR REPLACE INTO folders(path, mtime) VALUES (?, ?)", (folder, mtime))
                self.message = f"Scanning - {added} new tracks so far" if added else "Scanning"
            else:
                # An empty music folder almost always means the share isn't really there - keep the list
                if files_seen == 0 and len(known_folders) > 1:
                    raise OSError(f"music folder {self.root} looks empty - is the NAS share connected?")
                # folders that have gone away
                for gone in set(known_folders) - seen_folders:
                    n = con.execute("DELETE FROM tracks WHERE folder = ?", (gone,)).rowcount
                    removed += n
                    con.execute("DELETE FROM folders WHERE path = ?", (gone,))
                if full:
                    self.last_full_scan = time.time()
                self.last_scan = time.time()
            total = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            self.message = (f"{total} tracks - last scan {time.strftime('%H:%M')}"
                            f" (+{added} new, {changed} changed, -{removed} removed, {time.time() - started:.0f}s)")
            log.info("scan%s: %s", " (full)" if full else "", self.message)
            return dict(added=added, changed=changed, removed=removed, total=total)
        finally:
            self.scanning = False

    def _scan_folder(self, con, folder, files):
        rows = {r["path"]: r for r in con.execute("SELECT id, path, size, mtime FROM tracks WHERE folder = ?", (folder,))}
        added = changed = 0
        present = set()
        for name in files:
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            present.add(path)
            old = rows.get(path)
            if old is not None and old["size"] == st.st_size and old["mtime"] == st.st_mtime:
                continue
            t = read_tags(path, self.s.target_lufs)
            key = names.chart_key(t["artist"], t["title"])
            if old is None:
                con.execute("""INSERT INTO tracks(path, folder, size, mtime, artist, title, album, genre, duration,
                               chart_key, gain, added) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (path, folder, st.st_size, st.st_mtime, t["artist"], t["title"], t["album"], t["genre"],
                             t["duration"], key, t["gain"], time.time()))
                added += 1
            else:
                con.execute("""UPDATE tracks SET size=?, mtime=?, artist=?, title=?, album=?, genre=?, duration=?,
                               chart_key=?, gain=? WHERE id=?""",
                            (st.st_size, st.st_mtime, t["artist"], t["title"], t["album"], t["genre"], t["duration"],
                             key, t["gain"], old["id"]))
                changed += 1
        removed = 0
        for path, r in rows.items():
            if path not in present:
                con.execute("DELETE FROM tracks WHERE id = ?", (r["id"],))
                removed += 1
        return added, changed, removed

    # ------------------------------------------------------------------ loudness

    def measure(self, track_id: int, path: str, timeout: int = 240):
        """Measures a track's loudness (EBU R128) with ffmpeg and stores the gain for levelling.
        A track that can't be measured (damaged, or far too slow) is marked 0 dB - played as it is -
        so it isn't tried again and again. If the music folder itself has gone, nothing is marked."""
        try:
            # tags in odd encodings appear in ffmpeg's output, so never fail on decoding it
            out = subprocess.run(
                ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", path,
                 "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                capture_output=True, timeout=timeout).stderr.decode("utf-8", "replace")
            m = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", out)
            if m:
                gain = max(-15.0, min(12.0, round(self.s.target_lufs - float(m[-1]), 2)))
            elif not os.path.isdir(self.root):
                return None
            else:
                log.info("couldn't measure the loudness of %s - it will play as it is", path)
                gain = 0.0
        except subprocess.TimeoutExpired:
            log.info("measuring %s took too long - it will play as it is", path)
            gain = 0.0
        except OSError as e:
            log.debug("loudness not measured for %s: %s", path, e)
            return None
        connect().execute("UPDATE tracks SET gain = ? WHERE id = ?", (gain, track_id))
        return gain

    def next_unmeasured(self):
        return connect().execute(
            """SELECT id, path FROM tracks WHERE gain IS NULL AND (duration = 0 OR duration BETWEEN ? AND ?)
               ORDER BY random() LIMIT 1""", (self.s.min_seconds, self.s.max_seconds)).fetchone()

    def counts(self) -> dict:
        con = connect()
        playable = "(duration = 0 OR duration BETWEEN ? AND ?)"
        args = (self.s.min_seconds, self.s.max_seconds)
        total = con.execute(f"SELECT COUNT(*) FROM tracks WHERE {playable}", args).fetchone()[0]
        levelled = con.execute(f"SELECT COUNT(*) FROM tracks WHERE gain IS NOT NULL AND {playable}", args).fetchone()[0]
        return dict(total=total, levelled=levelled)
