# BlueNetplay

Netplay on real retro consoles.

BlueNetplay turns a Windows PC or a Raspberry Pi next to your console into
Bluetooth gamepads for a [BlueRetro](https://github.com/darthcloud/BlueRetro)
adapter, captures the console's picture and sound, and puts both in a web page.
Play from any browser at home, or invite friends over the internet: each of
them gets their own controller port, on the real hardware.

Tested with a Nintendo 64: two players over the internet at 60 fps, from
Chrome, Edge and Safari (iPhone), hosted on Windows and on a Raspberry Pi 5.
Other consoles that BlueRetro supports should work the same way; the page's
buttons and touch controls are laid out for the N64 for now.

I initially abandoned this project years ago, but have recently decided to revive & open-source it.

# What you need

| Part | What it needs to be | Tested with |
|---|---|---|
| Console | Anything [BlueRetro supports](https://github.com/darthcloud/BlueRetro/wiki) with a video output you can capture. | Nintendo 64 (NTSC) |
| BlueRetro adapter | A BlueRetro for your console, running the **universal** firmware (below). | ESP32 "hw1" board with N64 connectors |
| Bluetooth dongle | A USB Bluetooth adapter that supports both Bluetooth Classic and LE, and that [Bumble](https://github.com/google/bumble) can drive directly (standard USB HCI; Bumble also loads firmware for Realtek and Intel chips). On Windows and Linux alike, BlueNetplay drives the dongle itself through Bumble instead of the system's Bluetooth, so it's dedicated to BlueNetplay: your computer stops using it for its own Bluetooth. | Laird (Ezurio) BT851, Cypress CYW20704A2 |
| Capture device | A USB capture device with audio for your console's video output. Windows: any DirectShow device. Linux: a V4L2 + ALSA device (BlueNetplay is tuned for the GV-USB2 and includes a driver for it; others may need different capture settings). | I-O DATA GV-USB2 (composite) |
| Host | A Windows 10/11 PC, or a Linux computer such as a Raspberry Pi 4 or 5, ideally on Ethernet. For the relay, Windows uses an NVIDIA GPU (NVENC) or the CPU (x264). | Windows 10 with a Nvidia GPU; Raspberry Pi 5, Raspberry Pi OS Trixie |
| Relay server | Only for playing over the internet: a Linux server with nginx and a (sub)domain. | Ubuntu with Plesk |

**How many players:** player 1 is a Bluetooth Classic gamepad, and every
further player is a Bluetooth LE gamepad on the same dongle. So it depends on
how many LE connections your dongle holds at once. The tested dongle holds
one, which makes 2 players. More players need a dongle that holds more, or a
second dongle (not supported by the code yet, I don't have a second dongle to test so a PR is appreciated).

# BlueRetro firmware

Use the **universal** firmware, so BlueRetro detects the console by itself (or
set the console in BlueRetro's web config). BlueNetplay was tested with the
N64-only build, `BlueRetro_hw1_n64.bin`, which works the same for an N64.

To build or flash one, see BlueRetro's
[DIY build instructions](https://github.com/darthcloud/BlueRetro/wiki/BlueRetro-DIY-Build-Instructions)
and [flashing via USB serial](https://github.com/darthcloud/BlueRetro/wiki#82---via-usb-serial).

**Check the boot log** if inputs don't arrive. BlueRetro prints it on its USB
serial port at 921600 baud. Run `tools/blueretro_serial_log.ps1` (Windows, no
install) or `tools/blueretro_serial_log.py` (needs pyserial) on a computer
connected to the ESP32, then power the console on. Look for
`# Detected system : ... N64` (or your console; `# Hardcoded system` with a
console-specific build). If inputs connect but never reach the console, a
wrong system here is the first thing to check.

# Windows

1. Install **Python 3.10+**, then in this folder:

   ```sh
   pip install -r requirements.txt
   ```

2. Install **ffmpeg** (a full build with `h264_nvenc`, `libx264` and
   `libopus`) and put it on `PATH`.
3. Install the driver for your **capture device**.
4. Switch the **dongle** to WinUSB with [Zadig](https://zadig.akeo.ie/), once:
   **Options → List All Devices**, pick the dongle (check its USB ID, e.g.
   `04B4 F901` for the tested Laird, so you don't pick your keyboard or mouse),
   choose **WinUSB**, **Replace Driver**. Windows no longer uses it for its own
   Bluetooth. To undo, uninstall the device in Device Manager with "delete the
   driver software" ticked.
5. Run it:

   ```sh
   python bluenetplay.py --players 2 --fps 59.94 --host 0.0.0.0
   ```

   For a capture device other than the GV-USB2, add
   `--video-device "<name>" --audio-device "<name>"` with the names Windows
   shows for it (`ffmpeg -list_devices true -f dshow -i dummy` lists them).

Open http://localhost:8064 and switch the console on. BlueRetro connects within
a few seconds; **Controller: live** means input reaches the console. Other
devices on your network use `http://<this PC's address>:8064` (allow Python
through Windows Firewall on private networks).

Close OBS or anything else using the capture device first.

# Raspberry Pi

1. Copy this folder to the Pi, for example:

   ```sh
   scp -r bluenetplay.py gamepad.py ble_pads.py stream.py requirements.txt web drivers linux user@raspberrypi.local:bluenetplay/
   ```

2. On the Pi, in that folder:

   ```sh
   sudo bash linux/install.sh --service
   ```

   This installs ffmpeg and the kernel headers, builds the GV-USB2 driver from
   `drivers/` with DKMS (rebuilt automatically after kernel updates), adds a
   udev rule so the app can use the dongle and Linux's own Bluetooth leaves it
   alone, creates a Python environment in `.venv`, and starts BlueNetplay as
   the `bluenetplay` service at every boot. Leave out `--service` to start it
   by hand instead.

   The udev rule is for the tested dongle (`04b4:f901`). For another one, pass
   its USB ID from `lsusb`, e.g. `--dongle=0b05:1d70`. Your Pi's built-in
   Bluetooth isn't affected either way.

3. Log out and back in (or reboot) once, for the new group memberships.

The page is at `http://<pi>.local:8064`. Options for the service go in
`/etc/default/bluenetplay`:

```sh
BLUENETPLAY_ARGS="--players 2 --host 0.0.0.0 --relay wss://relay.example.com/console"
```

```sh
sudo systemctl restart bluenetplay
journalctl -u bluenetplay -f
```

To check the capture driver, the picture, the sound and the CPU in one go
(results in `bluenetplay-check/`):

```sh
bash linux/check.sh
```

Use **Ethernet**. Over Wi-Fi with Raspberry Pi OS's default power saving, the
home stream stalled now and then (packets delayed up to 0.3 s). If you must use
Wi-Fi: `sudo iw dev wlan0 set power_save off`.

The Pi captures each field as its own frame (720×240 at 59.94) and doubles the
lines, which is exactly right for 240p consoles like the N64. Its Linux driver
for the GV-USB2 is in [drivers/](drivers/README.md).

# Options

| Option | Default | |
|---|---|---|
| `--players N` | 1 | Controllers: 1 = Bluetooth Classic, 2 = plus one BLE pad on the same dongle. |
| `--host 0.0.0.0` | 127.0.0.1 | Let other devices on your network open the page. There's no password. |
| `--port N` | 8064 | Web port. |
| `--relay URL` | | Also stream to a relay server, e.g. `wss://relay.example.com/console`. |
| `--relay-bitrate` | 4M | H.264 bitrate to the relay (your upload). |
| `--h264-encoder` | Windows `h264_nvenc`, Linux `libx264` | `libx264` on Windows if there's no NVIDIA GPU. Also try it if friends see lag in browsers that decode in hardware (Vivaldi, Safari): measured in Chrome, the hardware decoder held NVENC's stream about 0.2 s, and x264's 1 ms. |
| `--quality N` | 5 | JPEG quality of the home stream, 2 (best) to 31. |
| `--video-size 720x576` | 720x480 | PAL (untested). |
| `--fps 59.94` | Windows 29.97 | Windows capture rate. On Linux it's always the field rate. |
| `--video-mode interlaced` | fields | Linux: whole 480i frames, only for real 480i sources. |
| `--video-device`, `--audio-device` | GV-USB2 | Other capture devices. |
| `--transport usb:VID:PID` | usb:0 (the first USB Bluetooth adapter) | Pick the dongle by USB ID if there are several. |
| `--no-bt`, `--no-video`, `--no-audio` | | Run without that part. |
| `--local-input` | | Windows: also read this PC's keyboard and XInput pads. |

# Playing

| Keyboard | N64 | Gamepad (standard layout) |
|---|---|---|
| W A S D | Stick (Shift = half tilt) | Left stick |
| Arrow keys | D-pad | D-pad |
| J / K | A / B | A / X |
| Space | Z | LT / RT |
| Q / E | L / R | LB / RB |
| Enter | Start | Start |
| I M U O (or numpad 8 2 4 6) | C up / down / left / right | Right stick, B, Y |
| P | Rumble Pak / Controller Pak | Back |

Click the picture first so the page gets keyboard focus. On phones and
tablets, **▶ Play (touch)** goes fullscreen in landscape with a floating
joystick and on-screen buttons. Every tab picks its player in the **Players**
card; two tabs on one player are combined.

**Player 2** is a Bluetooth LE gamepad on the same dongle. Pair it once: with
player 1 live and the console on a menu, press **Pair** next to player 2. That
sends BlueRetro's "search" combo (Z + Start + C-Down) as player 1, and BlueRetro
connects player 2 within seconds. The keys are saved in `ble_pads.json` (keep
it private; copy it along when you move hosts). Player 1 needs no pairing.

One dongle keeps only one BLE link, so it gives 2 players. Players 3 and 4
need a second dongle, which the code doesn't support yet.

# Playing online

1. Set up a relay server: [relay/DEPLOY.md](relay/DEPLOY.md).
2. Put the relay's secret in `relay_token.txt` next to `bluenetplay.py`.
3. Start the host with `--relay wss://relay.example.com/console`.
4. Make invite links on the server, one per friend, each pinned to a player
   (or watch-only), and send them.

Video and sound go once from the host to the relay (H.264 at 4 Mbit/s and Opus),
and the relay passes them to every friend; input comes back the same way. The
**Ping** pill shows browser ↔ relay plus relay ↔ host. Expect about 80 ms more
than playing next to the console.

Add `&debug=1` to an invite link for live stats (decode time, drops, network
gaps, pings) and a **Download log** button. `&decoder=hardware|auto|software`
and `&render=direct|bitmap|raf` switch the video decoder and drawing for
testing.

# Troubleshooting

| Problem | Fix |
|---|---|
| BlueRetro connects but the console ignores input | Check the boot log (above): the firmware must detect your console. |
| `LIBUSB_ERROR_...` opening the dongle | Windows: not on WinUSB yet, or another program uses it. Pi: log out and in after installing, or rerun `linux/install.sh`. |
| Player 2 stays "not paired" | Press **Pair** while player 1 is live. |
| Players 2 and 3 keep kicking each other off | One dongle supports one BLE player: use `--players 2`. |
| "Video: error" on Windows | OBS or another program has the capture device open. |
| Pi: no picture | `lsmod \| grep gvusb2`, then `sudo dmesg \| grep -i gv-usb2`. |
| Pi: picture and sound stall together | Wi-Fi power saving: use Ethernet. |
| Pi: controllers stay "waiting" after a boot | The dongle wasn't plugged in at start: `sudo systemctl restart bluenetplay`. |
| No sound | Click **Sound off** once; browsers only play sound after a click. |
| "The host is offline" on an invite link | The host isn't running with `--relay`, or `relay_token.txt` is wrong. |
| The light on BlueRetro stays solid after stopping | The dongle holds the link; start BlueNetplay again or unplug the dongle. |

# Tests

No hardware needed. Both run the gamepads on a virtual Bluetooth link against a
simulated BlueRetro (Python ports of its SDP parser, HID parser and N64
mapping):

```sh
python tests/test_gamepad_sim.py
python tests/test_ble_sim.py
```

Both end with `ALL PASSED`.

# Files

| Path | |
|---|---|
| `bluenetplay.py` | The app: web server, capture, Bluetooth players, relay link. |
| `gamepad.py` | Bluetooth Classic HID gamepad for BlueRetro. Also runs on its own on Windows (keyboard / XInput). |
| `ble_pads.py` | Bluetooth LE gamepads (player 2+), pairing and BlueRetro's search combo. |
| `stream.py` | H.264 and Opus for the relay, and the connection to it. |
| `web/` | The page. |
| `relay/` | The relay server, its systemd unit and [DEPLOY.md](relay/DEPLOY.md). |
| `linux/` | Raspberry Pi install and check scripts, udev rule, systemd unit. |
| `drivers/` | Linux driver for the GV-USB2 ([README](drivers/README.md)). |
| `tools/` | BlueRetro serial log readers. |
| `tests/` | Offline tests. |

# Credits

- [BlueRetro](https://github.com/darthcloud/BlueRetro) by Jacques Gagnon:
  the adapter firmware this is built around, and the source that showed what a
  controller must look like to it.
- [Bumble](https://github.com/google/bumble) by Google: the Bluetooth stack
  that lets a dongle act as a gamepad.
- [GV-USB2-Driver](https://github.com/Isaac-Lozano/GV-USB2-Driver) by Isaac
  Lozano: the Linux driver `drivers/` is based on.
- [FFmpeg](https://ffmpeg.org) for capture and encoding.

# License

BlueNetplay is licensed under the [Apache License 2.0](LICENSE). The GV-USB2
driver in `drivers/` keeps its original BSD-3-Clause / GPL-2.0 dual license
(see `drivers/LICENSE.bsd` and `drivers/LICENSE.gpl`). Included third-party
work and its licenses are listed in [NOTICE](NOTICE).
