-- Generality check on a second, non-TPC-H schema (IMDB / Join Order Benchmark shape).
-- Run against the database built by tests/generality/imdb_schema.sql.
-- Includes quoted mixed-case identifiers, a view, a partial index and an expression index.

-- @id: M1
-- @category: Generality (IMDB)
-- @purpose: Multi-way join over the JOB-style schema
SELECT min(t.title) AS movie_title, min(n.name) AS actor
FROM title t
JOIN cast_info ci ON ci.movie_id = t.id
JOIN name n ON n.id = ci.person_id
JOIN role_type rt ON rt.id = ci.role_id
JOIN kind_type kt ON kt.id = t.kind_id
WHERE rt.role = 'actress' AND kt.kind = 'movie' AND t.production_year BETWEEN 2005 AND 2010;

-- @id: M2
-- @category: Generality (IMDB)
-- @purpose: Selection matching a partial index
SELECT id, title FROM title WHERE production_year > 2015 AND production_year < 2018;

-- @id: M3
-- @category: Generality (IMDB)
-- @purpose: Selection matching an expression index
SELECT id, name FROM name WHERE lower(name) = 'person 1679091c5a880faf6fb5e6087eb1b2dc';

-- @id: M4
-- @category: Generality (IMDB)
-- @purpose: Quoted mixed-case table and column names
SELECT t.title, avg(r."Score") AS score
FROM "UserRating" r JOIN title t ON t.id = r."movieId"
WHERE r.rated_on >= DATE '2023-01-01'
GROUP BY t.title
ORDER BY score DESC
LIMIT 10;

-- @id: M5
-- @category: Generality (IMDB)
-- @purpose: Query over a view (the view is expanded by the planner)
SELECT count(*) FROM recent_movies rm JOIN movie_keyword mk ON mk.movie_id = rm.id;

-- @id: M6
-- @category: Generality (IMDB)
-- @purpose: Keyword search with EXISTS on a table without a primary key
SELECT count(*) FROM title t
WHERE EXISTS (SELECT 1 FROM movie_keyword mk JOIN keyword k ON k.id = mk.keyword_id
              WHERE mk.movie_id = t.id AND k.keyword LIKE 'kwa%');

-- @id: M7
-- @category: Generality (IMDB)
-- @purpose: Five-way join with companies and info
SELECT cn.country_code, count(DISTINCT t.id) AS movies
FROM company_name cn
JOIN movie_companies mc ON mc.company_id = cn.id
JOIN company_type ct ON ct.id = mc.company_type_id
JOIN title t ON t.id = mc.movie_id
JOIN movie_info mi ON mi.movie_id = t.id
WHERE ct.kind = 'production companies' AND mi.info_type_id = 16 AND t.production_year > 2000
GROUP BY cn.country_code;

-- @id: M8
-- @category: Generality (IMDB)
-- @purpose: Recursive CTE, window function and UNION ALL together
WITH RECURSIVE y(yr) AS (SELECT 2000 UNION ALL SELECT yr + 1 FROM y WHERE yr < 2005)
SELECT yr, cnt, sum(cnt) OVER (ORDER BY yr) AS cumulative
FROM (SELECT y.yr, count(t.id) AS cnt FROM y LEFT JOIN title t ON t.production_year = y.yr GROUP BY y.yr
      UNION ALL SELECT 1999, 0) s;

-- @id: M9
-- @category: Generality (IMDB)
-- @purpose: Set-returning function in the select list (ProjectSet) and FOR UPDATE-free LIMIT
SELECT id, generate_series(1, 2) AS copy_no FROM kind_type;

-- @id: M10
-- @category: Generality (IMDB)
-- @purpose: OR on two indexed columns (BitmapOr candidate)
SELECT count(*) FROM cast_info WHERE movie_id = 42 OR person_id = 4242;
