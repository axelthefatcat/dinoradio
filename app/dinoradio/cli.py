"""The 'dinoradio' command - check on the station and control it over SSH."""
import json
import os
import socket
import subprocess
import sys

from . import config

SOCKET_FILE = os.path.join(config.RUN_DIR, "brain.sock")

HELP = """Dino's Radio

  dinoradio status          What's on air, what's next, and how the station is doing
  dinoradio skip            Skip the current song
  dinoradio say "text"      The DJ says this over the next song to be queued
  dinoradio rescan          Look for new or changed music now (add 'full' to check every file)
  dinoradio standby on|off  Test standby mode as if the NAS had gone away
  dinoradio fill-standby N  Copy N more songs (default 25) from the NAS into the standby folder
  dinoradio backup          Back up now (it also does this every night)
  dinoradio settings        Edit the settings, then restart to use them
  dinoradio restart         Restart the station
  dinoradio update          Update to the latest version from GitHub (same as: update-radio)
  dinoradio log             Watch the station's log (Ctrl+C to stop)
  dinoradio stream          Show the stream address for players and Alexa
"""


def ask(line: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(SOCKET_FILE)
    except OSError:
        sys.exit("The radio isn't running.  Try: dinoradio restart   (then: dinoradio log)")
    s.sendall(line.encode("utf-8") + b"\n")
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8").strip()


def show_status():
    st = json.loads(ask("status"))
    print(f"{st['station']}:  {st['state']}" +
          (f"  (for {st['standby_minutes']} min - playing the standby tracks)" if st["state"] == "STANDBY" else ""))
    for label, key in (("Now", "now"), ("Next", "next")):
        i = st.get(key)
        if i:
            extra = f"  [{i['chart']}]" if i.get("chart") else ""
            print(f"  {label:5} {i['artist']} - {i['title']}{extra}")
            if i.get("dj"):
                print(f"        DJ: \"{i['dj']}\"")
    lv, ch = st["levelling"], st["charts"]
    print()
    print(f"  Music library   {st['library']}")
    print(f"  NAS             {st['nas']}")
    print(f"  Chart positions {ch['checked']} of {ch['songs']} songs looked up - {ch['top10']} top 10 hits, "
          f"{ch['hits']} chart hits ({ch['message']})")
    print(f"  Levelling       {lv['levelled']} of {lv['total']} tracks measured")
    print(f"  DJ              {st['dj']}")
    print(f"  Standby         {st['standby_tracks']} tracks, {st['standby_messages']} messages")
    print(f"  Backup          {st.get('backup', '-')}")
    print(f"  Up for          {st['uptime_minutes']} minutes")
    if st.get("history"):
        print("\n  Recently played")
        for when, artist, title, kind in st["history"][:8]:
            print(f"    {when}  {artist} - {title}" + ("  (standby)" if kind == "standby" else ""))


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    rest = " ".join(args[1:])
    if cmd in ("help", "-h", "--help"):
        print(HELP)
    elif cmd == "status":
        show_status()
    elif cmd in ("skip", "rescan", "say", "standby", "backup"):
        print(ask(f"{cmd} {rest}".strip()))
    elif cmd == "settings":
        subprocess.call(["sudo", os.environ.get("EDITOR", "nano"), config.SETTINGS_FILE])
        if input("Restart the station now to use the new settings? [Y/n] ").strip().lower() in ("", "y", "yes"):
            restart()
    elif cmd == "restart":
        restart()
    elif cmd == "update":
        os.execvp("sudo", ["sudo", "/usr/local/bin/update-radio"] + args[1:])
    elif cmd == "log":
        os.execvp("journalctl", ["journalctl", "-f", "-n", "50", "-u", "dinoradio"])
    elif cmd == "fill-standby":
        n = rest.strip() or "25"
        subprocess.call(["sudo", "-u", "dinoradio", sys.executable, "-m", "dinoradio.brain", "--fill-standby", n],
                        cwd=config.APP_DIR)
    elif cmd == "stream":
        tail = json.loads(ask("status"))["stream"].split(":", 2)[-1]     # "8000/radio-xxxx"
        host = socket.gethostname()
        ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
        print(f"On your network:  http://{ip[0] if ip else host}:{tail}")
        print(f"               or http://{host}.local:{tail}")
        print("For Alexa, point your HTTPS reverse proxy at that address.")
    else:
        print(HELP)
        sys.exit(1)


def restart():
    subprocess.call(["sudo", "systemctl", "restart", "dinoradio"])
    print("Restarted. Give it a minute, then: dinoradio status")


if __name__ == "__main__":
    main()
