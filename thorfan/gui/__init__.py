"""Placeholder for the GTK4 interface.

Not written yet. The pieces it will need already exist:

  * thorfan.core.control.send() for changes, which keeps the thermal interlock
    in the daemon rather than in the UI;
  * the daemon's "telemetry" action, which returns every sensor reading a
    display needs in one round trip;
  * thorfan/tui.py, which has already settled what to show and how to colour it.

Two constraints worth knowing before starting. The GUI must not run as root:
pair an unprivileged interface with the privileged daemon, using the `thorfan`
group or polkit to reach the control socket. And the tachometer lags a duty
cycle change by several seconds, so a live chart will show the two curves moving
out of step; that is the hardware, not a bug to fix.
"""
