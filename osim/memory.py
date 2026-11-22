"""RAM híbrida: first-fit reservado, paginação por demanda e swap no Disk.

O relógio é lógico (uma unidade por referência), tornando LRU reproduzível.
Páginas nunca tocadas são zeradas; páginas removidas têm seus bytes no disco.
"""
from dataclasses import dataclass, field
import re

from .disk import NoSpaceError
from .security import SecurityError
from .utils import format_size, parse_size


class MemoryError(ValueError):
    """Configuração, alocação ou referência de memória inválida."""


@dataclass
class Page:
    used: int
    frame: int = None
    last_access: int = 0
    referenced: bool = False
    swap: list = field(default_factory=list)


@dataclass
class Process:
    pid: int
    name: str
    size: int
    kind: str
    owner: str
    start: int = None
    pages: list = field(default_factory=list)

    def path(self):
        return f"[swap PID {self.pid}: {self.name}]"


@dataclass(frozen=True)
class Translation:
    pid: int
    page: int
    frame: int
    physical: int
    fault: bool
    events: tuple


class MemoryManager:
    def __init__(self, fs, ram_size=16 * 1024**2, reserved_size=4 * 1024**2,
                 kernel_size=1024**2, page_size=4096, algorithm="lru", report_every=5):
        if not (0 < kernel_size <= reserved_size < ram_size) or page_size <= 0:
            raise MemoryError("use 0 < kernel <= área reservada < RAM e página positiva")
        if reserved_size % page_size or (ram_size - reserved_size) % page_size:
            raise MemoryError("áreas reservada e dinâmica devem ser múltiplas da página")
        if algorithm not in ("lru", "second-chance"):
            raise MemoryError("algoritmo inválido: use lru ou second-chance")
        if report_every <= 0:
            raise MemoryError("intervalo do relatório deve ser positivo")
        self.fs = fs
        self.ram_size, self.reserved_size = ram_size, reserved_size
        self.kernel_size, self.page_size = kernel_size, page_size
        self.algorithm, self.report_every = algorithm, report_every
        self.processes = {}
        self.frames = {}  # índice de moldura dinâmica -> (Process, página, bytearray)
        self.next_pid = 100
        self.clock = self.hand = self.faults = self.hits = self.evictions = 0
        self.operations = 0
        self.periodic_reports = []
        from .processes import ProcessSimulator
        self.cpu = ProcessSimulator(self)

    @property
    def security(self):
        return self.fs.security

    @property
    def frame_count(self):
        return (self.ram_size - self.reserved_size) // self.page_size

    def _authorize(self, process):
        self.security.require_session()
        if process.kind == "critical":
            self.security.require_kernel("acesso a processo crítico")
        elif not self.security.is_kernel and process.owner != self.security.name:
            self.security.record("acesso a processo", process.name, "NEGADO")
            raise SecurityError("[NEGADO] processo pertence a outro usuário")

    def process(self, name):
        self.security.require_session()
        if name not in self.processes:
            raise MemoryError(f"processo '{name}' inexistente")
        process = self.processes[name]
        self._authorize(process)
        return process

    def reserved_map(self):
        blocks = [(0, self.kernel_size, "Núcleo Base do SO")]
        blocks += [(p.start, p.size, p.name) for p in self.processes.values()
                   if p.kind == "critical"]
        result, cursor = [], 0
        for start, size, name in sorted(blocks):
            if start > cursor:
                result.append((cursor, start - cursor, None))
            result.append((start, size, name))
            cursor = start + size
        if cursor < self.reserved_size:
            result.append((cursor, self.reserved_size - cursor, None))
        return result

    def create(self, name, size, kind="normal"):
        self.security.require_session()
        if kind not in ("normal", "critical"):
            raise MemoryError("tipo inválido: use normal ou critical")
        if kind == "critical":
            self.security.require_kernel("criação de processo crítico")
        if not name or len(name) > 128 or any(ord(c) < 32 for c in name):
            raise MemoryError("nome de processo inválido")
        if name in self.processes or name == "Núcleo Base do SO":
            raise MemoryError(f"processo '{name}' já existe")
        if size <= 0:
            raise MemoryError("tamanho do processo deve ser positivo")
        process = Process(self.next_pid, name, size, kind, self.security.name)
        if kind == "critical":
            holes = [(start, length) for start, length, label in self.reserved_map() if label is None]
            process.start = next((start for start, length in holes if length >= size), None)
            if process.start is None:
                free = sum(length for _, length in holes)
                largest = max((length for _, length in holes), default=0)
                reason = "fragmentação externa; execute memory_compact --reserved" if free >= size else "área reservada cheia"
                raise MemoryError(f"{reason}: livres {free} bytes, maior bloco {largest} bytes")
        else:
            process.pages = [Page(min(self.page_size, size - start))
                             for start in range(0, size, self.page_size)]
        self.processes[name] = process
        self.next_pid += 1
        self.security.record("criação de processo", name, f"OK PID {process.pid} {kind}")
        self._tick()
        return process

    def terminate(self, name):
        process = self.process(name)
        disk = self.fs.disk
        blocks = [b for page in process.pages for b in page.swap]
        if blocks:
            snap = disk.checkpoint()
            try:
                for block in blocks:
                    disk.release(block)
                disk.flush()
            except Exception:
                disk.restore(snap)
                raise
        for page in process.pages:
            if page.frame is not None:
                del self.frames[page.frame]
        del self.processes[name]
        self.cpu.cancel(name)
        self.security.record("encerramento de processo", name, "OK")
        self._tick()
        return process

    def compact(self):
        self.security.require_kernel("compactação da RAM reservada")
        moves, cursor = [], self.kernel_size
        for process in sorted((p for p in self.processes.values() if p.kind == "critical"),
                              key=lambda p: p.start):
            if process.start != cursor:
                moves.append((process.name, process.start, cursor, process.size))
                process.start = cursor
            cursor += process.size
        self.security.record("compactação da RAM", "reservada", f"OK {len(moves)} realocações")
        self._tick()
        return moves

    def set_algorithm(self, algorithm):
        self.security.require_kernel("configuração da substituição de páginas")
        if algorithm not in ("lru", "second-chance"):
            raise MemoryError("algoritmo inválido: use lru ou second-chance")
        self.algorithm = algorithm
        self.hand = 0

    def _victim(self):
        if self.algorithm == "lru":
            frame = min(self.frames, key=lambda f: (self.frames[f][0].pages[self.frames[f][1]].last_access, f))
            return frame, self.hand, []
        # Planeja a varredura sem alterar bits até o swap ter sido confirmado.
        hand, cleared = self.hand, set()
        while True:
            process, index, _ = self.frames[hand]
            page = process.pages[index]
            next_hand = (hand + 1) % self.frame_count
            if not page.referenced or hand in cleared:
                return hand, next_hand, cleared
            cleared.add(hand)
            hand = next_hand

    def _load(self, process, index):
        page, disk = process.pages[index], self.fs.disk
        events = [f"[PAGE FAULT DETECTADO] PID {process.pid}, Página Lógica {index} ausente na RAM."]
        events.append(f"Lendo do Swap (Disco), blocos {page.swap}." if page.swap
                      else "Primeiro acesso: página zerada sob demanda.")
        # Uma leitura pode falhar; não altera RAM/tabelas antes de concluir a E/S.
        data = (b"".join(disk.read_physical(b) for b in page.swap)[:self.page_size]
                if page.swap else bytes(self.page_size))
        frame = next((f for f in range(self.frame_count) if f not in self.frames), None)
        if frame is None:
            frame, hand, cleared = self._victim()
            victim, old_index, old_data = self.frames[frame]
            old_page = victim.pages[old_index]
            count = (self.page_size + disk.block_size - 1) // disk.block_size
            blocks = list(old_page.swap)
            # Reutiliza o espaço da página que está entrando quando o disco está cheio.
            reusable = list(page.swap) if not blocks else []
            candidates = reusable + disk.free_blocks()
            if not blocks:
                if len(candidates) < count:
                    raise NoSpaceError("swap sem espaço no disco; RAM e tabelas preservadas")
                blocks = candidates[:count]
            snap = disk.checkpoint()
            try:
                for i, block in enumerate(blocks):
                    if disk.owner[block] is not None:
                        disk.release(block)
                    disk.claim(block, victim, "SWAP", old_index)
                    disk.write(block, bytes(old_data[i * disk.block_size:(i + 1) * disk.block_size]))
                for block in page.swap:
                    if block not in blocks:
                        disk.release(block)
                disk.flush()
            except Exception:
                disk.restore(snap)
                raise
            events += [f"RAM cheia. Substituição {self.algorithm.upper()} (relógio lógico {self.clock}).",
                       f"Página {old_index} do PID {victim.pid}: último acesso {old_page.last_access}; "
                       f"salvando no Swap (Disco), blocos {blocks}."]
            old_page.swap, old_page.frame = blocks, None
            page.swap = []
            for f in cleared:
                p, i, _ = self.frames[f]
                p.pages[i].referenced = False
            self.hand = hand
            self.evictions += 1
        elif page.swap:
            snap = disk.checkpoint()
            try:
                for block in page.swap:
                    disk.release(block)
                disk.flush()
            except Exception:
                disk.restore(snap)
                raise
            page.swap = []
        self.frames[frame] = (process, index, bytearray(data))
        page.frame = frame
        self.faults += 1
        events.append(f"Carregando Página Lógica {index} na Moldura Física "
                      f"{frame + self.reserved_size // self.page_size} (RAM Dinâmica); tabela atualizada.")
        return events

    def translate(self, name, page_index, offset=0):
        process = self.process(name)
        if process.kind != "normal":
            raise MemoryError("processo crítico usa endereço contíguo, sem paginação")
        if not 0 <= page_index < len(process.pages):
            raise MemoryError("página lógica inválida")
        page = process.pages[page_index]
        if not 0 <= offset < page.used:
            raise MemoryError("deslocamento fora dos bytes válidos do processo")
        fault = page.frame is None
        events = self._load(process, page_index) if fault else []
        self.clock += 1
        page.last_access, page.referenced = self.clock, True
        if not fault:
            self.hits += 1
        frame = page.frame + self.reserved_size // self.page_size
        result = Translation(process.pid, page_index, frame, frame * self.page_size + offset,
                             fault, tuple(events))
        self._tick()
        return result

    def execute(self, name, instruction):
        match = re.fullmatch(r"(LOAD_DATA|STORE_DATA)\s+(?:Página\s+|Pagina\s+)?(\d+)(?:\s+(.+))?",
                             instruction, re.IGNORECASE)
        if not match:
            raise MemoryError('use LOAD_DATA Página N ou STORE_DATA Página N "texto"')
        operation, number, text = match.groups()
        if operation.upper() == "STORE_DATA" and text is None:
            raise MemoryError("STORE_DATA requer texto")
        if operation.upper() == "LOAD_DATA" and text is not None:
            raise MemoryError("LOAD_DATA não recebe texto")
        process = self.process(name)
        index = int(number)
        payload = text.encode("utf-8") if text is not None else None
        if payload is not None and (index >= len(process.pages) or len(payload) > process.pages[index].used):
            raise MemoryError("dados excedem os bytes válidos da página")
        result = self.translate(name, index)
        data = self.frames[process.pages[index].frame][2]
        if payload is not None:
            data[:len(payload)] = payload
        return result, bytes(data[:process.pages[index].used])

    def stats(self):
        holes = [length for _, length, label in self.reserved_map() if label is None]
        normal = [p for p in self.processes.values() if p.kind == "normal"]
        free = sum(holes)
        pages = [page for p in normal for page in p.pages]
        return dict(reserved_used=self.reserved_size - free, reserved_free=free,
                    largest_hole=max(holes, default=0), holes=len(holes),
                    external=free - max(holes, default=0),
                    virtual_bytes=sum(p.size for p in normal),
                    virtual_allocated=len(pages) * self.page_size,
                    resident_bytes=len(self.frames) * self.page_size,
                    internal=sum(self.page_size - page.used for page in pages),
                    resident_internal=sum(self.page_size - page.used for page in pages if page.frame is not None),
                    swap_blocks=sum(len(page.swap) for page in pages),
                    faults=self.faults, hits=self.hits, evictions=self.evictions)

    def _tick(self):
        self.operations += 1
        if self.operations % self.report_every == 0:
            self.periodic_reports.append((self.operations, self.stats()))
            self.periodic_reports = self.periodic_reports[-20:]


def render_process(process, page_size):
    lines = [f"PID {process.pid}: {process.name} | {process.kind} | {process.size} Bytes"]
    if process.kind == "critical":
        lines.append(f"Alocado de forma contígua na Área Reservada: "
                     f"[{format_size(process.start)} até {format_size(process.start + process.size)}]")
    else:
        lines += [f"Tamanho da Página: {page_size} Bytes | Requer {len(process.pages)} Páginas Lógicas.",
                  "Tabela de Páginas gerada sem alocar molduras na RAM (Alocação por demanda)."]
        for index, page in enumerate(process.pages):
            lines.append(f"Página Lógica {index}: {page.used} Bytes ocupados "
                         f"({100 * page.used / page_size:.0f}% de uso)")
        lines.append(f"Fragmentação Interna Total: {len(process.pages) * page_size - process.size} Bytes")
    return "\n".join(lines)


def render_translation(result):
    return "\n".join([*result.events,
                      f"Página Lógica {result.page} -> Moldura Física {result.frame} "
                      f"| Endereço Físico: {result.physical} [{'PAGE FAULT' if result.fault else 'HIT'}]"])


def render_memory(memory, region=None, details=False):
    memory.security.require_kernel("relatório da memória")
    stats = memory.stats()
    lines = [f"--- MEMÓRIA HÍBRIDA | RAM {format_size(memory.ram_size)} ---"]
    if region != "virtual":
        lines.append(f"--- MAPA DA ÁREA CRÍTICA/RESERVADA (Total: {format_size(memory.reserved_size)}) ---")
        for start, size, name in memory.reserved_map():
            lines.append(f"[{format_size(start)} - {format_size(start + size)}] : "
                         f"{name or 'FRAGMENTO LIVRE'} ({format_size(size)}) [{'OCUPADO' if name else 'LIVRE - LACUNA'}]")
        lines += [f"Consumo reservado: {format_size(stats['reserved_used'])} / {format_size(memory.reserved_size)}",
                  f"Total livre: {format_size(stats['reserved_free'])} | Maior bloco contíguo: {format_size(stats['largest_hole'])}",
                  f"Fragmentação Externa: {stats['external']} Bytes | {stats['holes']} fragmento(s) livre(s) | "
                  f"Detectada: {'SIM' if stats['external'] else 'NÃO'}"]
    if region != "reserved":
        lines += ["--- MEMÓRIA VIRTUAL / RAM DINÂMICA ---",
                  f"RAM dinâmica ocupada: {format_size(stats['resident_bytes'])} / {format_size(memory.ram_size - memory.reserved_size)}",
                  f"Memória virtual requisitada: {format_size(stats['virtual_bytes'])} | Capacidade paginada: {format_size(stats['virtual_allocated'])}",
                  f"Fragmentação Interna virtual: {stats['internal']} Bytes | Na RAM: {stats['resident_internal']} Bytes",
                  f"Swap: {stats['swap_blocks']} bloco(s) / {format_size(stats['swap_blocks'] * memory.fs.disk.block_size)} no disco compartilhado",
                  f"Algoritmo: {memory.algorithm.upper()} | Page Faults: {memory.faults} | Hits: {memory.hits} | Substituições: {memory.evictions}"]
        if details:
            for process in memory.processes.values():
                if process.kind != "normal":
                    continue
                lines.append(f"PID {process.pid}: {process.name} ({process.owner})")
                for index, page in enumerate(process.pages):
                    location = (f"Moldura Física {page.frame + memory.reserved_size // memory.page_size}"
                                if page.frame is not None else f"Swap {page.swap}" if page.swap else "sob demanda (zero)")
                    lines.append(f"  Página Lógica {index} -> {location} | R={int(page.referenced)} | último acesso={page.last_access}")
    return "\n".join(lines)


def add_memory_arguments(parser):
    parser.add_argument("--ram-size", default="16MB")
    parser.add_argument("--reserved-size", default="4MB")
    parser.add_argument("--kernel-size", default="1MB")
    parser.add_argument("--page-size", default="4KB")
    parser.add_argument("--page-algorithm", choices=("lru", "second-chance"), default="lru")
    parser.add_argument("--memory-report-every", type=int, default=5)


def memory_from_args(fs, args):
    return MemoryManager(fs, parse_size(args.ram_size), parse_size(args.reserved_size),
                         parse_size(args.kernel_size), parse_size(args.page_size),
                         args.page_algorithm, args.memory_report_every)
