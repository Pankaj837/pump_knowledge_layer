"""
Phase 2 - Graph build.

Turns a KnowledgeBase (ontology instances) into a single typed, directed
NetworkX graph with two interleaved layers:

* structural layer - Site -> Area -> System -> Equipment -> Component <- Sensor
* causal layer     - RootCause -> FailureMode -> Symptom -> Sensor
                     FailureMode -> FailureMode (fault propagation)

Every edge is validated against ontology.ONTOLOGY_SCHEMA.  The graph can be
exported to JSON (lossless round trip), RDF/Turtle, and PNG figures.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import networkx as nx

from ontology import (
    ONTOLOGY_SCHEMA,
    KnowledgeBase,
    NodeType,
    Relation,
    Severity,
    State,
    SymptomRule,
    alarm_id,
    symptom_id,
)

# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_graph(kb: KnowledgeBase | None = None) -> nx.DiGraph:
    """Build and validate the knowledge graph. Raises ValueError on any inconsistency."""
    if kb is None:
        from knowledge_base import PUMP_KB
        kb = PUMP_KB

    g = nx.DiGraph(name=f"Knowledge layer - {kb.primary_equipment}", primary_equipment=kb.primary_equipment)
    _add_structure(g, kb)
    _add_sensors_and_alarms(g, kb)
    _add_causal_layer(g, kb)

    problems = validate_graph(g)
    if problems:
        raise ValueError("Knowledge graph failed ontology validation:\n  " + "\n  ".join(problems))
    return g


def _add_node(g: nx.DiGraph, node_id: str, node_type: NodeType, **attrs) -> None:
    if node_id in g:
        raise ValueError(f"Duplicate node id: {node_id}")
    g.add_node(node_id, node_type=node_type.value, **attrs)


def _add_edge(g: nx.DiGraph, src: str, dst: str, relation: Relation, **attrs) -> None:
    for end in (src, dst):
        if end not in g:
            raise ValueError(f"Edge {src} -[{relation.value}]-> {dst}: unknown node '{end}'")
    if g.has_edge(src, dst):
        raise ValueError(f"Duplicate edge {src} -> {dst} ({g.edges[src, dst]['relation']} / {relation.value})")
    g.add_edge(src, dst, relation=relation.value, **attrs)


def _add_structure(g: nx.DiGraph, kb: KnowledgeBase) -> None:
    paths: dict[str, str] = {}
    for loc in kb.locations:  # parents are listed before children
        paths[loc.id] = f"{paths[loc.parent]}/{loc.id}" if loc.parent else loc.id
        _add_node(g, loc.id, NodeType.FUNCTIONAL_LOCATION, name=loc.name, level=loc.level, asset_path=paths[loc.id])
        if loc.parent:
            _add_edge(g, loc.parent, loc.id, Relation.CONTAINS)

    for eq in kb.equipment:
        _add_node(g, eq.id, NodeType.EQUIPMENT, name=eq.name, equipment_class=eq.equipment_class,
                  parameters=dict(eq.parameters), asset_path=f"{paths[eq.location]}/{eq.id}")
        _add_edge(g, eq.location, eq.id, Relation.CONTAINS)
    for eq in kb.equipment:
        for downstream in eq.feeds:
            _add_edge(g, eq.id, downstream, Relation.FEEDS)

    for comp in kb.components:
        local = comp.id.split(".", 1)[-1]
        _add_node(g, comp.id, NodeType.COMPONENT, name=comp.name, component_class=comp.component_class,
                  parameters=dict(comp.parameters), asset_path=f"{g.nodes[comp.equipment]['asset_path']}/{local}")
        _add_edge(g, comp.equipment, comp.id, Relation.HAS_COMPONENT)


_LIMIT_FIELDS = (("lo_lo", State.LOW_LOW), ("lo", State.LOW), ("hi", State.HIGH), ("hi_hi", State.HIGH_HIGH))


def _add_alarms(g: nx.DiGraph, signal) -> None:
    for field, state in _LIMIT_FIELDS:
        limit = getattr(signal, field)
        if limit is None:
            continue
        op = "<" if state in (State.LOW, State.LOW_LOW) else ">"
        severity = Severity.HIGH if state in (State.LOW_LOW, State.HIGH_HIGH) else Severity.MEDIUM
        aid = alarm_id(signal.tag, state)
        unit = "" if signal.unit in ("", "-") else f" {signal.unit}"
        _add_node(g, aid, NodeType.ALARM, name=f"{signal.name} {state.value}", state=state.value,
                  limit=limit, severity=severity.value,
                  message=f"{signal.name} {state.value} ({op} {limit}{unit})")
        _add_edge(g, signal.tag, aid, Relation.TRIGGERS)


def _add_sensors_and_alarms(g: nx.DiGraph, kb: KnowledgeBase) -> None:
    for s in kb.sensors:
        _add_node(g, s.tag, NodeType.SENSOR, key=s.key, name=s.name, measurand=s.measurand, unit=s.unit,
                  lo_lo=s.lo_lo, lo=s.lo, hi=s.hi, hi_hi=s.hi_hi, normal_value=s.normal_value, note=s.note,
                  asset_path=f"{g.nodes[s.monitors]['asset_path']}/{s.tag}")
        _add_edge(g, s.tag, s.monitors, Relation.MONITORS)
        _add_alarms(g, s)

    for v in kb.virtual_sensors:
        _add_node(g, v.tag, NodeType.VIRTUAL_SENSOR, key=v.key, name=v.name, unit=v.unit, formula=v.formula,
                  lo_lo=v.lo_lo, lo=v.lo, hi=v.hi, hi_hi=v.hi_hi, note=v.note,
                  asset_path=f"{g.nodes[kb.primary_equipment]['asset_path']}/{v.tag}")
        for src in v.derived_from:
            _add_edge(g, v.tag, src, Relation.DERIVED_FROM)
        _add_alarms(g, v)


def _symptom_node(g: nx.DiGraph, rule: SymptomRule, signals: dict[str, str]) -> str:
    """Return the shared Symptom node for (signal, state), creating it on first use."""
    if rule.key not in signals:
        raise ValueError(f"Symptom refers to unknown signal key '{rule.key}'")
    sid = symptom_id(rule.key, rule.state)
    if sid not in g:
        signal = signals[rule.key]
        _add_node(g, sid, NodeType.SYMPTOM, key=rule.key, state=rule.state.value,
                  name=f"{g.nodes[signal]['name']} {rule.state.value}")
        _add_edge(g, sid, signal, Relation.OBSERVED_BY)
    return sid


def _add_causal_layer(g: nx.DiGraph, kb: KnowledgeBase) -> None:
    signals = signal_index(g)

    for a in kb.actions:
        _add_node(g, a.id, NodeType.ACTION, name=a.description, action_type=a.action_type.value)

    for fm in kb.failure_modes:
        _add_node(g, fm.id, NodeType.FAILURE_MODE, name=fm.name, severity=fm.severity.value,
                  description=fm.description, physics=fm.physics)
    for fm in kb.failure_modes:
        _add_edge(g, fm.id, fm.occurs_in, Relation.OCCURS_IN)
        for rule in fm.symptoms:
            _add_edge(g, fm.id, _symptom_node(g, rule, signals), Relation.MANIFESTS_AS,
                      weight=rule.weight, key_symptom=rule.key_symptom)
        for target in fm.propagates_to:
            _add_edge(g, fm.id, target, Relation.PROPAGATES_TO)
        for aid in fm.actions:
            _add_edge(g, fm.id, aid, Relation.MITIGATED_BY)

    for rc in kb.root_causes:
        _add_node(g, rc.id, NodeType.ROOT_CAUSE, name=rc.name, description=rc.description, prior=rc.prior)
        for fm_id in rc.causes:
            _add_edge(g, rc.id, fm_id, Relation.CAUSES)
        for rule in rc.evidence:
            _add_edge(g, rc.id, _symptom_node(g, rule, signals), Relation.EVIDENCED_BY,
                      weight=rule.weight, key_symptom=rule.key_symptom)
        if rc.located_in:
            _add_edge(g, rc.id, rc.located_in, Relation.LOCATED_IN)
        for aid in rc.remedies:
            _add_edge(g, rc.id, aid, Relation.REMEDIED_BY)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_graph(g: nx.DiGraph) -> list[str]:
    """Check the graph against the ontology. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    valid_types = {t.value for t in NodeType}
    for n, d in g.nodes(data=True):
        if d.get("node_type") not in valid_types:
            problems.append(f"node {n}: invalid node_type {d.get('node_type')!r}")

    schema = {rel.value: ({t.value for t in dom}, {t.value for t in rng}) for rel, (dom, rng) in ONTOLOGY_SCHEMA.items()}
    for u, v, d in g.edges(data=True):
        rel = d.get("relation")
        if rel not in schema:
            problems.append(f"edge {u}->{v}: unknown relation {rel!r}")
            continue
        dom, rng = schema[rel]
        if g.nodes[u].get("node_type") not in dom:
            problems.append(f"edge {u} -[{rel}]-> {v}: domain {g.nodes[u].get('node_type')} not allowed")
        if g.nodes[v].get("node_type") not in rng:
            problems.append(f"edge {u} -[{rel}]-> {v}: range {g.nodes[v].get('node_type')} not allowed")

    for n in nodes_of_type(g, NodeType.FAILURE_MODE):
        symptoms = out_neighbors(g, n, Relation.MANIFESTS_AS)
        if not symptoms:
            problems.append(f"failure mode {n} has no symptoms")
        elif not any(g.edges[n, s]["key_symptom"] for s in symptoms):
            problems.append(f"failure mode {n} has no key symptom")
        if not in_neighbors(g, n, Relation.CAUSES):
            problems.append(f"failure mode {n} has no root cause")
    for n in nodes_of_type(g, NodeType.SYMPTOM):
        if len(out_neighbors(g, n, Relation.OBSERVED_BY)) != 1:
            problems.append(f"symptom {n} must be observed by exactly one signal")

    for rel in (Relation.CONTAINS, Relation.HAS_COMPONENT, Relation.PROPAGATES_TO, Relation.FEEDS):
        sub = nx.DiGraph([(u, v) for u, v, r in g.edges(data="relation") if r == rel.value])
        if not nx.is_directed_acyclic_graph(sub):
            problems.append(f"relation '{rel.value}' contains a cycle")
    return problems


# --------------------------------------------------------------------------- #
# Query helpers (used by the diagnostic engine and the LLM context builder)
# --------------------------------------------------------------------------- #
def nodes_of_type(g: nx.DiGraph, node_type: NodeType) -> list[str]:
    return [n for n, t in g.nodes(data="node_type") if t == node_type.value]


def out_neighbors(g: nx.DiGraph, node: str, relation: Relation) -> list[str]:
    return [v for _, v, r in g.out_edges(node, data="relation") if r == relation.value]


def in_neighbors(g: nx.DiGraph, node: str, relation: Relation) -> list[str]:
    return [u for u, _, r in g.in_edges(node, data="relation") if r == relation.value]


def signal_index(g: nx.DiGraph) -> dict[str, str]:
    """Map signal key (e.g. 'flow') -> sensor / virtual-sensor node id (e.g. 'FT-101')."""
    return {d["key"]: n for n, d in g.nodes(data=True)
            if d["node_type"] in (NodeType.SENSOR.value, NodeType.VIRTUAL_SENSOR.value)}


# --------------------------------------------------------------------------- #
# Export: JSON (lossless), RDF/Turtle, PNG
# --------------------------------------------------------------------------- #
def to_json_dict(g: nx.DiGraph) -> dict:
    return {
        "graph": dict(g.graph),
        "nodes": [{"id": n, **d} for n, d in g.nodes(data=True)],
        "edges": [{"source": u, "target": v, **d} for u, v, d in g.edges(data=True)],
    }


def export_json(g: nx.DiGraph, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_json_dict(g), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_json(path: str | Path) -> nx.DiGraph:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    g = nx.DiGraph(**data["graph"])
    for node in data["nodes"]:
        attrs = dict(node)
        g.add_node(attrs.pop("id"), **attrs)
    for edge in data["edges"]:
        attrs = dict(edge)
        g.add_edge(attrs.pop("source"), attrs.pop("target"), **attrs)
    return g


_ONTO_NS = "http://example.org/digitwin/ontology#"
_INST_NS = "http://example.org/digitwin/instance/"


def _iri(node_id: str) -> str:
    return f"<{_INST_NS}{quote(node_id, safe='')}>"


def _literal(value) -> str:
    if isinstance(value, bool):
        return f'"{str(value).lower()}"^^xsd:boolean'
    if isinstance(value, int):
        return f'"{value}"^^xsd:integer'
    if isinstance(value, float):
        return f'"{value!r}"^^xsd:double'
    if not isinstance(value, str):
        value = json.dumps(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def export_turtle(g: nx.DiGraph, path: str | Path) -> Path:
    """
    Serialise the ontology (classes + object properties with domain/range) and
    all instances to RDF Turtle.  Weighted signature edges are additionally
    reified as blank nodes carrying weight and key-symptom flag.
    """
    lines = [
        f"@prefix kl: <{_ONTO_NS}> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        "# ---- Ontology (TBox) ----",
    ]
    for t in NodeType:
        lines.append(f"kl:{t.value} a owl:Class .")
    for rel, (dom, rng) in ONTOLOGY_SCHEMA.items():
        decl = f"kl:{rel.value} a owl:ObjectProperty"
        if len(dom) == 1:
            decl += f" ; rdfs:domain kl:{next(iter(dom)).value}"
        if len(rng) == 1:
            decl += f" ; rdfs:range kl:{next(iter(rng)).value}"
        lines.append(decl + " .")

    lines += ["", "# ---- Instances (ABox) ----"]
    for n, d in g.nodes(data=True):
        props = [f"a kl:{d['node_type']}", f"rdfs:label {_literal(d.get('name', n))}",
                 f"kl:id {_literal(n)}"]
        for k, v in d.items():
            if k in ("node_type", "name") or v is None or v == "" or v == {}:
                continue
            props.append(f"kl:{k} {_literal(v)}")
        lines.append(f"{_iri(n)} " + " ;\n    ".join(props) + " .")

    for u, v, d in g.edges(data=True):
        lines.append(f"{_iri(u)} kl:{d['relation']} {_iri(v)} .")
        if "weight" in d:
            lines.append(
                f"{_iri(u)} kl:signatureEntry [ kl:symptom {_iri(v)} ; kl:relation {_literal(d['relation'])} ; "
                f"kl:weight {_literal(float(d['weight']))} ; kl:keySymptom {_literal(bool(d['key_symptom']))} ] ."
            )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_COLORS = {
    NodeType.FUNCTIONAL_LOCATION.value: "#8c8c8c",
    NodeType.EQUIPMENT.value: "#1f77b4",
    NodeType.COMPONENT.value: "#6baed6",
    NodeType.SENSOR.value: "#2ca02c",
    NodeType.VIRTUAL_SENSOR.value: "#98df8a",
    NodeType.SYMPTOM.value: "#ff7f0e",
    NodeType.FAILURE_MODE.value: "#d62728",
    NodeType.ROOT_CAUSE.value: "#9467bd",
}


def _layered_layout(sub: nx.DiGraph, n_layers: int, sweeps: int = 4) -> dict[str, tuple[float, float]]:
    """Column layout with barycenter crossing reduction (Sugiyama-style sweeps)."""
    undirected = sub.to_undirected(as_view=True)
    columns = [[n for n, l in sub.nodes(data="layer") if l == i] for i in range(n_layers)]

    def reorder(col: list[str], ref: list[str]) -> list[str]:
        index = {n: i / max(len(ref) - 1, 1) for i, n in enumerate(ref)}
        def bary(n: str) -> float:
            vals = [index[m] for m in undirected[n] if m in index]
            return sum(vals) / len(vals) if vals else 0.5
        return sorted(col, key=bary)

    for _ in range(sweeps):
        for i in range(1, n_layers):
            columns[i] = reorder(columns[i], columns[i - 1])
        for i in range(n_layers - 2, -1, -1):
            columns[i] = reorder(columns[i], columns[i + 1])

    return {n: (x * 10.0, (len(col) - 1) / 2.0 - y)  # each column vertically centred
            for x, col in enumerate(columns) for y, n in enumerate(col)}


def _draw_layers(g: nx.DiGraph, layers: list[list[NodeType]], relations: set[Relation],
                 title: str, path: Path, figsize: tuple[float, float], font_size: float, dpi: int) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    layer_of = {t.value: i for i, types in enumerate(layers) for t in types}
    nodes = [n for n, t in g.nodes(data="node_type") if t in layer_of]
    rels = {r.value for r in relations}
    sub = nx.DiGraph()
    for n in nodes:
        sub.add_node(n, layer=layer_of[g.nodes[n]["node_type"]])
    sub.add_edges_from((u, v) for u, v, r in g.edges(data="relation") if r in rels and u in sub and v in sub)

    pos = _layered_layout(sub, len(layers))
    fig, ax = plt.subplots(figsize=figsize)
    nx.draw_networkx_edges(sub, pos, ax=ax, alpha=0.35, arrowsize=8, width=0.8, node_size=300,
                           connectionstyle="arc3,rad=0.15")
    nx.draw_networkx_nodes(sub, pos, ax=ax, node_size=300,
                           node_color=[_COLORS[g.nodes[n]["node_type"]] for n in sub])
    labels = {n: (n if g.nodes[n]["node_type"] in (NodeType.SENSOR.value, NodeType.VIRTUAL_SENSOR.value,
                                                     NodeType.EQUIPMENT.value)
                  else g.nodes[n]["name"]) for n in sub}
    for n, (x, y) in pos.items():
        ax.text(x, y + 0.25, labels[n], fontsize=font_size, ha="center", va="bottom")
    present = {g.nodes[n]["node_type"] for n in sub}
    ax.legend(handles=[Patch(color=c, label=t) for t, c in _COLORS.items() if t in present],
              loc="lower center", ncol=len(present), fontsize=font_size + 1, frameon=False,
              bbox_to_anchor=(0.5, -0.04))
    ax.set_title(title, fontsize=font_size + 6)
    ax.axis("off")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


def draw_graph(g: nx.DiGraph, out_dir: str | Path, font_size: float = 10, dpi: int = 150,
               size_scale: float = 0.85) -> list[Path]:
    """Render the structural and causal layers as two PNG figures (requires matplotlib)."""
    out_dir = Path(out_dir)
    t = NodeType
    structural = _draw_layers(
        g, [[t.FUNCTIONAL_LOCATION], [t.EQUIPMENT], [t.COMPONENT], [t.SENSOR, t.VIRTUAL_SENSOR]],
        {Relation.CONTAINS, Relation.HAS_COMPONENT, Relation.MONITORS, Relation.FEEDS, Relation.DERIVED_FROM},
        f"Structural layer - {g.graph['primary_equipment']} (Site > Area > System > Equipment > Component < Sensor)",
        out_dir / "structural_layer.png", (18 * size_scale, 12 * size_scale), font_size, dpi)
    causal = _draw_layers(
        g, [[t.ROOT_CAUSE], [t.FAILURE_MODE], [t.SYMPTOM], [t.SENSOR, t.VIRTUAL_SENSOR]],
        {Relation.CAUSES, Relation.PROPAGATES_TO, Relation.MANIFESTS_AS, Relation.OBSERVED_BY},
        f"Causal layer - {g.graph['primary_equipment']} (RootCause > FailureMode > Symptom > Sensor)",
        out_dir / "causal_layer.png", (20 * size_scale, 18 * size_scale), font_size, dpi)
    return [structural, causal]
