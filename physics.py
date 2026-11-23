"""
First-principles pump physics used to compute *virtual sensors*.

Raw pressures alone cannot tell cavitation from impeller wear; the physically
meaningful quantities are

* NPSH margin  = NPSH_available / NPSH_required   (cavitation risk)
* Head ratio   = H_measured / H_curve(Q)           (hydraulic health vs. the OEM curve)

All pressures are absolute, in bar(a).  Fluid: water.
"""

from __future__ import annotations

import math

G = 9.80665  # m/s^2
BAR_TO_PA = 1.0e5
MMHG_TO_PA = 133.322


def water_vapor_pressure_bar(temp_c: float) -> float:
    """Saturation pressure of water, Antoine equation (1-374 degC), in bar(a)."""
    if temp_c <= 100.0:
        a, b, c = 8.07131, 1730.63, 233.426
    else:
        a, b, c = 8.14019, 1810.94, 244.485
    p_mmhg = 10.0 ** (a - b / (c + temp_c))
    return p_mmhg * MMHG_TO_PA / BAR_TO_PA


def water_density(temp_c: float) -> float:
    """Density of liquid water in kg/m^3 (Thiesen-type correlation, 0-100 degC, <0.1 % error)."""
    t = temp_c
    return 1000.0 * (1.0 - (t + 288.9414) * (t - 3.9863) ** 2 / (508929.2 * (t + 68.12963)))


def velocity_head_m(flow_m3h: float, pipe_id_m: float) -> float:
    """v^2 / 2g for a given volumetric flow in a circular pipe."""
    area = math.pi * pipe_id_m ** 2 / 4.0
    velocity = (max(flow_m3h, 0.0) / 3600.0) / area
    return velocity ** 2 / (2.0 * G)


def npsh_available_m(p_suction_bar_a: float, temp_c: float, flow_m3h: float, pipe_id_m: float) -> float:
    """
    NPSHa at the suction flange:
        NPSHa = (p_s - p_v) / (rho g) + v_s^2 / 2g
    (gauge-to-impeller-centreline elevation correction neglected).
    """
    rho = water_density(temp_c)
    p_v = water_vapor_pressure_bar(temp_c)
    return (p_suction_bar_a - p_v) * BAR_TO_PA / (rho * G) + velocity_head_m(flow_m3h, pipe_id_m)


def npsh_required_m(flow_m3h: float, rated_flow_m3h: float, npshr_rated_m: float) -> float:
    """
    NPSH3 approximation: flat up to the rated (BEP) flow, rising with Q^2 beyond it.
    Conservative simplification of a typical OEM NPSH3 curve.
    """
    ratio = max(flow_m3h, 0.0) / rated_flow_m3h
    return npshr_rated_m * max(1.0, ratio ** 2)


def pump_curve_head_m(flow_m3h: float, shutoff_head_m: float, rated_flow_m3h: float, rated_head_m: float) -> float:
    """Parabolic H-Q curve through (0, H0) and (Q_rated, H_rated):  H = H0 - k Q^2."""
    k = (shutoff_head_m - rated_head_m) / rated_flow_m3h ** 2
    return shutoff_head_m - k * max(flow_m3h, 0.0) ** 2


def developed_head_m(p_suction_bar_a: float, p_discharge_bar_a: float, temp_c: float) -> float:
    """Pump total head from the pressure rise (velocity-head difference and elevation neglected)."""
    return (p_discharge_bar_a - p_suction_bar_a) * BAR_TO_PA / (water_density(temp_c) * G)


def compute_virtual_signals(values: dict[str, float], params: dict) -> dict[str, float]:
    """
    Compute the pump's virtual sensors from numeric raw readings.

    `values` - raw numeric readings keyed by signal key.
    `params` - nameplate parameters of the pump (see knowledge_base.py).
    A virtual signal is returned only if all of its inputs are available.
    Fluid temperature defaults to the design temperature if not measured.
    """
    out: dict[str, float] = {}
    temp = values.get("fluid_temperature", params["design_fluid_temp_c"])
    q = values.get("flow")
    p_s = values.get("suction_pressure")
    p_d = values.get("discharge_pressure")

    if p_s is not None and q is not None:
        npsha = npsh_available_m(p_s, temp, q, params["suction_pipe_id_m"])
        npshr = npsh_required_m(q, params["rated_flow_m3h"], params["npshr_rated_m"])
        out["npsh_available_m"] = npsha
        out["npsh_required_m"] = npshr
        out["npsh_margin"] = npsha / npshr

    if p_s is not None and p_d is not None and q is not None:
        h_meas = developed_head_m(p_s, p_d, temp)
        h_curve = pump_curve_head_m(q, params["shutoff_head_m"], params["rated_flow_m3h"], params["rated_head_m"])
        out["head_measured_m"] = h_meas
        out["head_curve_m"] = h_curve
        out["head_ratio"] = h_meas / h_curve

    return out
