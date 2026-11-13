"""Dispositivos físicos, fila de E/S, FTL de SSD e arranjos RAID em memória.

Escritas entram na fila; leituras são barreiras que atendem as pendências antes
de devolver bytes. Cada disco HDD tem sua própria cabeça e política de serviço.
"""
from dataclasses import dataclass
import math

from .disk import DiskError, NoSpaceError
from .scheduling import schedule, render_schedule


@dataclass
class IORequest:
    operation: str
    block: int
    track: int
    label: str = ""
    data: bytes = b""
    result: bytes = b""


class HDD:
    kind = "hdd"

    def __init__(self, block_size, blocks, algorithm="sstf", tracks=200, head=53,
                 direction="up"):
        if block_size <= 0 or blocks <= 0:
            raise DiskError("tamanho de bloco e número de blocos devem ser positivos")
        schedule([], head, algorithm, tracks, direction)  # valida configuração
        self.block_size, self.blocks = block_size, blocks
        self.algorithm, self.tracks, self.head = algorithm, tracks, head
        self.direction = direction
        self.data, self.pending, self.history = {}, [], []
        self.labels = {}
        self.last_schedule = None
        self.total_movement = 0
        self.failed = False

    def check(self, block):
        if not 0 <= block < self.blocks:
            raise DiskError(f"bloco físico inválido: {block}")
        if self.failed:
            raise DiskError("disco físico indisponível")

    def write(self, block, data, label=""):
        self.check(block)
        if len(data) > self.block_size:
            raise DiskError("dados maiores que o bloco físico")
        if any(r.block == block for r in self.pending):
            self.flush()
        self.pending.append(IORequest("WRITE", block, block % self.tracks,
                                      label, bytes(data).ljust(self.block_size, b"\0")))

    def read(self, block, label=""):
        self.check(block)
        request = IORequest("READ", block, block % self.tracks, label)
        # Barreiras preservam dependências READ-after-WRITE e WRITE-after-WRITE.
        self.flush()
        self.pending.append(request)
        self.flush()
        return request.result

    def _read(self, block):
        return self.data.get(block, bytes(self.block_size))

    def _write(self, block, data):
        self.data[block] = data

    def flush(self):
        if not self.pending:
            return self.last_schedule
        if self.failed:
            raise DiskError("disco físico indisponível")
        # Só uma operação por endereço por lote; escritas repetidas são barreiras.
        tracks = [r.track for r in self.pending]
        plan = schedule(tracks, self.head, self.algorithm, self.tracks, self.direction)
        for i in plan.order:
            request = self.pending[i]
            if request.operation == "WRITE":
                self._write(request.block, request.data)
                self.labels[request.block] = request.label
            else:
                request.result = self._read(request.block)
            self.history.append(IORequest(request.operation, request.block, request.track, request.label))
        self.pending.clear()
        self.head = plan.path[-1]
        if self.algorithm == "scan" and len(plan.path) > 1:
            for a, b in reversed(list(zip(plan.path, plan.path[1:]))):
                if a != b:
                    self.direction = "up" if b > a else "down"
                    break
        self.total_movement += plan.total
        self.last_schedule = plan
        return plan

class SSD(HDD):
    """FTL: uma página por bloco lógico, escrita fora do lugar, GC por bloco.

    Reserva física de 100%; blocos de apagamento com quatro páginas. Wear
    leveling dinâmico escolhe páginas pelo desgaste, sem simular braço mecânico.
    """
    kind = "ssd"

    def __init__(self, block_size, blocks, **kwargs):
        super().__init__(block_size, blocks, algorithm="fcfs")
        self.pages_per_block = 4
        count = math.ceil((2 * blocks + self.pages_per_block) / self.pages_per_block)
        self.erase_counts = [0] * count
        self.program_counts = [0] * (count * self.pages_per_block)
        self.pages, self.mapping = {}, {}
        self.gc_count = 0

    def _free_pages(self):
        return [p for p in range(len(self.program_counts)) if p not in self.pages]

    def _pick(self, pages):
        return min(pages, key=lambda p: (self.erase_counts[p // self.pages_per_block],
                                         self.program_counts[p], p))

    def _collect(self):
        free = self._free_pages()
        if len(free) >= self.pages_per_block:
            return
        live = set(self.mapping.values())
        candidates = []
        for group, wear in enumerate(self.erase_counts):
            region = set(range(group * self.pages_per_block, (group + 1) * self.pages_per_block))
            invalid = len((region & self.pages.keys()) - live)
            movable = len(region & live)
            available = [p for p in free if p not in region]
            if invalid and movable <= len(available):
                candidates.append((wear, -invalid, group, region, available))
        if not candidates:
            raise NoSpaceError("SSD sem páginas para coleta de lixo")
        _, _, group, region, available = min(candidates, key=lambda c: c[:3])
        for logical, page in list(self.mapping.items()):
            if page in region:
                target = self._pick(available)
                available.remove(target)
                self.pages[target] = self.pages[page]
                self.program_counts[target] += 1
                self.mapping[logical] = target
        for page in region:
            self.pages.pop(page, None)
        self.erase_counts[group] += 1
        self.gc_count += 1

    def _write(self, block, data):
        self._collect()
        page = self._pick(self._free_pages())
        self.pages[page] = data
        self.mapping[block] = page
        self.program_counts[page] += 1

    def _read(self, block):
        page = self.mapping.get(block)
        return self.pages[page] if page is not None else bytes(self.block_size)

    def flush(self):
        if not self.pending:
            return None
        if self.failed:
            raise DiskError("SSD indisponível")
        for request in self.pending:
            if request.operation == "WRITE":
                self._write(request.block, request.data)
                self.labels[request.block] = request.label
            else:
                request.result = self._read(request.block)
            self.history.append(IORequest(request.operation, request.block, request.track, request.label))
        self.pending.clear()
        return None


def xor_blocks(parts, size):
    result = bytearray(size)
    for part in parts:
        for i, byte in enumerate(part):
            result[i] ^= byte
    return bytes(result)


class RAIDArray:
    """RAID 0 (stripe), 1 (espelho), 4 e 5 (XOR), stripe unit de um bloco."""

    def __init__(self, block_size, blocks, level=5, disks=3, **kwargs):
        if level not in (0, 1, 4, 5):
            raise DiskError("nível RAID inválido: use 0, 1, 4 ou 5")
        if disks < (3 if level in (4, 5) else 2):
            raise DiskError("quantidade insuficiente de discos para o nível RAID")
        self.level, self.block_size, self.blocks = level, block_size, blocks
        self.width = 1 if level == 1 else disks - (level in (4, 5))
        self.members = [HDD(block_size, math.ceil(blocks / self.width), **kwargs)
                        for _ in range(disks)]
        self.recoveries = []

    def location(self, block):
        if not 0 <= block < self.blocks:
            raise DiskError(f"bloco lógico RAID inválido: {block}")
        row, slot = divmod(block, self.width)
        if self.level == 1:
            return block, 0, None
        parity = (len(self.members) - 1 if self.level == 4 else
                  len(self.members) - 1 - row % len(self.members)) if self.level in (4, 5) else None
        disks = [i for i in range(len(self.members)) if i != parity]
        return row, disks[slot], parity

    def check_available(self):
        failed = sum(d.failed for d in self.members)
        if (self.level == 0 and failed) or (self.level == 1 and failed == len(self.members)) or (
                self.level in (4, 5) and failed > 1):
            raise DiskError(f"RAID {self.level} indisponível: falhas além da tolerância")

    def flush(self):
        for d in self.members:
            if not d.failed:
                d.flush()

    def _recover(self, row, missing, label=""):
        data = xor_blocks([d.read(row, label + " [XOR]") for i, d in enumerate(self.members)
                           if i != missing], self.block_size)
        self.recoveries.append(f"Stripe {row}: Disco {missing + 1} = XOR dos demais [OK]")
        return data

    def read(self, block, label=""):
        self.check_available()
        row, target, parity = self.location(block)
        if self.level == 1:
            return next(d for d in self.members if not d.failed).read(row, label + " [espelho]")
        if not self.members[target].failed:
            return self.members[target].read(row, label)
        return self._recover(row, target, label)

    def write(self, block, data, label=""):
        self.check_available()
        if len(data) > self.block_size:
            raise DiskError("dados maiores que o bloco RAID")
        row, target, parity = self.location(block)
        padded = bytes(data).ljust(self.block_size, b"\0")
        if self.level == 1:
            for d in self.members:
                if not d.failed:
                    d.write(row, padded, label + " [espelho]")
        elif parity is None:
            self.members[target].write(row, padded, label)
        else:
            # Read-modify-write: lê os dados antigos antes de alterar a stripe.
            if not self.members[parity].failed:
                old = (self._recover(row, target, label) if self.members[target].failed
                       else self.members[target].read(row, label + " [RMW dados]"))
                old_parity = self.members[parity].read(row, label + " [RMW paridade]")
                new_parity = xor_blocks([old, old_parity, padded], self.block_size)
                self.members[parity].write(row, new_parity, label + " [PARIDADE XOR]")
            if not self.members[target].failed:
                self.members[target].write(row, padded, label)

    def inconsistent_rows(self, rows):
        """Verifica a redundância física das stripes que podem ser comparadas."""
        if self.level == 0:
            return
        available = [member for member in self.members if not member.failed]
        if self.level in (4, 5) and len(available) != len(self.members):
            return
        if len(available) < 2:
            return
        for row in sorted(rows):
            parts = [member.read(row, "[auditoria RAID]") for member in available]
            if any(len(part) != self.block_size for part in parts):
                consistent = False
            elif self.level == 1:
                consistent = all(part == parts[0] for part in parts[1:])
            else:
                consistent = not any(xor_blocks(parts, self.block_size))
            if not consistent:
                yield row

    def fail(self, number):
        self._member(number)
        self.flush()
        self.members[number - 1].failed = True

    def _member(self, number):
        if not 1 <= number <= len(self.members):
            raise DiskError(f"disco deve estar entre 1 e {len(self.members)}")
        return self.members[number - 1]

    def rebuild(self, number):
        member = self._member(number)
        if not member.failed:
            raise DiskError("disco selecionado não está em falha")
        self.check_available()
        self.flush()
        # Reconstrói em substituto; só publica o disco quando estiver completo.
        replacement = HDD(self.block_size, member.blocks, algorithm=member.algorithm,
                          tracks=member.tracks, head=member.head, direction=member.direction)
        for row in range(member.blocks):
            data = (next(d for d in self.members if not d.failed).read(row, "rebuild espelho")
                    if self.level == 1 else self._recover(row, number - 1, "rebuild"))
            replacement.write(row, data, "rebuild")
        replacement.flush()
        self.members[number - 1] = replacement
        return member.blocks


def make_storage(kind, block_size, blocks, raid_level=5, disks=3, **kwargs):
    if kind == "hdd":
        return HDD(block_size, blocks, **kwargs)
    if kind == "ssd":
        return SSD(block_size, blocks)
    if kind == "raid":
        return RAIDArray(block_size, blocks, level=raid_level, disks=disks, **kwargs)
    raise DiskError("dispositivo inválido: use hdd, ssd ou raid")


def add_storage_arguments(parser):
    parser.add_argument("--device", choices=("hdd", "ssd", "raid"), default="hdd")
    parser.add_argument("--raid-level", type=int, choices=(0, 1, 4, 5), default=5)
    parser.add_argument("--disks", type=int, default=3, help="discos físicos no arranjo RAID")
    parser.add_argument("--scheduler", choices=("fcfs", "sstf", "scan", "c-scan"), default="sstf")
    parser.add_argument("--tracks", type=int, default=200)
    parser.add_argument("--head", type=int, default=53)
    parser.add_argument("--direction", choices=("up", "down"), default="up")


def storage_from_args(args, block_size):
    return make_storage(args.device, block_size, args.blocks, raid_level=args.raid_level,
                        disks=args.disks, algorithm=args.scheduler, tracks=args.tracks,
                        head=args.head, direction=args.direction)


def devices(storage):
    return storage.members if isinstance(storage, RAIDArray) else [storage]


def render_io(storage):
    lines = ["[FILA DE E/S FÍSICA]"]
    for number, d in enumerate(devices(storage), 1):
        lines.append(f"Disco {number} ({d.kind.upper()}): {'FALHA' if d.failed else 'OK'} | "
                     f"{len(d.pending)} pendentes | {len(d.history)} atendidas")
        if isinstance(d, SSD):
            lines.append("Sem braço mecânico; atendimento FCFS por páginas (FTL).")
        else:
            lines.append(f"Cabeça: {d.head} | Política: {d.algorithm.upper()} | "
                         f"Movimento acumulado: {d.total_movement} trilhas")
        for r in d.pending:
            address = f"bloco lógico {r.block}" if isinstance(d, SSD) else f"bloco {r.block} / trilha {r.track}"
            lines.append(f"  {r.operation} {address}: {r.label}")
        if d.last_schedule:
            lines.append(render_schedule(d.last_schedule))
        for r in d.history[-10:]:
            lines.append(f"  [ATENDIDA] {r.operation} bloco {r.block}: {r.label}")
    return "\n".join(lines)


def render_storage(storage):
    if isinstance(storage, SSD):
        return ("[SSD - FTL / WEAR LEVELING DINÂMICO]\n"
                f"Página: {storage.block_size} B | Páginas por bloco de apagamento: {storage.pages_per_block}\n"
                f"Mapeamento bloco lógico -> página física: {storage.mapping}\n"
                f"Programações por página: {storage.program_counts}\n"
                f"Apagamentos por bloco: {storage.erase_counts}\n"
                f"Coletas de lixo: {storage.gc_count} | Deslocamento: 0 (sem braço)")
    if not isinstance(storage, RAIDArray):
        return f"[HDD] Blocos: {storage.blocks} | Bloco: {storage.block_size} B"
    lines = [f"[RAID {storage.level}] {len(storage.members)} discos físicos | "
             f"Blocos: {storage.block_size} B | Capacidade lógica: {storage.blocks} blocos",
             "[E/S - DISTRIBUIÇÃO EM DISCO]"]
    for i, member in enumerate(storage.members):
        lines.append(f"Disco {i + 1}: {'FALHA' if member.failed else 'OK'}")
        for row in range(member.blocks):
            parity = storage.location(row * storage.width)[2]
            if storage.level == 1:
                role = f"ESPELHO bloco lógico {row}"
            elif i == parity:
                role = "PARIDADE: XOR dos blocos de dados"
            else:
                data_members = [j for j in range(len(storage.members)) if j != parity]
                logical = row * storage.width + data_members.index(i)
                if logical >= storage.blocks:
                    continue
                role = f"DADOS bloco lógico {logical}"
            if row in member.data or member.failed:
                preview = member.data.get(row, b"")[:16].hex()
                label = member.labels.get(row, "")
                lines.append(f"  Stripe {row}: {role} | {label} | bytes: {preview or 'zerado'}")
    lines.extend(storage.recoveries[-10:])
    return "\n".join(lines)
