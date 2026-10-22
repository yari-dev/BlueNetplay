# GV-USB2 Linux driver

A Linux driver for the **I-O DATA GV-USB2** SD capture device (USB `04bb:0532`:
a Syntek STK1150 USB bridge plus a Techwell TW9910 video decoder). One kernel
module, `gvusb2.ko`, provides:

- **video:** a V4L2 capture device (`/dev/videoN`, card "I-O DATA GV-USB2"),
  UYVY 4:2:2, composite or S-Video input, NTSC or PAL;
- **audio:** an ALSA capture card `GVUSB2` (line in, 48 kHz, stereo, S16_LE).

**Origin:** this is a reworked version of Isaac Lozano's
[GV-USB2-Driver](https://github.com/Isaac-Lozano/GV-USB2-Driver) (2019,
dual-licensed BSD-3-Clause / GPL-2.0, see `LICENSE.bsd` and `LICENSE.gpl`).
The register sequences that bring up the STK1150 and TW9910 are his work; the
changes below make it build cleanly, load and run reliably on current kernels
and Raspberry Pi OS, and capture with low latency.

**Tested:** on a Raspberry Pi 5 with Raspberry Pi OS Trixie (kernel
`6.18.50+rpt-rpi-2712`) and a real GV-USB2 fed by a Nintendo 64: 720×240 at
59.83 fields/s with a correct picture and game sound, through a two-player
session over the internet relay of BlueNetplay, the project around it. It
also builds without warnings (`W=1`), passes `sparse`, and links against the
exported symbols of the Pi OS kernels 6.12.109 and 6.18.50 (Pi 4 `rpi-v8` and
Pi 5 `rpi-2712`).

## Files

| File | What it does |
|---|---|
| `gvusb2-main.c` | *New.* The module: registers the video and the audio USB driver. |
| `gvusb2-core.c` | Register access to the STK1150 over USB control transfers. |
| `gvusb2-i2c.c` | Register access to the TW9910 through the STK1150's I2C master. |
| `gvusb2-vid.c` | Video USB driver: probe, isochronous URBs, assembling fields and frames. |
| `gvusb2-v4l2.c` | V4L2 interface: formats, standards, inputs, controls, buffer queue. |
| `gvusb2-snd.c` | Audio USB driver and ALSA capture device. |
| `gvusb2.h`, `gvusb2-vid.h`, `gvusb2-regs.h` | Shared definitions. |
| `Makefile`, `dkms.conf` | Out-of-tree build, and DKMS for automatic rebuilds (`dkms.conf` is *new*). |

## Install

The BlueNetplay installer does this for you
(`sudo bash linux/install.sh`, see the main README). By hand, on Raspberry
Pi OS, Debian or Ubuntu, with DKMS so it's rebuilt after every kernel update:

```bash
sudo apt install dkms linux-headers-$(uname -r)
sudo mkdir -p /usr/src/gvusb2-2.0
sudo cp *.c *.h Makefile dkms.conf /usr/src/gvusb2-2.0/
sudo dkms install -m gvusb2 -v 2.0
sudo modprobe gvusb2
```

Or for the running kernel only: `make && sudo make install`.

The module then loads by itself whenever the GV-USB2 is plugged in. To check:

```bash
sudo dmesg | grep -i gv-usb2
v4l2-ctl --list-devices --list-formats-ext
arecord -l
```

Verbose driver logs: `echo 'module gvusb2 +p' | sudo tee /sys/kernel/debug/dynamic_debug/control`.

## Video formats

| Size (NTSC / PAL) | Field | Rate | Use |
|---|---|---|---|
| 720×480 / 720×576 | `interlaced` | 29.97 / 25 frames/s | Whole frames with both fields woven. The default, and right for real 480i/576i video. |
| 720×240 / 720×288 | `alternate` | 59.94 / 50 fields/s | One field per buffer. Right for game consoles: 240p consoles such as the N64 send the same field type every time, so weaving would only fill every other line. |

The driver picks the mode from what the program asks for: the field height
(`-video_size 720x240`), `V4L2_FIELD_ALTERNATE`, or the field rate with
`VIDIOC_S_PARM` (`-framerate 60000/1001`). ffmpeg reads the rate *before* it
sets the size, so pass both:

```bash
ffmpeg -f v4l2 -input_format uyvy422 -framerate 60000/1001 -video_size 720x240 -i /dev/video0 -vf scale=720:480:flags=neighbor out.mp4
```

Controls: brightness, contrast, saturation, hue, sharpness, and the capture
window's vertical/horizontal start. `v4l2-ctl --list-inputs` shows "no
signal" while nothing is connected, and `--get-detected-standard` the
standard the TW9910 sees.

## What changed compared to the original

### It didn't load fully on a Raspberry Pi

- **Sound card number.** The original asked ALSA for card 0, which on a Pi
  already belongs to HDMI audio, so the audio part failed to load. It now
  takes the first free number (on the Pi 5: card 2).
- **No `tw9910` driver in Pi OS.** The original created an I2C adapter so the
  kernel's `tw9910` driver could bind to the decoder. Raspberry Pi OS doesn't
  build that driver. Where it does exist, it switches the decoder's outputs
  off when a stream stops and refuses to switch them on again (`-EPERM`,
  because this driver never sets a format on it). The driver now programs the
  TW9910 itself: the I2C adapter is gone, and `gvusb2-i2c.c` offers plain
  register read/write with a lock around each multi-step transfer.

### Bugs that could hang or crash the kernel

- **Endless I2C wait.** The busy-wait loop never counted its attempts, so a
  decoder that didn't answer hung the thread forever. It now gives up after
  100 tries, with a short sleep between them, and passes USB errors on.
- **Deadlock when streaming failed to start.** `start_streaming` returned
  with its lock held if submitting URBs failed, and didn't give the queued
  buffers back to videobuf2 (which the kernel warns about). Both fixed; the
  driver now uses one lock for the device and its queue.
- **Use after free when stopping.** `stop_streaming` took its lock
  "interruptibly": a signal made it skip the cleanup, leaving URBs writing
  into freed buffers. Stopping can no longer be interrupted.
- **Sound data before there was a buffer.** Sound URBs were started in
  `open()`, before ALSA had a buffer, so early data was copied through a NULL
  pointer. URBs now start in `prepare`, and data is only copied between
  trigger START and STOP. A second `open()` also submitted the URBs again
  before finding the device busy.
- **Unplugging while recording.** The sound driver freed its data on unplug
  even if the card was still open. That data now lives inside the ALSA card,
  which is freed after the last user closes it. The video side keeps a
  reference to the USB device for the same reason.
- **Ignored errors.** URB allocation failures went unnoticed (the return
  value wasn't checked), and `gvusb2_set_reg_mask` ignored write errors.

### Latency

- **Smaller URBs.** Each URB carried 256 packets: 32 ms of video and
  **256 ms of audio** waited in the kernel before any of it reached the
  program. Now video URBs are 32 microframes (4 ms) and audio URBs 8 packets
  (8 ms), and ALSA periods can be as short as 4 ms (the original's minimum was
  48 KB, about 256 ms).
- **Accurate timestamps.** Every field used to get the time its URB
  completed, up to 4 ms late and jittery (16 and 20 ms steps instead of
  16.7). It now gets the time of the USB microframe that ended it: on the
  N64, 16.58–16.81 ms between fields.

### Robustness

- **USB errors no longer stop the stream.** The original stopped resubmitting
  URBs after any error status. Transient errors are now skipped, and
  partially failed URBs (`-EXDEV`) are still used.
- **Quiet kernel log.** Empty isochronous packets are normal, but the
  original logged each one as an error at info level, as it did with counter
  mismatches, flooding `dmesg`. Debug messages are now rate-limited and off
  unless dynamic debug is enabled.
- **No hard-coded USB layout.** The video and audio alternate settings and
  packet sizes are read from the device's descriptors instead of being fixed
  numbers.
- **USB control timeouts** were given in jiffies (`HZ`) where milliseconds
  are expected; they now use the kernel's standard timeouts.

### Building on current kernels

- The original still compiles on 6.12 and 6.18, with 15 warnings (a global
  variable named `index` that clashes with a built-in function, missing
  prototypes). The new version has none, also with `W=1`.
- **One module instead of two.** `gvusb2-sound.ko` and `gvusb2-video.ko`
  both linked in `gvusb2-core.o`. Now `gvusb2.ko` registers both USB drivers
  (`gvusb2-main.c`), with one device table.
- **Current APIs.** `vb2_ops_wait_prepare/finish` are only used on kernels
  that still need them (before 6.18). `strncpy` became `strscpy`. The sound
  buffer is ALSA's managed vmalloc buffer instead of a hand-made one with its
  own `page` callback, and the obsolete `snd_pcm_lib_ioctl` is gone.
- **DKMS.** `dkms.conf` rebuilds the module automatically after kernel
  updates.

### New features

- One-field-per-buffer mode (`V4L2_FIELD_ALTERNATE`) at 59.94/50 Hz, chosen
  by size, field or frame rate (see above).
- `VIDIOC_G_PARM`/`S_PARM` (the original had none, so ffmpeg didn't know the
  frame rate), `ENUM_FRAMESIZES`, `ENUM_FRAMEINTERVALS`, `PREPARE_BUF` and
  DMABUF export.
- Input status ("no signal") and `QUERYSTD` from the TW9910's status
  registers.
- Changing the standard (`S_STD`) now reprograms the decoder's cropping and
  the capture window itself. The original left that to the `tw9910` driver
  (missing on Pi OS), always used NTSC cropping when streaming, and treated
  every PAL variant except PAL-B as NTSC-sized. PAL is untested: no PAL
  source was available.
- The card names itself "I-O DATA GV-USB2" in V4L2 and ALSA, and the probe
  logs the video and audio settings it found, plus the decoder's chip ID.

## Hardware notes

From the device's USB descriptors (bcdDevice 0005):

- Interface 0, video: isochronous IN `0x82`. Alternate setting 5 gives
  3×1024 bytes per microframe; 720×480 UYVY at 30 frames/s needs about 2600.
- Interface 1 has no endpoints.
- Interface 2, audio: isochronous IN `0x84`. Alternate setting 1 gives 256
  bytes every 1 ms.

The TW9910 answers on I2C address `0x44` with ID `0x58` (TW9910, revision 0).

Video packets: a packet with `0x80` set in its first byte is a status packet
and starts a new field, with `0x40` set for the odd (top) field. Data packets
start with a 4-byte header whose second byte is a 6-bit sequence counter. An
N64 sends 240p: every field arrives as an even (bottom) field, at 59.83 Hz.
