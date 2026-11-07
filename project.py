"""project.py  (Part 5: integration, testing and evaluation)

The main program. It ties the three modules together:

    preprocessing.py   reads the PostgreSQL plan and the AI agent's plan into PlanNode trees
    comparison.py      matches the two trees and explains the differences
    interface.py       the GUI that shows both trees, the colours and the explanations

How to run it (see INSTRUCTIONS.md for the full setup):

    python project.py                      open the GUI (default)
    python project.py --check              check the setup: Python, packages, database, API key
    python project.py --demo               compare Figure 1 and Figure 2 of the project
                                           description (no database or API key needed)
    python project.py --query "SELECT ..." compare one query in the terminal
    python project.py --evaluate           run the evaluation workload and write the
                                           difference-rate tables to results/
    python project.py --rescore results    recompute the evaluation tables from saved plans
                                           (no database or API key needed)

Connection settings come from the .env file next to this file, or from the standard
PostgreSQL variables (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD). They can be
overridden with --host, --port, --db and --user.

Nothing in this file depends on the TPC-H schema: the schema, statistics, table and
index names are always read from the database that is connected.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import getpass
import inspect
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

if sys.version_info < (3, 8):
    sys.exit("This program needs Python 3.8 or newer.")

try:
    import preprocessing as pp
    from preprocessing import AIConfig, DBConfig, PlanError, PlanNode
    import comparison as cmp
    from comparison import ComparisonResult
except ModuleNotFoundError as missing:          # usually psycopg2 is not installed
    sys.exit(f"Missing Python package '{missing.name}'. Install the requirements first:\n"
             f"    pip install -r requirements.txt")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKLOAD = os.path.join(HERE, "queries", "tpch_workload.sql")
DEFAULT_RESULTS = os.path.join(HERE, "results")
RECOMMENDED_PG_MAJOR = 18     # the version the team's plans and evaluation were made with


# ===========================================================================
# 1. Session: the one object the GUI talks to
# ===========================================================================
@dataclass
class Analysis:
    """Everything produced for one SQL query. The GUI only needs this object.

    pg_plan is always filled. If the AI agent could not answer (no API key, quota,
    no internet, unusable answer), ai_plan and comparison are None and ai_error
    holds a short message that can be shown to the user as it is."""
    sql: str
    database: str
    pg_plan: PlanNode
    pg_raw: Any                                   # EXPLAIN (FORMAT JSON) output
    ai_plan: Optional[PlanNode] = None
    ai_raw: Optional[str] = None                  # the AI's answer text
    ai_model: Optional[str] = None                # Gemini model that answered
    ai_error: Optional[str] = None
    comparison: Optional[ComparisonResult] = None
    timings: Dict[str, float] = field(default_factory=dict)   # seconds per stage

    @property
    def complete(self) -> bool:
        """True if both plans were produced and compared."""
        return self.comparison is not None

    def explanation_text(self) -> str:
        """Text for the results panel: the comparison, or why there is none."""
        if self.comparison is not None:
            return self.comparison.explanation_text()
        return ("Only the PostgreSQL plan is shown, because the AI plan could not be "
                f"produced.\n{self.ai_error}")

    def to_dict(self) -> dict:
        """JSON-ready dictionary (used by --json and by the evaluation files)."""
        return {
            "sql": self.sql, "database": self.database,
            "pg_raw": self.pg_raw, "pg_plan": self.pg_plan.to_dict(),
            "ai_raw": self.ai_raw, "ai_model": self.ai_model, "ai_error": self.ai_error,
            "ai_plan": self.ai_plan.to_dict() if self.ai_plan else None,
            "comparison": self.comparison.to_dict() if self.comparison else None,
            "explanation": self.explanation_text(), "timings": self.timings,
        }


class Session:
    """Holds the database connection and runs the whole pipeline for a query.

    Typical use from the GUI (run analyze() in a worker thread, because the AI
    agent takes 10 to 60 seconds; the GUI must stay responsive):

        session = Session()                       # settings from .env / environment
        session.connect()                         # or session.connect(DBConfig(...))
        session.list_databases()                  # for the database selector
        result = session.analyze(sql, on_progress=status_bar.set_text)
        result.pg_plan, result.ai_plan, result.comparison, result.explanation_text()

    Every method raises PlanError with a short, user-readable message on failure.
    """

    def __init__(self, db_config: Optional[DBConfig] = None,
                 ai_config: Optional[AIConfig] = None):
        pp.load_env_file()                        # .env next to preprocessing.py
        self.db_config = db_config or DBConfig.from_env()
        self.ai_config = ai_config or AIConfig.from_env()
        self.conn = None
        self._lock = threading.Lock()             # one query at a time per connection

    # --- connection -----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.conn is not None and not getattr(self.conn, "closed", False)

    def connect(self, db_config: Optional[DBConfig] = None) -> None:
        """(Re)connect, optionally with new settings. Raises PlanError."""
        if db_config is not None:
            self.db_config = db_config
        self.close()
        self.conn = pp.connect(self.db_config)

    def use_database(self, dbname: str) -> None:
        """Switch to another database on the same server (the GUI's schema selector)."""
        cfg = DBConfig(self.db_config.host, self.db_config.port, dbname,
                       self.db_config.user, self.db_config.password)
        self.connect(cfg)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def _require_connection(self):
        if not self.connected:
            raise PlanError("Not connected to a database. Connect first.")
        return self.conn

    def list_databases(self) -> List[str]:
        """Databases on the server that the user can connect to (not templates)."""
        conn = self._require_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT datname FROM pg_database "
                            "WHERE datallowconn AND NOT datistemplate ORDER BY datname")
                return [r[0] for r in cur.fetchall()]
        except Exception as exc:                  # psycopg2.Error
            raise PlanError(f"Could not list the databases. {pp._short(exc)}") from exc

    def schema_text(self) -> str:
        """Tables, columns and indexes of the connected database (for a schema panel)."""
        return pp.get_schema_text(self._require_connection())

    def server_version(self) -> str:
        conn = self._require_connection()
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            return str(cur.fetchone()[0])

    # --- the pipeline ---------------------------------------------------------
    def analyze(self, sql: str, use_cache: bool = True,
                on_progress: Optional[Callable[[str], None]] = None,
                ai_config: Optional[AIConfig] = None) -> Analysis:
        """Run the whole pipeline for one query:
            1. PostgreSQL plan (EXPLAIN, the query itself is never executed)
            2. AI agent plan (Gemini, cached on disk)
            3. comparison of the two plans
        A failure in step 1 raises PlanError (nothing can be shown). A failure in
        step 2 does not: the Analysis then holds the PostgreSQL plan and ai_error."""
        sql = single_statement(sql)
        progress = on_progress or (lambda _msg: None)
        with self._lock:
            conn = self._require_connection()
            timings: Dict[str, float] = {}

            progress("Getting the PostgreSQL plan...")
            start = time.perf_counter()
            pg_plan, pg_raw = pp.get_postgres_plan(conn, sql)
            timings["postgres"] = round(time.perf_counter() - start, 3)
            result = Analysis(sql=sql, database=self.db_config.dbname,
                              pg_plan=pg_plan, pg_raw=pg_raw, timings=timings)

            progress("Asking the AI agent for its plan (this can take up to a minute)...")
            start = time.perf_counter()
            try:
                ai_plan, ai_raw = pp.get_ai_plan(conn, sql, ai_config or self.ai_config,
                                                 use_cache=use_cache)
                result.ai_plan, result.ai_raw = ai_plan, ai_raw
                result.ai_model = ai_plan.extra.get("AI Model")
            except PlanError as exc:
                result.ai_error = str(exc)
            timings["ai"] = round(time.perf_counter() - start, 3)

            if result.ai_plan is not None:
                progress("Comparing the two plans...")
                start = time.perf_counter()
                result.comparison = cmp.compare_plans(pg_plan, result.ai_plan)
                timings["comparison"] = round(time.perf_counter() - start, 3)
            progress("Done.")
            return result


_SQL_TOKEN_RE = re.compile(
    r"--[^\n]*"                    # line comment
    r"|/\*.*?\*/"                  # block comment
    r"|'(?:[^']|'')*'"             # string literal
    r"|\"(?:[^\"]|\"\")*\""        # quoted identifier
    r"|\$([A-Za-z_]\w*|)\$.*?\$\1\$"  # dollar-quoted string
    r"|;", re.S)


def single_statement(sql: str) -> str:
    """Return the one SQL statement in sql, without the trailing semicolon.
    Semicolons inside strings, quoted names and comments are ignored. Raises
    PlanError if there is no statement or more than one (EXPLAIN takes one)."""
    sql = (sql or "").strip()
    statements, start = [], 0
    for match in _SQL_TOKEN_RE.finditer(sql):
        if match.group(0) == ";":
            statements.append(sql[start:match.start()])
            start = match.end()
    statements.append(sql[start:])

    def has_code(text: str) -> bool:
        return bool(re.sub(r"--[^\n]*|/\*.*?\*/", "", text, flags=re.S).strip())

    statements = [s.strip() for s in statements if has_code(s)]
    if not statements:
        raise PlanError("The query is empty. Please enter an SQL query.")
    if len(statements) > 1:
        raise PlanError(f"Please enter one SQL statement at a time "
                        f"(found {len(statements)} statements separated by ';').")
    return statements[0]


# ===========================================================================
# 2. Workload files (queries/*.sql)
# ===========================================================================
@dataclass
class WorkloadQuery:
    qid: str
    category: str
    purpose: str
    sql: str


def read_workload(path: str) -> List[WorkloadQuery]:
    """Read a workload file. Each query is preceded by tag comments:
        -- @id: A1
        -- @category: A. Selection / access path
        -- @purpose: what the query tests
        SELECT ... ;
    A query without tags gets an id Q1, Q2, ... and the category "Uncategorised",
    so a plain file of semicolon-separated queries also works."""
    if not os.path.exists(path):
        raise PlanError(f"Workload file not found: {path}")
    with open(path, encoding="utf-8") as f:
        text = f.read()

    queries: List[WorkloadQuery] = []
    tags: Dict[str, str] = {}
    buffer: List[str] = []
    for line in text.splitlines():
        tag = re.match(r"\s*--\s*@(\w+)\s*:\s*(.*)$", line)
        if tag and not buffer:
            tags[tag.group(1).lower()] = tag.group(2).strip()
            continue
        if not buffer and (not line.strip() or line.strip().startswith("--")):
            continue                               # comments between queries
        buffer.append(line)
        if _ends_statement("\n".join(buffer)):
            sql = single_statement("\n".join(buffer))
            n = len(queries) + 1
            queries.append(WorkloadQuery(qid=tags.get("id", f"Q{n}"),
                                         category=tags.get("category", "Uncategorised"),
                                         purpose=tags.get("purpose", ""), sql=sql))
            tags, buffer = {}, []
    if buffer and "".join(buffer).strip():
        sql = single_statement("\n".join(buffer))
        queries.append(WorkloadQuery(tags.get("id", f"Q{len(queries) + 1}"),
                                     tags.get("category", "Uncategorised"),
                                     tags.get("purpose", ""), sql))
    ids = [q.qid for q in queries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PlanError(f"Duplicate query ids in {path}: {', '.join(duplicates)}")
    return queries


def _ends_statement(text: str) -> bool:
    """True if text ends with a ';' that is real SQL (not inside a string or comment)."""
    last = None
    for match in _SQL_TOKEN_RE.finditer(text):
        last = match
    return last is not None and last.group(0) == ";" and not text[last.end():].strip(" \t\r\n")


# ===========================================================================
# 3. Evaluation: difference rate over a workload
# ===========================================================================
# Metrics copied from ComparisonResult.metrics into the per-query table.
METRIC_COLUMNS = ["difference_rate", "ted_rate", "pg_nodes", "ai_nodes", "identical_pairs",
                  "changed_pairs", "postgres_only_nodes", "ai_only_nodes",
                  "access_method_agreement", "join_algorithm_agreement",
                  "join_order_agreement", "median_q_error", "max_q_error"]


def _ai_warnings(plan: Optional[PlanNode]) -> List[str]:
    """Problems preprocessing flagged in the AI plan (invented tables or indexes,
    unknown operators, joins without two inputs)."""
    if plan is None:
        return []
    return [n.extra["Warning"] for _, n in cmp.iter_nodes(plan) if n.extra.get("Warning")]


def _record(query: WorkloadQuery, analysis: Optional[Analysis], error: Optional[str],
            repeats: Optional[List[Analysis]] = None) -> dict:
    """One row of the evaluation: the query, both plans, the metrics, the findings."""
    rec: Dict[str, Any] = {"id": query.qid, "category": query.category,
                           "purpose": query.purpose, "sql": query.sql, "error": error}
    if analysis is None:
        return rec
    rec.update(analysis.to_dict())
    rec["error"] = error or analysis.ai_error
    warnings = _ai_warnings(analysis.ai_plan)
    rec["ai_warnings"] = warnings
    if analysis.comparison is not None:
        res = analysis.comparison
        rec["metrics"] = {k: res.metrics.get(k) for k in METRIC_COLUMNS}
        rec["difference_categories"] = [d.category for d in res.differences]
    if repeats:
        rec["stability"] = _stability(analysis, repeats)
    return rec


def _stability(first: Analysis, others: List[Analysis]) -> dict:
    """How much the AI's answer changes when the same question is asked again:
    the difference rate between the AI plans of different runs (0 = always the
    same plan), and the spread of the difference rate against PostgreSQL."""
    runs = [a for a in [first] + others if a.ai_plan is not None]
    pairwise = [cmp.compare_plans(runs[i].ai_plan, runs[j].ai_plan).difference_rate
                for i in range(len(runs)) for j in range(i + 1, len(runs))]
    rates = [a.comparison.difference_rate for a in runs if a.comparison is not None]
    return {
        "runs": len(runs),
        "mean_ai_vs_ai_rate": round(statistics.mean(pairwise), 4) if pairwise else None,
        "distinct_ai_answers": len({a.ai_raw for a in runs}),
        "difference_rates": rates,
        "difference_rate_stdev": round(statistics.pstdev(rates), 4) if len(rates) > 1 else None,
    }


def evaluate(session: Session, queries: List[WorkloadQuery], out_dir: str,
             use_cache: bool = True, repeats: int = 1, delay: float = 0.0) -> List[dict]:
    """Run every query through the pipeline and write the evaluation files.
    A query that fails is recorded with its error and the run continues.
    Ctrl+C stops early and still writes the tables for the finished queries."""
    os.makedirs(os.path.join(out_dir, "queries"), exist_ok=True)
    meta = run_metadata(session, use_cache, repeats)
    records: List[dict] = []
    print(f"Evaluating {len(queries)} queries on database '{session.db_config.dbname}' "
          f"(PostgreSQL {meta['postgres_version']}, model {meta['ai_model']}).")
    try:
        for i, query in enumerate(queries, 1):
            print(f"[{i:>2}/{len(queries)}] {query.qid:<5} {query.category} ... ",
                  end="", flush=True)
            analysis, error, extra_runs = None, None, []
            try:
                analysis = session.analyze(query.sql, use_cache=use_cache)
                # Extra runs bypass the cache entirely, so they never overwrite it.
                no_cache = AIConfig(**{**session.ai_config.__dict__, "cache_dir": None})
                for _ in range(repeats - 1):
                    if delay:
                        time.sleep(delay)
                    extra_runs.append(session.analyze(query.sql, use_cache=False,
                                                      ai_config=no_cache))
            except PlanError as exc:
                error = str(exc)
            except KeyboardInterrupt:
                raise
            except Exception as exc:                # never lose the whole run
                error = f"Unexpected error: {type(exc).__name__}: {exc}"
            rec = _record(query, analysis, error, extra_runs or None)
            records.append(rec)
            _save_json(os.path.join(out_dir, "queries", f"{_safe_name(query.qid)}.json"), rec)
            if rec.get("metrics"):
                print(f"difference rate {rec['metrics']['difference_rate']:.2f}"
                      f"  ({rec.get('ai_model')}, AI {rec['timings'].get('ai', 0):.1f}s)")
            else:
                print(f"FAILED: {(rec.get('error') or '').splitlines()[0]}")
            if delay and analysis is not None and analysis.timings.get("ai", 0) > 1:
                time.sleep(delay)                   # only after real (uncached) AI calls
    except KeyboardInterrupt:
        print("\nStopped by the user. Writing the tables for the finished queries.")
    write_reports(records, meta, out_dir)
    return records


def run_metadata(session: Session, use_cache: bool, repeats: int) -> dict:
    """Facts needed to reproduce the run: versions, model and planner settings."""
    conn = session._require_connection()
    settings = {}
    with conn.cursor() as cur:
        for name in ("max_parallel_workers_per_gather", "work_mem", "random_page_cost",
                     "effective_cache_size", "default_statistics_target"):
            cur.execute("SELECT current_setting(%s)", (name,))
            settings[name] = cur.fetchone()[0]
    tables, indexes = pp.get_catalog_names(conn)
    return {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "database": session.db_config.dbname,
        "postgres_version": session.server_version(),
        "planner_settings": settings,
        "ai_model": session.ai_config.model,
        "ai_temperature": session.ai_config.temperature,
        "use_cache": use_cache, "repeats": repeats,
        "tables": sorted(tables), "indexes": sorted(indexes),
        "python": sys.version.split()[0],
    }


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text) or "query"


def _save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)


# --- aggregate tables ------------------------------------------------------
def _mean(values: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def _median(values: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 4) if vals else None


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "n/a"
    if pct:
        return f"{value * 100:.1f}%"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


RATE_BINS = [(0.0, 0.0, "0 (identical)"), (0.0, 0.1, "(0, 0.10]"), (0.1, 0.25, "(0.10, 0.25]"),
             (0.25, 0.5, "(0.25, 0.50]"), (0.5, 1.0, "(0.50, 1.00]")]


def summarize(records: List[dict]) -> dict:
    """Overall, per-category and per-finding statistics of an evaluation run."""
    done = [r for r in records if r.get("metrics")]
    m = lambda key: [r["metrics"].get(key) for r in done]          # noqa: E731
    rates = m("difference_rate")

    def block(rows: List[dict]) -> dict:
        get = lambda key: [r["metrics"].get(key) for r in rows]    # noqa: E731
        return {
            "queries": len(rows),
            "mean_difference_rate": _mean(get("difference_rate")),
            "median_difference_rate": _median(get("difference_rate")),
            "min_difference_rate": min(get("difference_rate")) if rows else None,
            "max_difference_rate": max(get("difference_rate")) if rows else None,
            "mean_ted_rate": _mean(get("ted_rate")),
            "identical_plans": sum(1 for v in get("difference_rate") if v == 0),
            "access_method_agreement": _mean(get("access_method_agreement")),
            "join_algorithm_agreement": _mean(get("join_algorithm_agreement")),
            "join_order_agreement": _mean(get("join_order_agreement")),
            "median_q_error": _median(get("median_q_error")),
        }

    categories: Dict[str, List[dict]] = {}
    for r in done:
        categories.setdefault(r["category"], []).append(r)

    finding_queries: Counter = Counter()
    finding_total: Counter = Counter()
    for r in done:
        cats = r.get("difference_categories", [])
        finding_total.update(cats)
        finding_queries.update(set(cats))

    bins = []
    for low, high, label in RATE_BINS:
        if low == high:
            count = sum(1 for v in rates if v == low)
        else:
            count = sum(1 for v in rates if low < v <= high)
        bins.append((label, count))

    warned = [r for r in done if r.get("ai_warnings")]
    stab = [r["stability"] for r in done if r.get("stability")]
    return {
        "attempted": len(records),
        "compared": len(done),
        "failed": [(r["id"], (r.get("error") or "").splitlines()[0]) for r in records
                   if not r.get("metrics")],
        "overall": block(done),
        "categories": {c: block(rows) for c, rows in sorted(categories.items())},
        "findings": [(cat, finding_queries[cat], finding_total[cat])
                     for cat, _ in finding_queries.most_common()],
        "rate_distribution": bins,
        "ai_plans_with_warnings": len(warned),
        "ai_warnings": [(r["id"], w) for r in warned for w in r["ai_warnings"]],
        "models": dict(Counter(r.get("ai_model") for r in done)),
        "stability": {
            "queries": len(stab),
            "mean_ai_vs_ai_rate": _mean([s["mean_ai_vs_ai_rate"] for s in stab]),
            "queries_with_varying_plans": sum(1 for s in stab
                                              if (s["mean_ai_vs_ai_rate"] or 0) > 0),
            "mean_difference_rate_stdev": _mean([s["difference_rate_stdev"] for s in stab]),
        } if stab else None,
    }


def write_reports(records: List[dict], meta: dict, out_dir: str) -> dict:
    """Write summary.csv (one row per query), summary.json and summary.md."""
    os.makedirs(out_dir, exist_ok=True)
    summary = summarize(records)

    with open(os.path.join(out_dir, "summary.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "category"] + METRIC_COLUMNS +
                        ["ai_warnings", "ai_model", "ai_seconds", "difference_categories",
                         "error"])
        for r in records:
            metrics = r.get("metrics") or {}
            writer.writerow([r["id"], r["category"]] + [metrics.get(k) for k in METRIC_COLUMNS]
                            + [len(r.get("ai_warnings") or []), r.get("ai_model"),
                               (r.get("timings") or {}).get("ai"),
                               "; ".join(sorted(set(r.get("difference_categories") or []))),
                               (r.get("error") or "").splitlines()[0] if r.get("error") else ""])

    _save_json(os.path.join(out_dir, "summary.json"), {"run": meta, "summary": summary})
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(_markdown(summary, meta, records))
    print(f"\nWrote {os.path.join(out_dir, 'summary.md')} (and summary.csv, summary.json, "
          f"queries/*.json).")
    o = summary["overall"]
    print(f"{summary['compared']} of {summary['attempted']} queries compared. Mean difference "
          f"rate {_fmt(o['mean_difference_rate'], True)}, median "
          f"{_fmt(o['median_difference_rate'], True)}, identical plans {o['identical_plans']}.")
    return summary


def _markdown(s: dict, meta: dict, records: List[dict]) -> str:
    """The summary as Markdown tables, ready to paste into the report."""
    o = s["overall"]
    lines = [
        "# Evaluation: PostgreSQL plan vs AI agent plan", "",
        f"- Date: {meta.get('date')}",
        f"- Database: {meta.get('database')} (PostgreSQL {meta.get('postgres_version')})",
        f"- AI model(s) that answered: "
        + ", ".join(f"{k} ({v} queries)" for k, v in s["models"].items()),
        f"- Temperature {meta.get('ai_temperature')}, cache {'on' if meta.get('use_cache') else 'off'}, "
        f"repeats per query {meta.get('repeats')}",
        "- Planner settings: " + ", ".join(f"{k}={v}" for k, v in
                                           (meta.get("planner_settings") or {}).items()),
        f"- Queries compared: {s['compared']} of {s['attempted']}", "",
        "## Overall", "",
        "| Metric | Value |", "|---|---|",
        f"| Mean difference rate | {_fmt(o['mean_difference_rate'], True)} |",
        f"| Median difference rate | {_fmt(o['median_difference_rate'], True)} |",
        f"| Range | {_fmt(o['min_difference_rate'], True)} to {_fmt(o['max_difference_rate'], True)} |",
        f"| Mean tree-edit-distance rate (cross-check) | {_fmt(o['mean_ted_rate'], True)} |",
        f"| Identical plans | {o['identical_plans']} of {o['queries']} |",
        f"| Access method agreement | {_fmt(o['access_method_agreement'], True)} |",
        f"| Join algorithm agreement | {_fmt(o['join_algorithm_agreement'], True)} |",
        f"| Join order agreement | {_fmt(o['join_order_agreement'], True)} |",
        f"| Median q-error of row estimates | {_fmt(o['median_q_error'])} |",
        f"| AI plans with invalid nodes | {s['ai_plans_with_warnings']} of {s['compared']} |",
        "", "## Distribution of the difference rate", "",
        "| Difference rate | Queries |", "|---|---|",
    ]
    lines += [f"| {label} | {count} |" for label, count in s["rate_distribution"]]
    lines += ["", "## By query category", "",
              "| Category | Queries | Mean rate | Min | Max | Identical | Access agr. | "
              "Join alg. agr. | Join order agr. |",
              "|---|---|---|---|---|---|---|---|---|"]
    for cat, b in s["categories"].items():
        lines.append(f"| {cat} | {b['queries']} | {_fmt(b['mean_difference_rate'], True)} | "
                     f"{_fmt(b['min_difference_rate'], True)} | {_fmt(b['max_difference_rate'], True)} | "
                     f"{b['identical_plans']} | {_fmt(b['access_method_agreement'], True)} | "
                     f"{_fmt(b['join_algorithm_agreement'], True)} | "
                     f"{_fmt(b['join_order_agreement'], True)} |")
    lines += ["", "## Kinds of differences found", "",
              "| Kind of difference | Queries where it occurs | Occurrences |", "|---|---|---|"]
    lines += [f"| {cat} | {nq} | {total} |" for cat, nq, total in s["findings"]]
    if s.get("stability"):
        st = s["stability"]
        lines += ["", "## Stability of the AI's answers", "",
                  f"- Queries asked more than once: {st['queries']}",
                  f"- Queries where the AI gave different plans across runs: "
                  f"{st['queries_with_varying_plans']}",
                  f"- Mean difference rate between the AI's own plans: "
                  f"{_fmt(st['mean_ai_vs_ai_rate'], True)}",
                  f"- Mean standard deviation of the difference rate: "
                  f"{_fmt(st['mean_difference_rate_stdev'], True)}"]
    lines += ["", "## Per query", "",
              "| Id | Category | PG nodes | AI nodes | Difference rate | TED rate | Access | "
              "Join alg. | Join order | Median q-error | Main differences |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        mt = r.get("metrics")
        if not mt:
            lines.append(f"| {r['id']} | {r['category']} | | | failed | | | | | | "
                         f"{(r.get('error') or '').splitlines()[0][:80]} |")
            continue
        cats = Counter(r.get("difference_categories") or [])
        lines.append(
            f"| {r['id']} | {r['category']} | {mt['pg_nodes']} | {mt['ai_nodes']} | "
            f"{_fmt(mt['difference_rate'], True)} | {_fmt(mt['ted_rate'], True)} | "
            f"{_fmt(mt['access_method_agreement'], True)} | "
            f"{_fmt(mt['join_algorithm_agreement'], True)} | "
            f"{_fmt(mt['join_order_agreement'], True)} | {_fmt(mt['median_q_error'])} | "
            f"{', '.join(c for c, _ in cats.most_common(3)) or 'none'} |")
    if s["ai_warnings"]:
        lines += ["", "## Invalid nodes in AI plans", ""]
        lines += [f"- {qid}: {w}" for qid, w in s["ai_warnings"]]
    if s["failed"]:
        lines += ["", "## Queries that could not be compared", ""]
        lines += [f"- {qid}: {err}" for qid, err in s["failed"]]
    return "\n".join(lines) + "\n"


def rescore(out_dir: str) -> dict:
    """Recompute every comparison from the plans saved by an earlier --evaluate run.
    Needs neither the database nor the API key, so the tables can be regenerated
    after comparison.py changes, and anyone can check the reported numbers."""
    summary_path = os.path.join(out_dir, "summary.json")
    qdir = os.path.join(out_dir, "queries")
    if not os.path.isdir(qdir):
        raise PlanError(f"No saved evaluation in {out_dir} (expected a 'queries' folder).")
    meta = {}
    if os.path.exists(summary_path):
        with open(summary_path, encoding="utf-8") as f:
            meta = json.load(f).get("run", {})
    tables = set(meta.get("tables") or []) or None
    indexes = set(meta.get("indexes") or []) or None

    records = []
    for name in sorted(os.listdir(qdir), key=_natural_key):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(qdir, name), encoding="utf-8") as f:
            saved = json.load(f)
        query = WorkloadQuery(saved["id"], saved["category"], saved.get("purpose", ""),
                              saved["sql"])
        if not saved.get("pg_raw"):
            records.append(_record(query, None, saved.get("error")))
            continue
        pg_plan = pp.parse_postgres_plan(saved["pg_raw"])
        analysis = Analysis(sql=saved["sql"], database=saved.get("database", ""),
                            pg_plan=pg_plan, pg_raw=saved["pg_raw"],
                            ai_raw=saved.get("ai_raw"), ai_model=saved.get("ai_model"),
                            timings=saved.get("timings") or {})
        if saved.get("ai_raw"):
            try:
                analysis.ai_plan = pp.parse_ai_plan(saved["ai_raw"], tables, indexes)
                analysis.comparison = cmp.compare_plans(pg_plan, analysis.ai_plan)
            except PlanError as exc:
                analysis.ai_error = str(exc)
        else:
            analysis.ai_error = saved.get("ai_error") or saved.get("error")
        rec = _record(query, analysis, None)
        if saved.get("stability"):
            rec["stability"] = saved["stability"]      # needs the extra runs; kept as saved
        records.append(rec)
        _save_json(os.path.join(qdir, name), rec)
    meta["rescored"] = datetime.datetime.now().isoformat(timespec="seconds")
    return write_reports(records, meta, out_dir)


def _natural_key(name: str) -> Tuple:
    """Sort A2 before A10."""
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name))


# ===========================================================================
# 4. Setup check (python project.py --check)
# ===========================================================================
def check_setup(session: Session, try_ai: bool = False) -> bool:
    """Check every requirement and print what to fix. Returns True if all is well."""
    ok = True

    def report(good: bool, text: str, fix: str = "", required: bool = True) -> None:
        """[OK] passed, [!!] must be fixed, [--] works but worth fixing."""
        nonlocal ok
        ok = ok and (good or not required)
        mark = "OK" if good else ("!!" if required else "--")
        print(f"  [{mark}] {text}" + (f"\n         -> {fix}" if fix and not good else ""))

    print("Checking the setup:")
    report(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}",
           "Install Python 3.8 or newer.")
    report(True, "psycopg2 is installed")
    env_path = os.path.join(HERE, ".env")
    report(os.path.exists(env_path), ".env file found" if os.path.exists(env_path)
           else ".env file not found (optional if the settings are in the environment)",
           "Create .env next to project.py, see INSTRUCTIONS.md.", required=False)
    try:
        session.connect()
        report(True, f"Connected to database '{session.db_config.dbname}' on "
                     f"{session.db_config.host}:{session.db_config.port}")
    except PlanError as exc:
        report(False, str(exc), "Check PGPASSWORD / PGDATABASE in .env and that PostgreSQL runs.")
        return False
    version = session.server_version()
    major = int(re.match(r"\d+", version).group())
    report(major >= RECOMMENDED_PG_MAJOR, f"PostgreSQL {version}",
           f"Plans can differ between versions; the team used PostgreSQL {RECOMMENDED_PG_MAJOR}.",
           required=False)
    with session.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        n_tables = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND (c.reltuples < 0 OR NOT EXISTS (SELECT 1 FROM pg_stats s "
                    "WHERE s.schemaname = 'public' AND s.tablename = c.relname))")
        not_analyzed = cur.fetchone()[0]
    report(n_tables > 0, f"{n_tables} tables in schema 'public'",
           "The database is empty. Restore the dump (setup/README.md).")
    report(not_analyzed == 0, "Planner statistics present (ANALYZE has run)"
           if not_analyzed == 0 else f"{not_analyzed} tables have no statistics",
           "Run ANALYZE; in the database (pgAdmin Query Tool or psql).")
    try:
        pg_plan, _ = pp.get_postgres_plan(session.conn, "SELECT 1")
        report(pg_plan.node_type == "Result", "EXPLAIN works")
    except PlanError as exc:
        report(False, f"EXPLAIN failed: {exc}")
    key = session.ai_config.api_key
    report(bool(key), "Gemini API key found" if key else "No Gemini API key",
           "Put GEMINI_API_KEY=... in .env (free key: https://aistudio.google.com/apikey). "
           "Without it only the PostgreSQL plan is shown.", required=False)
    if try_ai and key:
        print("  ... asking Gemini a test question (up to a minute)")
        result = session.analyze("SELECT count(*) FROM " + _smallest_table(session),
                                 use_cache=False)
        report(result.ai_error is None, f"Gemini answered ({result.ai_model}, "
                                        f"{result.timings.get('ai', 0):.0f}s)"
               if result.ai_error is None else f"Gemini failed: {result.ai_error}")
    print("Ready to run (items marked -- are optional)." if ok
          else "Some checks failed; fix the items marked !! (see the hints above).")
    return ok


def _smallest_table(session: Session) -> str:
    with session.conn.cursor() as cur:
        cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.reltuples LIMIT 1")
        row = cur.fetchone()
    if not row:
        raise PlanError("The database has no tables.")
    return '"' + row[0].replace('"', '""') + '"'


# ===========================================================================
# 5. GUI launch and command line
# ===========================================================================
def launch_gui(session: Session) -> int:
    """Open the GUI in interface.py. The GUI receives the Session and does its
    own connecting (so the user can type the password and choose the database)."""
    try:
        import interface
    except ModuleNotFoundError as exc:
        if exc.name != "interface":
            raise
        print("interface.py was not found next to project.py, so the GUI cannot open.\n"
              "Use the terminal version instead, for example:\n"
              '    python project.py --query "SELECT count(*) FROM nation"')
        return 1
    runner = getattr(interface, "run_gui", None) or getattr(interface, "main", None)
    if runner is None:
        print("interface.py has no run_gui(session) function.")
        return 1
    # run_gui(session) is the agreed entry point; a main() without parameters also works.
    if inspect.signature(runner).parameters:
        runner(session)
    else:
        runner()
    return 0


def _db_config_from_args(args: argparse.Namespace) -> DBConfig:
    pp.load_env_file()
    cfg = DBConfig.from_env()
    for attr, value in (("host", args.host), ("port", args.port), ("dbname", args.db),
                        ("user", args.user)):
        if value:
            setattr(cfg, attr, str(value))
    return cfg


def _ask_password(cfg: DBConfig) -> None:
    """Terminal modes only: ask for the password if it is not in .env / PGPASSWORD."""
    if not cfg.password:
        cfg.password = getpass.getpass(f"PostgreSQL password for {cfg.user}: ")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Compare the query plan of PostgreSQL with the plan of an AI agent.",
        epilog="Without a mode option the GUI opens.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check the setup and exit")
    mode.add_argument("--demo", action="store_true",
                      help="compare Figure 1 and Figure 2 of the project description (offline)")
    mode.add_argument("--query", metavar="SQL", help="compare one query in the terminal")
    mode.add_argument("--query-file", metavar="FILE", help="compare the query in a .sql file")
    mode.add_argument("--evaluate", nargs="?", const=DEFAULT_WORKLOAD, metavar="WORKLOAD",
                      help="evaluate a workload file (default: queries/tpch_workload.sql)")
    mode.add_argument("--rescore", nargs="?", const=DEFAULT_RESULTS, metavar="RESULTS",
                      help="recompute the evaluation tables from saved plans (offline)")
    ap.add_argument("--host", help="PostgreSQL host (default: PGHOST or localhost)")
    ap.add_argument("--port", help="PostgreSQL port (default: PGPORT or 5432)")
    ap.add_argument("--db", help="database name (default: PGDATABASE or tpch)")
    ap.add_argument("--user", help="database user (default: PGUSER or postgres)")
    ap.add_argument("--model", help="Gemini model to ask first (default: GEMINI_MODEL or built-in)")
    ap.add_argument("--no-cache", action="store_true", help="ask the AI again, ignore saved answers")
    ap.add_argument("--json", action="store_true", help="--query: print the full result as JSON")
    ap.add_argument("--out", default=DEFAULT_RESULTS, help="--evaluate: output folder")
    ap.add_argument("--only", help="--evaluate: comma-separated query ids or category letters, e.g. A1,B,C3")
    ap.add_argument("--repeats", type=int, default=1,
                    help="--evaluate: ask the AI this many times per query (measures stability)")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="--evaluate: seconds to wait between uncached AI calls (free-tier limits)")
    ap.add_argument("--ai", action="store_true", help="--check: also send one test question to Gemini")
    return ap


def _select(queries: List[WorkloadQuery], only: Optional[str]) -> List[WorkloadQuery]:
    """Keep the queries named by --only (exact ids, or id prefixes such as 'B')."""
    if not only:
        return queries
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    chosen = [q for q in queries if any(q.qid == w or (w.isalpha() and q.qid.startswith(w))
                                        for w in wanted)]
    if not chosen:
        raise PlanError(f"No query matches --only {only}.")
    return chosen


def main(argv: Optional[List[str]] = None) -> int:
    # Windows consoles cannot print every character an AI answer may contain.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        print("--repeats must be at least 1.")
        return 2

    try:
        if args.demo:
            pg_plan, ai_plan = cmp.demo_plans()
            cmp.print_result(cmp.compare_plans(pg_plan, ai_plan))
            return 0
        if args.rescore:
            rescore(args.rescore)
            return 0

        cfg = _db_config_from_args(args)
        ai_cfg = AIConfig.from_env()
        if args.model:
            ai_cfg.model = args.model
        session = Session(cfg, ai_cfg)

        if not (args.check or args.query or args.query_file or args.evaluate):
            return launch_gui(session)                     # the default: open the GUI

        _ask_password(cfg)
        if args.check:
            return 0 if check_setup(session, try_ai=args.ai) else 1

        session.connect()
        if args.evaluate:
            queries = _select(read_workload(args.evaluate), args.only)
            evaluate(session, queries, args.out, use_cache=not args.no_cache,
                     repeats=args.repeats, delay=args.delay)
            return 0

        if args.query_file:
            with open(args.query_file, encoding="utf-8") as f:
                sql = f.read()
        else:
            sql = args.query
        result = session.analyze(sql, use_cache=not args.no_cache,
                                 on_progress=lambda msg: print(msg, file=sys.stderr))
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, default=str))
        elif result.comparison is not None:
            cmp.print_result(result.comparison)
        else:
            print("PostgreSQL plan:")
            pp.print_tree(result.pg_plan)
            print("\n" + result.explanation_text())
        return 0
    except PlanError as error:
        print(f"Error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
