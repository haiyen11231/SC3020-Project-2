"""Integration tests for project.py (Part 5).

Run from the project folder:
    python -m unittest discover -s tests -v

Most tests are offline: they use real PostgreSQL plans saved in tests/fixtures and
hand-written AI answers, so they need neither a database nor an API key.
The DatabaseTests class runs only when SC3020_TEST_DB=1 is set; it connects with
the settings in .env and checks that every workload query can be planned.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import comparison as cmp                      # noqa: E402
import preprocessing as pp                    # noqa: E402
import project                                # noqa: E402
from preprocessing import PlanError           # noqa: E402

with open(os.path.join(HERE, "fixtures", "tpch_plans_pg16.json"), encoding="utf-8") as _f:
    FIXTURES = json.load(_f)


def pg_plan(qid):
    return pp.parse_postgres_plan(FIXTURES[qid]["explain"])


def as_ai_answer(plan, subplans_last=True, mark_subplans=True):
    """Write a PostgreSQL plan the way the prompt asks the AI to answer."""
    def node(n):
        kids = list(n.children)
        if subplans_last:
            kids.sort(key=lambda c: c.extra.get("Parent Relationship") in ("InitPlan", "SubPlan"))
        rel = n.extra.get("Parent Relationship")
        return {"node_type": n.node_type, "relation": n.relation, "index_name": n.index_name,
                "join_type": n.join_type, "strategy": n.extra.get("Strategy"),
                "conditions": dict(n.conditions), "keys": list(n.keys),
                "estimated_rows": n.rows, "reason": "test",
                "parent_relationship": rel if mark_subplans and rel in ("InitPlan", "SubPlan") else None,
                "children": [node(c) for c in kids]}
    return json.dumps(node(plan))


class SingleStatement(unittest.TestCase):
    def test_trailing_semicolon_and_comments(self):
        self.assertEqual(project.single_statement("  SELECT 1;  -- done\n"), "SELECT 1")

    def test_semicolons_inside_strings_names_and_comments(self):
        sql = "SELECT 'a;b', \"we;ird\", $$x;y$$ /* ; */ FROM t -- ;\n"
        self.assertEqual(project.single_statement(sql), sql.strip())

    def test_two_statements_rejected(self):
        with self.assertRaises(PlanError):
            project.single_statement("SELECT 1; DROP TABLE nation")

    def test_empty_rejected(self):
        for text in ("", "   ", "-- only a comment", ";"):
            with self.assertRaises(PlanError):
                project.single_statement(text)


class Workload(unittest.TestCase):
    def setUp(self):
        self.queries = project.read_workload(project.DEFAULT_WORKLOAD)

    def test_workload_is_diverse(self):
        self.assertGreaterEqual(len(self.queries), 40)
        categories = {q.category for q in self.queries}
        self.assertGreaterEqual(len(categories), 10)
        for q in self.queries:
            self.assertTrue(q.purpose, q.qid)
            self.assertNotIn(";", q.sql.rstrip()[-1:], q.qid)

    def test_ids_unique(self):
        ids = [q.qid for q in self.queries]
        self.assertEqual(len(ids), len(set(ids)))

    def test_plain_file_without_tags(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
            f.write("-- comment\nSELECT 1;\n\nSELECT ';'\nFROM t;\n")
        try:
            queries = project.read_workload(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual([q.qid for q in queries], ["Q1", "Q2"])
        self.assertEqual(queries[1].sql, "SELECT ';'\nFROM t")

    def test_select_by_id_and_letter(self):
        chosen = project._select(self.queries, "A1,F")
        self.assertIn("A1", [q.qid for q in chosen])
        self.assertTrue(all(q.qid == "A1" or q.qid.startswith("F") for q in chosen))


class AIAnswerParsing(unittest.TestCase):
    def test_correct_answer_gives_zero_difference(self):
        """A correct AI answer must not produce false differences (all fixtures,
        subqueries listed last as the prompt asks, with and without the marker)."""
        for qid in FIXTURES:
            pg = pg_plan(qid)
            for marked in (True, False):
                ai = pp.parse_ai_plan(as_ai_answer(pg, mark_subplans=marked))
                result = cmp.compare_plans(pg, ai)
                self.assertEqual(result.difference_rate, 0, (qid, marked, result.explanation_text()))
                self.assertFalse(result.differences, (qid, marked))

    def test_wrappers_and_markdown(self):
        body = '{"node_type": "Seq Scan", "relation": "nation", "children": []}'
        for text in (body, f"```json\n{body}\n```", '{"plan": %s}' % body,
                     'Here is the plan: [{"Plan": {"Node Type": "Seq Scan", '
                     '"Relation Name": "nation"}}]'):
            plan = pp.parse_ai_plan(text)
            self.assertEqual((plan.node_type, plan.relation), ("Seq Scan", "nation"), text)

    def test_exact_postgres_names_are_kept(self):
        for name in ("WorkTable Scan", "Recursive Union", "BitmapOr", "ProjectSet", "Group"):
            self.assertEqual(pp.canonical_node_type(name), name)

    def test_projectset_not_folded_but_projection_is(self):
        scan = {"node_type": "Seq Scan", "relation": "t"}
        self.assertEqual(pp.parse_ai_plan(json.dumps(
            {"node_type": "ProjectSet", "children": [scan]})).node_type, "ProjectSet")
        self.assertEqual(pp.parse_ai_plan(json.dumps(
            {"node_type": "Projection", "children": [scan]})).node_type, "Seq Scan")

    def test_extra_child_of_one_input_operator_is_a_subplan(self):
        plan = pp.parse_ai_plan(json.dumps({"node_type": "Hash Join", "children": [
            {"node_type": "Seq Scan", "relation": "a"},
            {"node_type": "Hash", "children": [{"node_type": "Seq Scan", "relation": "b"}]},
            {"node_type": "Aggregate", "children": [{"node_type": "Seq Scan", "relation": "c"}]}]}))
        self.assertNotIn("Warning", plan.extra)
        self.assertEqual(plan.children[2].extra.get("Parent Relationship"), "SubPlan")

    def test_partial_and_finalize_aggregates(self):
        plan = pp.parse_ai_plan(json.dumps({"node_type": "Finalize Aggregate", "children": [
            {"node_type": "Gather", "children": [{"node_type": "Partial HashAggregate",
             "children": [{"node_type": "Parallel Seq Scan", "relation": "t"}]}]}]}))
        self.assertEqual(plan.extra.get("Partial Mode"), "Finalize")
        self.assertEqual(plan.children[0].children[0].extra.get("Partial Mode"), "Partial")

    def test_cte_and_hash_names_are_labels_not_tables(self):
        """An AI naming the WITH query of a CTE Scan (or the table under a Hash) must
        not be flagged as inventing a table, nor differ from PostgreSQL."""
        pg = pg_plan("G1")
        answer = json.loads(as_ai_answer(pg))

        def name(n):
            if n["node_type"] == "CTE Scan":
                n["relation"] = "nation_revenue"
            if n["node_type"] == "Hash" and n["children"][0].get("relation"):
                n["relation"] = n["children"][0]["relation"]
            for c in n["children"]:
                name(c)
        name(answer)
        ai = pp.parse_ai_plan(json.dumps(answer), {"nation", "orders", "customer"}, set())
        self.assertFalse([n for _, n in cmp.iter_nodes(ai) if n.extra.get("Warning")])
        self.assertEqual(cmp.compare_plans(pg, ai).difference_rate, 0)

    def test_invented_index_is_flagged(self):
        plan = pp.parse_ai_plan('{"node_type": "Index Scan", "relation": "orders", '
                                '"index_name": "orders_date_idx"}', {"orders"}, {"orders_pkey"})
        self.assertIn("does not exist", plan.extra.get("Warning", ""))

    def test_real_difference_is_found(self):
        pg = pg_plan("A3")                       # Seq Scan on orders under a Gather
        ai = pp.parse_ai_plan('{"node_type": "Aggregate", "children": [{"node_type": '
                              '"Index Scan", "relation": "orders", "index_name": "orders_pkey"}]}')
        result = cmp.compare_plans(pg, ai)
        cats = {d.category for d in result.differences}
        self.assertIn("Access method", cats)
        self.assertIn("Parallelism", cats)
        self.assertTrue(0 < result.difference_rate < 1)


class FakeConnection:
    closed = False

    def close(self):
        self.closed = True


class SessionPipeline(unittest.TestCase):
    """Session.analyze with the database and Gemini replaced by fixtures."""

    def setUp(self):
        self.saved = (pp.get_postgres_plan, pp.get_ai_plan)
        self.session = project.Session(pp.DBConfig(dbname="tpch"), pp.AIConfig(api_key=""))
        self.session.conn = FakeConnection()

    def tearDown(self):
        pp.get_postgres_plan, pp.get_ai_plan = self.saved

    def fake_pg(self, conn, sql):
        qid = next(k for k, v in FIXTURES.items() if v["sql"] == sql)
        return pg_plan(qid), FIXTURES[qid]["explain"]

    def test_full_pipeline(self):
        pp.get_postgres_plan = self.fake_pg

        def fake_ai(conn, sql, cfg=None, use_cache=True):
            text = as_ai_answer(self.fake_pg(conn, sql)[0])
            plan = pp.parse_ai_plan(text)
            plan.extra["AI Model"] = "fake"
            return plan, text
        pp.get_ai_plan = fake_ai
        stages = []
        result = self.session.analyze(FIXTURES["C3"]["sql"] + ";", on_progress=stages.append)
        self.assertTrue(result.complete)
        self.assertEqual(result.ai_model, "fake")
        self.assertEqual(result.comparison.difference_rate, 0)
        self.assertGreaterEqual(len(stages), 3)
        json.dumps(result.to_dict(), default=str)

    def test_ai_failure_still_returns_postgres_plan(self):
        pp.get_postgres_plan = self.fake_pg

        def failing_ai(*args, **kwargs):
            raise PlanError("No Gemini API key.")
        pp.get_ai_plan = failing_ai
        result = self.session.analyze(FIXTURES["A1"]["sql"])
        self.assertFalse(result.complete)
        self.assertIsNotNone(result.pg_plan)
        self.assertIn("No Gemini API key", result.explanation_text())

    def test_not_connected(self):
        self.session.conn = None
        with self.assertRaises(PlanError):
            self.session.analyze("SELECT 1")


class EvaluationReports(unittest.TestCase):
    """write_reports and rescore on a results folder built from fixtures."""

    def test_reports_and_rescore(self):
        out = tempfile.mkdtemp()
        try:
            records = []
            for qid in FIXTURES:
                pg = pg_plan(qid)
                ai_text = as_ai_answer(pg)
                if qid == "A3":                   # one AI answer that differs
                    ai_text = ai_text.replace('"Seq Scan"', '"Index Scan"')
                ai = pp.parse_ai_plan(ai_text)
                analysis = project.Analysis(sql=FIXTURES[qid]["sql"], database="tpch",
                                            pg_plan=pg, pg_raw=FIXTURES[qid]["explain"],
                                            ai_plan=ai, ai_raw=ai_text, ai_model="fake",
                                            comparison=cmp.compare_plans(pg, ai))
                query = project.WorkloadQuery(qid, f"Cat {qid[0]}", "test", FIXTURES[qid]["sql"])
                rec = project._record(query, analysis, None)
                records.append(rec)
                os.makedirs(os.path.join(out, "queries"), exist_ok=True)
                project._save_json(os.path.join(out, "queries", f"{qid}.json"), rec)
            failed = project._record(project.WorkloadQuery("Z9", "Cat Z", "", "SELEC"),
                                     None, "syntax error")
            records.append(failed)
            project._save_json(os.path.join(out, "queries", "Z9.json"), failed)

            summary = project.write_reports(records, {"tables": [], "indexes": []}, out)
            self.assertEqual(summary["compared"], len(FIXTURES))
            self.assertEqual(summary["failed"], [("Z9", "syntax error")])
            self.assertEqual(summary["overall"]["identical_plans"], len(FIXTURES) - 1)
            for name in ("summary.md", "summary.csv", "summary.json"):
                self.assertTrue(os.path.exists(os.path.join(out, name)))

            again = project.rescore(out)          # offline recomputation
            self.assertEqual(again["overall"], summary["overall"])
        finally:
            shutil.rmtree(out)


@unittest.skipUnless(os.environ.get("SC3020_TEST_DB") == "1",
                     "set SC3020_TEST_DB=1 to run the tests that need PostgreSQL")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = project.Session()
        cls.session.connect()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_every_workload_query_plans_and_self_compares_to_zero(self):
        for q in project.read_workload(project.DEFAULT_WORKLOAD):
            with self.subTest(q.qid):
                plan, _ = pp.get_postgres_plan(self.session.conn, q.sql)
                self.assertEqual(cmp.compare_plans(plan, copy.deepcopy(plan)).difference_rate, 0)
                ai = pp.parse_ai_plan(as_ai_answer(plan))
                self.assertEqual(cmp.compare_plans(plan, ai).difference_rate, 0)

    def test_schema_and_statistics_readable(self):
        self.assertIn("TABLES:", self.session.schema_text())
        self.assertIn("STATISTICS", pp.get_table_stats_text(self.session.conn))
        self.assertIn(self.session.db_config.dbname, self.session.list_databases())


if __name__ == "__main__":
    unittest.main()
