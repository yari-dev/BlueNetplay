#!/bin/bash
OUT="${1:-bluenetplay-check}"
mkdir -p "$OUT"
exec > >(tee "$OUT/report.txt") 2>&1
section() { echo; echo "=== $*"; }

section "System"
tr -d '\0' < /proc/device-tree/model; echo
uname -r
grep PRETTY_NAME /etc/os-release
free -m | head -2
command -v vcgencmd >/dev/null && vcgencmd measure_temp && vcgencmd get_throttled

section "USB"
lsusb
lsusb -t

section "Driver"
dkms status 2>/dev/null | grep -i gvusb2 || echo "gvusb2 not in dkms"
lsmod | grep -E '^gvusb2|^videodev|^snd_pcm ' || true
modinfo -F version gvusb2 2>/dev/null | sed 's/^/gvusb2 version /'
sudo dmesg | grep -iE 'gvusb2|gv-usb2|04bb:0532' | tail -40

DEV=""
for n in /sys/class/video4linux/video*; do
  [ "$(cat "$n/name" 2>/dev/null)" = gvusb2 ] && DEV="/dev/$(basename "$n")"
done
section "V4L2 ($DEV)"
if [ -z "$DEV" ]; then
  echo "No gvusb2 video device. Is the GV-USB2 plugged in and the driver loaded?"
else
  v4l2-ctl -d "$DEV" --info --list-formats-ext --get-fmt-video --get-standard --get-input
  v4l2-ctl -d "$DEV" --list-inputs
  v4l2-ctl -d "$DEV" --get-parm
  v4l2-ctl -d "$DEV" --list-ctrls
fi

section "ALSA"
arecord -l

if [ -n "$DEV" ]; then
  for mode in fields:720x240:60000/1001 interlaced:720x480:30000/1001; do
    IFS=: read -r name size rate <<< "$mode"
    section "Capture: $name ($size), 5 s"
    timeout 20 ffmpeg -hide_banner -nostdin -f v4l2 -input_format uyvy422 -framerate "$rate" -video_size "$size" \
      -i "$DEV" -t 5 -f null - 2> "$OUT/ffmpeg-$name.log"
    grep -m1 'Stream #0:0: Video' "$OUT/ffmpeg-$name.log"
    tr '\r' '\n' < "$OUT/ffmpeg-$name.log" | grep 'frame=' | tail -1
    grep -iE 'error|warn|invalid|dequeued' "$OUT/ffmpeg-$name.log" | head -5
    vf="select=gte(n\,120)"
    [ "$name" = fields ] && vf="$vf,scale=720:480:flags=neighbor"
    timeout 20 ffmpeg -hide_banner -nostdin -loglevel error -f v4l2 -input_format uyvy422 \
      -framerate "$rate" -video_size "$size" -i "$DEV" -vf "$vf" -frames:v 1 -y "$OUT/snapshot-$name.png"
    ls -l "$OUT/snapshot-$name.png" 2>/dev/null
  done
fi

section "Audio: 3 s from hw:CARD=GVUSB2"
if timeout 10 arecord -q -D hw:CARD=GVUSB2,DEV=0 -f S16_LE -r 48000 -c 2 -d 3 "$OUT/audio.wav"; then
  ls -l "$OUT/audio.wav"
  ffmpeg -hide_banner -nostdin -i "$OUT/audio.wav" -af astats=measure_overall=Peak_level+RMS_level:measure_perchannel=none \
    -f null - 2>&1 | grep -E 'Peak level|RMS level'
else
  echo "arecord failed"
fi

section "CPU: x264 for the relay (720x480 at 59.94, 10 s of video)"
ffmpeg -hide_banner -nostdin -f lavfi -i testsrc2=size=720x480:rate=60000/1001 -t 10 \
  -pix_fmt yuv420p -c:v libx264 -preset ultrafast -tune zerolatency -b:v 4M -f null - 2>&1 \
  | tr '\r' '\n' | grep 'speed=' | tail -1
echo "(speed above 1.5x leaves room for capture, JPEG and Python)"

section "Kernel log after the tests"
sudo dmesg | grep -iE 'gvusb2|gv-usb2|urb|xhci|usb [0-9.-]+:' | tail -20

echo
echo "Done: $OUT/ (report.txt, snapshot-*.png, audio.wav)"
