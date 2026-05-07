#!/usr/bin/env python3
"""
ams.py

One-screen Antenna Measurement System (AMS) GUI for an anechoic chamber.

What this script does
- Talks to a Sunol Sciences FS-121 positioner at ASRL5::INSTR.
- Talks to a VNA at GPIB0::28::INSTR.
- Runs a radiation pattern scan by rotating the positioner and taking S21.
- Saves a tab-delimited Phi / Log Magnitude / Phase text file.
- Plots the pattern in the GUI.
- Saves a normalized polar plot image.
- Includes manual jog buttons, connection tests, and a debug/error panel.
- Includes a Simulation Mode so you can test the GUI without hardware.

Notes
- The GUI is intentionally simple and heavily commented.
- The VNA code is intentionally kept simple and is tuned for the HP 8753ES used in this chamber.
- The plotting/directivity logic is based on the uploaded Matlab files.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Type

import numpy as np

# PyVISA is optional at import time so Simulation Mode can still run.
try:
    import pyvisa
    from pyvisa import constants as visa_constants
except Exception:
    pyvisa = None
    visa_constants = None

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


# -----------------------------------------------------------------------------
# User-facing defaults
# -----------------------------------------------------------------------------
DEFAULT_POSITIONER_RESOURCE = "ASRL5::INSTR"
DEFAULT_VNA_RESOURCE = "GPIB0::28::INSTR"
DEFAULT_SAVE_FOLDER = str(Path.cwd() / "ams_results")
DEFAULT_BASE_NAME = "rad_pattern"
DEFAULT_FREQUENCY_GHZ = 5.5
DEFAULT_SPAN_DEG = 180.0
DEFAULT_STEP_DEG = 5.0
DEFAULT_SETTLE_S = 0.25
DEFAULT_JOG_DEG = 5.0
DEFAULT_SIMULATION_MODE = False
DEFAULT_USE_CORRECTION = True
DEFAULT_VISA_BACKEND = ""  # Blank = normal VISA backend. Use "@py" for pyvisa-py.
DEFAULT_VNA_FAMILY = "HP 8753"

APP_STORAGE_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
CALIBRATION_PROFILE_PATH = APP_STORAGE_DIR / "ams_chamber_calibration_profile.json"
CALIBRATION_TABLE_PATH = APP_STORAGE_DIR / "ams_chamber_calibration_table.tsv"
DEFAULT_CAL_START_GHZ = DEFAULT_FREQUENCY_GHZ
DEFAULT_CAL_STOP_GHZ = DEFAULT_FREQUENCY_GHZ
DEFAULT_CAL_STEP_GHZ = 0.1
DEFAULT_CAL_TX_GAIN_DBI = 0.0
DEFAULT_CAL_RX_GAIN_DBI = 0.0
DEFAULT_USE_CHAMBER_CALIBRATION = True

# -----------------------------------------------------------------------------
# Instrument / safety defaults
# -----------------------------------------------------------------------------
POSITIONER_STEPS_PER_DEG = 80.0          # FS-121: 80 microsteps per degree
POSITIONER_MIN_DEG = -360.0              # Software limit to protect cables
POSITIONER_MAX_DEG = 360.0               # Software limit to protect cables
POSITIONER_TIMEOUT_MS = 5000
POSITIONER_MOVE_TIMEOUT_S = 90.0
POSITIONER_POLL_S = 0.10
POSITIONER_SPEED_R_COMMAND = 480         # 1 rpm = 480R in the FS-121 docs
POSITIONER_ACCEL_P_COMMAND = 1000
POSITIONER_IDLE_POWER_COMMAND = "2W"    # 50% idle power
POSITIONER_DISABLE_HOME_SWITCH = True    # 4T disables negative-travel home switch

VNA_TIMEOUT_MS = 15000
MANUAL_VNA_QUERY_TIMEOUT_MS = 5000
CONNECT_VNA_PROBE_TIMEOUT_MS = 2500
HP8753_QUERY_DELAY_S = 0.05
HP8753_SWEEP_WAIT_S = 0.35
HP8753_FORMAT_SETTLE_S = 0.05
HP8753_WRITE_TIMEOUT_MS = 2000
HP8753_IDN_TIMEOUT_MS = 2000
VNA_MARKER_CHANNEL_MAG = 1
VNA_MARKER_CHANNEL_PHASE = 2

# Plot scaling copied from the old Matlab behavior.
PLOT_RHOMIN = -40.0
PLOT_RHOMAX_NORMALIZED = 0.0
PLOT_RHOMAX_RAW = 5.0


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
class ScanCancelled(Exception):
    """Raised when the user clicks Stop Scan."""


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def ensure_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)


def open_in_file_browser(path: Path) -> None:
    """Open a folder in the OS file browser."""
    path = path.resolve()
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def build_scan_angles(span_deg: float, step_deg: float) -> List[float]:
    """
    Build the list of requested scan angles.

    Old AMS behavior:
    - 360 deg span -> scan from -180 to +180.
    - Smaller span -> scan from -span/2 to +span/2.
    - Always centered about 0 deg.
    """
    if span_deg <= 0:
        raise ValueError("Span must be > 0 degrees.")
    if span_deg > 360:
        raise ValueError("Span must be <= 360 degrees.")
    if step_deg <= 0:
        raise ValueError("Step must be > 0 degrees.")

    if math.isclose(span_deg, 360.0, abs_tol=1e-9):
        start_deg = -180.0
        stop_deg = 180.0
    else:
        start_deg = -span_deg / 2.0
        stop_deg = span_deg / 2.0

    angles: List[float] = []
    current = start_deg
    # Build the list explicitly so the stop angle is always included.
    while current <= stop_deg + 1e-9:
        angles.append(round(current, 10))
        current += step_deg

    if not angles:
        angles = [start_deg, stop_deg]
    elif not math.isclose(angles[-1], stop_deg, abs_tol=1e-9):
        angles.append(stop_deg)

    return angles


def build_frequency_points(start_ghz: float, stop_ghz: float, step_ghz: float) -> List[float]:
    """Build an inclusive frequency list in GHz for chamber calibration."""
    if start_ghz <= 0 or stop_ghz <= 0:
        raise ValueError("Calibration frequencies must be > 0 GHz.")
    if stop_ghz < start_ghz:
        raise ValueError("Calibration stop frequency must be >= start frequency.")

    if math.isclose(start_ghz, stop_ghz, abs_tol=1e-12):
        return [float(start_ghz)]

    if step_ghz <= 0:
        raise ValueError("Calibration frequency step must be > 0 GHz when start != stop.")

    points: List[float] = []
    current = start_ghz
    while current <= stop_ghz + 1e-12:
        points.append(round(current, 12))
        current += step_ghz

    if not points:
        points = [float(start_ghz), float(stop_ghz)]
    elif not math.isclose(points[-1], stop_ghz, abs_tol=1e-12):
        points.append(float(stop_ghz))

    return points


def normalized_logmag(logmag_db: Sequence[float]) -> np.ndarray:
    mag = np.array(logmag_db, dtype=float)
    if np.all(~np.isfinite(mag)):
        return mag
    return mag - np.nanmax(mag)


def padded_rows_for_output(
    phi_deg: Sequence[float],
    logmag_db: Sequence[float],
    phase_deg: Sequence[float],
) -> List[Tuple[float, float, float]]:
    """
    Save format requested by the user:
    - tab-delimited
    - header row
    - if the scan is partial, pad with -180 and +180 rows containing NaN.
    """
    phi = list(map(float, phi_deg))
    mag = list(map(float, logmag_db))
    phs = list(map(float, phase_deg))

    rows: List[Tuple[float, float, float]] = []
    if not phi:
        return [(-180.0, math.nan, math.nan), (180.0, math.nan, math.nan)]

    if phi[0] > -180.0 + 1e-9:
        rows.append((-180.0, math.nan, math.nan))

    rows.extend(zip(phi, mag, phs))

    if phi[-1] < 180.0 - 1e-9:
        rows.append((180.0, math.nan, math.nan))

    return rows


def write_pattern_file(
    file_path: Path,
    phi_deg: Sequence[float],
    logmag_db: Sequence[float],
    phase_deg: Sequence[float],
    magnitude_header: str = "Log Magnitude (dB)",
) -> None:
    rows = padded_rows_for_output(phi_deg, logmag_db, phase_deg)
    with file_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(f"Phi (deg)\t{magnitude_header}\tPhase (deg)\n")
        for phi, mag, phs in rows:
            mag_text = "NaN" if not math.isfinite(mag) else f"{mag:.3f}"
            phs_text = "NaN" if not math.isfinite(phs) else f"{phs:.3f}"
            f.write(f"{phi:.3f}\t{mag_text}\t{phs_text}\n")


@dataclass
class CalibrationProfile:
    """Persistent chamber calibration profile K(f)."""

    created_at: str
    tx_gain_db: float
    rx_ref_gain_db: float
    start_freq_hz: float
    stop_freq_hz: float
    step_freq_hz: float
    freq_hz: List[float]
    s21_ref_db: List[float]
    phase_deg: List[float]
    k_db: List[float]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "created_at": self.created_at,
            "tx_gain_db": self.tx_gain_db,
            "rx_ref_gain_db": self.rx_ref_gain_db,
            "start_freq_hz": self.start_freq_hz,
            "stop_freq_hz": self.stop_freq_hz,
            "step_freq_hz": self.step_freq_hz,
            "freq_hz": self.freq_hz,
            "s21_ref_db": self.s21_ref_db,
            "phase_deg": self.phase_deg,
            "k_db": self.k_db,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CalibrationProfile":
        return cls(
            created_at=str(data.get("created_at", "")),
            tx_gain_db=float(data.get("tx_gain_db", 0.0)),
            rx_ref_gain_db=float(data.get("rx_ref_gain_db", 0.0)),
            start_freq_hz=float(data.get("start_freq_hz", 0.0)),
            stop_freq_hz=float(data.get("stop_freq_hz", 0.0)),
            step_freq_hz=float(data.get("step_freq_hz", 0.0)),
            freq_hz=[float(v) for v in data.get("freq_hz", [])],
            s21_ref_db=[float(v) for v in data.get("s21_ref_db", [])],
            phase_deg=[float(v) for v in data.get("phase_deg", [])],
            k_db=[float(v) for v in data.get("k_db", [])],
        )

    @property
    def point_count(self) -> int:
        return len(self.freq_hz)

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def save_table(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(
                "Frequency (GHz)\tS21_ref (dB)\tK (dB)\tPhase (deg)\tGtx_ref (dBi)\tGrx_ref (dBi)\n"
            )
            for freq_hz, s21_ref_db, k_db, phase_deg in zip(self.freq_hz, self.s21_ref_db, self.k_db, self.phase_deg):
                f.write(
                    f"{freq_hz/1e9:.9f}\t{s21_ref_db:.6f}\t{k_db:.6f}\t{phase_deg:.6f}\t{self.tx_gain_db:.6f}\t{self.rx_ref_gain_db:.6f}\n"
                )

    def describe(self) -> str:
        if self.point_count == 0:
            return "No calibration points saved."
        step_ghz = self.step_freq_hz / 1e9 if self.step_freq_hz > 0 else 0.0
        return (
            f"Created: {self.created_at}\n"
            f"Range: {self.start_freq_hz/1e9:.6f} to {self.stop_freq_hz/1e9:.6f} GHz\n"
            f"Step: {step_ghz:.6f} GHz\n"
            f"Points: {self.point_count}\n"
            f"Reference TX gain: {self.tx_gain_db:.3f} dBi\n"
            f"Reference RX gain: {self.rx_ref_gain_db:.3f} dBi"
        )

    def get_k_for_frequency(self, freq_hz: float) -> Tuple[float, str]:
        if not self.freq_hz or not self.k_db:
            raise ValueError("The saved chamber calibration has no frequency points.")

        freq_arr = np.array(self.freq_hz, dtype=float)
        k_arr = np.array(self.k_db, dtype=float)
        order = np.argsort(freq_arr)
        freq_arr = freq_arr[order]
        k_arr = k_arr[order]

        tol_hz = max(1.0, abs(self.step_freq_hz) * 0.01)
        close = np.where(np.isclose(freq_arr, freq_hz, rtol=0.0, atol=tol_hz))[0]
        if len(close) > 0:
            matched = float(freq_arr[int(close[0])])
            return float(k_arr[int(close[0])]), f"measured @ {matched/1e9:.6f} GHz"

        if len(freq_arr) == 1:
            raise ValueError(
                f"No saved chamber calibration point exists at {freq_hz/1e9:.6f} GHz. "
                f"Only {freq_arr[0]/1e9:.6f} GHz is available."
            )

        if freq_hz < float(freq_arr[0]) - tol_hz or freq_hz > float(freq_arr[-1]) + tol_hz:
            raise ValueError(
                f"{freq_hz/1e9:.6f} GHz is outside the saved calibration range "
                f"{freq_arr[0]/1e9:.6f} to {freq_arr[-1]/1e9:.6f} GHz."
            )

        index = int(np.searchsorted(freq_arr, freq_hz))
        left_index = max(0, index - 1)
        right_index = min(len(freq_arr) - 1, index)
        left_freq = float(freq_arr[left_index])
        right_freq = float(freq_arr[right_index])
        k_value = float(np.interp(freq_hz, freq_arr, k_arr))

        if math.isclose(left_freq, right_freq, abs_tol=tol_hz):
            return k_value, f"measured @ {left_freq/1e9:.6f} GHz"

        return k_value, f"interpolated between {left_freq/1e9:.6f} GHz and {right_freq/1e9:.6f} GHz"


def estimate_hpbw_and_directivity(phi_deg: Sequence[float], logmag_db: Sequence[float]) -> Tuple[float, float]:
    """
    Python version of the uploaded Directivity.m logic.

    Returns
    - directivity_estimate
    - hpbw_deg

    Notes
    - This intentionally follows the old Matlab approach closely.
    - It looks for the first -3.01 dB crossing on each side of the peak.
    - If the half-power points are not in the data, returns NaN.
    """
    phi = np.array(phi_deg, dtype=float)
    mag = np.array(logmag_db, dtype=float)

    valid = np.isfinite(phi) & np.isfinite(mag)
    phi = phi[valid]
    mag = mag[valid]

    if len(phi) < 3:
        return math.nan, math.nan

    max_index = int(np.nanargmax(mag))
    maximum = float(mag[max_index])
    target = maximum - 3.01

    # Walk left from the peak until we drop below the -3.01 dB point.
    left_index: Optional[int] = None
    for i in range(max_index, -1, -1):
        if mag[i] < target:
            left_index = i
            break

    # Walk right from the peak until we drop below the -3.01 dB point.
    right_index: Optional[int] = None
    for i in range(max_index, len(mag)):
        if mag[i] < target:
            right_index = i
            break

    if left_index is None or right_index is None:
        return math.nan, math.nan

    hpbw_deg = abs(float(phi[right_index] - phi[left_index]))
    hpbw_rad = math.radians(hpbw_deg)

    beam_solid_angle = 2 * math.pi * (1 - math.cos(hpbw_rad / 2.0))
    if beam_solid_angle <= 0:
        return math.nan, math.nan

    directivity = 4 * math.pi / beam_solid_angle
    directivity = directivity / 2.0  # Preserve the legacy Matlab behavior.

    if directivity > 100:
        directivity = math.nan

    return float(directivity), float(hpbw_deg)


# -----------------------------------------------------------------------------
# Simulation instruments
# -----------------------------------------------------------------------------
class FakePositioner:
    """Simple software-only positioner so the GUI can be tested without hardware."""

    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        self.current_deg = 0.0
        self.connected = False

    def connect(self) -> str:
        self.connected = True
        return f"SIMULATED POSITIONER ({self.resource_name})"

    def close(self) -> None:
        self.connected = False

    def current_angle_deg(self) -> float:
        return self.current_deg

    def move_absolute_deg(self, target_deg: float, wait: bool = True) -> None:
        target_deg = clamp(target_deg, POSITIONER_MIN_DEG, POSITIONER_MAX_DEG)
        self.current_deg = target_deg
        if wait:
            time.sleep(0.05)

    def jog_deg(self, delta_deg: float) -> None:
        self.move_absolute_deg(self.current_deg + delta_deg, wait=True)

    def go_zero(self) -> None:
        self.move_absolute_deg(0.0, wait=True)

    def set_zero_here(self) -> None:
        self.current_deg = 0.0

    def start_cw_continuous(self) -> None:
        self.current_deg = clamp(self.current_deg + 5.0, POSITIONER_MIN_DEG, POSITIONER_MAX_DEG)

    def start_ccw_continuous(self) -> None:
        self.current_deg = clamp(self.current_deg - 5.0, POSITIONER_MIN_DEG, POSITIONER_MAX_DEG)

    def stop(self) -> None:
        pass


class FakeVNA:
    """Simple software-only VNA so the GUI can be tested without hardware."""

    model_name = "Simulation"

    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        self.connected = False
        self.freq_hz = DEFAULT_FREQUENCY_GHZ * 1e9
        self.use_correction = True
        self.current_measurement = "S21"
        self.current_format = "LOGM"
        self.hold_enabled = True

    def connect(self) -> str:
        self.connected = True
        return f"SIMULATED VNA ({self.resource_name})"

    def close(self) -> None:
        self.connected = False

    def idn(self) -> str:
        return f"SIMULATED VNA ({self.resource_name})"

    def get_calibration_type(self) -> str:
        return "SIM"

    def get_correction_state(self) -> str:
        return "ON" if self.use_correction else "OFF"

    def preset(self) -> None:
        self.current_measurement = "S21"
        self.current_format = "LOGM"

    def set_hold(self, hold: bool) -> None:
        self.hold_enabled = hold

    def trigger_single(self) -> None:
        pass

    def set_measurement(self, measurement: str) -> None:
        self.current_measurement = measurement.upper()

    def set_display_format(self, display_format: str) -> None:
        self.current_format = display_format.upper()

    def send_manual(self, command: str) -> None:
        _ = command

    def query_manual(self, command: str) -> str:
        cmd = command.strip().upper()
        if cmd in {"*IDN?", "IDN?"}:
            return self.idn()
        return "SIM_OK"

    def configure_cw_s21(self, freq_hz: float, use_correction: bool = True) -> None:
        self.freq_hz = freq_hz
        self.use_correction = use_correction
        self.current_measurement = "S21"
        self.current_format = "LOGM"

    def single_s21_reading(self, current_angle_deg: float = 0.0) -> Tuple[float, float]:
        # A fake antenna pattern with a main lobe at boresight plus small side behavior.
        theta = math.radians(current_angle_deg)
        base = abs(math.cos(theta)) ** 6
        back_lobe = 0.08 * abs(math.cos(theta - math.pi)) ** 2
        side_term = 0.05 * abs(math.sin(2 * theta)) ** 3
        linear_mag = max(1e-6, base + back_lobe + side_term)

        # Make the absolute level look like a chamber measurement.
        logmag_db = -70.0 + 20.0 * math.log10(linear_mag)
        logmag_db += np.random.normal(scale=0.25)

        phase_deg = 35.0 * math.sin(theta) + 12.0 * math.sin(2.0 * theta)
        phase_deg += np.random.normal(scale=1.2)

        # Wrap phase into [-180, +180].
        while phase_deg > 180.0:
            phase_deg -= 360.0
        while phase_deg < -180.0:
            phase_deg += 360.0

        return float(logmag_db), float(phase_deg)


# -----------------------------------------------------------------------------
# Real hardware classes
# -----------------------------------------------------------------------------
class FS121Positioner:
    """
    Sunol Sciences FS-121 positioner.

    Important details from the uploaded operation page:
    - 9600-8-N-1 serial config
    - uppercase ASCII commands
    - X-1? reports current location
    - 80 microsteps / degree
    """

    def __init__(self, rm: "pyvisa.ResourceManager", resource_name: str):
        self.rm = rm
        self.resource_name = resource_name
        self.inst = None
        # Some controllers do not reliably answer X-1? through every VISA stack.
        # We keep a software-side position estimate so the GUI can still operate
        # instead of failing the entire connection just because readback is blank.
        self.last_known_steps = 0
        self.has_position_feedback = False

    def connect(self) -> str:
        if pyvisa is None:
            raise RuntimeError("PyVISA is not installed.")

        self.inst = self.rm.open_resource(self.resource_name)

        # Serial settings from the FS-121 page.
        self.inst.baud_rate = 9600
        self.inst.data_bits = 8
        self.inst.parity = visa_constants.Parity.none
        self.inst.stop_bits = visa_constants.StopBits.one
        self.inst.timeout = POSITIONER_TIMEOUT_MS
        self.inst.write_termination = "\r"
        self.inst.read_termination = "\r"
        self.inst.chunk_size = 1024

        try:
            self.inst.clear()
        except Exception:
            pass

        # Give the controller a simple known state.
        self._write("4!")
        time.sleep(0.25)
        self._write(POSITIONER_IDLE_POWER_COMMAND)
        self._write(f"{POSITIONER_SPEED_R_COMMAND}R")
        self._write(f"{POSITIONER_ACCEL_P_COMMAND}P")
        if POSITIONER_DISABLE_HOME_SWITCH:
            self._write("4T")

        # Try to read position feedback, but do not fail the whole connection if the
        # controller only returns a terminal-style wake-up character such as '*', or
        # if it returns nothing. Some FS-121 setups behave this way.
        steps = self._try_get_current_steps()
        if steps is None:
            self.has_position_feedback = False
            return f"FS-121 ({self.resource_name}) connected (position readback unavailable)"

        self.last_known_steps = steps
        self.has_position_feedback = True
        return f"FS-121 ({self.resource_name}) @ {steps/POSITIONER_STEPS_PER_DEG:.3f} deg"

    def close(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception:
                pass
        self.inst = None

    def _write(self, command: str) -> None:
        if self.inst is None:
            raise RuntimeError("Positioner not connected.")
        self.inst.write(command.strip().upper())

    def _query(self, command: str) -> str:
        if self.inst is None:
            raise RuntimeError("Positioner not connected.")

        cmd = command.strip().upper()

        # Clear stale bytes first when supported.
        try:
            self.inst.clear()
        except Exception:
            pass

        # Try a few common serial reply styles. The FS-121 docs show simple
        # terminal-style operation, and different VISA stacks can expose the
        # returned line ending as CR, LF, CRLF, or raw bytes.
        terminations = ["\n", "\r", "\r\n", None]
        last_error = None

        for term in terminations:
            try:
                if term is not None:
                    self.inst.read_termination = term
                    response = self.inst.query(cmd)
                    response = response.strip("\r\n \t\x00")
                    if response:
                        return response
                else:
                    # Raw fallback: write then read whatever arrives before timeout.
                    self.inst.write(cmd)
                    time.sleep(0.10)
                    raw = self.inst.read_bytes(128, break_on_termchar=False)
                    response = raw.decode(errors="ignore").strip("\r\n \t\x00")
                    if response:
                        return response
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise RuntimeError(f"No usable reply to {cmd!r} from positioner. Last error: {last_error}")
        raise RuntimeError(f"No usable reply to {cmd!r} from positioner.")

    @staticmethod
    def _extract_steps_from_text(response: str) -> Optional[int]:
        """Pull X-1,<steps> out of a noisy serial response if it is present."""
        cleaned = (response or "").strip("\r\n \t\x00")
        if not cleaned:
            return None

        # Expected format from the uploaded FS-121 page: X-1,10
        match = re.search(r"X-1\s*,\s*(-?\d+)", cleaned)
        if match:
            return int(match.group(1))

        # Sometimes just the numeric part can slip through a buggy serial stack.
        if re.fullmatch(r"-?\d+", cleaned):
            return int(cleaned)

        return None

    def _try_get_current_steps(self) -> Optional[int]:
        """Best-effort read of the current step count. Returns None on blank/'*'/noise."""
        try:
            response = self._query("X-1?")
        except Exception:
            return None

        steps = self._extract_steps_from_text(response)
        if steps is not None:
            self.last_known_steps = steps
            self.has_position_feedback = True
            return steps

        # A plain '*' or blank reply is treated as "controller is alive, but there is
        # no usable position readback right now" instead of a hard failure.
        if (response or "").strip() in {"", "*"}:
            return None

        # Try one more time in case the first read only returned a wake-up character.
        try:
            response2 = self._query("X-1?")
            steps = self._extract_steps_from_text(response2)
            if steps is not None:
                self.last_known_steps = steps
                self.has_position_feedback = True
                return steps
        except Exception:
            pass

        return None

    def current_steps(self) -> int:
        steps = self._try_get_current_steps()
        if steps is not None:
            return steps
        return int(self.last_known_steps)

    def current_angle_deg(self) -> float:
        return self.current_steps() / POSITIONER_STEPS_PER_DEG

    def move_absolute_deg(self, target_deg: float, wait: bool = True) -> None:
        target_deg = clamp(target_deg, POSITIONER_MIN_DEG, POSITIONER_MAX_DEG)
        target_steps = int(round(target_deg * POSITIONER_STEPS_PER_DEG))
        previous_steps = int(self.last_known_steps)
        self._write(f"{target_steps}G")

        # Update our software-side estimate immediately. If live feedback is working,
        # later reads will refine this. If live feedback is not working, the GUI can
        # still operate in open-loop mode.
        self.last_known_steps = target_steps

        if not wait:
            return

        if not self.has_position_feedback:
            # Open-loop fallback: estimate time from rpm and move size, then continue.
            delta_steps = abs(target_steps - previous_steps)
            microsteps_per_second = float(POSITIONER_SPEED_R_COMMAND)
            if microsteps_per_second <= 0:
                estimated_s = DEFAULT_SETTLE_S
            else:
                estimated_s = delta_steps / microsteps_per_second
            time.sleep(min(max(estimated_s + DEFAULT_SETTLE_S, DEFAULT_SETTLE_S), POSITIONER_MOVE_TIMEOUT_S))
            return

        start = time.time()
        while time.time() - start < POSITIONER_MOVE_TIMEOUT_S:
            current_steps = self.current_steps()
            if abs(current_steps - target_steps) <= 1:
                return
            time.sleep(POSITIONER_POLL_S)

        # Stop motion if it never settled.
        try:
            self.stop()
        finally:
            raise TimeoutError(f"Positioner move timed out going to {target_deg:.3f} deg.")

    def jog_deg(self, delta_deg: float) -> None:
        self.move_absolute_deg(self.current_angle_deg() + delta_deg, wait=True)

    def go_zero(self) -> None:
        self.move_absolute_deg(0.0, wait=True)

    def set_zero_here(self) -> None:
        self._write("0=")
        self.last_known_steps = 0
        time.sleep(0.10)

    def start_cw_continuous(self) -> None:
        self._write("+S")

    def start_ccw_continuous(self) -> None:
        self._write("-S")

    def stop(self) -> None:
        self._write("Z")


def open_message_resource_no_clear(rm: "pyvisa.ResourceManager", resource_name: str):
    """
    Open a VISA message-based resource without going through open_resource().

    Why this exists
    - Some PyVISA versions perform a viClear during open_resource().
    - On this chamber's HP 8753ES setup, that open-time clear can time out even
      though the instrument is otherwise reachable.

    Using open_bare_resource() opens the session without that extra clear step,
    then we attach that live session to the most appropriate PyVISA wrapper
    class so normal write/read calls still work.
    """
    if pyvisa is None:
        raise RuntimeError("PyVISA is not installed.")

    resource_pyclass = None
    try:
        info = rm.resource_info(resource_name, extended=True)
    except Exception:
        info = None

    if info is not None:
        try:
            resource_pyclass = rm._resource_classes[(info.interface_type, info.resource_class)]
        except Exception:
            resource_pyclass = None

    if resource_pyclass is None:
        resources_mod = getattr(pyvisa, "resources", None)
        for class_name in ("GPIBInstrument", "MessageBasedResource", "Resource"):
            if resources_mod is None:
                break
            candidate = getattr(resources_mod, class_name, None)
            if candidate is not None:
                resource_pyclass = candidate
                break

    if resource_pyclass is None:
        raise RuntimeError("Could not determine a PyVISA resource class for the VNA.")

    try:
        bare = rm.open_bare_resource(resource_name)
        session = bare[0] if isinstance(bare, tuple) else bare
    except Exception as exc:
        raise RuntimeError(
            f"Could not open {resource_name!r} with open_bare_resource(). {exc}"
        ) from exc

    inst = resource_pyclass(rm, resource_name)
    inst.session = session

    try:
        rm._created_resources.add(inst)
    except Exception:
        pass

    return inst


class BaseVNA:
    """Common helpers for supported VNAs."""

    model_name = "Generic VNA"

    def __init__(self, rm: "pyvisa.ResourceManager", resource_name: str):
        self.rm = rm
        self.resource_name = resource_name
        self.inst = None

    def connect(self) -> str:
        if pyvisa is None:
            raise RuntimeError("PyVISA is not installed.")
        self.inst = self.rm.open_resource(self.resource_name)
        self.inst.timeout = VNA_TIMEOUT_MS
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self.inst.chunk_size = 1024 * 1024
        try:
            self.inst.clear()
        except Exception:
            pass
        return self.idn()

    def close(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception:
                pass
        self.inst = None

    def _write(self, command: str) -> None:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")
        self.inst.write(command.strip())

    def _query_text(self, command: str) -> str:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")
        return self.inst.query(command.strip()).strip()

    @contextmanager
    def temporary_timeout(self, timeout_ms: int):
        if self.inst is None:
            raise RuntimeError("VNA not connected.")
        old_timeout = self.inst.timeout
        self.inst.timeout = timeout_ms
        try:
            yield
        finally:
            try:
                self.inst.timeout = old_timeout
            except Exception:
                pass

    def clear_io(self) -> None:
        if self.inst is None:
            return
        try:
            self.inst.clear()
        except Exception:
            pass

    @staticmethod
    def _clean_text_response(text: str) -> str:
        return (text or "").strip("\r\n \t\x00")

    def _query_one_of(self, commands: Sequence[str]) -> str:
        last_error = None
        for command in commands:
            try:
                return self._query_text(command)
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("No query commands were provided.")

    def _try_write(self, commands: Sequence[str]) -> None:
        last_error = None
        for command in commands:
            try:
                self._write(command)
                return
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("No write commands were provided.")

    def _read_text_command(self, command: str) -> str:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")
        self.inst.write(command.strip())
        return self.inst.read().strip()

    def _read_raw_command(self, command: str) -> bytes:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")
        self.inst.write(command.strip())
        return self.inst.read_raw()

    @staticmethod
    def _parse_ascii_values(text: str) -> List[float]:
        cleaned = text.replace(";", ",").replace("\r", ",").replace("\n", ",").strip(" ,")
        if not cleaned:
            return []
        return [float(part.strip()) for part in cleaned.split(",") if part.strip()]

    def idn(self) -> str:
        return self._query_one_of(["IDN?", "*IDN?"])

    def wait_opc(self) -> None:
        try:
            self._query_one_of(["OPC?", "*OPC?"])
        except Exception:
            time.sleep(0.25)

    def preset(self) -> None:
        self._try_write(["PRES", "*RST", "*CLS"])

    def get_calibration_type(self) -> str:
        return "Manual / Unknown"

    def get_correction_state(self) -> str:
        return "Unknown"

    def set_hold(self, hold: bool) -> None:
        raise NotImplementedError

    def trigger_single(self) -> None:
        raise NotImplementedError

    def set_measurement(self, measurement: str) -> None:
        raise NotImplementedError

    def set_display_format(self, display_format: str) -> None:
        raise NotImplementedError

    def configure_cw_s21(self, freq_hz: float, use_correction: bool = True) -> None:
        raise NotImplementedError

    def single_s21_reading(self, current_angle_deg: float = 0.0) -> Tuple[float, float]:
        raise NotImplementedError

    def send_manual(self, command: str) -> None:
        self._write(command)

    def query_manual(self, command: str) -> str:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")
        try:
            self.clear_io()
            with self.temporary_timeout(MANUAL_VNA_QUERY_TIMEOUT_MS):
                return self._query_text(command)
        except Exception as exc:
            self.clear_io()
            raise RuntimeError(
                f"Query failed for {command!r}. The VNA did not return a complete response before timeout."
            ) from exc


class HP8753ESVNA(BaseVNA):
    """Simple HP 8753ES support focused on one-point S21 chamber measurements."""

    model_name = "HP 8753ES"

    def __init__(self, rm: "pyvisa.ResourceManager", resource_name: str):
        super().__init__(rm, resource_name)
        self._configured_freq_hz: Optional[float] = None
        self._configured_measurement: Optional[str] = None
        self._configured_format: Optional[str] = None
        self._hold_enabled = False
        self._identity_text = f"HP 8753ES ({self.resource_name})"

    def connect(self) -> str:
        if pyvisa is None:
            raise RuntimeError("PyVISA is not installed.")
        self.inst = open_message_resource_no_clear(self.rm, self.resource_name)
        self.inst.timeout = VNA_TIMEOUT_MS
        # For this bench 8753ES, the safest path is raw HP-IB writes with EOI and
        # no automatic text terminator added by PyVISA.
        self.inst.write_termination = ""
        self.inst.read_termination = ""
        self.inst.chunk_size = 1024 * 1024
        try:
            self.inst.send_end = True
        except Exception:
            pass
        try:
            self.inst.query_delay = HP8753_QUERY_DELAY_S
        except Exception:
            pass
        # Do not require an ID query or viClear during connect. The bench 8753ES
        # can stall on those even though normal write/read commands still work.
        return self._identity_text

    def _recover_io(self) -> None:
        # Do not use viClear on the 8753ES path. On some VISA stacks that is the
        # exact operation that times out during connect and after failed reads.
        return

    def _write(self, command: str) -> None:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")

        cmd = command.strip()
        if not cmd:
            return

        errors: list[str] = []
        old_write_termination = getattr(self.inst, "write_termination", None)

        with self.temporary_timeout(HP8753_WRITE_TIMEOUT_MS):
            try:
                self.inst.write_termination = ""
            except Exception:
                pass

            try:
                raw_writer = getattr(self.inst, "write_raw", None)
                if raw_writer is not None:
                    raw_writer(cmd.encode("ascii", errors="ignore"))
                else:
                    self.inst.write(cmd)
                time.sleep(HP8753_QUERY_DELAY_S)
                return
            except Exception as exc:
                errors.append(f"write_raw/no-term write: {exc}")
            finally:
                try:
                    self.inst.write_termination = old_write_termination
                except Exception:
                    pass

        raise RuntimeError(f"HP 8753 write failed for {cmd!r}. {' | '.join(errors)}")

    def _read_text_reply(self) -> str:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")

        errors: list[str] = []
        old_read_termination = getattr(self.inst, "read_termination", None)

        try:
            self.inst.read_termination = ""
        except Exception:
            pass

        try:
            raw = self.inst.read_raw()
            text = self._clean_text_response(raw.decode(errors="ignore"))
            if text:
                return text
        except Exception as exc:
            errors.append(f"read_raw: {exc}")

        try:
            self.inst.read_termination = "\n"
            text = self._clean_text_response(self.inst.read())
            if text:
                return text
        except Exception as exc:
            errors.append(f"read line: {exc}")
        finally:
            try:
                self.inst.read_termination = old_read_termination
            except Exception:
                pass

        raise RuntimeError(" | ".join(errors) if errors else "no reply returned")

    def _readback_text(self, command: str, timeout_ms: int, use_query_first: bool) -> str:
        if self.inst is None:
            raise RuntimeError("VNA not connected.")

        cmd = command.strip()
        if not cmd:
            raise ValueError("Enter a VNA command first.")

        _ = use_query_first
        errors: list[str] = []

        with self.temporary_timeout(timeout_ms):
            candidates = [cmd]
            if cmd.upper() in {"IDN?", "*IDN?"}:
                candidates.insert(0, "OUTPIDEN")

            for candidate in candidates:
                try:
                    self._write(candidate)
                    reply = self._read_text_reply()
                    if reply:
                        return reply
                except Exception as exc:
                    errors.append(f"{candidate}: {exc}")
                    self._recover_io()

        joined = " | ".join(errors) if errors else "no reply returned"
        raise RuntimeError(f"No response returned for {command!r}. {joined}")

    def _query_text(self, command: str) -> str:
        return self._readback_text(command, MANUAL_VNA_QUERY_TIMEOUT_MS, use_query_first=True)

    def idn(self) -> str:
        errors: list[str] = []
        for command in ("OUTPIDEN", "IDN?", "*IDN?"):
            try:
                reply = self._readback_text(command, HP8753_IDN_TIMEOUT_MS, use_query_first=True)
                if reply:
                    self._identity_text = reply
                    return reply
            except Exception as exc:
                errors.append(f"{command}: {exc}")
        raise RuntimeError("HP 8753 ID query failed. " + " | ".join(errors))

    def query_manual(self, command: str) -> str:
        cmd = command.strip()
        if not cmd:
            raise ValueError("Enter a VNA command first.")
        if cmd.upper() in {"IDN?", "*IDN?", "OUTPIDEN"}:
            return self.idn()
        use_query_first = cmd.endswith("?")
        return self._readback_text(cmd, MANUAL_VNA_QUERY_TIMEOUT_MS, use_query_first=use_query_first)

    def wait_opc(self) -> None:
        # The old code queried OPC? after every SING. On this analyzer that can
        # cost a full VISA timeout per sweep when the reply is slow or absent.
        # A short fixed wait is much simpler and keeps scans responsive.
        time.sleep(HP8753_SWEEP_WAIT_S)

    def preset(self) -> None:
        self._write("PRES")
        self._configured_freq_hz = None
        self._configured_measurement = None
        self._configured_format = None
        self._hold_enabled = False

    def get_calibration_type(self) -> str:
        return "Manual on HP 8753"

    def get_correction_state(self) -> str:
        return "Manual on HP 8753"

    def set_hold(self, hold: bool) -> None:
        if self._hold_enabled == hold:
            return
        self._write("HOLD" if hold else "CONT")
        self._hold_enabled = hold

    def trigger_single(self) -> None:
        self._write("SING")
        self.wait_opc()

    def set_measurement(self, measurement: str) -> None:
        measurement = measurement.upper().strip()
        mapping = {
            "S11": "RFLP",
            "S21": "TRAP",
        }
        if measurement not in mapping:
            raise ValueError(f"HP 8753 shortcut not implemented for {measurement}.")
        if self._configured_measurement == measurement:
            return
        self._write(mapping[measurement])
        self._configured_measurement = measurement

    def set_display_format(self, display_format: str) -> None:
        display_format = display_format.upper().strip()
        mapping = {
            "LOGM": "LOGM",
            "PHAS": "PHAS",
            "LINM": "LINM",
        }
        if display_format not in mapping:
            raise ValueError(f"HP 8753 display format shortcut not implemented for {display_format}.")
        if self._configured_format == display_format:
            return
        self._write(mapping[display_format])
        self._configured_format = display_format
        time.sleep(HP8753_FORMAT_SETTLE_S)

    def set_single_point_frequency(self, freq_hz: float) -> None:
        if self._configured_freq_hz is not None and math.isclose(self._configured_freq_hz, freq_hz, rel_tol=0.0, abs_tol=1.0):
            return
        numeric = f"{freq_hz:.12g}"
        self._write(f"STAR {numeric}")
        self._write(f"STOP {numeric}")
        try:
            self._write("POIN 1")
        except Exception:
            pass
        self._configured_freq_hz = float(freq_hz)

    def _get_first_display_value(self) -> float:
        self._write("FORM4")
        time.sleep(HP8753_QUERY_DELAY_S)
        text = self._readback_text("OUTPFORM", MANUAL_VNA_QUERY_TIMEOUT_MS, use_query_first=False)
        values = self._parse_ascii_values(text)
        if not values:
            raise RuntimeError(f"HP 8753 returned no OUTPFORM values: {text!r}")
        return float(values[0])

    def configure_cw_s21(self, freq_hz: float, use_correction: bool = True) -> None:
        _ = use_correction
        self.set_measurement("S21")
        self.set_single_point_frequency(freq_hz)
        self.set_display_format("LOGM")
        self.set_hold(True)

    def single_s21_reading(self, current_angle_deg: float = 0.0) -> Tuple[float, float]:
        _ = current_angle_deg
        self.set_display_format("LOGM")
        self.trigger_single()
        mag_db = self._get_first_display_value()
        self.set_display_format("PHAS")
        self.trigger_single()
        phase_deg = self._get_first_display_value()
        self.set_display_format("LOGM")
        return float(mag_db), float(phase_deg)


class Anritsu37xxxDVNA(BaseVNA):
    """Anritsu 37XXXD-style VNA control for one-point S21 work."""

    model_name = "Anritsu 37XXXD"

    def idn(self) -> str:
        return self._query_one_of(["*IDN?", "IDN?"])

    def get_calibration_type(self) -> str:
        try:
            return self._query_text("CXX?")
        except Exception:
            return "?"

    def get_correction_state(self) -> str:
        try:
            state = self._query_text("CON?")
            return "ON" if state.strip() == "1" else "OFF"
        except Exception:
            return "?"

    def set_hold(self, hold: bool) -> None:
        if hold:
            self._write("HLD")

    def trigger_single(self) -> None:
        self._write("TRS")
        self._write("WFS")
        self._write("HLD")
        self.wait_opc()

    def set_measurement(self, measurement: str) -> None:
        measurement = measurement.upper().strip()
        if measurement not in {"S11", "S21"}:
            raise ValueError(f"Anritsu shortcut not implemented for {measurement}.")
        for channel in (VNA_MARKER_CHANNEL_MAG, VNA_MARKER_CHANNEL_PHASE):
            self._write(f"CH{channel};{measurement}")

    def set_display_format(self, display_format: str) -> None:
        display_format = display_format.upper().strip()
        mapping = {"LOGM": "MAG", "PHAS": "PHA"}
        if display_format not in mapping:
            raise ValueError(f"Anritsu display format shortcut not implemented for {display_format}.")
        self._write(f"CH{VNA_MARKER_CHANNEL_MAG};{mapping[display_format]}")

    def configure_cw_s21(self, freq_hz: float, use_correction: bool = True) -> None:
        self._write("*CLS")
        self._write("CON" if use_correction else "COF")
        self._write("FMA")
        self._write("D14")
        self._write(f"CWF {freq_hz:.6f} HZ")
        self._write("CWP 1")
        self._write(f"CH{VNA_MARKER_CHANNEL_MAG};S21;MAG;MR1")
        self._write(f"CH{VNA_MARKER_CHANNEL_MAG};MK1 {freq_hz:.6f} HZ")
        self._write(f"CH{VNA_MARKER_CHANNEL_PHASE};S21;PHA;MR1")
        self._write(f"CH{VNA_MARKER_CHANNEL_PHASE};MK1 {freq_hz:.6f} HZ")
        self._write("HLD")

    @staticmethod
    def _parse_arbitrary_block_ascii(raw: bytes) -> List[float]:
        raw = raw.strip()
        if not raw:
            return []
        if not raw.startswith(b"#"):
            text = raw.decode(errors="ignore")
            return BaseVNA._parse_ascii_values(text)
        if len(raw) < 3:
            raise RuntimeError("Arbitrary block was too short.")
        num_count_digits = int(chr(raw[1]))
        count_start = 2
        count_stop = 2 + num_count_digits
        byte_count = int(raw[count_start:count_stop].decode(errors="ignore"))
        data_start = count_stop
        data_stop = data_start + byte_count
        data_text = raw[data_start:data_stop].decode(errors="ignore")
        return BaseVNA._parse_ascii_values(data_text)

    def single_s21_reading(self, current_angle_deg: float = 0.0) -> Tuple[float, float]:
        _ = current_angle_deg
        self.trigger_single()
        try:
            mag_text = self._read_text_command(f"CH{VNA_MARKER_CHANNEL_MAG};OM1")
            phase_text = self._read_text_command(f"CH{VNA_MARKER_CHANNEL_PHASE};OM1")
            mag_vals = self._parse_ascii_values(mag_text)
            phase_vals = self._parse_ascii_values(phase_text)
            if not mag_vals or not phase_vals:
                raise RuntimeError("Empty marker result.")
            return float(mag_vals[0]), float(phase_vals[0])
        except Exception:
            raw = self._read_raw_command("OS21C")
            vals = self._parse_arbitrary_block_ascii(raw)
            if len(vals) < 2:
                raise RuntimeError(f"Could not parse OS21C output. Raw bytes length={len(raw)}")
            re_part = float(vals[0])
            im_part = float(vals[1])
            complex_s21 = complex(re_part, im_part)
            mag_linear = abs(complex_s21)
            logmag_db = -200.0 if mag_linear <= 0 else 20.0 * math.log10(mag_linear)
            phase_deg = math.degrees(math.atan2(complex_s21.imag, complex_s21.real))
            return float(logmag_db), float(phase_deg)


def connect_vna(rm: "pyvisa.ResourceManager", resource_name: str, log_cb) -> tuple[BaseVNA, str]:
    """Open the one VNA model used by this chamber."""
    vna = HP8753ESVNA(rm, resource_name)
    vna_name = vna.connect()
    log_cb("Using HP 8753ES driver.")
    return vna, vna_name


# -----------------------------------------------------------------------------
# GUI application
# -----------------------------------------------------------------------------
class AMSApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AMS - Anechoic Chamber Radiation Pattern Scan")
        self.root.geometry("1580x930")
        self.root.minsize(1320, 820)
        self.root.configure(bg="#0d1117")

        # Thread-safe queue for background scan updates.
        self.ui_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.scan_thread: Optional[threading.Thread] = None
        self.calibration_thread: Optional[threading.Thread] = None
        self.manual_thread: Optional[threading.Thread] = None
        self.connect_thread: Optional[threading.Thread] = None
        self.instrument_lock = threading.Lock()

        # Instrument handles.
        self.rm = None
        self.positioner = None
        self.vna = None
        self.connected = False

        # Current scan data.
        self.scan_phi: List[float] = []
        self.scan_mag: List[float] = []
        self.scan_phase: List[float] = []

        self.last_text_file: Optional[Path] = None
        self.last_plot_file: Optional[Path] = None
        self.calibration_profile: Optional[CalibrationProfile] = None
        self.raw_plot_ylabel = "Log Magnitude (dB)"
        self.raw_plot_title = "S21 vs Phi"
        self.current_value_prefix = "LogMag"
        self.current_value_units = "dB"

        self._build_vars()
        self._build_style()
        self._build_ui()
        self._set_state("disconnected")
        self._load_saved_calibration_profile(log_on_missing=False)

        self.log("AMS started.")
        self.log("Tip: set boresight to 0 deg before running a scan.")
        if self.calibration_profile is not None:
            self.log(f"Loaded saved chamber calibration from {CALIBRATION_PROFILE_PATH}.")

        self.root.after(100, self.process_ui_queue)

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------
    def _build_vars(self) -> None:
        self.positioner_resource_var = tk.StringVar(value=DEFAULT_POSITIONER_RESOURCE)
        self.vna_resource_var = tk.StringVar(value=DEFAULT_VNA_RESOURCE)
        self.visa_backend_var = tk.StringVar(value=DEFAULT_VISA_BACKEND)
        self.vna_family_var = tk.StringVar(value=DEFAULT_VNA_FAMILY)
        self.simulation_var = tk.BooleanVar(value=DEFAULT_SIMULATION_MODE)

        self.save_folder_var = tk.StringVar(value=DEFAULT_SAVE_FOLDER)
        self.base_name_var = tk.StringVar(value=DEFAULT_BASE_NAME)
        self.frequency_ghz_var = tk.StringVar(value=str(DEFAULT_FREQUENCY_GHZ))
        self.span_deg_var = tk.StringVar(value=str(DEFAULT_SPAN_DEG))
        self.step_deg_var = tk.StringVar(value=str(DEFAULT_STEP_DEG))
        self.settle_s_var = tk.StringVar(value=str(DEFAULT_SETTLE_S))
        self.jog_deg_var = tk.StringVar(value=str(DEFAULT_JOG_DEG))
        self.move_angle_deg_var = tk.StringVar(value="0.0")
        self.vna_manual_command_var = tk.StringVar(value="IDN?")
        self.use_correction_var = tk.BooleanVar(value=DEFAULT_USE_CORRECTION)
        self.use_chamber_calibration_var = tk.BooleanVar(value=DEFAULT_USE_CHAMBER_CALIBRATION)
        self.cal_start_ghz_var = tk.StringVar(value=str(DEFAULT_CAL_START_GHZ))
        self.cal_stop_ghz_var = tk.StringVar(value=str(DEFAULT_CAL_STOP_GHZ))
        self.cal_step_ghz_var = tk.StringVar(value=str(DEFAULT_CAL_STEP_GHZ))
        self.cal_tx_gain_var = tk.StringVar(value=str(DEFAULT_CAL_TX_GAIN_DBI))
        self.cal_rx_gain_var = tk.StringVar(value=str(DEFAULT_CAL_RX_GAIN_DBI))
        self.scan_tx_gain_var = tk.StringVar(value=str(DEFAULT_CAL_TX_GAIN_DBI))

        self.state_text_var = tk.StringVar(value="Disconnected")
        self.progress_text_var = tk.StringVar(value="0 / 0")
        self.current_angle_var = tk.StringVar(value="0.000 deg")
        self.last_reading_var = tk.StringVar(value="LogMag: -- dB   Phase: -- deg")
        self.hpbw_var = tk.StringVar(value="HPBW: --")
        self.directivity_var = tk.StringVar(value="Directivity: --")
        self.text_file_var = tk.StringVar(value="Text file: --")
        self.plot_file_var = tk.StringVar(value="Plot file: --")
        self.last_scan_mode_var = tk.StringVar(value="Last scan mode: --")
        self.calibration_status_var = tk.StringVar(
            value=f"No saved chamber calibration loaded.\nProfile path: {CALIBRATION_PROFILE_PATH}"
        )
        self.instrument_info_var = tk.StringVar(value="No instruments connected")

    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        bg = "#0d1117"
        panel = "#161b22"
        panel_2 = "#1f2630"
        text = "#e6edf3"
        muted = "#9da7b3"
        accent = "#2f81f7"

        style.configure("Root.TFrame", background=bg)
        style.configure("Card.TFrame", background=panel)
        style.configure("Inner.TFrame", background=panel_2)
        style.configure("Card.TLabelframe", background=panel, foreground=text)
        style.configure("Card.TLabelframe.Label", background=panel, foreground=text, font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background=panel, foreground=text, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=panel, foreground=muted, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=bg, foreground=text, font=("Segoe UI", 18, "bold"))
        style.configure("SubHeader.TLabel", background=bg, foreground=muted, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=8)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=10)
        style.map("Accent.TButton", background=[("active", accent), ("!disabled", accent)], foreground=[("!disabled", "white")])
        style.configure("TEntry", fieldbackground="#0f141b", foreground=text, insertcolor=text)
        style.configure("TCheckbutton", background=panel, foreground=text)
        style.configure("Horizontal.TProgressbar", troughcolor="#0f141b", background=accent, bordercolor="#0f141b")

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, style="Root.TFrame", padding=12)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=0)
        root_frame.columnconfigure(1, weight=1)
        root_frame.rowconfigure(1, weight=1)
        root_frame.rowconfigure(2, weight=0)

        header = ttk.Frame(root_frame, style="Root.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Anechoic Chamber AMS", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="One-screen radiation pattern scan GUI • positioner + VNA • debug-friendly",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # Left column: controls in a scrollable panel so manual controls and
        # test buttons remain visible on smaller displays.
        controls_panel = ttk.Frame(root_frame, style="Root.TFrame")
        controls_panel.grid(row=1, column=0, sticky="nsw", padx=(0, 12))
        controls_panel.rowconfigure(0, weight=1)
        controls_panel.columnconfigure(0, weight=1)

        self.controls_canvas = tk.Canvas(
            controls_panel,
            width=420,
            highlightthickness=0,
            bd=0,
            bg="#0d1117",
        )
        self.controls_canvas.grid(row=0, column=0, sticky="nsw")
        controls_scrollbar = ttk.Scrollbar(controls_panel, orient="vertical", command=self.controls_canvas.yview)
        controls_scrollbar.grid(row=0, column=1, sticky="ns")
        self.controls_canvas.configure(yscrollcommand=controls_scrollbar.set)

        controls = ttk.Frame(self.controls_canvas, style="Root.TFrame")
        self.controls_canvas_window = self.controls_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", self._on_controls_configure)
        self.controls_canvas.bind("<Configure>", self._on_controls_canvas_configure)
        self.controls_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # Right column: plots.
        plots = ttk.Frame(root_frame, style="Root.TFrame")
        plots.grid(row=1, column=1, sticky="nsew")
        plots.columnconfigure(0, weight=1)
        plots.columnconfigure(1, weight=1)
        plots.rowconfigure(0, weight=1)

        # Bottom row: debug panel.
        debug = ttk.Frame(root_frame, style="Root.TFrame")
        debug.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        debug.columnconfigure(0, weight=1)

        # Put the new interactive sections near the top so they are obvious.
        self._build_connection_card(controls)
        self._build_settings_card(controls)
        self._build_calibration_card(controls)
        self._build_manual_card(controls)
        self._build_run_card(controls)
        self._build_results_card(controls)

        self._build_plot_cards(plots)
        self._build_debug_card(debug)

    def _on_controls_configure(self, event=None) -> None:
        if hasattr(self, "controls_canvas"):
            self.controls_canvas.configure(scrollregion=self.controls_canvas.bbox("all"))

    def _on_controls_canvas_configure(self, event) -> None:
        if hasattr(self, "controls_canvas_window"):
            self.controls_canvas.itemconfigure(self.controls_canvas_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        if hasattr(self, "controls_canvas"):
            self.controls_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _build_connection_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Connections", style="Card.TLabelframe", padding=12)
        card.pack(fill="x", pady=(0, 10))

        self._labeled_entry(card, "Positioner VISA", self.positioner_resource_var)
        self._labeled_entry(card, "VNA VISA", self.vna_resource_var)
        self._labeled_entry(card, "VISA backend", self.visa_backend_var, help_text="Blank = NI/Keysight VISA, @py = pyvisa-py")

        family_row = ttk.Frame(card, style="Card.TFrame")
        family_row.pack(fill="x", pady=(0, 8))
        ttk.Label(family_row, text="VNA family").pack(anchor="w")
        ttk.Combobox(
            family_row,
            textvariable=self.vna_family_var,
            values=("HP 8753",),
            state="readonly",
        ).pack(fill="x")

        ttk.Checkbutton(card, text="Simulation Mode", variable=self.simulation_var).pack(anchor="w", pady=(4, 0))

        btns = ttk.Frame(card, style="Card.TFrame")
        btns.pack(fill="x", pady=(10, 6))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        ttk.Button(btns, text="Connect / Test", style="Accent.TButton", command=self.connect_instruments).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(btns, text="Disconnect", command=self.disconnect_instruments).grid(row=0, column=1, sticky="ew")

        ttk.Label(card, textvariable=self.instrument_info_var, style="Muted.TLabel", wraplength=330, justify="left").pack(anchor="w")

    def _build_settings_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Scan Settings", style="Card.TLabelframe", padding=12)
        card.pack(fill="x", pady=(0, 10))

        # Save folder row.
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="Save folder").pack(anchor="w")
        row2 = ttk.Frame(card, style="Card.TFrame")
        row2.pack(fill="x", pady=(0, 8))
        ttk.Entry(row2, textvariable=self.save_folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="Browse", command=self.browse_save_folder).pack(side="left", padx=(6, 0))

        self._labeled_entry(card, "Base file name", self.base_name_var)
        self._labeled_entry(card, "CW frequency (GHz)", self.frequency_ghz_var)
        self._labeled_entry(card, "Scan span (deg)", self.span_deg_var)
        self._labeled_entry(card, "Step size (deg)", self.step_deg_var)
        self._labeled_entry(card, "Settle delay after move (s)", self.settle_s_var)
        ttk.Checkbutton(card, text="Use VNA correction if calibration is present", variable=self.use_correction_var).pack(anchor="w", pady=(6, 0))

    def _build_calibration_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Chamber Calibration", style="Card.TLabelframe", padding=12)
        card.pack(fill="x", pady=(0, 10))

        ttk.Label(
            card,
            text=(
                "Measure two identical antennas at boresight, then save K(f) = S21_ref(f) - Gtx(f) - Grx_ref(f). "
                "Scans will use G_AUT(theta,f) = S21_AUT(theta,f) - K(f) - Gtx(f)."
            ),
            style="Muted.TLabel",
            wraplength=330,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        self._labeled_entry(card, "Calibration start frequency (GHz)", self.cal_start_ghz_var)
        self._labeled_entry(card, "Calibration stop frequency (GHz)", self.cal_stop_ghz_var)
        self._labeled_entry(card, "Calibration step size (GHz)", self.cal_step_ghz_var)
        self._labeled_entry(card, "Calibration TX antenna gain Gtx_ref (dBi)", self.cal_tx_gain_var)
        self._labeled_entry(card, "Calibration RX reference gain Grx_ref (dBi)", self.cal_rx_gain_var)
        self._labeled_entry(
            card,
            "Radiation pattern TX gain Gtx (dBi)",
            self.scan_tx_gain_var,
            help_text="Used during AUT scans. Defaults to the calibration TX gain after calibration is saved.",
        )
        ttk.Checkbutton(
            card,
            text="Apply saved chamber calibration to radiation pattern scans",
            variable=self.use_chamber_calibration_var,
        ).pack(anchor="w", pady=(6, 0))

        btn_row = ttk.Frame(card, style="Card.TFrame")
        btn_row.pack(fill="x", pady=(10, 6))
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        ttk.Button(btn_row, text="Run Chamber Calibration", style="Accent.TButton", command=self.run_chamber_calibration).grid(
            row=0, column=0, sticky="ew", padx=(0, 6)
        )
        ttk.Button(btn_row, text="Clear Saved Calibration", command=self.clear_saved_calibration).grid(
            row=0, column=1, sticky="ew"
        )

        ttk.Label(card, textvariable=self.calibration_status_var, style="Muted.TLabel", wraplength=330, justify="left").pack(
            anchor="w", pady=(4, 0)
        )

    def _build_manual_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Manual Controls / Quick Tests", style="Card.TLabelframe", padding=12)
        card.pack(fill="x", pady=(0, 10))

        pos = ttk.LabelFrame(card, text="Positioner", style="Card.TLabelframe", padding=10)
        pos.pack(fill="x", pady=(0, 10))
        self._labeled_entry(pos, "Jog size (deg)", self.jog_deg_var)
        self._labeled_entry(pos, "Move to absolute angle (deg)", self.move_angle_deg_var)

        row = ttk.Frame(pos, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 4))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="CCW Continuous", command=self.positioner_ccw_continuous).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Stop", command=self.positioner_stop).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="CW Continuous", command=self.positioner_cw_continuous).grid(row=0, column=2, sticky="ew")

        row = ttk.Frame(pos, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 4))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="Jog -", command=lambda: self.manual_jog(-1)).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Read Angle", command=self.read_position_angle).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Jog +", command=lambda: self.manual_jog(+1)).grid(row=0, column=2, sticky="ew")

        row = ttk.Frame(pos, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 0))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="Go Absolute", command=self.go_to_absolute_angle).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Go To 0", command=self.go_to_zero).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Set 0 Here", command=self.zero_here).grid(row=0, column=2, sticky="ew")

        vna = ttk.LabelFrame(card, text="VNA", style="Card.TLabelframe", padding=10)
        vna.pack(fill="x")
        self._labeled_entry(vna, "Raw VNA command", self.vna_manual_command_var, help_text="Use Query for commands ending in ?")

        row = ttk.Frame(vna, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 4))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="IDN?", command=lambda: self.vna_query_command("IDN?"), style="Accent.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Send", command=self.vna_send_command).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Query", command=self.vna_query_command_from_entry).grid(row=0, column=2, sticky="ew")

        row = ttk.Frame(vna, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 4))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="Preset", command=self.vna_preset).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Single Sweep", command=self.vna_single_sweep).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Hold", command=self.vna_hold).grid(row=0, column=2, sticky="ew")

        row = ttk.Frame(vna, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 4))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="S11", command=lambda: self.vna_select_measurement("S11")).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="S21", command=lambda: self.vna_select_measurement("S21")).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Apply S21 @ Freq", command=self.vna_apply_s21_setup).grid(row=0, column=2, sticky="ew")

        row = ttk.Frame(vna, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 0))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        ttk.Button(row, text="Log Mag", command=lambda: self.vna_set_format("LOGM")).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Phase", command=lambda: self.vna_set_format("PHAS")).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(row, text="Take One Reading", command=self.take_one_reading, style="Accent.TButton").grid(row=0, column=2, sticky="ew")

    def _build_run_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Run Scan", style="Card.TLabelframe", padding=12)
        card.pack(fill="x", pady=(0, 10))

        # State badge.
        badge_row = ttk.Frame(card, style="Card.TFrame")
        badge_row.pack(fill="x")
        ttk.Label(badge_row, text="State").pack(side="left")
        self.state_badge = tk.Label(
            badge_row,
            textvariable=self.state_text_var,
            bg="#30363d",
            fg="white",
            font=("Segoe UI", 10, "bold"),
            padx=12,
            pady=4,
        )
        self.state_badge.pack(side="right")

        btn_row = ttk.Frame(card, style="Card.TFrame")
        btn_row.pack(fill="x", pady=(10, 8))
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        self.run_button = ttk.Button(btn_row, text="Run Radiation Pattern Scan", style="Accent.TButton", command=self.start_scan)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_button = ttk.Button(btn_row, text="Stop Scan", command=self.stop_scan)
        self.stop_button.grid(row=0, column=1, sticky="ew")

        self.progress = ttk.Progressbar(card, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 6))
        ttk.Label(card, textvariable=self.progress_text_var, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.current_angle_var).pack(anchor="w", pady=(8, 0))
        ttk.Label(card, textvariable=self.last_reading_var).pack(anchor="w")

        ttk.Button(card, text="Open Save Folder", command=self.open_save_folder).pack(fill="x", pady=(8, 0))

    def _build_results_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Results", style="Card.TLabelframe", padding=12)
        card.pack(fill="x", pady=(0, 10))

        ttk.Label(card, textvariable=self.hpbw_var).pack(anchor="w")
        ttk.Label(card, textvariable=self.directivity_var).pack(anchor="w", pady=(2, 0))
        ttk.Label(card, textvariable=self.last_scan_mode_var, style="Muted.TLabel", wraplength=330, justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Separator(card).pack(fill="x", pady=8)
        ttk.Label(card, textvariable=self.text_file_var, style="Muted.TLabel", wraplength=330, justify="left").pack(anchor="w")
        ttk.Label(card, textvariable=self.plot_file_var, style="Muted.TLabel", wraplength=330, justify="left").pack(anchor="w", pady=(4, 0))

    def _build_plot_cards(self, parent: ttk.Frame) -> None:
        raw_card = ttk.LabelFrame(parent, text="Live Rectangular Plot", style="Card.TLabelframe", padding=8)
        raw_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        polar_card = ttk.LabelFrame(parent, text="Normalized Polar Plot", style="Card.TLabelframe", padding=8)
        polar_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Rectangular figure.
        self.raw_fig = Figure(figsize=(6, 5), dpi=100, facecolor="#161b22")
        self.raw_ax = self.raw_fig.add_subplot(111)
        self._style_axes(self.raw_ax)
        self.raw_canvas = FigureCanvasTkAgg(self.raw_fig, master=raw_card)
        self.raw_canvas.get_tk_widget().pack(fill="both", expand=True)

        # Polar figure.
        self.polar_fig = Figure(figsize=(6, 5), dpi=100, facecolor="#161b22")
        self.polar_ax = self.polar_fig.add_subplot(111, projection="polar")
        self._style_polar_axes(self.polar_ax)
        self.polar_canvas = FigureCanvasTkAgg(self.polar_fig, master=polar_card)
        self.polar_canvas.get_tk_widget().pack(fill="both", expand=True)

        self.refresh_plots()

    def _build_debug_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Debug / Error Panel", style="Card.TLabelframe", padding=8)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(card, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(toolbar, text="What happens gets logged here.", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(toolbar, text="Clear Log", command=self.clear_log).grid(row=0, column=1, sticky="e")

        self.log_text = ScrolledText(
            card,
            height=11,
            bg="#0f141b",
            fg="#e6edf3",
            insertbackground="#e6edf3",
            relief="flat",
            wrap="word",
            font=("Consolas", 10),
        )
        self.log_text.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(state="disabled")

    def _labeled_entry(self, parent: ttk.Frame, label: str, variable: tk.StringVar, help_text: str = "") -> None:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="x", pady=(0, 8))
        ttk.Label(frame, text=label).pack(anchor="w")
        ttk.Entry(frame, textvariable=variable).pack(fill="x")
        if help_text:
            ttk.Label(frame, text=help_text, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------------
    # Styling for matplotlib
    # ------------------------------------------------------------------
    def _style_axes(self, ax) -> None:
        ax.set_facecolor("#0f141b")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.grid(True, color="#30363d", alpha=0.6)
        ax.set_xlabel("Phi (deg)", color="#c9d1d9")
        ax.set_ylabel(self.raw_plot_ylabel, color="#c9d1d9")
        ax.set_title(self.raw_plot_title, color="#e6edf3", fontsize=12)

    def _style_polar_axes(self, ax) -> None:
        ax.set_facecolor("#0f141b")
        ax.tick_params(colors="#c9d1d9")
        ax.grid(True, color="#30363d", alpha=0.6)
        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)  # Positive angles on the right, like the old Matlab dirplot.
        ax.set_title("Normalized Radiation Pattern", color="#e6edf3", fontsize=12, pad=18)

    # ------------------------------------------------------------------
    # Logging / state / queue processing
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_state(self, state: str) -> None:
        state = state.lower().strip()
        color_map = {
            "disconnected": "#30363d",
            "ready": "#238636",
            "running": "#d29922",
            "complete": "#1f6feb",
            "error": "#da3633",
            "stopped": "#6e7681",
        }
        text_map = {
            "disconnected": "Disconnected",
            "ready": "Ready",
            "running": "Running",
            "complete": "Complete",
            "error": "Error",
            "stopped": "Stopped",
        }
        self.state_text_var.set(text_map.get(state, state.title()))
        self.state_badge.configure(bg=color_map.get(state, "#30363d"))

    def queue_event(self, name: str, payload: object = None) -> None:
        self.ui_queue.put((name, payload))

    def process_ui_queue(self) -> None:
        try:
            while True:
                name, payload = self.ui_queue.get_nowait()

                if name == "log":
                    self.log(str(payload))

                elif name == "state":
                    self._set_state(str(payload))

                elif name == "point":
                    phi, mag, phs, index, total = payload  # type: ignore[misc]
                    self.scan_phi.append(float(phi))
                    self.scan_mag.append(float(mag))
                    self.scan_phase.append(float(phs))
                    self.current_angle_var.set(f"Current angle: {phi:.3f} deg")
                    self.last_reading_var.set(
                        f"{self.current_value_prefix}: {mag:.3f} {self.current_value_units}   Phase: {phs:.3f} deg"
                    )
                    pct = 0.0 if total == 0 else (100.0 * index / total)
                    self.progress["value"] = pct
                    self.progress_text_var.set(f"{index} / {total}")
                    self.refresh_plots()

                elif name == "progress":
                    index, total, label_text = payload  # type: ignore[misc]
                    pct = 0.0 if total == 0 else (100.0 * index / total)
                    self.progress["value"] = pct
                    self.progress_text_var.set(str(label_text))

                elif name == "summary":
                    data = payload  # type: ignore[assignment]
                    self.hpbw_var.set(f"HPBW: {data['hpbw']}")
                    self.directivity_var.set(f"Directivity: {data['directivity']}")
                    self.text_file_var.set(f"Text file: {data['text_file']}")
                    self.plot_file_var.set(f"Plot file: {data['plot_file']}")
                    self.last_scan_mode_var.set(f"Last scan mode: {data['scan_mode']}")

                elif name == "scan_started":
                    self.scan_phi.clear()
                    self.scan_mag.clear()
                    self.scan_phase.clear()
                    self.progress["value"] = 0
                    self.progress_text_var.set("0 / 0")
                    self.last_text_file = None
                    self.last_plot_file = None
                    self.hpbw_var.set("HPBW: --")
                    self.directivity_var.set("Directivity: --")
                    self.text_file_var.set("Text file: --")
                    self.plot_file_var.set("Plot file: --")
                    self.refresh_plots()

                elif name == "scan_finished":
                    self.connected = True  # Still connected after a scan.
                    self.refresh_plots()

                elif name == "ui_call":
                    callback = payload
                    if callable(callback):
                        callback()

                elif name == "messagebox_error":
                    messagebox.showerror("AMS", str(payload))

                elif name == "manual_finished":
                    self.manual_thread = None

                elif name == "connection_finished":
                    self.connect_thread = None

                elif name == "calibration_finished":
                    self.calibration_thread = None

        except queue.Empty:
            pass

        self.root.after(100, self.process_ui_queue)

    # ------------------------------------------------------------------
    # Connection actions
    # ------------------------------------------------------------------
    def connect_instruments(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showwarning("AMS", "Stop the running scan before reconnecting instruments.")
            return
        if self.calibration_thread and self.calibration_thread.is_alive():
            messagebox.showwarning("AMS", "Stop the running calibration before reconnecting instruments.")
            return
        if self.manual_thread and self.manual_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current manual action to finish before reconnecting instruments.")
            return
        if self.connect_thread and self.connect_thread.is_alive():
            messagebox.showwarning("AMS", "A connection attempt is already running.")
            return

        self.disconnect_instruments(log_message=False)

        simulation = bool(self.simulation_var.get())
        pos_resource = self.positioner_resource_var.get().strip()
        vna_resource = self.vna_resource_var.get().strip()
        backend = self.visa_backend_var.get().strip()
        requested_family = self.vna_family_var.get().strip() or "HP 8753"

        def worker() -> None:
            local_rm = None
            local_positioner = None
            local_vna = None
            try:
                if simulation:
                    local_positioner = FakePositioner(pos_resource)
                    local_vna = FakeVNA(vna_resource)
                    pos_name = local_positioner.connect()
                    vna_name = local_vna.connect()

                    def apply_success() -> None:
                        self.positioner = local_positioner
                        self.vna = local_vna
                        self.rm = None
                        self.connected = True
                        self.instrument_info_var.set(f"{pos_name}\n{vna_name}")
                        self._set_state("ready")

                    self.queue_event("ui_call", apply_success)
                    self.queue_event("log", "Simulation mode enabled. No real hardware will be touched.")
                    return

                if pyvisa is None:
                    raise RuntimeError("PyVISA is not installed. Install pyvisa first.")

                local_rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()

                self.queue_event("log", f"Opening positioner at {pos_resource} and VNA at {vna_resource}...")
                if requested_family != "HP 8753":
                    self.queue_event("log", f"Ignoring unsupported VNA family setting {requested_family!r}; this build only uses HP 8753ES.")

                local_positioner = FS121Positioner(local_rm, pos_resource)
                pos_name = local_positioner.connect()
                local_vna, vna_name = connect_vna(local_rm, vna_resource, lambda msg: self.queue_event("log", msg))
                cal_type = local_vna.get_calibration_type()
                corr_state = local_vna.get_correction_state()

                def apply_success() -> None:
                    self.rm = local_rm
                    self.positioner = local_positioner
                    self.vna = local_vna
                    self.connected = True
                    self.instrument_info_var.set(
                        f"{pos_name}\n{vna_name}\nVNA family: HP 8753ES\nVNA calibration: {cal_type}   Correction: {corr_state}"
                    )
                    self._set_state("ready")

                self.queue_event("ui_call", apply_success)
                self.queue_event("log", "Connected to positioner and VNA successfully.")
                if "readback unavailable" in pos_name.lower():
                    self.queue_event("log", "Positioner warning: angle readback is unavailable, so the GUI will use software-estimated angles.")
                self.queue_event("log", "Positioner note: boresight must be set to 0 deg before scanning.")

            except Exception as exc:
                message = str(exc)
                if backend.strip() == "@py" and vna_resource.upper().startswith("GPIB") and ("gpib" in message.lower() or "linux-gpib" in message.lower()):
                    message = (
                        "The @py backend opened the serial positioner, but it cannot open the GPIB VNA on this PC. "
                        "Leave the VISA backend box blank to use NI-VISA / Keysight VISA for GPIB, or install gpib-ctypes if you really want GPIB through @py."
                    )
                try:
                    if local_vna is not None:
                        local_vna.close()
                except Exception:
                    pass
                try:
                    if local_positioner is not None:
                        local_positioner.close()
                except Exception:
                    pass
                try:
                    if local_rm is not None:
                        local_rm.close()
                except Exception:
                    pass

                def apply_failure() -> None:
                    self.positioner = None
                    self.vna = None
                    self.rm = None
                    self.connected = False
                    self.instrument_info_var.set(f"Connection failed: {message}")
                    self._set_state("error")

                self.queue_event("ui_call", apply_failure)
                self.queue_event("log", f"Connection failed: {message}")
                self.queue_event("log", traceback.format_exc())
                self.queue_event("messagebox_error", message)
            finally:
                self.queue_event("connection_finished")

        self._set_state("running")
        self.log("Connecting instruments...")
        self.connect_thread = threading.Thread(target=worker, daemon=True)
        self.connect_thread.start()

    def disconnect_instruments(self, log_message: bool = True) -> None:
        if self.manual_thread and self.manual_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current manual action to finish before disconnecting instruments.")
            return
        if self.calibration_thread and self.calibration_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current calibration to finish before disconnecting instruments.")
            return
        if self.connect_thread and self.connect_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current connection attempt to finish before disconnecting instruments.")
            return
        try:
            if self.positioner is not None:
                try:
                    self.positioner.close()
                except Exception:
                    pass
            if self.vna is not None:
                try:
                    self.vna.close()
                except Exception:
                    pass
            if self.rm is not None:
                try:
                    self.rm.close()
                except Exception:
                    pass
        finally:
            self.positioner = None
            self.vna = None
            self.rm = None
            self.connected = False
            self.instrument_info_var.set("No instruments connected")
            self._set_state("disconnected")
            if log_message:
                self.log("Disconnected instruments.")

    # ------------------------------------------------------------------
    # Manual UI actions
    # ------------------------------------------------------------------
    def browse_save_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.save_folder_var.get() or DEFAULT_SAVE_FOLDER)
        if folder:
            self.save_folder_var.set(folder)

    def open_save_folder(self) -> None:
        folder = Path(self.save_folder_var.get().strip() or DEFAULT_SAVE_FOLDER)
        ensure_folder(folder)
        open_in_file_browser(folder)

    def _require_connection(self) -> bool:
        if self.connected and self.positioner is not None and self.vna is not None:
            return True
        if self.connect_thread and self.connect_thread.is_alive():
            messagebox.showwarning("AMS", "Connection is still in progress.")
        else:
            messagebox.showwarning("AMS", "Connect the instruments first.")
        return False

    def _read_float(self, var: tk.StringVar, label: str) -> float:
        try:
            return float(var.get().strip())
        except Exception as exc:
            raise ValueError(f"Bad value for {label}: {var.get()!r}") from exc

    def _require_idle(self) -> bool:
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showwarning("AMS", "That action is disabled while a scan is running.")
            return False
        if self.calibration_thread and self.calibration_thread.is_alive():
            messagebox.showwarning("AMS", "That action is disabled while chamber calibration is running.")
            return False
        return True

    def _refresh_calibration_status(self) -> None:
        if self.calibration_profile is None:
            self.calibration_status_var.set(
                f"No saved chamber calibration loaded.\nProfile path: {CALIBRATION_PROFILE_PATH}"
            )
            return

        self.calibration_status_var.set(
            self.calibration_profile.describe()
            + f"\nProfile: {CALIBRATION_PROFILE_PATH}"
            + f"\nTable: {CALIBRATION_TABLE_PATH}"
        )

    def _load_saved_calibration_profile(self, log_on_missing: bool = True) -> None:
        if not CALIBRATION_PROFILE_PATH.exists():
            self.calibration_profile = None
            self._refresh_calibration_status()
            if log_on_missing:
                self.log(f"No saved chamber calibration found at {CALIBRATION_PROFILE_PATH}.")
            return

        try:
            data = json.loads(CALIBRATION_PROFILE_PATH.read_text(encoding="utf-8"))
            self.calibration_profile = CalibrationProfile.from_dict(data)
            self.scan_tx_gain_var.set(f"{self.calibration_profile.tx_gain_db:.6g}")
            self._refresh_calibration_status()
        except Exception as exc:
            self.calibration_profile = None
            self._refresh_calibration_status()
            if log_on_missing:
                self.log(f"Could not load saved chamber calibration: {exc}")

    def clear_saved_calibration(self) -> None:
        if not self._require_idle():
            return
        if self.manual_thread and self.manual_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current manual action to finish before clearing calibration.")
            return
        if not messagebox.askyesno("AMS", "Delete the saved chamber calibration profile and table?"):
            return

        for path in (CALIBRATION_PROFILE_PATH, CALIBRATION_TABLE_PATH):
            try:
                if path.exists():
                    path.unlink()
            except Exception as exc:
                messagebox.showerror("AMS", f"Could not delete {path}: {exc}")
                return

        self.calibration_profile = None
        self._refresh_calibration_status()
        self.last_scan_mode_var.set("Last scan mode: --")
        self.log("Saved chamber calibration cleared.")

    def run_chamber_calibration(self) -> None:
        if self.calibration_thread and self.calibration_thread.is_alive():
            messagebox.showwarning("AMS", "A chamber calibration is already running.")
            return
        if not self._require_idle():
            return
        if self.manual_thread and self.manual_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current manual action to finish before starting calibration.")
            return
        if not self._require_connection():
            return

        try:
            start_ghz = self._read_float(self.cal_start_ghz_var, "Calibration start frequency")
            stop_ghz = self._read_float(self.cal_stop_ghz_var, "Calibration stop frequency")
            step_ghz = self._read_float(self.cal_step_ghz_var, "Calibration step size")
            tx_gain_db = self._read_float(self.cal_tx_gain_var, "Calibration TX gain")
            rx_gain_db = self._read_float(self.cal_rx_gain_var, "Calibration RX reference gain")
            settle_s = self._read_float(self.settle_s_var, "Settle delay")
            use_correction = bool(self.use_correction_var.get())
            freq_points_ghz = build_frequency_points(start_ghz, stop_ghz, step_ghz)
        except Exception as exc:
            self._set_state("error")
            self.log(f"Could not start chamber calibration: {exc}")
            messagebox.showerror("AMS - Calibration Error", str(exc))
            return

        prompt = (
            "Calibration mode requires two identical antennas connected to TX and RX, both pointed at boresight.\n\n"
            "The positioner will be moved to 0 deg and the script will measure S21_ref(f) over the requested frequency list.\n\n"
            f"Reference TX gain Gtx_ref = {tx_gain_db:.3f} dBi\n"
            f"Reference RX gain Grx_ref = {rx_gain_db:.3f} dBi\n"
            f"Points = {len(freq_points_ghz)}\n\n"
            "Continue?"
        )
        if not messagebox.askyesno("AMS - Chamber Calibration", prompt):
            return

        self.stop_event.clear()
        self.progress["value"] = 0
        self.progress_text_var.set(f"Calibration 0 / {len(freq_points_ghz)}")
        self._set_state("running")
        self.log(
            f"Starting chamber calibration: {start_ghz:.6f} to {stop_ghz:.6f} GHz, step {step_ghz:.6f} GHz, points={len(freq_points_ghz)}"
        )

        args = {
            "freq_points_ghz": freq_points_ghz,
            "tx_gain_db": tx_gain_db,
            "rx_gain_db": rx_gain_db,
            "settle_s": settle_s,
            "use_correction": use_correction,
        }
        self.calibration_thread = threading.Thread(target=self._calibration_worker, kwargs=args, daemon=True)
        self.calibration_thread.start()

    def _calibration_worker(
        self,
        freq_points_ghz: Sequence[float],
        tx_gain_db: float,
        rx_gain_db: float,
        settle_s: float,
        use_correction: bool,
    ) -> None:
        try:
            total = len(freq_points_ghz)
            if total == 0:
                raise ValueError("No calibration frequencies were generated.")

            with self.instrument_lock:
                self.queue_event("log", "Moving positioner to 0 deg for calibration boresight...")
                self.positioner.go_zero()
                time.sleep(max(0.0, settle_s))
                current_angle = float(self.positioner.current_angle_deg())
                self._queue_current_angle(current_angle)

                measured_s21_db: List[float] = []
                measured_phase_deg: List[float] = []
                measured_k_db: List[float] = []
                freq_points_hz: List[float] = []

                for idx, freq_ghz in enumerate(freq_points_ghz, start=1):
                    if self.stop_event.is_set():
                        raise ScanCancelled("Calibration stopped by user.")

                    freq_hz = float(freq_ghz) * 1e9
                    self.vna.configure_cw_s21(freq_hz=freq_hz, use_correction=use_correction)
                    mag_db, phase_deg = self.vna.single_s21_reading(current_angle_deg=current_angle)
                    k_db = float(mag_db - tx_gain_db - rx_gain_db)

                    freq_points_hz.append(freq_hz)
                    measured_s21_db.append(float(mag_db))
                    measured_phase_deg.append(float(phase_deg))
                    measured_k_db.append(k_db)

                    label_text = f"Calibration {idx} / {total} @ {freq_ghz:.6f} GHz"
                    self.queue_event("progress", (idx, total, label_text))
                    self.queue_event(
                        "log",
                        f"{label_text}: S21_ref={mag_db:.3f} dB, phase={phase_deg:.3f} deg, K={k_db:.3f} dB",
                    )

            step_hz = 0.0 if len(freq_points_hz) <= 1 else abs(freq_points_hz[1] - freq_points_hz[0])
            profile = CalibrationProfile(
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                tx_gain_db=float(tx_gain_db),
                rx_ref_gain_db=float(rx_gain_db),
                start_freq_hz=float(freq_points_hz[0]),
                stop_freq_hz=float(freq_points_hz[-1]),
                step_freq_hz=float(step_hz),
                freq_hz=freq_points_hz,
                s21_ref_db=measured_s21_db,
                phase_deg=measured_phase_deg,
                k_db=measured_k_db,
            )
            profile.save_json(CALIBRATION_PROFILE_PATH)
            profile.save_table(CALIBRATION_TABLE_PATH)

            def apply_profile(profile: CalibrationProfile = profile) -> None:
                self.calibration_profile = profile
                self.scan_tx_gain_var.set(f"{profile.tx_gain_db:.6g}")
                self._refresh_calibration_status()

            self.queue_event("ui_call", apply_profile)
            self.queue_event("state", "complete")
            self.queue_event("log", f"Calibration saved to: {CALIBRATION_PROFILE_PATH}")
            self.queue_event("log", f"Calibration table saved to: {CALIBRATION_TABLE_PATH}")

        except ScanCancelled as exc:
            self.queue_event("state", "stopped")
            self.queue_event("log", str(exc))
            self.queue_event("log", "Saved calibration was left unchanged.")

        except Exception as exc:
            self.queue_event("state", "error")
            self.queue_event("log", f"Calibration failed: {exc}")
            self.queue_event("log", traceback.format_exc())
            self.queue_event("messagebox_error", str(exc))

        finally:
            self.queue_event("calibration_finished")

    def _run_manual_action(self, label: str, func) -> None:
        if not self._require_idle():
            return
        if self.manual_thread and self.manual_thread.is_alive():
            messagebox.showwarning("AMS", "Another manual action is already running.")
            return
        if not self._require_connection():
            return

        def worker() -> None:
            try:
                with self.instrument_lock:
                    result = func()
                if isinstance(result, str) and result:
                    self.queue_event("log", f"{label}: {result}")
                else:
                    self.queue_event("log", label)
                self.queue_event("state", "ready")
            except Exception as exc:
                self.queue_event("state", "error")
                self.queue_event("log", f"{label} failed: {exc}")
                self.queue_event("log", traceback.format_exc())
                self.queue_event("messagebox_error", str(exc))
            finally:
                self.queue_event("manual_finished")

        self._set_state("running")
        self.log(f"{label}...")
        self.manual_thread = threading.Thread(target=worker, daemon=True)
        self.manual_thread.start()

    def _queue_current_angle(self, angle_deg: float) -> None:
        self.queue_event("ui_call", lambda angle_deg=angle_deg: self.current_angle_var.set(f"Current angle: {angle_deg:.3f} deg"))

    def _queue_last_reading(self, mag_db: float, phase_deg: float) -> None:
        self.queue_event(
            "ui_call",
            lambda mag_db=mag_db, phase_deg=phase_deg: self.last_reading_var.set(
                f"LogMag: {mag_db:.3f} dB   Phase: {phase_deg:.3f} deg"
            ),
        )

    def read_position_angle(self) -> None:
        def action() -> str:
            current = float(self.positioner.current_angle_deg())
            self._queue_current_angle(current)
            return f"Positioner angle = {current:.3f} deg"
        self._run_manual_action("Read position", action)

    def go_to_absolute_angle(self) -> None:
        target = self._read_float(self.move_angle_deg_var, "Move to absolute angle")

        def action() -> str:
            if target < POSITIONER_MIN_DEG or target > POSITIONER_MAX_DEG:
                raise ValueError(f"Requested angle {target:.3f} deg is outside the software limit ±360 deg.")
            self.positioner.move_absolute_deg(target, wait=True)
            current = float(self.positioner.current_angle_deg())
            self._queue_current_angle(current)
            return f"Positioner moved to {current:.3f} deg"
        self._run_manual_action("Go absolute", action)

    def positioner_cw_continuous(self) -> None:
        self._run_manual_action("Positioner CW continuous", lambda: (self.positioner.start_cw_continuous(), "Started CW continuous move")[1])

    def positioner_ccw_continuous(self) -> None:
        self._run_manual_action("Positioner CCW continuous", lambda: (self.positioner.start_ccw_continuous(), "Started CCW continuous move")[1])

    def positioner_stop(self) -> None:
        self._run_manual_action("Positioner stop", lambda: (self.positioner.stop(), "Positioner stop command sent")[1])

    def vna_send_command(self) -> None:
        command = self.vna_manual_command_var.get().strip()
        if command.endswith("?"):
            self.vna_query_command(command)
            return
        self._run_manual_action(f"VNA SEND {command}", lambda: (self.vna.send_manual(command), f"Sent {command}")[1])

    def vna_query_command(self, command: str) -> None:
        self._run_manual_action(f"VNA QUERY {command}", lambda: self.vna.query_manual(command))

    def vna_query_command_from_entry(self) -> None:
        command = self.vna_manual_command_var.get().strip()
        self.vna_query_command(command)

    def vna_preset(self) -> None:
        self._run_manual_action("VNA preset", lambda: (self.vna.preset(), "Preset command sent")[1])

    def vna_single_sweep(self) -> None:
        self._run_manual_action("VNA single sweep", lambda: (self.vna.trigger_single(), "Single sweep complete")[1])

    def vna_hold(self) -> None:
        self._run_manual_action("VNA hold", lambda: (self.vna.set_hold(True), "Hold enabled")[1])

    def vna_select_measurement(self, measurement: str) -> None:
        self._run_manual_action(f"VNA measurement {measurement}", lambda: (self.vna.set_measurement(measurement), f"Selected {measurement}")[1])

    def vna_set_format(self, display_format: str) -> None:
        self._run_manual_action(f"VNA format {display_format}", lambda: (self.vna.set_display_format(display_format), f"Selected {display_format}")[1])

    def vna_apply_s21_setup(self) -> None:
        freq_ghz = self._read_float(self.frequency_ghz_var, "CW frequency")
        freq_hz = freq_ghz * 1e9
        use_correction = bool(self.use_correction_var.get())

        def action() -> str:
            self.vna.configure_cw_s21(freq_hz=freq_hz, use_correction=use_correction)
            return f"Configured one-point S21 at {freq_ghz:.6f} GHz"
        self._run_manual_action("Apply VNA S21 setup", action)

    def manual_jog(self, sign: int) -> None:
        jog_deg = self._read_float(self.jog_deg_var, "Jog size")

        def action() -> str:
            target = float(self.positioner.current_angle_deg()) + sign * jog_deg
            if target < POSITIONER_MIN_DEG or target > POSITIONER_MAX_DEG:
                raise ValueError(f"Requested angle {target:.3f} deg is outside the software limit ±360 deg.")
            self.positioner.move_absolute_deg(target, wait=True)
            current = float(self.positioner.current_angle_deg())
            self._queue_current_angle(current)
            return f"Positioner moved to {current:.3f} deg"
        self._run_manual_action("Jog positioner", action)

    def zero_here(self) -> None:
        def action() -> str:
            self.positioner.set_zero_here()
            current = float(self.positioner.current_angle_deg())
            self._queue_current_angle(current)
            return f"Current position set to 0 deg (reported {current:.3f} deg)"
        self._run_manual_action("Set zero here", action)

    def go_to_zero(self) -> None:
        def action() -> str:
            self.positioner.go_zero()
            current = float(self.positioner.current_angle_deg())
            self._queue_current_angle(current)
            return f"Positioner returned to {current:.3f} deg"
        self._run_manual_action("Go to zero", action)

    def take_one_reading(self) -> None:
        freq_ghz = self._read_float(self.frequency_ghz_var, "CW frequency")
        use_correction = bool(self.use_correction_var.get())
        freq_hz = freq_ghz * 1e9

        def action() -> str:
            current_angle = float(self.positioner.current_angle_deg())
            self.vna.configure_cw_s21(freq_hz=freq_hz, use_correction=use_correction)
            mag_db, phase_deg = self.vna.single_s21_reading(current_angle_deg=current_angle)
            self._queue_current_angle(current_angle)
            self._queue_last_reading(mag_db, phase_deg)
            return f"One S21 reading at {current_angle:.3f} deg -> {mag_db:.3f} dB, {phase_deg:.3f} deg"
        self._run_manual_action("Take one S21 reading", action)

    # ------------------------------------------------------------------
    # Scan control
    # ------------------------------------------------------------------
    def start_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showwarning("AMS", "A scan is already running.")
            return
        if self.calibration_thread and self.calibration_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the chamber calibration to finish before starting a scan.")
            return
        if self.manual_thread and self.manual_thread.is_alive():
            messagebox.showwarning("AMS", "Wait for the current manual action to finish before starting a scan.")
            return
        if not self._require_connection():
            return

        try:
            folder = Path(self.save_folder_var.get().strip() or DEFAULT_SAVE_FOLDER)
            ensure_folder(folder)

            base_name = self.base_name_var.get().strip() or DEFAULT_BASE_NAME
            freq_ghz = self._read_float(self.frequency_ghz_var, "CW frequency")
            span_deg = self._read_float(self.span_deg_var, "Scan span")
            step_deg = self._read_float(self.step_deg_var, "Step size")
            settle_s = self._read_float(self.settle_s_var, "Settle delay")
            use_correction = bool(self.use_correction_var.get())
            use_chamber_calibration = bool(self.use_chamber_calibration_var.get())

            angles = build_scan_angles(span_deg=span_deg, step_deg=step_deg)
            if min(angles) < POSITIONER_MIN_DEG or max(angles) > POSITIONER_MAX_DEG:
                raise ValueError("Requested scan would exceed the software angle limit of ±360 deg.")

            calibration_context = None
            scan_mode_text = "Raw S21"
            magnitude_header = "Log Magnitude (dB)"
            file_kind = "phi_logmag_phase"
            self.raw_plot_ylabel = "Log Magnitude (dB)"
            self.raw_plot_title = "S21 vs Phi"
            self.current_value_prefix = "LogMag"
            self.current_value_units = "dB"

            freq_hz = freq_ghz * 1e9
            if use_chamber_calibration:
                if self.calibration_profile is None:
                    raise ValueError(
                        f"Apply saved chamber calibration is enabled, but no saved calibration profile is loaded. Expected: {CALIBRATION_PROFILE_PATH}"
                    )
                scan_tx_gain_db = self._read_float(self.scan_tx_gain_var, "Radiation pattern TX gain")
                k_db, match_description = self.calibration_profile.get_k_for_frequency(freq_hz)
                calibration_context = {
                    "k_db": float(k_db),
                    "tx_gain_db": float(scan_tx_gain_db),
                    "match_description": str(match_description),
                }
                scan_mode_text = f"Calibrated gain ({match_description})"
                magnitude_header = "Calibrated Gain (dBi)"
                file_kind = "phi_gain_phase"
                self.raw_plot_ylabel = "Calibrated Gain (dBi)"
                self.raw_plot_title = "Calibrated Gain vs Phi"
                self.current_value_prefix = "Gain"
                self.current_value_units = "dBi"

            self.stop_event.clear()
            self.queue_event("scan_started")
            self.queue_event("state", "running")
            self.log(
                f"Starting scan: freq={freq_ghz} GHz, span={span_deg} deg, step={step_deg} deg, settle={settle_s} s, points={len(angles)}, mode={scan_mode_text}"
            )
            if calibration_context is not None:
                self.log(
                    f"Using chamber calibration constant K(f) {calibration_context['match_description']} and scan TX gain {calibration_context['tx_gain_db']:.3f} dBi."
                )

            args = {
                "folder": folder,
                "base_name": base_name,
                "freq_ghz": freq_ghz,
                "angles": angles,
                "settle_s": settle_s,
                "use_correction": use_correction,
                "calibration_context": calibration_context,
                "magnitude_header": magnitude_header,
                "file_kind": file_kind,
                "scan_mode_text": scan_mode_text,
            }
            self.scan_thread = threading.Thread(target=self._scan_worker, kwargs=args, daemon=True)
            self.scan_thread.start()

        except Exception as exc:
            self._set_state("error")
            self.log(f"Could not start scan: {exc}")
            self.log(traceback.format_exc())
            messagebox.showerror("AMS - Start Scan Error", str(exc))

    def stop_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            self.stop_event.set()
            self.log("Stop requested by user.")
        elif self.calibration_thread and self.calibration_thread.is_alive():
            self.stop_event.set()
            self.log("Stop requested by user for chamber calibration.")
        else:
            self.log("No scan or chamber calibration is currently running.")

    def _scan_worker(
        self,
        folder: Path,
        base_name: str,
        freq_ghz: float,
        angles: Sequence[float],
        settle_s: float,
        use_correction: bool,
        calibration_context: Optional[dict],
        magnitude_header: str,
        file_kind: str,
        scan_mode_text: str,
    ) -> None:
        try:
            freq_hz = freq_ghz * 1e9
            total = len(angles)

            with self.instrument_lock:
                self.queue_event("log", f"Configuring VNA for CW S21 at {freq_ghz:.6f} GHz...")
                self.vna.configure_cw_s21(freq_hz=freq_hz, use_correction=use_correction)

                # Move to the first requested angle before the loop begins.
                self.queue_event("log", f"Moving to start angle {angles[0]:.3f} deg...")
                self.positioner.move_absolute_deg(float(angles[0]), wait=True)
                time.sleep(max(0.0, settle_s))

                phi_local: List[float] = []
                mag_local: List[float] = []
                phase_local: List[float] = []

                for idx, angle in enumerate(angles, start=1):
                    if self.stop_event.is_set():
                        raise ScanCancelled("Scan stopped by user.")

                    # Move using absolute angle commands so the position does not drift.
                    self.positioner.move_absolute_deg(float(angle), wait=True)
                    time.sleep(max(0.0, settle_s))

                    current_angle = float(self.positioner.current_angle_deg())
                    raw_mag_db, phase_deg = self.vna.single_s21_reading(current_angle_deg=current_angle)
                    display_mag = float(raw_mag_db)
                    if calibration_context is not None:
                        display_mag = float(
                            raw_mag_db - calibration_context["k_db"] - calibration_context["tx_gain_db"]
                        )

                    phi_local.append(current_angle)
                    mag_local.append(display_mag)
                    phase_local.append(phase_deg)

                    self.queue_event("point", (current_angle, display_mag, phase_deg, idx, total))

                # Go back to boresight at the end, like the old AMS flow.
                self.queue_event("log", "Scan done. Returning positioner to 0 deg...")
                self.positioner.go_zero()

            # Save results.
            stamp = now_stamp()
            text_file = folder / f"{base_name}_{stamp}_{file_kind}.txt"
            plot_file = folder / f"{base_name}_{stamp}_normalized_polar.png"

            write_pattern_file(text_file, phi_local, mag_local, phase_local, magnitude_header=magnitude_header)
            self.save_normalized_polar_png(plot_file, phi_local, mag_local)

            directivity, hpbw = estimate_hpbw_and_directivity(phi_local, mag_local)
            hpbw_text = "NaN" if not math.isfinite(hpbw) else f"{hpbw:.3f} deg"
            directivity_text = "NaN" if not math.isfinite(directivity) else f"{directivity:.3f}"

            self.last_text_file = text_file
            self.last_plot_file = plot_file

            self.queue_event(
                "summary",
                {
                    "hpbw": hpbw_text,
                    "directivity": directivity_text,
                    "text_file": str(text_file),
                    "plot_file": str(plot_file),
                    "scan_mode": scan_mode_text,
                },
            )
            self.queue_event("state", "complete")
            self.queue_event("log", f"Scan complete. Data saved to: {text_file}")
            self.queue_event("log", f"Normalized polar plot saved to: {plot_file}")
            self.queue_event("scan_finished")

        except ScanCancelled as exc:
            try:
                if self.positioner is not None:
                    self.positioner.stop()
                    time.sleep(0.10)
                    self.positioner.go_zero()
            except Exception:
                pass
            self.queue_event("state", "stopped")
            self.queue_event("log", str(exc))
            self.queue_event("log", "Partial scan data was not saved.")
            self.queue_event("scan_finished")

        except Exception as exc:
            try:
                if self.positioner is not None:
                    self.positioner.stop()
                    time.sleep(0.10)
                    self.positioner.go_zero()
            except Exception:
                pass
            self.queue_event("state", "error")
            self.queue_event("log", f"Scan failed: {exc}")
            self.queue_event("log", traceback.format_exc())
            self.queue_event("scan_finished")

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------
    def refresh_plots(self) -> None:
        self._draw_rectangular_plot(self.raw_ax, self.scan_phi, self.scan_mag)
        self.raw_canvas.draw_idle()

        self._draw_polar_plot(self.polar_ax, self.scan_phi, self.scan_mag, normalize=True)
        self.polar_canvas.draw_idle()

    def _draw_rectangular_plot(self, ax, phi: Sequence[float], mag: Sequence[float]) -> None:
        ax.clear()
        self._style_axes(ax)
        ax.set_xlim(-180, 180)
        if phi and mag:
            ax.plot(phi, mag, linewidth=2)
            mag_arr = np.array(mag, dtype=float)
            mag_arr = mag_arr[np.isfinite(mag_arr)]
            if len(mag_arr) > 0:
                y_min = float(np.min(mag_arr))
                y_max = float(np.max(mag_arr))
                padding = max(2.5, 0.05 * max(1.0, abs(y_max - y_min)))
                y_low = 5.0 * math.floor((y_min - padding) / 5.0)
                y_high = 5.0 * math.ceil((y_max + padding) / 5.0)
                if math.isclose(y_low, y_high, abs_tol=1e-9):
                    y_low -= 5.0
                    y_high += 5.0
                ax.set_ylim(y_low, y_high)
                return
        ax.set_ylim(-100, 5)

    def _draw_polar_plot(self, ax, phi: Sequence[float], mag: Sequence[float], normalize: bool = True) -> None:
        ax.clear()
        self._style_polar_axes(ax)

        if not phi or not mag:
            ax.set_rlim(0, 40)
            ax.set_rticks([0, 10, 20, 30, 40])
            ax.set_yticklabels(["-40", "-30", "-20", "-10", "0 dB"])
            return

        phi_arr = np.array(phi, dtype=float)
        mag_arr = np.array(mag, dtype=float)
        valid = np.isfinite(phi_arr) & np.isfinite(mag_arr)
        phi_arr = phi_arr[valid]
        mag_arr = mag_arr[valid]

        if len(phi_arr) == 0:
            return

        if normalize:
            mag_arr = normalized_logmag(mag_arr)
            rho_max = PLOT_RHOMAX_NORMALIZED
        else:
            rho_max = PLOT_RHOMAX_RAW

        rho_min = PLOT_RHOMIN
        mag_arr = np.clip(mag_arr, rho_min, rho_max)

        # This recreates the old Matlab trick for plotting negative dB values on a polar axis:
        # radius = rho - rho_min  so that rho_min maps to 0 radius.
        radius = mag_arr - rho_min
        theta = np.deg2rad(phi_arr)

        ax.plot(theta, radius, linewidth=2)
        ax.set_rlim(0, rho_max - rho_min)

        tick_values = np.linspace(rho_min, rho_max, 5)
        tick_positions = tick_values - rho_min
        tick_labels = [f"{v:.0f}" for v in tick_values]
        if tick_labels:
            tick_labels[-1] = tick_labels[-1] + " dB"
        ax.set_rticks(tick_positions)
        ax.set_yticklabels(tick_labels)

    def save_normalized_polar_png(self, file_path: Path, phi: Sequence[float], mag: Sequence[float]) -> None:
        fig = Figure(figsize=(6, 6), dpi=150, facecolor="white")
        ax = fig.add_subplot(111, projection="polar")
        ax.set_facecolor("white")
        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)
        ax.grid(True, alpha=0.4)
        ax.set_title("Normalized Radiation Pattern", pad=18)

        if phi and mag:
            phi_arr = np.array(phi, dtype=float)
            mag_arr = np.array(mag, dtype=float)
            valid = np.isfinite(phi_arr) & np.isfinite(mag_arr)
            phi_arr = phi_arr[valid]
            mag_arr = mag_arr[valid]
            if len(phi_arr) > 0:
                mag_arr = normalized_logmag(mag_arr)
                mag_arr = np.clip(mag_arr, PLOT_RHOMIN, PLOT_RHOMAX_NORMALIZED)
                radius = mag_arr - PLOT_RHOMIN
                ax.plot(np.deg2rad(phi_arr), radius, linewidth=2)

        ax.set_rlim(0, PLOT_RHOMAX_NORMALIZED - PLOT_RHOMIN)
        tick_values = np.linspace(PLOT_RHOMIN, PLOT_RHOMAX_NORMALIZED, 5)
        tick_positions = tick_values - PLOT_RHOMIN
        tick_labels = [f"{v:.0f}" for v in tick_values]
        if tick_labels:
            tick_labels[-1] = tick_labels[-1] + " dB"
        ax.set_rticks(tick_positions)
        ax.set_yticklabels(tick_labels)

        fig.savefig(file_path, bbox_inches="tight")


def main() -> None:
    root = tk.Tk()
    app = AMSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
