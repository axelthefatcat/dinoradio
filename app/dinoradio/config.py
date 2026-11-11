"""Dino's Radio settings - read from /etc/dinoradio/settings.toml (edit with: dinoradio settings)."""
import os
import tomllib
from dataclasses import dataclass, field

SETTINGS_FILE = os.environ.get("DINORADIO_SETTINGS", "/etc/dinoradio/settings.toml")
DATA_DIR = os.environ.get("DINORADIO_DATA", "/var/lib/dinoradio")
RUN_DIR = os.environ.get("DINORADIO_RUN", "/run/dinoradio")
APP_DIR = os.environ.get("DINORADIO_APP", "/opt/dinoradio")


@dataclass
class Settings:
    # [station]
    name: str = "Dino's Radio"
    stream_mount: str = "dinoradio"
    bitrate: int = 128
    icecast_host: str = "127.0.0.1"
    icecast_port: int = 8000
    icecast_password: str = "hackme"
    # [music]
    music_folder: str = "/mnt/music"
    rescan_minutes: int = 15
    full_rescan_hour: int = 4
    extensions: list = field(default_factory=lambda: [".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac"])
    min_seconds: int = 60
    max_seconds: int = 720
    exclude_genres: list = field(default_factory=list)
    christmas: str = "december"          # december / always / never
    # [mix]
    top10_percent: int = 50
    chart_percent: int = 35
    other_percent: int = 15
    song_repeat_hours: int = 72
    artist_gap: int = 15
    # [dj]
    dj_enabled: bool = True
    voice: str = "en_GB-cori-medium"
    top10_chance: int = 100
    chart_chance: int = 50
    other_chance: int = 17
    max_in_a_row: int = 2
    at_least_every: int = 8
    back_announce_chance: int = 40
    station_ident_chance: int = 20
    talk_delay_seconds: float = 2.0
    duck_percent: int = 25
    voice_volume: float = 1.4
    # [sound]
    crossfade_seconds: float = 4.0
    levelling: bool = True
    target_lufs: float = -16.0
    # [charts]
    charts_enabled: bool = True
    contact_email: str = ""
    # [standby]
    check_seconds: int = 30
    standby_folder: str = os.path.join(DATA_DIR, "standby")
    message_every: int = 3               # in standby, the "please hold" message every this many tracks
    # [web]
    web_port: int = 8080                 # the status page - 0 turns it off
    # [backup]
    backup_folder: str = ""              # blank = on the SD card only (DATA_DIR/backups)
    backup_hour: int = 3
    backup_keep: int = 7
    telnet_port: int = 1234


SECTIONS = {
    "station": {"name": "name", "stream_mount": "stream_mount", "bitrate": "bitrate",
                "icecast_port": "icecast_port", "icecast_password": "icecast_password"},
    "music": {"folder": "music_folder", "rescan_minutes": "rescan_minutes", "full_rescan_hour": "full_rescan_hour",
              "extensions": "extensions", "min_seconds": "min_seconds", "max_seconds": "max_seconds",
              "exclude_genres": "exclude_genres", "christmas_songs": "christmas"},
    "mix": {"top10_percent": "top10_percent", "chart_percent": "chart_percent", "other_percent": "other_percent",
            "song_repeat_hours": "song_repeat_hours", "artist_gap": "artist_gap"},
    "dj": {"enabled": "dj_enabled", "voice": "voice", "top10_chance": "top10_chance", "chart_chance": "chart_chance",
           "other_chance": "other_chance", "max_in_a_row": "max_in_a_row", "at_least_every": "at_least_every",
           "back_announce_chance": "back_announce_chance", "station_ident_chance": "station_ident_chance",
           "talk_delay_seconds": "talk_delay_seconds", "duck_percent": "duck_percent", "voice_volume": "voice_volume"},
    "sound": {"crossfade_seconds": "crossfade_seconds", "levelling": "levelling", "target_lufs": "target_lufs"},
    "charts": {"enabled": "charts_enabled", "contact_email": "contact_email"},
    "standby": {"check_seconds": "check_seconds", "folder": "standby_folder", "message_every": "message_every"},
    "backup": {"folder": "backup_folder", "hour": "backup_hour", "keep": "backup_keep"},
    "web": {"port": "web_port"},
}


def load(path: str = SETTINGS_FILE) -> Settings:
    s = Settings()
    if not os.path.exists(path):
        return s
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for section, keys in SECTIONS.items():
        values = data.get(section, {})
        for key, attr in keys.items():
            if key in values:
                default = getattr(s, attr)
                value = values[key]
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, float):
                    value = float(value)
                elif isinstance(default, int):
                    value = int(value)
                setattr(s, attr, value)
    s.extensions = [e.lower() if e.startswith(".") else "." + e.lower() for e in s.extensions]
    s.exclude_genres = [g.lower() for g in s.exclude_genres]
    s.stream_mount = s.stream_mount.strip("/")
    if os.environ.get("DINORADIO_STANDBY"):
        s.standby_folder = os.environ["DINORADIO_STANDBY"]
    return s


USB_MOUNT = "/mnt/radio-backup"
SD_STANDBY = os.path.join(DATA_DIR, "standby")
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma")


def usable(folder: str) -> bool:
    """A folder on the USB drive only counts while the drive is plugged in."""
    if folder.startswith(USB_MOUNT + "/") and not os.path.ismount(USB_MOUNT):
        return False
    return os.path.isdir(folder)


def standby_tracks(s) -> list:
    """The standby tracks to use: the standby folder (normally on the USB drive), or the few kept
    on the SD card if that drive isn't there."""
    for folder in dict.fromkeys([s.standby_folder, SD_STANDBY]):
        if usable(folder):
            try:
                files = sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(AUDIO_EXT))
            except OSError:
                files = []
            if files:
                return files
    return []
