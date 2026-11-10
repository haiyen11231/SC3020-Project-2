"""Tests for the non-graphical helpers of interface.py (no window is opened).

Run from the project folder:  python -m unittest discover -s tests -v
Skipped automatically on a Python without Tkinter.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import interface
except ImportError:                      # Python built without Tkinter
    interface = None

from preprocessing import PlanNode       # noqa: E402


def node(node_type, children=(), **kw):
    return PlanNode(node_type=node_type, children=list(children), **kw)


@unittest.skipIf(interface is None, "Tkinter is not available")
class Labels(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(interface.node_title(node("Seq Scan")), "Seq Scan")
        agg = node("Aggregate", extra={"Strategy": "Hashed", "Partial Mode": "Partial"})
        self.assertEqual(interface.node_title(agg), "Partial Aggregate (Hashed)")
        self.assertEqual(interface.node_title(node("Aggregate", extra={"Strategy": "Plain"})),
                         "Aggregate")
        self.assertEqual(interface.node_title(node("Hash Join", join_type="Semi")),
                         "Hash Join (Semi)")
        self.assertEqual(interface.node_title(node("Hash Join", join_type="Inner")), "Hash Join")

    def test_subtitle_and_rows(self):
        scan = node("Index Scan", relation="orders", index_name="orders_pkey",
                    extra={"Alias": "o"}, rows=1)
        self.assertEqual(interface.node_subtitle(scan), "orders o")
        self.assertEqual(interface.node_subtitle(node("CTE Scan", extra={"CTE Name": "x"})), "x")
        self.assertEqual(interface.rows_text(scan), "≈ 1 row")
        self.assertEqual(interface.rows_text(node("Seq Scan", rows=6001215.0)), "≈ 6,001,215 rows")
        self.assertEqual(interface.rows_text(node("Seq Scan")), "")

    def test_wrap_words(self):
        self.assertEqual(interface.wrap_words("Finalize Aggregate (Sorted)", 20),
                         ["Finalize Aggregate", "(Sorted)"])
        lines = interface.wrap_words("a very long operator name that will not fit", 10)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 10 for line in lines))

    def test_verdict_and_percent(self):
        self.assertIn("identical", interface.verdict(0))
        self.assertIn("very similar", interface.verdict(0.1))
        self.assertIn("very different", interface.verdict(0.8))
        self.assertEqual(interface.percent(None), "n/a")
        self.assertEqual(interface.percent(0.335), "34%")

    def test_quote_ident(self):
        self.assertEqual(interface.quote_ident("lineitem"), "lineitem")
        self.assertEqual(interface.quote_ident("UserRating"), '"UserRating"')
        self.assertEqual(interface.quote_ident('we"ird'), '"we""ird"')


@unittest.skipIf(interface is None, "Tkinter is not available")
class Layout(unittest.TestCase):
    def test_parent_centred_over_children(self):
        plan = node("Hash Join", [node("Seq Scan"), node("Hash", [node("Seq Scan")])])
        positions, columns, depth = interface.layout_tree(plan)
        self.assertEqual((columns, depth), (2, 2))
        self.assertEqual(positions["0.0"], (0, 1))
        self.assertEqual(positions["0.1"], (1, 1))
        self.assertEqual(positions["0"], (0.5, 0))

    def test_single_node(self):
        positions, columns, depth = interface.layout_tree(node("Result"))
        self.assertEqual((positions, columns, depth), ({"0": (0, 0)}, 1, 0))


@unittest.skipIf(interface is None, "Tkinter is not available")
class Workload(unittest.TestCase):
    def test_reads_shipped_workload(self):
        queries = interface.read_workload_file(interface.WORKLOAD_FILE)
        self.assertGreaterEqual(len(queries), 40)
        qid, purpose, sql = queries[0]
        self.assertTrue(qid and purpose and sql.endswith(";"))

    def test_plain_file_and_missing_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
            f.write("-- just a comment\nSELECT 1;\nSELECT 2\nFROM t;\n")
        try:
            queries = interface.read_workload_file(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual([q[0] for q in queries], ["Q1", "Q2"])
        self.assertEqual(interface.read_workload_file("/no/such/file.sql"), [])


if __name__ == "__main__":
    unittest.main()
