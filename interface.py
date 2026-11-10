"""SC3020 Project 2 -- GUI and QEP visualization (submission release).

Two complementary views are provided: a graphical, scrollable QEP node-link tree
and an accessible, expandable outline. Both support colour-coded differences,
selection of matching nodes, and textual explanations.
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from comparison import STATUS_COLORS, STATUS_LABELS, compare_plans, demo_plans, iter_nodes, plan_from_dict
from preprocessing import DBConfig, PlanError

# Dark text on soft status-coloured backgrounds keeps the labels readable.
ROW_BACKGROUNDS = {
    "same": "#DFF2DE",
    "changed": "#FFF0D2",
    "postgres_only": "#FDE1E1",
    "ai_only": "#DDEEFF",
}
BG = "#F5F7FA"

# Figure 1 / Figure 2 demonstration query (offline mode is never executed).
DEMO_SQL = (
    "SELECT 100.00 * SUM(CASE WHEN p_type LIKE 'PROMO%' "
    "THEN l_extendedprice * (1 - l_discount) ELSE 0 END) "
    "/ SUM(l_extendedprice * (1 - l_discount)) AS promo_revenue\n"
    "FROM lineitem, part\n"
    "WHERE l_partkey = p_partkey\n"
    "  AND l_shipdate >= DATE '1993-11-01'\n"
    "  AND l_shipdate < DATE '1993-11-01' + INTERVAL '1 month';"
)


class PlanDiagram(ttk.Frame):
    """Scroll-and-zoom QEP diagram with clickable, color-coded nodes.

    The layout is deliberately based on *structure* (not table names or SQL text),
    so the same widget works with arbitrary PostgreSQL schemas and AI plans.
    The parent handles matching a selected node against the other QEP.
    """

    NODE_W = 186
    NODE_H = 58
    LEAF_GAP = 36
    LEVEL_GAP = 55
    MARGIN = 35

    def __init__(self, parent: ttk.Frame, on_node_click) -> None:
        super().__init__(parent)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg="#FFFFFF", highlightthickness=0,
                                takefocus=True)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<MouseWheel>", self._wheel)  # Windows and macOS
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(2, "units"))
        self.canvas.bind("<Shift-MouseWheel>", self._wheel_horizontal)
        self.callback = on_node_click
        self.plan = None
        self.comparison = None
        self.side = "postgres"
        self.zoom = 1.0
        self.selected_path = None
        self.node_rectangles = {}
        self.node_centres = {}
        self.item_to_path = {}
        self.total_width = 1
        self.total_height = 1

    def _wheel(self, event) -> None:
        step = 1 if event.delta > 0 else -1
        self.canvas.yview_scroll(-3 * step, "units")

    def _wheel_horizontal(self, event) -> None:
        step = 1 if event.delta > 0 else -1
        self.canvas.xview_scroll(-3 * step, "units")

    def _click(self, event) -> None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            path = self.item_to_path.get(item)
            if path is not None:
                self.callback(path)
                return

    @staticmethod
    def _shorten(value, length=26) -> str:
        """Give every node a compact label even with long arbitrary table names."""
        text = " ".join(str(value or "").split())
        return text if len(text) <= length else text[:length - 1] + "…"

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.55, min(1.6, zoom))
        if self.plan is not None:
            self.render(self.plan, self.comparison, self.side,
                        selected_path=self.selected_path)

    def render(self, plan, comparison, side: str,
               selected_path: Optional[str] = None) -> None:
        """Layout and draw the entire plan in operator order, edges first."""
        self.plan, self.comparison, self.side = plan, comparison, side
        self.selected_path = selected_path
        self.canvas.delete("all")
        self.node_rectangles.clear()
        self.node_centres.clear()
        self.item_to_path.clear()
        if plan is None:
            self.canvas.create_text(25, 30, text="AI plan unavailable. See explanation below.",
                                    font=("Arial", 12), anchor="nw", fill="#67788A")
            self.canvas.configure(scrollregion=(0, 0, 460, 100))
            return

        scale = self.zoom
        node_w, node_h = self.NODE_W * scale, self.NODE_H * scale
        slot = (self.NODE_W + self.LEAF_GAP) * scale
        step_y = (self.NODE_H + self.LEVEL_GAP) * scale
        margin = self.MARGIN * scale
        next_leaf = [0]
        nodes = {}
        centres = {}
        depths = {}

        def place(node, path, depth):
            children = []
            for i, child in enumerate(node.children):
                child_path = f"{path}.{i}"
                children.append(place(child, child_path, depth + 1))
            if children:
                x = (children[0] + children[-1]) / 2.0
            else:
                x = margin + node_w / 2 + next_leaf[0] * slot
                next_leaf[0] += 1
            centres[path] = (x, margin + depth * step_y)
            nodes[path] = node
            depths[path] = depth
            return x

        root_x = place(plan, "0", 0)
        width = max(2 * margin + node_w, 2 * margin +
                    (max(next_leaf[0], 1) - 1) * slot + node_w)
        height = 2 * margin + (max(depths.values()) * step_y) + node_h
        self.total_width, self.total_height = width, height
        self.node_centres = centres

        # Edges precede boxes so that they cannot cover any labels.
        for parent_path, node in nodes.items():
            px, py = centres[parent_path]
            for i, _child in enumerate(node.children):
                cx, cy = centres[f"{parent_path}.{i}"]
                mid = (py + node_h + cy) / 2.0
                self.canvas.create_line(px, py + node_h, px, mid, cx, mid,
                                        cx, cy - 2, fill="#8394A5", width=1.6 * scale,
                                        arrow="last", arrowshape=(7 * scale,
                                                                  8 * scale, 3 * scale))

        for path, node in nodes.items():
            x, y = centres[path]
            if comparison is not None:
                status = comparison.status(side, path)
                colour = ROW_BACKGROUNDS.get(status, "#FFFFFF")
                stroke = STATUS_COLORS.get(status, "#8492A6")
            else:
                status, colour, stroke = None, "#EFF3F9", "#9AACBE"
            active = path == selected_path
            if active:
                stroke = "#4D3BDB"
            rect = self.canvas.create_rectangle(x - node_w / 2, y,
                                                x + node_w / 2, y + node_h,
                                                fill=colour, outline=stroke,
                                                width=3.7 if active else 2.0)
            self.node_rectangles[path] = (rect, stroke)
            main = self.canvas.create_text(x, y + node_h * 0.30,
                                           text=self._shorten(node.node_type),
                                           fill="#17202A", font=("Arial", max(8, int(10 * scale)), "bold"),
                                           width=node_w - 12 * scale, justify="center")
            secondary = node.relation or node.index_name or node.join_type
            if secondary:
                subtitle = self._shorten(secondary)
            elif node.rows is not None:
                subtitle = "Est. rows: " + self._shorten(node.rows, 14)
            else:
                subtitle = "AI operator" if side == "ai" else "PostgreSQL operator"
            sub = self.canvas.create_text(x, y + node_h * 0.69,
                                          text=subtitle, fill="#465668",
                                          font=("Arial", max(7, int(9 * scale))),
                                          width=node_w - 12 * scale, justify="center")
            for item in (rect, main, sub):
                self.item_to_path[item] = path

        self.canvas.configure(scrollregion=(0, 0, width, height))
        self.canvas.update_idletasks()
        if selected_path and selected_path in centres:
            self.focus(selected_path)
        else:
            self._scroll_to(root_x, margin + node_h / 2)

    def _scroll_to(self, x, y) -> None:
        view_width = max(1, self.canvas.winfo_width())
        view_height = max(1, self.canvas.winfo_height())
        if self.total_width > view_width:
            target = min(1, max(0, (x - view_width / 2) /
                                 (self.total_width - view_width)))
            self.canvas.xview_moveto(target)
        else:
            self.canvas.xview_moveto(0)
        if self.total_height > view_height:
            target = min(1, max(0, (y - view_height / 2) /
                                 (self.total_height - view_height)))
            self.canvas.yview_moveto(target)
        else:
            self.canvas.yview_moveto(0)

    def focus(self, path: Optional[str]) -> None:
        """Highlight a chosen node and scroll it into view."""
        previous = self.selected_path
        self.selected_path = path
        if previous in self.node_rectangles:
            rectangle, base_stroke = self.node_rectangles[previous]
            self.canvas.itemconfigure(rectangle, outline=base_stroke, width=2)
        if path in self.node_rectangles:
            rectangle, _base_stroke = self.node_rectangles[path]
            self.canvas.itemconfigure(rectangle, outline="#4D3BDB", width=4)
            x, y = self.node_centres[path]
            self._scroll_to(x, y + self.NODE_H * self.zoom / 2)


class PlanExplorer:
    def __init__(self, root: tk.Tk, session: Any) -> None:
        self.root = root
        self.session = session
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.busy = False
        self.result = None
        self.paths: Dict[str, Dict[str, str]] = {"postgres": {}, "ai": {}}
        self.difference_items = []
        self._syncing_selection = False
        # The cross-platform Clam theme respects per-row Treeview colours,
        # including on systems where native themes ignore background tags.
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=23)
        self.root.title("SC3020  |  QEP Comparison Explorer")
        self.root.geometry("1280x900")
        self.root.minsize(890, 630)
        self.root.configure(bg=BG)
        self._build_ui()
        self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        ttk.Label(main, text="Query Execution Plan Comparison", font=("Arial", 19, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        connection = ttk.LabelFrame(main, text="1  PostgreSQL connection", padding=9)
        connection.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        cfg = self.session.db_config
        self.host = tk.StringVar(value=cfg.host)
        self.port = tk.StringVar(value=str(cfg.port))
        self.database = tk.StringVar(value=cfg.dbname)
        self.user = tk.StringVar(value=cfg.user)
        self.password = tk.StringVar(value=cfg.password)
        items = [("Host", self.host, 18), ("Port", self.port, 7),
                 ("Database", self.database, 18), ("User", self.user, 14),
                 ("Password", self.password, 15)]
        for col, (name, var, width) in enumerate(items):
            group = ttk.Frame(connection)
            group.grid(row=0, column=col, padx=(0, 9), sticky="w")
            ttk.Label(group, text=name).pack(anchor="w")
            if name == "Database":
                self.db_picker = ttk.Combobox(group, textvariable=var, width=width)
                self.db_picker.pack(anchor="w")
                self.db_picker.bind("<<ComboboxSelected>>", self._switch_database)
            else:
                ttk.Entry(group, textvariable=var, width=width,
                          show="*" if name == "Password" else "").pack(anchor="w")
        self.connect_btn = ttk.Button(connection, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=len(items), sticky="s", pady=(0, 1))
        connection.columnconfigure(len(items), weight=1)

        query_frame = ttk.LabelFrame(main, text="2  SQL query", padding=9)
        query_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        query_frame.columnconfigure(0, weight=1)
        self.sql = tk.Text(query_frame, height=3, wrap="word", font=("Consolas", 11),
                           undo=True, padx=8, pady=6)
        self.sql.grid(row=0, column=0, columnspan=5, sticky="ew")
        self.sql.insert("1.0", "SELECT 1;")  # schema-independent starter query
        bar = ttk.Frame(query_frame)
        bar.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.run_btn = ttk.Button(bar, text="Run comparison", command=self._run_query)
        self.run_btn.pack(side="left")
        self.refresh_ai = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Refresh AI response (ignore cache)",
                        variable=self.refresh_ai).pack(side="left", padx=12)
        ttk.Button(bar, text="Offline demo", command=self._show_demo).pack(side="left")
        ttk.Button(bar, text="Open saved result...", command=self._open_saved).pack(side="left", padx=(8, 0))
        self.export_btn = ttk.Button(bar, text="Save result...", command=self._export_result,
                                     state="disabled")
        self.export_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="Not connected. Connect to PostgreSQL, or try Offline demo.")
        ttk.Label(query_frame, textvariable=self.status, wraplength=1100,
                  foreground="#44576A").grid(row=2, column=0, columnspan=5,
                                               sticky="w", pady=(5, 0))

        results = ttk.Frame(main)
        results.grid(row=3, column=0, sticky="nsew")
        results.rowconfigure(2, weight=1)
        results.columnconfigure(0, weight=1)
        ttk.Label(results, text="3  Results", font=("Arial", 12, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 3))
        self.summary = tk.StringVar(value="Run a query or load the demo to compare execution plans.")
        ttk.Label(results, textvariable=self.summary).grid(row=1, column=0, sticky="w", pady=(0, 6))
        panes = ttk.Panedwindow(results, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew")
        self.main_panes = panes
        trees_panel = ttk.Frame(panes)
        trees_panel.rowconfigure(0, weight=1)
        trees_panel.columnconfigure(0, weight=1)
        trees_panel.columnconfigure(1, weight=1)
        self.trees: Dict[str, ttk.Treeview] = {}
        for col, (side, title) in enumerate((("postgres", "PostgreSQL optimizer"),
                                             ("ai", "AI agent"))):
            panel = ttk.LabelFrame(trees_panel, text=title, padding=5)
            panel.grid(row=0, column=col, sticky="nsew", padx=(0, 5) if col == 0 else (5, 0))
            panel.rowconfigure(0, weight=1)
            panel.columnconfigure(0, weight=1)
            tree = ttk.Treeview(panel, columns=("rows", "cost"), show="tree headings", height=14)
            tree.heading("#0", text="Operator / Relation")
            tree.heading("rows", text="Est. rows")
            tree.heading("cost", text="Est. cost")
            tree.column("#0", width=300, minwidth=180, stretch=True)
            tree.column("rows", width=78, minwidth=60, stretch=False, anchor="e")
            tree.column("cost", width=78, minwidth=60, stretch=False, anchor="e")
            for key, color in ROW_BACKGROUNDS.items():
                tree.tag_configure(key, background=color, foreground="#17202A")
            tree.grid(row=0, column=0, sticky="nsew")
            scroll_y = ttk.Scrollbar(panel, orient="vertical", command=tree.yview)
            scroll_y.grid(row=0, column=1, sticky="ns")
            scroll_x = ttk.Scrollbar(panel, orient="horizontal", command=tree.xview)
            scroll_x.grid(row=1, column=0, sticky="ew")
            tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
            tree.bind("<<TreeviewSelect>>", lambda _e, s=side: self._selected_node(s))
            self.trees[side] = tree
        visual_tabs = ttk.Notebook(panes)
        diagram_page = ttk.Frame(visual_tabs, padding=4)
        diagram_page.rowconfigure(1, weight=1)
        diagram_page.columnconfigure(0, weight=1)
        diagram_page.columnconfigure(1, weight=1)
        control_row = ttk.Frame(diagram_page)
        control_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(control_row, text="Graphical QEP trees   •   click nodes to inspect matches   •   scroll to navigate").pack(side="left")
        ttk.Label(control_row, text="Zoom:").pack(side="right", padx=(6, 3))
        self.zoom_choice = tk.StringVar(value="100%")
        zoom_picker = ttk.Combobox(control_row, textvariable=self.zoom_choice,
                                   values=("60%", "80%", "100%", "125%", "150%"),
                                   width=6, state="readonly")
        zoom_picker.pack(side="right")
        zoom_picker.bind("<<ComboboxSelected>>", self._change_zoom)
        self.diagrams = {}
        for col, (side, title) in enumerate((("postgres", "PostgreSQL optimizer"),
                                             ("ai", "AI agent"))):
            frame = ttk.LabelFrame(diagram_page, text=title, padding=4)
            frame.grid(row=1, column=col, sticky="nsew", padx=(0, 5) if col == 0 else (5, 0))
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            diagram = PlanDiagram(frame, lambda path, s=side: self._diagram_node_clicked(s, path))
            diagram.grid(row=0, column=0, sticky="nsew")
            self.diagrams[side] = diagram
        visual_tabs.add(diagram_page, text="Visual QEP trees")
        visual_tabs.add(trees_panel, text="Detailed outline")
        self.visual_tabs = visual_tabs
        panes.add(visual_tabs, weight=3)

        detail_panel = ttk.Frame(panes)
        detail_panel.rowconfigure(0, weight=1)
        detail_panel.columnconfigure(0, weight=1)
        self.tabs = ttk.Notebook(detail_panel)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        explain_tab = ttk.Frame(self.tabs, padding=4)
        explain_tab.rowconfigure(1, weight=1)
        explain_tab.columnconfigure(0, weight=1)
        ttk.Label(explain_tab, text="Click a difference to locate its nodes in both trees.").grid(
            row=0, column=0, sticky="w")
        explain_split = ttk.Panedwindow(explain_tab, orient="horizontal")
        explain_split.grid(row=1, column=0, sticky="nsew")
        list_panel = ttk.Frame(explain_split)
        list_panel.rowconfigure(0, weight=1)
        list_panel.columnconfigure(0, weight=1)
        self.diff_list = tk.Listbox(list_panel, height=8, exportselection=False)
        self.diff_list.grid(row=0, column=0, sticky="nsew")
        self.diff_list.bind("<<ListboxSelect>>", self._selected_difference)
        dy = ttk.Scrollbar(list_panel, orient="vertical", command=self.diff_list.yview)
        dy.grid(row=0, column=1, sticky="ns")
        self.diff_list.configure(yscrollcommand=dy.set)
        explain_split.add(list_panel, weight=1)
        self.explain = self._text_panel(explain_split)
        explain_split.add(self.explain.master, weight=2)
        self.tabs.add(explain_tab, text="Comparison & explanations")

        details_tab = ttk.Frame(self.tabs, padding=4)
        details_tab.rowconfigure(0, weight=1)
        details_tab.columnconfigure(0, weight=1)
        self.details = self._text_panel(details_tab)
        self.details.master.grid(row=0, column=0, sticky="nsew")
        self.tabs.add(details_tab, text="Selected node details")
        panes.add(detail_panel, weight=2)
        # Without an initial sash position, Tk may allocate only two tree rows.
        # Prefer a tall QEP visualization while keeping explanations scrollable.
        self.root.after(150, self._set_initial_pane)

        legend = ttk.Frame(main)
        legend.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(legend, text="Legend:").pack(side="left")
        for status, name in STATUS_LABELS.items():
            tk.Label(legend, text="  " + name + "  ", background=ROW_BACKGROUNDS[status],
                     foreground="#17202A", padx=4, pady=3).pack(side="left", padx=4)

    def _set_initial_pane(self) -> None:
        try:
            height = self.main_panes.winfo_height()
            if height > 160:
                self.main_panes.sashpos(0, int(height * 0.72))
        except tk.TclError:
            pass  # the window may have closed during startup

    @staticmethod
    def _text_panel(parent):
        holder = ttk.Frame(parent)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        text = tk.Text(holder, state="disabled", wrap="word", font=("Arial", 10), padx=9, pady=8)
        text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(holder, command=text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=sb.set)
        return text

    @staticmethod
    def _write_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_btn.configure(state=state)
        self.connect_btn.configure(state=state)
        self.db_picker.configure(state=state)
        if message:
            self.status.set(message)

    def _worker(self, operation, *args) -> None:
        """Do blocking database/API work off the Tk thread."""
        try:
            outcome = operation(*args)
            self.events.put(("result", outcome))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.events.put(("finished", None))

    def _launch(self, operation, message: str, *args) -> None:
        if self.busy:
            return
        self._set_busy(True, message)
        threading.Thread(target=self._worker, args=(operation, *args), daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "progress":
                    self.status.set(str(data))
                elif kind == "result":
                    task, payload = data
                    if task == "connect":
                        dbs = payload
                        self.db_picker.configure(values=dbs)
                        self.database.set(self.session.db_config.dbname)
                        self.status.set(f"Connected to {self.database.get()}.")
                    elif task == "analysis":
                        self._display(payload)
                    elif task == "database":
                        self.status.set(f"Switched to {self.database.get()}.")
                elif kind == "error":
                    self.status.set("Operation failed.")
                    messagebox.showerror("SC3020 QEP Explorer", str(data))
                elif kind == "finished":
                    self._set_busy(False)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._drain_queue)

    def _connect(self) -> None:
        config = DBConfig(host=self.host.get().strip(), port=self.port.get().strip(),
                          dbname=self.database.get().strip(), user=self.user.get().strip(),
                          password=self.password.get())
        def work():
            self.session.connect(config)
            try:
                names = self.session.list_databases()
            except PlanError:
                # Browsing pg_database can be restricted on some installations.
                # The already-established connection is still usable.
                names = [config.dbname]
            return ("connect", names)
        self._launch(work, "Connecting to PostgreSQL...")

    def _switch_database(self, _event=None) -> None:
        if self.busy or not self.session.connected:
            return
        name = self.database.get().strip()
        if name == self.session.db_config.dbname:
            return
        self._launch(lambda: (self.session.use_database(name), ("database", None))[1],
                     f"Switching to {name}...")

    def _run_query(self) -> None:
        if not self.session.connected:
            messagebox.showwarning("Connect first", "Connect to PostgreSQL first, or try Offline demo.")
            return
        # Do not silently analyze an old database after the user edits the form.
        cfg = self.session.db_config
        current = (self.host.get().strip(), self.port.get().strip(),
                   self.database.get().strip(), self.user.get().strip(), self.password.get())
        connected_to = (str(cfg.host), str(cfg.port), str(cfg.dbname),
                        str(cfg.user), str(cfg.password))
        if current != connected_to:
            messagebox.showwarning("Connection settings changed",
                                   "Click Connect to apply the updated database settings first.")
            return
        sql = self.sql.get("1.0", "end-1c")
        if not sql.strip():
            messagebox.showwarning("Missing SQL", "Enter one SQL query.")
            return
        use_cache = not self.refresh_ai.get()
        def work():
            data = self.session.analyze(
                sql, use_cache=use_cache,
                on_progress=lambda msg: self.events.put(("progress", msg)))
            return ("analysis", data)
        self._launch(work, "Getting query execution plans...")

    def _show_demo(self) -> None:
        if self.busy:
            return
        pg, ai = demo_plans()
        comparison = compare_plans(pg, ai)
        self.sql.delete("1.0", "end")
        self.sql.insert("1.0", DEMO_SQL)
        data = SimpleNamespace(pg_plan=pg, ai_plan=ai, comparison=comparison,
                               ai_model="Example from assignment", database="Demo",
                               explanation_text=comparison.explanation_text)
        self._display(data)
        self.status.set("Offline demo loaded; database and API key not required.")

    def _open_saved(self) -> None:
        if self.busy:
            return
        folder = Path(__file__).resolve().parent / "results" / "queries"
        filename = filedialog.askopenfilename(
            title="Choose a saved query result", initialdir=str(folder if folder.exists() else Path.cwd()),
            filetypes=[("JSON result files", "*.json"), ("All files", "*")])
        if not filename:
            return
        try:
            with open(filename, encoding="utf-8") as f:
                content = json.load(f)
            pg = plan_from_dict(content["pg_plan"])
            ai_raw = content.get("ai_plan")
            ai = plan_from_dict(ai_raw) if ai_raw else None
            comparison = compare_plans(pg, ai) if ai is not None else None
            explanation = (comparison.explanation_text if comparison else
                           lambda: "No AI plan in this saved result. " + str(content.get("ai_error") or ""))
            data = SimpleNamespace(pg_plan=pg, ai_plan=ai, comparison=comparison,
                                   ai_model=content.get("ai_model"),
                                   database=content.get("database", "Saved result"),
                                   explanation_text=explanation)
            if content.get("sql"):
                self.sql.delete("1.0", "end")
                self.sql.insert("1.0", content["sql"])
            self._display(data)
            self.status.set(f"Loaded saved result: {Path(filename).name}")
        except (KeyError, ValueError, TypeError, OSError, PlanError) as exc:
            messagebox.showerror("Invalid result file", str(exc))

    def _display(self, data: Any) -> None:
        self.result = data
        self.export_btn.configure(state="normal")
        self.paths = {"postgres": {}, "ai": {}}
        comp = data.comparison
        self._syncing_selection = True
        try:
            for side, plan in (("postgres", data.pg_plan), ("ai", data.ai_plan)):
                tree = self.trees[side]
                tree.delete(*tree.get_children())
                if plan is None:
                    continue
                for path, node in iter_nodes(plan):
                    parent_path = path.rpartition(".")[0] if "." in path else None
                    parent_id = self.paths[side].get(parent_path, "") if parent_path else ""
                    label = node.node_type + (f"  [{node.relation}]" if node.relation else "")
                    if node.index_name:
                        label += f"  ({node.index_name})"
                    status = comp.status(side, path) if comp is not None else None
                    item_id = tree.insert(parent_id, "end", text=label,
                                          values=(self._fmt(node.rows), self._fmt(node.cost)),
                                          tags=(status,) if status else (), open=True)
                    self.paths[side][path] = item_id
        finally:
            self._syncing_selection = False
        for side, plan in (("postgres", data.pg_plan), ("ai", data.ai_plan)):
            self.diagrams[side].render(plan, comp, side)
        if comp is not None:
            metrics = comp.metrics
            self.summary.set(
                f"Database: {data.database}     Difference rate: {comp.difference_rate:.1%}"
                f"     PostgreSQL nodes: {metrics.get('pg_nodes', '?')}"
                f"     AI nodes: {metrics.get('ai_nodes', '?')}"
                f"     AI model: {data.ai_model or 'Unknown'}")
        else:
            self.summary.set(f"Database: {data.database}     PostgreSQL plan only (AI unavailable)")
        self._write_text(self.explain, data.explanation_text())
        self.tabs.select(0)  # make difference explanations visible on each new result
        self._write_text(self.details, "Click a node in either tree to view details and matching-node notes.")
        self.diff_list.delete(0, "end")
        self.difference_items = list(comp.differences) if comp else []
        if not self.difference_items:
            self.diff_list.insert("end", "No differences" if comp else "AI plan unavailable")
        for i, difference in enumerate(self.difference_items, 1):
            self.diff_list.insert("end", f"{i}. [{difference.category}] {difference.text}")
        self.status.set("Comparison displayed." if comp is not None else "PostgreSQL plan displayed (AI unavailable).")

    def _change_zoom(self, _event=None) -> None:
        try:
            zoom = int(self.zoom_choice.get().rstrip("%")) / 100
        except ValueError:
            return
        for diagram in self.diagrams.values():
            diagram.set_zoom(zoom)

    def _export_result(self) -> None:
        """Save comparison and source plans, without database credentials."""
        if self.result is None or self.busy:
            return
        filename = filedialog.asksaveasfilename(
            title="Save QEP comparison result", defaultextension=".json",
            filetypes=[("JSON file", "*.json")], initialfile="qep_comparison.json")
        if not filename:
            return
        try:
            data = self.result
            if hasattr(data, "to_dict"):
                output = data.to_dict()
            else:
                output = {
                    "sql": self.sql.get("1.0", "end-1c"),
                    "database": data.database,
                    "pg_plan": data.pg_plan.to_dict(),
                    "ai_plan": data.ai_plan.to_dict() if data.ai_plan else None,
                    "ai_model": data.ai_model,
                    "comparison": data.comparison.to_dict() if data.comparison else None,
                    "explanation": data.explanation_text(),
                }
            Path(filename).write_text(json.dumps(output, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
            self.status.set(f"Saved result: {Path(filename).name}")
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror("Could not save result", str(exc))

    @staticmethod
    def _fmt(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, (float, int)):
            return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
        return str(value)

    def _select_paths(self, pg_path: Optional[str], ai_path: Optional[str]) -> None:
        self._syncing_selection = True
        try:
            for side, path in (("postgres", pg_path), ("ai", ai_path)):
                tree = self.trees[side]
                if path is None or path not in self.paths[side]:
                    if tree.selection():
                        tree.selection_remove(tree.selection())
                    continue
                item = self.paths[side][path]
                # Tk posts TreeviewSelect callbacks asynchronously. Avoid generating
                # another event for an already-selected item (infinite ping-pong).
                if tree.selection() != (item,):
                    tree.selection_set(item)
                tree.focus(item)
                tree.see(item)
        finally:
            self._syncing_selection = False
        for side, path in (("postgres", pg_path), ("ai", ai_path)):
            self.diagrams[side].focus(path)

    def _selected_difference(self, _event=None) -> None:
        if self.result is None or not self.difference_items:
            return
        indices = self.diff_list.curselection()
        if not indices:
            return
        difference = self.difference_items[indices[0]]
        self._select_paths(difference.pg_path, difference.ai_path)
        self._write_text(self.details, f"[{difference.category}]\n{difference.text}")

    def _diagram_node_clicked(self, side: str, path: str) -> None:
        if self.result is not None:
            self._show_node_details(side, path)

    def _selected_node(self, side: str) -> None:
        if self._syncing_selection or self.result is None:
            return
        tree = self.trees[side]
        selection = tree.selection()
        if not selection:
            return
        item = selection[0]
        path = next((k for k, value in self.paths[side].items() if value == item), None)
        if path is not None:
            self._show_node_details(side, path)

    def _show_node_details(self, side: str, path: str) -> None:
        plan = self.result.pg_plan if side == "postgres" else self.result.ai_plan
        if plan is None:
            return
        node = next((n for p, n in iter_nodes(plan) if p == path), None)
        if node is None:
            return
        comp = self.result.comparison
        partner = comp.partner(side, path) if comp else None
        self._select_paths(path if side == "postgres" else partner,
                           path if side == "ai" else partner)
        details = [f"Source: {'PostgreSQL' if side == 'postgres' else 'AI'}",
                   f"Node path: {path}", f"Operation: {node.node_type}"]
        for label, val in (("Original operator", node.raw_type), ("Relation", node.relation),
                           ("Index", node.index_name), ("Join type", node.join_type),
                           ("Estimated rows", node.rows), ("Estimated cost", node.cost)):
            if val is not None and val != "":
                details.append(f"{label}: {val}")
        if node.conditions:
            details += ["", "Conditions:"] + [f"  {k}: {v}" for k, v in node.conditions.items()]
        if node.keys:
            details += ["", "Keys: " + ", ".join(node.keys)]
        if node.extra:
            details += ["", "Extra:"] + [f"  {k}: {v}" for k, v in node.extra.items()]
        if comp is not None:
            details += ["", f"Status: {STATUS_LABELS.get(comp.status(side, path), 'Unknown')}",
                        f"Matching path in other plan: {partner or 'None'}"]
            notes = comp.notes(side, path)
            if notes:
                details += ["", "Comparison notes:"] + ["  - " + note for note in notes]
        self._write_text(self.details, "\n".join(details))
        self.tabs.select(1)

    def _on_close(self) -> None:
        if not self.busy:
            self.session.close()
        self.root.destroy()


def run_gui(session) -> None:
    """Called by project.py. Blocks until the GUI window closes."""
    root = tk.Tk()
    PlanExplorer(root, session)
    root.mainloop()
