#!/bin/bash
set -euo pipefail

DRIVER_VERSION=2.0
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-}"
WANT_SERVICE=0
DONGLE=04b4:f901
for arg in "$@"; do
  case "$arg" in
    --service) WANT_SERVICE=1 ;;
    --dongle=*) DONGLE="${arg#--dongle=}" ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done
DONGLE="$(echo "$DONGLE" | tr 'A-F' 'a-f')"
if ! [[ "$DONGLE" =~ ^[0-9a-f]{4}:[0-9a-f]{4}$ ]]; then
  echo "--dongle needs the USB ID as VID:PID, e.g. --dongle=04b4:f901 (see lsusb)"
  exit 2
fi
DONGLE_VID="${DONGLE%%:*}"
DONGLE_PID="${DONGLE##*:}"

if [ "$(id -u)" -ne 0 ] || [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
  echo "Run this with sudo from your normal user: sudo bash linux/install.sh"
  exit 1
fi
step() { echo; echo "==> $*"; }

if ! sudo -u "$USER_NAME" test -w "$REPO"; then
  echo "$REPO is not writable by $USER_NAME. Fix it with:"
  echo "  sudo chown -R $USER_NAME: $REPO && chmod -R u+w $REPO"
  echo "then run this script again."
  exit 1
fi

step "Installing packages (ffmpeg, DKMS, kernel headers, Python venv)"
KREL="$(uname -r)"
headers=("linux-headers-$KREL")
case "$KREL" in
  *-rpi-2712) headers+=(linux-headers-rpi-2712) ;;
  *-rpi-v8)   headers+=(linux-headers-rpi-v8) ;;
esac
apt-get update
apt-get install -y ffmpeg dkms python3-venv libusb-1.0-0 v4l-utils alsa-utils "${headers[@]}"

step "Building and installing the GV-USB2 driver (gvusb2 $DRIVER_VERSION)"
SRC="/usr/src/gvusb2-$DRIVER_VERSION"
if dkms status -m gvusb2 -v "$DRIVER_VERSION" | grep -q gvusb2; then
  dkms remove -m gvusb2 -v "$DRIVER_VERSION" --all || true
fi
rm -rf "$SRC"
mkdir -p "$SRC"
cp "$REPO"/drivers/{*.c,*.h,Makefile,dkms.conf} "$SRC"/
dkms add -m gvusb2 -v "$DRIVER_VERSION"
dkms build -m gvusb2 -v "$DRIVER_VERSION"
dkms install -m gvusb2 -v "$DRIVER_VERSION" --force
modprobe -r gvusb2 2>/dev/null || true
modprobe gvusb2

step "Installing udev rules for the Bluetooth dongle $DONGLE"
sed -e "s/04b4/$DONGLE_VID/g" -e "s/f901/$DONGLE_PID/g" "$REPO/linux/99-bluenetplay.rules"   > /etc/udev/rules.d/99-bluenetplay.rules
chmod 0644 /etc/udev/rules.d/99-bluenetplay.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor="$DONGLE_VID"
for g in plugdev video audio; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME"
done

step "Creating the Python environment in $REPO/.venv"
sudo -u "$USER_NAME" python3 -m venv "$REPO/.venv"
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install --upgrade pip >/dev/null
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -r "$REPO/requirements.txt"

if [ "$WANT_SERVICE" = 1 ]; then
  step "Installing the bluenetplay service"
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$REPO|g" "$REPO/linux/bluenetplay.service" \
    > /etc/systemd/system/bluenetplay.service
  if [ ! -f /etc/default/bluenetplay ]; then
    echo 'BLUENETPLAY_ARGS="--players 2 --host 0.0.0.0"' > /etc/default/bluenetplay
  fi
  systemctl daemon-reload
  systemctl enable --now bluenetplay
fi

step "Check"
if lsusb -d 04bb:0532 | grep -q .; then
  echo "GV-USB2:    plugged in"
  for n in /sys/class/video4linux/video*; do
    [ "$(cat "$n/name" 2>/dev/null)" = gvusb2 ] && echo "  video:    /dev/$(basename "$n")"
  done
  grep -q GVUSB2 /proc/asound/cards && echo "  audio:    ALSA card GVUSB2"
else
  echo "GV-USB2:    not plugged in (plug it in; the driver loads by itself)"
fi
if lsusb -d "$DONGLE" | grep -q .; then
  echo "Dongle:     $DONGLE plugged in"
else
  echo "Dongle:     $DONGLE not plugged in"
fi
echo
echo "Done. Log out and back in once (new group memberships), then run:"
echo "  cd $REPO && .venv/bin/python bluenetplay.py --players 2 --host 0.0.0.0"
[ "$WANT_SERVICE" = 1 ] && echo "The service is running: systemctl status bluenetplay / journalctl -u bluenetplay -f"
exit 0
