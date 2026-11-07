"""comparison.py (Part 3): compares the PostgreSQL plan with the AI plan.

Usage:
    result = compare_plans(pg_plan, ai_plan)
    result.difference_rate        # 0 = identical, 1 = nothing in common
    result.explanation_text()     # differences and similarities as text
    result.status("postgres", "0.1")   # node status, for the GUI colours

Nodes are matched by the data they produce, not by tree position:
    1. Scans are matched by table (and filters, for self-joins).
    2. Joins are matched by the set of scans below them.
    3. One-input operators (Sort, Hash, Aggregate, ...) are aligned in order
       above each matched pair.
Each pair is then compared field by field.

Difference rate: each node costs 0 (identical), 0.5 (same operator, other
details) or 1 (other operator, or no partner). The sum is divided by the
total node count. See docs/comparison.md for details.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import statistics
from dataclasses import dataclass, field
from itertools import permutations
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

from preprocessing import PlanError, PlanNode

# Node statuses and GUI colours
SAME = "same"                    # matched, same operator and details
CHANGED = "changed"              # matched, but operator or details differ
ONLY_PG = "postgres_only"        # node exists only in the PostgreSQL plan
ONLY_AI = "ai_only"              # node exists only in the AI plan

STATUS_COLORS = {SAME: "#4CAF50", CHANGED: "#FFA726", ONLY_PG: "#EF5350", ONLY_AI: "#42A5F5"}
STATUS_LABELS = {SAME: "Same in both plans", CHANGED: "Present in both, but different",
                 ONLY_PG: "Only in the PostgreSQL plan", ONLY_AI: "Only in the AI plan"}

PG, AI = "postgres", "ai"
SIDE_NAME = {PG: "PostgreSQL", AI: "the AI"}
PLAN_NAME = {PG: "PostgreSQL plan", AI: "AI plan"}

# Operator groups (general PostgreSQL facts, no schema)
SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
              "Tid Scan", "Tid Range Scan", "Sample Scan", "Foreign Scan", "Custom Scan"}
BITMAP_INNER = {"Bitmap Index Scan", "BitmapAnd", "BitmapOr"}
JOIN_TYPES = {"Nested Loop", "Hash Join", "Merge Join"}
PARALLEL_TYPES = {"Gather", "Gather Merge"}
ORDERED_KEY_TYPES = {"Sort", "Incremental Sort", "Gather Merge", "Merge Append"}

# Operators that do the same job. Two members of one family can be matched
# (shown as "changed" instead of one removed and one added node).
_FAMILIES = [
    {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Tid Scan",
     "Tid Range Scan", "Sample Scan"},
    {"Nested Loop", "Hash Join", "Merge Join"},
    {"Sort", "Incremental Sort"},
    {"Gather", "Gather Merge"},
    {"Aggregate", "Unique"},
    {"Materialize", "Memoize"},
    {"Append", "Merge Append"},
    BITMAP_INNER,
]

OPERATOR_INFO = {
    "Seq Scan": "reads every row of the table and applies the filter to each",
    "Index Scan": "looks up the matching rows through an index, then fetches them from the table",
    "Index Only Scan": "answers from the index alone, without reading the table rows",
    "Bitmap Heap Scan": "collects matching row locations from an index first, then reads "
                        "the table pages in physical order",
    "Bitmap Index Scan": "finds matching row locations in an index for a Bitmap Heap Scan",
    "Nested Loop": "for every outer row, searches the inner input for matches",
    "Hash Join": "builds a hash table on the inner input, then probes it with every outer row",
    "Merge Join": "merges two inputs that are sorted on the join key",
    "Hash": "builds the in-memory hash table for a Hash Join",
    "Sort": "sorts its input",
    "Incremental Sort": "sorts an input that is already partly sorted",
    "Aggregate": "computes aggregates (GROUP BY, SUM, COUNT, ...)",
    "Unique": "removes duplicate rows from sorted input",
    "Gather": "collects rows from parallel worker processes",
    "Gather Merge": "collects sorted rows from parallel workers, keeping the order",
    "Limit": "stops after the first rows",
    "Materialize": "stores its input in memory so it can be re-read cheaply",
    "Memoize": "caches inner results for repeated parameter values",
    "Append": "concatenates the results of its inputs",
    "Subquery Scan": "reads the result of a subquery",
    "Result": "computes values without reading a table",
    "WindowAgg": "computes window functions",
    "SetOp": "computes INTERSECT or EXCEPT",
    "BitmapAnd": "intersects the row locations found by several bitmap index scans",
    "BitmapOr": "unites the row locations found by several bitmap index scans",
    "Merge Append": "merges sorted inputs, keeping the order",
    "CTE Scan": "reads the result of a WITH query",
    "Function Scan": "reads the rows returned by a function",
    "Values Scan": "reads a VALUES list",
    "WorkTable Scan": "reads the working table of a recursive query",
    "Recursive Union": "evaluates a recursive WITH query",
    "ProjectSet": "evaluates set-returning functions",
    "Tid Scan": "fetches rows by their physical location (ctid)",
    "LockRows": "locks the selected rows (SELECT ... FOR UPDATE)",
}


def _info(node_type: str) -> str:
    """' (what the operator does)', or '' for an operator we do not know."""
    text = OPERATOR_INFO.get(node_type)
    return f" ({text})" if text else ""

_WHY = {
    "scan": ("A sequential scan wins when many rows qualify. An index scan wins when "
             "few rows qualify. A bitmap scan sits in between."),
    "join": ("Hash join suits large inputs. Nested loop suits a small outer input, "
             "especially with an index on the inner side. Merge join suits sorted inputs."),
    "dedup": ("Hashing needs no sort. Unique needs sorted input. "
              "Hashing is usually cheaper unless the input is already sorted."),
    "agg": ("Hashed aggregation needs no sorted input but more memory. "
            "Sorted aggregation needs sorted input but little memory."),
}


def _family(node_type: str) -> Optional[int]:
    for i, fam in enumerate(_FAMILIES):
        if node_type in fam:
            return i
    return None


def _compatible(a: str, b: str) -> bool:
    """True if two operators may be matched to each other (same or same family)."""
    return a == b or (_family(a) is not None and _family(a) == _family(b))


# Tree helpers
def iter_nodes(plan: PlanNode, path: str = "0") -> Iterator[Tuple[str, PlanNode]]:
    """Yield (path, node) in pre-order. Root is "0", its children "0.0", "0.1"."""
    yield path, plan
    for i, child in enumerate(plan.children):
        yield from iter_nodes(child, f"{path}.{i}")


def node_at(plan: PlanNode, path: str) -> PlanNode:
    """The node at a path produced by iter_nodes()."""
    node = plan
    for part in path.split(".")[1:]:
        node = node.children[int(part)]
    return node


def plan_from_dict(d: Any) -> PlanNode:
    """PlanNode from to_dict() output. A PlanNode is returned unchanged."""
    if isinstance(d, PlanNode):
        return d
    if not isinstance(d, dict) or "node_type" not in d:
        raise PlanError("A plan to compare is not in the common PlanNode format.")
    known = {k: v for k, v in d.items() if k in PlanNode.__dataclass_fields__}
    known["children"] = [plan_from_dict(c) for c in d.get("children") or []]
    return PlanNode(**known)


# Expression normalisation: "(lineitem.l_partkey = part.p_partkey)" and
# "p_partkey = l_partkey" count as the same predicate.
_CAST_RE = re.compile(r"::\s*(?:(?:timestamp|time)(?:\(\d+\))? with(?:out)? time zone|"
                      r"character varying|double precision|bit varying|\"?[a-z_][a-z0-9_]*\"?)"
                      r"(?:\(\d+(?:,\s*\d+)?\))?(?:\[\])?", re.I)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_$]*)\b(\s*\()?")
_OP_RE = re.compile(r"!~~\*?|~~\*?|<=|>=|<>|!=|=|<|>|\bnot\s+like\b|\blike\b|"
                    r"\bnot\s+in\b|\bin\b|\bis\s+not\s+null\b|\bis\s+null\b|\bbetween\b|"
                    r"\bany\b|\bexists\b")
_OP_CANON = {"!=": "<>", "~~": "like", "~~*": "like", "!~~": "not like", "!~~*": "not like",
             "any": "in", "between": "range"}
_KEYWORDS = {
    "and", "or", "not", "like", "ilike", "in", "is", "null", "between", "true", "false",
    "case", "when", "then", "else", "end", "any", "all", "some", "exists", "date",
    "interval", "timestamp", "time", "zone", "with", "without", "year", "month", "day",
    "hour", "minute", "second", "as", "distinct", "subplan", "initplan", "returns",
    "numeric", "integer", "bigint", "text", "bpchar", "character", "varying", "double",
    "precision", "real", "boolean", "asc", "desc", "nulls", "first", "last", "from",
    "using", "hashed", "select", "where", "on",
}


def _norm_expr(expr: Any) -> str:
    """Lower case, no casts, no table prefixes, no brackets, single spaces."""
    s = str(expr).lower()
    s = _CAST_RE.sub("", s)
    s = re.sub(r"\b[a-z_][a-z0-9_]*\.(?=[a-z_\"])", "", s)   # alias.column -> column
    s = s.replace('"', "")
    s = re.sub(r"[()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _split_and(expr: str) -> List[str]:
    """Split on top-level AND, skipping brackets and BETWEEN x AND y."""
    text = str(expr)
    parts, depth, start, i = [], 0, 0, 0
    lower = text.lower()
    pending_between = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":                       # skip string literals
            j = i + 1
            while j < len(text) and not (text[j] == "'" and text[j + 1:j + 2] != "'"):
                j += 2 if text[j:j + 2] == "''" else 1
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif lower.startswith("between", i) and _word_at(lower, i, 7):
            pending_between += 1
        elif lower.startswith("and", i) and _word_at(lower, i, 3):
            if pending_between:
                pending_between -= 1
            elif depth <= _outer_depth(text):
                parts.append(text[start:i])
                start = i + 3
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _word_at(text: str, i: int, length: int) -> bool:
    """True if text[i:i+length] is a whole word, not part of e.g. x_and_y."""
    before = text[i - 1] if i > 0 else " "
    after = text[i + length] if i + length < len(text) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _outer_depth(text: str) -> int:
    """Brackets around the whole expression, e.g. 1 for '(a AND b)'."""
    t = text.strip()
    depth = 0
    while t.startswith("(") and t.endswith(")"):
        level, ok = 0, True
        for k, ch in enumerate(t):
            level += ch == "("
            level -= ch == ")"
            if level == 0 and k < len(t) - 1:
                ok = False
                break
        if not ok:
            break
        depth += 1
        t = t[1:-1].strip()
    return depth


def _columns(expr: str) -> FrozenSet[str]:
    """Column names in an expression (no functions, keywords or literals)."""
    s = _STRING_RE.sub(" ", str(expr).lower())
    s = _CAST_RE.sub("", s)
    s = re.sub(r"\b[a-z_][a-z0-9_]*\.(?=[a-z_\"])", "", s).replace('"', "")
    cols = set()
    for name, call in _IDENT_RE.findall(s):
        if not call and name not in _KEYWORDS and not name.startswith("$"):
            cols.add(name)
    return frozenset(cols)


@dataclass(frozen=True)
class Predicate:
    """One AND-ed condition: its columns and comparison operators."""
    columns: FrozenSet[str]
    operators: FrozenSet[str]
    text: str                                      # normalised text, for messages


_SUBPLAN_RE = re.compile(r"\bsubplan\b", re.I)
_SUBQUERY_REF_RE = re.compile(
    r"\(\s*initplan \d+\s*\)\.col\d+|\$\d+|\(\s*select\b[^()]*(\([^()]*\)[^()]*)*\)", re.I)


def _predicates(conditions: Dict[str, str]) -> List[Predicate]:
    """Predicates in a node's conditions. SubPlan tests are skipped (the
    subquery note covers them). InitPlan results and inline subqueries
    become one placeholder, so they compare equal."""
    preds = []
    for expr in conditions.values():
        for part in _split_and(str(expr)):
            if _SUBPLAN_RE.search(part):
                continue
            shown = _norm_expr(part)
            part = _SUBQUERY_REF_RE.sub(" $sub ", part)
            norm = _STRING_RE.sub(" ", part.lower())
            ops = frozenset(_OP_CANON.get(re.sub(r"\s+", " ", o), re.sub(r"\s+", " ", o))
                            for o in _OP_RE.findall(norm))
            # "x BETWEEN a AND b" and "x >= a AND x <= b" test the same thing
            if "range" in ops:
                ops = (ops - {"range"}) | {">=", "<="}
            cols = _columns(part)
            if cols or ops:
                preds.append(Predicate(cols, ops, shown))
    return preds


def _range_halves(preds: List[Predicate]) -> Set[Tuple[FrozenSet[str], str]]:
    """Columns and comparison directions tested. BETWEEN equals >= plus <=."""
    out = set()
    for p in preds:
        for op in p.operators:
            if op in ("<", "<="):
                out.add((p.columns, "upper"))
            elif op in (">", ">="):
                out.add((p.columns, "lower"))
            else:
                out.add((p.columns, op))
        if not p.operators:
            out.add((p.columns, "?"))
    return out


def _literal_form(text: str) -> str:
    """Predicate text without spaces, literals kept."""
    return re.sub(r"\s+", "", text)


def _is_column_equality(p: Predicate) -> bool:
    """'a = b' between two columns: a join predicate."""
    return (p.operators == frozenset({"="}) and len(p.columns) == 2
            and not re.search(r"'|\b\d", p.text))


_BARE_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*)( desc)?$")


def _norm_keys(keys: List[str]) -> Tuple[str, ...]:
    """Group / sort keys without casts, table prefixes, brackets or ASC."""
    return tuple(_norm_expr(k).replace(" asc", "") for k in keys)


def _partial_mode(node: PlanNode) -> str:
    """'Partial' for a worker-side aggregate, else 'Simple' (Finalize counts as Simple)."""
    mode = node.extra.get("Partial Mode", "Simple")
    return "Partial" if mode == "Partial" else "Simple"


def _is_subplan(node: PlanNode) -> bool:
    """True for a subquery plan (InitPlan: run once, SubPlan: run per row)."""
    return node.extra.get("Parent Relationship") in ("InitPlan", "SubPlan")


def _a(word: str) -> str:
    """'a' or 'an' before a word."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def _q_error(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Cardinality error max(a/b, b/a)."""
    if a is None or b is None:
        return None
    a, b = max(float(a), 1.0), max(float(b), 1.0)
    return max(a / b, b / a)


# Result objects
@dataclass
class Difference:
    """One explained difference between the plans."""
    category: str                  # e.g. "Access method", "Join order"
    text: str                      # readable explanation
    pg_path: Optional[str] = None  # node in the PostgreSQL plan it refers to, if any
    ai_path: Optional[str] = None  # node in the AI plan it refers to, if any


@dataclass
class NodeMatch:
    """Two matched nodes, or one unmatched node (other path None)."""
    pg_path: Optional[str]
    ai_path: Optional[str]
    status: str
    cost: float                    # contribution to the difference rate (per node)
    notes: List[str] = field(default_factory=list)


@dataclass
class ComparisonResult:
    pg_plan: PlanNode
    ai_plan: PlanNode
    matches: List[NodeMatch]
    differences: List[Difference]
    similarities: List[str]
    metrics: Dict[str, Any]
    join_order: Dict[str, str]

    def __post_init__(self):
        self._by_side: Dict[str, Dict[str, NodeMatch]] = {PG: {}, AI: {}}
        for m in self.matches:
            if m.pg_path is not None:
                self._by_side[PG][m.pg_path] = m
            if m.ai_path is not None:
                self._by_side[AI][m.ai_path] = m

    # GUI helpers
    @property
    def difference_rate(self) -> float:
        return self.metrics["difference_rate"]

    @property
    def identical(self) -> bool:
        return self.metrics["difference_rate"] == 0

    def status(self, side: str, path: str) -> str:
        """Node status. side: "postgres" or "ai"."""
        return self._by_side[side][path].status

    def color(self, side: str, path: str) -> str:
        return STATUS_COLORS[self.status(side, path)]

    def partner(self, side: str, path: str) -> Optional[str]:
        """Path of the matched node in the other plan, or None."""
        m = self._by_side[side][path]
        return m.ai_path if side == PG else m.pg_path

    def notes(self, side: str, path: str) -> List[str]:
        """Explanations for one node (tooltips)."""
        return list(self._by_side[side][path].notes)

    def node_statuses(self, side: str) -> Dict[str, str]:
        return {p: m.status for p, m in self._by_side[side].items()}

    # Text output
    def explanation_text(self) -> str:
        m = self.metrics
        lines = []
        if self.identical:
            lines.append("The two plans are identical in structure, operators and details.")
        else:
            lines.append(f"Difference rate: {m['difference_rate']:.0%} "
                         f"(tree edit distance rate: {m['ted_rate']:.0%}). "
                         f"{m['identical_pairs']} of {m['pg_nodes']} PostgreSQL nodes have an "
                         f"identical counterpart in the AI plan's {m['ai_nodes']} nodes.")
        lines.append("")
        lines.append(f"PostgreSQL join order: {self.join_order[PG]}")
        lines.append(f"AI join order:         {self.join_order[AI]}")
        if self.differences:
            lines += ["", "DIFFERENCES"]
            for i, d in enumerate(self.differences, 1):
                lines.append(f"{i}. [{d.category}] {d.text}")
        if self.similarities:
            lines += ["", "SIMILARITIES"]
            lines += [f"- {s}" for s in self.similarities]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "metrics": self.metrics,
            "join_order": self.join_order,
            "differences": [d.__dict__ for d in self.differences],
            "similarities": self.similarities,
            "matches": [m.__dict__ for m in self.matches],
            "node_status": {PG: self.node_statuses(PG), AI: self.node_statuses(AI)},
        }


# The comparison algorithm
UNIT, ANCHOR, CHAIN, ABSORBED = "unit", "anchor", "chain", "absorbed"


class _Info:
    """Matching state of one node."""
    __slots__ = ("node", "path", "side", "parent", "children", "subs", "kind", "unit_of", "main_sig",
                 "partner", "token", "sig", "fuzzy_sig", "inputs_differ")

    def __init__(self, node: PlanNode, path: str, side: str, parent: Optional["_Info"]):
        self.node, self.path, self.side, self.parent = node, path, side, parent
        # children: the operator's real inputs, in order (outer first for a join).
        # subs: subquery plans attached to it (InitPlan / SubPlan). PostgreSQL lists
        # them before the inputs, so keeping them apart makes children[0] / [1]
        # always mean the outer / inner input.
        self.children: List[_Info] = []
        self.subs: List[_Info] = []
        self.kind = CHAIN
        self.unit_of: Optional[_Info] = None   # for ABSORBED: the Bitmap Heap Scan
        self.partner: Optional[_Info] = None
        self.token: Optional[str] = None       # units only
        self.sig: FrozenSet[str] = frozenset()
        self.main_sig: FrozenSet[str] = frozenset()   # sig without subquery plans
        self.fuzzy_sig: FrozenSet[str] = frozenset()
        self.inputs_differ = False

    @property
    def type(self) -> str:
        return self.node.node_type

    @property
    def all_children(self) -> List["_Info"]:
        """Inputs and subquery plans together."""
        return self.children + self.subs

    def __repr__(self):
        return f"<{self.side} {self.path} {self.type}>"


class _Comparer:
    def __init__(self, pg_plan: PlanNode, ai_plan: PlanNode):
        self.plans = {PG: pg_plan, AI: ai_plan}
        self.info: Dict[str, List[_Info]] = {PG: [], AI: []}
        self.root: Dict[str, _Info] = {}
        for side, plan in self.plans.items():
            self.root[side] = self._index(plan, "0", side, None)
            self._classify(self.root[side])
        # Nested Loop <-> its inner scan, which may hold the join condition
        self.param_inner: Dict[int, _Info] = {}
        for side in (PG, AI):
            for inf in self.info[side]:
                if inf.type == "Nested Loop" and len(inf.children) == 2:
                    cur = inf.children[1]
                    while cur.kind == CHAIN:
                        cur = cur.children[0]
                    if cur.kind == UNIT:
                        self.param_inner[id(inf)] = cur
                        self.param_inner[id(cur)] = inf
        self.columns = {side: {c for inf in self.info[side]
                               for p in _predicates(inf.node.conditions) for c in p.columns}
                        | {c for inf in self.info[side] for k in inf.node.keys
                           for c in _columns(k)}
                        for side in (PG, AI)}

    # Step 1: index and classify
    def _index(self, node: PlanNode, path: str, side: str, parent) -> _Info:
        inf = _Info(node, path, side, parent)
        self.info[side].append(inf)
        kids = [self._index(c, f"{path}.{i}", side, inf) for i, c in enumerate(node.children)]
        inf.children = [k for k in kids if not _is_subplan(k.node)]
        inf.subs = [k for k in kids if _is_subplan(k.node)]
        return inf

    def _classify(self, inf: _Info, heap: Optional[_Info] = None) -> None:
        if heap is not None and inf.type in BITMAP_INNER:
            inf.kind, inf.unit_of = ABSORBED, heap
        elif inf.type in SCAN_TYPES or not inf.children:
            inf.kind = UNIT
        elif len(inf.children) >= 2:
            inf.kind = ANCHOR
        else:
            inf.kind = CHAIN
        owner = inf if inf.type == "Bitmap Heap Scan" else (heap if inf.kind == ABSORBED else None)
        for c in inf.all_children:
            self._classify(c, owner)

    def _preds(self, inf: _Info) -> List[Predicate]:
        """Predicates of a node. A join condition on the inner index scan of a
        Nested Loop is counted at the Nested Loop."""
        preds = _predicates(inf.node.conditions)
        other = self.param_inner.get(id(inf))
        if other is None:
            return preds
        if inf.kind == UNIT:
            return [p for p in preds if not _is_column_equality(p)]
        return preds + [p for p in _predicates(other.node.conditions) if _is_column_equality(p)]

    def _same_conditions(self, p: _Info, a: _Info) -> bool:
        return _range_halves(self._preds(p)) == _range_halves(self._preds(a))

    def _keys_equal(self, p: _Info, a: _Info) -> bool:
        """Compare keys. An alias (revenue) matches an expression (sum(...))."""
        ordered = p.type in ORDERED_KEY_TYPES
        kp, ka = _norm_keys(p.node.keys), _norm_keys(a.node.keys)
        if (kp if ordered else set(kp)) == (ka if ordered else set(ka)):
            return True
        if len(kp) != len(ka):
            return False
        if not ordered:
            kp, ka = sorted(set(kp) - set(ka)), sorted(set(ka) - set(kp))
            if len(kp) != len(ka):
                return False
        for x, y in zip(kp, ka):
            if x == y:
                continue
            if not (self._alias_of(x, y, AI) or self._alias_of(y, x, PG)):
                return False
        return True

    def _alias_of(self, alias: str, expr: str, other_side: str) -> bool:
        """True if alias is an unknown bare name and expr an expression, same direction."""
        m = _BARE_KEY_RE.match(alias)
        if not m or _BARE_KEY_RE.match(expr) or m.group(1) in self.columns[other_side]:
            return False
        return bool(m.group(2)) == expr.endswith(" desc")

    def _of(self, side: str, kind: str) -> List[_Info]:
        return [i for i in self.info[side] if i.kind == kind]

    # Text descriptions
    @staticmethod
    def _unit_key(inf: _Info) -> str:
        return (inf.node.relation or inf.type).lower()

    def _relations(self, inf: _Info, main_only: bool = False) -> List[str]:
        """Tables read in the subtree, one entry per scan. main_only skips subqueries."""
        out = []
        stack = [inf]
        while stack:
            cur = stack.pop()
            if cur.kind == UNIT:
                out.append(cur.node.relation or cur.type)
            stack.extend(reversed(cur.children if main_only else cur.all_children))
        return out

    def _rel_text(self, inf: _Info) -> str:
        rels = self._relations(inf, main_only=True)
        return ", ".join(rels) if rels else "no table"

    def _describe(self, inf: _Info) -> str:
        n = inf.node
        if inf.kind == UNIT and n.relation:
            text = f"{n.node_type} on {n.relation}"
            if n.index_name:
                text += f" using {n.index_name}"
            return text
        if inf.kind == ABSORBED:
            return f"{n.node_type}" + (f" on {n.index_name}" if n.index_name else "")
        if n.node_type in JOIN_TYPES and len(inf.children) == 2:
            return (f"{n.node_type} of ({self._rel_text(inf.children[0])}) with "
                    f"({self._rel_text(inf.children[1])})")
        details = [n.extra[k] for k in ("Strategy",) if n.extra.get(k)]
        if n.extra.get("Partial Mode") in ("Partial", "Finalize"):
            details.append(n.extra["Partial Mode"])
        label = n.node_type + (f" ({', '.join(details)})" if details else "")
        return f"{label} over ({self._rel_text(inf)})"

    # Step 2: match units
    def _unit_similarity(self, p: _Info, a: _Info) -> float:
        score = 0.0
        if p.type == a.type:
            score += 2
        elif _compatible(p.type, a.type):
            score += 1
        pi, ai = self._index_names(p), self._index_names(a)
        if pi and pi == ai:
            score += 1
        pp, ap = self._preds(p), self._preds(a)
        ps, as_ = _range_halves(pp), _range_halves(ap)
        score += 2 * (len(ps & as_) / len(ps | as_) if ps or as_ else 1.0)
        # literals separate e.g. nation n1 ('FRANCE') from n2 ('GERMANY')
        pt = {_literal_form(x.text) for x in pp}
        at = {_literal_form(x.text) for x in ap}
        score += len(pt & at) / len(pt | at) if pt or at else 1.0
        # same join partners
        cp, ca = self._context(p), self._context(a)
        if cp or ca:
            score += len(cp & ca) / len(cp | ca)
        return score

    def _context(self, inf: _Info) -> Set[str]:
        """Matched scans under the nearest join above this scan."""
        cur = inf.parent
        while cur is not None and cur.kind != ANCHOR:
            cur = cur.parent
        if cur is None:
            return set()
        out, stack = set(), [cur]
        while stack:
            x = stack.pop()
            if x.kind == UNIT and x is not inf and x.token:
                out.add(x.token)
            stack.extend(x.all_children)
        return out

    def _index_names(self, inf: _Info) -> Set[str]:
        names = {inf.node.index_name.lower()} if inf.node.index_name else set()
        for c in self.info[inf.side]:
            if c.kind == ABSORBED and c.unit_of is inf and c.node.index_name:
                names.add(c.node.index_name.lower())
        return names

    def _match_units(self) -> None:
        groups: Dict[str, Tuple[List[_Info], List[_Info]]] = {}
        for side, idx in ((PG, 0), (AI, 1)):
            for u in self._of(side, UNIT):
                groups.setdefault(self._unit_key(u), ([], []))[idx].append(u)
        # Single-read tables first; they give context for self-joins.
        ordered = sorted(groups.values(), key=lambda g: max(len(g[0]), len(g[1])) > 1)
        for pgs, ais in ordered:
            for p, a in self._best_assignment(pgs, ais, self._unit_similarity):
                self._pair(p, a)
                p.token = a.token = f"u:{p.path}"
        # Unmatched scans get their own token
        for side in (PG, AI):
            for u in self._of(side, UNIT):
                if u.partner is None:
                    u.token = f"{side}:{u.path}"

    @staticmethod
    def _best_assignment(left: List[_Info], right: List[_Info], score) -> List[Tuple[_Info, _Info]]:
        """Best one-to-one assignment. Exhaustive up to 7 items, greedy above."""
        if not left or not right:
            return []
        swap = len(left) > len(right)
        small, big = (right, left) if swap else (left, right)
        if len(big) <= 7:
            best, best_pairs = -1.0, []
            for perm in permutations(range(len(big)), len(small)):
                total = sum(score(*((big[j], s) if swap else (s, big[j])))
                            for s, j in zip(small, perm))
                if total > best + 1e-9:
                    best = total
                    best_pairs = [((big[j], s) if swap else (s, big[j]))
                                  for s, j in zip(small, perm)]
            return best_pairs
        cands = sorted(((score(l, r), i, j) for i, l in enumerate(left)
                        for j, r in enumerate(right)), key=lambda t: (-t[0], t[1], t[2]))
        used_l, used_r, pairs = set(), set(), []
        for _, i, j in cands:
            if i not in used_l and j not in used_r:
                used_l.add(i)
                used_r.add(j)
                pairs.append((left[i], right[j]))
        return pairs

    @staticmethod
    def _pair(p: _Info, a: _Info) -> None:
        p.partner, a.partner = a, p

    # Step 3: match anchors
    def _compute_sigs(self, inf: _Info) -> FrozenSet[str]:
        sig: Set[str] = set()
        main: Set[str] = set()
        fuzzy: Set[str] = set()
        if inf.kind == UNIT:
            sig.add(inf.token)
            main.add(inf.token)
            fuzzy.add(inf.token if inf.partner else "t:" + self._unit_key(inf))
        for c in inf.all_children:
            self._compute_sigs(c)
            sig |= c.sig
            fuzzy |= c.fuzzy_sig
            if not _is_subplan(c.node):
                main |= c.main_sig
        inf.sig, inf.main_sig, inf.fuzzy_sig = frozenset(sig), frozenset(main), frozenset(fuzzy)
        return inf.sig

    def _match_anchors(self) -> None:
        for side in (PG, AI):
            self._compute_sigs(self.root[side])
        # deepest first, so a join is matched before a Sort above it
        pg_anchors = sorted(self._of(PG, ANCHOR), key=lambda i: -i.path.count("."))
        ai_anchors = self._of(AI, ANCHOR)
        # Pass 1 ignores subquery plans: PostgreSQL may run a subquery as a SubPlan
        # where the AI writes a join. Pass 2 uses all scans.
        for attr, need_compatible in (("main_sig", True), ("sig", False)):
            by_sig: Dict[FrozenSet[str], List[_Info]] = {}
            for a in ai_anchors:
                if a.partner is None:
                    by_sig.setdefault(getattr(a, attr), []).append(a)
            for p in pg_anchors:
                if p.partner is not None:
                    continue
                cands = [a for a in by_sig.get(getattr(p, attr), []) if a.partner is None
                         and (_compatible(a.type, p.type) or not need_compatible)]
                if cands:
                    cands.sort(key=lambda a: (a.type != p.type, not _compatible(a.type, p.type)))
                    self._pair(p, cands[0])

        # Leftover joins (join order differs): pair by input overlap
        rest_p = [p for p in pg_anchors if p.partner is None]
        rest_a = [a for a in ai_anchors if a.partner is None]
        cands = []
        for p in rest_p:
            for a in rest_a:
                if not _compatible(p.type, a.type):
                    continue
                jac = len(p.fuzzy_sig & a.fuzzy_sig) / len(p.fuzzy_sig | a.fuzzy_sig)
                if jac >= 0.5:      # below 50%: not the same join
                    cands.append((jac, -abs(len(p.fuzzy_sig) - len(a.fuzzy_sig)), p, a))
        cands.sort(key=lambda t: (-t[0], -t[1], t[2].path, t[3].path))
        for _, _, p, a in cands:
            if p.partner is None and a.partner is None:
                self._pair(p, a)
                p.inputs_differ = a.inputs_differ = True

    # Step 4: match chains and bitmap sub-nodes
    def _chain_above(self, inf: _Info) -> List[_Info]:
        chain, child, cur = [], inf, inf.parent
        # Stop at the node a subquery plan is attached to: its operators belong to
        # the main plan's chain, not to the subquery's.
        while cur is not None and cur.kind == CHAIN and child not in cur.subs:
            chain.append(cur)
            child, cur = cur, cur.parent
        return chain

    def _chain_similarity(self, p: _Info, a: _Info) -> float:
        """0 if incompatible, else 2 plus detail similarity (0 to 1)."""
        if not _compatible(p.type, a.type):
            return 0.0
        checks = [p.type == a.type, _partial_mode(p.node) == _partial_mode(a.node)]
        sp, sa = p.node.extra.get("Strategy"), a.node.extra.get("Strategy")
        if sp and sa:
            checks.append(sp.lower() == sa.lower())
        if p.node.keys and a.node.keys:
            checks.append(self._keys_equal(p, a))
        return 2 + sum(checks) / len(checks)

    @staticmethod
    def _align(left: List[_Info], right: List[_Info], score) -> List[Tuple[_Info, _Info]]:
        """Order-preserving alignment with the best total score (weighted LCS)."""
        n, m = len(left), len(right)
        dp = [[0.0] * (m + 1) for _ in range(n + 1)]
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                s = score(left[i - 1], right[j - 1])
                best = max(dp[i - 1][j], dp[i][j - 1])
                if s > 0:
                    best = max(best, dp[i - 1][j - 1] + s)
                dp[i][j] = best
        pairs, i, j = [], n, m
        while i > 0 and j > 0:
            s = score(left[i - 1], right[j - 1])
            if s > 0 and abs(dp[i][j] - (dp[i - 1][j - 1] + s)) < 1e-9:
                pairs.append((left[i - 1], right[j - 1]))
                i, j = i - 1, j - 1
            elif abs(dp[i][j] - dp[i - 1][j]) < 1e-9:
                i -= 1
            else:
                j -= 1
        return pairs[::-1]

    def _match_chains(self) -> None:
        for p in self.info[PG]:
            if p.kind in (UNIT, ANCHOR) and p.partner is not None:
                a = p.partner
                for x, y in self._align(self._chain_above(p), self._chain_above(a),
                                        self._chain_similarity):
                    self._pair(x, y)
                if p.kind == UNIT:
                    sub_p = [c for c in self.info[PG] if c.unit_of is p]
                    sub_a = [c for c in self.info[AI] if c.unit_of is a]
                    for x, y in self._align(sub_p, sub_a, self._absorbed_similarity):
                        self._pair(x, y)

    def _match_root_chains(self) -> None:
        """Align the operators at the top of both plans (e.g. the final Aggregate),
        even when the joins below them were not matched to each other."""
        tops = {}
        for side in (PG, AI):
            chain, cur = [], self.root[side]
            while cur.kind == CHAIN:
                chain.append(cur)
                cur = cur.children[0]
            tops[side] = [c for c in reversed(chain) if c.partner is None]
        for x, y in self._align(tops[PG], tops[AI], self._chain_similarity):
            self._pair(x, y)

    def _match_build_sides(self) -> None:
        """Pair the Hash nodes of a matched join when they hash different inputs."""
        for p in self._of(PG, ANCHOR):
            a = p.partner
            if a is None or len(p.children) != 2 or len(a.children) != 2:
                continue
            hp, ha = p.children[1], a.children[1]
            if hp.type == ha.type == "Hash" and hp.partner is None and ha.partner is None:
                self._pair(hp, ha)
                hp.inputs_differ = ha.inputs_differ = True

    @staticmethod
    def _absorbed_similarity(p: _Info, a: _Info) -> float:
        if not _compatible(p.type, a.type):
            return 0.0
        same_index = (p.node.index_name or "").lower() == (a.node.index_name or "").lower()
        return 2 + (p.type == a.type) * 0.5 + same_index * 0.5

    # Compare one matched pair
    def _pair_differences(self, p: _Info, a: _Info) -> List[Tuple[str, str, bool]]:
        """(category, text, is_operator_change) for each difference in a pair."""
        out: List[Tuple[str, str, bool]] = []
        pn, an = p.node, a.node
        reason = an.extra.get("AI Reason")
        why_ai = f" The AI's reason: \"{reason}\"" if reason else ""

        if pn.node_type != an.node_type:
            if p.kind == UNIT and pn.node_type in SCAN_TYPES:
                cat, why = "Access method", _WHY["scan"]
                subject = f"Table {pn.relation or an.relation}"
            elif pn.node_type in JOIN_TYPES:
                cat, why = "Join algorithm", _WHY["join"]
                subject = f"The join of ({self._rel_text(p)})"
            elif {pn.node_type, an.node_type} == {"Aggregate", "Unique"}:
                cat, why = "Duplicate elimination", _WHY["dedup"]
                subject = f"Above ({self._rel_text(p)})"
            else:
                cat, why = "Operator", ""
                subject = f"Above ({self._rel_text(p)})"
            text = (f"{subject}: PostgreSQL uses {self._describe(p)}. "
                    f"The AI uses {self._describe(a)}.")
            if why:
                text += " " + why
            out.append((cat, text + why_ai, True))

        # index used
        pi, ai_ = self._index_names(p), self._index_names(a)
        if pi and ai_ and pi != ai_:
            out.append(("Index", f"{pn.node_type} on {pn.relation or an.relation}: PostgreSQL "
                        f"uses index {', '.join(sorted(pi))}, the AI uses "
                        f"{', '.join(sorted(ai_))}.", False))

        # non-join node over different inputs
        if p.inputs_differ and pn.node_type not in JOIN_TYPES:
            out.append(("Join inputs", f"{pn.node_type}: PostgreSQL applies it to "
                        f"({self._rel_text(p)}), the AI to ({self._rel_text(a)}).", False))

        # join inputs, input order and join type
        if pn.node_type in JOIN_TYPES and an.node_type in JOIN_TYPES \
                and len(p.children) == 2 and len(a.children) == 2:
            out += self._join_differences(p, a)
        elif pn.join_type and an.join_type and pn.join_type.lower() != an.join_type.lower():
            out.append(("Join type", f"{self._describe(p)}: PostgreSQL performs "
                        f"{_a(pn.join_type)} {pn.join_type} join, the AI {_a(an.join_type)} "
                        f"{an.join_type} join.", False))

        # conditions
        if p.kind != ABSORBED and not p.inputs_differ and not self._same_conditions(p, a):
            out.append(("Conditions", self._condition_text(p, a), False))

        # strategy (Aggregate / SetOp)
        sp, sa = pn.extra.get("Strategy"), an.extra.get("Strategy")
        if sp and sa and sp.lower() != sa.lower() and pn.node_type == an.node_type:
            out.append(("Aggregation strategy",
                        f"{self._describe(p)}: PostgreSQL uses {sp} aggregation, the AI "
                        f"uses {sa}. {_WHY['agg'] if pn.node_type == 'Aggregate' else ''}"
                        .strip() + why_ai, False))

        # group / sort keys
        if pn.keys and an.keys:
            if not self._keys_equal(p, a):
                out.append(("Keys", f"{self._describe(p)}: PostgreSQL uses keys "
                            f"[{', '.join(pn.keys)}], the AI uses [{', '.join(an.keys)}].",
                            False))
        return out

    def _join_differences(self, p: _Info, a: _Info) -> List[Tuple[str, str, bool]]:
        out = []
        pn, an = p.node, a.node
        po, pi = p.children
        ao, ai_ = a.children
        swapped = False
        if p.inputs_differ:
            out.append(("Join inputs",
                        f"{pn.node_type}: PostgreSQL joins ({self._rel_text(po)}) with "
                        f"({self._rel_text(pi)}). The AI joins ({self._rel_text(ao)}) with "
                        f"({self._rel_text(ai_)}).", False))
        elif po.sig == ai_.sig and pi.sig == ao.sig and po.sig != pi.sig:
            swapped = True
            if pn.node_type == an.node_type == "Hash Join":
                text = (f"Hash Join of ({self._rel_text(p)}): PostgreSQL hashes "
                        f"({self._rel_text(pi)}). The AI hashes ({self._rel_text(ai_)}). "
                        f"PostgreSQL usually hashes the smaller input.")
            else:
                text = (f"Join of ({self._rel_text(p)}): outer and inner inputs are "
                        f"swapped. PostgreSQL's outer input is ({self._rel_text(po)}).")
            out.append(("Join input order", text, False))

        jp, ja = (pn.join_type or "Inner"), (an.join_type or "Inner")
        mirror = {"left": "right", "right": "left"}
        if jp.lower() != ja.lower() and not (swapped and mirror.get(jp.lower()) == ja.lower()):
            out.append(("Join type", f"Join of ({self._rel_text(p)}): PostgreSQL performs "
                        f"{_a(jp)} {jp} join, the AI {_a(ja)} {ja} join.", False))
        return out

    def _condition_text(self, p: _Info, a: _Info) -> str:
        pp, ap = self._preds(p), self._preds(a)
        ph, ah = _range_halves(pp), _range_halves(ap)
        only_p = [x.text for x in pp if any(h not in ah for h in _range_halves([x]))]
        only_a = [x.text for x in ap if any(h not in ph for h in _range_halves([x]))]
        parts = [f"{self._describe(p)}:"]
        if only_p:
            parts.append(f"PostgreSQL applies {'; '.join(only_p)} here, the AI does not.")
        if only_a:
            parts.append(f"The AI applies {'; '.join(only_a)} here, PostgreSQL does not.")
        return " ".join(parts)

    # Plan-level findings
    def _join_order_text(self, inf: _Info) -> str:
        abbr = {"Hash Join": "HJ", "Nested Loop": "NL", "Merge Join": "MJ"}
        cur, subs = inf, []
        while cur.kind == CHAIN:            # subqueries attached along the way
            subs += cur.subs
            cur = cur.children[0]
        subs += [c for c in cur.subs if c.kind != ABSORBED]
        if cur.kind == UNIT:
            text = cur.node.relation or cur.type
            subs += [c for c in cur.children if c.kind != ABSORBED]
        else:
            label = abbr.get(cur.type, cur.type)
            text = f"{label}({', '.join(self._join_order_text(c) for c in cur.children)})"
        if subs:
            text += f"[{', '.join(self._join_order_text(c) for c in subs)}]"
        return text

    def _join_sets(self, side: str, ignore: Set[str] = frozenset()) -> List[Tuple[str, ...]]:
        """Tables under each join, without subquery plans and without the tables
        in ignore. Joins left with fewer than 2 tables are dropped."""
        out = set()
        for i in self._of(side, ANCHOR):
            if i.type in JOIN_TYPES:
                rels = tuple(sorted(r.lower() for r in self._relations(i, main_only=True)
                                    if r.lower() not in ignore))
                if len(rels) >= 2:
                    out.add(rels)
        return sorted(out)

    # Run
    def run(self) -> ComparisonResult:
        self._match_units()
        self._match_anchors()
        self._match_chains()
        self._match_root_chains()
        self._match_build_sides()

        matches: List[NodeMatch] = []
        differences: List[Difference] = []
        similarities: List[str] = []
        q_errors: List[Tuple[float, _Info]] = []
        identical = changed = 0
        total_cost = 0.0
        scan_pairs = scan_same = join_pairs = join_same = 0

        for p in self.info[PG]:                                       # matched pairs
            a = p.partner
            if a is None:
                continue
            diffs = self._pair_differences(p, a)
            op_change = any(d[2] for d in diffs)
            cost = 1.0 if op_change else (0.5 if diffs else 0.0)
            total_cost += 2 * cost
            notes = [d[1] for d in diffs]
            if diffs:
                changed += 1
            else:
                identical += 1
            matches.append(NodeMatch(p.path, a.path, CHANGED if diffs else SAME, cost, notes))
            for cat, text, _ in diffs:
                differences.append(Difference(cat, text, p.path, a.path))
            if p.kind == UNIT and p.type in SCAN_TYPES:
                scan_pairs += 1
                scan_same += p.type == a.type
            if p.type in JOIN_TYPES and a.type in JOIN_TYPES:
                join_pairs += 1
                join_same += p.type == a.type
            q = (_q_error(p.node.rows, a.node.rows)
                 if self._total_rows(p) and self._total_rows(a) else None)
            if q is not None:
                q_errors.append((q, p))
            if not diffs and p.kind in (UNIT, ANCHOR):
                similarities.append(f"Both plans use {self._describe(p)}.")

        for side, status in ((PG, ONLY_PG), (AI, ONLY_AI)):              # unmatched nodes
            for inf in self.info[side]:
                if inf.partner is not None:
                    continue
                total_cost += 1
                cat = "Missing operator" if side == PG else "Extra operator"
                text = f"Only the {PLAN_NAME[side]} has {self._describe(inf)}{_info(inf.type)}."
                if side == AI and inf.node.extra.get("AI Reason"):
                    text += f" The AI's reason: \"{inf.node.extra['AI Reason']}\""
                matches.append(NodeMatch(inf.path if side == PG else None,
                                         inf.path if side == AI else None, status, 1.0, [text]))
                differences.append(Difference(cat, text, inf.path if side == PG else None,
                                              inf.path if side == AI else None))

        differences = self._plan_level(differences, similarities, q_errors) + differences
        n_pg, n_ai = len(self.info[PG]), len(self.info[AI])
        metrics = {
            "difference_rate": round(total_cost / (n_pg + n_ai), 4),
            "ted_rate": round(tree_edit_distance(self.plans[PG], self.plans[AI]) / (n_pg + n_ai), 4),
            "pg_nodes": n_pg,
            "ai_nodes": n_ai,
            "matched_pairs": identical + changed,
            "identical_pairs": identical,
            "changed_pairs": changed,
            "postgres_only_nodes": sum(1 for i in self.info[PG] if i.partner is None),
            "ai_only_nodes": sum(1 for i in self.info[AI] if i.partner is None),
            "access_method_agreement": round(scan_same / scan_pairs, 4) if scan_pairs else None,
            "join_algorithm_agreement": round(join_same / join_pairs, 4) if join_pairs else None,
            "join_order_agreement": self._join_order_agreement(),
            "median_q_error": round(statistics.median(q for q, _ in q_errors), 2) if q_errors else None,
            "max_q_error": round(max(q for q, _ in q_errors), 2) if q_errors else None,
        }
        counts: Dict[str, int] = {}
        for line in similarities:
            counts[line] = counts.get(line, 0) + 1
        similarities = [line if counts[line] == 1 else f"{line[:-1]} ({counts[line]} times)."
                        for line in dict.fromkeys(similarities)]
        if not differences and metrics["difference_rate"] == 0:
            similarities.insert(0, "The plans are identical.")
        matches.sort(key=lambda m: (m.pg_path is None, _path_key(m.pg_path or m.ai_path)))
        return ComparisonResult(self.plans[PG], self.plans[AI], matches, differences,
                                similarities, metrics,
                                {PG: self._join_order_text(self.root[PG]),
                                 AI: self._join_order_text(self.root[AI])})

    @staticmethod
    def _total_rows(inf: _Info) -> bool:
        """False below a Gather (rows per worker), on a Nested Loop inner side (rows per
        loop) or below a Limit (PostgreSQL gives the rows of a complete run, but the Limit
        stops it early, so the AI's smaller number can be right)."""
        child, cur = inf, inf.parent
        while cur is not None:
            if cur.type in PARALLEL_TYPES or cur.type == "Limit":
                return False
            if cur.type == "Nested Loop" and len(cur.children) == 2 and cur.children[1] is child:
                return False
            child, cur = cur, cur.parent
        return True

    def _join_order_agreement(self) -> Optional[float]:
        jp, ja = self._join_sets(PG), self._join_sets(AI)
        if not jp and not ja:
            return None
        sp, sa = set(jp), set(ja)
        return round(len(sp & sa) / len(sp | sa), 4)

    def _plan_level(self, pair_diffs: List[Difference], similarities: List[str],
                    q_errors: List[Tuple[float, _Info]]) -> List[Difference]:
        out: List[Difference] = []

        # tables read by only one plan
        rp = [r.lower() for r in self._relations(self.root[PG])]
        ra = [r.lower() for r in self._relations(self.root[AI])]
        for t in sorted(set(rp) | set(ra)):
            cp, ca = rp.count(t), ra.count(t)
            if cp == ca:
                continue
            if not ca or not cp:
                side = PG if cp else AI
                text = (f"Only the {PLAN_NAME[side]} reads table {t}"
                        + (". The AI may have invented it." if side == AI else
                           ". The AI plan cannot produce the full result without it."))
            else:
                text = f"Table {t} is read {cp} times by PostgreSQL but {ca} times by the AI."
            out.append(Difference("Tables", text))

        # subqueries run as SubPlan / InitPlan
        sub_tables: Set[str] = set()
        sub_joins: Set[int] = set()
        for inf in self.info[PG]:
            same_place = (inf.partner is not None and inf.partner.parent is not None
                          and inf.partner.parent is (inf.parent.partner if inf.parent else None))
            if _is_subplan(inf.node) and inf.parent is not None and not same_place:
                kind = inf.node.extra["Parent Relationship"]
                rels = self._relations(inf)
                sub_tables |= {r.lower() for r in rels}
                joined = [j for j in self._of(AI, ANCHOR) if j.type in JOIN_TYPES and
                          {r.lower() for r in rels} <= {r.lower() for r in self._relations(j)}]
                text = (f"PostgreSQL evaluates the subquery over ({', '.join(rels)}) as a "
                        f"separate {kind} ({'computed once' if kind == 'InitPlan' else 'checked for each row'}) "
                        f"used by {self._describe(inf.parent)}")
                if joined:
                    j = min(joined, key=lambda x: len(self._relations(x)))
                    sub_joins.add(id(j))
                    jt = f"{j.node.join_type} " if j.node.join_type and j.node.join_type != "Inner" else ""
                    words = f"{jt}{j.type}"
                    text += f", while the AI turns it into {_a(words)} {words} ({self._describe(j)})."
                else:
                    text += "."
                out.append(Difference("Subquery", text, inf.path, None))

        # join order
        jp, ja = self._join_sets(PG, sub_tables), self._join_sets(AI, sub_tables)
        if jp or ja:
            if jp == ja:
                if jp:
                    lead = "Apart from the subquery, both" if sub_tables else "Both"
                    similarities.insert(0, f"{lead} plans join the tables in the same order "
                                        "(every intermediate join result is the same).")
            else:
                only_p = [s for s in jp if s not in ja]
                only_a = [s for s in ja if s not in jp]
                text = "The join order differs."
                if only_p:
                    text += " PostgreSQL builds the intermediate result(s) " + \
                        "; ".join("{" + ", ".join(s) + "}" for s in only_p) + \
                        ", which the AI plan does not."
                if only_a:
                    text += " The AI builds " + \
                        "; ".join("{" + ", ".join(s) + "}" for s in only_a) + \
                        ", which PostgreSQL does not."
                text += " This changes the size of every intermediate result."
                out.append(Difference("Join order", text))

        # parallelism
        gp = [i for i in self.info[PG] if i.type in PARALLEL_TYPES]
        ga = [i for i in self.info[AI] if i.type in PARALLEL_TYPES]
        if bool(gp) != bool(ga):
            side, nodes = (PG, gp) if gp else (AI, ga)
            other = AI if side == PG else PG
            partial = any(_partial_mode(i.node) == "Partial" for i in self.info[side])
            text = (f"{SIDE_NAME[side][0].upper() + SIDE_NAME[side][1:]} runs part of the "
                    f"plan in parallel ({', '.join(i.type for i in nodes)})")
            text += (", with partial aggregation in each worker" if partial else "")
            text += f". The {PLAN_NAME[other]} runs in a single process."
            out.append(Difference("Parallelism", text))
        elif gp and ga:
            similarities.append("Both plans use parallel workers (Gather).")

        # predicates applied in one plan only
        allp = [x for i in self.info[PG] for x in _predicates(i.node.conditions)]
        alla = [x for i in self.info[AI] if id(i) not in sub_joins
                for x in _predicates(i.node.conditions)]
        hp, ha = _range_halves(allp), _range_halves(alla)
        already = " ".join(d.text for d in pair_diffs if d.category == "Conditions")
        miss_a = sorted({x.text for x in allp if x.text not in already
                         and any(h not in ha for h in _range_halves([x]))})
        miss_p = sorted({x.text for x in alla if x.text not in already
                         and any(h not in hp for h in _range_halves([x]))})
        if miss_a:
            out.append(Difference("Conditions", "These conditions appear in the PostgreSQL "
                                  "plan but nowhere in the AI plan: " + "; ".join(miss_a) + "."))
        if miss_p:
            out.append(Difference("Conditions", "These conditions appear in the AI plan but "
                                  "nowhere in the PostgreSQL plan: " + "; ".join(miss_p) + "."))

        # AI plan warnings from preprocessing
        for inf in self.info[AI]:
            warning = inf.node.extra.get("Warning")
            if warning:
                out.append(Difference("Invalid AI node", f"{self._describe(inf)}: {warning}.",
                                      None, inf.path))

        # cardinality (reported, not in the rate)
        if q_errors:
            q, p = max(q_errors, key=lambda t: t[0])
            if q >= 10:
                out.append(Difference(
                    "Cardinality estimate",
                    f"The largest disagreement on row estimates is at {self._describe(p)}: "
                    f"PostgreSQL estimates {p.node.rows:,.0f} rows, the AI "
                    f"{p.partner.node.rows:,.0f} (factor {q:.1f}). Row estimates drive "
                    f"operator choice.",
                    p.path, p.partner.path))
        return out


def _path_key(path: Optional[str]) -> Tuple[int, ...]:
    return tuple(int(x) for x in path.split(".")) if path else ()


# Zhang-Shasha tree edit distance (second, order-sensitive metric)
def _ted_label_cost(a: PlanNode, b: PlanNode) -> float:
    """Relabel cost: 0, 1 or 2, same scale as the difference rate."""
    if a.node_type == b.node_type and (a.relation or "").lower() == (b.relation or "").lower():
        ia, ib = (a.index_name or "").lower(), (b.index_name or "").lower()
        return 1.0 if ia and ib and ia != ib else 0.0
    if _compatible(a.node_type, b.node_type):
        return 1.0
    return 2.0


def tree_edit_distance(t1: PlanNode, t2: PlanNode) -> float:
    """Zhang-Shasha (1989) tree edit distance, insert/delete cost 1."""
    def postorder(root):
        nodes, lmd = [], []

        def walk(n):
            first = None
            for c in n.children:
                fc = walk(c)
                first = fc if first is None else first
            nodes.append(n)
            idx = len(nodes) - 1
            lmd.append(first if first is not None else idx)
            return lmd[idx]
        walk(root)
        return nodes, lmd

    n1, l1 = postorder(t1)
    n2, l2 = postorder(t2)
    kr1 = sorted({max(i for i in range(len(n1)) if l1[i] == l) for l in set(l1)})
    kr2 = sorted({max(j for j in range(len(n2)) if l2[j] == l) for l in set(l2)})
    td = [[0.0] * len(n2) for _ in range(len(n1))]

    for i in kr1:
        for j in kr2:
            li, lj = l1[i], l2[j]
            m, n = i - li + 2, j - lj + 2
            fd = [[0.0] * n for _ in range(m)]
            for x in range(1, m):
                fd[x][0] = fd[x - 1][0] + 1
            for y in range(1, n):
                fd[0][y] = fd[0][y - 1] + 1
            for x in range(1, m):
                for y in range(1, n):
                    i1, j1 = li + x - 1, lj + y - 1
                    if l1[i1] == li and l2[j1] == lj:
                        fd[x][y] = min(fd[x - 1][y] + 1, fd[x][y - 1] + 1,
                                       fd[x - 1][y - 1] + _ted_label_cost(n1[i1], n2[j1]))
                        td[i1][j1] = fd[x][y]
                    else:
                        fd[x][y] = min(fd[x - 1][y] + 1, fd[x][y - 1] + 1,
                                       fd[l1[i1] - li][l2[j1] - lj] + td[i1][j1])
    return td[-1][-1]


# Public functions
def compare_plans(pg_plan: Any, ai_plan: Any) -> ComparisonResult:
    """Compare two plans (PlanNode or dict). Raises PlanError on bad input."""
    if pg_plan is None or ai_plan is None:
        raise PlanError("Both plans are needed for a comparison.")
    return _Comparer(plan_from_dict(pg_plan), plan_from_dict(ai_plan)).run()


def summarize_rates(results: List[ComparisonResult]) -> Dict[str, Optional[float]]:
    """Average metrics over many queries."""
    def mean(key):
        vals = [r.metrics[key] for r in results if r.metrics.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    return {
        "queries": len(results),
        "mean_difference_rate": mean("difference_rate"),
        "mean_ted_rate": mean("ted_rate"),
        "identical_plans": sum(r.identical for r in results),
        "mean_access_method_agreement": mean("access_method_agreement"),
        "mean_join_algorithm_agreement": mean("join_algorithm_agreement"),
        "mean_join_order_agreement": mean("join_order_agreement"),
        "median_of_median_q_error": (round(statistics.median(
            [r.metrics["median_q_error"] for r in results
             if r.metrics["median_q_error"] is not None]), 2)
            if any(r.metrics["median_q_error"] is not None for r in results) else None),
    }


def print_result(result: ComparisonResult) -> None:
    """Print both trees with statuses, then the explanation."""
    marks = {SAME: " ", CHANGED: "~", ONLY_PG: "-", ONLY_AI: "+"}
    for side, plan, title in ((PG, result.pg_plan, "PostgreSQL plan"),
                              (AI, result.ai_plan, "AI plan")):
        print(f"{title}   (~ changed, - only PostgreSQL, + only AI)")
        for path, node in iter_nodes(plan):
            depth = path.count(".")
            detail = node.relation or node.index_name or ""
            print(f"  {marks[result.status(side, path)]} " + "  " * depth + node.node_type
                  + (f" [{detail}]" if detail else ""))
        print()
    print(result.explanation_text())


# Figure 1 vs Figure 2 of the project description
def demo_plans() -> Tuple[PlanNode, PlanNode]:
    """Figure 1 (PostgreSQL) and Figure 2 (Gemini) plans, built by hand."""
    pg = PlanNode("Aggregate", "Aggregate", extra={"Strategy": "Plain", "Partial Mode": "Finalize"},
                  source=PG, rows=1, children=[
        PlanNode("Gather", "Gather", source=PG, rows=2, children=[
            PlanNode("Aggregate", "Aggregate", source=PG, rows=1,
                     extra={"Strategy": "Plain", "Partial Mode": "Partial"}, children=[
                PlanNode("Hash Join", "Hash Join", join_type="Inner", source=PG, rows=31000,
                         conditions={"Hash Cond": "(lineitem.l_partkey = part.p_partkey)"},
                         children=[
                    PlanNode("Seq Scan", "Seq Scan", relation="lineitem", source=PG, rows=31000,
                             conditions={"Filter": "((l_shipdate >= '1993-11-01'::date) AND "
                                         "(l_shipdate < '1993-12-01 00:00:00'::timestamp "
                                         "without time zone))"}),
                    PlanNode("Hash", "Hash", source=PG, rows=83000, children=[
                        PlanNode("Seq Scan", "Seq Scan", relation="part", source=PG,
                                 rows=83000)])])])])])
    ai = PlanNode("Aggregate", "Projection & Projection Arithmetic", source=AI,
                  extra={"Strategy": "Plain"}, children=[
        PlanNode("Hash Join", "Hash Join (Probe Phase)", join_type="Inner", source=AI,
                 conditions={"Hash Cond": "l_partkey = p_partkey"}, children=[
            PlanNode("Index Scan", "Index Scan / Filtered Table Scan", relation="lineitem",
                     index_name="lineitem_shipdate_idx", source=AI, rows=75000,
                     conditions={"Index Cond": "l_shipdate >= '1993-11-01' AND "
                                 "l_shipdate < '1993-12-01'"},
                     extra={"AI Reason": "If an index on l_shipdate exists, it is used to "
                            "extract only rows where l_shipdate falls in November 1993."}),
            PlanNode("Hash", "Hash Join (Build Phase)", source=AI, children=[
                PlanNode("Seq Scan", "Full Table Scan (FTS)", relation="part", source=AI,
                         rows=200000)])])])
    return pg, ai


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Compare the PostgreSQL and AI plans of a query")
    ap.add_argument("sql", nargs="?", help="SQL query (needs the database and GEMINI_API_KEY)")
    ap.add_argument("--demo", action="store_true",
                    help="compare Figure 1 and Figure 2 of the project description (no database)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    try:
        if args.demo or not args.sql:
            pg_plan, ai_plan = demo_plans()
        else:
            from preprocessing import DBConfig, connect, get_ai_plan, get_postgres_plan, \
                load_env_file
            load_env_file()
            config = DBConfig.from_env()
            if not config.password:
                config.password = getpass.getpass(f"Password for {config.user}: ")
            connection = connect(config)
            pg_plan, _ = get_postgres_plan(connection, args.sql)
            ai_plan, _ = get_ai_plan(connection, args.sql)
            connection.close()
        outcome = compare_plans(pg_plan, ai_plan)
    except PlanError as error:
        print(f"Error: {error}")
        raise SystemExit(1)
    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2))
    else:
        print_result(outcome)
