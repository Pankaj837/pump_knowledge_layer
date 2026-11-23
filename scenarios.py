"""
Test scenarios: realistic sensor snapshots for P-101 with the expected outcome.

Pressures were derived from the pump curve H = 60 - 0.001 Q^2 (see physics.py),
so that each snapshot is physically self-consistent, e.g. at shut-off
(deadheading) discharge pressure = suction pressure + 60 m of head.
Used by main.py (demo) and tests/test_diagnosis.py (regression tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Normal operation at the duty point: 100 m3/h @ 50 m, 30 degC water
BASELINE = {
    "suction_pressure": 1.6,           # bar(a)
    "discharge_pressure": 6.5,         # bar(a) -> 50.2 m head
    "flow": 100.0,                     # m3/h
    "radial_vibration": 2.0,           # mm/s RMS
    "axial_vibration": 1.5,            # mm/s RMS
    "bearing_temperature": 60.0,       # degC
    "fluid_temperature": 30.0,         # degC
    "motor_winding_temperature": 95.0, # degC
    "seal_chamber_temperature": 45.0,  # degC
    "motor_current": 33.0,             # A
    "seal_leak": 0,                    # 0/1
    "suction_tank_level": 70.0,        # %
    "strainer_dp": 0.05,               # bar
}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    readings: dict
    expected_status: str
    expected_primary: str | None = None          # top PRIMARY failure mode
    expected_root_cause: str | None = None       # top-ranked root cause of that failure mode
    expected_secondary: tuple[str, ...] = ()     # failure modes explained by the primary one
    use_baseline: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def sensor_states(self) -> dict:
        return {**BASELINE, **self.readings} if self.use_baseline else dict(self.readings)


SCENARIOS = (
    Scenario("healthy", "Normal operation at the duty point.", {}, "HEALTHY"),
    Scenario(
        "cavitation_clogged_strainer",
        "Strainer fouled: suction pressure collapses, head breaks down (~15 %), broadband vibration.",
        {"suction_pressure": 0.45, "discharge_pressure": 4.92, "flow": 78.0, "radial_vibration": 6.2,
         "strainer_dp": 0.55, "motor_current": 28.0},
        "FAULT_DETECTED", "FM-CAVITATION", "RC-STRAINER-CLOGGED"),
    Scenario(
        "cavitation_hot_fluid",
        "Upstream cooler lost: 85 degC water. Suction pressure still looks NORMAL, but vapour pressure "
        "(0.58 bar) consumes the NPSH margin - only the physics-derived signal reveals cavitation.",
        {"fluid_temperature": 85.0, "suction_pressure": 1.0, "discharge_pressure": 5.26, "flow": 95.0,
         "radial_vibration": 5.5},
        "FAULT_DETECTED", "FM-CAVITATION", "RC-HIGH-FLUID-TEMP"),
    Scenario(
        "dry_running_empty_tank",
        "Suction tank ran empty: no flow, no head, motor unloaded, seal overheating.",
        {"suction_tank_level": 5.0, "suction_pressure": 0.8, "discharge_pressure": 1.1, "flow": 2.0,
         "motor_current": 13.0, "seal_chamber_temperature": 95.0},
        "FAULT_DETECTED", "FM-DRY-RUN", "RC-EMPTY-TANK"),
    Scenario(
        "deadhead_closed_discharge",
        "Discharge valve closed: shut-off pressure, ~zero flow, motor current DROPS, liquid heating.",
        {"suction_pressure": 1.7, "discharge_pressure": 7.56, "flow": 3.0, "motor_current": 18.0,
         "seal_chamber_temperature": 78.0},
        "FAULT_DETECTED", "FM-DEADHEAD", "RC-DISCHARGE-VALVE-CLOSED"),
    Scenario(
        "misalignment_with_bearing_distress",
        "Misaligned coupling: high axial vibration and an overheating DE bearing (cascade).",
        {"radial_vibration": 5.2, "axial_vibration": 6.0, "bearing_temperature": 86.0},
        "FAULT_DETECTED", "FM-MISALIGNMENT", "RC-INSTALL-MISALIGN", expected_secondary=("FM-BEARING",)),
    Scenario(
        "bearing_degradation",
        "DE bearing failing: hot bearing, high radial vibration, axial vibration normal.",
        {"bearing_temperature": 88.0, "radial_vibration": 6.0, "axial_vibration": 2.0},
        "FAULT_DETECTED", "FM-BEARING", "RC-LUBRICATION"),
    Scenario(
        "impeller_imbalance",
        "Fouled impeller: high radial 1x vibration, everything else healthy.",
        {"radial_vibration": 6.5, "axial_vibration": 1.8, "bearing_temperature": 65.0},
        "FAULT_DETECTED", "FM-IMBALANCE", "RC-IMPELLER-FOULING"),
    Scenario(
        "seal_leak",
        "Mechanical seal leaking with warm seal chamber.",
        {"seal_leak": 1, "seal_chamber_temperature": 76.0},
        "FAULT_DETECTED", "FM-SEAL-LEAK", "RC-SEAL-FACE-WEAR"),
    Scenario(
        "impeller_wear",
        "Worn wear rings: head 19 % below the OEM curve at the measured flow, NPSH margin healthy.",
        {"suction_pressure": 1.65, "discharge_pressure": 5.8, "flow": 88.0, "motor_current": 31.0},
        "FAULT_DETECTED", "FM-IMPELLER-WEAR", "RC-WEAR-RING-WEAR"),
    Scenario(
        "motor_overload_runout",
        "Bypass opened downstream: pump runs out to 138 m3/h, motor above full-load current.",
        {"suction_pressure": 1.2, "discharge_pressure": 5.2, "flow": 138.0, "motor_current": 43.0,
         "motor_winding_temperature": 148.0},
        "FAULT_DETECTED", "FM-OVERLOAD", "RC-RUNOUT"),
    Scenario(
        "discrete_states_input",
        "Only discrete alarm states are available (no numeric values, so no physics-derived signals).",
        {"PT-101": "LOW", "VT-101": "HIGH", "PT-102": "LOW", "FT-101": "LOW"},
        "FAULT_DETECTED", "FM-CAVITATION", "RC-SUCTION-VALVE-THROTTLED", use_baseline=False),
    Scenario(
        "pump_stopped",
        "Motor not running: diagnosis is suppressed.",
        {"motor_current": 0.4, "flow": 0.0, "discharge_pressure": 1.6},
        "NOT_RUNNING"),
)
