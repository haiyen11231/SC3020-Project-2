"""interface.py  (Part 4: GUI and visualisation)

The window has two areas:

    Left sidebar   what the user DOES, top to bottom: the database (connected
                   automatically with the settings in .env), the SQL query (type one
                   or pick an example), the "Compare plans" button, and the result
                   card with the difference rate in large type and a one-line verdict.
    Right area     what the user READS: PostgreSQL's plan and the AI agent's plan as
                   colour-coded trees side by side, and below them the list of
                   differences and the details of the selected operator.

Clicking a difference, or any operator in a tree, frames the corresponding operators
in BOTH trees and shows their details. Less frequent actions (the offline example
from the project description, opening and saving results, connection settings) are
in the File menu.

The look is drawn with Tk's portable "clam" theme and a fixed light palette, so the
window looks the same on Windows and macOS (including macOS dark mode).

The window only talks to the Session object that project.py passes to run_gui().
Slow work (connecting, EXPLAIN, asking the AI agent) runs in a background thread so
the window never freezes. Nothing depends on a particular schema: the example list is
built from whatever database is connected.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import preprocessing as pp
from comparison import STATUS_COLORS, compare_plans, demo_plans, iter_nodes, node_at, plan_from_dict
from preprocessing import DBConfig, PlanError, PlanNode

HERE = os.path.dirname(os.path.abspath(__file__))
WORKLOAD_FILE = os.path.join(HERE, "queries", "tpch_workload.sql")
RESULTS_DIR = os.path.join(HERE, "results", "queries")

# --- palette ------------------------------------------------------------------
APP_BG = "#EEF1F5"          # window background
CARD = "#FFFFFF"            # panels
LINE = "#D9DEE5"            # panel borders and separators
INK = "#1F2933"             # main text
MUTED = "#616E7C"           # secondary text
ACCENT = "#2563EB"          # the one action colour (Compare button, links)
ACCENT_DARK = "#1D4ED8"
ERROR_RED = "#C62828"
EDGE = "#A0AAB6"            # tree edges
HIGHLIGHT = "#7C3AED"       # frame around the selected operator and its match
BUTTON_BG = "#E7EBF0"

# Node borders use the status colours from comparison.py; fills are light tints.
STATUS_FILL = {"same": "#E8F5E9", "changed": "#FFF3E0",
               "postgres_only": "#FFEBEE", "ai_only": "#E3F2FD"}
STATUS_TEXT = {"same": "#2E7D32", "changed": "#C2410C",          # darker, for text
               "postgres_only": "#C62828", "ai_only": "#1565C0"}
STATUS_SHORT = {"same": "Same in both", "changed": "In both, but different",
                "postgres_only": "Only in PostgreSQL's plan", "ai_only": "Only in the AI's plan"}
NEUTRAL_FILL, NEUTRAL_BORDER = "#F5F7FA", "#9AA5B1"

# Tk on macOS draws a point as one pixel (Windows: 1.33 px), so text sized for
# Windows looks tiny there. Every font size goes through pt(), which enlarges it
# on macOS only.
FONT_SCALE = 1.25 if sys.platform == "darwin" else 1.0


def pt(size: float) -> int:
    return max(1, round(size * FONT_SCALE))


# The example query of Figures 1 and 2 in the project description.
DEMO_SQL = ("SELECT 100.00 * sum(CASE WHEN p_type LIKE 'PROMO%'\n"
            "            THEN l_extendedprice * (1 - l_discount) ELSE 0 END)\n"
            "       / sum(l_extendedprice * (1 - l_discount)) AS promo_revenue\n"
            "FROM lineitem, part\n"
            "WHERE l_partkey = p_partkey\n"
            "  AND l_shipdate >= DATE '1993-11-01'\n"
            "  AND l_shipdate < DATE '1993-11-01' + INTERVAL '1' MONTH;")

HELP_TEXT = (
    "1. The program connects to PostgreSQL with the settings in the .env file "
    "(File > Connection settings to change them).\n\n"
    "2. Pick a query from the examples, or type your own SQL.\n\n"
    "3. Press 'Compare plans'. PostgreSQL's plan appears at once; the AI agent can take "
    "up to a minute the first time (its answers are saved, so repeating a query is instant).\n\n"
    "4. The large number is the difference rate: the share of operators without an "
    "identical match (0% = identical plans). The colours in the trees show how each "
    "operator compares. Click a difference, or any operator, to frame the matching "
    "operators in both trees and see the details.\n\n"
    "No database? File > 'Show the example from the project description' works offline.")

# ===========================================================================
# Text helpers (no Tk; easy to test)
# ===========================================================================
def node_title(node: PlanNode) -> str:
    """Operator name with the detail that changes its meaning, e.g.
    'Partial Aggregate (Hashed)' or 'Hash Join (Semi)'."""
    name = node.node_type
    if name == "Aggregate":
        mode = node.extra.get("Partial Mode")
        strategy = node.extra.get("Strategy")
        prefix = f"{mode} " if mode in ("Partial", "Finalize") else ""
        suffix = f" ({strategy})" if strategy and strategy != "Plain" else ""
        return f"{prefix}Aggregate{suffix}"
    if name in ("Hash Join", "Merge Join", "Nested Loop") and node.join_type \
            and node.join_type != "Inner":
        return f"{name} ({node.join_type})"
    return name


def node_subtitle(node: PlanNode) -> str:
    """What the operator reads: table (with its alias) or the WITH query name."""
    name = node.relation or node.extra.get("CTE Name") or node.extra.get("Name")
    alias = node.extra.get("Alias")
    if not name:
        return ""
    return f"{name} {alias}" if alias and alias != name else str(name)


def rows_text(node: PlanNode) -> str:
    if node.rows is None:
        return ""
    rows = int(round(node.rows))
    return f"≈ {rows:,} row" + ("" if rows == 1 else "s")


def wrap_words(text: str, width: int, max_lines: int = 2) -> List[str]:
    """Break text into at most max_lines lines of about width characters."""
    lines: List[str] = []
    for word in str(text).split():
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = shorten(lines[-1] + " …", width)
    return [shorten(line, width) for line in lines] or [""]


def shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:max(1, limit - 1)] + "…"


def percent(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def layout_tree(plan: PlanNode) -> Tuple[Dict[str, Tuple[float, int]], int, int]:
    """Top-down tree layout: leaves take successive columns and every parent sits
    centred above its children. Returns {path: (column, depth)}, the number of
    columns and the maximum depth."""
    positions: Dict[str, Tuple[float, int]] = {}
    next_column = [0]

    def place(node: PlanNode, path: str, depth: int) -> float:
        columns = [place(child, f"{path}.{i}", depth + 1)
                   for i, child in enumerate(node.children)]
        if columns:
            column = (columns[0] + columns[-1]) / 2
        else:
            column = next_column[0]
            next_column[0] += 1
        positions[path] = (column, depth)
        return column

    place(plan, "0", 0)
    return positions, max(next_column[0], 1), max(d for _, d in positions.values())


def verdict(rate: float) -> str:
    """Plain-English reading of a difference rate."""
    if rate == 0:
        return "the plans are identical"
    if rate < 0.2:
        return "the plans are very similar"
    if rate < 0.5:
        return "the plans differ in some places"
    return "the plans are very different"


# ===========================================================================
# Example queries (built from the connected database, so any schema works)
# ===========================================================================
def quote_ident(name: str) -> str:
    """Quote an identifier only when PostgreSQL needs it (capitals, spaces, ...)."""
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def read_workload_file(path: str) -> List[Tuple[str, str, str]]:
    """(id, purpose, sql) for each query of a workload file with @id/@purpose tags."""
    if not os.path.exists(path):
        return []
    queries, tags, buffer = [], {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            tag = re.match(r"\s*--\s*@(\w+)\s*:\s*(.*)", line)
            if tag and not buffer:
                tags[tag.group(1).lower()] = tag.group(2).strip()
                continue
            if not buffer and (not line.strip() or line.strip().startswith("--")):
                continue
            buffer.append(line)
            if line.rstrip().endswith(";"):
                queries.append((tags.get("id", f"Q{len(queries) + 1}"),
                                tags.get("purpose", ""), "".join(buffer).strip()))
                tags, buffer = {}, []
    return queries


def schema_examples(conn) -> List[Tuple[str, str]]:
    """A few simple queries generated from the schema itself (largest table,
    smallest table, joins along foreign keys)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                        "ORDER BY c.reltuples DESC, c.relname")
            tables = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT child.relname, ca.attname, parent.relname, pa.attname "
                        "FROM pg_constraint k "
                        "JOIN pg_class child ON child.oid = k.conrelid "
                        "JOIN pg_class parent ON parent.oid = k.confrelid "
                        "JOIN pg_namespace n ON n.oid = child.relnamespace "
                        "JOIN pg_attribute ca ON ca.attrelid = k.conrelid AND ca.attnum = k.conkey[1] "
                        "JOIN pg_attribute pa ON pa.attrelid = k.confrelid AND pa.attnum = k.confkey[1] "
                        "WHERE k.contype = 'f' AND n.nspname = 'public' "
                        "AND cardinality(k.conkey) = 1 "
                        "ORDER BY child.reltuples DESC LIMIT 3")
            foreign_keys = cur.fetchall()
    except Exception:              # examples are a convenience; never block the GUI
        return []
    examples = []
    if tables:
        examples.append((shorten(f"Schema: count rows of {tables[0]}", 46),
                         f"SELECT count(*) FROM {quote_ident(tables[0])};"))
        examples.append((shorten(f"Schema: first 10 rows of {tables[-1]}", 46),
                         f"SELECT * FROM {quote_ident(tables[-1])} LIMIT 10;"))
    for child, child_col, parent, parent_col in foreign_keys:
        examples.append((shorten(f"Schema: join {child} with {parent}", 46),
                         f"SELECT count(*)\nFROM {quote_ident(child)} c\n"
                         f"JOIN {quote_ident(parent)} p "
                         f"ON c.{quote_ident(child_col)} = p.{quote_ident(parent_col)};"))
    return examples


def collect_examples(conn) -> List[Tuple[str, str]]:
    """Example queries that PostgreSQL can plan on the connected database: the
    project description's query, the evaluation workload, then schema examples.
    Each is checked with EXPLAIN (fast; nothing is executed)."""
    def plannable(sql: str) -> bool:
        try:
            pp.get_postgres_plan(conn, sql)
            return True
        except PlanError:
            return False

    examples = []
    if plannable(DEMO_SQL):
        examples.append(("Project description example (Fig. 1 and 2)", DEMO_SQL))
    for qid, purpose, sql in read_workload_file(WORKLOAD_FILE):
        if plannable(sql):
            # Short labels so they fit the drop-down: "C4: 6-way join".
            short = re.split(r"[,(]", purpose)[0].strip() or purpose
            examples.append((shorten(f"{qid}: {short}", 46), sql))
    examples += schema_examples(conn)
    return examples



# ===========================================================================
# Small building blocks
# ===========================================================================
def card(parent, padding=(14, 12)) -> Tuple[tk.Frame, ttk.Frame]:
    """A white panel with a thin border. Returns (outer frame, inner content frame)."""
    outer = tk.Frame(parent, bg=CARD, bd=0, highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=LINE)
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True)
    return outer, inner


class PlanView(tk.Frame):
    """One query plan drawn top-down (root at the top) in a white panel, with
    colour-coded operator boxes. It fits the tree to the panel width as long as the
    text stays readable, and scrolls otherwise. The -, Fit and + buttons zoom."""

    BOX_W, BOX_H, GAP_X, GAP_Y, MARGIN = 190, 76, 24, 40, 18
    MIN_FIT_SCALE = 0.75            # below this the text gets hard to read; scroll instead

    def __init__(self, parent, side: str, title: str, family: str,
                 on_click: Callable[[str, str], None]) -> None:
        super().__init__(parent, bg=CARD, bd=0, highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=LINE)
        self.side, self.family, self.on_click = side, family, on_click
        self.plan: Optional[PlanNode] = None
        self.comparison = None
        self.message = ""
        self.scale, self.fit = 1.0, True
        self.boxes: Dict[str, Tuple[int, str]] = {}       # path -> (rectangle id, border)
        self.centres: Dict[str, Tuple[float, float]] = {}
        self.highlighted: Set[str] = set()
        self.total = (1.0, 1.0)
        self._resize_job = None

        header = ttk.Frame(self, style="Card.TFrame", padding=(14, 10, 10, 6))
        header.pack(fill="x")
        ttk.Label(header, text=title, style="CardTitle.TLabel").pack(side="left")
        self.subtitle_var = tk.StringVar()
        ttk.Label(header, textvariable=self.subtitle_var,
                  style="CardMuted.TLabel").pack(side="left", padx=(8, 0))
        for text, command in (("+", lambda: self.zoom(1.25)), ("Fit", self.fit_width),
                              ("−", lambda: self.zoom(0.8))):
            ttk.Button(header, text=text, width=3 if len(text) == 1 else 4,
                       style="Small.TButton", command=command).pack(side="right", padx=(4, 0))

        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True, padx=(1, 1), pady=(0, 1))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, bg=CARD, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ttk.Scrollbar(body, orient="horizontal", command=self.canvas.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)

        self.canvas.tag_bind("node", "<Button-1>", self._clicked)
        self.canvas.tag_bind("node", "<Enter>", lambda _e: self.canvas.configure(cursor="hand2"))
        self.canvas.tag_bind("node", "<Leave>", lambda _e: self.canvas.configure(cursor=""))
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self._wheel(e, horizontal=True))
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(2, "units"))

    # --- public ----------------------------------------------------------------
    def set_subtitle(self, text: str) -> None:
        self.subtitle_var.set(text)

    def show(self, plan: Optional[PlanNode], comparison=None, message: str = "") -> None:
        """Draw a plan (or, without one, a message) and reset zoom and selection."""
        self.plan, self.comparison, self.message = plan, comparison, message
        self.highlighted = set()
        self.fit = True
        self._draw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def highlight(self, path: Optional[str]) -> None:
        """Frame one operator in purple (None clears) and scroll it into view."""
        self.highlighted = {path} if path else set()
        self._apply_highlight()
        if path:
            self._scroll_to(path)

    def zoom(self, factor: float) -> None:
        self.fit = False
        self.scale = max(0.4, min(1.6, self.scale * factor))
        self._draw()

    def fit_width(self) -> None:
        self.fit = True
        self._draw()

    # --- drawing ---------------------------------------------------------------
    def _status(self, path: str) -> Optional[str]:
        if self.comparison is None:
            return None
        try:
            return self.comparison.status(self.side, path)
        except KeyError:
            return None

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        self.boxes.clear()
        self.centres.clear()
        view_w = max(canvas.winfo_width(), 300)
        view_h = max(canvas.winfo_height(), 100)
        if self.plan is None:
            if self.message:
                canvas.create_text(view_w / 2, 40, text=self.message, anchor="n", fill=MUTED,
                                   font=(self.family, pt(10)), width=view_w - 60,
                                   justify="center")
            self.total = (view_w, view_h)
            canvas.configure(scrollregion=(0, 0, view_w, view_h))
            return

        # Box sizes are designed for 96 dpi (1.33 px per point). On screens where
        # text is drawn larger or smaller (Windows scaling, macOS), the boxes are
        # scaled the same way so the text always fits.
        unit = canvas.winfo_fpixels("1p") / (96 / 72) * FONT_SCALE
        positions, columns, depth = layout_tree(self.plan)
        natural = (columns * (self.BOX_W + self.GAP_X) - self.GAP_X + 2 * self.MARGIN) * unit
        if self.fit:
            self.scale = max(self.MIN_FIT_SCALE, min(1.0, (view_w - 4) / natural))
        s = self.scale * unit
        w, h = self.BOX_W * s, self.BOX_H * s
        gap_x, gap_y, margin = self.GAP_X * s, self.GAP_Y * s, self.MARGIN * s
        width = 2 * margin + columns * (w + gap_x) - gap_x
        height = 2 * margin + depth * (h + gap_y) + h
        offset = max(0.0, (view_w - width) / 2)            # centre a narrow tree

        def top_centre(path: str) -> Tuple[float, float]:
            column, level = positions[path]
            return offset + margin + column * (w + gap_x) + w / 2, margin + level * (h + gap_y)

        nodes = dict(iter_nodes(self.plan))
        # Edges first, so the boxes are drawn over them. A dashed edge leads to a
        # subquery plan (InitPlan / SubPlan) rather than a normal input.
        for path, node in nodes.items():
            px, py = top_centre(path)
            for i, child in enumerate(node.children):
                cx, cy = top_centre(f"{path}.{i}")
                mid = py + h + gap_y / 2
                subquery = child.extra.get("Parent Relationship") in ("InitPlan", "SubPlan")
                canvas.create_line(px, py + h, px, mid, cx, mid, cx, cy, fill=EDGE,
                                   width=max(1.0, 1.5 * s), dash=(4, 3) if subquery else None)

        # Font sizes are in points; a character is about 0.6 of the font height wide,
        # which sets how many characters fit on one line of a box.
        title_size = max(pt(9), pt(10 * self.scale))
        small_size = max(pt(8), pt(9 * self.scale))
        title_font = (self.family, title_size, "bold")
        small_font = (self.family, small_size)
        px_per_pt = unit / FONT_SCALE
        title_chars = max(8, int((w - 12 * unit) / (0.8 * title_size * px_per_pt)))
        small_chars = max(8, int((w - 10 * unit) / (0.75 * small_size * px_per_pt)))
        title_line, small_line = title_size * 1.6 * px_per_pt, small_size * 1.6 * px_per_pt
        for path, node in nodes.items():
            x, y = top_centre(path)
            status = self._status(path)
            border = STATUS_COLORS.get(status, NEUTRAL_BORDER)
            tags = ("node", "p:" + path)
            rect = canvas.create_rectangle(x - w / 2, y, x + w / 2, y + h,
                                           fill=STATUS_FILL.get(status, NEUTRAL_FILL),
                                           outline=border, width=2, tags=tags)
            # Up to two lines of operator name, then what it reads, then either the
            # index it uses or its estimated rows (everything else is in the details).
            lines = [(text, title_font, INK, title_line)
                     for text in wrap_words(node_title(node), title_chars)]
            for text in (node_subtitle(node),
                         f"using {node.index_name}" if node.index_name else rows_text(node)):
                if text:
                    lines.append((shorten(text, small_chars), small_font, MUTED, small_line))
            cursor = y + (h - sum(line[3] for line in lines)) / 2
            for text, font, colour, line_height in lines:
                canvas.create_text(x, cursor + line_height / 2, text=text, font=font,
                                   fill=colour, tags=tags)
                cursor += line_height
            self.boxes[path] = (rect, border)
            self.centres[path] = (x, y + h / 2)

        self.total = (max(width, view_w), max(height, view_h))
        canvas.configure(scrollregion=(0, 0, self.total[0], self.total[1]))
        self._apply_highlight()

    def _apply_highlight(self) -> None:
        for path, (rect, border) in self.boxes.items():
            selected = path in self.highlighted
            self.canvas.itemconfigure(rect, outline=HIGHLIGHT if selected else border,
                                      width=4 if selected else 2)

    def _scroll_to(self, path: str) -> None:
        if path not in self.centres:
            return
        x, y = self.centres[path]
        total_w, total_h = self.total
        view_w, view_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if total_w > view_w:
            self.canvas.xview_moveto(max(0.0, min(total_w - view_w, x - view_w / 2)) / total_w)
        if total_h > view_h:
            self.canvas.yview_moveto(max(0.0, min(total_h - view_h, y - view_h / 2)) / total_h)

    # --- events ----------------------------------------------------------------
    def _clicked(self, _event) -> None:
        for tag in self.canvas.gettags("current"):
            if tag.startswith("p:"):
                self.on_click(self.side, tag[2:])
                return

    def _resized(self, _event) -> None:
        if self.fit and (self.plan is not None or self.message):
            if self._resize_job is not None:
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(120, self._redraw_after_resize)

    def _redraw_after_resize(self) -> None:
        self._resize_job = None
        self._draw()

    def _wheel(self, event, horizontal: bool = False) -> None:
        # Windows reports multiples of 120 per notch, macOS small numbers.
        delta = event.delta
        steps = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        if horizontal:
            self.canvas.xview_scroll(steps, "units")
        else:
            self.canvas.yview_scroll(steps, "units")


class TextPanel(tk.Frame):
    """A white panel with a title and read-only text that can hold clickable lines."""

    def __init__(self, parent, title: str, family: str, hint: str = "") -> None:
        super().__init__(parent, bg=CARD, bd=0, highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=LINE)
        header = ttk.Frame(self, style="Card.TFrame", padding=(14, 10, 14, 0))
        header.pack(fill="x")
        ttk.Label(header, text=title, style="CardTitle.TLabel").pack(side="left")
        self.hint_var = tk.StringVar(value=hint)
        ttk.Label(header, textvariable=self.hint_var,
                  style="CardMuted.TLabel").pack(side="left", padx=(8, 0))
        holder = tk.Frame(self, bg=CARD)
        holder.pack(fill="both", expand=True, padx=(14, 2), pady=(6, 8))
        self.text = tk.Text(holder, wrap="word", bg=CARD, fg=INK, relief="flat", bd=0,
                            highlightthickness=0, font=(family, pt(10)), cursor="arrow",
                            padx=0, pady=2, spacing1=1, spacing3=1, width=20, height=4,
                            selectbackground="#DBEAFE", selectforeground=INK)
        scroll = ttk.Scrollbar(holder, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        self.text.tag_configure("h", font=(family, pt(10), "bold"), spacing1=10, spacing3=3)
        self.text.tag_configure("bold", font=(family, pt(10), "bold"))
        self.text.tag_configure("muted", foreground=MUTED)
        self.text.tag_configure("accent", foreground=ACCENT, font=(family, pt(10), "bold"))
        self.text.tag_configure("item", spacing1=7, lmargin2=pt(18))
        self.text.tag_configure("error", foreground=ERROR_RED, font=(family, pt(11), "bold"))
        self.text.tag_configure("mono", font=(mono, pt(10)))
        for status, colour in STATUS_TEXT.items():
            self.text.tag_configure("st_" + status, foreground=colour,
                                    font=(family, pt(10), "bold"))
        self.text.configure(state="disabled")
        self._links = 0

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for tag in self.text.tag_names():
            if tag.startswith("link"):
                self.text.tag_delete(tag)
        self.text.configure(state="disabled")
        self._links = 0

    def add(self, text: str, *tags: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text, tags)
        self.text.configure(state="disabled")

    def add_link(self, parts: List[Tuple[str, Tuple[str, ...]]],
                 callback: Callable[[], None]) -> None:
        """One clickable paragraph made of (text, tags) parts; it is shaded on hover."""
        tag = f"link{self._links}"
        self._links += 1
        self.text.configure(state="normal")
        for text, tags in parts:
            self.text.insert("end", text, (tag, "item") + tuple(tags))
        self.text.insert("end", "\n")
        self.text.configure(state="disabled")
        self.text.tag_bind(tag, "<Button-1>", lambda _e: callback())
        self.text.tag_bind(tag, "<Enter>", lambda _e: self._hover(tag, True))
        self.text.tag_bind(tag, "<Leave>", lambda _e: self._hover(tag, False))

    def _hover(self, tag: str, inside: bool) -> None:
        self.text.tag_configure(tag, background="#EEF2FF" if inside else CARD)
        self.text.configure(cursor="hand2" if inside else "arrow")


# ===========================================================================
# Connection dialog (shown only when the automatic connection fails, or on request)
# ===========================================================================
class ConnectionDialog(tk.Toplevel):
    def __init__(self, app: "App", message: str = "") -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Connect to PostgreSQL")
        self.configure(bg=APP_BG)
        self.transient(app.root)
        self.resizable(False, False)
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        row = 0
        if message:
            ttk.Label(frame, text=message, style="Error.TLabel", wraplength=420,
                      justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 12))
            row += 1
        cfg = app.session.db_config
        self.fields: Dict[str, tk.StringVar] = {}
        for label, key, value, secret in (("Host", "host", cfg.host, False),
                                          ("Port", "port", cfg.port, False),
                                          ("Database", "dbname", cfg.dbname, False),
                                          ("User", "user", cfg.user, False),
                                          ("Password", "password", cfg.password, True)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(value or ""))
            entry = ttk.Entry(frame, textvariable=var, width=34, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4, padx=(12, 0))
            if key == "password":
                self.password_entry = entry
            self.fields[key] = var
            row += 1
        ttk.Label(frame, text="The defaults come from the .env file next to project.py.",
                  style="Muted.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=row + 1, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Offline example instead",
                   command=self._offline).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=8)
        ttk.Button(buttons, text="Connect", style="Primary.TButton",
                   command=self._connect).pack(side="left")
        self.bind("<Return>", lambda _e: self._connect())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(60, self._grab)
        self.password_entry.focus_set()

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass                    # not viewable yet on some platforms; harmless

    def _connect(self) -> None:
        values = {key: var.get().strip() for key, var in self.fields.items()}
        values["password"] = self.fields["password"].get()   # spaces are allowed
        self.destroy()
        self.app.connect(DBConfig(**values))

    def _offline(self) -> None:
        self.destroy()
        self.app.show_demo()


# ===========================================================================
# The main window
# ===========================================================================
class App:
    def __init__(self, root: tk.Tk, session: Any) -> None:
        self.root, self.session = root, session
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self._busy_message, self._busy_since = "", 0.0
        self.result = None
        self.examples: List[Tuple[str, str]] = []
        self.family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.is_mac = sys.platform == "darwin"
        # Screen pixels per design pixel, so the sidebar keeps its proportions
        # under Windows display scaling and on macOS.
        self.unit = root.winfo_fpixels("1p") / (96 / 72) * FONT_SCALE

        self._setup_window()
        self._setup_styles()
        self._build_menu()
        self._build_status_bar()          # packed first so it stays at the bottom
        self._build_layout()
        self._show_welcome()
        self.root.after(150, self._poll_events)
        self.root.after(250, lambda: self.connect())          # automatic connection
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # --- look ----------------------------------------------------------------------
    def _setup_window(self) -> None:
        self.root.title("SC3020 Project 2: PostgreSQL vs AI query plans")
        self.root.configure(bg=APP_BG)
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(1600, int(screen_w * 0.94)), min(1000, int(screen_h * 0.88))
        self.root.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{max(0, (screen_h - height) // 4)}")
        self.root.minsize(1050, 650)

    def _setup_styles(self) -> None:
        """One light palette on Tk's portable 'clam' theme, so the window looks the
        same on every system (native themes follow macOS dark mode, for example)."""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        f = self.family
        style.configure(".", background=APP_BG, foreground=INK, font=(f, pt(10)),
                        bordercolor=LINE, lightcolor=CARD, darkcolor=LINE,
                        troughcolor=APP_BG, focuscolor=ACCENT, fieldbackground=CARD,
                        selectbackground="#DBEAFE", selectforeground=INK, insertcolor=INK)
        for name, options in {
            "TFrame": {"background": APP_BG},
            "Card.TFrame": {"background": CARD},
            "TLabel": {"background": APP_BG, "foreground": INK},
            "Muted.TLabel": {"background": APP_BG, "foreground": MUTED},
            "Error.TLabel": {"background": APP_BG, "foreground": ERROR_RED,
                             "font": (f, pt(10), "bold")},
            "AppTitle.TLabel": {"background": APP_BG, "foreground": INK,
                                "font": (f, pt(15), "bold")},
            "Status.TLabel": {"background": APP_BG, "foreground": MUTED},
            "CardTitle.TLabel": {"background": CARD, "foreground": INK,
                                 "font": (f, pt(11), "bold")},
            "CardMuted.TLabel": {"background": CARD, "foreground": MUTED},
            "Section.TLabel": {"background": CARD, "foreground": MUTED,
                               "font": (f, pt(9), "bold")},
            "Connected.TLabel": {"background": CARD, "foreground": STATUS_TEXT["same"]},
            "NotConnected.TLabel": {"background": CARD, "foreground": ERROR_RED},
            "TButton": {"background": BUTTON_BG, "foreground": INK, "bordercolor": LINE,
                        "lightcolor": BUTTON_BG, "darkcolor": BUTTON_BG, "padding": (10, 4)},
            "Small.TButton": {"padding": (4, 0), "font": (f, pt(10))},
            "Primary.TButton": {"background": ACCENT, "foreground": "white",
                                "bordercolor": ACCENT, "lightcolor": ACCENT,
                                "darkcolor": ACCENT, "font": (f, pt(11), "bold"),
                                "padding": (10, 8)},
            "TCombobox": {"fieldbackground": CARD, "background": BUTTON_BG, "foreground": INK,
                          "arrowcolor": INK, "bordercolor": LINE, "lightcolor": CARD,
                          "darkcolor": LINE, "padding": 5},
            "TEntry": {"fieldbackground": CARD, "foreground": INK, "padding": 4},
            "Card.TCheckbutton": {"background": CARD, "foreground": INK,
                                  "indicatorbackground": CARD, "indicatorforeground": ACCENT},
            "TScrollbar": {"background": "#CBD2DA", "troughcolor": CARD, "bordercolor": CARD,
                           "lightcolor": "#CBD2DA", "darkcolor": "#CBD2DA",
                           "arrowcolor": MUTED, "gripcount": 0},
            "TPanedwindow": {"background": APP_BG},
            "Sash": {"sashthickness": 10, "gripcount": 0, "background": APP_BG},
            "Horizontal.TProgressbar": {"background": ACCENT, "troughcolor": LINE,
                                        "bordercolor": LINE, "lightcolor": ACCENT,
                                        "darkcolor": ACCENT},
        }.items():
            style.configure(name, **options)
        style.map("TButton", background=[("disabled", BUTTON_BG), ("pressed", "#D3D9E1"),
                                         ("active", "#DDE2E9")],
                  foreground=[("disabled", "#9AA5B1")])
        accent_states = [("disabled", "#9DB7F2"), ("pressed", ACCENT_DARK), ("active", ACCENT_DARK)]
        style.map("Primary.TButton", background=accent_states, lightcolor=accent_states,
                  darkcolor=accent_states, bordercolor=accent_states,
                  foreground=[("disabled", "white")])
        style.map("TCombobox", fieldbackground=[("disabled", APP_BG), ("readonly", CARD)],
                  foreground=[("disabled", MUTED), ("readonly", INK)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)],
                  background=[("active", "#DDE2E9")])
        style.map("Card.TCheckbutton", background=[("active", CARD)],
                  indicatorbackground=[("pressed", CARD), ("active", CARD)])
        style.map("TScrollbar", background=[("active", "#B4BDC8")])
        # The drop-down list of a combobox is a classic Tk listbox.
        for option, value in (("background", CARD), ("foreground", INK),
                              ("selectBackground", "#DBEAFE"), ("selectForeground", INK),
                              ("font", (f, pt(10)))):
            self.root.option_add(f"*TCombobox*Listbox.{option}", value)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Show the example from the project description (offline)",
                              command=self.show_demo)
        file_menu.add_command(label="Open a saved result...", command=self.open_saved)
        file_menu.add_command(label="Save this result...", command=self.save_result)
        file_menu.add_separator()
        file_menu.add_command(label="Connection settings...",
                              command=lambda: self.open_connection_dialog())
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.close)
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="How to use this program",
                              command=lambda: messagebox.showinfo("How to use", HELP_TEXT,
                                                                  parent=self.root))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(16, 0, 16, 8))
        bar.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=int(160 * self.unit))

    # --- layout ------------------------------------------------------------------
    def _build_layout(self) -> None:
        main = ttk.Frame(self.root, padding=(14, 14, 14, 8))
        main.pack(fill="both", expand=True)
        sidebar_w = int(330 * self.unit)
        main.columnconfigure(0, minsize=sidebar_w)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(main)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_sidebar(sidebar, wrap=sidebar_w - int(40 * self.unit))
        self._build_results_area(right)

    def _build_sidebar(self, sidebar: ttk.Frame, wrap: int) -> None:
        ttk.Label(sidebar, text="Query plan comparison", style="AppTitle.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="PostgreSQL's optimizer vs an AI agent",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        # Database
        outer, box = card(sidebar)
        outer.pack(fill="x")
        ttk.Label(box, text="DATABASE", style="Section.TLabel").pack(anchor="w")
        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.database_var = tk.StringVar()
        self.database_box = ttk.Combobox(row, textvariable=self.database_var,
                                         state="readonly", width=10)
        self.database_box.pack(side="left", fill="x", expand=True)
        self.database_box.bind("<<ComboboxSelected>>", self._database_selected)
        ttk.Button(row, text="Settings...", command=lambda: self.open_connection_dialog()
                   ).pack(side="left", padx=(8, 0))
        self.connection_var = tk.StringVar(value="Connecting...")
        self.connection_label = ttk.Label(box, textvariable=self.connection_var,
                                          style="CardMuted.TLabel", wraplength=wrap)
        self.connection_label.pack(anchor="w", pady=(6, 0))

        # Result card: packed at the bottom before the query card, so when the
        # window is short it is the query editor that shrinks, never the result.
        outer, box = card(sidebar)
        outer.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Label(box, text="RESULT", style="Section.TLabel").pack(anchor="w")
        line = ttk.Frame(box, style="Card.TFrame")
        line.pack(fill="x", pady=(2, 0))
        self.rate_var = tk.StringVar(value="–")
        self.rate_label = tk.Label(line, textvariable=self.rate_var, bg=CARD, fg=MUTED,
                                   font=(self.family, pt(30), "bold"))
        self.rate_label.pack(side="left")
        ttk.Label(line, text="difference\nrate", style="CardMuted.TLabel",
                  justify="left").pack(side="left", padx=(10, 0))
        self.verdict_var = tk.StringVar()
        self.verdict_label = tk.Label(box, textvariable=self.verdict_var, bg=CARD, fg=INK,
                                      font=(self.family, pt(11), "bold"), justify="left",
                                      anchor="w", wraplength=wrap)
        self.verdict_label.pack(fill="x")
        self.detail_var = tk.StringVar()
        ttk.Label(box, textvariable=self.detail_var, style="CardMuted.TLabel",
                  wraplength=wrap, justify="left").pack(anchor="w", pady=(4, 0))

        # Query
        outer, box = card(sidebar)
        outer.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(box, text="SQL QUERY", style="Section.TLabel").pack(anchor="w")
        self.example_var = tk.StringVar()
        self.example_box = ttk.Combobox(box, textvariable=self.example_var,
                                        state="readonly", width=10)
        self.example_box.pack(fill="x", pady=(6, 8))
        self.example_box.bind("<<ComboboxSelected>>", self._example_selected)
        editor = tk.Frame(box, bg=CARD, highlightthickness=1, highlightbackground=LINE,
                          highlightcolor=ACCENT)
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        self.sql_text = tk.Text(editor, width=30, height=5, wrap="word", undo=True,
                                font=(mono, pt(10)), bg=CARD, fg=INK, insertbackground=INK,
                                relief="flat", bd=0, padx=8, pady=6, highlightthickness=0,
                                selectbackground="#DBEAFE", selectforeground=INK)
        self.sql_text.pack(fill="both", expand=True)
        for sequence in ("<Control-Return>", "<Command-Return>", "<F5>"):
            try:
                self.sql_text.bind(sequence, self.run_query)
                self.root.bind(sequence, self.run_query)
            except tk.TclError:
                pass                # <Command-...> exists only on macOS
        # The controls below the editor are packed from the bottom up, so the editor
        # takes whatever height is left.
        shortcut = "Cmd+Enter" if self.is_mac else "Ctrl+Enter"
        ttk.Label(box, text=f"Shortcut: {shortcut}",
                  style="CardMuted.TLabel").pack(side="bottom", anchor="w")
        self.fresh_ai = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Ask the AI again (skip its saved answer)",
                        variable=self.fresh_ai, style="Card.TCheckbutton"
                        ).pack(side="bottom", anchor="w", pady=(6, 0))
        self.run_button = ttk.Button(box, text="Compare plans", style="Primary.TButton",
                                     command=self.run_query)
        self.run_button.pack(side="bottom", fill="x", pady=(10, 0))
        editor.pack(fill="both", expand=True)

    def _build_results_area(self, right: ttk.Frame) -> None:
        legend = ttk.Frame(right)
        legend.pack(fill="x", pady=(2, 10))
        for status in ("same", "changed", "postgres_only", "ai_only"):
            tk.Frame(legend, width=int(14 * self.unit), height=int(14 * self.unit),
                     bg=STATUS_FILL[status], highlightthickness=2,
                     highlightbackground=STATUS_COLORS[status]).pack(side="left", padx=(0, 6))
            ttk.Label(legend, text=STATUS_SHORT[status]).pack(side="left", padx=(0, 18))
        ttk.Label(legend, text="Click any box to see its match",
                  style="Muted.TLabel").pack(side="right")

        panes = ttk.Panedwindow(right, orient="vertical")
        panes.pack(fill="both", expand=True)
        trees = ttk.Frame(panes)
        trees.rowconfigure(0, weight=1)
        trees.columnconfigure(0, weight=1, uniform="trees")
        trees.columnconfigure(1, weight=1, uniform="trees")
        self.views = {
            "postgres": PlanView(trees, "postgres", "PostgreSQL", self.family, self.node_clicked),
            "ai": PlanView(trees, "ai", "AI agent", self.family, self.node_clicked)}
        self.views["postgres"].grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.views["ai"].grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        panes.add(trees, weight=3)
        texts = ttk.Frame(panes)
        texts.rowconfigure(0, weight=1)
        texts.columnconfigure(0, weight=3)
        texts.columnconfigure(1, weight=2)
        self.explanation = TextPanel(texts, "Differences", self.family,
                                     hint="click one to see it in both trees")
        self.details = TextPanel(texts, "Selected operator", self.family)
        self.explanation.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(8, 0))
        self.details.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(8, 0))
        panes.add(texts, weight=2)
        self._panes = panes
        self.root.after(400, self._initial_sash)

    def _initial_sash(self) -> None:
        try:
            height = self._panes.winfo_height()
            if height > 200:
                self._panes.sashpos(0, int(height * 0.64))
        except tk.TclError:
            pass

    # --- background work -------------------------------------------------------
    def _start(self, message: str, work: Callable[[], Any], done: Callable[[Any], None],
               failed: Callable[[Exception], None]) -> bool:
        """Run work() in a thread; done(result) or failed(error) then runs in the
        Tk thread. Only one job at a time."""
        if self.busy:
            return False
        self.busy = True
        self._busy_message, self._busy_since = message, time.time()
        self._set_controls(enabled=False)
        self.progress.pack(side="right")
        self.progress.start(12)

        def runner() -> None:
            try:
                self.events.put(("done", done, work()))
            except Exception as exc:          # shown to the user, never lost
                self.events.put(("done", failed, exc))
        threading.Thread(target=runner, daemon=True).start()
        return True

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    self._busy_message = event[1]
                else:
                    _kind, callback, payload = event
                    self._stop_busy()
                    callback(payload)
        except queue.Empty:
            pass
        if self.busy:
            seconds = int(time.time() - self._busy_since)
            self.status_var.set(f"{self._busy_message}   ({seconds} s)")
        try:
            self.root.after(150, self._poll_events)
        except tk.TclError:
            pass                                  # window closed

    def _stop_busy(self) -> None:
        self.busy = False
        self.progress.stop()
        self.progress.pack_forget()
        self._set_controls(enabled=True)

    def _set_controls(self, enabled: bool) -> None:
        self.run_button.configure(state="normal" if enabled else "disabled",
                                  text="Compare plans" if enabled else "Working...")
        for box in (self.database_box, self.example_box):
            box.configure(state="readonly" if enabled else "disabled")

    # --- connection ------------------------------------------------------------
    def connect(self, config: Optional[DBConfig] = None) -> None:
        def work():
            self.session.connect(config)
            try:
                names = self.session.list_databases()
            except PlanError:
                names = [self.session.db_config.dbname]
            return names, self.session.server_version(), collect_examples(self.session.conn)
        self.connection_var.set("Connecting...")
        self._start("Connecting to PostgreSQL...", work, self._connected, self._connect_failed)

    def _connected(self, payload) -> None:
        names, version, examples = payload
        cfg = self.session.db_config
        self.database_box.configure(values=names)
        self.database_var.set(cfg.dbname)
        self.connection_var.set(f"● Connected  ·  PostgreSQL {version.split()[0]}"
                                f"  ·  {cfg.host}:{cfg.port}")
        self.connection_label.configure(style="Connected.TLabel")
        self._set_examples(examples)
        self.status_var.set("Ready. Pick an example query or type your own, "
                            "then press Compare plans.")

    def _connect_failed(self, error: Exception) -> None:
        self.connection_var.set("● Not connected")
        self.connection_label.configure(style="NotConnected.TLabel")
        self.status_var.set("Not connected to PostgreSQL.")
        self.open_connection_dialog(f"Could not connect automatically.\n{error}")

    def open_connection_dialog(self, message: str = "") -> None:
        if not self.busy:
            ConnectionDialog(self, message)

    def _database_selected(self, _event=None) -> None:
        name = self.database_var.get()
        if not name or name == self.session.db_config.dbname:
            return

        def work():
            self.session.use_database(name)
            return self.session.server_version(), collect_examples(self.session.conn)
        if not self._start(f"Switching to database {name}...", work,
                           self._database_switched, self._connect_failed):
            self.database_var.set(self.session.db_config.dbname)

    def _database_switched(self, payload) -> None:
        version, examples = payload
        self.sql_text.delete("1.0", "end")      # the old query belongs to the old schema
        self._connected((list(self.database_box.cget("values")), version, examples))
        self._show_welcome()

    # --- examples --------------------------------------------------------------
    def _set_examples(self, examples: List[Tuple[str, str]]) -> None:
        self.examples = examples
        self.example_box.configure(values=[label for label, _ in examples])
        self.example_var.set(f"Pick one of {len(examples)} example queries..." if examples
                             else "No example queries for this database")
        if examples and not self.sql_text.get("1.0", "end-1c").strip():
            self._put_sql(examples[0][1])

    def _example_selected(self, _event=None) -> None:
        labels = [label for label, _ in self.examples]
        if self.example_var.get() in labels:
            self._put_sql(self.examples[labels.index(self.example_var.get())][1])

    def _put_sql(self, sql: str) -> None:
        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", sql)

    # --- running a query -------------------------------------------------------
    def run_query(self, _event=None):
        if self.busy:
            return "break"
        sql = self.sql_text.get("1.0", "end-1c")
        if not sql.strip():
            self._show_error("Type an SQL query first, or pick one of the example queries.")
            return "break"
        if not self.session.connected:
            self.open_connection_dialog("Connect to a database first.")
            return "break"
        use_cache = not self.fresh_ai.get()

        def work():
            return self.session.analyze(
                sql, use_cache=use_cache,
                on_progress=lambda message: self.events.put(("progress", message)))
        self._start("Getting the plans...", work, self.show_result, self._query_failed)
        return "break"

    def _query_failed(self, error: Exception) -> None:
        if isinstance(error, PlanError):
            self._show_error(str(error))
        else:
            self._show_error(f"Unexpected error: {type(error).__name__}: {error}")

    # --- showing results -------------------------------------------------------
    def _set_result_card(self, rate: str, colour: str, verdict_text: str, detail: str) -> None:
        self.rate_var.set(rate)
        self.rate_label.configure(fg=colour)
        self.verdict_var.set(verdict_text)
        self.verdict_label.configure(fg=ERROR_RED if colour == ERROR_RED else INK)
        self.detail_var.set(detail)

    def _show_welcome(self) -> None:
        self.result = None
        self._set_result_card("–", MUTED, "No comparison yet",
                              "Press Compare plans to see both plans and how they differ.")
        for side, name in (("postgres", "PostgreSQL's plan"), ("ai", "The AI agent's plan")):
            self.views[side].set_subtitle("")
            self.views[side].show(None, message=f"{name} will appear here.")
        self.explanation.clear()
        self.explanation.hint_var.set("")
        self.explanation.add("How to read the results\n", "bold")
        self.explanation.add(
            "Each box in a tree is one operator of a query plan. Data flows upwards, from "
            "the tables at the bottom to the result at the top. The colour of a box shows "
            "how it compares with the other plan (see the legend above the trees).\n\n"
            "The large number on the left is the difference rate: the share of operators "
            "without an identical match. 0% means the two plans are identical.", "muted")
        self.details.clear()
        self.details.add("Click an operator in either tree to see its details here.", "muted")

    def _show_error(self, message: str) -> None:
        self._set_result_card("!", ERROR_RED, "Could not compare this query",
                              message.splitlines()[0])
        self.status_var.set(message.splitlines()[0])
        self.explanation.clear()
        self.explanation.hint_var.set("")
        self.explanation.add("Could not compare this query\n", "error")
        self.explanation.add(message + "\n\n")
        self.explanation.add("Check the SQL, or pick one of the example queries.", "muted")

    def show_result(self, result: Any) -> None:
        """Display an Analysis from project.py (or an object with the same fields)."""
        self.result = result
        comparison = result.comparison
        model = getattr(result, "ai_model", None)
        self.views["postgres"].set_subtitle(f"database {result.database}")
        self.views["ai"].set_subtitle(model or "")
        self.views["postgres"].show(result.pg_plan, comparison)
        self.views["ai"].show(result.ai_plan, comparison,
                              message="The AI agent did not return a plan.\n\n"
                                      + (getattr(result, "ai_error", "") or ""))
        if comparison is not None:
            m, rate = comparison.metrics, comparison.difference_rate
            colour = (STATUS_TEXT["same"] if rate < 0.2 else
                      STATUS_TEXT["changed"] if rate < 0.5 else ERROR_RED)
            self._set_result_card(
                f"{rate:.0%}", colour, verdict(rate).capitalize() + ".",
                f"{m['identical_pairs']} of PostgreSQL's {m['pg_nodes']} operators have an "
                f"identical match among the AI's {m['ai_nodes']}.")
            self.status_var.set("Done. Click a difference or an operator to see it "
                                "in both trees.")
        else:
            error = getattr(result, "ai_error", "") or ""
            self._set_result_card("–", ERROR_RED, "The AI plan is unavailable",
                                  error.splitlines()[0] if error else "")
            self.status_var.set("PostgreSQL's plan is shown. The AI agent did not answer.")
        self._write_explanation(result)
        self.details.clear()
        self.details.add("Click an operator in either tree, or a difference on the left, "
                         "to see the details here.", "muted")

    def _write_explanation(self, result: Any) -> None:
        panel, comparison = self.explanation, result.comparison
        panel.clear()
        if comparison is None:
            panel.hint_var.set("")
            panel.add("The AI agent did not return a plan\n", "error")
            panel.add((getattr(result, "ai_error", "") or "") + "\n\n")
            panel.add("What to try: check GEMINI_API_KEY in the .env file and the internet "
                      "connection, or wait a minute if the free quota is used up, then "
                      "press Compare plans again. PostgreSQL's plan is complete.", "muted")
            return

        if comparison.identical:
            panel.hint_var.set("")
            panel.add("The AI agent proposed exactly the same plan as PostgreSQL.\n", "bold")
        else:
            panel.hint_var.set(f"{len(comparison.differences)} found · "
                               f"click one to see it in both trees")
            for i, difference in enumerate(comparison.differences, 1):
                panel.add_link([(f"{i}.  ", ("muted",)), (difference.category, ("accent",)),
                                ("   " + difference.text, ())],
                               lambda d=difference: self.difference_clicked(d))
        if comparison.similarities:
            panel.add("\nWhat is the same\n", "h")
            for line in comparison.similarities:
                panel.add(f"• {line}\n")

        panel.add("\nJoin order\n", "h")
        panel.add(f"PostgreSQL  {comparison.join_order.get('postgres', '')}\n", "mono")
        panel.add(f"AI agent    {comparison.join_order.get('ai', '')}\n", "mono")
        panel.add("HJ = Hash Join, NL = Nested Loop, MJ = Merge Join; "
                  "[ ] = subqueries run separately.\n", "muted")

        m = comparison.metrics
        panel.add("\nMeasures\n", "h")
        for label, value, meaning in (
                ("Difference rate", percent(m.get("difference_rate")),
                 "share of operators without an identical match"),
                ("Tree edit distance rate", percent(m.get("ted_rate")),
                 "order-sensitive cross-check"),
                ("Access method agreement", percent(m.get("access_method_agreement")),
                 "same scan type for each table"),
                ("Join algorithm agreement", percent(m.get("join_algorithm_agreement")),
                 "same algorithm for each join"),
                ("Join order agreement", percent(m.get("join_order_agreement")),
                 "same intermediate join results"),
                ("Median q-error", "n/a" if m.get("median_q_error") is None
                 else f"{m['median_q_error']:.2f}",
                 "row estimates apart (1 = equal)")):
            panel.add(f"{label}: ", "bold")
            panel.add(f"{value}  ")
            panel.add(f"{meaning}\n", "muted")

    # --- clicking ---------------------------------------------------------------
    def _highlight(self, pg_path: Optional[str], ai_path: Optional[str]) -> None:
        self.views["postgres"].highlight(pg_path)
        self.views["ai"].highlight(ai_path)

    def node_clicked(self, side: str, path: str) -> None:
        if self.result is None:
            return
        comparison = self.result.comparison
        partner = None
        if comparison is not None:
            try:
                partner = comparison.partner(side, path)
            except KeyError:
                partner = None
        self._highlight(path if side == "postgres" else partner,
                        path if side == "ai" else partner)
        self._show_details(side, path, partner)

    def difference_clicked(self, difference) -> None:
        self._highlight(difference.pg_path, difference.ai_path)
        if difference.pg_path is not None:
            self._show_details("postgres", difference.pg_path, difference.ai_path, lead=difference)
        elif difference.ai_path is not None:
            self._show_details("ai", difference.ai_path, None, lead=difference)
        else:
            self.details.clear()
            self.details.add(f"{difference.category}\n", "accent")
            self.details.add(difference.text + "\n\n")
            self.details.add("This finding is about the plan as a whole, "
                             "not about a single operator.", "muted")

    def _show_details(self, side: str, path: str, partner: Optional[str], lead=None) -> None:
        plan = self.result.pg_plan if side == "postgres" else self.result.ai_plan
        other_plan = self.result.ai_plan if side == "postgres" else self.result.pg_plan
        try:
            node = node_at(plan, path)
        except Exception:
            return
        panel, comparison = self.details, self.result.comparison
        owner = "PostgreSQL's plan" if side == "postgres" else "the AI agent's plan"
        other = "the AI agent's plan" if side == "postgres" else "PostgreSQL's plan"
        panel.clear()
        if lead is not None:
            panel.add(f"{lead.category}\n", "accent")
            panel.add(lead.text + "\n\n")
        panel.add(f"{node_title(node)}", "bold")
        panel.add(f"   in {owner}\n", "muted")
        if comparison is not None:
            try:
                status = comparison.status(side, path)
                panel.add(f"■ {STATUS_SHORT[status]}\n", "st_" + status)
            except KeyError:
                pass
            if partner is not None and other_plan is not None:
                try:
                    panel.add(f"Matches {node_title(node_at(other_plan, partner))} in {other} "
                              f"(framed in purple).\n", "muted")
                except Exception:
                    pass
            else:
                panel.add(f"No matching operator in {other}.\n", "muted")

        facts = [("Table", node.relation), ("Index", node.index_name),
                 ("WITH query", node.extra.get("CTE Name")),
                 ("Join type", node.join_type), ("Strategy", node.extra.get("Strategy")),
                 ("Estimated rows", f"{node.rows:,.0f}" if node.rows is not None else None),
                 ("Estimated cost", f"{node.cost:,.2f}" if node.cost is not None else None),
                 ("Subquery", node.extra.get("Parent Relationship")
                  if node.extra.get("Parent Relationship") in ("InitPlan", "SubPlan") else None),
                 ("The AI wrote", node.raw_type if side == "ai" and node.raw_type
                  and node.raw_type != node.node_type else None)]
        facts = [(label, value) for label, value in facts if value]
        if facts or node.conditions or node.keys:
            panel.add("\nDetails\n", "h")
        for label, value in facts:
            panel.add(f"{label}: ", "bold")
            panel.add(f"{value}\n")
        for label, condition in node.conditions.items():
            panel.add(f"{label}: ", "bold")
            panel.add(f"{condition}\n", "mono")
        if node.keys:
            panel.add("Keys: ", "bold")
            panel.add(", ".join(node.keys) + "\n", "mono")
        if node.extra.get("AI Reason"):
            panel.add("\nThe AI's reason\n", "h")
            panel.add(f"{node.extra['AI Reason']}\n")
        if node.extra.get("Warning"):
            panel.add("\nProblem in the AI's answer\n", "h")
            panel.add(f"{node.extra['Warning']}\n", "error")
        if comparison is not None:
            try:
                notes = comparison.notes(side, path)
            except KeyError:
                notes = []
            if notes:
                panel.add("\nWhy it has this colour\n", "h")
                for note in notes:
                    panel.add(f"• {note}\n")

    # --- File menu ---------------------------------------------------------------
    def show_demo(self) -> None:
        """Figures 1 and 2 of the project description; needs no database or key."""
        if self.busy:
            return
        pg_plan, ai_plan = demo_plans()
        self._put_sql(DEMO_SQL)
        self.show_result(SimpleNamespace(
            sql=DEMO_SQL, database="(project description)",
            pg_plan=pg_plan, ai_plan=ai_plan, ai_error=None,
            ai_model="Gemini, Figure 2 of the project description",
            comparison=compare_plans(pg_plan, ai_plan)))
        self.status_var.set("Showing the example from the project description "
                            "(no database or API key needed).")

    def open_saved(self) -> None:
        """Open a result saved by 'Save this result' or by project.py --evaluate."""
        if self.busy:
            return
        start = RESULTS_DIR if os.path.isdir(RESULTS_DIR) else HERE
        filename = filedialog.askopenfilename(
            parent=self.root, title="Open a saved result", initialdir=start,
            filetypes=[("Saved results", "*.json"), ("All files", "*")])
        if not filename:
            return
        try:
            with open(filename, encoding="utf-8") as f:
                saved = json.load(f)
            pg_plan = plan_from_dict(saved["pg_plan"])
            ai_plan = plan_from_dict(saved["ai_plan"]) if saved.get("ai_plan") else None
            result = SimpleNamespace(
                sql=saved.get("sql", ""), database=saved.get("database") or "saved result",
                pg_plan=pg_plan, ai_plan=ai_plan, ai_model=saved.get("ai_model"),
                ai_error=saved.get("ai_error") or saved.get("error"),
                comparison=compare_plans(pg_plan, ai_plan) if ai_plan is not None else None)
        except (OSError, ValueError, KeyError, TypeError, PlanError) as exc:
            messagebox.showerror("Could not open the file",
                                 f"This file is not a saved result.\n\n{exc}", parent=self.root)
            return
        if result.sql:
            self._put_sql(result.sql)
        self.show_result(result)
        self.status_var.set(f"Showing the saved result {os.path.basename(filename)}.")

    def save_result(self) -> None:
        if self.result is None:
            messagebox.showinfo("Nothing to save", "Compare a query first.", parent=self.root)
            return
        filename = filedialog.asksaveasfilename(
            parent=self.root, title="Save this result", defaultextension=".json",
            initialfile="plan_comparison.json", filetypes=[("JSON file", "*.json")])
        if not filename:
            return
        result = self.result
        if hasattr(result, "to_dict"):
            data = result.to_dict()
        else:
            data = {"sql": result.sql, "database": result.database,
                    "pg_plan": result.pg_plan.to_dict(),
                    "ai_plan": result.ai_plan.to_dict() if result.ai_plan else None,
                    "ai_model": result.ai_model, "ai_error": result.ai_error,
                    "comparison": result.comparison.to_dict() if result.comparison else None}
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self.root)
            return
        self.status_var.set(f"Saved to {os.path.basename(filename)}.")

    def close(self) -> None:
        if not self.busy:
            try:
                self.session.close()
            except Exception:
                pass
        self.root.destroy()


def _sharp_text_on_windows() -> None:
    """Without this, Windows scales the whole window up as a bitmap on high-DPI
    screens and the text looks blurry."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def run_gui(session: Any) -> None:
    """Entry point called by project.py. Blocks until the window is closed."""
    _sharp_text_on_windows()
    root = tk.Tk()
    App(root, session)
    root.mainloop()
