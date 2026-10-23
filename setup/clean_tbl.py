"""Clean TPC-H .tbl files so PostgreSQL can import them.

dbgen writes a trailing '|' at the end of every line, which makes Postgres
fail with "extra data after last expected column". This script removes it
and writes the result as .csv files (still '|' separated) into an output folder.

"""
import os
import sys

TABLES = ["region", "nation", "part", "supplier",
          "partsupp", "customer", "orders", "lineitem"]


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


def main():
    in_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(in_dir, "clean_csv")
    os.makedirs(out_dir, exist_ok=True)

    for table in TABLES:
        src = os.path.join(in_dir, table + ".tbl")
        dst = os.path.join(out_dir, table + ".csv")
        if not os.path.exists(src):
            print(f"SKIPPED {table}: {src} not found")
            continue
        rows = clean_file(src, dst)
        print(f"{table}: {rows} rows -> {dst}")


if __name__ == "__main__":
    main()