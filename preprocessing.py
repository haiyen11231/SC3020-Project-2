"""preprocessing.py  (Part 1: PostgreSQL and data preprocessing)

Reads the inputs from PostgreSQL and puts the query plan into ONE standard tree
format (PlanNode), so comparison.py, interface.py and the AI module all share it.

What is in here:
    1. connect()              database connection (settings come from env vars)
    2. PlanNode               the standard plan format used by every module
    3. get_schema_text()      reads tables, columns and indexes from ANY database
    4. get_postgres_plan()    runs EXPLAIN (FORMAT JSON) and parses it into PlanNode

Nothing here depends on the TPC-H schema, so it works for any database.

Errors: every function raises PlanError with a short message that is safe to show
to the user (wrong password, missing database, invalid SQL, and so on).

Quick test from the command line:
    python preprocessing.py "SELECT count(*) FROM lineitem"
"""
import argparse
import getpass
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    args = ap.parse_args()

    config = DBConfig.from_env()
    if not config.password:
        config.password = getpass.getpass(f"Password for {config.user}: ")
    try:
        connection = connect(config)
        plan, _ = get_postgres_plan(connection, args.sql)
        print_tree(plan)
        connection.close()
    except PlanError as error:
        print(f"Error: {error}")
        raise SystemExit(1)
