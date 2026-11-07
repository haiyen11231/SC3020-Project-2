# Evaluation: PostgreSQL plan vs AI agent plan

- Date: 2026-11-07T21:36:53
- Database: tpch (PostgreSQL 18.6 (Homebrew))
- AI model(s) that answered: gemini-3.5-flash (5 queries), gemini-3-flash-preview (1 queries), gemini-3.6-flash (1 queries), gemini-3.1-flash-lite (44 queries)
- Temperature 0.0, cache on, repeats per query 3
- Planner settings: max_parallel_workers_per_gather=2, work_mem=4MB, random_page_cost=4, effective_cache_size=4GB, default_statistics_target=100
- Queries compared: 51 of 51

## Overall

| Metric | Value |
|---|---|
| Mean difference rate | 33.5% |
| Median difference rate | 33.3% |
| Range | 0.0% to 88.9% |
| Mean tree-edit-distance rate (cross-check) | 27.6% |
| Identical plans | 11 of 51 |
| Access method agreement | 82.6% |
| Join algorithm agreement | 75.9% |
| Join order agreement | 84.4% |
| Median q-error of row estimates | 1.00 |
| AI plans with invalid nodes | 2 of 51 |

## Distribution of the difference rate

| Difference rate | Queries |
|---|---|
| 0 (identical) | 11 |
| (0, 0.10] | 0 |
| (0.10, 0.25] | 6 |
| (0.25, 0.50] | 24 |
| (0.50, 1.00] | 10 |

## By query category

| Category | Queries | Mean rate | Min | Max | Identical | Access agr. | Join alg. agr. | Join order agr. |
|---|---|---|---|---|---|---|---|---|
| A. Selection / access path | 8 | 12.5% | 0.0% | 33.3% | 5 | 100.0% | n/a | n/a |
| B. Two-table join | 6 | 32.6% | 0.0% | 88.9% | 2 | 75.0% | 83.3% | 100.0% |
| C. Multi-way join | 6 | 31.8% | 0.0% | 79.3% | 1 | 80.3% | 74.5% | 79.4% |
| D. Aggregation | 6 | 38.8% | 11.8% | 57.1% | 0 | 83.3% | 100.0% | 100.0% |
| E. Sorting / Top-N | 3 | 9.5% | 0.0% | 14.3% | 1 | 100.0% | n/a | n/a |
| F. Subquery | 8 | 54.5% | 35.7% | 77.8% | 0 | 75.0% | 50.0% | 66.7% |
| G. CTE | 2 | 40.6% | 33.3% | 47.8% | 0 | 100.0% | 100.0% | 66.7% |
| H. Set operation | 3 | 40.7% | 33.3% | 55.6% | 0 | 100.0% | n/a | n/a |
| I. Window function | 2 | 28.6% | 14.3% | 42.9% | 0 | 50.0% | n/a | n/a |
| J. Outer join | 3 | 37.6% | 33.3% | 46.2% | 0 | 100.0% | 100.0% | 100.0% |
| K. Miscellaneous | 4 | 37.3% | 0.0% | 77.8% | 2 | 33.3% | 0.0% | 100.0% |

## Kinds of differences found

| Kind of difference | Queries where it occurs | Occurrences |
|---|---|---|
| Missing operator | 32 | 75 |
| Parallelism | 31 | 31 |
| Extra operator | 18 | 34 |
| Aggregation strategy | 15 | 15 |
| Conditions | 14 | 17 |
| Access method | 14 | 17 |
| Join input order | 12 | 12 |
| Join algorithm | 7 | 8 |
| Cardinality estimate | 7 | 7 |
| Join inputs | 6 | 8 |
| Subquery | 5 | 6 |
| Join type | 3 | 3 |
| Join order | 2 | 2 |
| Invalid AI node | 2 | 2 |
| Tables | 2 | 3 |
| Duplicate elimination | 1 | 1 |
| Operator | 1 | 1 |
| Keys | 1 | 1 |

## Stability of the AI's answers

- Queries asked more than once: 51
- Queries where the AI gave different plans across runs: 2
- Mean difference rate between the AI's own plans: 1.7%
- Mean standard deviation of the difference rate: 1.2%

## Per query

| Id | Category | PG nodes | AI nodes | Difference rate | TED rate | Access | Join alg. | Join order | Median q-error | Main differences |
|---|---|---|---|---|---|---|---|---|---|---|
| A1 | A. Selection / access path | 1 | 1 | 0.0% | 0.0% | 100.0% | n/a | n/a | 1.00 | none |
| A2 | A. Selection / access path | 1 | 1 | 0.0% | 0.0% | 100.0% | n/a | n/a | 1.00 | none |
| A3 | A. Selection / access path | 4 | 4 | 0.0% | 0.0% | 100.0% | n/a | n/a | 1.25 | none |
| A4 | A. Selection / access path | 2 | 1 | 33.3% | 33.3% | 100.0% | n/a | n/a | n/a | Parallelism, Missing operator |
| A5 | A. Selection / access path | 2 | 2 | 0.0% | 0.0% | 100.0% | n/a | n/a | 1.07 | none |
| A6 | A. Selection / access path | 2 | 1 | 33.3% | 33.3% | 100.0% | n/a | n/a | n/a | Parallelism, Missing operator |
| A7 | A. Selection / access path | 1 | 1 | 0.0% | 0.0% | 100.0% | n/a | n/a | 3.85 | none |
| A8 | A. Selection / access path | 4 | 2 | 33.3% | 33.3% | 100.0% | n/a | n/a | 1.00 | Missing operator, Parallelism |
| B1 | B. Two-table join | 3 | 3 | 0.0% | 0.0% | 100.0% | 100.0% | 100.0% | 3.75 | none |
| B2 | B. Two-table join | 5 | 3 | 62.5% | 50.0% | 50.0% | 100.0% | 100.0% | n/a | Missing operator, Parallelism, Join input order |
| B3 | B. Two-table join | 5 | 4 | 11.1% | 11.1% | 100.0% | 100.0% | 100.0% | n/a | Parallelism, Missing operator |
| B4 | B. Two-table join | 7 | 5 | 33.3% | 33.3% | 100.0% | 100.0% | 100.0% | 1.00 | Missing operator, Parallelism, Join input order |
| B5 | B. Two-table join | 4 | 4 | 0.0% | 0.0% | 100.0% | 100.0% | 100.0% | 1.00 | none |
| B6 | B. Two-table join | 4 | 5 | 88.9% | 44.4% | 0.0% | 0.0% | 100.0% | 1.01 | Access method, Aggregation strategy, Join algorithm |
| C1 | C. Multi-way join | 7 | 7 | 0.0% | 0.0% | 100.0% | 100.0% | 100.0% | 1.00 | none |
| C2 | C. Multi-way join | 15 | 12 | 14.8% | 11.1% | 100.0% | 100.0% | 100.0% | 1.00 | Missing operator, Parallelism, Aggregation strategy |
| C3 | C. Multi-way join | 14 | 15 | 79.3% | 65.5% | 40.0% | 0.0% | 33.3% | 1.00 | Extra operator, Missing operator, Access method |
| C4 | C. Multi-way join | 19 | 17 | 27.8% | 19.4% | 83.3% | 80.0% | 100.0% | 5.00 | Missing operator, Parallelism, Aggregation strategy |
| C5 | C. Multi-way join | 13 | 11 | 37.5% | 25.0% | 75.0% | 66.7% | 100.0% | 1.00 | Missing operator, Parallelism, Aggregation strategy |
| C6 | C. Multi-way join | 18 | 17 | 31.4% | 22.9% | 83.3% | 100.0% | 42.9% | 1.00 | Missing operator, Extra operator, Join order |
| D1 | D. Aggregation | 5 | 2 | 57.1% | 42.9% | 100.0% | n/a | n/a | 3.50 | Missing operator, Parallelism, Aggregation strategy |
| D2 | D. Aggregation | 4 | 4 | 37.5% | 12.5% | 0.0% | n/a | n/a | 1.00 | Aggregation strategy, Access method |
| D3 | D. Aggregation | 4 | 2 | 50.0% | 33.3% | 100.0% | n/a | n/a | 1.00 | Missing operator, Parallelism, Aggregation strategy |
| D4 | D. Aggregation | 5 | 2 | 42.9% | 42.9% | 100.0% | n/a | n/a | n/a | Missing operator, Parallelism |
| D5 | D. Aggregation | 8 | 9 | 11.8% | 5.9% | 100.0% | 100.0% | 100.0% | 1.00 | Aggregation strategy, Extra operator |
| D6 | D. Aggregation | 4 | 2 | 33.3% | 33.3% | 100.0% | n/a | n/a | 1.00 | Missing operator, Parallelism |
| E1 | E. Sorting / Top-N | 4 | 3 | 14.3% | 14.3% | 100.0% | n/a | n/a | 1.00 | Parallelism, Missing operator |
| E2 | E. Sorting / Top-N | 2 | 2 | 0.0% | 0.0% | 100.0% | n/a | n/a | 1.00 | none |
| E3 | E. Sorting / Top-N | 4 | 3 | 14.3% | 14.3% | 100.0% | n/a | n/a | 1.00 | Parallelism, Missing operator |
| F1 | F. Subquery | 5 | 4 | 77.8% | 66.7% | 50.0% | 0.0% | 100.0% | 1000.00 | Missing operator, Parallelism, Cardinality estimate |
| F2 | F. Subquery | 4 | 6 | 60.0% | 60.0% | 50.0% | n/a | 0.0% | 1.50 | Extra operator, Subquery, Parallelism |
| F3 | F. Subquery | 7 | 5 | 75.0% | 50.0% | 50.0% | 0.0% | 100.0% | 1.00 | Missing operator, Parallelism, Aggregation strategy |
| F4 | F. Subquery | 7 | 5 | 50.0% | 33.3% | 50.0% | 100.0% | 100.0% | 1.00 | Missing operator, Parallelism, Join input order |
| F5 | F. Subquery | 5 | 3 | 37.5% | 25.0% | 100.0% | n/a | n/a | 1.00 | Missing operator, Parallelism, Conditions |
| F6 | F. Subquery | 3 | 5 | 50.0% | 50.0% | 100.0% | n/a | n/a | 25.00 | Conditions, Extra operator, Subquery |
| F7 | F. Subquery | 9 | 5 | 35.7% | 28.6% | 100.0% | 100.0% | 100.0% | 1.63 | Missing operator, Parallelism, Aggregation strategy |
| F8 | F. Subquery | 3 | 5 | 50.0% | 50.0% | 100.0% | n/a | 0.0% | 25.00 | Extra operator, Subquery, Cardinality estimate |
| G1 | G. CTE | 14 | 9 | 47.8% | 47.8% | 100.0% | 100.0% | 33.3% | 1.00 | Missing operator, Join inputs, Subquery |
| G2 | G. CTE | 9 | 6 | 33.3% | 46.7% | 100.0% | 100.0% | 100.0% | 4.43 | Missing operator, Tables, Subquery |
| H1 | H. Set operation | 4 | 5 | 33.3% | 22.2% | 100.0% | n/a | n/a | 1.42 | Cardinality estimate, Duplicate elimination, Extra operator |
| H2 | H. Set operation | 4 | 5 | 55.6% | 55.6% | 100.0% | n/a | n/a | 15.88 | Extra operator, Parallelism, Cardinality estimate |
| H3 | H. Set operation | 4 | 5 | 33.3% | 33.3% | 100.0% | n/a | n/a | 1.00 | Extra operator, Parallelism, Missing operator |
| I1 | I. Window function | 3 | 4 | 14.3% | 14.3% | 100.0% | n/a | n/a | 1.00 | Conditions, Extra operator |
| I2 | I. Window function | 4 | 3 | 42.9% | 28.6% | 0.0% | n/a | n/a | 1.06 | Parallelism, Access method, Missing operator |
| J1 | J. Outer join | 8 | 5 | 46.2% | 38.5% | 100.0% | 100.0% | 100.0% | 11.41 | Missing operator, Parallelism, Cardinality estimate |
| J2 | J. Outer join | 5 | 4 | 33.3% | 33.3% | 100.0% | 100.0% | 100.0% | n/a | Parallelism, Join input order, Join type |
| J3 | J. Outer join | 9 | 6 | 33.3% | 20.0% | 100.0% | 100.0% | 100.0% | 1.00 | Missing operator, Parallelism, Conditions |
| K1 | K. Miscellaneous | 1 | 1 | 0.0% | 0.0% | n/a | n/a | n/a | 1.00 | none |
| K2 | K. Miscellaneous | 3 | 4 | 71.4% | 57.1% | 0.0% | 0.0% | 100.0% | 1.00 | Conditions, Join algorithm, Join input order |
| K3 | K. Miscellaneous | 4 | 5 | 77.8% | 66.7% | 0.0% | n/a | n/a | 1.00 | Extra operator, Missing operator, Parallelism |
| K4 | K. Miscellaneous | 2 | 2 | 0.0% | 0.0% | 100.0% | n/a | n/a | 1.00 | none |

## Invalid nodes in AI plans

- F6: a join needs 2 inputs, the AI gave 1
- K3: index scan without an index name
