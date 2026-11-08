"""Camada física: disco simulado dividido em blocos de tamanho fixo.

O disco só sabe QUEM ocupa cada bloco e guarda os bytes brutos. A decisão de
ONDE alocar é responsabilidade das estratégias (allocation.py).
"""
from dataclasses import dataclass
from copy import deepcopy


class DiskError(Exception):
    """Erro genérico da camada de disco."""


class NoSpaceError(DiskError):
    """Não há espaço (ou espaço utilizável) para a alocação pedida."""


@dataclass(frozen=True, eq=False)
class BlockOwner:
    """Dono de um bloco. kind: DIR_META | FILE_INODE | DATA."""
    node: object
    kind: str
    index: int = 0          # posição do bloco dentro do arquivo (kind == DATA)


class Disk:
    def __init__(self, block_size: int, total_blocks: int, storage=None):
        if block_size <= 0 or total_blocks <= 0:
            raise DiskError("tamanho de bloco e número de blocos devem ser positivos")
        self.block_size = block_size
        self.total_blocks = total_blocks
        self.owner = [None] * total_blocks     # BlockOwner | None
        self.raw = {}                          # bloco -> bytes
        from .storage import HDD
        self.storage = storage or HDD(block_size, total_blocks)
        if self.storage.block_size != block_size or self.storage.blocks != total_blocks:
            raise DiskError("geometria física incompatível com o disco lógico")

    # ---- consultas ---------------------------------------------------
    @property
    def capacity(self) -> int:
        return self.block_size * self.total_blocks

    @property
    def free_count(self) -> int:
        return sum(1 for o in self.owner if o is None)

    @property
    def used_count(self) -> int:
        return self.total_blocks - self.free_count

    def is_free(self, b: int) -> bool:
        return 0 <= b < self.total_blocks and self.owner[b] is None

    def first_free(self):
        return next((i for i, o in enumerate(self.owner) if o is None), None)

    def free_blocks(self) -> list:
        return [i for i, o in enumerate(self.owner) if o is None]

    def free_runs(self) -> list:
        """Regiões livres contíguas: lista de (início, comprimento)."""
        runs, start = [], None
        for i, o in enumerate(self.owner):
            if o is None and start is None:
                start = i
            elif o is not None and start is not None:
                runs.append((start, i - start))
                start = None
        if start is not None:
            runs.append((start, self.total_blocks - start))
        return runs

    # ---- modificações ------------------------------------------------
    def claim(self, b: int, node, kind: str, index: int = 0) -> None:
        if not self.is_free(b):
            raise DiskError(f"bloco {b} indisponível")
        self.owner[b] = BlockOwner(node, kind, index)
        if kind != "DATA":
            try:
                self.write(b, b"")  # metadados também geram uma escrita física
            except Exception:
                self.owner[b] = None
                raise

    def release(self, b: int) -> None:
        self._check_block(b)
        self.storage.write(b, b"", self._label(b) + " [liberação]")
        self.owner[b] = None
        self.raw.pop(b, None)

    def write(self, b: int, data: bytes) -> None:
        self._check_block(b)
        if len(data) > self.block_size:
            raise DiskError("dados maiores que o bloco")
        self.storage.write(b, bytes(data), self._label(b))
        self.raw[b] = bytes(data)

    def read(self, b: int) -> bytes:
        self._check_block(b)
        return self.storage.read(b, self._label(b))[:len(self.raw.get(b, b""))]

    def _check_block(self, b):
        if not 0 <= b < self.total_blocks:
            raise DiskError(f"bloco lógico inválido: {b}")

    def _label(self, b):
        owner = self.owner[b]
        return f"{owner.node.path()} [{owner.kind}]" if owner else "[livre]"

    def flush(self):
        return self.storage.flush()

    def checkpoint(self):
        self.flush()
        return list(self.owner), dict(self.raw), deepcopy(self.storage)

    def restore(self, state):
        owner, raw, storage = state
        self.owner, self.raw, self.storage = list(owner), dict(raw), storage

    def configure_storage(self, kind, **options):
        """Migra os bytes para outro dispositivo antes de publicar a mudança."""
        from .storage import make_storage
        replacement = make_storage(kind, self.block_size, self.total_blocks, **options)
        contents = {b: self.read(b) for b, owner in enumerate(self.owner) if owner is not None}
        for b, data in contents.items():
            replacement.write(b, data, self._label(b) + " [migração]")
        replacement.flush()
        self.storage = replacement

    def reset(self) -> None:
        for b, owner in enumerate(self.owner):
            if owner is not None:
                self.storage.write(b, b"", "[reset]")
        self.owner = [None] * self.total_blocks
        self.raw = {}
