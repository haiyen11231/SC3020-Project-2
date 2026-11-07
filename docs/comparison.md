# QEP comparison (`comparison.py`, Part 3)

`comparison.py` takes the PostgreSQL plan and the AI plan of one query, both in the common `PlanNode` format (see `qep_format.md`). It finds which nodes correspond, explains every difference and similarity in plain English, and calculates a difference rate. It uses no schema or query-specific information, so it works with any database.

## How to use it

```python
from preprocessing import DBConfig, connect, get_postgres_plan, get_ai_plan
from comparison import compare_plans, iter_nodes, STATUS_COLORS, STATUS_LABELS

pg_plan, _ = get_postgres_plan(conn, sql)
ai_plan, _ = get_ai_plan(conn, sql)
result = compare_plans(pg_plan, ai_plan)        # also accepts plan.to_dict() output

result.difference_rate          # 0.0 = identical, 1.0 = nothing in common
result.explanation_text()       # the full explanation as text (results panel)
result.differences              # list of Difference(category, text, pg_path, ai_path)
result.similarities             # list of strings
result.metrics                  # all numbers, see below
result.join_order               # {"postgres": "HJ(lineitem, part)", "ai": "..."}
result.to_dict()                # everything as JSON-ready dicts
```

Try it without a database or API key (it compares Figure 1 and Figure 2 of the project description):

```
python comparison.py --demo
python comparison.py "SELECT ..."      # real query: needs the database and GEMINI_API_KEY
python comparison.py --json "SELECT ..."
```

## For the GUI (interface.py)

Every node is identified by its **path**: the root is `"0"`, its children are `"0.0"`, `"0.1"`, and so on. `iter_nodes(plan)` yields `(path, node)` pairs in this order, and `node_at(plan, path)` returns the node at a path.

```python
for path, node in iter_nodes(result.pg_plan):
    status = result.status("postgres", path)    # "same", "changed", "postgres_only", "ai_only"
    color = result.color("postgres", path)      # STATUS_COLORS[status]
    tip = result.notes("postgres", path)        # explanations for this node (tooltip)
    other = result.partner("postgres", path)    # path of the matching node in the AI plan, or None
```

| Status | Colour | Meaning |
|---|---|---|
| `same` | green `#4CAF50` | matched, same operator and details |
| `changed` | orange `#FFA726` | matched, but the operator or a detail differs |
| `postgres_only` | red `#EF5350` | only in the PostgreSQL plan |
| `ai_only` | blue `#42A5F5` | only in the AI plan |

`STATUS_LABELS` holds legend text for each status. Every `Difference` carries `pg_path` and `ai_path` (`None` for plan-wide findings), so clicking a difference can highlight its nodes. `compare_plans` raises `PlanError` when a plan is missing or malformed, the same as the other modules.

## For the evaluation (difference rate)

`result.metrics` contains:

| Key | Meaning |
|---|---|
| `difference_rate` | main metric, defined below, in [0, 1] |
| `ted_rate` | normalised Zhang-Shasha tree edit distance, an order-sensitive cross-check, in [0, 1] |
| `pg_nodes`, `ai_nodes` | number of nodes in each plan |
| `matched_pairs`, `identical_pairs`, `changed_pairs` | node pairs found, and how many are identical or changed |
| `postgres_only_nodes`, `ai_only_nodes` | nodes without a partner |
| `access_method_agreement` | share of matched table scans that use the same scan operator |
| `join_algorithm_agreement` | share of matched joins that use the same join algorithm |
| `join_order_agreement` | Jaccard overlap of the two sets of intermediate join results (1 = same join order) |
| `median_q_error`, `max_q_error` | cardinality-estimate error, max(a/b, b/a), over nodes where both plans give rows |

Unavailable values are `None`. For example, a plan with no joins has no join agreement. `summarize_rates(list_of_results)` averages all of these over a set of queries.

**Difference rate.** Each node of either plan gets a cost:

- **0**: matched to a node with the same operator and the same details.
- **0.5**: same operator, but some details differ: index, conditions, group/sort keys, aggregation strategy, outer/inner order, or the inputs it works on.
- **1**: different operator, or no partner in the other plan.

A matched pair holds two nodes, so its cost counts twice.

```
difference_rate = sum of node costs / (nodes in PostgreSQL plan + nodes in AI plan)
```

The rate is 0 for identical plans and 1 when nothing corresponds. It gives the same value whichever plan is passed first. Row estimates are not counted, because they are estimates rather than plan decisions; they are reported through the q-error instead.

## The algorithm

Matching nodes by their position in the tree fails as soon as the AI joins tables in another order or swaps the inputs of a join, because every position shifts. Nodes are matched by **the data they produce** instead.

1. **Classify each node.**
   - A *unit* reads a base table (Seq Scan, Index Scan, Bitmap Heap Scan, ...), or is another leaf. The Bitmap Index Scans under a Bitmap Heap Scan belong to that unit.
   - An *anchor* combines two or more inputs (joins, Append, ...).
   - A *chain* node has exactly one input (Hash, Sort, Aggregate, Gather, Limit, ...). It belongs to the unit or anchor directly below it.
   - Subquery plans attached to a node (InitPlan / SubPlan) are not counted as its inputs. PostgreSQL lists them before the real inputs, so they are kept apart; otherwise a join carrying a subquery would take the subquery as its outer input. They are classified and matched like any other subtree.
2. **Match units.** Units are grouped by table. Within a group, the best one-to-one assignment is chosen by a score that rewards the same operator, the same index, the same predicates, the same literal values, and the same already-matched join partners. Tables read once are matched first, so their matches provide the context that separates self-joins (for example `nation n1` / `nation n2`).
3. **Match anchors by signature.** An anchor's signature is the set of matched units below it. Two joins with the same signature produce the same intermediate result, so they correspond even if the algorithm or the input order differs. Joins left over (because the join order differs) are paired only if their inputs overlap by at least 50% (Jaccard), and are reported as "inputs differ". Otherwise they stay unmatched.
4. **Match chains.** Above each matched pair, the two lists of one-input operators are aligned with a weighted LCS dynamic programme that keeps their order. For example, PostgreSQL's `Aggregate (Finalize) - Gather - Aggregate (Partial)` against the AI's single `Aggregate` matches the final aggregates and leaves Gather and the partial aggregate as PostgreSQL-only. The Hash nodes of a hash join whose build side differs are paired as changed.
5. **Compare each pair** on its operator, index, join type, outer/inner order (detects swapped hash build sides), conditions, keys and aggregation strategy.
6. **Plan-wide findings:**
   - tables read by only one plan
   - join order (the sets of intermediate results)
   - parallelism (Gather, partial/final aggregation)
   - subqueries that PostgreSQL runs as a SubPlan or InitPlan but the AI turns into a join
   - conditions that appear in only one plan
   - invalid AI nodes flagged by preprocessing (invented tables or indexes, unknown operators)
   - the largest cardinality disagreement

**Comparing conditions.** PostgreSQL writes `((l_shipdate >= '1993-11-01'::date) AND ...)`, while the AI may write `l.l_shipdate BETWEEN ...`. Before comparing, expressions are normalised:

- casts, table prefixes, brackets and case are removed;
- the expression is split on top-level `AND`, ignoring the `AND` inside `BETWEEN`;
- each predicate is reduced to its columns and comparison direction, so `BETWEEN` equals a `>=` / `<=` pair and `LIKE` equals PostgreSQL's `~~`.

A join condition that PostgreSQL places on the inner index scan of a Nested Loop is counted at the Nested Loop. That way it equals the same condition written on a Hash Join. An `ORDER BY` alias (`revenue`) matches the expression PostgreSQL prints (`sum(...)`).

**Cardinality estimates.** PostgreSQL's row estimates below a Gather are per worker, on the inner side of a Nested Loop they are per loop, and below a Limit they describe a complete run that the Limit cuts short. Those nodes are left out of the q-error, so that per-worker or per-loop numbers are never compared with the AI's totals.

**Cost.** Unit matching tries every assignment within one table, which takes at most 7! orders and in practice only a few. Above 7 reads of one table it switches to a greedy assignment. Chain alignment is O(a·b) per chain, and the tree edit distance is O(n²) for these tree shapes. Two 40-table plans (240 nodes) compare in under 0.1 s.

## Limitations

- **Expression comparison is syntactic.** It does not prove two different-looking predicates equivalent (for example `a < b` against `b > a`). Literal values are ignored when deciding whether two conditions are equal, so a wrong constant in the AI's plan is not reported. Literals are only used to tell apart repeated scans of the same table.
- **Column names don't say which table they belong to.** Without the schema, a join predicate is recognised by its shape (column = column), not by checking which table each column comes from.
- **Pairing after a different join order is a heuristic.** Joins that cover different tables are paired by input overlap, which is a reasonable pairing but not the only possible one.
- **Some PostgreSQL details are not compared:** cost, parallel worker count, scan direction and output columns. The AI does not produce reliable values for these.
