# ThorFAN

Fan control and thermal policy editor for the NVIDIA Jetson AGX Thor.

[中文文档](README.zh-CN.md)

The stock `nvfancontrol` profile caps the fan at **5371 RPM**, even when the SoC
sits at 109 °C and the GPU is already throttling. The fan itself reaches roughly
**13650 RPM**. ThorFAN unlocks that headroom, with a safety interlock so you
cannot cook the board by accident.

Measured on a Jetson AGX Thor Developer Kit, L4T R38.4.0, under a sustained
27B-parameter LLM inference load:

| Fan state | Duty cycle | Speed | Junction temp |
|---|---|---|---|
| Stock closed loop | pwm 97 | 5400 RPM | 108.9 °C |
| Full speed | pwm 255 | 13658 RPM | 103.1 °C |

Note that full speed bought only 5.8 °C. See [Expectations](#expectations).

## Status

Early development. The control layer, CLI and terminal display work; the GTK4
interface is not written yet.

## Requirements

- NVIDIA Jetson with a PWM fan (developed on AGX Thor, L4T R38+)
- Python 3.10+, standard library only
- Root for anything that drives the fan
- GTK4 and libadwaita, only for the unwritten GUI

## Quick start

No installation needed to look around. From a clone:

```bash
git clone https://github.com/swsususu/ThorFAN.git
cd ThorFAN
python3 -m thorfan.cli status
```

That reads sensors only and needs no privileges. To control the fan, run the
live display as root:

```bash
sudo python3 -m thorfan.cli tui
```

Press `q` to quit; the fan returns to `nvfancontrol` when you do.

That is the whole minimum path. Everything below is about installing it properly
and about the other commands.

## Install

Ubuntu 24.04 marks its Python as externally managed (PEP 668), so a plain
`pip install` is refused. Pick one:

```bash
# A: no install, always use `python3 -m thorfan.cli` from the clone
# B: system-wide, which is what the systemd unit expects
sudo pip install --break-system-packages .
# C: isolated; `thorfan` lands in ~/.local/bin
pipx install .
```

With B or C the `thorfan` command works directly, and the examples below use
that shorter form.

### Run at boot

```bash
sudo install -m 644 packaging/thorfan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now thorfan
journalctl -u thorfan -f
```

The unit expects `thorfan` at `/usr/bin/thorfan`. With pipx or a virtualenv,
edit `ExecStart` and `ExecStopPost` to the real path first.

It deliberately does not declare `Conflicts=nvfancontrol.service`: ThorFAN stops
and restarts the vendor daemon itself, and a systemd conflict would fight that.

### Control without sudo

The control socket is root-only by default. Create a `thorfan` group to let the
display change the fan as a normal user:

```bash
sudo groupadd -f thorfan
sudo usermod -aG thorfan "$USER"
sudo systemctl restart thorfan   # the socket reads the group when it binds
```

Log out and back in, or run `newgrp thorfan`, for the membership to apply. This
only helps when a root daemon is already running, because an unprivileged
process cannot start a control loop; the usual pairing is the systemd service
plus an unprivileged display. Anyone in this group can drive the fan, so treat
membership as equivalent to being able to write `pwm1`.

## Usage

### Look at the current state

```bash
thorfan status
```

```
fan     pwm=58/255 enabled=True rpm=1786
vendor  nvfancontrol=active
thorfan daemon not running

zone                  temp  critical   margin
tj-thermal           55.1C    114.5C    59.4C
soc012-thermal       55.1C    114.5C    59.4C
cpu-thermal          53.7C    114.5C    60.8C
soc345-thermal       53.7C    114.5C    60.8C
gpu-thermal       unavailable    114.5C      n/a

VDD_GPU                   0.00 W
VDD_CPU_SOC_MSS           6.51 W
VIN_SYS_5V0               6.52 W
total                    13.03 W

throttling  none
```

The thermal zones:

| Zone | Meaning |
|---|---|
| `tj-thermal` | Junction temperature: the **aggregate maximum** of the others, not a sensor of its own. This is what `nvfancontrol` and the throttling logic use. |
| `cpu-thermal` | CPU cluster |
| `gpu-thermal` | GPU core. Reads `unavailable` while the GPU is power-gated at idle, which is normal. |
| `soc012-thermal` | One region of the SoC die |
| `soc345-thermal` | Another region |

All five share a 114.5 °C critical trip. Because `tj` is the aggregate, it is
almost always the hottest; the other rows tell you *where* the heat is.

`VDD_GPU` reading 0.00 W is the same power-gating: no current flows to a
railgated GPU.

### The live display

```bash
sudo thorfan tui
```

```
ThorFAN  mode: manual
────────────────────────────────────────────────────
fan   pwm 230/255 ( 90%)  ████████████████████░░░░
      13572 rpm           ███████████┃░░░░░░░░░░░░  max 13650
                                     ┗ stock cap 5371
temps tj        *85.2 C   ██████████████░░░░░░░░░░  crit 114
      gpu        84.8 C   █████████████░░░░░░░░░░░  crit 114
      cpu        78.1 C   ███████████░░░░░░░░░░░░░  crit 114
power VDD_GPU            32.40 W
      VDD_CPU_SOC_MSS    14.22 W
      total              53.16 W
throt ● gpu  ○ cpu  ○ soc012
freq  gpu-gpc 18/169
chart ⠀⠀⠀⢀⣀⣀⠀⠀⠀⢀⣀⣀⣀⡀                     115C
      ⢀⠔⠁⠀⠀⠉⠢⡀⠀⢀⠔⠁⠀⠀⠈                      30C
      last 45s      temp tj   duty
────────────────────────────────────────────────────
[↑/↓] ±5%   [1-9] 10-90%   [0] 100%   [c]urve  [m]anual  [v]endor  [g]raph  [q]uit
```

| Key | Action |
|---|---|
| `↑` `↓` (or `k` `j`) | Duty cycle ±5%, relative to the current speed |
| `1`–`9` | Jump to 10%–90% |
| `0` | 100% |
| `c` | Curve mode: follow the configured temperature curve |
| `m` | Manual mode: hold a fixed duty cycle |
| `v` | Vendor mode: hand the fan back to `nvfancontrol` |
| `g` | Toggle the chart |
| `q` or `Esc` | Quit, releasing the fan |

Reading the display:

- The `┃` on the rpm bar is the stock 5371 RPM ceiling. Everything to its right
  is headroom the vendor profile never uses.
- `*` marks the hottest zone. That is the zone the chart plots, the curve reads,
  and the interlock watches.
- `●` means that subsystem is actually being throttled; `○` means it is not.
  The `freq` row appears only when something is capped.
- The chart shows the hottest zone's temperature (cyan) and the duty cycle
  (dim), scaled to their own ranges. What it is for is the *shape* of the
  response: how fast the fan reacts, and whether it overshoots. Gaps are real
  missing samples, not glitches.

Run as root with no daemon present, the display starts its own control loop for
the session and hands the fan back on exit, so nothing else is needed. If a
daemon is already running it is used as-is and never displaced. Without root or
group membership the display still runs, read-only.

### Change the speed from the command line

```bash
sudo thorfan set 90%      # percentage
sudo thorfan set 255      # a bare number is pwm, 0-255
sudo thorfan mode curve   # follow the temperature curve
sudo thorfan mode vendor  # hand the fan back
```

With a daemon running these take effect immediately over its control socket.
With none, `set` and `mode` start one in the foreground with the setting already
applied, and release the fan when you interrupt them. Add `--save` to record the
setting for the systemd service instead of running a loop now.

`set` does not print the resulting rpm, because the tachometer lags a change by
several seconds; run `thorfan status` a moment later for the settled speed.

The CLI never writes `pwm1` itself. The 100 °C interlock lives in the control
loop, and a duty cycle set by a process that has already exited would have no
interlock at all. That is why changing the speed always implies a running loop.

### Other commands

```bash
thorfan profile           # the vendor curve, with its RPM ceiling called out
sudo thorfan daemon       # just the control loop; what the systemd unit runs
sudo thorfan restore      # return the fan to nvfancontrol
```

### Configure the curve

Curve mode reads `/etc/thorfan/config.json`. There is no editor yet, so write it
by hand:

```bash
sudo mkdir -p /etc/thorfan
sudo tee /etc/thorfan/config.json > /dev/null <<'EOF'
{
  "version": 1,
  "mode": "curve",
  "manual_pwm": 128,
  "poll_interval_s": 2.0,
  "curve": [
    {"temp_c": 40,  "pwm": 77},
    {"temp_c": 60,  "pwm": 90},
    {"temp_c": 75,  "pwm": 120},
    {"temp_c": 85,  "pwm": 165},
    {"temp_c": 95,  "pwm": 210},
    {"temp_c": 100, "pwm": 255}
  ]
}
EOF
```

`temp_c` is an **absolute temperature**, unlike the vendor profile's first
column, which is headroom below the critical trip and therefore inverted. The
40 °C point matches the vendor profile's quietest step, so idle noise is
unchanged; the difference is at the top, where the stock profile stops at
pwm 97 and this goes to 255.

A malformed file falls back to vendor mode and the default curve rather than
refusing to start.

## How it works

Three modes:

- **vendor** — `nvfancontrol` owns the fan; ThorFAN only observes and never
  writes, so the two cannot fight over `pwm1`.
- **curve** — ThorFAN stops `nvfancontrol` and drives the fan from a
  piecewise-linear temperature/duty-cycle curve.
- **manual** — a fixed duty cycle.

### Safety

Giving a UI the power to stop the fan on a board that idles near 108 °C demands
interlocks, so:

- Above **100 °C** any user setting is overridden and the fan goes to full
  speed. The override latches until the temperature falls below 95 °C.
- Stopping the daemon always restarts `nvfancontrol`, including on crashes and
  `SIGTERM`. If the vendor daemon cannot be restarted, the fan is forced to full
  speed rather than left at an arbitrary value.
- If no thermal zone can be read at all, the fan goes to full speed: there is no
  safe way to guess a temperature.
- A corrupt or hand-edited config falls back to vendor mode instead of refusing
  to start.

One gap to know about: `SIGKILL` bypasses all of this, leaving the fan at its
last duty cycle. Only the systemd unit's `ExecStopPost` covers that case, which
is a reason to prefer the service over a foreground daemon for long runs.

## Expectations

Full speed bought 5.8 °C on the tested unit. The heatsink and airflow path are
close to saturation, so the fan is not the lever that escapes the throttling
band; a lower `nvpmodel` power mode or a smaller workload is. This is why the
display shows the power rails: under load, they are what moves the temperature.

## Platform notes

Details that cost time to discover, recorded here so others need not repeat the
experiments:

**`pwm1_enable=0` switches the fan off — it is not a "manual mode".** Writing a
duty cycle while the output is disabled has no effect. To drive the fan by hand,
write `pwm1` first and `pwm1_enable=1` afterwards.

**The vendor profile's first column is not temperature.** With
`TMARGIN ENABLED`, it is headroom below the critical trip point, so *smaller
means hotter*. A row reading `0 0 255 5371` applies when the SoC is at its
limit, not when it is cold.

**In `close_loop` mode the PWM column is advisory.** `nvfancontrol` targets the
RPM column, which is why the stock profile settles at pwm 97 despite the table
saying 255.

**Tegra thermal zones intermittently return `EAGAIN`.** A plain read of
`/sys/class/thermal/thermal_zone*/temp` fails occasionally under load; reads
must be retried.

**`gpu-thermal` is unreadable while the GPU is power-gated.** This is not the
intermittent failure above: with the GPU idle, every read of that zone returns
`EAGAIN`, measured at 300 failures out of 300 attempts, and starts working
again under load. Retrying cannot fix it, so a zone that cannot be read is
skipped rather than treated as an error.

A consequence worth knowing if you build on this: retrying a permanently
failing read turns the backoff into a fixed delay. The retry ladder costs 30 ms
per gated zone, which was 80% of a display refresh until the read path for
optional sensors was changed to a single unretried attempt.

**hwmon numbering is not stable.** `pwm_tach` moved from `hwmon4` to `hwmon2`
between boots on the development machine. Look devices up by the contents of
their `name` file. `cooling_device*` numbering does not sort either:
`cooling_device10` precedes `cooling_device2` lexically.

**Throttle alerts say directly what temperature only implies.** The
`*-throttle-alert` cooling devices step above 0 when the SoC is actually being
throttled, and `devfreq-gpu-gpc-0` reports the current GPU frequency cap.

**Only three INA3221 channels are real rails.** `in1`–`in3` carry labels and
matching `curr*_input` nodes; the rest are shunt voltages and a sum, and adding
them into a total would double-count.

**`/etc/nvfancontrol.conf` belongs to no dpkg package.** The
`nvidia-l4t-nvfancontrol` package ships templates in
`/etc/nvpower/nvfancontrol/` and `nvpower` copies the board-matched one into
place. Edits survive package upgrades but not a reflash.

## Recovery

If anything goes wrong:

```bash
sudo systemctl stop thorfan        # if the service is installed
sudo thorfan restore
thorfan status                     # expect nvfancontrol=active
```

Forcing full speed by hand, minding the write order:

```bash
sudo systemctl stop nvfancontrol
sudo sh -c 'echo 255 > /sys/class/hwmon/hwmon1/pwm1; echo 1 > /sys/class/hwmon/hwmon1/pwm1_enable'
```

Check the hwmon number first with `cat /sys/class/hwmon/hwmon*/name`, since it
is not stable.

## Development

```bash
python3 -m pytest tests/ -q     # 138 tests, no hardware required
```

The tests fake sysfs, so they run anywhere. The terminal display needs a real
terminal; for a smoke test:

```bash
timeout 10 script -qc "TERM=xterm-256color python3 -m thorfan.cli tui" /dev/null \
  < <(sleep 5; printf 'q')
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the safety invariants that must not
regress, and [DEVELOPMENT.zh-CN.md](DEVELOPMENT.zh-CN.md) (Chinese) for the
reasoning behind the design decisions and a fuller record of the hardware
behaviour.

## Licence

MIT — see [LICENSE](LICENSE).

## Disclaimer

ThorFAN removes the vendor fan speed ceiling and lets you set an arbitrary duty
cycle, including stopping the fan, on hardware that runs near its thermal limit.
The safety interlocks described above are best-effort software checks, not a
substitute for the vendor thermal policy. You are responsible for any damage to
your hardware. Use at your own risk.
