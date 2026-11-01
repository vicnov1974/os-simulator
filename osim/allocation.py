"""Estratégias de alocação física de blocos (Strategy Pattern).

Cada estratégia decide QUAIS blocos de dados um arquivo recebe. O método
`choose(disk, n, prefer)` é chamado com os blocos antigos do arquivo já
liberados (`prefer`), para permitir crescer/encolher "no lugar" quando possível.
"""
from abc import ABC, abstractmethod

from .disk import NoSpaceError

POINTER_SIZE = 4  # bytes por ponteiro de bloco (i-node simulado)


class AllocationStrategy(ABC):
    key = ""
    title = ""
    uses_file_inode = False   # arquivo precisa de um bloco de metadados (i-node)?
    contiguous = False        # exige blocos contíguos?

    @abstractmethod
    def choose(self, disk, n: int, prefer: list) -> list:
        """Retorna a lista (ordenada) de n blocos de dados."""

    def max_blocks(self, disk):
        """Limite de blocos por arquivo (None = sem limite)."""
        return None

    def dir_label(self, is_root: bool) -> str:
        return "DIR_RAIZ" if is_root else "DIR"

    def data_note(self, node, i: int, disk) -> str:
        return ""


class ContiguousAllocation(AllocationStrategy):
    """Arquivo ocupa blocos consecutivos (first-fit). Sofre fragmentação externa."""
    key = "contiguous"
    title = "Contígua"
    contiguous = True

    def choose(self, disk, n, prefer):
        if n == 0:
            return []
        # 1) tenta manter o arquivo no mesmo lugar (crescer/encolher in-place)
        if prefer:
            s = prefer[0]
            if s + n <= disk.total_blocks and all(disk.is_free(b) for b in range(s, s + n)):
                return list(range(s, s + n))
        # 2) first-fit: primeira região livre grande o bastante
        for start, length in disk.free_runs():
            if length >= n:
                return list(range(start, start + n))
        largest = max((l for _, l in disk.free_runs()), default=0)
        if disk.free_count >= n:
            raise NoSpaceError(
                f"fragmentação externa: há {disk.free_count} blocos livres, mas a maior "
                f"região contígua tem só {largest} (necessário: {n}). "
                f"Execute 'compact' para compactar o disco."
            )
        raise NoSpaceError(f"disco cheio: necessário {n} bloco(s), livres {disk.free_count}")


class _NonContiguous(AllocationStrategy):
    """Base para estratégias que aceitam qualquer bloco livre."""

    def choose(self, disk, n, prefer):
        keep = list(prefer[:n])
        need = n - len(keep)
        if need > 0:
            taken = set(keep)
            extra = [b for b in disk.free_blocks() if b not in taken][:need]
            if len(extra) < need:
                raise NoSpaceError(
                    f"disco cheio: necessário {n} bloco(s) de dados, "
                    f"disponíveis {len(extra) + len(keep)}"
                )
            keep += extra
        return keep


class LinkedAllocation(_NonContiguous):
    """Encadeada (estilo FAT): cada bloco aponta para o próximo."""
    key = "linked"
    title = "Encadeada (FAT)"

    def data_note(self, node, i, disk):
        if i + 1 < len(node.blocks):
            return f" -> próximo: {node.blocks[i + 1]:02d}"
        return " -> FIM (EOF)"


class IndexedAllocation(_NonContiguous):
    """Indexada: um bloco i-node por arquivo guarda os ponteiros dos dados."""
    key = "indexed"
    title = "Indexada (i-node)"
    uses_file_inode = True

    def max_blocks(self, disk):
        return disk.block_size // POINTER_SIZE

    def dir_label(self, is_root):
        return "INODE_RAIZ" if is_root else "INODE_DIR"


STRATEGIES = {
    "contiguous": ContiguousAllocation,
    "linked": LinkedAllocation,
    "indexed": IndexedAllocation,
}

_ALIASES = {"contigua": "contiguous", "contígua": "contiguous", "encadeada": "linked",
            "fat": "linked", "indexada": "indexed", "inode": "indexed", "i-node": "indexed"}


def make_strategy(name: str) -> AllocationStrategy:
    key = _ALIASES.get(name.lower(), name.lower())
    if key not in STRATEGIES:
        raise ValueError(f"estratégia desconhecida: '{name}' "
                         f"(opções: {', '.join(STRATEGIES)})")
    return STRATEGIES[key]()
