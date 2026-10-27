"""preprocessing.py  (Part 1: PostgreSQL and data preprocessing,
                      Part 2: AI agent and prompt engineering)

Reads the inputs from PostgreSQL and the AI agent and puts both query plans into
ONE standard tree format (PlanNode), so comparison.py and interface.py share it.

What is in here:
    1. connect()              database connection (settings come from env vars)
    2. PlanNode               the standard plan format used by every module
    3. get_schema_text()      reads tables, columns and indexes from ANY database
    4. get_postgres_plan()    runs EXPLAIN (FORMAT JSON) and parses it into PlanNode
    5. get_ai_plan()          asks Google Gemini for the plan and parses it into PlanNode

Nothing here depends on the TPC-H schema, so it works for any database.

Errors: every function raises PlanError with a short message that is safe to show
to the user (wrong password, missing database, invalid SQL, no API key, and so on).

Quick test from the command line:
    python preprocessing.py "SELECT count(*) FROM lineitem"
    python preprocessing.py --ai "SELECT count(*) FROM lineitem"   (needs GEMINI_API_KEY)
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2


class PlanError(Exception):
    """A problem with a short, readable message that the GUI can show as it is."""


def _short(exc: Exception) -> str:
    """First line of a database error, without the traceback noise."""
    diag = getattr(exc, "diag", None)
    text = getattr(diag, "message_primary", None) or str(exc).strip()
    return text.splitlines()[0] if text else "Unknown error."


# ---------------------------------------------------------------------------
# 1. Database connection
# ---------------------------------------------------------------------------
@dataclass
class DBConfig:
    host: str = "localhost"
    port: str = "5432"
    dbname: str = "tpch"
    user: str = "postgres"
    password: str = ""

    @classmethod
    def from_env(cls) -> "DBConfig":
        """Read settings from the standard PostgreSQL environment variables."""
        return cls(
            host=os.environ.get("PGHOST", "localhost"),
            port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ.get("PGDATABASE", "tpch"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
        )


def connect(cfg: DBConfig, readonly: bool = True):
    """Open a connection. Read-only by default, because EXPLAIN never needs to
    write to the database (this also blocks any extra statement hidden in the SQL).
    Raises PlanError with a short message if the connection fails."""
    try:
        conn = psycopg2.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname,
                                user=cfg.user, password=cfg.password)
    except psycopg2.Error as exc:
        raise PlanError(f"Could not connect to database '{cfg.dbname}' on "
                        f"{cfg.host}:{cfg.port} as '{cfg.user}'. {_short(exc)}") from exc
    conn.set_session(readonly=readonly, autocommit=True)
    return conn


# ---------------------------------------------------------------------------
# Common plan format
# ---------------------------------------------------------------------------
@dataclass
class PlanNode:
    """One operator in a query plan, identical for PostgreSQL and AI plans."""
    node_type: str                       # canonical name, e.g. "Seq Scan"
    raw_type: str = ""                   # name exactly as the source wrote it
    relation: Optional[str] = None       # table scanned, if any
    index_name: Optional[str] = None
    join_type: Optional[str] = None      # Inner, Left, ...
    conditions: Dict[str, str] = field(default_factory=dict)  # Filter, Hash Cond, ...
    keys: List[str] = field(default_factory=list)             # group / sort keys
    extra: Dict[str, str] = field(default_factory=dict)       # Strategy, ...
    cost: Optional[float] = None         # PostgreSQL only
    rows: Optional[float] = None         # PostgreSQL only
    children: List["PlanNode"] = field(default_factory=list)
    source: str = ""                     # "postgres" or "ai"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["children"] = [c.to_dict() for c in self.children]
        return d


# ---------------------------------------------------------------------------
# 2. Schema reader (used to give the AI the same facts PostgreSQL has)
# ---------------------------------------------------------------------------
def get_schema_text(conn, schemas=("public",)) -> str:
    """Return tables, columns and index definitions as plain text.
    Raises PlanError if the schema cannot be read."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns WHERE table_schema = ANY(%s) "
                "ORDER BY table_name, ordinal_position", (list(schemas),))
            tables: Dict[str, List[str]] = {}
            for table, col, dtype in cur.fetchall():
                tables.setdefault(table, []).append(f"{col} {dtype}")

            cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = ANY(%s) "
                        "ORDER BY indexdef", (list(schemas),))
            indexes = [r[0] for r in cur.fetchall()]
    except psycopg2.Error as exc:
        raise PlanError(f"Could not read the database schema. {_short(exc)}") from exc

    lines = ["TABLES:"]
    lines += [f"  {t}({', '.join(cols)})" for t, cols in tables.items()]
    lines.append("INDEXES:")
    lines += [f"  {ix}" for ix in indexes]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. PostgreSQL plan
# ---------------------------------------------------------------------------
_COND_KEYS = ["Index Cond", "Recheck Cond", "Filter", "Hash Cond",
              "Merge Cond", "Join Filter"]
_EXTRA_KEYS = ["Strategy", "Partial Mode", "Parent Relationship", "Scan Direction"]


def _pg_node(p: dict) -> PlanNode:
    node = PlanNode(
        node_type=p["Node Type"],
        raw_type=p["Node Type"],
        relation=p.get("Relation Name"),
        index_name=p.get("Index Name"),
        join_type=p.get("Join Type"),
        cost=p.get("Total Cost"),
        rows=p.get("Plan Rows"),
        source="postgres",
    )
    node.conditions = {k: p[k] for k in _COND_KEYS if k in p}
    node.keys = list(p.get("Group Key", [])) + list(p.get("Sort Key", []))
    node.extra = {k: str(p[k]) for k in _EXTRA_KEYS if k in p}
    node.children = [_pg_node(c) for c in p.get("Plans", [])]
    return node


def parse_postgres_plan(explain_json: Any) -> PlanNode:
    """Turn the EXPLAIN (FORMAT JSON) output into a PlanNode tree."""
    try:
        if isinstance(explain_json, str):
            explain_json = json.loads(explain_json)
        top = explain_json[0] if isinstance(explain_json, list) else explain_json
        return _pg_node(top["Plan"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise PlanError("The plan returned by PostgreSQL could not be read.") from exc


def get_postgres_plan(conn, sql: str) -> Tuple[PlanNode, Any]:
    """Run EXPLAIN (not ANALYZE, so the query itself is never executed).
    Raises PlanError with a short message if the query is empty or invalid."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise PlanError("The query is empty. Please enter an SQL query.")
    try:
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (FORMAT JSON) " + sql)
            raw = cur.fetchone()[0]
    except psycopg2.Error as exc:
        raise PlanError(f"PostgreSQL could not plan this query. {_short(exc)}") from exc
    return parse_postgres_plan(raw), raw


# ---------------------------------------------------------------------------
# 4. AI agent plan (Part 2)
# ---------------------------------------------------------------------------
# The AI plan is produced in three steps:
#   a. build_ai_prompt()  gives the LLM the schema, the table sizes and the SQL,
#                         and asks for one JSON tree using PostgreSQL's operator names
#   b. call_gemini()      sends the prompt to Google Gemini (free tier) over HTTPS
#   c. parse_ai_plan()    turns the reply into a PlanNode tree, mapping whatever
#                         operator names the AI used onto PostgreSQL's names
# get_ai_plan() runs all three, and asks the AI once more if the reply is unusable.

_HERE = os.path.dirname(os.path.abspath(__file__))


class AIFormatError(PlanError):
    """The AI answered, but the answer is not a usable plan (bad JSON, no operators)."""


def load_env_file(path: str = os.path.join(_HERE, ".env")) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class AIConfig:
    api_key: str = ""
    model: str = "gemini-3.5-flash"      # free tier model in Google AI Studio
    # Tried in order when the model above is busy, over quota or retired.
    fallback_models: Tuple[str, ...] = ("gemini-3.6-flash", "gemini-3-flash-preview",
                                        "gemini-3.1-flash-lite")
    temperature: float = 0.0             # same query -> (almost) same plan
    timeout: int = 90                    # seconds per request
    cache_dir: Optional[str] = os.path.join(_HERE, ".ai_cache")  # None turns it off

    @classmethod
    def from_env(cls) -> "AIConfig":
        """Read GEMINI_API_KEY and GEMINI_MODEL from the environment or the .env file."""
        load_env_file()
        return cls(
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL", cls.model),
        )


# --- Extra database facts for the prompt -----------------------------------
_RANGE_TYPES = ("smallint", "integer", "bigint", "numeric", "real", "double precision",
                "date", "timestamp without time zone", "timestamp with time zone")


def get_table_stats_text(conn, schemas=("public",)) -> str:
    """The statistics the optimizer uses for cardinality estimation, as text:
    T(R) tuples and B(R) blocks per table, and V(R,A) distinct values plus the
    min / max value (for numbers and dates) per column. They come from pg_class
    and pg_stats (filled by ANALYZE), so the AI gets the same facts as PostgreSQL."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, c.reltuples::bigint, c.relpages "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname = ANY(%s) "
                "ORDER BY c.relname", (list(schemas),))
            tables = cur.fetchall()
            cur.execute(
                "SELECT c.table_name, c.column_name, c.data_type, s.n_distinct, "
                "       (s.histogram_bounds::text::text[])[1], "
                "       (s.histogram_bounds::text::text[])"
                "           [cardinality(s.histogram_bounds::text::text[])] "
                "FROM information_schema.columns c "
                "LEFT JOIN pg_stats s ON s.schemaname = c.table_schema "
                "     AND s.tablename = c.table_name AND s.attname = c.column_name "
                "WHERE c.table_schema = ANY(%s) "
                "ORDER BY c.table_name, c.ordinal_position", (list(schemas),))
            columns = cur.fetchall()
    except psycopg2.Error as exc:
        raise PlanError(f"Could not read the table statistics. {_short(exc)}") from exc

    by_table: Dict[str, List[str]] = {}
    tuples_of = {t: n for t, n, _ in tables}
    for table, col, dtype, n_distinct, low, high in columns:
        if n_distinct is None:
            continue  # no statistics for this column (table not analyzed)
        # A negative n_distinct is a fraction of the row count.
        distinct = n_distinct if n_distinct >= 0 else -n_distinct * max(tuples_of.get(table, 0), 0)
        text = f"{col}: V={int(round(distinct)):,}"
        if dtype in _RANGE_TYPES and low is not None:
            text += f", min={low}, max={high}"
        by_table.setdefault(table, []).append(text)

    lines = ["STATISTICS (T = tuples, B = 8 KB blocks, V = distinct values; "
             "planner estimates):"]
    for table, tuples, pages in tables:
        count = "unknown" if tuples is None or tuples < 0 else f"{tuples:,}"
        lines.append(f"  {table}: T={count}, B={pages:,}")
        lines += [f"    {c}" for c in by_table.get(table, [])]
    return "\n".join(lines)


def get_catalog_names(conn, schemas=("public",)) -> Tuple[Set[str], Set[str]]:
    """Names of all tables (and views) and all indexes, used to spot AI plans that
    mention tables or indexes that do not exist."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = ANY(%s)", (list(schemas),))
            tables = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = ANY(%s)",
                        (list(schemas),))
            indexes = {r[0] for r in cur.fetchall()}
    except psycopg2.Error as exc:
        raise PlanError(f"Could not read the table and index names. {_short(exc)}") from exc
    return tables, indexes


# --- a. Prompt --------------------------------------------------------------
PG_OPERATORS = [
    "Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Bitmap Index Scan",
    "Subquery Scan", "CTE Scan", "Function Scan", "Values Scan",
    "Nested Loop", "Hash Join", "Merge Join", "Hash",
    "Sort", "Incremental Sort", "Aggregate", "WindowAgg", "Unique", "SetOp",
    "Gather", "Gather Merge", "Limit", "Materialize", "Memoize",
    "Append", "Merge Append", "Result",
]

AI_SYSTEM_PROMPT = """You are the cost-based query optimizer of PostgreSQL 18. Given \
a database schema, the optimizer statistics and an SQL query, output the query \
execution plan (QEP) that PostgreSQL 18 with default settings would most likely \
choose. The QEP is a physical query plan: an operator tree in which data flows from \
the leaves to the root, and every node names the algorithm that runs it.

Output format:
- Reply with ONE JSON object only: the root node of the plan tree. No markdown, no text \
before or after it.
- Every node has exactly these fields:
  "node_type":  one operator name from the list below, spelled exactly as listed
  "relation":   table name for scan nodes, otherwise null
  "index_name": index used by an index or bitmap index scan, otherwise null
  "join_type":  "Inner", "Left", "Right", "Full", "Semi" or "Anti" for joins, otherwise null
  "strategy":   "Plain", "Sorted" or "Hashed" for Aggregate and SetOp, otherwise null
  "conditions": object from label to expression; labels are "Filter", "Index Cond", \
"Recheck Cond", "Hash Cond", "Merge Cond" and "Join Filter"; {} if none
  "keys":       list of group by or sort keys, [] if none
  "estimated_rows": estimated number of rows this node outputs (its cardinality)
  "reason":     one short sentence explaining why the optimizer chooses this operator
  "children":   list of input nodes, [] for leaf nodes

Operator names: """ + ", ".join(PG_OPERATORS) + """.

Use PostgreSQL's names for the standard algorithms:
- Access methods: sequential (table) scan is "Seq Scan"; index scan is "Index Scan" \
or "Index Only Scan"; multi-index (bitmap) scan is "Bitmap Heap Scan" over a \
"Bitmap Index Scan".
- Join algorithms: nested-loop and index nested-loop join are "Nested Loop" (for an \
index nested-loop join the inner child is an Index Scan); sort-merge join is \
"Merge Join"; hash join is "Hash Join".
- Grouping and aggregation are "Aggregate" (strategy Hashed or Sorted); duplicate \
elimination (DISTINCT) is "Unique" over a Sort, or a Hashed "Aggregate".
- Selection and projection are applied on the fly inside other operators, never as \
separate nodes: put a selection in the "conditions" of the node that applies it.

PostgreSQL plan conventions:
- For a join, the first child is the outer input and the second child is the inner input.
- A Hash Join's second child is always a Hash node holding the build relation; its \
first child is the probe relation.
- A Bitmap Heap Scan reads the rows found by its Bitmap Index Scan child.
- A parallel plan puts Gather or Gather Merge above the part that runs in parallel workers.
- A Merge Join needs both inputs sorted on the join key.

Use only the tables, columns and indexes in the schema (an index scan is impossible \
without a matching index). Choose the join order, access methods and join algorithms \
from the estimated cardinalities, using the statistics given (T, B and V)."""


def build_ai_prompt(sql: str, schema_text: str, stats_text: str = "") -> str:
    """The user part of the prompt. The fixed instructions are in AI_SYSTEM_PROMPT."""
    parts = ["DATABASE SCHEMA:", schema_text]
    if stats_text:
        parts += ["", stats_text]
    parts += ["", "SQL QUERY:", sql.strip().rstrip(";"), "",
              "Return the JSON plan of this query."]
    return "\n".join(parts)


# --- b. Gemini call ---------------------------------------------------------
_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")


def _gemini_error(exc: urllib.error.HTTPError) -> Tuple[str, Optional[float]]:
    """Error message and suggested retry delay (seconds) from a Gemini error reply."""
    try:
        err = json.loads(exc.read().decode("utf-8")).get("error", {})
    except (ValueError, OSError):
        return f"HTTP {exc.code}", None
    delay = None
    for detail in err.get("details", []):
        match = re.fullmatch(r"([\d.]+)s", str(detail.get("retryDelay", "")))
        if match:
            delay = float(match.group(1))
    return (err.get("message") or f"HTTP {exc.code}").splitlines()[0], delay


class _ModelUnavailable(PlanError):
    """This model is busy, over its quota, retired or too slow; another may work."""


def call_gemini(system: str, prompt: str, cfg: AIConfig) -> Tuple[str, str]:
    """Send one prompt to Gemini and return (answer text, model that answered).
    Free-tier models are often busy, so cfg.model is tried first and then each of
    cfg.fallback_models. Raises PlanError if no model gives an answer."""
    if not cfg.api_key:
        raise PlanError("No Gemini API key. Get a free key at "
                        "https://aistudio.google.com/apikey and set GEMINI_API_KEY "
                        "(or put GEMINI_API_KEY=... in the .env file).")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": cfg.temperature,
                             "responseMimeType": "application/json"},
    }
    failures = []
    for model in [cfg.model] + [m for m in cfg.fallback_models if m != cfg.model]:
        try:
            return _ask_model(model, body, cfg), model
        except _ModelUnavailable as exc:
            failures.append(f"{model}: {exc}")
    raise PlanError("No Gemini model could answer. Try again in a minute.\n"
                    + "\n".join(failures))


def _ask_model(model: str, body: dict, cfg: AIConfig, attempts: int = 2) -> str:
    """One model: retry a short wait, then give up with _ModelUnavailable."""
    request = urllib.request.Request(
        _GEMINI_URL.format(model=model), data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": cfg.api_key},
        method="POST")

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            message, delay = _gemini_error(exc)
            if exc.code in (400, 401, 403) and "key" in message.lower():
                raise PlanError(f"Gemini rejected the API key. ({message})") from exc
            if exc.code in (429, 500, 503, 504) and attempt < attempts \
                    and (delay or 0) <= 30:
                time.sleep(delay or 5)
                continue
            if exc.code in (404, 429, 500, 503, 504):
                raise _ModelUnavailable(f"HTTP {exc.code}. {message}") from exc
            raise PlanError(f"Gemini request failed (HTTP {exc.code}). {message}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
                raise _ModelUnavailable(f"no answer within {cfg.timeout}s") from exc
            raise PlanError("Could not reach Gemini. Check the internet connection. "
                            f"({reason})") from exc
        except ValueError as exc:
            raise PlanError("Gemini sent a reply that is not valid JSON.") from exc

    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason", "no answer")
        raise PlanError(f"Gemini returned no plan ({reason}).")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        reason = candidates[0].get("finishReason", "empty answer")
        raise PlanError(f"Gemini returned no plan ({reason}).")
    return text


# --- c. Parse the AI answer into PlanNode ------------------------------------
_TYPE_RULES = [
    (r"nested.?loop|\bnl\b|loop join|index.?based join|\binlj\b", "Nested Loop"),
    (r"hash.*join|hash (semi|anti)|grace|one.?pass join", "Hash Join"),
    (r"merge.*join|sort.?merge|zig.?zag", "Merge Join"),
    (r"bitmap index", "Bitmap Index Scan"),
    (r"bitmap|multi.?index", "Bitmap Heap Scan"),
    (r"index only", "Index Only Scan"),
    (r"index", "Index Scan"),
    (r"\bseq|sequential|full table|table scan|full scan|\bfts\b", "Seq Scan"),
    (r"subquery", "Subquery Scan"),
    (r"\bcte\b", "CTE Scan"),
    (r"function scan", "Function Scan"),
    (r"values", "Values Scan"),
    (r"gather.?merge", "Gather Merge"),
    (r"gather|exchange", "Gather"),
    (r"merge append", "Merge Append"),
    (r"append|union all", "Append"),
    (r"window", "WindowAgg"),
    (r"agg|group", "Aggregate"),
    (r"incremental sort", "Incremental Sort"),
    (r"sort|order by|top.?n", "Sort"),
    (r"setop|intersect|except", "SetOp"),
    (r"unique|distinct|duplicate", "Unique"),
    (r"limit|fetch first", "Limit"),
    (r"materiali[sz]e", "Materialize"),
    (r"memoize", "Memoize"),
    (r"hash", "Hash"),
    (r"result", "Result"),
]
_JOIN_TYPES = {"Nested Loop", "Hash Join", "Merge Join"}
_SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
               "Bitmap Index Scan"}
# Operators PostgreSQL never shows as separate nodes; with one input they are
# folded into that input instead of becoming an extra node.
_FOLD_RE = re.compile(r"project|compute scalar|^filter$|^selection$|^select$")

_ALIASES = {  # normalized field name -> PlanNode field
    "node type": "node_type", "type": "node_type", "operation": "node_type",
    "operator": "node_type", "op": "node_type",
    "relation": "relation", "relation name": "relation", "table": "relation",
    "table name": "relation",
    "index name": "index_name", "index": "index_name",
    "join type": "join_type", "strategy": "strategy",
    "conditions": "conditions", "condition": "conditions",
    "keys": "keys", "group key": "keys", "sort key": "keys", "group by": "keys",
    "order by": "keys",
    "estimated rows": "rows", "rows": "rows", "plan rows": "rows",
    "cardinality": "rows", "estimated cardinality": "rows",
    "reason": "reason", "explanation": "reason", "description": "reason",
    "children": "children", "plans": "children", "inputs": "children",
    "id": "id", "step": "id", "node id": "id",
}
_COND_LABELS = {k.lower(): k for k in _COND_KEYS}


def _norm_key(key: str) -> str:
    return re.sub(r"[_\-\s]+", " ", str(key)).strip().lower()


def _fields(raw: dict) -> Dict[str, Any]:
    """Map the AI's field names (any spelling, or PostgreSQL's EXPLAIN names)
    onto our field names. Condition labels at node level go into 'conditions'."""
    out: Dict[str, Any] = {"conditions": {}}
    for key, value in raw.items():
        nk = _norm_key(key)
        if nk in _COND_LABELS:
            out["conditions"][_COND_LABELS[nk]] = value
        elif nk in _ALIASES:
            name = _ALIASES[nk]
            if name == "conditions":
                out["conditions"].update(_conditions(value))
            elif name == "keys" and "keys" in out:
                out["keys"] = list(out["keys"]) + list(_as_list(value))
            else:
                out.setdefault(name, value)
    return out


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_number(value: Any) -> Optional[float]:
    """A row estimate such as 6000, "6,000" or "~6000" as a float (None if absent)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or ""))
    return float(match.group().replace(",", "")) if match else None


def _conditions(value: Any) -> Dict[str, str]:
    """Conditions may come as an object, a single string or a list of strings."""
    if not value:
        return {}
    if isinstance(value, dict):
        return {_COND_LABELS.get(_norm_key(k), str(k)): str(v)
                for k, v in value.items() if v not in (None, "")}
    return {"Filter": " AND ".join(str(v) for v in _as_list(value))}


def canonical_node_type(name: str) -> Optional[str]:
    """PostgreSQL's name for an operator name written by the AI (None if unknown)."""
    # A hash join written as two steps, e.g. "Hash Join (Build Phase)" and
    # "Hash Join (Probe Phase)": the build phase is PostgreSQL's Hash node.
    full = _norm_key(name)
    if re.search(r"\bbuild", full):
        return "Hash"
    if re.search(r"\bprobe", full):
        return "Hash Join"
    text = _norm_key(re.sub(r"\(.*?\)", " ", name))
    for pattern, pg_name in _TYPE_RULES:
        if re.search(pattern, text):
            return pg_name
    return None


def _match_name(name: str, known: Set[str]) -> Optional[str]:
    """Return the catalog spelling of name (case-insensitive), or None."""
    lower = name.lower()
    return next((k for k in known if k.lower() == lower), None)


def _clean_relation(value: Any) -> Optional[str]:
    """'public.lineitem', 'lineitem l' and 'lineitem (filtered)' all become 'lineitem'."""
    if not value or not isinstance(value, str):
        return None
    text = re.sub(r"\(.*?\)", " ", value).strip()
    if not text:
        return None
    return text.split()[0].split(".")[-1].strip('"`')


class _AIPlanBuilder:
    """Turns the decoded JSON of an AI answer into a PlanNode tree."""

    MAX_NODES = 500  # guards against runaway or cyclic answers

    def __init__(self, tables: Optional[Set[str]], indexes: Optional[Set[str]]):
        self.tables = tables or set()
        self.indexes = indexes or set()
        self.count = 0

    # Accept a tree, PostgreSQL's own EXPLAIN shape, or a flat list of steps.
    def build(self, data: Any) -> PlanNode:
        data = self._unwrap(data)
        if isinstance(data, list):
            data = self._tree_from_steps(data)
        if not isinstance(data, dict):
            raise AIFormatError("The AI answer is not a JSON object describing a plan.")
        return self._node(data, depth=0)

    @staticmethod
    def _unwrap(data: Any) -> Any:
        """Remove wrappers such as {"plan": {...}} or [{"Plan": {...}}]."""
        for _ in range(5):
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            elif isinstance(data, dict) and len(data) <= 2:
                inner = next((v for k, v in data.items()
                              if _norm_key(k) in ("plan", "query plan", "qep", "root",
                                                  "execution plan", "steps", "nodes")
                              and isinstance(v, (dict, list))), None)
                if inner is None:
                    break
                data = inner
            else:
                break
        return data

    def _tree_from_steps(self, steps: List[Any]) -> dict:
        """A flat list of steps whose 'children' hold step ids instead of nodes."""
        by_id: Dict[str, dict] = {}
        for step in steps:
            if not isinstance(step, dict):
                raise AIFormatError("The AI plan lists steps that are not JSON objects.")
            sid = _fields(step).get("id")
            if sid is None:
                raise AIFormatError("The AI plan is a list of steps without step ids, "
                                    "so the tree shape is unknown.")
            by_id[str(sid)] = step
        referenced = {str(c) for s in by_id.values()
                      for c in _as_list(_fields(s).get("children"))
                      if not isinstance(c, dict)}
        roots = [sid for sid in by_id if sid not in referenced]
        if not roots:
            raise AIFormatError("The AI plan steps form a cycle; no root step found.")

        def expand(sid: str, path: Set[str]) -> dict:
            if sid in path:
                raise AIFormatError("The AI plan steps form a cycle.")
            step = dict(by_id[sid])
            child_key = next((k for k in step if _ALIASES.get(_norm_key(k)) == "children"),
                             None)
            if child_key:
                step[child_key] = [c if isinstance(c, dict) else expand(str(c), path | {sid})
                                   for c in _as_list(step[child_key]) if str(c) in by_id
                                   or isinstance(c, dict)]
            return step

        return expand(roots[-1], set())  # the last unreferenced step is the final one

    def _node(self, raw: Any, depth: int) -> PlanNode:
        self.count += 1
        if self.count > self.MAX_NODES or depth > 100:
            raise AIFormatError("The AI plan is too large or nested too deeply.")
        if not isinstance(raw, dict):
            raise AIFormatError("A node of the AI plan is not a JSON object.")
        f = _fields(raw)
        raw_type = str(f.get("node_type") or "").strip()
        if not raw_type:
            raise AIFormatError("A node of the AI plan has no operator name (node_type).")
        children = [self._node(c, depth + 1) for c in _as_list(f.get("children"))]

        # Projection / filter steps are part of other nodes in PostgreSQL.
        if len(children) == 1 and _FOLD_RE.search(_norm_key(raw_type)):
            child = children[0]
            for label, expr in f["conditions"].items():
                child.conditions.setdefault(label, expr)
            child.extra.setdefault("Folded", raw_type)
            return child

        pg_type = canonical_node_type(raw_type)
        node = PlanNode(node_type=pg_type or raw_type, raw_type=raw_type, source="ai",
                        conditions=f["conditions"], children=children)
        node.keys = [str(k) for k in _as_list(f.get("keys"))]
        node.rows = _as_number(f.get("rows"))
        warnings: List[str] = []
        if pg_type is None:
            warnings.append(f"'{raw_type}' is not a PostgreSQL operator")

        self._set_relation(node, f, warnings)
        self._set_join_and_strategy(node, f, raw_type, warnings)
        if f.get("reason"):
            node.extra["AI Reason"] = str(f["reason"])
        if warnings:
            node.extra["Warning"] = "; ".join(warnings)
        return node

    def _set_relation(self, node: PlanNode, f: Dict[str, Any], warnings: List[str]):
        relation = _clean_relation(f.get("relation"))
        if relation and self.tables:
            known = _match_name(relation, self.tables)
            if known is None:
                warnings.append(f"table '{relation}' does not exist in the database")
            relation = known or relation
        node.relation = relation
        if node.node_type in _SCAN_TYPES - {"Bitmap Index Scan"} and not relation:
            warnings.append("scan without a table name")

        index = f.get("index_name")
        index = str(index).strip() if index else None
        if not index and node.node_type in ("Index Scan", "Index Only Scan",
                                            "Bitmap Index Scan"):
            warnings.append("index scan without an index name")
        if index and self.indexes:
            known = _match_name(index, self.indexes)
            if known is None:
                warnings.append(f"index '{index}' does not exist in the database")
            index = known or index
        node.index_name = index

    @staticmethod
    def _set_join_and_strategy(node: PlanNode, f: Dict[str, Any], raw_type: str,
                               warnings: List[str]):
        name = _norm_key(raw_type)
        if node.node_type in _JOIN_TYPES:
            join_type = f.get("join_type")
            if not join_type:
                found = re.search(r"\b(left|right|full|semi|anti)\b", name)
                join_type = found.group(1) if found else "Inner"
            node.join_type = str(join_type).strip().title()
            if len(node.children) != 2:
                warnings.append(f"a join needs 2 inputs, the AI gave {len(node.children)}")
            elif node.node_type == "Hash Join" and node.children[1].node_type != "Hash":
                # PostgreSQL always builds the hash table in a Hash node; add it so
                # the two plans differ only in real decisions, not in notation.
                node.children[1] = PlanNode(
                    node_type="Hash", raw_type="(added)", source="ai",
                    extra={"Note": "Hash node added during normalisation"},
                    children=[node.children[1]])

        if node.node_type in ("Aggregate", "SetOp"):
            strategy = f.get("strategy")
            if not strategy:
                if "hash" in name:
                    strategy = "Hashed"
                elif re.search(r"group|sort|stream", name):
                    strategy = "Sorted"
                elif node.node_type == "Aggregate" and not node.keys:
                    strategy = "Plain"
            if strategy:
                node.extra["Strategy"] = str(strategy).strip().title()


def _extract_json(text: str) -> Any:
    """Decode the JSON in an AI answer, ignoring markdown fences and stray text."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise AIFormatError("The AI answer contains no JSON.")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[min(starts):])
    except ValueError as exc:
        raise AIFormatError(f"The AI answer is not valid JSON ({exc}).") from exc
    return data


def parse_ai_plan(text: str, tables: Optional[Set[str]] = None,
                  indexes: Optional[Set[str]] = None) -> PlanNode:
    """Turn the AI's answer into a PlanNode tree in PostgreSQL's vocabulary.
    tables / indexes (optional) are the real names, used to flag invented ones.
    Raises AIFormatError if the answer cannot be read as a plan."""
    if not text or not text.strip():
        raise AIFormatError("The AI answer is empty.")
    return _AIPlanBuilder(tables, indexes).build(_extract_json(text))


# --- Run everything ---------------------------------------------------------
def _cache_path(cfg: AIConfig, system: str, prompt: str) -> Optional[str]:
    if not cfg.cache_dir:
        return None
    key = hashlib.sha256("\n".join([cfg.model, str(cfg.temperature), system, prompt])
                         .encode("utf-8")).hexdigest()
    return os.path.join(cfg.cache_dir, key[:32] + ".json")


def get_ai_plan(conn, sql: str, cfg: Optional[AIConfig] = None,
                use_cache: bool = True) -> Tuple[PlanNode, str]:
    """Ask the AI agent for the plan of sql and return (PlanNode tree, raw answer).
    The schema, statistics and index names are read from conn, so this works for
    any database. If the answer is unusable the AI is asked once more to fix it.
    The model that answered is stored in the root node's extra["AI Model"].
    Answers are cached on disk so repeated runs (and evaluation) cost no quota;
    pass use_cache=False to ask again.
    Raises PlanError with a short message on any failure."""
    cfg = cfg or AIConfig.from_env()
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise PlanError("The query is empty. Please enter an SQL query.")

    tables, indexes = get_catalog_names(conn)
    prompt = build_ai_prompt(sql, get_schema_text(conn), get_table_stats_text(conn))
    cache = _cache_path(cfg, AI_SYSTEM_PROMPT, prompt)

    if use_cache and cache and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                saved = json.load(f)
            plan = parse_ai_plan(saved["text"], tables, indexes)
            plan.extra["AI Model"] = saved["model"]
            return plan, saved["text"]
        except (ValueError, KeyError, TypeError, AIFormatError):
            pass  # broken cache entry: ask the AI again

    text, model = call_gemini(AI_SYSTEM_PROMPT, prompt, cfg)
    try:
        plan = parse_ai_plan(text, tables, indexes)
    except AIFormatError as first_error:
        repair = (prompt + "\n\nYour previous answer could not be used: "
                  f"{first_error}\nPrevious answer:\n{text[:4000]}\n\n"
                  "Return only the corrected JSON plan in the required format.")
        text, model = call_gemini(AI_SYSTEM_PROMPT, repair, cfg)
        try:
            plan = parse_ai_plan(text, tables, indexes)
        except AIFormatError as exc:
            raise PlanError(f"The AI did not return a usable plan. {exc}") from exc
    plan.extra["AI Model"] = model

    if cache:
        os.makedirs(cfg.cache_dir, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"model": model, "text": text}, f, indent=1)
    return plan, text


# ---------------------------------------------------------------------------
# Helpers and command line
# ---------------------------------------------------------------------------
def print_tree(node: PlanNode, indent: int = 0) -> None:
    """Print a plan tree as indented text (handy for quick checks)."""
    detail = node.relation or node.index_name or ""
    print("  " * indent + node.node_type + (f" [{detail}]" if detail else ""))
    for child in node.children:
        print_tree(child, indent + 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Print the PostgreSQL plan tree of a query")
    ap.add_argument("sql", help="SQL query text")
    ap.add_argument("--ai", action="store_true", help="also print the AI agent's plan")
    ap.add_argument("--no-cache", action="store_true", help="ask the AI again, ignore cache")
    args = ap.parse_args()

    load_env_file()
    config = DBConfig.from_env()
    if not config.password:
        config.password = getpass.getpass(f"Password for {config.user}: ")
    try:
        connection = connect(config)
        plan, _ = get_postgres_plan(connection, args.sql)
        print("PostgreSQL plan:")
        print_tree(plan)
        if args.ai:
            ai_plan, _ = get_ai_plan(connection, args.sql, use_cache=not args.no_cache)
            print("\nAI plan:")
            print_tree(ai_plan)
        connection.close()
    except PlanError as error:
        print(f"Error: {error}")
        raise SystemExit(1)
