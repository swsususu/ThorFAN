# Contributing to ThorFAN

## Before anything else: this code can damage hardware

ThorFAN removes a vendor safety ceiling on a board that idles near 108 °C and
trips at 114.5 °C. A change that looks cosmetic can leave a fan stopped. Please
hold changes to the control path to a higher standard than you normally would.

The invariants that must not regress:

- **Vendor mode never writes `pwm1`.** Two controllers fighting over one sysfs
  node produce oscillation. Locked by `test_vendor_mode_never_writes`.
- **Every exit path restores `nvfancontrol`.** Including exceptions and
  `SIGTERM`. If the vendor daemon cannot be restarted, force full speed rather
  than leaving the last duty cycle.
- **The 100 °C override beats user intent.** Locked by
  `test_emergency_overrides_manual_stop`.
- **No readable sensor means full speed.** Guessing a temperature is not safe.
  Locked by `test_all_zones_unreadable_forces_full_speed`.
- **A corrupt config degrades to vendor mode**, never to a refusal to start: an
  unmanaged fan is worse than ignored settings.
- **The CLI never writes `pwm1` directly.** The interlock lives in the control
  loop, so a short-lived process setting a duty cycle would leave the fan
  entirely unprotected.

If your change touches any of these, say so explicitly in the pull request.

## Development setup

No third-party dependencies for the core, and the tests fake sysfs, so they run
on any Linux machine:

```bash
git clone https://github.com/swsususu/ThorFAN.git
cd ThorFAN
python3 -m pytest tests/ -q
```

Only `pytest` is needed:

```bash
pip install --user pytest
```

Hardware is required only to verify behaviour against real sysfs. If you do not
have a Jetson, say so in the pull request and describe what you could not test;
that is more useful than a confident claim.

## Running against hardware

Read-only commands are safe and unprivileged:

```bash
python3 -m thorfan.cli status
python3 -m thorfan.cli profile
```

Anything that drives the fan needs root. Start in vendor mode, which observes
without taking over:

```bash
sudo python3 -m thorfan.cli mode vendor --save
sudo python3 -m thorfan.cli daemon -v
```

Then in another terminal, watch what happens as you change settings:

```bash
python3 -m thorfan.cli status
sudo python3 -m thorfan.cli set 90%
sudo python3 -m thorfan.cli mode vendor
systemctl is-active nvfancontrol      # must be active again
```

Keep an eye on the temperature the first few times. `sudo thorfan tui` shows it
continuously.

If you leave the fan in a bad state:

```bash
sudo python3 -m thorfan.cli restore
```

## Testing expectations

New behaviour needs a test. The existing suite is the specification for the
safety properties, so please extend it rather than working around it.

Conventions in the suite worth following:

- Hardware is faked, never mocked wholesale: `FakeFan` records writes so a test
  can assert on the *decisions* the controller made.
- Tests that lock a safety property say so in the docstring, with the reason.
  `test_emergency_overrides_manual_stop` is the model.
- Sensor failure is a first-class case, not an edge case. A zone that cannot be
  read, a rail with no current node, and a board where nothing reads at all all
  have tests.
- An `autouse` fixture keeps the suite away from the host's real
  `nvfancontrol`; do not remove it, or running the tests on a Jetson will stop
  the vendor daemon.

The display's rendering is tested against a `FakeWindow` that raises on
out-of-bounds writes, which is how layout bugs in narrow terminals get caught
without a terminal.

## Code style

Match what is there. Specifics that are deliberate:

- **Comments explain why, not what.** The codebase is full of hardware facts
  that are not inferable from the code (`pwm1_enable=0` switches the fan off,
  the vendor profile's first column is inverted). Those comments are the most
  valuable part of the file; please add to them when you learn something.
- **No third-party dependencies in `thorfan/core` or `thorfan/tui.py`.** The
  standard library has been sufficient, and users of this tool tend to mind
  extra packages on an embedded board. The unwritten GUI is the one exception,
  because GTK4 cannot be reimplemented.
- Type hints throughout, `from __future__ import annotations` at the top.
- Docstrings on anything non-obvious, explaining the intent rather than
  restating the signature.
- British spelling in prose is fine, as is American; do not churn existing text
  to change it.

## Commit messages

Describe the change and, when it is not obvious, why it was necessary:

```
Skip power-gated zones instead of failing the control loop

gpu-thermal returns EAGAIN on every read while the GPU is railgated, so
the retry ladder cannot help and hottest() raised on every poll at idle.
```

One logical change per commit. If a fix needs a refactor first, make that a
separate commit.

## Pull requests

Please include:

- What changed and why.
- What you tested, and on what hardware and L4T version.
- **What you could not test.** This matters more than usual here, because the
  failure mode is thermal.

A pull request that adds a hardware fact to `DEVELOPMENT.zh-CN.md` or the
platform notes in the README is welcome on its own, even with no code.

## Reporting bugs

Include the output of:

```bash
python3 -m thorfan.cli status
python3 -m thorfan.cli profile
cat /proc/device-tree/model; echo
cat /etc/nv_tegra_release 2>/dev/null | head -1
```

And describe what the fan actually did, since that is frequently not what the
configuration would suggest.

If ThorFAN left your fan in a dangerous state, please report it as a bug with
that framing. A fan stopped when it should not have been is the most serious
class of defect in this project.

## Things known to be missing

Currently open, in rough priority order:

- The GTK4 GUI. `thorfan/gui/` is an empty package and the entry point exists
  but the module does not. The control socket and its `telemetry` action are
  ready; `thorfan/tui.py` has already settled what to display and how.
- Curve editing. Curve mode reads `/etc/thorfan/config.json`, which has to be
  written by hand. The socket protocol has room for
  `{"action": "set", "curve": [...]}`.
- Vendor profile editing. Raising the `5371` in `/etc/nvfancontrol.conf` is a
  lighter alternative to running a daemon at all;
  `vendor.backup_vendor_conf()` already exists for it.
- The systemd unit has not been verified on hardware, in particular whether
  `ExecStopPost` really hands the fan back after `SIGKILL`.

`DEVELOPMENT.zh-CN.md` has a fuller list with the reasoning behind each.
