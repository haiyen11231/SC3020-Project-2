"""Tests for comparison.py. No database or API key needed.

Run from the project folder:  python -m unittest tests/test_comparison.py
"""
import copy
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from preprocessing import PlanError, PlanNode, parse_ai_plan  # noqa: E402
from comparison import (AI, CHANGED, ONLY_AI, ONLY_PG, PG, SAME, compare_plans,  # noqa: E402
                        demo_plans, iter_nodes)


def P(node_type, relation=None, children=None, **kw):
    return PlanNode(node_type, node_type, relation=relation, children=children or [], **kw)


def statuses(result, side, plan):
    return {node.node_type + (f"[{node.relation}]" if node.relation else ""): result.status(side, path)
            for path, node in iter_nodes(plan)}


class FigureExample(unittest.TestCase):
    """Figure 1 (PostgreSQL) against Figure 2 (Gemini) of the project description."""

    def setUp(self):
        self.pg, self.ai = demo_plans()
        self.r = compare_plans(self.pg, self.ai)

    def test_scan_difference_found(self):
        s = statuses(self.r, PG, self.pg)
        self.assertEqual(s["Seq Scan[lineitem]"], CHANGED)
        self.assertEqual(s["Seq Scan[part]"], SAME)
        self.assertEqual(s["Gather"], ONLY_PG)
        cats = [d.category for d in self.r.differences]
        self.assertIn("Access method", cats)
        self.assertIn("Parallelism", cats)

    def test_rate(self):
        # 2 unmatched PG nodes + 1 changed pair (counts 2) = 4 over 7 + 5 nodes
        self.assertAlmostEqual(self.r.difference_rate, 4 / 12, places=4)

    def test_index_cond_vs_filter_not_a_difference(self):
        self.assertFalse(any(d.category == "Conditions" for d in self.r.differences))

    def test_dict_input(self):
        r2 = compare_plans(self.pg.to_dict(), self.ai.to_dict())
        self.assertEqual(r2.difference_rate, self.r.difference_rate)
        json.dumps(r2.to_dict())


class Cases(unittest.TestCase):
    def test_identical(self):
        pg, _ = demo_plans()
        r = compare_plans(pg, copy.deepcopy(pg))
        self.assertEqual(r.difference_rate, 0)
        self.assertEqual(r.metrics["ted_rate"], 0)
        self.assertTrue(all(m.status == SAME for m in r.matches))
        self.assertEqual(r.differences, [])

    def test_bad_input(self):
        pg, _ = demo_plans()
        for bad in (None, {"x": 1}, 3):
            with self.assertRaises(PlanError):
                compare_plans(pg, bad)

    def test_swapped_hash_build_side_and_mirrored_join_type(self):
        pg = P("Hash Join", join_type="Right", conditions={"Hash Cond": "(o.k = c.k)"}, children=[
            P("Seq Scan", "orders", conditions={"Filter": "((d >= '1994-01-01'::date) AND (d <= '1994-12-31'::date))"}),
            P("Hash", children=[P("Seq Scan", "customer")])])
        ai = P("Hash Join", join_type="Left", conditions={"Hash Cond": "c.k = o.k"}, children=[
            P("Seq Scan", "customer"),
            P("Hash", children=[P("Seq Scan", "orders", conditions={"Filter": "d BETWEEN '1994-01-01' AND '1994-12-31'"})])])
        r = compare_plans(pg, ai)
        cats = [d.category for d in r.differences]
        self.assertIn("Join input order", cats)
        self.assertNotIn("Join type", cats)       # Right with swapped inputs == Left
        self.assertNotIn("Conditions", cats)      # BETWEEN == >= AND <=
        self.assertEqual(statuses(r, PG, pg)["Seq Scan[orders]"], SAME)

    def test_join_order(self):
        pg = P("Hash Join", children=[P("Hash Join", children=[P("Seq Scan", "a"), P("Hash", children=[P("Seq Scan", "b")])]),
                                      P("Hash", children=[P("Seq Scan", "c")])])
        ai = P("Hash Join", children=[P("Hash Join", children=[P("Seq Scan", "a"), P("Hash", children=[P("Seq Scan", "c")])]),
                                      P("Hash", children=[P("Seq Scan", "b")])])
        r = compare_plans(pg, ai)
        self.assertIn("Join order", [d.category for d in r.differences])
        self.assertEqual(r.status(PG, "0"), SAME)       # top join produces the same result
        # join results {a,b},{a,b,c} against {a,c},{a,b,c}: Jaccard 1/3
        self.assertAlmostEqual(r.metrics["join_order_agreement"], 1 / 3, places=3)

    def test_self_join_matched_by_literal(self):
        def plan(first, second):
            return P("Hash Join", children=[
                P("Hash Join", children=[P("Seq Scan", "customer"), P("Hash", children=[
                    P("Seq Scan", "nation", conditions={"Filter": f"(n_name = '{first}'::bpchar)"})])]),
                P("Hash", children=[P("Hash Join", children=[P("Seq Scan", "supplier"), P("Hash", children=[
                    P("Seq Scan", "nation", conditions={"Filter": f"(n_name = '{second}'::bpchar)"})])])])])
        r = compare_plans(plan("GERMANY", "FRANCE"), plan("GERMANY", "FRANCE"))
        self.assertEqual(r.difference_rate, 0)

    def test_not_in_subplan_vs_anti_join(self):
        # PostgreSQL 18 runs NOT IN as a hashed SubPlan; Gemini wrote an anti join
        pg = P("Aggregate", extra={"Strategy": "Sorted"}, keys=["p_brand"], children=[
            P("Hash Join", join_type="Inner", conditions={"Hash Cond": "(partsupp.ps_partkey = part.p_partkey)"}, children=[
                P("Index Only Scan", "partsupp", index_name="partsupp_pkey",
                  conditions={"Filter": "(NOT (ANY (ps_suppkey = (hashed SubPlan 1).col1)))"}, children=[
                      P("Seq Scan", "supplier", extra={"Parent Relationship": "SubPlan"})]),
                P("Hash", children=[P("Seq Scan", "part")])])])
        ai = P("Aggregate", extra={"Strategy": "Hashed"}, keys=["p_brand"], children=[
            P("Hash Join", join_type="Anti", conditions={"Hash Cond": "ps_suppkey = s_suppkey"}, children=[
                P("Hash Join", join_type="Inner", conditions={"Hash Cond": "ps_partkey = p_partkey"}, children=[
                    P("Seq Scan", "partsupp"), P("Hash", children=[P("Seq Scan", "part")])]),
                P("Hash", children=[P("Seq Scan", "supplier")])])])
        r = compare_plans(pg, ai)
        cats = [d.category for d in r.differences]
        self.assertIn("Subquery", cats)
        self.assertNotIn("Join type", cats)        # Inner join pairs with the inner join
        self.assertNotIn("Conditions", cats)
        self.assertNotIn("Join order", cats)
        self.assertEqual(r.partner(PG, "0.0"), "0.0.0")   # partsupp-part joins match
        self.assertEqual(r.partner(PG, "0"), "0")         # top aggregates match
        self.assertEqual(r.status(AI, "0.0"), ONLY_AI)    # anti join is AI-only

    def test_ai_warnings_and_unknown_table(self):
        pg, _ = demo_plans()
        ai = parse_ai_plan(json.dumps({"node_type": "Magic Join", "children": [
            {"node_type": "Seq Scan", "relation": "lineitem"},
            {"node_type": "Seq Scan", "relation": "nosuch"}]}), {"lineitem", "part"}, set())
        r = compare_plans(pg, ai)
        cats = [d.category for d in r.differences]
        self.assertIn("Invalid AI node", cats)
        self.assertIn("Tables", cats)
        self.assertEqual(r.status(AI, "0.1"), ONLY_AI)


class RandomPlans(unittest.TestCase):
    """Invariants on many random plan pairs."""

    def tree(self, rng, tables):
        if len(tables) == 1:
            n = P(rng.choice(["Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"]), tables[0])
            if n.node_type == "Bitmap Heap Scan":
                n.children = [P("Bitmap Index Scan", index_name=tables[0] + "_idx")]
        else:
            k = rng.randint(1, len(tables) - 1)
            n = P(rng.choice(["Hash Join", "Nested Loop", "Merge Join"]),
                  children=[self.tree(rng, tables[:k]), self.tree(rng, tables[k:])])
        while rng.random() < 0.3:
            n = P(rng.choice(["Sort", "Aggregate", "Hash", "Gather", "Limit", "Materialize"]), children=[n])
        return n

    def test_invariants(self):
        rng = random.Random(7)
        for _ in range(300):
            tabs = rng.sample("abcdefghij", rng.randint(1, 8))
            t1 = self.tree(rng, list(tabs))
            tabs2 = list(tabs)
            rng.shuffle(tabs2)
            t2 = self.tree(rng, tabs2)
            r, rev = compare_plans(t1, t2), compare_plans(t2, t1)
            self.assertTrue(0 <= r.difference_rate <= 1)
            self.assertTrue(0 <= r.metrics["ted_rate"] <= 1)
            self.assertAlmostEqual(r.difference_rate, rev.difference_rate)
            for side, plan in ((PG, t1), (AI, t2)):
                for path, _ in iter_nodes(plan):
                    self.assertIn(r.status(side, path), (SAME, CHANGED, ONLY_PG, ONLY_AI))
            self.assertEqual(compare_plans(t1, copy.deepcopy(t1)).difference_rate, 0)


if __name__ == "__main__":
    unittest.main()
