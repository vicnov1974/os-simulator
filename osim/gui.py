"""Interface gráfica (Tkinter) no estilo do Windows Explorer.

Toda a lógica vem de FileSystem / Disk / report: esta camada só desenha e
encaminha ações. Os métodos de ação (new_folder, new_file, delete, rename,
save_file, compact, set_strategy, format_disk) não abrem diálogos e podem ser
chamados diretamente (usado pelos testes); os métodos `ask_*` pedem os dados ao
usuário e chamam as ações.
"""
import tkinter as tk
from math import ceil
from tkinter import messagebox, simpledialog, ttk

from . import report
from .allocation import STRATEGIES, IndexedAllocation, make_strategy
from .disk import Disk, DiskError
from .filesystem import FileSystem, FSError
from .utils import format_size, parse_size

FILE_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#0fa3b1",
               "#d81b8c", "#7a9a01", "#469990", "#9a6324", "#800000", "#5c6bc0"]
C_FREE, C_DIR, C_INODE = "#f0f0f0", "#2b5797", "#7e3794"
C_WASTE, C_EDGE = "#f4b6b0", "#a8a8a8"


# ----------------------------------------------------------------------
# Diálogos
# ----------------------------------------------------------------------
class NewFileDialog(simpledialog.Dialog):
    def body(self, master):
        for r, text in enumerate(("Nome:", "Tamanho (ex.: 10KB, 2500):", "Conteúdo (opcional):")):
            ttk.Label(master, text=text).grid(row=r, column=0, sticky="w", pady=3)
        self.e_name = ttk.Entry(master, width=32)
        self.e_size = ttk.Entry(master, width=32)
        self.e_content = ttk.Entry(master, width=32)
        for r, e in enumerate((self.e_name, self.e_size, self.e_content)):
            e.grid(row=r, column=1, padx=6, pady=3)
        return self.e_name

    def apply(self):
        self.result = (self.e_name.get().strip(), self.e_size.get().strip(),
                       self.e_content.get())


class FormatDialog(simpledialog.Dialog):
    def __init__(self, parent, disk, strategy):
        self._d, self._s = disk, strategy
        super().__init__(parent, "Formatar disco")

    def body(self, master):
        ttk.Label(master, text="Tamanho do bloco:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(master, text="Total de blocos:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Label(master, text="Alocação:").grid(row=2, column=0, sticky="w", pady=3)
        self.v_bs = tk.StringVar(value=format_size(self._d.block_size).replace(" ", ""))
        self.v_n = tk.StringVar(value=str(self._d.total_blocks))
        self.v_a = tk.StringVar(value=self._s.key)
        first = ttk.Combobox(master, textvariable=self.v_bs, width=14,
                             values=["512B", "1KB", "2KB", "4KB"])
        first.grid(row=0, column=1, padx=6)
        ttk.Entry(master, textvariable=self.v_n, width=16).grid(row=1, column=1, padx=6)
        ttk.Combobox(master, textvariable=self.v_a, state="readonly", width=14,
                     values=list(STRATEGIES)).grid(row=2, column=1, padx=6)
        ttk.Label(master, text="ATENÇÃO: todos os arquivos serão apagados.",
                  foreground="#b00020").grid(row=3, column=0, columnspan=2, pady=(8, 0))
        return first

    def apply(self):
        self.result = (self.v_bs.get(), self.v_n.get(), self.v_a.get())


# ----------------------------------------------------------------------
# Janela principal
# ----------------------------------------------------------------------
class App(tk.Tk):
    CELL, GAP, PAD = 34, 4, 8

    def __init__(self, fs: FileSystem = None):
        super().__init__()
        self.title("Simulador de SO - Etapa 1: Explorador de Arquivos")
        self.geometry("1200x800")
        self.minsize(950, 640)
        self.fs = fs or FileSystem(Disk(4096, 64), IndexedAllocation())
        self.highlight = None
        self.expanded = {"/"}
        self.back, self.fwd = [], []
        self._busy = False
        self._cols = 1
        self._last_w = 0
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self.status = tk.StringVar(value="Pronto.")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w",
                  padding=(6, 2)).pack(side="bottom", fill="x")
        self.refresh()

    # ---- helpers de interação (sobrescritos nos testes) -----------------
    def show_error(self, msg: str) -> None:
        messagebox.showerror("Erro", msg, parent=self)

    def confirm(self, msg: str) -> bool:
        return messagebox.askyesno("Confirmar", msg, parent=self)

    def _try(self, fn, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
        except (FSError, DiskError, ValueError) as exc:
            self.show_error(str(exc))
            self.refresh()
            return False
        self.refresh()
        return True

    # ---- construção da interface ------------------------------------------
    def _build_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Nova pasta...", command=self.ask_new_folder)
        f.add_command(label="Novo arquivo...", command=self.ask_new_file)
        f.add_separator()
        f.add_command(label="Sair", command=self.destroy)
        m.add_cascade(label="Arquivo", menu=f)
        d = tk.Menu(m, tearoff=0)
        d.add_command(label="Compactar disco", command=self.compact)
        d.add_command(label="Relatório detalhado...", command=self.show_report)
        d.add_separator()
        d.add_command(label="Formatar disco...", command=self.ask_format)
        m.add_cascade(label="Disco", menu=d)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="Sobre", command=lambda: messagebox.showinfo(
            "Sobre", "Simulador de SO Modular - Etapa 1\nSistema de arquivos e diagnóstico "
                     "de disco.", parent=self))
        m.add_cascade(label="Ajuda", menu=h)
        self.config(menu=m)

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self.btn_back = ttk.Button(bar, text="◀", width=3, command=self.go_back)
        self.btn_fwd = ttk.Button(bar, text="▶", width=3, command=self.go_forward)
        self.btn_up = ttk.Button(bar, text="▲", width=3, command=self.go_up)
        for b in (self.btn_back, self.btn_fwd, self.btn_up):
            b.pack(side="left", padx=1)
        self.addr = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.addr)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<Return>", lambda e: self.navigate(self.addr.get().strip()))
        ttk.Button(bar, text="Nova pasta", command=self.ask_new_folder).pack(side="left", padx=2)
        ttk.Button(bar, text="Novo arquivo", command=self.ask_new_file).pack(side="left", padx=2)
        ttk.Label(bar, text="  Alocação:").pack(side="left")
        self._title_to_key = {cls.title: k for k, cls in STRATEGIES.items()}
        self.combo = ttk.Combobox(bar, state="readonly", width=18,
                                  values=list(self._title_to_key))
        self.combo.pack(side="left", padx=4)
        self.combo.bind("<<ComboboxSelected>>", self._on_strategy)

    def _build_body(self):
        main = ttk.PanedWindow(self, orient="vertical")
        main.pack(fill="both", expand=True, padx=4, pady=2)
        top = ttk.PanedWindow(main, orient="horizontal")
        main.add(top, weight=3)

        # árvore de pastas (esquerda)
        lf = ttk.Frame(top)
        self.tree = ttk.Treeview(lf, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<<TreeviewOpen>>", lambda e: self.expanded.add(self.tree.focus()))
        self.tree.bind("<<TreeviewClose>>", lambda e: self.expanded.discard(self.tree.focus()))
        top.add(lf, weight=1)

        # lista de itens (direita)
        rf = ttk.Frame(top)
        cols = ("tipo", "tamanho", "blocos", "interna")
        self.lv = ttk.Treeview(rf, columns=cols, selectmode="browse")
        for col, text, w, anchor in (("#0", "Nome", 220, "w"), ("tipo", "Tipo", 80, "w"),
                                     ("tamanho", "Tamanho", 90, "e"),
                                     ("blocos", "Blocos ocupados", 220, "w"),
                                     ("interna", "Frag. interna", 100, "e")):
            self.lv.heading(col, text=text)
            self.lv.column(col, width=w, anchor=anchor)
        sb2 = ttk.Scrollbar(rf, orient="vertical", command=self.lv.yview)
        self.lv.configure(yscrollcommand=sb2.set)
        self.lv.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        self.lv.bind("<<TreeviewSelect>>", self._on_list_select)
        self.lv.bind("<Double-1>", self._on_double)
        self.lv.bind("<Return>", lambda e: self.open_selected())
        self.lv.bind("<Delete>", lambda e: self.ask_delete())
        self.lv.bind("<F2>", lambda e: self.ask_rename())
        self.lv.bind("<BackSpace>", lambda e: self.go_up())
        self.lv.bind("<Button-3>", self._context_menu)
        self.lv.bind("<Button-2>", self._context_menu)
        top.add(rf, weight=4)

        # painel do disco (embaixo)
        df = ttk.LabelFrame(main, text=" Mapa do disco ", padding=4)
        main.add(df, weight=3)
        left = ttk.Frame(df)
        left.pack(side="left", fill="both", expand=True)
        wrap = ttk.Frame(left)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, background="white", highlightthickness=1,
                                highlightbackground="#999")
        csb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=csb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.hover = tk.StringVar(value="Passe o mouse sobre um bloco para ver detalhes.")
        ttk.Label(left, textvariable=self.hover, anchor="w").pack(fill="x", pady=(3, 0))
        self._build_legend(left)

        right = ttk.Frame(df)
        right.pack(side="right", fill="y", padx=(8, 0))
        self.banner = tk.Label(right, text="", font=("TkDefaultFont", 10, "bold"),
                               fg="white", pady=5)
        self.banner.pack(fill="x")
        self.btn_compact = ttk.Button(right, text="Compactar disco", command=self.compact)
        self.btn_compact.pack(fill="x", pady=4)
        self.diag = tk.Text(right, width=62, height=10, wrap="word", state="disabled",
                            font="TkFixedFont", background="#fafafa")
        self.diag.pack(fill="both", expand=True)

    def _build_legend(self, parent):
        lg = ttk.Frame(parent)
        lg.pack(fill="x", pady=(4, 0))
        for color, text in ((C_DIR, "Diretório"), (C_INODE, "i-node"),
                            ("#4363d8", "Dados (bloco cheio)"),
                            (C_WASTE, "Fragmentação interna"), (C_FREE, "Livre")):
            tk.Label(lg, bg=color, width=2, relief="solid", bd=1).pack(side="left", padx=(6, 2))
            ttk.Label(lg, text=text).pack(side="left")

    def _build_status(self):
        pass

    # ---- navegação -----------------------------------------------------------
    def navigate(self, path: str, push: bool = True) -> bool:
        prev = self.fs.cwd.path()
        try:
            self.fs.chdir(path)
        except (FSError, DiskError) as exc:
            self.show_error(str(exc))
            self.refresh()
            return False
        if push and self.fs.cwd.path() != prev:
            self.back.append(prev)
            self.fwd.clear()
        self.highlight = None
        self.refresh()
        return True

    def go_back(self):
        if self.back:
            self.fwd.append(self.fs.cwd.path())
            self._go_hist(self.back.pop())

    def go_forward(self):
        if self.fwd:
            self.back.append(self.fs.cwd.path())
            self._go_hist(self.fwd.pop())

    def _go_hist(self, path):
        try:
            self.fs.chdir(path)
        except FSError:
            self.fs.cwd = self.fs.root
        self.highlight = None
        self.refresh()

    def go_up(self):
        parent = self.fs.cwd.parent
        if parent is not None:
            self.navigate(parent.path())

    # ---- atualização da tela ----------------------------------------------------
    def _alive(self, node) -> bool:
        while node.parent is not None:
            if node.parent.children.get(node.name) is not node:
                return False
            node = node.parent
        return node is self.fs.root

    def refresh(self):
        if self.highlight is not None and not self._alive(self.highlight):
            self.highlight = None
        self._busy = True
        try:
            self.addr.set(self.fs.cwd.path())
            self.combo.set(self.fs.strategy.title)
            self._fill_tree()
            self._fill_list()
        finally:
            self._busy = False
        self.draw_map()
        self.update_diag()
        self.btn_back.state(["!disabled" if self.back else "disabled"])
        self.btn_fwd.state(["!disabled" if self.fwd else "disabled"])
        self.btn_up.state(["!disabled" if self.fs.cwd.parent else "disabled"])
        d = self.fs.disk
        self.status.set(f"{self.fs.cwd.path()}  |  {len(self.fs.cwd.children)} item(ns)  |  "
                        f"{d.used_count}/{d.total_blocks} blocos usados")

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())

        def add(node, parent_iid):
            iid = node.path()
            text = "/ (raiz)" if node.is_root else node.name
            self.tree.insert(parent_iid, "end", iid=iid, text=text, open=iid in self.expanded)
            for c in self.fs.sorted_children(node):
                if c.is_dir:
                    add(c, iid)
        add(self.fs.root, "")
        cur = self.fs.cwd.path()
        node = self.fs.cwd.parent
        while node is not None:                       # garante o caminho visível
            self.tree.item(node.path(), open=True)
            node = node.parent
        self.tree.selection_set(cur)
        self.tree.see(cur)

    def _fill_list(self):
        self.lv.delete(*self.lv.get_children())
        bs = self.fs.disk.block_size
        for n in self.fs.sorted_children(self.fs.cwd):
            if n.is_dir:
                vals = ("Pasta", "", f"bloco {n.meta_block:02d}", "")
                text = n.name + "/"
            else:
                blocks = ", ".join(f"{b:02d}" for b in n.blocks[:8]) + (
                    ", ..." if len(n.blocks) > 8 else "")
                if n.meta_block is not None:
                    blocks = f"i-node {n.meta_block:02d} | " + blocks
                waste = len(n.blocks) * bs - n.size
                vals = ("Arquivo", format_size(n.size), blocks or "-", f"{waste} B")
                text = n.name
            self.lv.insert("", "end", iid=n.path(), text=text, values=vals)
        if self.highlight is not None and self.highlight.parent is self.fs.cwd:
            self.lv.selection_set(self.highlight.path())

    # ---- eventos da árvore e da lista ------------------------------------------------
    def _on_tree_select(self, _e):
        if self._busy:
            return
        sel = self.tree.selection()
        if sel and sel[0] != self.fs.cwd.path():
            self.navigate(sel[0])

    def _on_list_select(self, _e):
        if self._busy:
            return
        sel = self.lv.selection()
        self.highlight = self.fs.resolve(sel[0]) if sel else None
        self.draw_map()
        self.update_diag()

    def _on_double(self, _e):
        self.open_selected()

    def selected_node(self):
        sel = self.lv.selection()
        return self.fs.resolve(sel[0]) if sel else None

    def open_selected(self):
        n = self.selected_node()
        if n is None:
            return
        if n.is_dir:
            self.navigate(n.path())
        else:
            self.open_editor(n)

    def _context_menu(self, event):
        row = self.lv.identify_row(event.y)
        menu = tk.Menu(self, tearoff=0)
        if row:
            self.lv.selection_set(row)
            menu.add_command(label="Abrir", command=self.open_selected)
            menu.add_command(label="Renomear\tF2", command=self.ask_rename)
            menu.add_command(label="Excluir\tDel", command=self.ask_delete)
            menu.add_separator()
            menu.add_command(label="Propriedades", command=self.show_properties)
        else:
            self.lv.selection_remove(*self.lv.selection())
            menu.add_command(label="Nova pasta...", command=self.ask_new_folder)
            menu.add_command(label="Novo arquivo...", command=self.ask_new_file)
            menu.add_separator()
            menu.add_command(label="Atualizar", command=self.refresh)
        menu.tk_popup(event.x_root, event.y_root)

    # ---- mapa do disco -----------------------------------------------------------------
    def _on_canvas_resize(self, event):
        if abs(event.width - self._last_w) > 4:
            self.draw_map()

    def _file_colors(self) -> dict:
        return {n: FILE_COLORS[i % len(FILE_COLORS)] for i, n in enumerate(self.fs.files())}

    def draw_map(self):
        c, d = self.canvas, self.fs.disk
        bs, cell, gap, pad = d.block_size, self.CELL, self.GAP, self.PAD
        c.delete("all")
        width = max(c.winfo_width(), 240)
        self._last_w = c.winfo_width()
        self._cols = cols = max(1, (width - 2 * pad - 16 + gap) // (cell + gap))
        colors = self._file_colors()
        small = ("TkDefaultFont", 7)
        for b in range(d.total_blocks):
            r, col = divmod(b, cols)
            x, y = pad + col * (cell + gap), pad + r * (cell + gap)
            o = d.owner[b]
            label_color = "white"
            if o is None:
                c.create_rectangle(x, y, x + cell, y + cell, fill=C_FREE, outline=C_EDGE)
                label_color = "#888"
            elif o.kind == "DIR_META":
                c.create_rectangle(x, y, x + cell, y + cell, fill=C_DIR, outline=C_EDGE)
                c.create_text(x + cell / 2, y + cell / 2 + 4, text="D", fill="white",
                              font=("TkDefaultFont", 11, "bold"))
            elif o.kind == "FILE_INODE":
                c.create_rectangle(x, y, x + cell, y + cell, fill=C_INODE, outline=C_EDGE)
                c.create_text(x + cell / 2, y + cell / 2 + 4, text="i", fill="white",
                              font=("TkDefaultFont", 11, "bold"))
            else:
                used = min(bs, o.node.size - o.index * bs)
                ratio = used / bs
                if ratio < 1:      # parte não usada do bloco = fragmentação interna
                    c.create_rectangle(x, y, x + cell, y + cell, fill=C_WASTE, outline="")
                    c.create_rectangle(x, y, x + cell, y + max(2, cell * ratio),
                                       fill=colors[o.node], outline="")
                    c.create_rectangle(x, y, x + cell, y + cell, outline=C_EDGE)
                else:
                    c.create_rectangle(x, y, x + cell, y + cell, fill=colors[o.node],
                                       outline=C_EDGE)
            c.create_text(x + 3, y + 2, anchor="nw", text=str(b), fill=label_color, font=small)
            if self.highlight is not None and o is not None and o.node is self.highlight:
                c.create_rectangle(x - 1, y - 1, x + cell + 1, y + cell + 1,
                                   outline="black", width=3)
        rows = ceil(d.total_blocks / cols)
        c.configure(scrollregion=(0, 0, width, 2 * pad + rows * (cell + gap)))

    def _block_at(self, event):
        c = self.canvas
        x, y = c.canvasx(event.x) - self.PAD, c.canvasy(event.y) - self.PAD
        step = self.CELL + self.GAP
        if x < 0 or y < 0:
            return None
        col, row = int(x // step), int(y // step)
        if col >= self._cols or x % step > self.CELL or y % step > self.CELL:
            return None
        b = row * self._cols + col
        return b if b < self.fs.disk.total_blocks else None

    def _on_hover(self, event):
        b = self._block_at(event)
        if b is None:
            return
        if self.fs.disk.owner[b] is None:
            self.hover.set(f"Bloco {b:02d} [LIVRE]")
        else:
            label, text = report.describe_block(self.fs, b)
            self.hover.set(f"Bloco {b:02d} [{label}]: {text}")

    def _on_canvas_click(self, event):
        b = self._block_at(event)
        if b is None or self.fs.disk.owner[b] is None:
            return
        node = self.fs.disk.owner[b].node
        target = node if node.is_dir else node.parent
        if target is not self.fs.cwd:
            self.back.append(self.fs.cwd.path())
            self.fwd.clear()
            self.fs.cwd = target
        self.highlight = node
        self.refresh()

    # ---- diagnóstico -----------------------------------------------------------------------
    def update_diag(self):
        d = self.fs.disk
        a = report.analyze(self.fs)
        lines = [f"Bloco: {format_size(d.block_size)} | {d.total_blocks} blocos "
                 f"({format_size(d.capacity)})",
                 f"Alocação: {self.fs.strategy.title}",
                 f"Usados: {d.used_count} | Livres: {d.free_count}", "",
                 report.render_global_report(self.fs)]
        if self.highlight is not None and not self.highlight.is_dir:
            lines += ["", "--- ARQUIVO SELECIONADO ---",
                      report.render_file_report(self.fs, self.highlight)]
        self.diag.configure(state="normal")
        self.diag.delete("1.0", "end")
        self.diag.insert("1.0", "\n".join(lines))
        self.diag.configure(state="disabled")
        if a["needs_compaction"]:
            self.banner.configure(text="⚠ COMPACTAÇÃO NECESSÁRIA", bg="#c62828")
            self.btn_compact.configure(text="⚠ Compactar disco (necessário)")
        elif a["scattered_files"]:
            self.banner.configure(text="Compactação opcional", bg="#ef8f00")
            self.btn_compact.configure(text="Compactar disco")
        else:
            self.banner.configure(text="✔ Compactação não necessária", bg="#2e7d32")
            self.btn_compact.configure(text="Compactar disco")

    def show_report(self):
        win = tk.Toplevel(self)
        win.title("Relatório detalhado do disco")
        win.geometry("900x600")
        txt = tk.Text(win, wrap="none", font="TkFixedFont")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", report.render_status(self.fs, details=True))
        txt.configure(state="disabled")

    def show_properties(self):
        n = self.selected_node()
        if n is None:
            return
        if n.is_dir:
            info = (f"{n.path()}\nTipo: pasta\nItens: {len(n.children)}\n"
                    f"Bloco: {n.meta_block}")
        else:
            bs = self.fs.disk.block_size
            info = (f"{n.path()}\nTipo: arquivo\nTamanho: {n.size} bytes "
                    f"({format_size(n.size)})\n"
                    f"Blocos de dados: {n.blocks or '-'}\n"
                    f"I-node: {n.meta_block if n.meta_block is not None else '-'}\n"
                    f"Fragmentação interna: {len(n.blocks) * bs - n.size} bytes")
        messagebox.showinfo("Propriedades", info, parent=self)

    # ---- ações (sem diálogo; usadas também pelos testes) ------------------------------------
    def _join(self, name: str) -> str:
        if not name or "/" in name:
            raise FSError("nome inválido (vazio ou contendo '/')")
        base = self.fs.cwd.path()
        return ("" if base == "/" else base) + "/" + name

    def new_folder(self, name: str) -> bool:
        return self._try(lambda: self.fs.mkdir(self._join(name)))

    def new_file(self, name: str, size_text: str = "", content: str = "") -> bool:
        def run():
            size = parse_size(size_text) if size_text else None
            node = self.fs.create(self._join(name), size=size,
                                  content=content.replace("\\n", "\n").encode("utf-8"))
            self.highlight = node
        return self._try(run)

    def delete(self, path: str) -> bool:
        return self._try(lambda: self.fs.remove(path, recursive=True))

    def rename(self, path: str, new_name: str) -> bool:
        def run():
            node = self.fs.resolve(path)
            if not new_name or "/" in new_name:
                raise FSError("nome inválido")
            self.fs.move(path, node.parent.path().rstrip("/") + "/" + new_name)
        return self._try(run)

    def save_file(self, path: str, text: str) -> bool:
        return self._try(lambda: self.fs.write(path, text.encode("utf-8")))

    def compact(self) -> bool:
        before = len(self.fs.disk.free_runs())
        box = {}
        ok = self._try(lambda: box.update(m=self.fs.compact()))
        if ok:
            self.status.set(f"Compactação concluída: {box['m']} bloco(s) realocado(s). "
                            f"Regiões livres: {before} -> {len(self.fs.disk.free_runs())}.")
        return ok

    def set_strategy(self, key: str) -> bool:
        box = {}
        ok = self._try(lambda: box.update(m=self.fs.change_strategy(key)))
        if ok:
            self.status.set(f"Estratégia alterada para {self.fs.strategy.title}; "
                            f"{box['m']} bloco(s) realocado(s).")
        return ok

    def format_disk(self, block_size: str, blocks: str, alloc: str) -> bool:
        def run():
            bs, n = parse_size(block_size), int(blocks)
            self.fs = FileSystem(Disk(bs, n), make_strategy(alloc))
            self.highlight = None
            self.back.clear()
            self.fwd.clear()
            self.expanded = {"/"}
        return self._try(run)

    # ---- ações com diálogo ----------------------------------------------------------------------
    def ask_new_folder(self):
        name = simpledialog.askstring("Nova pasta", "Nome da pasta:", parent=self)
        if name:
            self.new_folder(name.strip())

    def ask_new_file(self):
        dlg = NewFileDialog(self, "Novo arquivo")
        if dlg.result and dlg.result[0]:
            self.new_file(*dlg.result)

    def ask_delete(self):
        n = self.selected_node()
        if n is None:
            return
        extra = " e todo o seu conteúdo" if n.is_dir and n.children else ""
        if self.confirm(f"Excluir '{n.name}'{extra}?"):
            self.delete(n.path())

    def ask_rename(self):
        n = self.selected_node()
        if n is None:
            return
        name = simpledialog.askstring("Renomear", "Novo nome:", initialvalue=n.name,
                                      parent=self)
        if name:
            self.rename(n.path(), name.strip())

    def ask_format(self):
        dlg = FormatDialog(self, self.fs.disk, self.fs.strategy)
        if dlg.result and self.confirm("Formatar o disco apaga TODOS os arquivos. Continuar?"):
            self.format_disk(*dlg.result)

    def _on_strategy(self, _e):
        key = self._title_to_key[self.combo.get()]
        if key != self.fs.strategy.key:
            self.set_strategy(key)

    def open_editor(self, node):
        data = self.fs.read_node(node)
        binary = b"\0" in data
        win = tk.Toplevel(self)
        win.title(f"Editar - {node.path()}")
        win.geometry("640x420")
        note = (f"Conteúdo binário/zerado ({len(data)} bytes). Salvar substitui o conteúdo "
                f"pelo texto abaixo." if binary else f"{len(data)} bytes")
        ttk.Label(win, text=note, padding=4).pack(fill="x")
        txt = tk.Text(win, wrap="word", undo=True)
        txt.pack(fill="both", expand=True, padx=4)
        if not binary:
            txt.insert("1.0", data.decode("utf-8", errors="replace"))

        def save():
            if self.save_file(node.path(), txt.get("1.0", "end-1c")):
                win.destroy()
        row = ttk.Frame(win, padding=4)
        row.pack(fill="x")
        ttk.Button(row, text="Salvar", command=save).pack(side="right", padx=2)
        ttk.Button(row, text="Cancelar", command=win.destroy).pack(side="right")


def run(fs: FileSystem = None) -> None:
    App(fs).mainloop()
