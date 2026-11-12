#!/bin/bash
# Dino's Radio - installer for a Raspberry Pi (Raspberry Pi OS Lite).
#   Run from this folder:   sudo bash install.sh
#   Change NAS / station answers later:   sudo bash install.sh --reconfigure
# Safe to run again - it updates the program and keeps your settings, database and standby tracks.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/dinoradio
ETC=/etc/dinoradio
DATA=/var/lib/dinoradio
MNT=/mnt/music
USER_NAME=dinoradio
PIPER_RELEASE="https://github.com/rhasspy/piper/releases/download/2023.11.14-2"
VOICES_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
VOICE=en_GB-cori-medium          # the default; the settings file's [dj] voice wins once it exists

say()  { printf '\n\033[1;35m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mStopped: %s\033[0m\n' "$*"; exit 1; }
ask()  { local q="$1" def="$2" ans; read -r -p "   $q [$def]: " ans </dev/tty; echo "${ans:-$def}"; }
secret() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-16}" || true; }

[ "$(id -u)" -eq 0 ] || die "please run with sudo:   sudo bash install.sh"
RECONFIGURE=0; RESTORE=0; USB=0
for arg in "$@"; do
  case "$arg" in
    --reconfigure) RECONFIGURE=1 ;;
    --restore)     RESTORE=1 ;;
    --usb)         USB=1 ;;
  esac
done
BACKUP_MNT=/mnt/radio-backup
BACKUP_DIR=$BACKUP_MNT/dinoradio

echo
echo "  ╔══════════════════════════════════════════╗"
echo "  ║        Dino's Radio  -  installer        ║"
echo "  ╚══════════════════════════════════════════╝"

# ---------------------------------------------------------------- questions
if [ ! -f "$ETC/settings.toml" ] || [ $RECONFIGURE -eq 1 ]; then
  say "A few questions (press Enter to accept the suggestion in brackets)"
  STATION=$(ask "Station name" "Dino's Radio")
  NAS=""
  while [ -z "$NAS" ]; do NAS=$(ask "NAS address (e.g. 192.168.1.20)" ""); done
  SHARE=$(ask "Shared folder on the NAS" "music")
  SUB=$(ask "Folder inside it with the music (blank = all of it)" "")
  NASUSER=$(ask "NAS user name (a read-only user is best)" "radio")
  read -r -s -p "   NAS password for $NASUSER: " NASPASS </dev/tty; echo
  SMBVER=$(ask "SMB version (3.0 suits Synology; try 2.1 or 1.0 for old NASes)" "3.0")
  EMAIL=$(ask "Contact email Wikipedia sees with chart lookups (blank = none)" "")
  WEATHER=$(ask "Town for the DJ's hourly weather, e.g. York (blank = no weather)" "")
  CONFIGURE=1
else
  CONFIGURE=0
  ok "Keeping your existing settings (use --reconfigure to change the NAS or station name)"
fi

# ---------------------------------------------------------------- packages
say "Installing the radio software (this can take 10-20 minutes on a Pi 2)"
ICE_SOURCE=$(secret 16); ICE_ADMIN=$(secret 12)
if [ -f "$ETC/settings.toml" ] && [ $CONFIGURE -eq 0 ]; then
  ICE_SOURCE=$(sed -n 's/^icecast_password *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
fi
echo "icecast2 icecast2/icecast-setup boolean true"           | debconf-set-selections
echo "icecast2 icecast2/hostname string $(hostname)"           | debconf-set-selections
echo "icecast2 icecast2/sourcepassword password $ICE_SOURCE"   | debconf-set-selections
echo "icecast2 icecast2/relaypassword password $(secret 16)"   | debconf-set-selections
echo "icecast2 icecast2/adminpassword password $ICE_ADMIN"     | debconf-set-selections
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends icecast2 ffmpeg python3 python3-mutagen python3-numpy \
    cifs-utils curl ca-certificates sqlite3 nano >/dev/null
# An earlier version used Liquidsoap, which crashes on the Pi 2 - take it away if it's there
if dpkg -s liquidsoap >/dev/null 2>&1; then
  systemctl disable --now dinoradio-liquidsoap >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/dinoradio-liquidsoap.service
  apt-get purge -y -q liquidsoap >/dev/null && apt-get autoremove -y -q >/dev/null
fi
ENCODERS=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
[[ "$ENCODERS" == *libmp3lame* ]] || die "this FFmpeg can't make MP3 (no libmp3lame)"
ok "Icecast, FFmpeg $(ffmpeg -version | head -1 | awk '{print $3}'), Python $(python3 -V | awk '{print $2}') with NumPy"

# ---------------------------------------------------------------- user and folders
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$APP" "$ETC" "$DATA/standby" "$DATA/voice" "$MNT"
rm -rf "$APP/dinoradio"
cp -r "$HERE/app/dinoradio" "$APP/"
find "$APP/dinoradio" -name '__pycache__' -prune -exec rm -rf {} +
chown -R "$USER_NAME:$USER_NAME" "$DATA"
cat > /usr/local/bin/dinoradio <<'EOF'
#!/bin/sh
cd /opt/dinoradio && exec python3 -m dinoradio.cli "$@"
EOF
chmod 755 /usr/local/bin/dinoradio
cat > /usr/local/bin/update-radio <<'UPDATER'
#!/bin/bash
# Updates Dino's Radio to the latest version on GitHub. Settings, charts, levels and backups are kept.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading the latest Dino's Radio from GitHub..."
curl -fsSL https://github.com/axelthefatcat/dinoradio/archive/refs/heads/main.tar.gz | tar xz -C "$TMP"
cd "$TMP/dinoradio-main"
bash install.sh "$@"
UPDATER
chmod 755 /usr/local/bin/update-radio
ok "Program installed in $APP  (update it any time with: update-radio)"

# ---------------------------------------------------------------- the DJ voice
say "The DJ voice (Piper)"
case "$(uname -m)" in
  armv7l|armv6l) PIPER_ARCH=armv7l ;;
  aarch64)       PIPER_ARCH=aarch64 ;;
  x86_64)        PIPER_ARCH=x86_64 ;;
  *)             PIPER_ARCH="" ;;
esac
if [ -z "$PIPER_ARCH" ]; then
  warn "No Piper voice engine for this processor ($(uname -m)) - the radio will run without the DJ"
else
  if [ ! -x "$APP/piper/piper" ]; then
    curl -fsSL "$PIPER_RELEASE/piper_linux_${PIPER_ARCH}.tar.gz" -o /tmp/piper.tgz || die "couldn't download Piper from GitHub"
    tar -xzf /tmp/piper.tgz -C "$APP" && rm -f /tmp/piper.tgz
  fi
  mkdir -p "$APP/voices"
  if [ -f "$ETC/settings.toml" ]; then
    SET_VOICE=$(sed -n '/^\[dj\]/,/^\[/ s/^voice *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
    [ -n "$SET_VOICE" ] && VOICE="$SET_VOICE"
  fi
  # The chosen voice, plus the standard one as a fall-back if the chosen one is too slow for this Pi.
  # en_GB-cori-high  ->  en/en_GB/cori/high/en_GB-cori-high.onnx
  for V in "$VOICE" en_GB-cori-medium; do
    if [[ "$V" =~ ^(([a-z]+)_[A-Z]+)-([a-z0-9_]+)-(x_low|low|medium|high)$ ]]; then
      VOICE_PATH="${BASH_REMATCH[2]}/${BASH_REMATCH[1]}/${BASH_REMATCH[3]}/${BASH_REMATCH[4]}"
    else
      die "the voice \"$V\" in the settings isn't a Piper voice name (like en_GB-cori-high)"
    fi
    for ext in onnx onnx.json; do
      if [ ! -s "$APP/voices/$V.$ext" ]; then
        curl -fsSL "$VOICES_URL/$VOICE_PATH/$V.$ext?download=true" -o "$APP/voices/$V.$ext.part" \
          || die "couldn't download the $V voice from huggingface.co"
        mv "$APP/voices/$V.$ext.part" "$APP/voices/$V.$ext"
      fi
    done
  done
  if echo "Testing, testing." | LD_LIBRARY_PATH="$APP/piper" "$APP/piper/piper" --model "$APP/voices/$VOICE.onnx" \
       --output_file /tmp/dj-test.wav >/dev/null 2>&1 && [ -s /tmp/dj-test.wav ]; then
    ok "DJ voice works ($VOICE)"
  else
    warn "The DJ voice didn't work on this Pi - the radio will play without the DJ. Send the output of:"
    warn "  echo hello | $APP/piper/piper --model $APP/voices/$VOICE.onnx --output_file /tmp/t.wav"
  fi
  rm -f /tmp/dj-test.wav
fi

# ---------------------------------------------------------------- the NAS
if [ $CONFIGURE -eq 1 ]; then
  say "Connecting to the music on the NAS"
  umask 077
  printf 'username=%s\npassword=%s\n' "$NASUSER" "$NASPASS" > "$ETC/nas.credentials"
  umask 022
  chmod 600 "$ETC/nas.credentials"
  UIDN=$(id -u "$USER_NAME"); GIDN=$(id -g "$USER_NAME")
  OPTS="credentials=$ETC/nas.credentials,ro,vers=$SMBVER,iocharset=utf8,uid=$UIDN,gid=$GIDN,file_mode=0444,dir_mode=0555,soft,echo_interval=10,_netdev,nofail,x-systemd.mount-timeout=30"
  sed -i -e "\#[[:space:]]${MNT}[[:space:]]#d" -e "/^# Dino's Radio music share/d" /etc/fstab
  printf "# Dino's Radio music share\n//%s/%s %s cifs %s 0 0\n" "$NAS" "$SHARE" "$MNT" "$OPTS" >> /etc/fstab
  systemctl daemon-reload
fi
umount -l "$MNT" 2>/dev/null || true
if mount "$MNT" 2>/tmp/mount.err; then
  ok "NAS share mounted at $MNT"
else
  warn "Couldn't mount the NAS share: $(cat /tmp/mount.err)"
  warn "Check the address, share name, user and password, then run: sudo bash install.sh --reconfigure"
fi

cat > /usr/local/sbin/dinoradio-remount <<EOF
#!/bin/sh
# Reconnects the music share after the NAS has been away (called by the radio when it can't read the music)
umount -l -f $MNT 2>/dev/null
exec mount $MNT
EOF
chmod 755 /usr/local/sbin/dinoradio-remount
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/dinoradio-remount" > /etc/sudoers.d/dinoradio
chmod 440 /etc/sudoers.d/dinoradio

# ---------------------------------------------------------------- backup drive (USB)
BACKUP_SETTING=""
STANDBY_SETTING="$DATA/standby"
USB_STANDBY=0
if [ -f "$ETC/settings.toml" ] && [ $CONFIGURE -eq 0 ] && [ $USB -eq 0 ]; then
  BACKUP_SETTING=$(sed -n '/^\[backup\]/,/^\[/ s/^folder *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
fi
if [ $CONFIGURE -eq 1 ] || [ $USB -eq 1 ] || ! grep -q '^\[backup\]' "$ETC/settings.toml" 2>/dev/null; then
  say "Backup drive"
  # USB drives with a filesystem on them: "path|fstype|size|label|uuid"
  mapfile -t DRIVES < <(lsblk -J -o PATH,TRAN,FSTYPE,SIZE,LABEL,UUID,PARTUUID,TYPE,MOUNTPOINT 2>/dev/null | python3 -c '
import json, sys
# USB drives: marked "usb", or any /dev/sd* disk (the SD card is mmcblk*); never the system disk
def walk(devs, usb=False):
    for d in devs:
        u = usb or d.get("tran") == "usb" or (d.get("path") or "").startswith("/dev/sd")
        ident = d.get("uuid") or d.get("partuuid")
        if u and d.get("fstype") and d.get("fstype") != "swap" and ident and d.get("type") in ("part", "disk") \
                and d.get("mountpoint") not in ("/", "/boot", "/boot/firmware"):
            idk = "UUID=" + d["uuid"] if d.get("uuid") else "PARTUUID=" + d["partuuid"]
            print("|".join(str(x or "") for x in (d.get("path"), d.get("fstype"), d.get("size"), d.get("label"), idk)))
        walk(d.get("children") or [], u)
walk(json.load(sys.stdin).get("blockdevices", []))' || true)
  if [ ${#DRIVES[@]} -eq 0 ]; then
    warn "No USB drive found - backups will be kept on the SD card for now."
    lsblk -o NAME,TRAN,FSTYPE,SIZE,UUID,MOUNTPOINT 2>/dev/null | sed 's/^/      /' || true
    warn "Plug one in later and run:  sudo bash install.sh --usb"
  else
    echo "   USB drives found:"
    i=0; for d in "${DRIVES[@]}"; do i=$((i+1)); IFS='|' read -r p f z l _ <<<"$d"; echo "     $i) ${l:-unnamed}  $z  ($f, $p)"; done
    PICK=$(ask "Which one for the nightly backups? (0 = none)" "1")
    if [[ "$PICK" =~ ^[0-9]+$ ]] && [ "$PICK" -ge 1 ] && [ "$PICK" -le ${#DRIVES[@]} ]; then
      IFS='|' read -r DEV FST _ LBL DUUID <<<"${DRIVES[$((PICK-1))]}"
      UIDN=$(id -u "$USER_NAME"); GIDN=$(id -g "$USER_NAME")
      case "$FST" in
        vfat|exfat) BOPTS="uid=$UIDN,gid=$GIDN,umask=0022" ;;
        ntfs)       FST=ntfs3; BOPTS="uid=$UIDN,gid=$GIDN,umask=0022" ;;
        *)          BOPTS="defaults" ;;
      esac
      mkdir -p "$BACKUP_MNT"
      umount "$DEV" 2>/dev/null || true
      sed -i -e "\#[[:space:]]${BACKUP_MNT}[[:space:]]#d" -e "/^# Dino's Radio backup drive/d" /etc/fstab
      printf "# Dino's Radio backup drive\n%s %s %s %s,nofail,x-systemd.device-timeout=10s 0 0\n" \
        "$DUUID" "$BACKUP_MNT" "$FST" "$BOPTS" >> /etc/fstab
      systemctl daemon-reload
      if mount "$BACKUP_MNT" 2>/tmp/usb.err; then
        mkdir -p "$BACKUP_DIR" && chown "$USER_NAME:$USER_NAME" "$BACKUP_DIR" 2>/dev/null || true
        BACKUP_SETTING="$BACKUP_DIR"
        STANDBY_SETTING="$BACKUP_DIR/standby"
        mkdir -p "$STANDBY_SETTING"
        cp -n "$DATA"/standby/* "$STANDBY_SETTING/" 2>/dev/null || true
        chown -R "$USER_NAME:$USER_NAME" "$BACKUP_DIR" 2>/dev/null || true
        USB_STANDBY=1
        ok "Backups and standby tracks will go on the USB drive ${LBL:-$DEV} (in its dinoradio folder)"
      else
        warn "Couldn't use that drive: $(cat /tmp/usb.err) - backups stay on the SD card"
      fi
    fi
  fi
else
  mountpoint -q "$BACKUP_MNT" 2>/dev/null || mount "$BACKUP_MNT" 2>/dev/null || true
fi

# ---------------------------------------------------------------- restore
RESTORED=0
LATEST=""
for dir in "$BACKUP_DIR" "$DATA/backups"; do
  if [ -d "$dir" ]; then
    f=$(ls -1 "$dir"/dinoradio-backup-*.tar.gz 2>/dev/null | tail -1 || true)
    if [ -n "$f" ]; then LATEST="$f"; break; fi
  fi
done
if [ -n "$LATEST" ] && { [ $RESTORE -eq 1 ] || [ ! -s "$DATA/radio.db" ]; }; then
  say "Restore"
  echo "   Found a backup: $(basename "$LATEST")"
  ANSWER=$(ask "Restore it? (track list, chart positions, volume levels, settings, standby tracks)" "Y")
  if [[ ! "$ANSWER" =~ ^[Nn] ]]; then
    systemctl stop dinoradio 2>/dev/null || true
    TMPR=$(mktemp -d)
    tar -xzf "$LATEST" -C "$TMPR"
    rm -f "$DATA/radio.db" "$DATA/radio.db-wal" "$DATA/radio.db-shm"
    cp "$TMPR/radio.db" "$DATA/radio.db"
    [ -d "$TMPR/voice" ] && cp "$TMPR"/voice/* "$DATA/voice/" 2>/dev/null || true
    [ -f "$TMPR/network.conf" ] && cp "$TMPR/network.conf" /tmp/dinoradio-network.conf
    [ -d "$(dirname "$LATEST")/standby" ] && cp -n "$(dirname "$LATEST")"/standby/* "$DATA/standby/" 2>/dev/null || true
    if [ -f "$TMPR/settings.toml" ]; then
      [ -f "$ETC/settings.toml" ] && cp "$ETC/settings.toml" "$ETC/settings.toml.before-restore"
      cp "$TMPR/settings.toml" "$ETC/settings.toml"
      ICE_SOURCE=$(sed -n 's/^icecast_password *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
      if [ -n "$BACKUP_SETTING" ] && grep -q '^\[backup\]' "$ETC/settings.toml"; then
        sed -i "/^\[backup\]/,/^\[/ s|^folder *=.*|folder = \"$BACKUP_SETTING\"   # nightly backups (blank = on the SD card only)|" "$ETC/settings.toml"
      fi
    fi
    rm -rf "$TMPR"
    chown -R "$USER_NAME:$USER_NAME" "$DATA"
    RESTORED=1
    ok "Restored - the radio picks up where it left off (same stream address, no full rescan)"
  fi
fi

# ---------------------------------------------------------------- settings
if [ $CONFIGURE -eq 1 ] && [ $RESTORED -eq 0 ]; then
  MOUNT_NAME="radio-$(secret 10 | tr 'A-Z' 'a-z')"
  if [ -f "$ETC/settings.toml" ]; then
    OLD_MOUNT=$(sed -n 's/^stream_mount *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
    [ -n "$OLD_MOUNT" ] && MOUNT_NAME="$OLD_MOUNT"
    cp "$ETC/settings.toml" "$ETC/settings.toml.bak"
  fi
  FOLDER="$MNT"; [ -n "$SUB" ] && FOLDER="$MNT/$SUB"
  STATION_TOML=${STATION//\"/\'}
  sed -e "s|@STATION@|$STATION_TOML|" -e "s|@MOUNT@|$MOUNT_NAME|" -e "s|@PASSWORD@|$ICE_SOURCE|" \
      -e "s|@FOLDER@|$FOLDER|" -e "s|@EMAIL@|$EMAIL|" -e "s|@BACKUP@|$BACKUP_SETTING|" -e "s|@STANDBY@|$STANDBY_SETTING|" -e "s|@WEATHER@|${WEATHER//|/}|" \
      "$HERE/settings.template.toml" > "$ETC/settings.toml"
  ok "Settings written to $ETC/settings.toml"
elif ! grep -q '^\[backup\]' "$ETC/settings.toml"; then
  { echo; sed -n '/^\[backup\]/,/^keep/p' "$HERE/settings.template.toml" | sed "s|@BACKUP@|$BACKUP_SETTING|"; } >> "$ETC/settings.toml"
  ok "Backup settings added"
fi
if ! grep -q '^\[web\]' "$ETC/settings.toml"; then
  { echo; sed -n '/^\[web\]/,$p' "$HERE/settings.template.toml"; } >> "$ETC/settings.toml"
fi
if ! grep -q '^\[weather\]' "$ETC/settings.toml"; then
  say "Weather"
  WEATHER=$(ask "Town for the DJ's hourly weather, e.g. York (blank = no weather)" "")
  { echo; sed -n '/^\[weather\]/,/^to_hour/p' "$HERE/settings.template.toml" | sed "s|@WEATHER@|${WEATHER//|/}|"; } \
    >> "$ETC/settings.toml"
  ok "Weather: ${WEATHER:-off}"
fi
if [ $USB -eq 1 ] && [ $RESTORED -eq 0 ] && [ $CONFIGURE -eq 0 ]; then
  sed -i "/^\[backup\]/,/^\[/ s|^folder *=.*|folder = \"$BACKUP_SETTING\"   # nightly backups (blank = on the SD card only)|" "$ETC/settings.toml"
fi
if [ $USB_STANDBY -eq 1 ] && [ $RESTORED -eq 0 ]; then
  sed -i "/^\[standby\]/,/^\[/ s|^folder *=.*|folder = \"$STANDBY_SETTING\"   # the tracks it plays when the NAS isn't there|" "$ETC/settings.toml"
  grep -q '^message_every' "$ETC/settings.toml" || \
    sed -i "/^\[standby\]/a message_every = 3                 # the \"please hold\" message every this many standby tracks" "$ETC/settings.toml"
fi
chown root:"$USER_NAME" "$ETC/settings.toml"; chmod 640 "$ETC/settings.toml"

# ---------------------------------------------------------------- Icecast
say "Icecast (the stream server)"
ICE=/etc/icecast2/icecast.xml
sed -i -e "s|<source-password>[^<]*</source-password>|<source-password>$ICE_SOURCE</source-password>|" \
       -e "s|<hostname>[^<]*</hostname>|<hostname>$(hostname)</hostname>|" "$ICE"
if [ $CONFIGURE -eq 1 ]; then
  sed -i "s|<admin-password>[^<]*</admin-password>|<admin-password>$ICE_ADMIN</admin-password>|" "$ICE"
  echo "Icecast admin page: http://$(hostname).local:8000/admin/   user: admin   password: $ICE_ADMIN" > "$ETC/icecast-admin.txt"
  chmod 600 "$ETC/icecast-admin.txt"
fi
# Keep the stream's address off Icecast's public status pages (it works like a password)
S_MOUNT_NAME=$(sed -n 's/^stream_mount *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
python3 - "$ICE" "$S_MOUNT_NAME" <<'PYEOF'
import re, sys
path, mount = sys.argv[1], sys.argv[2]
xml = open(path).read()
xml = re.sub(r"\s*<!-- dinoradio -->.*?<!-- /dinoradio -->", "", xml, flags=re.S)
block = (f"\n    <!-- dinoradio -->\n    <mount type=\"normal\">\n        <mount-name>/{mount}</mount-name>\n"
         f"        <hidden>1</hidden>\n        <public>0</public>\n    </mount>\n    <!-- /dinoradio -->\n")
xml = xml.replace("</icecast>", block + "</icecast>", 1)
open(path, "w").write(xml)
PYEOF
[ -f /etc/default/icecast2 ] && sed -i 's/^ENABLE=.*/ENABLE=true/' /etc/default/icecast2
systemctl enable icecast2 >/dev/null 2>&1 || true
systemctl restart icecast2
ok "Icecast running on port 8000"

# ---------------------------------------------------------------- services
say "Starting Dino's Radio"
cp "$HERE/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload
if ! sudo -u "$USER_NAME" sh -c "cd $APP && python3 -c 'import dinoradio.brain, numpy, mutagen'" 2>/tmp/py.err; then
  cat /tmp/py.err
  die "the radio program wouldn't load (above) - please send me that message"
fi
ok "Radio program checked"

if mountpoint -q "$MNT"; then
  if [ -z "$(ls -A "$DATA/standby" 2>/dev/null)" ]; then
    echo "   Choosing 5 standby tracks for the SD card (used if the USB drive is ever missing)..."
    sudo -u "$USER_NAME" sh -c "cd $APP && DINORADIO_STANDBY=$DATA/standby python3 -m dinoradio.brain --fill-standby 5" \
      | sed 's/^/     /' || true
  fi
  if [ $USB_STANDBY -eq 1 ] && [ "$(find "$STANDBY_SETTING" -maxdepth 1 -type f | wc -l)" -lt 20 ]; then
    echo "   Copying 50 songs to the USB drive for standby (takes a few minutes)..."
    sudo -u "$USER_NAME" sh -c "cd $APP && python3 -m dinoradio.brain --fill-standby 50" | tail -1 | sed 's/^/     /' || true
  fi
fi

systemctl enable dinoradio >/dev/null 2>&1
systemctl restart dinoradio
ok "Running - and it starts by itself whenever the Pi powers up"

S_MOUNT=$(sed -n 's/^stream_mount *= *"\([^"]*\)".*/\1/p' "$ETC/settings.toml" | head -1)
IP=$(hostname -I | awk '{print $1}')

# ---------------------------------------------------------------- restore the fixed IP address
NET_CHANGE=""
if [ $RESTORED -eq 1 ] && [ -f /tmp/dinoradio-network.conf ] && command -v nmcli >/dev/null; then
  N_METHOD=$(sed -n 's/^method=//p' /tmp/dinoradio-network.conf)
  N_ADDR=$(sed -n 's/^addresses=//p' /tmp/dinoradio-network.conf)
  N_GW=$(sed -n 's/^gateway=//p' /tmp/dinoradio-network.conf)
  N_DNS=$(sed -n 's/^dns=//p' /tmp/dinoradio-network.conf)
  CONN=$(nmcli -t -f NAME,DEVICE connection show | awk -F: '$2=="eth0"{print $1; exit}')
  NOW_ADDR=$(nmcli -g ipv4.addresses connection show "$CONN" 2>/dev/null || true)
  if [ "$N_METHOD" = "manual" ] && [ -n "$N_ADDR" ] && [ -n "$CONN" ] && [ "$NOW_ADDR" != "$N_ADDR" ]; then
    say "Fixed IP address"
    echo "   The backup had this Pi on ${N_ADDR%/*} (gateway $N_GW)."
    ANSWER=$(ask "Put that address back? (your PuTTY session will disconnect - reconnect to ${N_ADDR%/*})" "Y")
    if [[ ! "$ANSWER" =~ ^[Nn] ]]; then
      NET_CHANGE="$CONN"
      IP="${N_ADDR%/*}"
    fi
  fi
fi
say "All done"
cat <<EOF
   Your stream:      http://$IP:8000/$S_MOUNT
              or     http://$(hostname).local:8000/$S_MOUNT

   The first time, it reads the whole music library (allow up to an hour for a big one),
   then the chart positions and volume levels fill in quietly in the background.

   Status page:      http://$IP:8080   (live status, a player and the log)
   Check on it:      dinoradio status
   Watch it work:    dinoradio log
   All commands:     dinoradio help
EOF
[ -f "$ETC/icecast-admin.txt" ] && echo "   $(cat "$ETC/icecast-admin.txt")"
echo

if [ -n "$NET_CHANGE" ]; then
  echo "   Switching to ${N_ADDR%/*} in a few seconds - reconnect PuTTY to that address."
  nmcli connection modify "$NET_CHANGE" ipv4.method manual ipv4.addresses "$N_ADDR" \
      ipv4.gateway "$N_GW" ipv4.dns "${N_DNS:-$N_GW}"
  sleep 2
  nmcli connection up "$NET_CHANGE" >/dev/null 2>&1 || true
fi
rm -f /tmp/dinoradio-network.conf
