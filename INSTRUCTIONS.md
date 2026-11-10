# How to run the software (SC3020 Project 2)

The program compares the query execution plan (QEP) that PostgreSQL chooses for an SQL
query with the plan that an AI agent (Google Gemini) proposes, shows both plans as
colour-coded trees and explains every difference.

These steps are written for **Windows 10/11** (Command Prompt). The same commands work
in PyCharm's terminal, and on macOS/Linux with `python3` instead of `python`.

## 1. What you need

| Requirement | Notes |
|---|---|
| Python 3.10 or newer | https://www.python.org/downloads/. In the installer, tick **Add python.exe to PATH**. |
| PostgreSQL 18 with a loaded database | For TPC-H, follow `setup/README.md` Part A (about 15 minutes). Any other database works too, see section 6. |
| A Gemini API key | Free: sign in at https://aistudio.google.com/apikey and click **Create API key**. Without a key the program still runs, but shows only the PostgreSQL plan. |
| Internet access | Needed only for the AI agent's plan. |

## 2. Install the Python package

Open Command Prompt **in the project folder** (the folder that contains `project.py`) and run:

```
python -m pip install -r requirements.txt
```

## 3. Enter your settings

Copy `.env.example` to a new file named `.env` in the same folder and fill in your values:

```
copy .env.example .env
notepad .env
```

```
PGHOST=localhost
PGPORT=5432
PGDATABASE=tpch
PGUSER=postgres
PGPASSWORD=your-postgres-password
GEMINI_API_KEY=your-gemini-key
```

Notepad may save the file as `.env.txt`. In the Save dialog choose **Save as type: All files**.
The database settings can also be typed in the GUI instead.

## 4. Check the setup

```
python project.py --check
```

Every line should show `[OK]`. A line marked `[--]` is optional; a line marked `[!!]`
must be fixed and comes with a hint. Add `--ai` to also send one test question to Gemini.

## 5. Run the program

```
python project.py
```

The window opens and connects by itself with the settings in `.env` (if that fails, a
small form asks for the password or database). Everything you do is in the left sidebar:

1. **Pick a query** from the example list (built from the connected database), or type
   your own SQL in the box.
2. **Press Compare plans** (or Ctrl+Enter). PostgreSQL's plan appears at once; the AI
   plan takes 10 to 60 seconds the first time and is instant afterwards (answers are
   saved in `.ai_cache/`).
3. **Read the results.** The large number at the bottom of the sidebar is the difference
   rate, with a one-line verdict. The two plans are drawn side by side on the right;
   colours show how each operator compares (legend above the trees). Click a difference,
   or any box in a tree, to frame the matching operators in both trees and see details.

*File > Show the example from the project description* works without a database or key.
*Help > How to use this program* repeats these steps.

**PyCharm:** open the project folder, set the interpreter (File > Settings > Project >
Python Interpreter) to the Python from step 1, install `psycopg2-binary` there if PyCharm
asks, then right-click `project.py` and choose **Run 'project'**.

### Without the GUI

| Command | What it does |
|---|---|
| `python project.py --demo` | Compares Figure 1 and Figure 2 of the project description. Needs no database or key. |
| `python project.py --query "SELECT ..."` | Compares one query and prints both trees and the explanation. |
| `python project.py --query-file my_query.sql` | The same, with the query in a file. |
| `python project.py --query "SELECT ..." --json` | Prints everything as JSON. |
| `python project.py --query "SELECT ..." --no-cache` | Asks Gemini again instead of using the saved answer. |

## 6. Using another database (for example IMDB)

Nothing in the program depends on TPC-H. The schema, indexes and statistics given to the
AI are read from whichever database is connected. To use another database:

1. Load it into PostgreSQL (tables in the `public` schema).
2. Run `ANALYZE;` in it once, so the planner statistics exist.
3. Choose it in the GUI, or start with `python project.py --db imdb`, or set `PGDATABASE=imdb` in `.env`.

## 7. Reproducing the evaluation in the report

```
python project.py --evaluate
```

This runs all queries in `queries/tpch_workload.sql` (about 15 to 40 minutes on the Gemini
free tier) and writes to `results/`:

- `summary.md`: the tables used in the report (overall, per category, kinds of differences)
- `summary.csv`: one row per query with every metric
- `queries/<id>.json`: the SQL, both raw plans, both parsed trees and the full comparison

Useful options: `--only A,B3` (some queries), `--repeats 3` (ask the AI three times per
query to measure how stable its answers are), `--delay 5` (pause between AI calls if the
free quota is exceeded), `--evaluate my_queries.sql` (your own workload; one query per
`;`, tags optional).

`python project.py --rescore` recomputes all tables from the saved plans in `results/`
without the database or the API key, so the reported numbers can be checked offline.

## 8. Tests

```
python -m unittest discover -s tests -v
```

The tests run without a database. To include the tests that use PostgreSQL, first run
`set SC3020_TEST_DB=1` in the same Command Prompt.

## 9. Troubleshooting

| Message | Fix |
|---|---|
| `Missing Python package 'psycopg2'` | Run step 2 with the same Python that runs `project.py`. |
| `'python' is not recognized` | Use `py` instead of `python`, or reinstall Python with "Add to PATH" ticked. |
| `Could not connect to database ... password authentication failed` | Wrong `PGPASSWORD` in `.env`. |
| `Could not connect ... database "tpch" does not exist` | Create and restore the database (`setup/README.md`), or set `PGDATABASE`. |
| `[!!] ... tables have no statistics` | Run `ANALYZE;` in the database. |
| `No Gemini API key` | Add `GEMINI_API_KEY=...` to `.env` (step 3). Only the PostgreSQL plan is shown until then. |
| `Gemini rejected the API key` | The key is wrong or was deleted; create a new one. |
| `No Gemini model could answer` | The free models are busy or over quota. Wait a minute and try again. |
| `Please enter one SQL statement at a time` | Remove the extra statements; EXPLAIN takes one query. |
| Plans differ from the report | Check `python project.py --check` shows PostgreSQL 18 and that `ANALYZE` has run. |
