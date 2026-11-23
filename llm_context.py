"""
Phase 4 (code companion to docs/LLM_Integration_Essay.md) - GraphRAG context builder.

Retrieval step of Retrieval-Augmented Generation: instead of letting an LLM
answer from its general training data, we retrieve the *diagnosis-specific
subgraph* of the knowledge layer and serialise it into a grounded prompt.
Every fact in the prompt carries a node id so the generated report can cite
it, and a reviewer can trace each sentence back to the graph.

No LLM call is made here: the output is a provider-independent prompt string.
"""

from __future__ import annotations

import networkx as nx

from diagnostic_engine import default_graph
from graph_builder import in_neighbors, out_neighbors
from ontology import Relation

SYSTEM_PROMPT = """\
You are a reliability engineer writing an incident report for rotating equipment.
Rules:
1. Use ONLY the facts in the CONTEXT section. Do not add causes, values or standards that are not there.
2. Cite the node id in square brackets for every factual claim, e.g. [FM-CAVITATION], [PT-101].
3. Keep the engine's confidence values and root-cause statuses; do not upgrade an UNVERIFIED cause to confirmed.
4. If the context is insufficient to conclude something, say so explicitly and name the missing measurement.
"""

REPORT_TASK = """\
Write the report with these sections:
1. Summary (equipment, status, priority, most likely failure mode and root cause)
2. Evidence (abnormal measurements and physics-derived quantities that support the diagnosis)
3. Causal analysis (explain each causal chain in plain engineering language, using the mechanism text)
4. Alternatives considered (ruled-out or lower-confidence hypotheses and why)
5. Recommended actions (in the given order: IMMEDIATE, DIAGNOSTIC, CORRECTIVE)
6. Confidence and limitations (unverified causes, unmeasured key symptoms)
"""


def retrieve_subgraph(result: dict, graph: nx.DiGraph | None = None) -> nx.DiGraph:
    """
    Retrieval: the minimal subgraph that explains the diagnosis -
    diagnosed failure modes + their symptoms/sensors + root causes + locations + actions.
    """
    g = graph if graph is not None else default_graph()
    nodes: set[str] = {result["equipment"]["id"]}
    for d in result["diagnoses"]:
        fm = d["id"]
        nodes.add(fm)
        nodes.update(out_neighbors(g, fm, Relation.OCCURS_IN))
        nodes.update(out_neighbors(g, fm, Relation.MITIGATED_BY))
        for sym in out_neighbors(g, fm, Relation.MANIFESTS_AS):
            nodes.add(sym)
            nodes.update(out_neighbors(g, sym, Relation.OBSERVED_BY))
        for rc in in_neighbors(g, fm, Relation.CAUSES):
            nodes.add(rc)
            nodes.update(out_neighbors(g, rc, Relation.LOCATED_IN))
            nodes.update(out_neighbors(g, rc, Relation.REMEDIED_BY))
            nodes.update(out_neighbors(g, rc, Relation.EVIDENCED_BY))
        nodes.update(d["explained_by"])
    return g.subgraph(nodes).copy()


def _fmt(value) -> str:
    return "n/a" if value is None else str(value)


def build_llm_context(result: dict, graph: nx.DiGraph | None = None) -> str:
    """Serialise a diagnose_fault() result + its retrieved subgraph into a grounded LLM prompt."""
    g = graph if graph is not None else default_graph()
    eq = result["equipment"]
    eq_node = g.nodes[eq["id"]]
    out = ["# SYSTEM", SYSTEM_PROMPT, "# CONTEXT", "",
           "## Asset",
           f"- [{eq['id']}] {eq['name']} ({eq_node['equipment_class']}), path {eq['asset_path']}",
           "- Nameplate: " + ", ".join(f"{k}={v}" for k, v in eq_node["parameters"].items()),
           "", f"## Diagnostic status: {result['status']} (priority {_fmt(result['priority'])})", "",
           "## Sensor snapshot",
           "| Tag | Signal | Value | Unit | State | Source |", "|---|---|---|---|---|---|"]
    for o in result["observations"].values():
        out.append(f"| [{o['tag']}] | {o['name']} | {_fmt(o['value'])} | {o['unit']} | {o['state']} | {o['source']} |")
    if result["derived_quantities"]:
        out += ["", "## Physics-derived quantities"]
        out += [f"- {k} = {v}" for k, v in result["derived_quantities"].items()]
        for tag in ("NPSH-101", "HEAD-101"):
            if tag in g:
                out.append(f"- [{tag}] formula: {g.nodes[tag]['formula']}")
    out += ["", "## Active alarms"]
    out += [f"- [{a['id']}] {a['severity']}: {a['message']}" for a in result["active_alarms"]] or ["- none"]

    out += ["", "## Diagnoses (ranked)"]
    for d in result["diagnoses"]:
        out += ["", f"### [{d['id']}] {d['name']}",
                f"- confidence {d['confidence']}, evidence coverage {d['evidence_coverage']}, "
                f"severity {d['severity']}, role {d['role']}"
                + (f" (consequence of {', '.join(f'[{x}]' for x in d['explained_by'])})" if d["explained_by"] else ""),
                f"- location: {d['location']}",
                f"- mechanism: {d['physics']}"]
        if d["unconfirmed_key_symptoms"]:
            out.append(f"- key symptoms NOT measured: {', '.join(d['unconfirmed_key_symptoms'])}")
        out.append("- evidence:")
        out += [f"  - [{e['sensor']}] {e['signal']}: expected {e['expected']}, observed {_fmt(e['observed'])} "
                f"({_fmt(e['value'])} {e['unit'] or ''}) -> {e['outcome']}" for e in d["evidence"]]
        out.append("- causal chains:")
        out += [f"  - ({c['status']}) {c['text']}  nodes: {c['nodes'][0]} -> {c['nodes'][1]} -> "
                f"{{{', '.join(c['nodes'][2:])}}}" for c in d["causal_chains"]]
        out.append("- root causes:")
        out += [f"  - [{r['id']}] {r['name']}: {r['status']}"
                + (f" (score {r['score']})" if r["score"] is not None else "") for r in d["root_causes"]]
        out.append("- recommended actions:")
        out += [f"  - [{a['id']}] {a['type']}: {a['description']}" for a in d["recommended_actions"]]

    if result["ruled_out"]:
        out += ["", "## Hypotheses ruled out"]
        out += [f"- [{r['id']}] {r['name']}: {r['reason']}" for r in result["ruled_out"]]
    if result["warnings"]:
        out += ["", "## Data-quality warnings"] + [f"- {w}" for w in result["warnings"]]

    sub = retrieve_subgraph(result, g)
    out += ["", f"## Knowledge-graph facts ({sub.number_of_edges()} triples retrieved)"]
    out += sorted(f"- [{u}] {r} [{v}]" for u, v, r in sub.edges(data="relation"))
    out += ["", "# TASK", REPORT_TASK]
    return "\n".join(out)
