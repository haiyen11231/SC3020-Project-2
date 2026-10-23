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
| `rows` | number or null | Estimated rows (PostgreSQL only) | `6000` |
| `children` | list of nodes | Inputs of this operator. For a join, the first child is the outer input | |
| `source` | string | `postgres` or `ai` | `postgres` |

Only `node_type` and `children` are always filled. Every other field can be null or empty, so check before using it.



## How to use it

```python
from preprocessing import DBConfig, connect, get_postgres_plan, get_schema_text, print_tree

conn = connect(DBConfig.from_env())          # reads PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
plan, raw_json = get_postgres_plan(conn, "SELECT count(*) FROM lineitem")
print_tree(plan)                             # quick text view
as_dict = plan.to_dict()                     # nested dictionary, ready for json.dumps
schema = get_schema_text(conn)               # tables, columns and indexes as text (for AI prompts)
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




