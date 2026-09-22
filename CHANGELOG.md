# Changelog

All notable changes to ThorFAN are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-22

First public release. The control layer, command line and terminal display are
usable; the GTK4 interface is not written yet.

### Added

- Fan control with three modes: `vendor` (observe only, `nvfancontrol` drives),
  `curve` (piecewise-linear temperature to duty cycle), and `manual` (fixed
  duty cycle).
- Safety interlock that forces full speed above 100 °C and latches until the
  temperature falls below 95 °C, overriding any user setting.
- Fan ownership handover that restores `nvfancontrol` on every exit path,
  including unhandled exceptions and `SIGTERM`, and forces full speed if the
  vendor daemon cannot be restarted.
- `thorfan status`: temperatures with critical trip margins, fan duty cycle and
  speed, power rail draw, and throttle alert state.
- `thorfan tui`: a curses display refreshing at 1 Hz with interactive control.
  Shows the stock 5371 RPM ceiling marked in place on the speed bar, a braille
  chart of recent temperature and duty cycle history, and which thermal zone is
  driving control decisions. Run as root it starts its own control loop for the
  session, so no separate daemon is needed.
- `thorfan set` accepting a percentage (`90%`) or a raw pwm value (`0`-`255`),
  applied immediately through a running control loop.
- `thorfan mode` to switch between vendor, curve and manual at runtime.
- `thorfan profile` to display the vendor fan profile, noting that its RPM
  ceiling is roughly 40% of what the fan can do.
- `thorfan daemon` and `thorfan restore` for service use.
- A Unix socket control channel (`/run/thorfan.sock`) so settings change without
  a restart while the thermal interlock stays in force. Root-only by default,
  widened to `0660` when a `thorfan` group exists.
- Power rail readings from INA3221, exposing the three labelled rails and
  excluding shunt-voltage channels that would double-count.
- Throttle alert and frequency cap readings from thermal cooling devices, which
  report throttling directly rather than implying it from temperature.
- A systemd unit that deliberately omits `Conflicts=nvfancontrol.service`,
  leaving fan ownership to the daemon as the single authority.
- Configuration persisted as JSON, degrading to vendor mode and the default
  curve when the file is corrupt rather than refusing to start.
- 138 tests that fake sysfs and run without hardware.
- English and Chinese documentation.

### Platform behaviour handled

These are properties of the hardware rather than features, but each one required
a specific accommodation:

- `pwm1_enable=0` switches the fan off rather than selecting a manual mode, so
  duty cycle is written before enabling the output.
- `gpu-thermal` returns `EAGAIN` on every read while the GPU is power-gated, so
  unreadable zones are skipped instead of failing the control loop.
- Thermal zone reads intermittently return `EAGAIN` under load and are retried
  on the control path, but read once on the display path, where a retry ladder
  against a permanently failing sensor costs 30 ms of pure waiting.
- hwmon numbering is not stable across boots, so devices are found by the
  contents of their `name` file.
- `cooling_device*` paths do not sort meaningfully, since `cooling_device10`
  precedes `cooling_device2` lexically.

### Known limitations

- `SIGKILL` bypasses the ownership handover, leaving the fan at its last duty
  cycle. Only the systemd unit's `ExecStopPost` covers this.
- The systemd unit has not been verified on hardware.
- Raising the fan ceiling gained only 5.8 °C on the tested unit; the heatsink
  and airflow path appear close to saturation.
- The fan curve can only be edited by writing `/etc/thorfan/config.json` by
  hand.

[Unreleased]: https://github.com/swsususu/ThorFAN/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/swsususu/ThorFAN/releases/tag/v0.1.0
