"""
Phase 1 - Instance data for the selected asset: centrifugal pump P-101.

Asset: end-suction centrifugal pump in a cooling-water circuit.
    Duty point: 100 m3/h @ 50 m, 2950 rpm, 22 kW induction motor (400 V, FLA 40 A).
    Process flow:  TK-100 -> STR-100 -> HV-101 -> P-101 -> NRV-102 -> HV-102

Everything the diagnostic engine knows comes from here (via the graph); the
engine itself contains no pump-specific logic.

Alarm limits are engineering values chosen to be consistent with the pump
curve (see physics.py): e.g. the discharge-pressure NORMAL band 5.6-7.2 bar(a)
corresponds to the API 610 Preferred Operating Region (70-120 % of rated flow).
Vibration limits follow ISO 10816-3, Group 2 machines (15-300 kW), flexible
support: 4.5 mm/s = zone C (alert), 7.1 mm/s = zone D (danger).
Root-cause `prior` values are engineering-judgement estimates that a real
deployment would replace with CMMS failure statistics.
"""

from __future__ import annotations

from ontology import (
    ActionType,
    Component,
    Equipment,
    FailureMode,
    FunctionalLocation,
    KnowledgeBase,
    MaintenanceAction,
    RootCause,
    Sensor,
    Severity,
    State,
    SymptomRule as S,
    VirtualSensor,
)

LOW_LOW, LOW, NORMAL, HIGH, HIGH_HIGH = (State.LOW_LOW, State.LOW, State.NORMAL, State.HIGH, State.HIGH_HIGH)

PUMP = "P-101"

# --------------------------------------------------------------------------- #
# Plant hierarchy and equipment
# --------------------------------------------------------------------------- #
LOCATIONS = (
    FunctionalLocation("Plant1", "Plant 1", "Site"),
    FunctionalLocation("Utilities", "Utilities Area", "Area", parent="Plant1"),
    FunctionalLocation("CoolingWater", "Cooling Water System", "System", parent="Utilities"),
)

PUMP_PARAMETERS = {
    "rated_flow_m3h": 100.0,
    "rated_head_m": 50.0,
    "shutoff_head_m": 60.0,          # 120 % of rated head (typical radial impeller)
    "npshr_rated_m": 4.0,            # NPSH3 at rated flow
    "speed_rpm": 2950,
    "motor_rated_power_kw": 22.0,
    "motor_full_load_current_a": 40.0,
    "suction_pipe_id_m": 0.125,      # DN125
    "design_fluid_temp_c": 30.0,
    "fluid": "cooling water",
    "por_flow_m3h": [70.0, 120.0],   # API 610 Preferred Operating Region (70-120 % BEP)
    "running_current_threshold_a": 3.0,  # below this the motor is considered stopped
}

EQUIPMENT = (
    Equipment("TK-100", "Cooling water suction tank", "Tank", "CoolingWater", feeds=("STR-100",)),
    Equipment("STR-100", "Suction strainer", "Strainer", "CoolingWater", feeds=("HV-101",)),
    Equipment("HV-101", "Suction isolation valve", "ManualValve", "CoolingWater", feeds=(PUMP,)),
    Equipment(PUMP, "Cooling water pump", "CentrifugalPump", "CoolingWater",
              parameters=PUMP_PARAMETERS, feeds=("NRV-102",)),
    Equipment("NRV-102", "Discharge check valve", "CheckValve", "CoolingWater", feeds=("HV-102",)),
    Equipment("HV-102", "Discharge isolation valve", "ManualValve", "CoolingWater"),
)

COMPONENTS = (
    Component("P-101.CASING", "Volute casing", "Casing", PUMP),
    Component("P-101.IMPELLER", "Closed impeller", "Impeller", PUMP, {"vanes": 6}),
    Component("P-101.WEAR-RINGS", "Wear rings", "WearRing", PUMP, {"design_clearance_mm": 0.35}),
    Component("P-101.SHAFT", "Pump shaft", "Shaft", PUMP),
    Component("P-101.BRG-DE", "Drive-end bearing", "RollingBearing", PUMP, {"type": "6310 C3"}),
    Component("P-101.BRG-NDE", "Non-drive-end bearing", "RollingBearing", PUMP, {"type": "7310 BECBM pair"}),
    Component("P-101.SEAL", "Mechanical seal", "MechanicalSeal", PUMP, {"flush_plan": "API 682 Plan 11"}),
    Component("P-101.COUPLING", "Flexible coupling", "Coupling", PUMP),
    Component("P-101.MOTOR", "Induction motor", "ElectricMotor", PUMP,
              {"rated_power_kw": 22.0, "voltage_v": 400, "insulation_class": "F"}),
    Component("P-101.BASEPLATE", "Baseplate and foundation", "Baseplate", PUMP),
)

# --------------------------------------------------------------------------- #
# Sensors (physical) and virtual sensors (physics-derived)
# --------------------------------------------------------------------------- #
SENSORS = (
    Sensor("PT-101", "suction_pressure", "Suction pressure", "pressure", "bar(a)", "P-101.CASING",
           lo_lo=0.6, lo=0.9, hi=2.5, normal_value=1.6),
    Sensor("PT-102", "discharge_pressure", "Discharge pressure", "pressure", "bar(a)", "P-101.CASING",
           lo_lo=3.0, lo=5.6, hi=7.2, hi_hi=8.0, normal_value=6.5,
           note="NORMAL band = pump curve over the Preferred Operating Region"),
    Sensor("FT-101", "flow", "Discharge flow", "volumetric flow", "m3/h", PUMP,
           lo_lo=10.0, lo=70.0, hi=120.0, hi_hi=135.0, normal_value=100.0,
           note="LOW/HIGH = outside API 610 Preferred Operating Region"),
    Sensor("VT-101", "radial_vibration", "DE bearing radial vibration", "vibration velocity (RMS)", "mm/s",
           "P-101.BRG-DE", hi=4.5, hi_hi=7.1, normal_value=2.0, note="ISO 10816-3 Group 2, flexible"),
    Sensor("VT-102", "axial_vibration", "DE bearing axial vibration", "vibration velocity (RMS)", "mm/s",
           "P-101.BRG-DE", hi=4.5, hi_hi=7.1, normal_value=1.5, note="ISO 10816-3 Group 2, flexible"),
    Sensor("TT-101", "bearing_temperature", "DE bearing temperature", "temperature", "degC", "P-101.BRG-DE",
           hi=82.0, hi_hi=93.0, normal_value=60.0),
    Sensor("TT-102", "fluid_temperature", "Pumped fluid temperature", "temperature", "degC", PUMP,
           hi=45.0, hi_hi=60.0, normal_value=30.0),
    Sensor("TT-103", "motor_winding_temperature", "Motor winding temperature", "temperature", "degC",
           "P-101.MOTOR", hi=140.0, hi_hi=155.0, normal_value=95.0, note="Class F insulation (155 degC)"),
    Sensor("TT-104", "seal_chamber_temperature", "Seal chamber temperature", "temperature", "degC", "P-101.SEAL",
           hi=70.0, hi_hi=90.0, normal_value=45.0),
    Sensor("IT-101", "motor_current", "Motor current", "electric current", "A", "P-101.MOTOR",
           lo=22.0, hi=40.0, hi_hi=44.0, normal_value=33.0, note="hi = full-load current"),
    Sensor("LS-101", "seal_leak", "Seal leak detector", "leak switch (0/1)", "-", "P-101.SEAL",
           hi=0.5, normal_value=0.0),
    Sensor("LT-100", "suction_tank_level", "Suction tank level", "level", "%", "TK-100",
           lo_lo=10.0, lo=20.0, hi=95.0, normal_value=70.0),
    Sensor("PDT-100", "strainer_dp", "Suction strainer differential pressure", "differential pressure", "bar",
           "STR-100", hi=0.3, hi_hi=0.6, normal_value=0.05),
)

VIRTUAL_SENSORS = (
    VirtualSensor("NPSH-101", "npsh_margin", "NPSH margin ratio", "-", ("PT-101", "TT-102", "FT-101"),
                  "NPSHa / NPSH3 with NPSHa = (p_s - p_v(T)) / (rho g) + v^2/2g",
                  lo_lo=1.1, lo=1.3, note="margin < 1.3 => cavitation risk"),
    VirtualSensor("HEAD-101", "head_ratio", "Head ratio vs. OEM curve", "-", ("PT-101", "PT-102", "TT-102", "FT-101"),
                  "H_measured / H_curve(Q) with H_measured = (p_d - p_s) / (rho g)",
                  lo_lo=0.8, lo=0.9, hi=1.1, note="< 0.9 => pump develops less head than its curve"),
)

# --------------------------------------------------------------------------- #
# Maintenance actions
# --------------------------------------------------------------------------- #
_I, _C, _D = ActionType.IMMEDIATE, ActionType.CORRECTIVE, ActionType.DIAGNOSTIC
ACTIONS = (
    MaintenanceAction("A-STOP-PUMP", "Stop the pump and start the standby unit to prevent further damage", _I),
    MaintenanceAction("A-PLAN-SHUTDOWN", "Plan a controlled shutdown and switch to the standby pump", _I),
    MaintenanceAction("A-REDUCE-FLOW", "Throttle the discharge valve to return flow to the Preferred Operating Region (70-120 m3/h)", _I),
    MaintenanceAction("A-LIMIT-FLOW", "Temporarily reduce flow (throttle discharge) to cut suction-side losses until the cause is removed", _I),
    MaintenanceAction("A-OPEN-DISCHARGE", "Open the discharge valve / restore the discharge path; verify minimum-flow bypass is open", _I),
    MaintenanceAction("A-RESTORE-SUCTION", "Restore suction supply (tank level, suction valve) and re-prime before restart", _I),
    MaintenanceAction("A-NPSH-CHECK", "Recalculate NPSHa vs. NPSH3 at the actual flow and fluid temperature", _D),
    MaintenanceAction("A-VIB-SPECTRUM", "Take an FFT vibration spectrum (1x, 2x, bearing defect frequencies) to confirm the mechanical fault", _D),
    MaintenanceAction("A-PERF-TEST", "Run a performance test and compare measured H-Q points with the OEM curve", _D),
    MaintenanceAction("A-LUBE-CHECK", "Check bearing lubricant level, condition and contamination (oil analysis)", _D),
    MaintenanceAction("A-ELEC-CHECK", "Check supply voltage balance, motor insulation resistance and overload relay settings", _D),
    MaintenanceAction("A-CLEAN-STRAINER", "Clean or replace the suction strainer element", _C),
    MaintenanceAction("A-RESTORE-LEVEL", "Restore suction tank level; check level control and make-up supply", _C),
    MaintenanceAction("A-COOL-FLUID", "Restore fluid cooling (check upstream cooler) to lower the vapour pressure", _C),
    MaintenanceAction("A-OPEN-SUCTION-VALVE", "Fully open the suction isolation valve (never throttle on the suction side)", _C),
    MaintenanceAction("A-REPRIME", "Vent the casing and re-prime; locate and seal air-ingress points", _C),
    MaintenanceAction("A-ALIGN", "Laser-align pump and motor shafts to coupling tolerance", _C),
    MaintenanceAction("A-PIPE-SUPPORT", "Check pipe supports and remove nozzle loads (pipe strain)", _C),
    MaintenanceAction("A-SOFT-FOOT", "Check and correct soft foot, grout and anchor bolts", _C),
    MaintenanceAction("A-RELUBRICATE", "Replenish or replace the lubricant with the specified grade", _C),
    MaintenanceAction("A-REPLACE-BEARING", "Replace the drive-end bearing; inspect shaft and housing fits", _C),
    MaintenanceAction("A-BALANCE-IMPELLER", "Clean and dynamically balance the impeller (ISO 21940-11 grade G6.3)", _C),
    MaintenanceAction("A-REPLACE-IMPELLER", "Inspect and repair or replace the impeller", _C),
    MaintenanceAction("A-RESTORE-CLEARANCE", "Replace wear rings to restore the design running clearance", _C),
    MaintenanceAction("A-REPLACE-SEAL", "Replace seal faces and elastomers; verify the seal flush plan", _C),
    MaintenanceAction("A-REPAIR-CHECK-VALVE", "Inspect and repair the discharge check valve", _C),
    MaintenanceAction("A-CLEAR-DISCHARGE", "Inspect and clear the discharge line / downstream blockage", _C),
    MaintenanceAction("A-FREE-ROTOR", "With the pump isolated, check the rotor turns freely; inspect for rubbing", _C),
    MaintenanceAction("A-CHECK-FLUID", "Verify fluid density and viscosity against the pump datasheet", _C),
)

# --------------------------------------------------------------------------- #
# Failure modes: symptom signatures + physics of the mechanism
# --------------------------------------------------------------------------- #
FAILURE_MODES = (
    FailureMode(
        "FM-CAVITATION", "Cavitation", "P-101.IMPELLER", Severity.HIGH,
        "Vapour bubbles form at the impeller eye and collapse inside the impeller.",
        "When the absolute pressure at the impeller eye approaches the fluid's vapour pressure "
        "(NPSHa approaching NPSH3), vapour bubbles form and then collapse violently in higher-pressure "
        "regions. The implosions cause broadband vibration and noise, pit the vanes, and vapour blocking "
        "the passages breaks down the developed head and flow.",
        symptoms=(
            S("npsh_margin", LOW, 3, key_symptom=True),
            S("suction_pressure", LOW, 2),
            S("radial_vibration", HIGH, 2),
            S("head_ratio", LOW, 1),
            S("discharge_pressure", LOW, 1),
            S("flow", LOW, 1),
        ),
        propagates_to=("FM-IMPELLER-WEAR", "FM-SEAL-LEAK"),
        actions=("A-LIMIT-FLOW", "A-NPSH-CHECK"),
    ),
    FailureMode(
        "FM-BEARING", "Drive-end bearing degradation", "P-101.BRG-DE", Severity.HIGH,
        "Rolling-element bearing distress (lubrication failure, contamination or fatigue).",
        "Loss of the lubricant film, contamination or fatigue spalling increases rolling friction, raising "
        "bearing temperature; defects on races or rolling elements generate impacts that raise radial "
        "vibration. Higher temperature degrades the lubricant further, a self-accelerating loop.",
        symptoms=(
            S("bearing_temperature", HIGH, 3, key_symptom=True),
            S("radial_vibration", HIGH, 2),
        ),
        actions=("A-VIB-SPECTRUM", "A-LUBE-CHECK", "A-PLAN-SHUTDOWN"),
    ),
    FailureMode(
        "FM-MISALIGNMENT", "Pump-motor shaft misalignment", "P-101.COUPLING", Severity.MEDIUM,
        "Angular or parallel offset between pump and motor shafts.",
        "The coupling is forced to flex every revolution, producing cyclic reaction forces that are "
        "characteristically strong in the axial direction (1x and 2x running speed) and add load to the "
        "bearings, which raises bearing temperature.",
        symptoms=(
            S("axial_vibration", HIGH, 3, key_symptom=True),
            S("radial_vibration", HIGH, 1.5),
            S("bearing_temperature", HIGH, 1),
        ),
        propagates_to=("FM-BEARING", "FM-SEAL-LEAK"),
        actions=("A-VIB-SPECTRUM",),
    ),
    FailureMode(
        "FM-IMBALANCE", "Rotor / impeller imbalance", "P-101.IMPELLER", Severity.MEDIUM,
        "Mass centre of the rotor offset from its axis of rotation.",
        "The offset mass creates a centrifugal force F = m*e*omega^2 rotating at 1x running speed, giving "
        "predominantly radial vibration. Hydraulic performance (head, NPSH) is unaffected, which "
        "distinguishes imbalance from cavitation: while the NPSH margin is low, vibration cannot be "
        "attributed to imbalance.",
        symptoms=(
            S("radial_vibration", HIGH, 3, key_symptom=True),
            S("npsh_margin", NORMAL, 2, key_symptom=True),
            S("axial_vibration", NORMAL, 1.5),
            S("bearing_temperature", NORMAL, 2),
            S("head_ratio", NORMAL, 1.5),
        ),
        propagates_to=("FM-BEARING",),
        actions=("A-VIB-SPECTRUM",),
    ),
    FailureMode(
        "FM-SEAL-LEAK", "Mechanical seal leakage", "P-101.SEAL", Severity.HIGH,
        "Process fluid escaping across the mechanical seal faces.",
        "The seal relies on a thin fluid film between rotating and stationary faces. Face wear, thermal "
        "distortion or heat-checking (e.g. after dry running) and elastomer degradation open the gap; "
        "leakage trips the leak detector and frictional heat raises the seal-chamber temperature.",
        symptoms=(
            S("seal_leak", HIGH, 3, key_symptom=True),
            S("seal_chamber_temperature", HIGH, 1),
        ),
        actions=("A-PLAN-SHUTDOWN",),
    ),
    FailureMode(
        "FM-DRY-RUN", "Dry running (loss of suction)", PUMP, Severity.CRITICAL,
        "The pump runs without liquid in the casing.",
        "Without liquid the impeller cannot develop head: discharge pressure collapses toward suction "
        "pressure and the head ratio falls to near zero. The motor only supplies mechanical losses, so "
        "current drops well below normal. The seal faces lose their lubricating and cooling film and "
        "overheat within minutes.",
        symptoms=(
            S("flow", LOW_LOW, 3, key_symptom=True),
            S("discharge_pressure", LOW, 2, key_symptom=True),
            S("motor_current", LOW, 2),
            S("head_ratio", LOW, 1),
            S("seal_chamber_temperature", HIGH, 1.5),
        ),
        propagates_to=("FM-SEAL-LEAK",),
        actions=("A-STOP-PUMP", "A-RESTORE-SUCTION"),
    ),
    FailureMode(
        "FM-DEADHEAD", "Deadheading (blocked discharge)", PUMP, Severity.CRITICAL,
        "The pump runs against a closed or blocked discharge.",
        "The pump operates at shut-off: pressure rises to the shut-off head while flow is ~zero. For a "
        "radial centrifugal pump absorbed power is minimum at shut-off, so motor current FALLS (it does not "
        "rise). All absorbed power heats the trapped liquid, so seal-chamber temperature climbs and the "
        "liquid can eventually flash to vapour.",
        symptoms=(
            S("flow", LOW_LOW, 3, key_symptom=True),
            S("discharge_pressure", HIGH, 3, key_symptom=True),
            S("motor_current", LOW, 2),
            S("seal_chamber_temperature", HIGH, 1),
            S("head_ratio", NORMAL, 1),
        ),
        actions=("A-OPEN-DISCHARGE", "A-STOP-PUMP"),
    ),
    FailureMode(
        "FM-IMPELLER-WEAR", "Hydraulic degradation (impeller / wear-ring wear)", "P-101.IMPELLER", Severity.MEDIUM,
        "Pump develops less head than its OEM curve at the same flow.",
        "Vane erosion and opened wear-ring clearances increase internal recirculation from discharge back "
        "to suction, so the pump develops less head than its curve at the measured flow (head ratio < 1). "
        "NPSH margin stays healthy and the motor stays loaded, which separates wear from cavitation and "
        "from loss of suction.",
        symptoms=(
            S("head_ratio", LOW, 3, key_symptom=True),
            S("npsh_margin", NORMAL, 3, key_symptom=True),
            S("motor_current", NORMAL, 2, key_symptom=True),
            S("discharge_pressure", LOW, 1),
            S("flow", LOW, 1),
        ),
        actions=("A-PERF-TEST",),
    ),
    FailureMode(
        "FM-OVERLOAD", "Motor overload", "P-101.MOTOR", Severity.HIGH,
        "Motor draws more than its full-load current.",
        "Motor current tracks shaft load. For a radial centrifugal pump absorbed power rises with flow, so "
        "running far right on the curve (runout) overloads the motor; mechanical drag, a denser fluid or a "
        "low/unbalanced supply voltage do the same. The extra I^2R losses raise the winding temperature "
        "toward the insulation limit.",
        symptoms=(
            S("motor_current", HIGH, 3, key_symptom=True),
            S("motor_winding_temperature", HIGH, 2),
        ),
        actions=("A-REDUCE-FLOW", "A-ELEC-CHECK"),
    ),
)

# --------------------------------------------------------------------------- #
# Root causes: what makes each failure mode happen, and how to confirm it
# --------------------------------------------------------------------------- #
ROOT_CAUSES = (
    RootCause("RC-STRAINER-CLOGGED", "Clogged suction strainer",
              "Debris on the strainer adds suction-side pressure loss, lowering NPSHa.",
              causes=("FM-CAVITATION", "FM-DRY-RUN"),
              evidence=(S("strainer_dp", HIGH, 3, key_symptom=True),),
              located_in="STR-100", prior=0.7, remedies=("A-CLEAN-STRAINER",)),
    RootCause("RC-LOW-TANK-LEVEL", "Low suction tank level",
              "Reduced static head (and possible vortexing) lowers NPSHa.",
              causes=("FM-CAVITATION",),
              evidence=(S("suction_tank_level", LOW, 3, key_symptom=True),),
              located_in="TK-100", prior=0.6, remedies=("A-RESTORE-LEVEL",)),
    RootCause("RC-HIGH-FLUID-TEMP", "High fluid temperature",
              "Vapour pressure rises steeply with temperature, consuming the NPSH margin.",
              causes=("FM-CAVITATION",),
              evidence=(S("fluid_temperature", HIGH, 3, key_symptom=True),),
              located_in=PUMP, prior=0.4, remedies=("A-COOL-FLUID",)),
    RootCause("RC-SUCTION-VALVE-THROTTLED", "Partially closed suction valve",
              "A throttled suction valve adds pressure loss after the strainer. Confirmed by elimination: "
              "low suction pressure with normal strainer DP, tank level and fluid temperature.",
              causes=("FM-CAVITATION",),
              evidence=(S("suction_pressure", LOW, 2, key_symptom=True), S("strainer_dp", NORMAL, 2),
                        S("suction_tank_level", NORMAL, 1), S("fluid_temperature", NORMAL, 2)),
              located_in="HV-101", prior=0.3, remedies=("A-OPEN-SUCTION-VALVE",)),
    RootCause("RC-RUNOUT", "Runout operation (low system resistance)",
              "Flow far beyond rated raises NPSH3, suction losses and absorbed power.",
              causes=("FM-CAVITATION", "FM-OVERLOAD"),
              evidence=(S("flow", HIGH, 3, key_symptom=True), S("discharge_pressure", LOW, 1)),
              located_in=PUMP, prior=0.5, remedies=("A-REDUCE-FLOW",)),
    RootCause("RC-EMPTY-TANK", "Empty suction tank",
              "No liquid reaches the pump suction.",
              causes=("FM-DRY-RUN",),
              evidence=(S("suction_tank_level", LOW_LOW, 3, key_symptom=True),),
              located_in="TK-100", prior=0.6, remedies=("A-RESTORE-LEVEL", "A-REPRIME")),
    RootCause("RC-SUCTION-VALVE-CLOSED", "Closed suction valve",
              "Suction isolation valve left closed; pump pulls a vacuum on its own casing.",
              causes=("FM-DRY-RUN",),
              evidence=(S("suction_pressure", LOW, 2, key_symptom=True),
                        S("suction_tank_level", NORMAL, 2)),
              located_in="HV-101", prior=0.4, remedies=("A-OPEN-SUCTION-VALVE",)),
    RootCause("RC-LOSS-OF-PRIME", "Air ingress / loss of prime",
              "Air leaking into the suction line or casing displaces the liquid.",
              causes=("FM-DRY-RUN",), located_in="P-101.CASING", prior=0.3, remedies=("A-REPRIME",)),
    RootCause("RC-LUBRICATION", "Insufficient or degraded lubrication",
              "Low oil level, wrong grade or oxidised lubricant.",
              causes=("FM-BEARING",), located_in="P-101.BRG-DE", prior=0.7, remedies=("A-RELUBRICATE",)),
    RootCause("RC-LUBE-CONTAMINATION", "Lubricant contamination",
              "Water or particles in the lubricant damage raceways.",
              causes=("FM-BEARING",), located_in="P-101.BRG-DE", prior=0.5,
              remedies=("A-RELUBRICATE", "A-REPLACE-BEARING")),
    RootCause("RC-BEARING-FATIGUE", "Bearing fatigue (end of L10 life)",
              "Sub-surface fatigue spalling after the rated number of cycles.",
              causes=("FM-BEARING",), located_in="P-101.BRG-DE", prior=0.3, remedies=("A-REPLACE-BEARING",)),
    RootCause("RC-INSTALL-MISALIGN", "Improper alignment after installation / overhaul",
              "Shafts not aligned to tolerance when the pump was last installed.",
              causes=("FM-MISALIGNMENT",), located_in="P-101.COUPLING", prior=0.6, remedies=("A-ALIGN",)),
    RootCause("RC-PIPE-STRAIN", "Pipe strain on pump nozzles",
              "Piping forces distort the casing and move the pump shaft.",
              causes=("FM-MISALIGNMENT",), located_in="P-101.CASING", prior=0.4,
              remedies=("A-PIPE-SUPPORT", "A-ALIGN")),
    RootCause("RC-SOFT-FOOT", "Soft foot / foundation settlement",
              "Uneven support lets the machine move when bolts are tightened or under load.",
              causes=("FM-MISALIGNMENT",), located_in="P-101.BASEPLATE", prior=0.3,
              remedies=("A-SOFT-FOOT", "A-ALIGN")),
    RootCause("RC-IMPELLER-FOULING", "Uneven impeller fouling",
              "Non-uniform deposits shift the impeller's mass centre.",
              causes=("FM-IMBALANCE",), located_in="P-101.IMPELLER", prior=0.5, remedies=("A-BALANCE-IMPELLER",)),
    RootCause("RC-IMPELLER-DAMAGE", "Impeller damage (foreign object / broken vane)",
              "Missing or damaged vane material unbalances the rotor and reduces head.",
              causes=("FM-IMBALANCE", "FM-IMPELLER-WEAR"), located_in="P-101.IMPELLER", prior=0.3,
              remedies=("A-REPLACE-IMPELLER",)),
    RootCause("RC-WEAR-RING-WEAR", "Excessive wear-ring clearance",
              "Abrasive wear opens the running clearance and increases internal recirculation.",
              causes=("FM-IMPELLER-WEAR",), located_in="P-101.WEAR-RINGS", prior=0.6,
              remedies=("A-RESTORE-CLEARANCE",)),
    RootCause("RC-SEAL-FACE-WEAR", "Seal face wear (end of life)",
              "Normal wear of the carbon / silicon-carbide faces.",
              causes=("FM-SEAL-LEAK",), located_in="P-101.SEAL", prior=0.6, remedies=("A-REPLACE-SEAL",)),
    RootCause("RC-ELASTOMER-DEGRADATION", "Seal elastomer degradation",
              "O-rings harden or swell from temperature or chemical attack.",
              causes=("FM-SEAL-LEAK",), evidence=(S("fluid_temperature", HIGH, 2),),
              located_in="P-101.SEAL", prior=0.3, remedies=("A-REPLACE-SEAL",)),
    RootCause("RC-DISCHARGE-VALVE-CLOSED", "Closed discharge valve",
              "Discharge isolation valve closed or not opened after start-up.",
              causes=("FM-DEADHEAD",), located_in="HV-102", prior=0.6, remedies=("A-OPEN-DISCHARGE",)),
    RootCause("RC-CHECK-VALVE-STUCK", "Check valve stuck closed",
              "The non-return valve disc is jammed shut.",
              causes=("FM-DEADHEAD",), located_in="NRV-102", prior=0.3, remedies=("A-REPAIR-CHECK-VALVE",)),
    RootCause("RC-DISCHARGE-BLOCKED", "Downstream line blockage",
              "Debris or a closed valve further downstream blocks the discharge line.",
              causes=("FM-DEADHEAD",), located_in="HV-102", prior=0.2, remedies=("A-CLEAR-DISCHARGE",)),
    RootCause("RC-MECH-BINDING", "Mechanical binding / rubbing",
              "Rotor rubbing on stationary parts adds friction load.",
              causes=("FM-OVERLOAD",),
              evidence=(S("radial_vibration", HIGH, 1.5), S("bearing_temperature", HIGH, 1.5)),
              located_in="P-101.SHAFT", prior=0.3, remedies=("A-FREE-ROTOR",)),
    RootCause("RC-SUPPLY-VOLTAGE", "Low or unbalanced supply voltage",
              "For the same load, lower or unbalanced voltage increases motor current.",
              causes=("FM-OVERLOAD",), located_in="P-101.MOTOR", prior=0.4, remedies=("A-ELEC-CHECK",)),
    RootCause("RC-DENSE-FLUID", "Fluid denser or more viscous than design",
              "Absorbed power is proportional to density (and rises with viscosity).",
              causes=("FM-OVERLOAD",), located_in=PUMP, prior=0.1, remedies=("A-CHECK-FLUID",)),
)

PUMP_KB = KnowledgeBase(
    locations=LOCATIONS,
    equipment=EQUIPMENT,
    components=COMPONENTS,
    sensors=SENSORS,
    virtual_sensors=VIRTUAL_SENSORS,
    failure_modes=FAILURE_MODES,
    root_causes=ROOT_CAUSES,
    actions=ACTIONS,
    primary_equipment=PUMP,
)
