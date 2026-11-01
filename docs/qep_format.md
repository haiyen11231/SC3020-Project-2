# Common QEP format

Every module (preprocessing, AI parsing, comparison, GUI) uses the same tree format, defined as `PlanNode` in `preprocessing.py`. A query plan is one root node, and each node holds its child nodes. 

## Fields of one node

| Field | Type | Meaning | Example |
|---|---|---|---|
| `node_type` | string | Operator name, always in PostgreSQL's naming | `Seq Scan`, `Hash Join`, `Aggregate` |
| `raw_type` | string | Name exactly as the source wrote it (useful for AI plans) | `Full Table Scan (FTS)` |
| `relation` | string or null | Table scanned, for scan nodes | `lineitem` |
| `index_name` | string or null | Index used, for index scans | `lineitem_pkey` |
| `join_type` | string or null | Join kind, for join nodes | `Inner` |
| `conditions` | object | Filters and join conditions by label | `{"Hash Cond": "(l_partkey = p_partkey)"}` |
| `keys` | list of strings | Group by or sort keys | `["l_returnflag"]` |
| `extra` | object | Other details | `{"Strategy": "Plain"}` |
| `cost` | number or null | Estimated total cost (PostgreSQL only) | `1234.5` |
| `rows` | number or null | Estimated rows, i.e. the cardinality estimate (PostgreSQL's, or the AI's own for AI plans) | `6000` |
| `children` | list of nodes | Inputs of this operator. For a join, the first child is the outer input | |
| `source` | string | `postgres` or `ai` | `postgres` |

Only `node_type` and `children` are always filled. Every other field can be null or empty, so check before using it.



## AI plans

`get_ai_plan()` returns the AI agent's plan in the same `PlanNode` format, with `source = "ai"`. The AI's operator names are mapped to PostgreSQL's (`Full Table Scan` becomes `Seq Scan`, `HashAggregate` becomes `Aggregate` with `Strategy: Hashed`), and the original name stays in `raw_type`. `cost` is always null for AI plans, but `rows` holds the AI's own cardinality estimate when it gives one.

The AI receives the same inputs the optimizer uses: the schema with its indexes, and the statistics T(R), B(R), V(R,A) and the min/max of number and date columns, read from `pg_class` and `pg_stats`.

AI plans can have these extra keys in `extra`:

| Key | Meaning |
|---|---|
| `AI Model` | Root node only: the Gemini model that answered (another model is tried when the default is busy) |
| `AI Reason` | The AI's one-sentence reason for choosing this operator |
| `Warning` | Something wrong with the AI's node: a table or index that does not exist, an unknown operator, a join without 2 inputs |
| `Note` | Set on a `Hash` node that normalization added, because the AI wrote a Hash Join without one (`raw_type` is `(added)`) |
| `Folded` | The AI wrote a separate Projection or Filter step; it was merged into this node, as PostgreSQL does |

## How to use it

```python
from preprocessing import DBConfig, connect, get_postgres_plan, get_schema_text, print_tree

conn = connect(DBConfig.from_env())          # reads PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
plan, raw_json = get_postgres_plan(conn, "SELECT count(*) FROM lineitem")
print_tree(plan)                             # quick text view
as_dict = plan.to_dict()                     # nested dictionary, ready for json.dumps
schema = get_schema_text(conn)               # tables, columns and indexes as text (for AI prompts)

from preprocessing import get_ai_plan
ai_plan, ai_text = get_ai_plan(conn, "SELECT count(*) FROM lineitem")  # needs GEMINI_API_KEY
# ai_text is the AI's raw answer; answers are cached in .ai_cache/, use_cache=False asks again
```

The Gemini API key is read from the `GEMINI_API_KEY` environment variable, or from a `.env` file next to `preprocessing.py` containing `GEMINI_API_KEY=...` (the `.env` file is git-ignored). Get a free key at https://aistudio.google.com/apikey.

## Comparing two plans

`comparison.py` compares the two plans and returns which nodes correspond, a status and colour for every node, the explanations, and the difference rate. See `comparison.md` for the API (node paths, statuses, metrics) and the algorithm.

```python
from comparison import compare_plans
result = compare_plans(pg_plan, ai_plan)
print(result.explanation_text())
```

## Errors

Every function in `preprocessing.py` raises `PlanError` when something goes wrong (wrong password, missing database, invalid or empty SQL). Its message is short and safe to show to the user as it is, so the GUI should catch it and display the text instead of crashing:

```python
from preprocessing import PlanError

try:
    plan, raw_json = get_postgres_plan(conn, sql)
except PlanError as error:
    show_message(str(error))      # for example: PostgreSQL could not plan this query. syntax error at or near "SELEC"
```
