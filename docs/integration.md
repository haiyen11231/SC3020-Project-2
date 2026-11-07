# Integration: how interface.py plugs into project.py

`python project.py` (no options) imports `interface.py` and calls:

```python
def run_gui(session) -> None:     # in interface.py; blocks until the window is closed
```

`session` is a `project.Session`. It already holds the settings from `.env` and does
all the work; the GUI only collects input and draws results. Everything below is
available on that object.

**Do not `import project` in interface.py.** `project.py` runs as `__main__`, so a second
import would create a second copy of its classes. Use the `session` you are given, and
import `preprocessing` and `comparison` directly for helpers and constants.

## 1. Connecting and choosing the database

```python
from preprocessing import DBConfig, PlanError

cfg = session.db_config            # prefilled from .env: host, port, dbname, user, password
session.connect(DBConfig(host, port, dbname, user, password))   # raises PlanError
session.connected                  # True / False
session.list_databases()           # ["imdb", "postgres", "tpch"] for a drop-down
session.use_database("imdb")       # switch database (same server and login)
session.schema_text()              # tables, columns and indexes as text (optional panel)
```

Show the connection form prefilled from `session.db_config`. If `cfg.password` is empty,
ask for it in the form.

## 2. Running a query

```python
result = session.analyze(sql, use_cache=True, on_progress=callback)   # raises PlanError
```

- **Run it in a worker thread.** The AI agent takes 10 to 60 seconds. With Tkinter, start
  a `threading.Thread`, put the result (or the PlanError) on a `queue.Queue`, and poll the
  queue with `root.after(100, ...)`. Never touch widgets from the worker thread.
- `on_progress(message)` is called with short status texts ("Asking the AI agent..."). It
  runs in the worker thread, so pass the text through the queue as well.
- `use_cache=False` asks Gemini again (a "Refresh AI plan" checkbox).
- `PlanError` means nothing can be shown (empty or invalid SQL, two statements, lost
  connection). Show `str(error)` in a message box; it is written for the user.

## 3. What `result` (a `project.Analysis`) holds

| Attribute | Meaning |
|---|---|
| `result.pg_plan` | PostgreSQL plan, a `PlanNode` tree. Always present. |
| `result.ai_plan` | AI plan, a `PlanNode` tree, or `None` if the AI failed. |
| `result.ai_error` | Why the AI plan is missing (no key, quota, no internet...), or `None`. |
| `result.comparison` | `comparison.ComparisonResult`, or `None` when there is no AI plan. |
| `result.complete` | `True` when both plans were compared. |
| `result.explanation_text()` | Text for the results panel (the comparison, or the AI error). |
| `result.ai_model` | Gemini model that answered (show it under the AI tree). |
| `result.timings` | Seconds per stage: `postgres`, `ai`, `comparison`. |

When `result.comparison` is `None`, draw the PostgreSQL tree without colours and show
`result.explanation_text()`. The program must still be useful without an API key.

## 4. Drawing the two trees with colours

```python
from comparison import iter_nodes, STATUS_COLORS, STATUS_LABELS

res = result.comparison
for side, plan in (("postgres", result.pg_plan), ("ai", result.ai_plan)):
    for path, node in iter_nodes(plan):        # path "0", "0.0", "0.1", ... (pre-order)
        depth = path.count(".")
        label = node.node_type + (f"\n{node.relation}" if node.relation else "")
        colour = res.color(side, path)          # green / orange / red / blue
        tooltip = "\n".join(res.notes(side, path))    # why this node differs
        partner = res.partner(side, path)       # path of the matching node, or None
```

- Legend: one entry per `STATUS_LABELS` item with its `STATUS_COLORS` colour.
- Useful node details for a tooltip or side panel: `node.index_name`, `node.join_type`,
  `node.conditions` (dict), `node.keys`, `node.rows` (estimated rows), `node.cost`
  (PostgreSQL only), `node.extra.get("AI Reason")`, `node.extra.get("Warning")` (invalid AI node).
- Clicking a node can highlight `partner` in the other tree.
- `res.differences` is a list of `Difference(category, text, pg_path, ai_path)`; clicking
  one can highlight `pg_path` / `ai_path` (either may be `None`).
- `res.difference_rate`, `res.metrics` and `res.join_order` can go in a summary line.

## 5. What the project description asks the GUI to do

- choose the database schema (section 1)
- enter the SQL query in a Query panel and run it (section 2)
- show both QEPs as trees (section 4)
- highlight the corresponding changes with colour-coded nodes (section 4)
- show the textual explanation of the differences in a results panel (`result.explanation_text()`)

## 6. Testing the GUI without Gemini

`python project.py --demo` shows the expected output for Figure 1 vs Figure 2 in the
terminal. To get the same objects in the GUI without a database, use:

```python
from comparison import compare_plans, demo_plans
pg, ai = demo_plans()
res = compare_plans(pg, ai)
```
