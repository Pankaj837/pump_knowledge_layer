"""
Phase 1 - Ontology design.

Defines the *vocabulary* of the knowledge layer: which classes of things exist
(equipment, components, sensors, alarms, failure modes, ...), which parameters
each class carries, and which relationships are allowed between them.

The ontology is asset-agnostic.  The concrete centrifugal-pump instance data
lives in ``knowledge_base.py``; the graph is built in ``graph_builder.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class NodeType(str, Enum):
    """Ontology classes. Every graph node carries exactly one of these."""

    FUNCTIONAL_LOCATION = "FunctionalLocation"  # Site / Area / System (ISA-95 style)
    EQUIPMENT = "Equipment"                     # a maintainable asset, e.g. pump P-101
    COMPONENT = "Component"                     # a part of an equipment, e.g. DE bearing
    SENSOR = "Sensor"                           # physical instrument, e.g. PT-101
    VIRTUAL_SENSOR = "VirtualSensor"            # physics-derived signal, e.g. NPSH margin
    ALARM = "Alarm"                             # limit violation raised by a sensor
    SYMPTOM = "Symptom"                         # a (signal, state) pair, e.g. "flow LOW"
    FAILURE_MODE = "FailureMode"                # how the asset fails, e.g. cavitation
    ROOT_CAUSE = "RootCause"                    # why it fails, e.g. clogged strainer
    ACTION = "MaintenanceAction"                # what to do about it


class Relation(str, Enum):
    """Typed, directed relationships (edge labels)."""

    CONTAINS = "contains"              # FunctionalLocation -> FunctionalLocation | Equipment
    HAS_COMPONENT = "has_component"    # Equipment -> Component
    FEEDS = "feeds"                    # Equipment -> Equipment (process flow direction)
    MONITORS = "monitors"              # Sensor -> Equipment | Component
    DERIVED_FROM = "derived_from"      # VirtualSensor -> Sensor
    TRIGGERS = "triggers"              # Sensor | VirtualSensor -> Alarm
    OBSERVED_BY = "observed_by"        # Symptom -> Sensor | VirtualSensor
    MANIFESTS_AS = "manifests_as"      # FailureMode -> Symptom   (weighted)
    OCCURS_IN = "occurs_in"            # FailureMode -> Equipment | Component
    PROPAGATES_TO = "propagates_to"    # FailureMode -> FailureMode (fault cascade)
    CAUSES = "causes"                  # RootCause -> FailureMode
    EVIDENCED_BY = "evidenced_by"      # RootCause -> Symptom     (weighted)
    LOCATED_IN = "located_in"          # RootCause -> Equipment | Component
    MITIGATED_BY = "mitigated_by"      # FailureMode -> MaintenanceAction (immediate response)
    REMEDIED_BY = "remedied_by"        # RootCause -> MaintenanceAction  (permanent fix)


class State(str, Enum):
    """Discrete states a signal can be in (ISA-18.2 style alarm bands)."""

    LOW_LOW = "LOW_LOW"
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    HIGH_HIGH = "HIGH_HIGH"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionType(str, Enum):
    IMMEDIATE = "IMMEDIATE"      # protect the machine now (stop, throttle, ...)
    CORRECTIVE = "CORRECTIVE"    # remove the cause (clean, align, replace, ...)
    DIAGNOSTIC = "DIAGNOSTIC"    # confirm the hypothesis (spectrum analysis, test, ...)


# --------------------------------------------------------------------------- #
# Ontology schema: allowed (domain, relation, range) triples.
# graph_builder.validate_graph() checks every edge against this table, so a
# typo in the knowledge base cannot silently create a meaningless link.
# --------------------------------------------------------------------------- #
_N = NodeType
ONTOLOGY_SCHEMA: dict[Relation, tuple[frozenset[NodeType], frozenset[NodeType]]] = {
    Relation.CONTAINS: (frozenset({_N.FUNCTIONAL_LOCATION}),
                        frozenset({_N.FUNCTIONAL_LOCATION, _N.EQUIPMENT})),
    Relation.HAS_COMPONENT: (frozenset({_N.EQUIPMENT}), frozenset({_N.COMPONENT})),
    Relation.FEEDS: (frozenset({_N.EQUIPMENT}), frozenset({_N.EQUIPMENT})),
    Relation.MONITORS: (frozenset({_N.SENSOR}), frozenset({_N.EQUIPMENT, _N.COMPONENT})),
    Relation.DERIVED_FROM: (frozenset({_N.VIRTUAL_SENSOR}), frozenset({_N.SENSOR})),
    Relation.TRIGGERS: (frozenset({_N.SENSOR, _N.VIRTUAL_SENSOR}), frozenset({_N.ALARM})),
    Relation.OBSERVED_BY: (frozenset({_N.SYMPTOM}), frozenset({_N.SENSOR, _N.VIRTUAL_SENSOR})),
    Relation.MANIFESTS_AS: (frozenset({_N.FAILURE_MODE}), frozenset({_N.SYMPTOM})),
    Relation.OCCURS_IN: (frozenset({_N.FAILURE_MODE}), frozenset({_N.EQUIPMENT, _N.COMPONENT})),
    Relation.PROPAGATES_TO: (frozenset({_N.FAILURE_MODE}), frozenset({_N.FAILURE_MODE})),
    Relation.CAUSES: (frozenset({_N.ROOT_CAUSE}), frozenset({_N.FAILURE_MODE})),
    Relation.EVIDENCED_BY: (frozenset({_N.ROOT_CAUSE}), frozenset({_N.SYMPTOM})),
    Relation.LOCATED_IN: (frozenset({_N.ROOT_CAUSE}), frozenset({_N.EQUIPMENT, _N.COMPONENT})),
    Relation.MITIGATED_BY: (frozenset({_N.FAILURE_MODE}), frozenset({_N.ACTION})),
    Relation.REMEDIED_BY: (frozenset({_N.ROOT_CAUSE}), frozenset({_N.ACTION})),
}


# --------------------------------------------------------------------------- #
# Class definitions (structural classes + their parameters)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FunctionalLocation:
    """A node of the plant hierarchy: Site -> Area -> System."""

    id: str
    name: str
    level: str                      # "Site" | "Area" | "System"
    parent: str | None = None


@dataclass(frozen=True)
class Equipment:
    """A maintainable asset with its design (nameplate) parameters."""

    id: str
    name: str
    equipment_class: str            # e.g. "CentrifugalPump", "Tank", "Strainer", "Valve"
    location: str                   # FunctionalLocation id
    parameters: dict = field(default_factory=dict)
    feeds: tuple[str, ...] = ()     # downstream equipment ids (process flow)


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    component_class: str            # e.g. "RollingBearing", "Impeller", "MechanicalSeal"
    equipment: str                  # parent Equipment id
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Sensor:
    """
    A measured signal.  Alarm limits define the discrete state bands:

        value < lo_lo  -> LOW_LOW        lo <= value <= hi -> NORMAL
        value < lo     -> LOW            value > hi        -> HIGH
                                         value > hi_hi     -> HIGH_HIGH
    A limit set to None is not configured for that sensor.
    """

    tag: str                        # instrument tag, e.g. "PT-101"
    key: str                        # signal name used in sensor_states, e.g. "suction_pressure"
    name: str
    measurand: str                  # physical quantity, e.g. "pressure"
    unit: str
    monitors: str                   # Equipment or Component id
    lo_lo: float | None = None
    lo: float | None = None
    hi: float | None = None
    hi_hi: float | None = None
    normal_value: float | None = None   # typical value at the duty point (documentation)
    note: str = ""


@dataclass(frozen=True)
class VirtualSensor:
    """A physics-derived signal computed from physical sensors (see physics.py)."""

    tag: str
    key: str
    name: str
    unit: str
    derived_from: tuple[str, ...]   # sensor tags
    formula: str                    # human-readable formula (documentation / LLM context)
    lo_lo: float | None = None
    lo: float | None = None
    hi: float | None = None
    hi_hi: float | None = None
    note: str = ""


@dataclass(frozen=True)
class SymptomRule:
    """
    An expected observation: signal `key` is in `state`.

    weight  - how strongly the observation supports the hypothesis (1..3).
    key_symptom - if True and the signal is *known* but not in the expected
                  state, the hypothesis is ruled out (necessary condition).
    """

    key: str
    state: State
    weight: float = 1.0
    key_symptom: bool = False


@dataclass(frozen=True)
class FailureMode:
    id: str
    name: str
    occurs_in: str                  # Equipment or Component id
    severity: Severity
    description: str
    physics: str                    # why the symptoms appear (the causal mechanism)
    symptoms: tuple[SymptomRule, ...]
    propagates_to: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()   # MaintenanceAction ids (immediate response)


@dataclass(frozen=True)
class RootCause:
    id: str
    name: str
    description: str
    causes: tuple[str, ...]         # FailureMode ids
    evidence: tuple[SymptomRule, ...] = ()   # observations that confirm this cause
    located_in: str | None = None   # Equipment or Component id
    prior: float = 0.5              # relative field likelihood 0..1 (used for ranking ties)
    remedies: tuple[str, ...] = ()  # MaintenanceAction ids (permanent fix)


@dataclass(frozen=True)
class MaintenanceAction:
    id: str
    description: str
    action_type: ActionType


@dataclass(frozen=True)
class KnowledgeBase:
    """Container for one asset's instance data."""

    locations: tuple[FunctionalLocation, ...]
    equipment: tuple[Equipment, ...]
    components: tuple[Component, ...]
    sensors: tuple[Sensor, ...]
    virtual_sensors: tuple[VirtualSensor, ...]
    failure_modes: tuple[FailureMode, ...]
    root_causes: tuple[RootCause, ...]
    actions: tuple[MaintenanceAction, ...]
    primary_equipment: str          # the asset being diagnosed


# --------------------------------------------------------------------------- #
# Node-id helpers (single place that defines id conventions)
# --------------------------------------------------------------------------- #
def symptom_id(key: str, state: State) -> str:
    return f"SYM:{key}:{state.value}"


def alarm_id(sensor_tag: str, state: State) -> str:
    suffix = {State.LOW_LOW: "LL", State.LOW: "L", State.HIGH: "H", State.HIGH_HIGH: "HH"}[state]
    return f"ALM:{sensor_tag}-{suffix}"
