"""Tests for config persistence and vendor profile parsing."""

from __future__ import annotations

import json

from thorfan.core.config import Config
from thorfan.core.policy import CurvePoint, FanCurve, Mode
from thorfan.core.vendor import parse_profile

# A verbatim excerpt of the stock Thor profile, including the 5371 RPM cap
# that motivates this project.
STOCK_CONF = """
POLLING_INTERVAL 2

<FAN 1>
\tTMARGIN ENABLED
\tFAN_PROFILE cool {
\t\t#TEMP \tHYST\tPWM\tRPM
\t\t0\t0\t255\t5371
\t\t15\t0\t255\t5371
\t\t24\t0\t192\t4170
\t\t45\t0\t77\t1750
\t}
\tFAN_PROFILE quiet {
\t\t0\t0\t128\t3000
\t\t45\t0\t60\t1200
\t}
\tFAN_DEFAULT_PROFILE cool
"""


# ---- config -----------------------------------------------------------


def test_config_roundtrip(tmp_path):
    original = Config(
        mode=Mode.CURVE,
        manual_pwm=200,
        curve=FanCurve([CurvePoint(50, 100), CurvePoint(90, 240)]),
        poll_interval_s=1.5,
    )
    path = tmp_path / "config.json"
    original.save(str(path))

    loaded = Config.load(str(path))
    assert loaded.mode is Mode.CURVE
    assert loaded.manual_pwm == 200
    assert loaded.poll_interval_s == 1.5
    assert [(p.temp_c, p.pwm) for p in loaded.curve.points] == [
        (50.0, 100), (90.0, 240)
    ]


def test_missing_config_yields_defaults(tmp_path):
    loaded = Config.load(str(tmp_path / "absent.json"))
    assert loaded.mode is Mode.VENDOR
    assert loaded.curve.points  # default curve populated


def test_corrupt_config_yields_defaults(tmp_path):
    """A bad config must never prevent the fan from being managed."""
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")
    loaded = Config.load(str(path))
    assert loaded.mode is Mode.VENDOR


def test_unknown_mode_falls_back_to_vendor(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mode": "telepathy"}))
    assert Config.load(str(path)).mode is Mode.VENDOR


def test_out_of_range_manual_pwm_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"manual_pwm": 9000}))
    assert Config.load(str(path)).manual_pwm == 128


def test_invalid_curve_falls_back_to_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"curve": [{"temp_c": 50}]}))  # missing pwm
    loaded = Config.load(str(path))
    assert len(loaded.curve.points) == len(FanCurve.default().points)


def test_save_is_atomic(tmp_path):
    """No .tmp file should survive a successful save."""
    path = tmp_path / "config.json"
    Config().save(str(path))
    assert path.exists()
    assert not (tmp_path / "config.json.tmp").exists()


# ---- vendor parsing ---------------------------------------------------


def test_parse_stock_profile():
    points = parse_profile(STOCK_CONF, "cool")
    assert len(points) == 4
    assert (points[0].tmargin_c, points[0].pwm, points[0].rpm) == (0.0, 255, 5371)
    assert max(p.rpm for p in points) == 5371


def test_parse_selects_named_profile():
    points = parse_profile(STOCK_CONF, "quiet")
    assert len(points) == 2
    assert points[0].rpm == 3000


def test_parse_unknown_profile_is_empty():
    assert parse_profile(STOCK_CONF, "turbo") == []


def test_parse_skips_comments_and_blanks():
    points = parse_profile(STOCK_CONF, "cool")
    # The '#TEMP HYST PWM RPM' header must not become a data row.
    assert all(isinstance(p.pwm, int) for p in points)
