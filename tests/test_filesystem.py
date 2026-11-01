import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osim import report
from osim.allocation import ContiguousAllocation, IndexedAllocation, LinkedAllocation
from osim.disk import Disk, NoSpaceError
from osim.filesystem import FileSystem, FSError
from osim.utils import format_size, parse_size


def make(strategy, block=4096, blocks=16):
    return FileSystem(Disk(block, blocks), strategy)


# ---------------------------------------------------------------- utils
def test_parse_and_format_size():
    assert parse_size("10KB") == 10240
    assert parse_size("512") == 512
    assert parse_size("1.5MB") == 1572864
    assert format_size(10240) == "10 KB"
    assert format_size(100) == "100 B"
    with pytest.raises(ValueError):
        parse_size("abc")


# ----------------------------------------------------- árvore / caminhos
def test_tree_and_absolute_paths():
    fs = make(IndexedAllocation())
    fs.mkdir("/sistema")
    fs.mkdir("/sistema/logs")
    fs.create("/sistema/logs/app.log", size=parse_size("10KB"))
    assert fs.tree() == ("/\n└── sistema/ [DIR]\n    └── logs/ [DIR]\n"
                         "        └── app.log (10 KB) [ARQ]")


def test_relative_paths_and_cd():
    fs = make(LinkedAllocation(), 1024)
    fs.mkdir("/home/user", parents=True)
    fs.chdir("/home/user")
    fs.create("a.txt", content=b"oi")
    fs.mkdir("./sub")
    fs.create("sub/../b.txt", content=b"b")
    assert fs.resolve("/home/user/a.txt").path() == "/home/user/a.txt"
    assert fs.resolve("../user/b.txt").path() == "/home/user/b.txt"
    fs.chdir("..")
    assert fs.cwd.path() == "/home"
    fs.chdir("../../..")             # '..' na raiz permanece na raiz
    assert fs.cwd.path() == "/"


def test_errors():
    fs = make(IndexedAllocation())
    fs.mkdir("/a")
    with pytest.raises(FSError):
        fs.mkdir("/a")               # já existe
    with pytest.raises(FSError):
        fs.mkdir("/x/y")             # pai inexistente
    with pytest.raises(FSError):
        fs.read("/a")                # é diretório
    with pytest.raises(FSError):
        fs.remove("/")


# ---------------------------------------------------------------- CRUD
@pytest.mark.parametrize("strategy", [ContiguousAllocation, LinkedAllocation, IndexedAllocation])
def test_crud_roundtrip(strategy):
    fs = make(strategy(), 1024, 32)
    fs.create("/f.txt", content=b"hello")
    assert fs.read("/f.txt") == b"hello"
    big = bytes(range(256)) * 20                      # 5120 bytes = 5 blocos
    fs.write("/f.txt", big)
    assert fs.read("/f.txt") == big
    assert len(fs.resolve("/f.txt").blocks) == 5
    fs.write("/f.txt", b"!", append=True)
    assert fs.read("/f.txt") == big + b"!"
    fs.resize("/f.txt", 100)
    assert fs.read("/f.txt") == (big + b"!")[:100]
    assert len(fs.resolve("/f.txt").blocks) == 1
    used = fs.disk.used_count
    fs.remove("/f.txt")
    assert fs.disk.used_count < used
    assert fs.disk.used_count == 1                    # só a raiz


def test_rmdir_rm_r_mv_cp():
    fs = make(IndexedAllocation(), 1024, 32)
    fs.mkdir("/a/b", parents=True)
    fs.create("/a/b/x.txt", content=b"x")
    with pytest.raises(FSError):
        fs.remove("/a", only_dir=True)               # não vazio
    with pytest.raises(FSError):
        fs.remove("/a")                              # dir sem -r
    fs.copy("/a/b/x.txt", "/a")
    assert fs.read("/a/x.txt") == b"x"
    fs.move("/a/b", "/c")
    assert fs.read("/c/x.txt") == b"x"
    with pytest.raises(FSError):
        fs.move("/a", "/a/novo")                     # dentro de si mesmo
    fs.chdir("/c")
    fs.remove("/c", recursive=True)
    assert fs.cwd.path() == "/"
    assert "c" not in fs.root.children
    assert fs.disk.used_count == 4                   # raiz + /a + (i-node + dado de /a/x.txt)


def test_blocks_freed_after_recursive_remove():
    fs = make(IndexedAllocation(), 1024, 32)
    fs.mkdir("/d")
    fs.create("/d/f", size=3000)
    fs.remove("/d", recursive=True)
    assert fs.disk.used_count == 1


# --------------------------------------------------- alocação por estratégia
def test_indexed_layout_matches_statement_example():
    fs = make(IndexedAllocation())
    fs.mkdir("/sistema")
    fs.mkdir("/sistema/logs")
    n = fs.create("/sistema/logs/app.log", size=10240)
    assert n.meta_block == 3 and n.blocks == [4, 5, 6]
    a = report.analyze(fs)
    assert a["internal_total"] == 2048
    assert a["external"] == 0 and not a["needs_compaction"]


def test_indexed_inode_limit():
    fs = make(IndexedAllocation(), 512, 512)          # 128 ponteiros por i-node
    with pytest.raises(FSError):
        fs.create("/grande", size=129 * 512)
    assert fs.disk.used_count == 1                    # rollback do i-node


def test_linked_uses_scattered_blocks():
    fs = make(LinkedAllocation(), 1024, 16)
    fs.create("/a", size=2048)
    fs.create("/b", size=1024)
    fs.create("/c", size=2048)
    fs.remove("/a")
    fs.create("/d", size=3072)                       # usa buraco + fim
    assert fs.resolve("/d").blocks == [1, 2, 6]
    assert not report.is_contiguous(fs.resolve("/d").blocks)


def test_contiguous_external_fragmentation_and_compaction():
    fs = make(ContiguousAllocation(), 1024, 16)
    fs.create("/a", size=2048)    # 1-2
    fs.create("/b", size=2048)    # 3-4
    fs.create("/c", size=2048)    # 5-6
    fs.remove("/b")
    a = report.analyze(fs)
    assert len(a["free_runs"]) == 2 and a["needs_compaction"]
    assert a["external"] == 2 * 1024                  # menor região (2 blocos) fica "presa"
    with pytest.raises(NoSpaceError) as exc:
        fs.create("/big", size=11 * 1024)             # 11 livres, mas maior região = 9
    assert "compact" in str(exc.value)
    fs.compact()
    a = report.analyze(fs)
    assert a["external"] == 0 and not a["needs_compaction"]
    fs.create("/big", size=11 * 1024)                 # agora cabe
    assert fs.read("/a") == b"\0" * 2048


def test_compact_preserves_contents():
    fs = make(ContiguousAllocation(), 1024, 16)
    fs.create("/a", content=b"AAAA" * 300)
    fs.create("/b", content=b"BBBB" * 300)
    fs.remove("/a")
    fs.compact()
    assert fs.read("/b") == b"BBBB" * 300
    assert fs.resolve("/b").blocks[0] == 1


def test_contiguous_grow_relocates_and_rolls_back():
    fs = make(ContiguousAllocation(), 1024, 8)
    fs.create("/a", content=b"a" * 1024)              # bloco 1
    fs.create("/b", content=b"b" * 1024)              # bloco 2
    fs.write("/a", b"a" * 3000)                       # 3 blocos: realoca para 3..5
    assert fs.resolve("/a").blocks == [3, 4, 5]
    with pytest.raises(NoSpaceError):
        fs.write("/a", b"x" * 20000)
    assert fs.read("/a") == b"a" * 3000               # estado preservado


def test_set_alloc_migration_preserves_data():
    fs = make(LinkedAllocation(), 1024, 32)
    fs.mkdir("/d")
    fs.create("/d/f", content=b"dados" * 500)
    fs.change_strategy("indexed")
    assert isinstance(fs.strategy, IndexedAllocation)
    assert fs.resolve("/d/f").meta_block is not None
    assert fs.read("/d/f") == b"dados" * 500
    fs.change_strategy("contiguous")
    assert fs.resolve("/d/f").meta_block is None
    assert fs.read("/d/f") == b"dados" * 500


def test_report_renders():
    fs = make(IndexedAllocation())
    fs.mkdir("/sistema")
    fs.create("/sistema/app.log", size=10240)
    text = report.render_status(fs, details=True)
    assert "[INODE_RAIZ]" in text and "[LIVRE]" in text
    assert "Fragmentação Interna" in text and "Necessidade de Compactação" in text
