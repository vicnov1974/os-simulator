"""Sistema de arquivos hierárquico (árvore) sobre o disco simulado.

- Caminhos absolutos (/a/b) e relativos (a/b, ./a, ../a), com diretório corrente.
- CRUD de diretórios e arquivos.
- Cada diretório ocupa 1 bloco de metadados; arquivos ocupam blocos de dados
  (e 1 bloco de i-node na alocação indexada).
"""
import math

from .allocation import AllocationStrategy, make_strategy
from .disk import Disk, NoSpaceError


class FSError(Exception):
    """Erro de operação no sistema de arquivos."""


class Node:
    def __init__(self, name: str, parent, is_dir: bool):
        self.name = name
        self.parent = parent
        self.is_dir = is_dir
        self.children = {}          # nome -> Node (apenas diretórios)
        self.size = 0               # bytes reais (apenas arquivos)
        self.meta_block = None      # bloco do diretório / i-node
        self.blocks = []            # blocos de dados, em ordem lógica

    @property
    def is_root(self) -> bool:
        return self.parent is None

    def path(self) -> str:
        parts, n = [], self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))


class FileSystem:
    def __init__(self, disk: Disk, strategy: AllocationStrategy):
        self.disk = disk
        self.strategy = strategy
        self.root = Node("", None, True)
        self.cwd = self.root
        self._alloc_meta(self.root, "DIR_META")

    # ------------------------------------------------------------------
    # Resolução de caminhos
    # ------------------------------------------------------------------
    def _parts(self, path: str):
        if not path:
            raise FSError("caminho vazio")
        start = self.root if path.startswith("/") else self.cwd
        return start, [p for p in path.split("/") if p not in ("", ".")]

    def _walk_parts(self, node: Node, parts: list, original: str) -> Node:
        for p in parts:
            if p == "..":
                node = node.parent or node
                continue
            if not node.is_dir:
                raise FSError(f"'{node.name}' não é um diretório")
            if p not in node.children:
                raise FSError(f"'{original}': arquivo ou diretório inexistente")
            node = node.children[p]
        return node

    def resolve(self, path: str) -> Node:
        start, parts = self._parts(path)
        return self._walk_parts(start, parts, path)

    def _resolve_parent(self, path: str):
        start, parts = self._parts(path)
        if not parts or parts[-1] == "..":
            raise FSError(f"nome inválido em '{path}'")
        parent = self._walk_parts(start, parts[:-1], path)
        if not parent.is_dir:
            raise FSError(f"'{parent.name}' não é um diretório")
        return parent, parts[-1]

    @staticmethod
    def _check_name(name: str) -> None:
        if "\0" in name or len(name) > 255:
            raise FSError("nome de arquivo inválido")

    def chdir(self, path: str) -> None:
        node = self.resolve(path)
        if not node.is_dir:
            raise FSError(f"'{path}' não é um diretório")
        self.cwd = node

    # ------------------------------------------------------------------
    # Navegação / consulta
    # ------------------------------------------------------------------
    @staticmethod
    def sorted_children(node: Node) -> list:
        return sorted(node.children.values(), key=lambda c: (not c.is_dir, c.name))

    def walk(self, start: Node = None):
        """Percorre a árvore em pré-ordem (diretórios antes de arquivos)."""
        start = start or self.root
        yield start
        if start.is_dir:
            for child in self.sorted_children(start):
                yield from self.walk(child)

    def files(self) -> list:
        return [n for n in self.walk() if not n.is_dir]

    def tree(self, path: str = "/") -> str:
        from .utils import format_size

        def label(n: Node) -> str:
            if n.is_root:
                return "/"
            if n.is_dir:
                return f"{n.name}/ [DIR]"
            return f"{n.name} ({format_size(n.size)}) [ARQ]"

        node = self.resolve(path)
        if node.is_root:
            lines = ["/"]
        elif node.is_dir:
            lines = [f"{node.path()} [DIR]"]
        else:
            lines = [f"{node.path()} ({format_size(node.size)}) [ARQ]"]

        def rec(n: Node, prefix: str):
            kids = self.sorted_children(n)
            for i, c in enumerate(kids):
                last = i == len(kids) - 1
                lines.append(prefix + ("└── " if last else "├── ") + label(c))
                if c.is_dir:
                    rec(c, prefix + ("    " if last else "│   "))

        if node.is_dir:
            rec(node, "")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Alocação de metadados e dados
    # ------------------------------------------------------------------
    def _alloc_meta(self, node: Node, kind: str) -> None:
        b = self.disk.first_free()
        if b is None:
            raise NoSpaceError("disco cheio: sem bloco livre para metadados")
        self.disk.claim(b, node, kind)
        node.meta_block = b

    def _free_node(self, node: Node) -> None:
        for b in node.blocks:
            self.disk.release(b)
        node.blocks = []
        if node.meta_block is not None:
            self.disk.release(node.meta_block)
            node.meta_block = None

    def _store(self, node: Node, data: bytes) -> None:
        """Grava o conteúdo completo de um arquivo (realoca se necessário)."""
        bs = self.disk.block_size
        n = math.ceil(len(data) / bs)
        limit = self.strategy.max_blocks(self.disk)
        if limit is not None and n > limit:
            raise FSError(f"arquivo excede o limite do i-node ({limit} blocos = "
                          f"{limit * bs} bytes)")
        old = list(node.blocks)
        old_data = [self.disk.read(b) for b in old]
        for b in old:
            self.disk.release(b)
        try:
            new = self.strategy.choose(self.disk, n, old)
        except NoSpaceError:                      # rollback: restaura estado anterior
            for i, b in enumerate(old):
                self.disk.claim(b, node, "DATA", i)
                self.disk.write(b, old_data[i])
            raise
        for i, b in enumerate(new):
            self.disk.claim(b, node, "DATA", i)
            self.disk.write(b, data[i * bs:(i + 1) * bs])
        node.blocks = new
        node.size = len(data)

    # ------------------------------------------------------------------
    # CRUD - diretórios
    # ------------------------------------------------------------------
    def _new_dir(self, parent: Node, name: str) -> Node:
        self._check_name(name)
        node = Node(name, parent, True)
        self._alloc_meta(node, "DIR_META")
        parent.children[name] = node
        return node

    def mkdir(self, path: str, parents: bool = False) -> Node:
        if parents:
            node, parts = self._parts(path)
            for p in parts:
                if p == "..":
                    node = node.parent or node
                    continue
                child = node.children.get(p)
                if child is None:
                    child = self._new_dir(node, p)
                elif not child.is_dir:
                    raise FSError(f"'{child.path()}' já existe e não é um diretório")
                node = child
            return node
        parent, name = self._resolve_parent(path)
        if name in parent.children:
            raise FSError(f"'{path}' já existe")
        return self._new_dir(parent, name)

    # ------------------------------------------------------------------
    # CRUD - arquivos
    # ------------------------------------------------------------------
    def create(self, path: str, size: int = None, content: bytes = b"") -> Node:
        parent, name = self._resolve_parent(path)
        if name in parent.children:
            raise FSError(f"'{path}' já existe")
        self._check_name(name)
        data = content if size is None else content[:size].ljust(size, b"\0")
        node = Node(name, parent, False)
        try:
            if self.strategy.uses_file_inode:
                self._alloc_meta(node, "FILE_INODE")
            self._store(node, data)
        except Exception:
            self._free_node(node)
            raise
        parent.children[name] = node
        return node

    def _file(self, path: str) -> Node:
        node = self.resolve(path)
        if node.is_dir:
            raise FSError(f"'{path}' é um diretório")
        return node

    def read_node(self, node: Node) -> bytes:
        return b"".join(self.disk.read(b) for b in node.blocks)[:node.size]

    def read(self, path: str) -> bytes:
        return self.read_node(self._file(path))

    def write(self, path: str, data: bytes, append: bool = False) -> Node:
        """Atualiza o conteúdo (sobrescreve ou anexa). Cria o arquivo se não existir."""
        try:
            node = self._file(path)
        except FSError as exc:
            if "inexistente" not in str(exc):
                raise
            return self.create(path, content=data)
        self._store(node, (self.read_node(node) + data) if append else data)
        return node

    def resize(self, path: str, size: int) -> Node:
        node = self._file(path)
        data = self.read_node(node)
        self._store(node, data[:size].ljust(size, b"\0"))
        return node

    def _contains(self, ancestor: Node, node: Node) -> bool:
        while node is not None:
            if node is ancestor:
                return True
            node = node.parent
        return False

    def remove(self, path: str, recursive: bool = False, only_dir: bool = False) -> int:
        """Remove arquivo/diretório. Retorna quantos blocos foram liberados."""
        node = self.resolve(path)
        if node.is_root:
            raise FSError("não é possível remover o diretório raiz")
        if only_dir and not node.is_dir:
            raise FSError(f"'{path}' não é um diretório")
        if node.is_dir and not only_dir and not recursive:
            raise FSError(f"'{path}' é um diretório (use rm -r ou rmdir)")
        if node.is_dir and node.children and not recursive:
            raise FSError(f"diretório '{path}' não está vazio (use rm -r)")
        if self._contains(node, self.cwd):
            self.cwd = node.parent
        freed = 0
        for n in list(self.walk(node)):
            freed += len(n.blocks) + (1 if n.meta_block is not None else 0)
            self._free_node(n)
        del node.parent.children[node.name]
        return freed

    def move(self, src: str, dst: str) -> Node:
        node = self.resolve(src)
        if node.is_root:
            raise FSError("não é possível mover o diretório raiz")
        try:
            target = self.resolve(dst)
        except FSError:
            target = None
        if target is not None and target.is_dir:
            parent, name = target, node.name
        elif target is not None:
            if target is node:
                return node
            raise FSError(f"'{dst}' já existe")
        else:
            parent, name = self._resolve_parent(dst)
        if parent is node.parent and name == node.name:
            return node
        if name in parent.children:
            raise FSError(f"'{name}' já existe em '{parent.path()}'")
        if node.is_dir and self._contains(node, parent):
            raise FSError("não é possível mover um diretório para dentro de si mesmo")
        self._check_name(name)
        del node.parent.children[node.name]
        node.name, node.parent = name, parent
        parent.children[name] = node
        return node

    def copy(self, src: str, dst: str) -> Node:
        node = self._file(src)
        data = self.read_node(node)
        try:
            target = self.resolve(dst)
        except FSError:
            target = None
        if target is not None and target.is_dir:
            dst = target.path().rstrip("/") + "/" + node.name
        return self.create(dst, content=data)

    # ------------------------------------------------------------------
    # Re-layout: compactação e troca de estratégia
    # ------------------------------------------------------------------
    def _snapshot(self, nodes):
        return (list(self.disk.owner), dict(self.disk.raw),
                [(n, n.meta_block, list(n.blocks), n.size) for n in nodes], self.strategy)

    def _restore(self, snap):
        owner, raw, node_state, strategy = snap
        self.disk.owner, self.disk.raw, self.strategy = list(owner), dict(raw), strategy
        for n, meta, blocks, size in node_state:
            n.meta_block, n.blocks, n.size = meta, list(blocks), size

    def relayout(self, strategy: AllocationStrategy = None) -> int:
        """Reescreve todo o disco de forma compacta, na ordem da árvore.

        Usado por `compact` (elimina fragmentação externa) e por `set_alloc`
        (migra os arquivos para outra estratégia). Retorna nº de blocos movidos.
        Operação atômica: em caso de falha o estado anterior é restaurado.
        """
        nodes = list(self.walk())
        contents = {n: self.read_node(n) for n in nodes if not n.is_dir}
        old_pos = {n: ([n.meta_block], list(n.blocks)) for n in nodes}
        snap = self._snapshot(nodes)
        if strategy is not None:
            self.strategy = strategy
        try:
            self.disk.reset()
            for n in nodes:
                n.meta_block, n.blocks = None, []
            for n in nodes:
                if n.is_dir:
                    self._alloc_meta(n, "DIR_META")
                else:
                    n.size = 0
                    if self.strategy.uses_file_inode:
                        self._alloc_meta(n, "FILE_INODE")
                    self._store(n, contents[n])
        except Exception:
            self._restore(snap)
            raise
        moved = 0
        for n in nodes:
            old_meta, old_blocks = old_pos[n]
            moved += int(old_meta != [n.meta_block])
            moved += sum(1 for i, b in enumerate(n.blocks)
                         if i >= len(old_blocks) or old_blocks[i] != b)
        return moved

    def compact(self) -> int:
        return self.relayout()

    def change_strategy(self, name: str) -> int:
        return self.relayout(make_strategy(name))
