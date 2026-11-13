"""Diagnóstico do disco: mapa gráfico, fragmentação interna/externa e compactação."""
import string

from .utils import format_size

_SYMBOLS = string.ascii_uppercase + string.ascii_lowercase + string.digits
_PALETTE = [31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96]


def paint(text: str, code, color: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if color else text


def is_contiguous(blocks: list) -> bool:
    return all(b2 == b1 + 1 for b1, b2 in zip(blocks, blocks[1:]))


def _fmt_blocks(blocks: list, limit: int = 8) -> str:
    shown = ", ".join(f"{b:02d}" for b in blocks[:limit])
    return shown + (", ..." if len(blocks) > limit else "")


# ----------------------------------------------------------------------
# Análise (números puros - também usada pelos testes)
# ----------------------------------------------------------------------
def analyze(fs) -> dict:
    d, bs = fs.disk, fs.disk.block_size
    files = fs.files()
    runs = d.free_runs()
    largest = max((l for _, l in runs), default=0)
    allocated_data = sum(len(n.blocks) * bs for n in files)
    internal = sum(len(n.blocks) * bs - n.stored_size for n in files)
    scattered = [n for n in files if not is_contiguous(n.blocks)]

    if fs.strategy.contiguous:
        external = (d.free_count - largest) * bs if len(runs) > 1 else 0
        needs_compaction = len(runs) > 1
    else:
        external = 0
        needs_compaction = False

    return {
        "block_size": bs, "total_blocks": d.total_blocks,
        "used": d.used_count, "free": d.free_count,
        "free_runs": runs, "largest_run": largest,
        "allocated_data": allocated_data,
        "internal_total": internal,
        "files_with_internal": sum(1 for n in files if len(n.blocks) * bs > n.stored_size),
        "external": external,
        "needs_compaction": needs_compaction,
        "scattered_files": scattered,
    }


# ----------------------------------------------------------------------
# Mapa gráfico (grade)
# ----------------------------------------------------------------------
def _symbols(fs) -> dict:
    def first_block(n):
        return min(([n.meta_block] if n.meta_block is not None else []) + n.blocks,
                   default=10 ** 9)
    ordered = sorted(fs.files(), key=first_block)
    return {n: (_SYMBOLS[i] if i < len(_SYMBOLS) else "*") for i, n in enumerate(ordered)}


def render_grid(fs, color: bool = False, cols: int = 16) -> str:
    d, bs = fs.disk, fs.disk.block_size
    syms = _symbols(fs)
    order = list(syms)
    w = max(2, len(str(d.total_blocks - 1)))
    lines = []
    for row in range(0, d.total_blocks, cols):
        cells = []
        for b in range(row, min(row + cols, d.total_blocks)):
            o = d.owner[b]
            if o is None:
                cell = paint(" . ", 90, color)
            elif o.kind == "DIR_META":
                cell = paint("[#]", "1;34", color)
            elif o.kind == "FILE_INODE":
                cell = paint("[@]", "1;35", color)
            else:
                s = syms[o.node]
                used = min(bs, o.node.stored_size - o.index * bs)
                text = f"[{s}]" if used == bs else f"({s})"
                cell = paint(text, _PALETTE[order.index(o.node) % len(_PALETTE)], color)
            cells.append(cell)
        lines.append(f"{row:0{w}d} | " + "".join(cells))

    lines.append("")
    lines.append("Legenda: [#] diretório | [@] i-node | [X] bloco cheio | "
                 "(X) bloco parcial = fragmentação interna | . livre")
    for n, s in syms.items():
        lines.append(f"  {paint(s, _PALETTE[order.index(n) % len(_PALETTE)], color)} = "
                     f"{n.path()}  ({format_size(n.size)}, {len(n.blocks)} bloco(s))")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Mapa de alocação detalhado (lista bloco a bloco)
# ----------------------------------------------------------------------
def _describe(fs, b: int):
    d, bs = fs.disk, fs.disk.block_size
    o = d.owner[b]
    node = o.node
    if o.kind == "DIR_META":
        return fs.strategy.dir_label(node.is_root), f"Diretório '{node.path()}'"
    if o.kind == "FILE_INODE":
        return "INODE_ARQ", (f"Arquivo '{node.path()}' "
                             f"(índice -> blocos: {_fmt_blocks(node.blocks)})")
    used = min(bs, node.stored_size - o.index * bs)
    note = fs.strategy.data_note(node, o.index, d)
    return "DADO", f"'{node.path()}' (Bloco {o.index + 1} - {used} bytes ocupados){note}"


def render_block_map(fs) -> str:
    d = fs.disk
    w = max(2, len(str(d.total_blocks - 1)))
    lines, b = [], 0
    while b < d.total_blocks:
        if d.owner[b] is None:
            e = b
            while e + 1 < d.total_blocks and d.owner[e + 1] is None:
                e += 1
            head = f"Bloco {b:0{w}d}" + (f"..{e:0{w}d}" if e > b else "")
            lines.append(f"{head:<24}: [LIVRE]")
            b = e + 1
        else:
            label, text = _describe(fs, b)
            lines.append(f"{'Bloco %0*d [%s]' % (w, b, label):<24}: {text}")
            b += 1
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Relatório de fragmentação
# ----------------------------------------------------------------------
def render_file_report(fs, n) -> str:
    """Relatório de um único arquivo (tamanho real x alocado, fragmentação interna)."""
    bs = fs.disk.block_size
    w = max(2, len(str(fs.disk.total_blocks - 1)))
    alloc = len(n.blocks) * bs
    waste = alloc - n.stored_size
    meta = " + 1 bloco de i-node" if n.meta_block is not None else ""
    out = [f"Arquivo '{n.path()}'",
           f"  Tamanho Real            : {n.size} bytes ({format_size(n.size)})",
           f"  Espaço Alocado no Disco : {alloc} bytes "
           f"({len(n.blocks)} bloco(s) de {format_size(bs)}{meta})"]
    if n.encryption:
        out.append(f"  Conteúdo criptografado  : {n.stored_size} bytes (AES-GCM)")
    if waste > 0 and n.blocks:
        out.append(f"  Fragmentação Interna    : {waste} bytes "
                   f"({waste / bs * 100:.0f}% de desperdício no Bloco {n.blocks[-1]:0{w}d})")
    else:
        out.append("  Fragmentação Interna    : 0 bytes")
    if n.blocks:
        kind = "contíguos" if is_contiguous(n.blocks) else "NÃO contíguos (dispersos)"
        out.append(f"  Blocos de dados         : {_fmt_blocks(n.blocks)} ({kind})")
    return "\n".join(out)


def render_file_reports(fs) -> str:
    files = fs.files()
    return "\n".join(render_file_report(fs, n) for n in files) if files else "(nenhum arquivo)"


def describe_block(fs, b: int):
    """(rótulo, texto) do bloco b, que deve estar ocupado."""
    return _describe(fs, b)


def render_global_report(fs, color: bool = False) -> str:
    a = analyze(fs)
    bs = a["block_size"]
    runs = a["free_runs"]
    out = ["--- RELATÓRIO DE FRAGMENTAÇÃO (GLOBAL) ---"]

    pct = (a["internal_total"] / a["allocated_data"] * 100) if a["allocated_data"] else 0
    out.append(f"Fragmentação Interna             : {a['internal_total']} bytes "
               f"({a['files_with_internal']} arquivo(s) afetado(s); "
               f"{pct:.1f}% do espaço alocado a dados)")

    holes = ", ".join(f"[{s:02d}..{s + l - 1:02d}]" if l > 1 else f"[{s:02d}]" for s, l in runs)
    out.append(f"Blocos Livres                    : {a['free']} ({a['free'] * bs} bytes) "
               f"em {len(runs)} região(ões) {holes}".rstrip())
    out.append(f"Maior Região Livre Contígua      : {a['largest_run']} bloco(s) "
               f"({a['largest_run'] * bs} bytes)")

    if fs.strategy.contiguous:
        out.append(f"Fragmentação Externa             : {a['external']} bytes"
                   + (f" ({a['free'] - a['largest_run']} bloco(s) livres dispersos fora "
                      f"da maior região)" if a["external"] else ""))
    else:
        scat = (f"; {a['free']} bloco(s) livres em {len(runs)} região(ões), todos utilizáveis"
                if len(runs) > 1 else "")
        out.append(f"Fragmentação Externa             : 0 bytes "
                   f"(não se aplica: alocação não-contígua{scat})")

    if a["needs_compaction"]:
        txt = paint("SIM", "1;31", color)
        out.append(f"Necessidade de Compactação       : {txt} - o espaço livre está dividido; "
                   f"arquivos maiores que {a['largest_run']} bloco(s) não podem ser alocados. "
                   f"Use o comando 'compact'.")
    else:
        txt = paint("NÃO", "1;32", color)
        reason = ("blocos livres formam uma única região" if fs.strategy.contiguous
                  else "alocação não-contígua não exige blocos adjacentes")
        extra = ""
        if a["scattered_files"]:
            extra = (f" (opcional: {len(a['scattered_files'])} arquivo(s) com blocos "
                     f"dispersos; 'compact' melhora o acesso sequencial)")
        out.append(f"Necessidade de Compactação       : {txt} ({reason}){extra}")
    return "\n".join(out)


def render_status(fs, details: bool = False, color: bool = False) -> str:
    d = fs.disk
    pct_used = d.used_count / d.total_blocks * 100
    out = [
        f"Tamanho do Bloco de Disco: {format_size(d.block_size)} | "
        f"Total de Blocos: {d.total_blocks} ({format_size(d.capacity)})",
        f"Estratégia de Alocação   : {fs.strategy.title}",
        f"Ocupação                 : {d.used_count} usado(s) / {d.free_count} livre(s) "
        f"({pct_used:.1f}% usado)",
        "",
        "[MAPA GRÁFICO DO DISCO]",
        render_grid(fs, color),
        "",
    ]
    if details:
        out += [f"[MAPA DE ALOCAÇÃO DO DISCO - {fs.strategy.title}]",
                render_block_map(fs), "",
                "[RELATÓRIO POR ARQUIVO]", render_file_reports(fs), ""]
    out.append(render_global_report(fs, color))
    return "\n".join(out)
