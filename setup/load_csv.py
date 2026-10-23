"""Load cleaned TPC-H CSVs (from clean_tbl.py) into a PostgreSQL database.

Setup:
    pip install psycopg2-binary

Usage (run in the folder that contains the clean_csv folder):
    python load_csv.py
    python load_csv.py --host localhost --port 5432 --db tpch --user postgres --dir clean_csv

The password is read at the prompt. 


"""
import argparse
import getpass
import os
import time

import psycopg2

# Parents before children so foreign keys are satisfied.
TABLES = ["region", "nation", "part", "supplier",
          "partsupp", "customer", "orders", "lineitem"]


def parse_args():
    p = argparse.ArgumentParser(description="Load TPC-H CSVs into PostgreSQL")
    p.add_argument("--host", default=os.environ.get("PGHOST", "localhost"))
    p.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    p.add_argument("--db", default=os.environ.get("PGDATABASE", "tpch"))
    p.add_argument("--user", default=os.environ.get("PGUSER", "postgres"))
    p.add_argument("--dir", default="clean_csv", help="folder with the CSV files")
    return p.parse_args()


def main():
    args = parse_args()
    password = os.environ.get("PGPASSWORD") or getpass.getpass(
        f"Password for {args.user}: ")

    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.db,
                            user=args.user, password=password)
    try:
        with conn.cursor() as cur:
            # Empty all tables in one statement so foreign keys do not block it.
            cur.execute("TRUNCATE " + ", ".join(TABLES))
            conn.commit()

            for table in TABLES:
                path = os.path.join(args.dir, table + ".csv")
                if not os.path.exists(path):
                    raise FileNotFoundError(f"Missing file: {path}")
                start = time.time()
                with open(path, "r", encoding="latin-1") as f:
                    cur.copy_expert(
                        f"COPY {table} FROM STDIN WITH (FORMAT csv, DELIMITER '|')", f)
                conn.commit()
                cur.execute(f"SELECT count(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"{table}: {count} rows loaded in {time.time() - start:.1f}s")

            # Refresh statistics so the query planner makes realistic plans.
            print("Running ANALYZE...")
            cur.execute("ANALYZE")
            conn.commit()
        print("Done.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
