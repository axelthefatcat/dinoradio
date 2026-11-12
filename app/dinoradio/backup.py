"""Nightly backup of everything the radio has learnt, so a rebuilt Pi doesn't start from scratch:
the database (track list, chart positions, volume levels, play history), the settings, and the
standby tracks and messages. Restore with:  sudo bash install.sh --restore"""
import glob
import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time

from . import config
from .db import connect

log = logging.getLogger("backup")
PREFIX = "dinoradio-backup-"


USB_MOUNT = config.USB_MOUNT


def folder_for(s) -> str:
    return s.backup_folder or os.path.join(config.DATA_DIR, "backups")


def drive_missing(s) -> bool:
    """True if the backup belongs on the USB drive and the drive isn't plugged in / mounted
    (so we don't quietly fill up the SD card instead)."""
    folder = folder_for(s)
    return folder.startswith(USB_MOUNT + "/") and not os.path.ismount(USB_MOUNT)


def make(s) -> str:
    if drive_missing(s):
        raise OSError("the backup USB drive isn't plugged in")
    dest = folder_for(s)
    os.makedirs(dest, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H%M")
    out = os.path.join(dest, f"{PREFIX}{stamp}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        # a consistent copy of the database, even while the radio is using it
        db_copy = os.path.join(tmp, "radio.db")
        target = sqlite3.connect(db_copy)
        connect().backup(target)
        target.close()
        part = out + ".part"
        with tarfile.open(part, "w:gz") as tar:
            tar.add(db_copy, arcname="radio.db")
            if os.access(config.SETTINGS_FILE, os.R_OK):
                tar.add(config.SETTINGS_FILE, arcname="settings.toml")
            net = _network()
            if net:
                net_file = os.path.join(tmp, "network.conf")
                with open(net_file, "w") as f:
                    f.write(net)
                tar.add(net_file, arcname="network.conf")
            for f in glob.glob(os.path.join(config.DATA_DIR, "voice", "standby-*")):
                tar.add(f, arcname=os.path.join("voice", os.path.basename(f)))
        os.replace(part, out)
    if not s.standby_folder.startswith(USB_MOUNT + "/"):      # tracks on the drive itself needn't be copied
        _mirror_standby(s, os.path.join(dest, "standby"))
    for old in sorted(glob.glob(os.path.join(dest, PREFIX + "*.tar.gz")))[:-max(1, s.backup_keep)]:
        try:
            os.remove(old)
        except OSError:
            pass
    log.info("backup saved: %s", out)
    return out


def _network() -> str:
    """The wired network settings (fixed IP address etc.), so a restore can put them back."""
    try:
        rows = subprocess.run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
                              capture_output=True, text=True, timeout=10).stdout.splitlines()
        name = next((r.rsplit(":", 1)[0] for r in rows if r.endswith(":eth0")), None)
        if not name:
            return ""
        out = subprocess.run(["nmcli", "-g", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                              "connection", "show", name], capture_output=True, text=True, timeout=10).stdout
        method, addresses, gateway, dns = (out.splitlines() + ["", "", "", ""])[:4]
        return f"method={method}\naddresses={addresses}\ngateway={gateway}\ndns={dns}\n"
    except (OSError, subprocess.SubprocessError):
        return ""


def _mirror_standby(s, dest):
    """The standby tracks are big, so they're copied once (and kept in step) rather than in every backup."""
    os.makedirs(dest, exist_ok=True)
    src = s.standby_folder
    names = set(os.listdir(src)) if os.path.isdir(src) else set()
    for name in names:
        a, b = os.path.join(src, name), os.path.join(dest, name)
        if os.path.isfile(a) and (not os.path.exists(b) or os.path.getsize(a) != os.path.getsize(b)):
            shutil.copyfile(a, b)
    for name in set(os.listdir(dest)) - names:
        try:
            os.remove(os.path.join(dest, name))
        except OSError:
            pass


def latest(s):
    files = sorted(glob.glob(os.path.join(folder_for(s), PREFIX + "*.tar.gz")))
    return files[-1] if files else None
