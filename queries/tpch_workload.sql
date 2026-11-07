-- Evaluation workload for SC3020 Project 2 (TPC-H, scale factor 1).
--
-- These queries were written by the team for this project. None of them is one
-- of the 22 official TPC-H benchmark queries, because those are very likely in the
-- training data of current LLMs (the project description asks for this).
--
-- Each query targets one planner decision (access path, join algorithm, join
-- order, aggregation strategy, subquery handling, ...), so the evaluation can say
-- WHERE the AI agrees or disagrees with PostgreSQL, not only how much.
--
-- Format read by `python project.py --evaluate`:
--   -- @id: <short unique id>
--   -- @category: <category name>
--   -- @purpose: <what the query tests>
--   <SQL ending with a semicolon>
-- Lines starting with "--" outside these tags are comments.


-- ===================================================================
-- A. Single-table selection: which access path?
-- ===================================================================

-- @id: A1
-- @category: A. Selection / access path
-- @purpose: Point lookup on the primary key (expected: Index Scan on orders_pkey)
SELECT o_orderdate, o_totalprice, o_orderstatus
FROM orders
WHERE o_orderkey = 4242;

-- @id: A2
-- @category: A. Selection / access path
-- @purpose: Narrow range on the primary key (expected: Index Scan)
SELECT o_orderkey, o_totalprice
FROM orders
WHERE o_orderkey BETWEEN 100000 AND 100500;

-- @id: A3
-- @category: A. Selection / access path
-- @purpose: Wide range on the primary key, most rows qualify (expected: Seq Scan despite the index)
SELECT count(*), avg(o_totalprice)
FROM orders
WHERE o_orderkey < 5000000;

-- @id: A4
-- @category: A. Selection / access path
-- @purpose: Very selective filter on a column WITHOUT an index (only a Seq Scan is possible)
SELECT c_name, c_phone, c_acctbal
FROM customer
WHERE c_acctbal > 9990;

-- @id: A5
-- @category: A. Selection / access path
-- @purpose: Only key columns are read (candidate for Index Only Scan)
SELECT count(*)
FROM partsupp
WHERE ps_partkey < 200;

-- @id: A6
-- @category: A. Selection / access path
-- @purpose: Substring LIKE that no B-tree index can serve (parallel Seq Scan)
SELECT p_partkey, p_name, p_retailprice
FROM part
WHERE p_name LIKE '%forest%green%';

-- @id: A7
-- @category: A. Selection / access path
-- @purpose: IN-list on the leading primary-key column (Index Scan or Bitmap scan)
SELECT l_orderkey, l_linenumber, l_quantity
FROM lineitem
WHERE l_orderkey IN (1, 32, 70000, 4000000, 5999975);

-- @id: A8
-- @category: A. Selection / access path
-- @purpose: Predicate on a function of a column, which blocks index use and is hard to estimate
SELECT count(*)
FROM orders
WHERE extract(year FROM o_orderdate) = 1996
  AND o_orderpriority = '1-URGENT';


-- ===================================================================
-- B. Two-table joins: which join algorithm?
-- ===================================================================

-- @id: B1
-- @category: B. Two-table join
-- @purpose: Selective lookup on the outer side, indexed inner side (expected: Nested Loop + Index Scan)
SELECT l.l_linenumber, l.l_quantity, p.p_name
FROM lineitem l
JOIN part p ON p.p_partkey = l.l_partkey
WHERE l.l_orderkey = 777;

-- @id: B2
-- @category: B. Two-table join
-- @purpose: One customer's orders; the join column o_custkey has no index
SELECT o.o_orderkey, o.o_orderdate, o.o_totalprice
FROM customer c
JOIN orders o ON o.o_custkey = c.c_custkey
WHERE c.c_custkey = 1234;

-- @id: B3
-- @category: B. Two-table join
-- @purpose: Filters on both inputs, large tables (expected: Hash Join, smaller side hashed)
SELECT c.c_name, o.o_orderdate, o.o_totalprice
FROM customer c
JOIN orders o ON o.o_custkey = c.c_custkey
WHERE c.c_mktsegment = 'MACHINERY'
  AND o.o_orderdate >= DATE '1997-06-01'
  AND o.o_orderdate <  DATE '1997-06-08';

-- @id: B4
-- @category: B. Two-table join
-- @purpose: Full join of a large table with a small one, then grouping
SELECT s.s_name, count(*) AS parts_supplied, sum(ps.ps_availqty) AS total_qty
FROM supplier s
JOIN partsupp ps ON ps.ps_suppkey = s.s_suppkey
GROUP BY s.s_name;

-- @id: B5
-- @category: B. Two-table join
-- @purpose: Non-equality (theta) self-join; only Nested Loop can do it
SELECT n1.n_name AS lower_nation, n2.n_name AS higher_nation
FROM nation n1
JOIN nation n2 ON n1.n_nationkey < n2.n_nationkey AND n1.n_regionkey = n2.n_regionkey;

-- @id: B6
-- @category: B. Two-table join
-- @purpose: Join of two large tables on their shared key, both sorted by it (Merge Join candidate)
SELECT o.o_orderkey, count(*) AS lines
FROM orders o
JOIN lineitem l ON l.l_orderkey = o.o_orderkey
WHERE o.o_orderkey < 20000
GROUP BY o.o_orderkey;


-- ===================================================================
-- C. Multi-way joins (3 to 6 tables): which join order?
-- ===================================================================

-- @id: C1
-- @category: C. Multi-way join
-- @purpose: 3-way join along a foreign-key chain with a filter on the smallest table
SELECT s.s_name, s.s_acctbal, n.n_name
FROM region r
JOIN nation n   ON n.n_regionkey = r.r_regionkey
JOIN supplier s ON s.s_nationkey = n.n_nationkey
WHERE r.r_name = 'ASIA'
  AND s.s_acctbal > 9000;

-- @id: C2
-- @category: C. Multi-way join
-- @purpose: 4-way join, revenue of returned items per customer nation for one quarter
SELECT n.n_name, sum(l.l_extendedprice * (1 - l.l_discount)) AS lost_revenue
FROM customer c
JOIN orders o   ON o.o_custkey = c.c_custkey
JOIN lineitem l ON l.l_orderkey = o.o_orderkey
JOIN nation n   ON n.n_nationkey = c.c_nationkey
WHERE o.o_orderdate >= DATE '1994-04-01'
  AND o.o_orderdate <  DATE '1994-07-01'
  AND l.l_returnflag = 'R'
GROUP BY n.n_name
ORDER BY lost_revenue DESC;

-- @id: C3
-- @category: C. Multi-way join
-- @purpose: 5-way join, cheapest offer for small brass parts from European suppliers
SELECT p.p_partkey, p.p_mfgr, s.s_name, ps.ps_supplycost
FROM part p
JOIN partsupp ps ON ps.ps_partkey = p.p_partkey
JOIN supplier s  ON s.s_suppkey = ps.ps_suppkey
JOIN nation n    ON n.n_nationkey = s.s_nationkey
JOIN region r    ON r.r_regionkey = n.n_regionkey
WHERE p.p_size = 3
  AND p.p_type LIKE '%BRASS'
  AND r.r_name = 'EUROPE'
ORDER BY ps.ps_supplycost
LIMIT 50;

-- @id: C4
-- @category: C. Multi-way join
-- @purpose: 6-way join, AIR shipments where customer and supplier are in the same nation
SELECT n.n_name, count(*) AS shipments
FROM region r
JOIN nation n   ON n.n_regionkey = r.r_regionkey
JOIN customer c ON c.c_nationkey = n.n_nationkey
JOIN orders o   ON o.o_custkey = c.c_custkey
JOIN lineitem l ON l.l_orderkey = o.o_orderkey
JOIN supplier s ON s.s_suppkey = l.l_suppkey AND s.s_nationkey = c.c_nationkey
WHERE r.r_name = 'AMERICA'
  AND l.l_shipmode = 'AIR'
  AND o.o_orderdate >= DATE '1996-01-01'
  AND o.o_orderdate <  DATE '1996-04-01'
GROUP BY n.n_name;

-- @id: C5
-- @category: C. Multi-way join
-- @purpose: Star-shaped join, fact table with three filtered dimensions
SELECT o.o_orderpriority, sum(l.l_quantity) AS qty
FROM lineitem l
JOIN part p     ON p.p_partkey = l.l_partkey
JOIN supplier s ON s.s_suppkey = l.l_suppkey
JOIN orders o   ON o.o_orderkey = l.l_orderkey
WHERE p.p_brand = 'Brand#23'
  AND p.p_container = 'MED BOX'
  AND s.s_acctbal < 0
GROUP BY o.o_orderpriority;

-- @id: C6
-- @category: C. Multi-way join
-- @purpose: The same table joined twice in different roles (nation as supplier and customer nation)
SELECT sn.n_name AS supplier_nation, cn.n_name AS customer_nation, count(*) AS line_count
FROM lineitem l
JOIN supplier s  ON s.s_suppkey = l.l_suppkey
JOIN orders o    ON o.o_orderkey = l.l_orderkey
JOIN customer c  ON c.c_custkey = o.o_custkey
JOIN nation sn   ON sn.n_nationkey = s.s_nationkey
JOIN nation cn   ON cn.n_nationkey = c.c_nationkey
WHERE sn.n_name = 'JAPAN'
  AND cn.n_name = 'BRAZIL'
  AND l.l_shipdate BETWEEN DATE '1995-01-01' AND DATE '1995-03-31'
GROUP BY sn.n_name, cn.n_name;


-- ===================================================================
-- D. Aggregation and grouping: which strategy?
-- ===================================================================

-- @id: D1
-- @category: D. Aggregation
-- @purpose: Few groups over a large table with HAVING (Hashed or parallel partial aggregation)
SELECT l_shipmode, avg(l_quantity) AS avg_qty, count(*) AS n
FROM lineitem
WHERE l_receiptdate > l_commitdate
GROUP BY l_shipmode
HAVING count(*) > 100000;

-- @id: D2
-- @category: D. Aggregation
-- @purpose: Very many groups (one per order) followed by Top-N
SELECT l_orderkey, sum(l_extendedprice) AS order_value
FROM lineitem
GROUP BY l_orderkey
ORDER BY order_value DESC
LIMIT 10;

-- @id: D3
-- @category: D. Aggregation
-- @purpose: COUNT(DISTINCT) (needs a sort inside the aggregate)
SELECT count(DISTINCT o_custkey) AS active_customers
FROM orders
WHERE o_orderdate >= DATE '1998-01-01';

-- @id: D4
-- @category: D. Aggregation
-- @purpose: SELECT DISTINCT on two low-cardinality columns (HashAggregate or Sort + Unique)
SELECT DISTINCT c_nationkey, c_mktsegment
FROM customer;

-- @id: D5
-- @category: D. Aggregation
-- @purpose: ROLLUP, i.e. several grouping levels in one aggregate
SELECT r.r_name, n.n_name, count(*) AS suppliers
FROM supplier s
JOIN nation n ON n.n_nationkey = s.s_nationkey
JOIN region r ON r.r_regionkey = n.n_regionkey
GROUP BY ROLLUP (r.r_name, n.n_name);

-- @id: D6
-- @category: D. Aggregation
-- @purpose: Conditional aggregation (pivot) with FILTER and CASE, no GROUP BY keys
SELECT count(*) FILTER (WHERE o_orderstatus = 'F') AS finished,
       count(*) FILTER (WHERE o_orderstatus = 'O') AS open,
       sum(CASE WHEN o_orderpriority IN ('1-URGENT', '2-HIGH') THEN 1 ELSE 0 END) AS high_priority
FROM orders
WHERE o_orderdate >= DATE '1995-01-01' AND o_orderdate < DATE '1996-01-01';


-- ===================================================================
-- E. Sorting and Top-N
-- ===================================================================

-- @id: E1
-- @category: E. Sorting / Top-N
-- @purpose: ORDER BY a column without an index plus LIMIT (top-N heapsort, Gather Merge)
SELECT o_orderkey, o_custkey, o_totalprice
FROM orders
ORDER BY o_totalprice DESC
LIMIT 20;

-- @id: E2
-- @category: E. Sorting / Top-N
-- @purpose: ORDER BY the primary key descending plus LIMIT (backward Index Scan, no Sort)
SELECT o_orderkey, o_orderdate
FROM orders
ORDER BY o_orderkey DESC
LIMIT 5;

-- @id: E3
-- @category: E. Sorting / Top-N
-- @purpose: Full sort on two keys with a large OFFSET
SELECT p_partkey, p_name, p_retailprice
FROM part
ORDER BY p_retailprice, p_partkey
LIMIT 10 OFFSET 1000;


-- ===================================================================
-- F. Subqueries: unnested into joins, or run as SubPlan / InitPlan?
-- ===================================================================

-- @id: F1
-- @category: F. Subquery
-- @purpose: Uncorrelated IN subquery (expected: unnested into a semi join)
SELECT c_custkey, c_name
FROM customer
WHERE c_custkey IN (SELECT o_custkey FROM orders WHERE o_totalprice > 500000);

-- @id: F2
-- @category: F. Subquery
-- @purpose: NOT IN subquery (not unnested because of NULL semantics: hashed SubPlan)
SELECT count(*)
FROM part
WHERE p_partkey NOT IN (SELECT l_partkey FROM lineitem WHERE l_quantity = 50
                        AND l_shipdate >= DATE '1998-01-01');

-- @id: F3
-- @category: F. Subquery
-- @purpose: Correlated EXISTS (expected: semi join)
SELECT o_orderpriority, count(*) AS late_orders
FROM orders o
WHERE o.o_orderdate >= DATE '1996-10-01' AND o.o_orderdate < DATE '1997-01-01'
  AND EXISTS (SELECT 1 FROM lineitem l
              WHERE l.l_orderkey = o.o_orderkey AND l.l_receiptdate > l.l_commitdate + 30)
GROUP BY o_orderpriority;

-- @id: F4
-- @category: F. Subquery
-- @purpose: NOT EXISTS (expected: anti join)
SELECT count(*) AS customers_without_orders
FROM customer c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.o_custkey = c.c_custkey);

-- @id: F5
-- @category: F. Subquery
-- @purpose: Uncorrelated scalar subquery (expected: InitPlan, computed once)
SELECT p_partkey, p_name, p_retailprice
FROM part
WHERE p_retailprice > (SELECT avg(p_retailprice) * 1.09 FROM part);

-- @id: F6
-- @category: F. Subquery
-- @purpose: Correlated scalar subquery (PostgreSQL does not unnest it: SubPlan per row)
SELECT s.s_name, s.s_acctbal
FROM supplier s
WHERE s.s_acctbal > (SELECT avg(s2.s_acctbal) + 4000 FROM supplier s2
                     WHERE s2.s_nationkey = s.s_nationkey);

-- @id: F7
-- @category: F. Subquery
-- @purpose: Derived table (subquery in FROM) with aggregation, joined back
SELECT c.c_name, t.order_count, t.spent
FROM (SELECT o_custkey, count(*) AS order_count, sum(o_totalprice) AS spent
      FROM orders GROUP BY o_custkey HAVING count(*) > 35) t
JOIN customer c ON c.c_custkey = t.o_custkey;

-- @id: F8
-- @category: F. Subquery
-- @purpose: Correlated scalar subquery in the SELECT list
SELECT n.n_name,
       (SELECT count(*) FROM supplier s WHERE s.s_nationkey = n.n_nationkey) AS suppliers
FROM nation n
WHERE n.n_regionkey = 2;


-- ===================================================================
-- G. Common table expressions
-- ===================================================================

-- @id: G1
-- @category: G. CTE
-- @purpose: CTE referenced twice (materialized once, read by two CTE Scans)
WITH nation_revenue AS (
    SELECT c.c_nationkey, sum(o.o_totalprice) AS revenue
    FROM customer c JOIN orders o ON o.o_custkey = c.c_custkey
    WHERE o.o_orderdate >= DATE '1993-01-01' AND o.o_orderdate < DATE '1994-01-01'
    GROUP BY c.c_nationkey)
SELECT n.n_name, nr.revenue
FROM nation_revenue nr
JOIN nation n ON n.n_nationkey = nr.c_nationkey
WHERE nr.revenue > (SELECT avg(revenue) FROM nation_revenue);

-- @id: G2
-- @category: G. CTE
-- @purpose: Recursive CTE (Recursive Union over a WorkTable Scan) joined to a table
WITH RECURSIVE days(d) AS (
    SELECT DATE '1995-03-01'
    UNION ALL
    SELECT d + 1 FROM days WHERE d < DATE '1995-03-07')
SELECT days.d, count(o.o_orderkey) AS orders_placed
FROM days
LEFT JOIN orders o ON o.o_orderdate = days.d
GROUP BY days.d
ORDER BY days.d;


-- ===================================================================
-- H. Set operations
-- ===================================================================

-- @id: H1
-- @category: H. Set operation
-- @purpose: UNION with duplicate removal (Append + HashAggregate or Unique)
SELECT c_nationkey AS nationkey FROM customer WHERE c_acctbal > 9900
UNION
SELECT s_nationkey FROM supplier WHERE s_acctbal > 9900;

-- @id: H2
-- @category: H. Set operation
-- @purpose: INTERSECT (SetOp)
SELECT o_custkey FROM orders WHERE o_orderpriority = '1-URGENT' AND o_orderdate >= DATE '1998-06-01'
INTERSECT
SELECT c_custkey FROM customer WHERE c_mktsegment = 'AUTOMOBILE';

-- @id: H3
-- @category: H. Set operation
-- @purpose: EXCEPT on part keys (SetOp Except)
SELECT p_partkey FROM part WHERE p_size = 50
EXCEPT
SELECT ps_partkey FROM partsupp WHERE ps_availqty < 100;


-- ===================================================================
-- I. Window functions
-- ===================================================================

-- @id: I1
-- @category: I. Window function
-- @purpose: rank() per partition (WindowAgg over a Sort)
SELECT * FROM (
    SELECT c_nationkey, c_name, c_acctbal,
           rank() OVER (PARTITION BY c_nationkey ORDER BY c_acctbal DESC) AS rnk
    FROM customer) ranked
WHERE rnk <= 3;

-- @id: I2
-- @category: I. Window function
-- @purpose: Running total with a ROWS frame over one customer's orders
SELECT o_orderdate, o_totalprice,
       sum(o_totalprice) OVER (ORDER BY o_orderdate ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
FROM orders
WHERE o_custkey = 3001;


-- ===================================================================
-- J. Outer joins
-- ===================================================================

-- @id: J1
-- @category: J. Outer join
-- @purpose: LEFT JOIN keeping customers without orders (count includes zeros)
SELECT c.c_custkey, c.c_name, count(o.o_orderkey) AS orders
FROM customer c
LEFT JOIN orders o ON o.o_custkey = c.c_custkey
WHERE c.c_nationkey = 7 AND c.c_acctbal > 9000
GROUP BY c.c_custkey, c.c_name;

-- @id: J2
-- @category: J. Outer join
-- @purpose: LEFT JOIN ... IS NULL, an anti-join written as an outer join
SELECT p.p_partkey, p.p_name
FROM part p
LEFT JOIN lineitem l ON l.l_partkey = p.p_partkey AND l.l_shipdate >= DATE '1998-11-01'
WHERE l.l_orderkey IS NULL
  AND p.p_size = 1;

-- @id: J3
-- @category: J. Outer join
-- @purpose: FULL OUTER JOIN of two aggregated inputs
SELECT coalesce(c.nationkey, s.nationkey) AS nationkey, c.customers, s.suppliers
FROM (SELECT c_nationkey AS nationkey, count(*) AS customers FROM customer GROUP BY c_nationkey) c
FULL OUTER JOIN
     (SELECT s_nationkey AS nationkey, count(*) AS suppliers FROM supplier GROUP BY s_nationkey) s
ON c.nationkey = s.nationkey;


-- ===================================================================
-- K. Miscellaneous shapes
-- ===================================================================

-- @id: K1
-- @category: K. Miscellaneous
-- @purpose: No table at all (a single Result node)
SELECT 1 + 1 AS two, current_date AS today;

-- @id: K2
-- @category: K. Miscellaneous
-- @purpose: Join with an inline VALUES list (Values Scan)
SELECT v.label, o.o_totalprice
FROM (VALUES (1, 'first'), (7, 'seventh'), (32, 'thirty-second')) AS v(k, label)
JOIN orders o ON o.o_orderkey = v.k;

-- @id: K3
-- @category: K. Miscellaneous
-- @purpose: OR across two different columns (BitmapOr if both were indexed; here one is not)
SELECT count(*)
FROM orders
WHERE o_orderkey < 1000 OR o_clerk = 'Clerk#000000001';

-- @id: K4
-- @category: K. Miscellaneous
-- @purpose: String functions and arithmetic in the projection, LIMIT without ORDER BY
SELECT upper(c_name) AS name, length(c_address) AS address_len, c_acctbal * 1.1 AS adjusted
FROM customer
WHERE c_mktsegment = 'BUILDING'
LIMIT 15;
