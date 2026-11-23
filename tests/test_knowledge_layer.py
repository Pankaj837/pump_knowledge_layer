"""
Regression tests for the P-101 knowledge layer.

Run from the project folder:   python -m pytest -q     (or)   python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import networkx as nx  # noqa: E402

from diagnostic_engine import classify, compare_state, default_graph, diagnose_fault  # noqa: E402
from graph_builder import (build_graph, export_json, export_turtle, load_json,  # noqa: E402
                           nodes_of_type, validate_graph)
from llm_context import build_llm_context, retrieve_subgraph  # noqa: E402
from ontology import NodeType, Relation, State  # noqa: E402
from physics import (compute_virtual_signals, pump_curve_head_m, water_density,  # noqa: E402
                     water_vapor_pressure_bar)
from scenarios import BASELINE, SCENARIOS  # noqa: E402


class TestPhysics(unittest.TestCase):
    def test_vapor_pressure_matches_steam_tables(self):
        # IAPWS steam-table values (bar): 20 C 0.02339, 60 C 0.1995, 100 C 1.01325
        self.assertAlmostEqual(water_vapor_pressure_bar(20), 0.02339, delta=0.0005)
        self.assertAlmostEqual(water_vapor_pressure_bar(60), 0.1995, delta=0.002)
        self.assertAlmostEqual(water_vapor_pressure_bar(100), 1.01325, delta=0.005)

    def test_water_density(self):
        self.assertAlmostEqual(water_density(4), 1000.0, delta=0.1)
        self.assertAlmostEqual(water_density(30), 995.65, delta=0.2)

    def test_pump_curve_passes_through_design_points(self):
        self.assertEqual(pump_curve_head_m(0, 60, 100, 50), 60)
        self.assertAlmostEqual(pump_curve_head_m(100, 60, 100, 50), 50)

    def test_baseline_is_at_duty_point(self):
        params = default_graph().nodes["P-101"]["parameters"]
        v = compute_virtual_signals(BASELINE, params)
        self.assertAlmostEqual(v["head_ratio"], 1.0, delta=0.01)
        self.assertGreater(v["npsh_margin"], 3.0)

    def test_hotter_fluid_reduces_npsh_margin(self):
        params = default_graph().nodes["P-101"]["parameters"]
        cold = compute_virtual_signals({**BASELINE, "fluid_temperature": 30}, params)["npsh_margin"]
        hot = compute_virtual_signals({**BASELINE, "fluid_temperature": 85}, params)["npsh_margin"]
        self.assertLess(hot, cold)


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.g = build_graph()

    def test_graph_is_valid_against_ontology(self):
        self.assertEqual(validate_graph(self.g), [])

    def test_every_failure_mode_has_cause_symptom_location_action(self):
        for fm in nodes_of_type(self.g, NodeType.FAILURE_MODE):
            rels = {r for _, _, r in self.g.out_edges(fm, data="relation")}
            self.assertTrue({Relation.MANIFESTS_AS.value, Relation.OCCURS_IN.value,
                             Relation.MITIGATED_BY.value} <= rels, fm)
            self.assertTrue(any(r == Relation.CAUSES.value for _, _, r in self.g.in_edges(fm, data="relation")), fm)

    def test_asset_paths_are_hierarchical(self):
        self.assertEqual(self.g.nodes["P-101.BRG-DE"]["asset_path"], "Plant1/Utilities/CoolingWater/P-101/BRG-DE")
        self.assertEqual(self.g.nodes["TT-101"]["asset_path"], "Plant1/Utilities/CoolingWater/P-101/BRG-DE/TT-101")

    def test_validation_catches_schema_violation(self):
        bad = self.g.copy()
        bad.add_edge("FT-101", "FM-CAVITATION", relation=Relation.CAUSES.value)  # a sensor cannot cause a failure
        self.assertTrue(any("domain" in p for p in validate_graph(bad)))

    def test_validation_catches_propagation_cycle(self):
        bad = self.g.copy()
        bad.add_edge("FM-SEAL-LEAK", "FM-DRY-RUN", relation=Relation.PROPAGATES_TO.value)
        self.assertTrue(any("cycle" in p for p in validate_graph(bad)))

    def test_json_round_trip_is_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            g2 = load_json(export_json(self.g, Path(tmp) / "g.json"))
        self.assertTrue(nx.utils.graphs_equal(self.g, g2))

    def test_turtle_export_parses(self):
        try:
            import rdflib
        except ImportError:
            self.skipTest("rdflib not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = export_turtle(self.g, Path(tmp) / "g.ttl")
            rdf = rdflib.Graph().parse(path, format="turtle")
        self.assertGreater(len(rdf), self.g.number_of_edges())


class TestStateLogic(unittest.TestCase):
    def test_classify_bands(self):
        limits = {"lo_lo": 10, "lo": 70, "hi": 120, "hi_hi": 135}
        self.assertEqual(classify(5, limits), State.LOW_LOW)
        self.assertEqual(classify(50, limits), State.LOW)
        self.assertEqual(classify(70, limits), State.NORMAL)   # limit itself is still normal
        self.assertEqual(classify(120, limits), State.NORMAL)
        self.assertEqual(classify(130, limits), State.HIGH)
        self.assertEqual(classify(140, limits), State.HIGH_HIGH)

    def test_compare_state(self):
        self.assertEqual(compare_state(State.LOW, State.LOW_LOW), "MATCH")
        self.assertEqual(compare_state(State.LOW_LOW, State.LOW), "ABSENT")
        self.assertEqual(compare_state(State.LOW, State.NORMAL), "ABSENT")
        self.assertEqual(compare_state(State.LOW, State.HIGH), "CONTRADICTS")
        self.assertEqual(compare_state(State.NORMAL, State.HIGH), "CONTRADICTS")
        self.assertEqual(compare_state(State.HIGH, None), "UNKNOWN")


class TestDiagnosis(unittest.TestCase):
    def test_all_scenarios(self):
        for s in SCENARIOS:
            with self.subTest(s.name):
                r = diagnose_fault(s.sensor_states)
                self.assertEqual(r["status"], s.expected_status)
                primary = next((d for d in r["diagnoses"] if d["role"] == "PRIMARY"), None)
                self.assertEqual(primary["id"] if primary else None, s.expected_primary)
                if s.expected_root_cause:
                    self.assertEqual(primary["root_causes"][0]["id"], s.expected_root_cause)
                secondary = {d["id"] for d in r["diagnoses"] if d["role"] == "SECONDARY"}
                self.assertEqual(secondary, set(s.expected_secondary))
                json.dumps(r)  # result must be JSON-serialisable

    def test_deadhead_physics_current_drops(self):
        """Closed discharge must NOT be diagnosed as overload: centrifugal pump power is minimum at shut-off."""
        s = next(s for s in SCENARIOS if s.name == "deadhead_closed_discharge")
        r = diagnose_fault(s.sensor_states)
        self.assertEqual(r["observations"]["motor_current"]["state"], "LOW")
        self.assertIn("FM-OVERLOAD", {x["id"] for x in r["ruled_out"]})

    def test_hot_fluid_cavitation_found_only_through_physics(self):
        s = next(s for s in SCENARIOS if s.name == "cavitation_hot_fluid")
        r = diagnose_fault(s.sensor_states)
        self.assertEqual(r["observations"]["suction_pressure"]["state"], "NORMAL")
        self.assertEqual(r["observations"]["npsh_margin"]["state"], "LOW")
        self.assertEqual(r["observations"]["npsh_margin"]["source"], "derived")

    def test_secondary_failure_links_to_upstream(self):
        s = next(s for s in SCENARIOS if s.name == "misalignment_with_bearing_distress")
        bearing = next(d for d in diagnose_fault(s.sensor_states)["diagnoses"] if d["id"] == "FM-BEARING")
        self.assertEqual(bearing["explained_by"], ["FM-MISALIGNMENT"])
        self.assertEqual(bearing["root_causes"][0]["status"], "ACTIVE_FAILURE_MODE")

    def test_inputs_by_tag_and_state_are_equivalent(self):
        by_key = diagnose_fault({"seal_leak": 1, "seal_chamber_temperature": 76})
        by_tag = diagnose_fault({"LS-101": "ACTIVE", "TT-104": "HIGH"})
        self.assertEqual([d["id"] for d in by_key["diagnoses"]], [d["id"] for d in by_tag["diagnoses"]])

    def test_bad_inputs_are_reported_not_fatal(self):
        r = diagnose_fault({**BASELINE, "unknown_sensor": 5, "flow": "garbage", "radial_vibration": float("nan")})
        self.assertEqual(r["unknown_inputs"], ["unknown_sensor"])
        self.assertTrue(any("flow" in w for w in r["warnings"]))
        self.assertNotIn("flow", r["observations"])

    def test_empty_input(self):
        self.assertEqual(diagnose_fault({})["status"], "HEALTHY")

    def test_abnormal_but_unexplained(self):
        r = diagnose_fault({**BASELINE, "motor_winding_temperature": 150})  # hot winding, current normal
        self.assertEqual(r["status"], "ABNORMAL_UNEXPLAINED")

    def test_missing_key_symptom_is_flagged(self):
        r = diagnose_fault({"radial_vibration": 6.0})
        bearing = next(d for d in r["diagnoses"] if d["id"] == "FM-BEARING")
        self.assertIn("DE bearing temperature HIGH", bearing["unconfirmed_key_symptoms"])


class TestLLMContext(unittest.TestCase):
    def test_context_is_grounded_and_cites_nodes(self):
        s = next(s for s in SCENARIOS if s.name == "cavitation_clogged_strainer")
        r = diagnose_fault(s.sensor_states)
        ctx = build_llm_context(r)
        for must in ("[FM-CAVITATION]", "[RC-STRAINER-CLOGGED]", "[PDT-100]", "Use ONLY the facts", "# TASK"):
            self.assertIn(must, ctx)
        sub = retrieve_subgraph(r)
        self.assertIn("STR-100", sub)
        self.assertNotIn("FM-OVERLOAD", sub)  # retrieval is focused on the diagnosis


if __name__ == "__main__":
    unittest.main()
