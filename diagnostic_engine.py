"""
Phase 3 - Rule-based diagnostic engine.

    diagnose_fault(sensor_states) -> dict

The engine is generic: it contains no pump-specific rules.  Everything it
knows (thresholds, symptom signatures, causal links, actions) is read from the
knowledge graph built in graph_builder.py.

Inference pipeline
------------------
1. Normalise inputs      - accept numeric readings or discrete states, by signal key or tag.
2. Virtual sensors       - compute physics-derived signals (NPSH margin, head ratio).
3. Discretise            - map every value to LOW_LOW / LOW / NORMAL / HIGH / HIGH_HIGH.
4. Alarms                - list the Alarm nodes triggered by abnormal states.
5. Match signatures      - score every FailureMode against its Symptom signature.
6. Fault propagation     - mark failure modes explained by an upstream active failure mode.
7. Root-cause isolation  - walk CAUSES edges backwards; confirm/deny each cause with its evidence.
8. Explain               - causal chains, evidence table and recommended actions.

Scoring (fully explainable)
---------------------------
For each expected symptom with weight w:
    MATCH        observed state agrees with the expectation          +w
    ABSENT       expected abnormal but signal is NORMAL (or not yet
                 at the expected LL/HH level)                         0
    CONTRADICTS  opposite direction, or abnormal where NORMAL expected  -w
    UNKNOWN      signal not available                                  0
confidence = clamp( sum / total_weight , 0, 1 )
A *key symptom* that is known but not MATCHed rules the hypothesis out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

import networkx as nx

from graph_builder import build_graph, in_neighbors, nodes_of_type, out_neighbors, signal_index
from ontology import ActionType, NodeType, Relation, Severity, State, alarm_id
from physics import compute_virtual_signals

DEFAULT_MIN_CONFIDENCE = 0.35
SUPPORTED_THRESHOLD = 0.6

_STATE_ALIASES = {
    "LL": State.LOW_LOW, "LOW_LOW": State.LOW_LOW, "LOWLOW": State.LOW_LOW, "VERY_LOW": State.LOW_LOW,
    "L": State.LOW, "LOW": State.LOW,
    "N": State.NORMAL, "NORMAL": State.NORMAL, "OK": State.NORMAL, "OFF": State.NORMAL, "FALSE": State.NORMAL,
    "H": State.HIGH, "HIGH": State.HIGH, "ON": State.HIGH, "ACTIVE": State.HIGH, "TRUE": State.HIGH,
    "HH": State.HIGH_HIGH, "HIGH_HIGH": State.HIGH_HIGH, "HIGHHIGH": State.HIGH_HIGH, "VERY_HIGH": State.HIGH_HIGH,
}
_DIRECTION = {State.LOW_LOW: -1, State.LOW: -1, State.NORMAL: 0, State.HIGH: 1, State.HIGH_HIGH: 1}
_SEVERITY_RANK = {s.value: i for i, s in enumerate(Severity)}
_ACTION_ORDER = {ActionType.IMMEDIATE.value: 0, ActionType.DIAGNOSTIC.value: 1, ActionType.CORRECTIVE.value: 2}
_RC_STATUS_ORDER = {"ACTIVE_FAILURE_MODE": 0, "SUPPORTED": 1, "PARTIAL": 2, "UNVERIFIED": 3,
                    "NOT_SUPPORTED": 4, "RULED_OUT": 5}


@lru_cache(maxsize=1)
def default_graph() -> nx.DiGraph:
    """The pump knowledge graph, built once and reused."""
    return build_graph()


# --------------------------------------------------------------------------- #
# Step 1-3: inputs -> discrete states
# --------------------------------------------------------------------------- #
def classify(value: float, limits: Mapping[str, Any]) -> State:
    """Map a numeric value to a discrete state using a signal's alarm limits."""
    if limits.get("lo_lo") is not None and value < limits["lo_lo"]:
        return State.LOW_LOW
    if limits.get("lo") is not None and value < limits["lo"]:
        return State.LOW
    if limits.get("hi_hi") is not None and value > limits["hi_hi"]:
        return State.HIGH_HIGH
    if limits.get("hi") is not None and value > limits["hi"]:
        return State.HIGH
    return State.NORMAL


def _parse_value(raw: Any) -> float | State | None:
    if isinstance(raw, State):
        return raw
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return None if math.isnan(raw) or math.isinf(raw) else float(raw)
    if isinstance(raw, str):
        alias = _STATE_ALIASES.get(raw.strip().upper().replace(" ", "_").replace("-", "_"))
        if alias is not None:
            return alias
        try:
            return _parse_value(float(raw))
        except ValueError:
            return None
    return None


def _normalise_inputs(g: nx.DiGraph, sensor_states: Mapping[str, Any], warnings: list[str]):
    signals = signal_index(g)
    by_tag = {tag.upper(): key for key, tag in signals.items()}
    numeric: dict[str, float] = {}
    discrete: dict[str, State] = {}
    unknown: list[str] = []
    for name, raw in sensor_states.items():
        key = name if name in signals else by_tag.get(str(name).upper())
        if key is None:
            unknown.append(name)
            continue
        value = _parse_value(raw)
        if value is None:
            warnings.append(f"Ignored unreadable value for '{name}': {raw!r}")
        elif isinstance(value, State):
            discrete[key] = value
        else:
            numeric[key] = value
    if unknown:
        warnings.append(f"Unknown signals ignored: {', '.join(map(str, unknown))}")
    return numeric, discrete, unknown


# --------------------------------------------------------------------------- #
# Step 5: signature matching
# --------------------------------------------------------------------------- #
def compare_state(expected: State, observed: State | None) -> str:
    if observed is None:
        return "UNKNOWN"
    if expected == State.NORMAL:
        return "MATCH" if observed == State.NORMAL else "CONTRADICTS"
    if observed == State.NORMAL:
        return "ABSENT"
    if _DIRECTION[observed] != _DIRECTION[expected]:
        return "CONTRADICTS"
    if expected in (State.LOW_LOW, State.HIGH_HIGH) and observed != expected:
        return "ABSENT"  # right direction but not (yet) at the extreme level
    return "MATCH"


@dataclass
class Evaluation:
    score: float
    coverage: float
    evidence: list[dict] = field(default_factory=list)
    ruled_out_by: str | None = None


def _evaluate(g: nx.DiGraph, owner: str, relation: Relation, states: dict[str, State],
              observations: dict[str, dict]) -> Evaluation:
    signals = signal_index(g)
    total = known = points = 0.0
    evidence: list[dict] = []
    ruled_out_by = None
    for sym in out_neighbors(g, owner, relation):
        edge, node = g.edges[owner, sym], g.nodes[sym]
        key, expected, w = node["key"], State(node["state"]), float(edge["weight"])
        observed = states.get(key)
        outcome = compare_state(expected, observed)
        total += w
        if outcome != "UNKNOWN":
            known += w
        points += {"MATCH": w, "CONTRADICTS": -w}.get(outcome, 0.0)
        if edge["key_symptom"] and outcome not in ("MATCH", "UNKNOWN") and ruled_out_by is None:
            ruled_out_by = f"key symptom '{node['name']}' not present (observed {observed.value})"
        obs = observations.get(key, {})
        evidence.append({
            "symptom": sym, "signal": key, "sensor": signals[key], "name": node["name"],
            "expected": expected.value, "observed": observed.value if observed else None,
            "value": obs.get("value"), "unit": obs.get("unit"),
            "weight": w, "key_symptom": edge["key_symptom"], "outcome": outcome,
        })
    score = max(0.0, min(1.0, points / total)) if total else 0.0
    return Evaluation(score, known / total if total else 0.0, evidence, ruled_out_by)


# --------------------------------------------------------------------------- #
# Step 7: root causes
# --------------------------------------------------------------------------- #
def _action(g: nx.DiGraph, aid: str) -> dict:
    return {"id": aid, "type": g.nodes[aid]["action_type"], "description": g.nodes[aid]["name"]}


def _rank_root_causes(g, fm, states, observations, active: dict[str, float], explained_by: list[str]) -> list[dict]:
    ranked = []
    for upstream in explained_by:  # active upstream failure modes at least as certain as this one
            ranked.append({"id": upstream, "name": g.nodes[upstream]["name"], "kind": "FailureMode",
                           "status": "ACTIVE_FAILURE_MODE", "score": round(active[upstream], 2), "prior": None,
                           "asset_path": _occurs_in_path(g, upstream), "evidence": [], "remedies": []})
    for rc in in_neighbors(g, fm, Relation.CAUSES):
        ev = _evaluate(g, rc, Relation.EVIDENCED_BY, states, observations)
        if not ev.evidence or ev.coverage == 0:
            status, score = "UNVERIFIED", None
        elif ev.ruled_out_by:
            status, score = "RULED_OUT", 0.0
        elif ev.score >= SUPPORTED_THRESHOLD:
            status, score = "SUPPORTED", ev.score
        elif ev.score > 0:
            status, score = "PARTIAL", ev.score
        else:
            status, score = "NOT_SUPPORTED", 0.0
        located = out_neighbors(g, rc, Relation.LOCATED_IN)
        ranked.append({
            "id": rc, "name": g.nodes[rc]["name"], "kind": "RootCause", "status": status,
            "score": None if score is None else round(score, 2), "prior": g.nodes[rc]["prior"],
            "asset_path": g.nodes[located[0]]["asset_path"] if located else None,
            "description": g.nodes[rc]["description"], "evidence": ev.evidence,
            "remedies": [_action(g, a) for a in out_neighbors(g, rc, Relation.REMEDIED_BY)],
        })
    ranked.sort(key=lambda r: (_RC_STATUS_ORDER[r["status"]], -(r["score"] or 0), -(r["prior"] or 0)))
    return ranked


def _occurs_in_path(g: nx.DiGraph, fm: str) -> str:
    return g.nodes[out_neighbors(g, fm, Relation.OCCURS_IN)[0]]["asset_path"]


def _plausible_causes(root_causes: list[dict]) -> list[dict]:
    """Causes backed by sensor evidence; if none, the two most likely unverifiable ones."""
    plausible = [r for r in root_causes if r["status"] in ("ACTIVE_FAILURE_MODE", "SUPPORTED", "PARTIAL")]
    return plausible or [r for r in root_causes if r["status"] == "UNVERIFIED"][:2]


def _causal_chains(g: nx.DiGraph, fm: str, plausible: list[dict], evidence: list[dict]) -> list[dict]:
    matched = [e for e in evidence if e["outcome"] == "MATCH" and e["expected"] != State.NORMAL.value]
    effects = ", ".join(f"{e['name']} [{e['sensor']}]" for e in matched) or "no abnormal symptom"
    chains = []
    for r in plausible:
        rel = "propagates_to" if r["kind"] == "FailureMode" else "causes"
        where = f" @ {r['asset_path']}" if r["asset_path"] else ""
        chains.append({
            "nodes": [r["id"], fm] + [e["symptom"] for e in matched],
            "text": f"{r['name']}{where} --{rel}--> {g.nodes[fm]['name']} --manifests_as--> {effects}",
            "status": r["status"],
        })
    return chains


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def diagnose_fault(sensor_states: Mapping[str, Any], graph: nx.DiGraph | None = None,
                   min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> dict:
    """
    Diagnose the asset from a snapshot of sensor readings.

    sensor_states: {signal key or tag: value}. Values may be numeric
        (e.g. {"suction_pressure": 0.45}) or discrete states
        (e.g. {"PT-101": "LOW", "seal_leak": True}).
    graph: knowledge graph; defaults to the P-101 pump graph.
    min_confidence: failure modes scoring below this are not reported.

    Returns a JSON-serialisable dict (see README for the schema).
    """
    g = graph if graph is not None else default_graph()
    equipment = g.graph["primary_equipment"]
    params = g.nodes[equipment]["parameters"]
    signals = signal_index(g)
    warnings: list[str] = []

    numeric, discrete, unknown = _normalise_inputs(g, sensor_states, warnings)

    # Step 2: virtual sensors from first-principles physics
    derived = compute_virtual_signals(numeric, params) if numeric else {}
    if derived and "fluid_temperature" not in numeric:
        warnings.append(f"fluid_temperature not provided: design value {params['design_fluid_temp_c']} degC "
                        "assumed for NPSH / head calculations")
    derived_keys = [key for key in derived if key in signals and key not in numeric and key not in discrete]
    for key in derived_keys:
        numeric[key] = derived[key]

    # Step 3: discretise
    states: dict[str, State] = dict(discrete)
    observations: dict[str, dict] = {}
    for key, value in numeric.items():
        if key not in discrete:
            states[key] = classify(value, g.nodes[signals[key]])
    for key, state in states.items():
        node = g.nodes[signals[key]]
        observations[key] = {
            "tag": signals[key], "name": node["name"], "unit": node["unit"],
            "value": round(numeric[key], 3) if key in numeric else None, "state": state.value,
            "source": "derived" if key in derived_keys else ("measured" if key in numeric else "state_input"),
        }

    result: dict[str, Any] = {
        "equipment": {"id": equipment, "name": g.nodes[equipment]["name"],
                      "asset_path": g.nodes[equipment]["asset_path"]},
        "status": None, "priority": None,
        "observations": observations,
        "derived_quantities": {k: round(v, 3) for k, v in derived.items()},
        "active_alarms": [], "diagnoses": [], "ruled_out": [], "below_threshold": [],
        "unknown_inputs": unknown, "warnings": warnings,
    }

    # Machine stopped: symptoms are meaningless
    current = numeric.get("motor_current")
    if current is not None and current < params.get("running_current_threshold_a", 0):
        result["status"] = "NOT_RUNNING"
        warnings.append("Motor current indicates the pump is stopped; no diagnosis performed")
        return result

    # Step 4: alarms
    for key, state in states.items():
        aid = alarm_id(signals[key], state) if state != State.NORMAL else None
        if aid and aid in g:
            a = g.nodes[aid]
            result["active_alarms"].append({"id": aid, "severity": a["severity"], "message": a["message"]})
        elif aid:
            result["active_alarms"].append({"id": aid, "severity": Severity.MEDIUM.value,
                                            "message": f"{g.nodes[signals[key]]['name']} {state.value}"})

    # Step 5: score every failure mode
    evaluations: dict[str, Evaluation] = {}
    for fm in nodes_of_type(g, NodeType.FAILURE_MODE):
        ev = _evaluate(g, fm, Relation.MANIFESTS_AS, states, observations)
        if ev.ruled_out_by:
            result["ruled_out"].append({"id": fm, "name": g.nodes[fm]["name"], "reason": ev.ruled_out_by})
        elif ev.score < min_confidence:
            result["below_threshold"].append({"id": fm, "name": g.nodes[fm]["name"], "confidence": round(ev.score, 2)})
        else:
            evaluations[fm] = ev
    active = {fm: ev.score for fm, ev in evaluations.items()}

    # Steps 6-8: propagation, root causes, explanation
    for fm, ev in evaluations.items():
        explained_by = sorted((u for u in in_neighbors(g, fm, Relation.PROPAGATES_TO)
                               if u in active and active[u] >= active[fm]), key=lambda u: -active[u])
        root_causes = _rank_root_causes(g, fm, states, observations, active, explained_by)
        plausible = _plausible_causes(root_causes)
        actions = [_action(g, a) for a in out_neighbors(g, fm, Relation.MITIGATED_BY)]
        for rc in plausible:
            actions += [a for a in rc["remedies"] if a not in actions]
        actions.sort(key=lambda a: _ACTION_ORDER[a["type"]])
        node = g.nodes[fm]
        result["diagnoses"].append({
            "id": fm, "name": node["name"], "confidence": round(ev.score, 2),
            "evidence_coverage": round(ev.coverage, 2),
            "unconfirmed_key_symptoms": [e["name"] for e in ev.evidence
                                         if e["key_symptom"] and e["outcome"] == "UNKNOWN"],
            "role": "SECONDARY" if explained_by else "PRIMARY", "explained_by": explained_by,
            "severity": node["severity"], "location": _occurs_in_path(g, fm),
            "description": node["description"], "physics": node["physics"],
            "evidence": ev.evidence, "root_causes": root_causes,
            "causal_chains": _causal_chains(g, fm, plausible, ev.evidence),
            "recommended_actions": actions,
        })

    result["diagnoses"].sort(key=lambda d: (d["role"] != "PRIMARY", -d["confidence"],
                                            -_SEVERITY_RANK[d["severity"]]))
    if any(d["role"] == "PRIMARY" for d in result["diagnoses"]):
        result["status"] = "FAULT_DETECTED"
        result["priority"] = max((d["severity"] for d in result["diagnoses"]), key=_SEVERITY_RANK.get)
    elif result["active_alarms"]:
        result["status"] = "ABNORMAL_UNEXPLAINED"
        result["priority"] = Severity.MEDIUM.value
        warnings.append("Alarms are active but no failure-mode signature matched; manual investigation needed")
    else:
        result["status"] = "HEALTHY"
    return result


# --------------------------------------------------------------------------- #
# Human-readable report
# --------------------------------------------------------------------------- #
def format_report(result: dict, max_root_causes: int = 3) -> str:
    eq = result["equipment"]
    lines = [f"Equipment : {eq['id']} - {eq['name']}  ({eq['asset_path']})",
             f"Status    : {result['status']}" + (f"   Priority: {result['priority']}" if result["priority"] else "")]
    if result["active_alarms"]:
        lines.append("Alarms    : " + "; ".join(f"[{a['severity']}] {a['message']}" for a in result["active_alarms"]))
    dq = result["derived_quantities"]
    if dq:
        lines.append("Physics   : " + ", ".join(f"{k}={v}" for k, v in dq.items()))
    for i, d in enumerate(result["diagnoses"], 1):
        role = d["role"] + (f" (consequence of {', '.join(d['explained_by'])})" if d["explained_by"] else "")
        lines += ["", f"{i}. {d['name']}  confidence={d['confidence']:.2f}  severity={d['severity']}  {role}",
                  f"   Location : {d['location']}"]
        if d["unconfirmed_key_symptoms"]:
            lines.append(f"   Note     : key symptom not measured - {', '.join(d['unconfirmed_key_symptoms'])}")
        for e in d["evidence"]:
            if e["outcome"] != "UNKNOWN":
                val = f"{e['value']} {e['unit']}" if e["value"] is not None else ""
                lines.append(f"   - {e['signal']:<26} expected {e['expected']:<9} observed {e['observed']:<9} "
                             f"{val:<16} {e['outcome']}")
        for c in d["causal_chains"]:
            lines.append(f"   Chain [{c['status']}]: {c['text']}")
        lines.append("   Root causes: " + "; ".join(
            f"{r['name']} ({r['status']})" for r in d["root_causes"][:max_root_causes]))
        lines.append("   Actions:")
        lines += [f"     [{a['type']}] {a['description']}" for a in d["recommended_actions"]]
    if result["ruled_out"]:
        lines += ["", "Ruled out : " + "; ".join(r["name"] for r in result["ruled_out"])]
    for w in result["warnings"]:
        lines.append(f"Warning   : {w}")
    return "\n".join(lines)
