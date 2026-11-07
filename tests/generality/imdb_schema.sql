-- IMDB / JOB-shaped schema with synthetic data (test only). Includes awkward
-- features on purpose: quoted mixed-case table, partial and expression indexes,
-- a view, a table without a primary key.
CREATE TABLE kind_type (id integer PRIMARY KEY, kind varchar(15) NOT NULL);
CREATE TABLE info_type (id integer PRIMARY KEY, info varchar(32) NOT NULL);
CREATE TABLE role_type (id integer PRIMARY KEY, role varchar(32) NOT NULL);
CREATE TABLE company_type (id integer PRIMARY KEY, kind varchar(32));
CREATE TABLE title (id integer PRIMARY KEY, title text NOT NULL, kind_id integer NOT NULL,
                    production_year integer, episode_nr integer);
CREATE TABLE name (id integer PRIMARY KEY, name text NOT NULL, gender varchar(1), name_pcode_cf varchar(5));
CREATE TABLE company_name (id integer PRIMARY KEY, name text NOT NULL, country_code varchar(255));
CREATE TABLE keyword (id integer PRIMARY KEY, keyword text NOT NULL);
CREATE TABLE cast_info (id integer PRIMARY KEY, person_id integer NOT NULL, movie_id integer NOT NULL,
                        role_id integer NOT NULL, nr_order integer, note text);
CREATE TABLE movie_info (id integer PRIMARY KEY, movie_id integer NOT NULL, info_type_id integer NOT NULL,
                         info text NOT NULL, note text);
CREATE TABLE movie_keyword (movie_id integer NOT NULL, keyword_id integer NOT NULL);   -- no primary key
CREATE TABLE movie_companies (id integer PRIMARY KEY, movie_id integer NOT NULL, company_id integer NOT NULL,
                              company_type_id integer NOT NULL, note text);
CREATE TABLE "UserRating" ("movieId" integer, "Score" numeric(3,1), rated_on date);

INSERT INTO kind_type SELECT i, (ARRAY['movie','tv series','tv movie','video movie','tv mini series','video game','episode'])[i] FROM generate_series(1,7) i;
INSERT INTO info_type SELECT i, 'info' || i FROM generate_series(1,113) i;
INSERT INTO role_type SELECT i, (ARRAY['actor','actress','producer','writer','cinematographer','composer','costume designer','director','editor','miscellaneous crew','production designer','guest'])[i] FROM generate_series(1,12) i;
INSERT INTO company_type SELECT i, (ARRAY['distributors','production companies','special effects companies','miscellaneous companies'])[i] FROM generate_series(1,4) i;
INSERT INTO title SELECT i, 'Title ' || md5(i::text), 1 + i % 7, CASE WHEN i % 13 = 0 THEN NULL ELSE 1900 + i % 120 END, NULLIF(i % 30, 0) FROM generate_series(1,250000) i;
INSERT INTO name SELECT i, 'Person ' || md5(i::text), (ARRAY['m','f',NULL])[1 + i % 3], substr(md5(i::text),1,5) FROM generate_series(1,400000) i;
INSERT INTO company_name SELECT i, 'Company ' || i, '[' || (ARRAY['us','gb','de','fr','jp','in','sg'])[1 + i % 7] || ']' FROM generate_series(1,20000) i;
INSERT INTO keyword SELECT i, 'kw' || md5(i::text) FROM generate_series(1,30000) i;
INSERT INTO cast_info SELECT i, 1 + (i::bigint * 7919) % 400000, 1 + (i::bigint * 104729) % 250000, 1 + i % 12, i % 20, CASE WHEN i % 5 = 0 THEN '(voice)' END FROM generate_series(1,1500000) i;
INSERT INTO movie_info SELECT i, 1 + (i * 31) % 250000, 1 + i % 113, 'info value ' || (i % 5000), NULL FROM generate_series(1,800000) i;
INSERT INTO movie_keyword SELECT 1 + (i * 17) % 250000, 1 + (i * 13) % 30000 FROM generate_series(1,500000) i;
INSERT INTO movie_companies SELECT i, 1 + (i * 37) % 250000, 1 + i % 20000, 1 + i % 4, CASE WHEN i % 3 = 0 THEN '(co-production)' END FROM generate_series(1,300000) i;
INSERT INTO "UserRating" SELECT 1 + i % 250000, (i % 100) / 10.0, date '2020-01-01' + i % 1500 FROM generate_series(1,100000) i;

CREATE INDEX cast_info_movie_id ON cast_info (movie_id);
CREATE INDEX cast_info_person_id ON cast_info (person_id);
CREATE INDEX movie_info_movie_id ON movie_info (movie_id);
CREATE INDEX movie_keyword_movie_id ON movie_keyword (movie_id);
CREATE INDEX movie_keyword_keyword_id ON movie_keyword (keyword_id);
CREATE INDEX movie_companies_movie_id ON movie_companies (movie_id);
CREATE INDEX title_year_recent ON title (production_year) WHERE production_year > 2000;   -- partial
CREATE INDEX name_lower_name ON name (lower(name));                                         -- expression
CREATE INDEX "UserRating_movieId" ON "UserRating" ("movieId");
CREATE VIEW recent_movies AS SELECT id, title, production_year FROM title WHERE kind_id = 1 AND production_year >= 2010;
ANALYZE;
