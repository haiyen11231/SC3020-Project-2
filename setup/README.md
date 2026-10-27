# TPC-H Database Setup (SC3020 Project 2)

This folder explains how to get the TPC-H database running on your machine, and records exactly how the database and the dump file were produced.

There are two routes:

- **Part A: Quick setup (everyone).** Install PostgreSQL 18 and restore the shared dump file. About 15 minutes.
- **Part B: How the dump was made.** Generating, cleaning and loading the data from scratch. Only needed if you want to rebuild the data yourself or want to describe the method in the report.

Dump file (zipped, it contains `tpch.dump`, about 307 MB): https://drive.google.com/file/d/1K6Dt6kxy5pLPHruLYdg4yMeunAxuXpBa/view?usp=sharing



## Files in this folder

| File | Purpose |
|---|---|
| `tpch_schema.sql` | Creates the 8 TPC-H tables (taken from the project appendix, with the `OIDS`, `OWNER` and tablespace lines removed) |
| `clean_tbl.py` | Removes the trailing `\|` from every line of the `.tbl` files and writes `.csv` files |
| `load_csv.py` | Loads the cleaned CSV files into PostgreSQL and runs `ANALYZE` |

If you restore the dump (Part A), you do not need the two scripts. They are only for rebuilding the data yourself (Part B).

## Part A: Quick setup (restore the dump)

Everyone must use **PostgreSQL 18**. A different version can produce different query plans.

1. **Install PostgreSQL 18.** Download the Windows installer from https://www.postgresql.org/download/windows/ and choose version 18. Keep pgAdmin 4 ticked, set a password for the `postgres` user (remember it), and keep port 5432. 
2. **Download the zip** from the Drive link above, right-click it and choose Extract All, and put the extracted `tpch.dump` in a simple folder, for example `C:\tpch\tpch.dump`. Restore needs the extracted `.dump` file, not the zip.
3. **Create an empty database.** Open pgAdmin 4 and connect to PostgreSQL 18 with your password. Right-click Databases, then Create, then Database. Name it exactly `tpch` (lowercase, no spaces) and click Save.
4. **Restore the dump.** Open Command Prompt and run:

   ```
   "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -U postgres -h localhost -d tpch --no-owner "C:\tpch\tpch.dump"
   ```

   Enter your password when asked. It takes a few minutes and prints nothing when it succeeds.
5. **Refresh the statistics.** A dump does not carry the planner statistics, so run this in the pgAdmin Query Tool on `tpch`. It makes your query plans match everyone else's:

   ```sql
   ANALYZE;
   ```
6. **Check the row counts** (see the table in the Verification section below).

## Part B: How the dump was made

This is the exact method used, in order. If you restored the dump in Part A, skip Part B completely. Use it only if you want to rebuild the data yourself, which needs `clean_tbl.py` and `load_csv.py` from this folder.

### B1. Generate the data with the TPC-H tool

1. Download **TPC-H Tools V3.0.1** from the TPC website (the project PDF links to it). It may ask you to fill in a short form. Unzip it. Inside is a folder named `dbgen`.
2. Install **Visual Studio Community** (free) from https://visualstudio.microsoft.com/downloads/. In the installer, tick the workload **Desktop development with C++** and install. Without this workload the project cannot be built.
3. In the `dbgen` folder, open **`tpch.sln`** in Visual Studio. Do not open `tpch.vcproj`: it is an old project format and Visual Studio shows it as plain text. Accept the upgrade prompt if one appears.
4. Build the project (Build menu, then Build Solution). Success looks like `1 succeeded` in the Output panel.
5. Run it (Local Windows Debugger). dbgen runs with the default scale factor 1 and writes 8 files into the `dbgen` folder: `customer.tbl`, `lineitem.tbl`, `nation.tbl`, `orders.tbl`, `part.tbl`, `partsupp.tbl`, `region.tbl`, `supplier.tbl`. In total they are about 1 GB, and `lineitem.tbl` alone is about 750 MB.

To use a smaller dataset instead, copy `dists.dss` next to the built `dbgen.exe` and run `dbgen -s 0.1 -f` from Command Prompt. The team uses scale factor 1, so skip this unless you have a reason.

### B2. Clean the data

dbgen writes an extra `|` at the end of every line. PostgreSQL rejects those lines with the error `extra data after last expected column`. `clean_tbl.py` removes the last `|` from each line and writes `.csv` files into a `clean_csv` folder. It reads line by line, so the large `lineitem.tbl` file does not need to fit in memory.

The core of the script:

```python
def clean_file(src_path, dst_path):
    """Copy src to dst line by line, dropping the final '|' of each line."""
    rows = 0
    with open(src_path, "r", encoding="latin-1", newline="") as src, \
         open(dst_path, "w", encoding="latin-1", newline="") as dst:
        for line in src:
            line = line.rstrip("\r\n")
            if line.endswith("|"):
                line = line[:-1]
            dst.write(line + "\n")
            rows += 1
    return rows
```

Run it from the folder that holds the `.tbl` files (copy `clean_tbl.py` there first):

```
python clean_tbl.py
```

Expected output: one line per table with its row count, for example `lineitem: 6001215 rows`.

### B3. Create the database and tables

1. In pgAdmin 4, create a database named exactly `tpch` (no spaces; a trailing space in the name breaks the scripts).
2. Open the Query Tool on `tpch`, paste the contents of `tpch_schema.sql` and run it (F5). The tables are created in foreign key order: region, nation, part, supplier, partsupp, customer, orders, lineitem.
3. To confirm all 8 tables exist, run:

   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema = 'public' ORDER BY table_name;
   ```

### B4. Load the data

`load_csv.py` loads the cleaned CSV files into PostgreSQL. It does three things:

1. Empties all 8 tables in one `TRUNCATE` statement, so foreign keys do not block it.
2. Loads each CSV with PostgreSQL's `COPY` command, parent tables first so foreign keys hold: region, nation, part, supplier, partsupp, customer, orders, lineitem. The `lineitem` table takes about 4 minutes.
3. Runs `ANALYZE` so the query planner has statistics.

To run it, install the library once, then run the script from the folder that contains `clean_csv` (copy `load_csv.py` there first). Enter your `postgres` password when asked:

```
pip install psycopg2-binary
python load_csv.py
```

If your database name, port or user are different, add options, for example `python load_csv.py --db tpch --port 5432 --user postgres`.

The core of the script:

```python
import psycopg2

TABLES = ["region", "nation", "part", "supplier",
          "partsupp", "customer", "orders", "lineitem"]  # parents first

conn = psycopg2.connect(host="localhost", port=5432, dbname="tpch",
                        user="postgres", password="YOUR_PASSWORD")
cur = conn.cursor()
cur.execute("TRUNCATE " + ", ".join(TABLES))   # empty all tables at once
for table in TABLES:
    with open(f"clean_csv/{table}.csv", "r", encoding="latin-1") as f:
        cur.copy_expert(
            f"COPY {table} FROM STDIN WITH (FORMAT csv, DELIMITER '|')", f)
    conn.commit()
cur.execute("ANALYZE")
conn.commit()
conn.close()
```

If you prefer a graphical route, pgAdmin 4 does the same: right-click each table, choose Import/Export Data, select Import, pick the CSV from `clean_csv`, set the format to csv, the delimiter to `|`, and turn the header off. Import the tables in the same order as above, then run `ANALYZE;`.

### B5. Create the dump

Only needed if you are making a new dump to share. If you restored the dump in Part A, skip this step.

```
"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -U postgres -h localhost -Fc -f tpch.dump tpch
```

`pg_dump` prints nothing when it succeeds. The result is `tpch.dump`, about 307 MB. Zip it and upload it to Drive for the team.

## Verification

After Part A or Part B, row counts must match these (scale factor 1):

| Table | Rows |
|---|---|
| region | 5 |
| nation | 25 |
| supplier | 10,000 |
| customer | 150,000 |
| part | 200,000 |
| partsupp | 800,000 |
| orders | 1,500,000 |
| lineitem | 6,001,215 |

Check each count in the Query Tool, for example:

```sql
SELECT count(*) FROM lineitem;
```

Then check that the planner behaves as in Figure 1 of the project PDF. Run this in the Query Tool:

```sql
EXPLAIN (FORMAT JSON)
SELECT 100.00 * sum(case
        when p_type like 'PROMO%'
        then l_extendedprice * (1 - l_discount)
        else 0 end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue
FROM lineitem, part
WHERE l_partkey = p_partkey
  AND l_shipdate >= date '1993-11-01'
  AND l_shipdate < date '1993-11-01' + interval '1' month;
```

The plan should be: Seq Scan on `lineitem`, Seq Scan on `part` feeding a Hash, a Hash Join, then Aggregate, Gather, Aggregate. If your plan differs, check that `ANALYZE` has run and that you are on PostgreSQL 18.

## Part C: Gemini API key (for the AI plan)

The program asks Google Gemini for the AI agent's query plan (free tier). 

1. **Get a free key.** Sign in at https://aistudio.google.com/apikey with a Google account and click Create API key. Copy the key.
2. **Create a file named `.env`** in the project folder (next to `preprocessing.py`) with these lines:

   ```
   GEMINI_API_KEY=paste-your-key-here
   PGPASSWORD=your-postgres-password
   ```

   On Windows, Notepad may save it as `.env.txt`. In the Save dialog, set "Save as type" to "All files" and type the name `.env`. 
3. **Test it** from Command Prompt in the project folder:

   ```
   pip install -r requirements.txt
   python preprocessing.py --ai "SELECT count(*) FROM nation"
   ```

   It prints the PostgreSQL plan, and after 10 to 60 seconds the AI plan. Both should be `Aggregate` over `Seq Scan [nation]`.

Notes:

- The default model is `gemini-3.5-flash`. When it is busy or over its free quota, the program tries other free Gemini models by itself. To force a model, add `GEMINI_MODEL=model-name` to `.env`.
- AI answers are saved in the `.ai_cache` folder, so running the same query again is instant and uses no quota. Add `--no-cache` to ask Gemini again, or delete the folder to start fresh.
- If you see `No Gemini model could answer`, the free models are busy: wait a minute and retry. `Gemini rejected the API key` means the key in `.env` is wrong.
