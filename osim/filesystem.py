"""Sistema de arquivos hierárquico (árvore) sobre o disco simulado.

- Caminhos absolutos (/a/b) e relativos (a/b, ./a, ../a), com diretório corrente.
- CRUD de diretórios e arquivos.
- Cada diretório ocupa 1 bloco de metadados; arquivos ocupam blocos de dados
  (e 1 bloco de i-node na alocação indexada).
"""
import hashlib
import math

from .allocation import AllocationStrategy, make_strategy
from .disk import Disk, DiskError, NoSpaceError
from .security import (IntegrityError, Security, SecurityError, SIGNATURES, TAG_BYTES,
                       decrypt, encrypt, parse_mode)
from .storage import RAIDArray


class FSError(Exception):
    """Erro de operação no sistema de arquivos."""


class NotFoundError(FSError):
    """O caminho acessível não contém o objeto solicitado."""


class Node:
    def __init__(self, name: str, parent, is_dir: bool, owner="root", group="admin"):
        self.name = name
        self.parent = parent
        self.is_dir = is_dir
        self.children = {}          # nome -> Node (apenas diretórios)
        self.size = 0               # bytes reais (apenas arquivos)
        self.meta_block = None      # bloco do diretório / i-node
        self.blocks = []            # blocos de dados, em ordem lógica
        self.owner, self.group = owner, group
        self.mode = 0o755 if is_dir else 0o644
        self.encryption = None
        self.block_hashes = ()

    @property
    def stored_size(self):
        return self.size + (TAG_BYTES if self.encryption else 0)

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
    def __init__(self, disk: Disk, strategy: AllocationStrategy, security=None):
        self.disk = disk
        self.strategy = strategy
        self.security = security if security is not None else Security()
        self.root = Node("", None, True)
        self.cwd = self.root
        self._alloc_meta(self.root, "DIR_META")

    # ------------------------------------------------------------------
    # Resolução de caminhos
    # ------------------------------------------------------------------
    def _parts(self, path: str):
        self.security.require_session()
        if not path:
            raise FSError("caminho vazio")
        start = self.root if path.startswith("/") else self.cwd
        return start, [p for p in path.split("/") if p not in ("", ".")]

    def _walk_parts(self, node: Node, parts: list, original: str) -> Node:
        self._check_ancestors(node)
        for p in parts:
            if not node.is_dir:
                raise FSError(f"'{node.name}' não é um diretório")
            self.security.require(node, "x")
            if p == "..":
                node = node.parent or node
                continue
            if p not in node.children:
                raise NotFoundError(f"'{original}': arquivo ou diretório inexistente")
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
        self.security.require(node, "x")
        self.cwd = node

    # ------------------------------------------------------------------
    # Navegação / consulta
    # ------------------------------------------------------------------
    def _ancestors(self, node):
        parents = []
        while node.parent is not None:
            if node.parent.children.get(node.name) is not node:
                raise NotFoundError("objeto removido do sistema de arquivos")
            node = node.parent
            parents.append(node)
        if node is not self.root:
            raise NotFoundError("objeto removido do sistema de arquivos")
        return reversed(parents)

    def _check_ancestors(self, node):
        for parent in self._ancestors(node):
            self.security.require(parent, "x")

    @staticmethod
    def _sorted_children(node):
        return sorted(node.children.values(), key=lambda c: (not c.is_dir, c.name))

    def sorted_children(self, node: Node) -> list:
        self._check_ancestors(node)
        self.security.require(node, "rx")
        return self._sorted_children(node)

    def visible_children(self, node: Node) -> list:
        if not self.security.allows(node, "rx"):
            return []
        if not all(self.security.allows(parent, "x") for parent in self._ancestors(node)):
            return []
        return self._sorted_children(node)

    def walk(self, start: Node = None):
        """Percorre a árvore em pré-ordem (diretórios antes de arquivos)."""
        start = start or self.root
        self.security.require_session()
        self._check_ancestors(start)
        yield from self._walk(start, self.visible_children)

    def _walk(self, start, children):
        yield start
        if start.is_dir:
            for child in children(start):
                yield from self._walk(child, children)

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
        if node.is_dir:
            self.security.require(node, "rx")
        if node.is_root:
            lines = ["/"]
        elif node.is_dir:
            lines = [f"{node.path()} [DIR]"]
        else:
            lines = [f"{node.path()} ({format_size(node.size)}) [ARQ]"]

        def rec(n: Node, prefix: str):
            kids = self.visible_children(n)
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

    def _store(self, node: Node, data: bytes, seal=None) -> None:
        """Grava o conteúdo completo de um arquivo (realoca se necessário)."""
        bs = self.disk.block_size
        n = math.ceil(len(data) / bs)
        limit = self.strategy.max_blocks(self.disk)
        if limit is not None and n > limit:
            raise FSError(f"arquivo excede o limite do i-node ({limit} blocos = "
                          f"{limit * bs} bytes)")
        chunks = [data[i * bs:(i + 1) * bs] for i in range(n)]
        hashes = tuple(hashlib.sha256(chunk.ljust(bs, b"\0")).digest() for chunk in chunks)
        old = list(node.blocks)
        snap = self.disk.checkpoint()
        try:
            for b in old:
                self.disk.release(b)
            new = self.strategy.choose(self.disk, n, old)
            for i, (b, chunk) in enumerate(zip(new, chunks)):
                self.disk.claim(b, node, "DATA", i)
                self.disk.write(b, chunk)
            if seal or node.encryption:
                self.disk.flush()
        except Exception:                         # inclui falhas físicas e RAID
            self.disk.restore(snap)
            raise
        node.blocks = new
        node.encryption = seal
        node.size = len(data) - (TAG_BYTES if seal else 0)
        node.block_hashes = hashes

    # ------------------------------------------------------------------
    # CRUD - diretórios
    # ------------------------------------------------------------------
    def _new_dir(self, parent: Node, name: str) -> Node:
        self.security.require(parent, "wx")
        self._check_name(name)
        user = self.security.current
        node = Node(name, parent, True, user.name, user.primary_group)
        self._alloc_meta(node, "DIR_META")
        parent.children[name] = node
        return node

    def mkdir(self, path: str, parents: bool = False) -> Node:
        if parents:
            node, parts = self._parts(path)
            self._check_ancestors(node)
            for p in parts:
                self.security.require(node, "x")
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
    def create(self, path: str, size: int = None, content: bytes = b"", key=None) -> Node:
        data = content if size is None else content[:size].ljust(size, b"\0")
        if key is not None:
            ciphertext, seal = encrypt(data, key)
            return self._create(path, ciphertext, seal)
        return self._create(path, data)

    def _create(self, path, data, seal=None):
        parent, name = self._resolve_parent(path)
        self.security.require(parent, "wx")
        if name in parent.children:
            raise FSError(f"'{path}' já existe")
        self._check_name(name)
        user = self.security.current
        node = Node(name, parent, False, user.name, user.primary_group)
        snap = self.disk.checkpoint()
        try:
            if self.strategy.uses_file_inode:
                self._alloc_meta(node, "FILE_INODE")
            self._store(node, data, seal)
        except Exception:
            self.disk.restore(snap)
            raise
        parent.children[name] = node
        return node

    def _file(self, path: str) -> Node:
        node = self.resolve(path)
        if node.is_dir:
            raise FSError(f"'{path}' é um diretório")
        return node

    def _read_meta(self, node):
        if node.meta_block is not None:
            if self.disk.read_physical(node.meta_block) != bytes(self.disk.block_size):
                self._integrity_error(node, node.meta_block)

    def _integrity_error(self, node, block):
        self.security.record("integridade", node.path(), f"ALERTA bloco {block}")
        raise IntegrityError(f"Hash do bloco {block} difere do registro oficial de '{node.path()}'")

    def _load(self, node):
        self._read_meta(node)
        if len(node.blocks) != len(node.block_hashes):
            self._integrity_error(node, "de dados (mapa divergente)")
        chunks = []
        for b, expected in zip(node.blocks, node.block_hashes):
            chunk = self.disk.read_physical(b)
            if hashlib.sha256(chunk).digest() != expected:
                self._integrity_error(node, b)
            chunks.append(chunk)
        return b"".join(chunks)[:node.stored_size]

    def _unlock(self, node, data, key):
        try:
            return decrypt(data, node.encryption, key)
        except SecurityError:
            self.security.record("descriptografia", node.path(), "NEGADO")
            raise

    def read_node(self, node: Node, key=None) -> bytes:
        self._check_ancestors(node)
        self.security.require(node, "r")
        if node.is_dir:
            raise FSError(f"'{node.path()}' é um diretório")
        data = self._load(node)
        return self._unlock(node, data, key)[0] if node.encryption else data

    def read(self, path: str, key=None) -> bytes:
        return self.read_node(self._file(path), key)

    def write(self, path: str, data: bytes, append: bool = False, key=None) -> Node:
        """Atualiza o conteúdo (sobrescreve ou anexa). Cria o arquivo se não existir."""
        try:
            node = self._file(path)
        except NotFoundError:
            return self.create(path, content=data, key=key)
        self.security.require(node, "w")
        if node.encryption:
            previous, derived = self._unlock(node, self._load(node), key)
            new_data = previous + data if append else data
            ciphertext, seal = encrypt(new_data, None, key=derived, salt=node.encryption.salt)
            self._store(node, ciphertext, seal)
        else:
            new_data = self._load(node) + data if append else data
            if key is None:
                self._store(node, new_data)
            else:
                ciphertext, seal = encrypt(new_data, key)
                self._store(node, ciphertext, seal)
        return node

    def resize(self, path: str, size: int, key=None) -> Node:
        node = self._file(path)
        self.security.require(node, "w")
        data = self._load(node)
        if node.encryption:
            data, derived = self._unlock(node, data, key)
            data = data[:size].ljust(size, b"\0")
            ciphertext, seal = encrypt(data, None, key=derived, salt=node.encryption.salt)
            self._store(node, ciphertext, seal)
        elif key is not None:
            ciphertext, seal = encrypt(data[:size].ljust(size, b"\0"), key)
            self._store(node, ciphertext, seal)
        else:
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
        self.security.require(node.parent, "wx")
        nodes = list(self._walk(node, self._sorted_children))
        for child in nodes:
            if child.is_dir and child.children:
                self.security.require(child, "rwx")
        snap = self._snapshot(nodes)
        freed = 0
        try:
            for n in nodes:
                freed += len(n.blocks) + (1 if n.meta_block is not None else 0)
                self._free_node(n)
        except Exception:
            self._restore(snap)
            raise
        if self._contains(node, self.cwd):
            self.cwd = node.parent
        del node.parent.children[node.name]
        return freed

    def move(self, src: str, dst: str) -> Node:
        node = self.resolve(src)
        if node.is_root:
            raise FSError("não é possível mover o diretório raiz")
        try:
            target = self.resolve(dst)
        except NotFoundError:
            target = None
        if target is not None and target.is_dir:
            parent, name = target, node.name
        elif target is not None:
            if target is node:
                return node
            raise FSError(f"'{dst}' já existe")
        else:
            parent, name = self._resolve_parent(dst)
        self.security.require(node.parent, "wx")
        self.security.require(parent, "wx")
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
        self.security.require(node, "r")
        data = self._load(node)
        try:
            target = self.resolve(dst)
        except NotFoundError:
            target = None
        if target is not None and target.is_dir:
            dst = target.path().rstrip("/") + "/" + node.name
        return self._create(dst, data, node.encryption)

    # ------------------------------------------------------------------
    # Re-layout: compactação e troca de estratégia
    # ------------------------------------------------------------------
    def _snapshot(self, nodes):
        return (self.disk.checkpoint(),
                [(n, n.meta_block, list(n.blocks), n.size, n.encryption, n.block_hashes)
                 for n in nodes], self.strategy)

    def _restore(self, snap):
        disk_state, node_state, strategy = snap
        self.disk.restore(disk_state)
        self.strategy = strategy
        for n, meta, blocks, size, seal, hashes in node_state:
            n.meta_block, n.blocks, n.size = meta, list(blocks), size
            n.encryption, n.block_hashes = seal, hashes

    def relayout(self, strategy: AllocationStrategy = None) -> int:
        """Reescreve todo o disco de forma compacta, na ordem da árvore.

        Usado por `compact` (elimina fragmentação externa) e por `set_alloc`
        (migra os arquivos para outra estratégia). Retorna nº de blocos movidos.
        Operação atômica: em caso de falha o estado anterior é restaurado.
        """
        self.security.require_kernel("compactação ou troca de alocação")
        nodes = list(self.walk())
        contents = {n: self._load(n) for n in nodes}
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
                    self._store(n, contents[n], n.encryption)
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

    def login(self, name, password):
        self.security.login(name, password)
        self.cwd = self.root

    def logout(self):
        self.security.logout()
        self.cwd = self.root

    def chmod(self, path, mode):
        node = self.resolve(path)
        self.security.require_owner(node)
        node.mode = parse_mode(mode)
        self.security.record("chmod", node.path(), f"OK {node.mode:03o}")
        # Uma mudança de permissão pode invalidar a pasta em que a sessão está.
        if not self.security.allows(self.cwd, "x") or any(
                not self.security.allows(parent, "x") for parent in self._ancestors(self.cwd)):
            self.cwd = self.root
        return node

    def chown(self, path, owner, group=None):
        self.security.require_kernel("alteração de proprietário")
        if owner not in self.security.users:
            raise SecurityError(f"usuário '{owner}' inexistente")
        group = self.security.users[owner].primary_group if group is None else group
        if group not in self.security.groups:
            raise SecurityError(f"grupo '{group}' inexistente")
        node = self.resolve(path)
        node.owner, node.group = owner, group
        self.security.record("chown", node.path(), f"OK {owner}:{group}")
        return node

    def check_execute(self, path):
        node = self._file(path)
        self.security.require(node, "x")
        self.security.record("execução simulada", node.path(), "OK")
        return node

    def encrypt_file(self, path, key):
        node = self._file(path)
        self.security.require(node, "r")
        self.security.require(node, "w")
        if node.encryption:
            raise SecurityError("arquivo já criptografado")
        data, seal = encrypt(self._load(node), key)
        self._store(node, data, seal)
        self.security.record("criptografia", node.path(), "OK")
        return node, data[:12]

    def decrypt_file(self, path, key):
        node = self._file(path)
        self.security.require(node, "r")
        self.security.require(node, "w")
        if node.encryption is None:
            raise SecurityError("arquivo não criptografado")
        data, _ = self._unlock(node, self._load(node), key)
        self._store(node, data)
        self.security.record("descriptografia", node.path(), "OK")
        return node

    def antivirus_scan(self, path="/"):
        self.security.require_kernel("varredura de integridade")
        findings = []
        storage = self.disk.storage
        rows = set()
        for node in self.walk(self.resolve(path)):
            if isinstance(storage, RAIDArray):
                blocks = node.blocks if node.meta_block is None else [node.meta_block] + node.blocks
                rows.update(storage.location(b)[0] for b in blocks)
            try:
                data = self._load(node)
            except (IntegrityError, DiskError) as exc:
                findings.append((node.path(), f"[ALERTA] {exc}"))
                continue
            if node.is_dir:
                continue
            if node.encryption:
                status = "[OK - Criptografado e Íntegro] (assinaturas indisponíveis)"
            elif any(signature in data for signature in SIGNATURES):
                status = "[ALERTA] Assinatura maliciosa simulada"
                self.security.record("assinatura", node.path(), "ALERTA")
            else:
                status = "[OK - Íntegro]"
            findings.append((node.path(), status))
        if isinstance(storage, RAIDArray):
            for row in storage.inconsistent_rows(rows):
                target = f"RAID {storage.level} / stripe {row}"
                findings.append((target, "[ALERTA] Espelho ou paridade divergente"))
                self.security.record("integridade RAID", target, "ALERTA")
        alerts = sum(status.startswith("[ALERTA]") for _, status in findings)
        self.security.record("varredura", path, f"{alerts} alerta(s)")
        return findings

    def simulate_corruption(self, path, index=0):
        self.security.require_kernel("injeção de falha em bloco")
        node = self._file(path)
        if not 0 <= index < len(node.blocks):
            raise FSError("índice de bloco de dados inválido")
        block = node.blocks[index]
        content = self.disk.read_physical(block)
        self.disk.storage.write(block, bytes([content[0] ^ 1]) + content[1:], "[falha simulada]")
        self.disk.flush()
        self.security.record("falha simulada", node.path(), f"bloco {block}")
        return block

    def configure_storage(self, kind, **options):
        self.security.require_kernel("configuração de armazenamento")
        for node in self.walk():
            self._load(node)
        self.disk.configure_storage(kind, **options)
        self.security.record("configuração de armazenamento", kind, "OK")

    def format(self, block_size, blocks, strategy):
        self.security.require_kernel("formatação")
        new = FileSystem(Disk(block_size, blocks), strategy, self.security)
        self.disk, self.strategy = new.disk, new.strategy
        self.root, self.cwd = new.root, new.cwd
        self.security.record("formatação", "/", "OK")

    def raid_action(self, action, number):
        self.security.require_kernel("simulação ou reconstrução de falha RAID")
        if not isinstance(self.disk.storage, RAIDArray):
            raise FSError("configure um arranjo RAID primeiro")
        if action == "fail":
            result = self.disk.storage.fail(number)
        elif action == "rebuild":
            result = self.disk.storage.rebuild(number)
        else:
            raise FSError("ação RAID inválida")
        self.security.record(action, f"Disco {number}", "OK")
        return result
