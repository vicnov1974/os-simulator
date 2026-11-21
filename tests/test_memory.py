"""Etapa 4: isolamento das áreas, políticas, bytes de swap e integração real."""
import random
from pathlib import Path
import subprocess
import sys

import pytest

from osim.allocation import make_strategy
from osim.disk import Disk, DiskError, NoSpaceError
from osim.filesystem import FileSystem, FSError
from osim.memory import MemoryManager, MemoryError, render_memory, render_process
from osim.report import render_status
from osim.security import SecurityError
from osim.shell import Shell
from osim.storage import make_storage


def setup_memory(frames=2, block_size=4096, blocks=32, device="hdd", alloc="indexed", **options):
    fs = FileSystem(Disk(block_size, blocks, make_storage(device, block_size, blocks)), make_strategy(alloc))
    fs.memory = MemoryManager(fs, ram_size=4096 * (frames + 2), reserved_size=8192,
                              kernel_size=4096, **options)
    return fs, fs.memory


def assert_consistent(memory):
    disk = memory.fs.disk
    blocks = []
    resident = []
    for process in memory.processes.values():
        if process.kind == "critical":
            assert memory.kernel_size <= process.start
            assert process.start + process.size <= memory.reserved_size
        for index, page in enumerate(process.pages):
            assert page.frame is None or not page.swap
            if page.frame is not None:
                assert 0 <= page.frame < memory.frame_count
                assert memory.frames[page.frame][:2] == (process, index)
                resident.append(page.frame)
            for block in page.swap:
                assert disk.owner[block].kind == "SWAP"
                assert disk.owner[block].node is process
                assert disk.owner[block].index == index
                blocks.append(block)
    assert len(blocks) == len(set(blocks))
    assert len(resident) == len(memory.frames) == len(set(resident))
    assert set(blocks) == {b for b, owner in enumerate(disk.owner) if owner and owner.kind == "SWAP"}


def test_reserved_example_fragmentation_failure_and_compaction():
    fs = FileSystem(Disk(4096, 16), make_strategy("indexed"))
    memory = fs.memory
    kb = 1024
    disk = memory.create("Kernel_DiskDriver", 512 * kb, "critical")
    memory.create("Kernel_NetDriver", 1024 * kb, "critical")
    crypto = memory.create("Kernel_CryptoEngine", 512 * kb, "critical")
    memory.terminate("Kernel_NetDriver")
    stats = memory.stats()
    assert stats["reserved_free"] == 2048 * kb
    assert stats["largest_hole"] == stats["external"] == 1024 * kb
    with pytest.raises(MemoryError, match="fragmentação externa"):
        memory.create("grande", 1536 * kb, "critical")
    old_pid, old_start = crypto.pid, crypto.start
    assert memory.compact() == [(crypto.name, old_start, 1536 * kb, 512 * kb)]
    assert disk.start == 1024 * kb and crypto.pid == old_pid
    assert memory.stats()["largest_hole"] == 2048 * kb
    assert memory.stats()["external"] == 0
    memory.create("grande", 1536 * kb, "critical")
    with pytest.raises(MemoryError, match="reservada cheia"):
        memory.create("enorme", 4 * 1024**2, "critical")
    assert_consistent(memory)


def test_demand_paging_translation_and_internal_fragmentation():
    _, memory = setup_memory()
    process = memory.create("App_Navegador", 10240)
    assert [p.used for p in process.pages] == [4096, 4096, 2048]
    assert not memory.frames
    assert memory.stats()["internal"] == 2048
    assert "50% de uso" in render_process(process, 4096)
    first = memory.translate(process.name, 1, 15)
    assert first.fault and first.frame == 2 and first.physical == 8207
    second = memory.translate(process.name, 1)
    assert not second.fault and first.frame == second.frame
    assert memory.faults == memory.hits == 1
    memory.translate(process.name, 2)
    assert memory.stats()["resident_internal"] == 2048
    assert_consistent(memory)


def test_lru_is_global_and_hit_updates_recency():
    _, memory = setup_memory()
    a = memory.create("a", 8192)
    b = memory.create("b", 4096)
    memory.translate("a", 0)
    memory.translate("b", 0)
    memory.translate("a", 0)
    result = memory.translate("a", 1)
    assert result.fault and b.pages[0].frame is None
    assert a.pages[0].frame is not None and b.pages[0].swap
    assert memory.evictions == 1
    assert "LRU" in "\n".join(result.events)
    assert_consistent(memory)


def test_second_chance_clears_bits_and_honors_reference():
    _, memory = setup_memory(frames=3, algorithm="second-chance")
    p = memory.create("a", 5 * 4096)
    for index in range(4):
        memory.translate("a", index)
    assert p.pages[0].frame is None
    assert not p.pages[1].referenced and not p.pages[2].referenced
    assert p.pages[3].referenced
    memory.translate("a", 1)  # recebe segunda chance
    result = memory.translate("a", 4)
    assert p.pages[1].frame is not None and p.pages[2].frame is None
    assert "SECOND-CHANCE" in "\n".join(result.events)
    assert_consistent(memory)


@pytest.mark.parametrize("algorithm", ["lru", "second-chance"])
@pytest.mark.parametrize("device", ["hdd", "ssd", "raid"])
@pytest.mark.parametrize("block_size", [1024, 4096, 8192])
def test_swap_roundtrip_preserves_bytes_and_generates_physical_io(algorithm, device, block_size):
    fs, memory = setup_memory(frames=1, block_size=block_size, device=device, algorithm=algorithm)
    memory.create("app", 8192)
    memory.execute("app", "STORE_DATA Página 0 conteúdo-ç")
    memory.execute("app", "STORE_DATA Página 1 segunda")
    for _ in range(2):
        result, data = memory.execute("app", "LOAD_DATA Página 0")
        assert result.fault and data.startswith("conteúdo-ç".encode())
        assert memory.execute("app", "LOAD_DATA Página 1")[1].startswith(b"segunda")
    members = getattr(fs.disk.storage, "members", [fs.disk.storage])
    assert any(r.operation == "READ" and "swap" in r.label for d in members for r in d.history)
    assert any(r.operation == "WRITE" and "swap" in r.label for d in members for r in d.history)
    assert_consistent(memory)
    memory.terminate("app")
    assert not memory.frames and fs.disk.used_count == 1


@pytest.mark.parametrize("alloc", ["contiguous", "linked", "indexed"])
def test_files_compaction_migration_and_swap_coexist(alloc):
    fs, memory = setup_memory(frames=1, alloc=alloc)
    fs.create("/arquivo", content=b"arquivo original")
    memory.create("app", 8192)
    memory.execute("app", "STORE_DATA Página 0 memoria original")
    memory.translate("app", 1)
    assert "[S]" in render_status(fs, details=True)
    for strategy in ("linked", "indexed", "contiguous"):
        fs.change_strategy(strategy)
        fs.compact()
        assert fs.read("/arquivo") == b"arquivo original"
        assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"memoria original")
        memory.translate("app", 1)
        assert_consistent(memory)
    for device in ("ssd", "raid", "hdd"):
        fs.configure_storage(device)
        assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"memoria original")
        memory.translate("app", 1)
    assert fs.antivirus_scan()[0][1].startswith("[OK")
    with pytest.raises(FSError, match="encerre os processos"):
        fs.format(1024, 16, make_strategy("linked"))
    memory.terminate("app")
    fs.format(1024, 16, make_strategy("linked"))
    assert fs.memory is memory and fs.memory.fs is fs


def test_swap_reuses_incoming_blocks_on_full_disk():
    fs, memory = setup_memory(frames=1, blocks=2)
    memory.create("app", 8192)
    memory.execute("app", "STORE_DATA Página 0 primeira")
    memory.execute("app", "STORE_DATA Página 1 segunda")
    assert fs.disk.free_count == 0
    for _ in range(3):
        assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"primeira")
        assert memory.execute("app", "LOAD_DATA Página 1")[1].startswith(b"segunda")
        assert_consistent(memory)


def test_full_swap_preserves_state_and_can_retry_after_freeing_file():
    fs, memory = setup_memory(frames=1, blocks=3, alloc="linked")
    fs.create("/ocupado", size=8192)
    process = memory.create("app", 8192)
    memory.execute("app", "STORE_DATA Página 0 importante")
    with pytest.raises(NoSpaceError, match="swap sem espaço"):
        memory.translate("app", 1)
    assert memory.faults == 1 and memory.evictions == 0
    assert process.pages[0].frame == 0 and process.pages[1].frame is None
    assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"importante")
    fs.remove("/ocupado")
    memory.translate("app", 1)
    assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"importante")
    assert_consistent(memory)


@pytest.mark.parametrize("algorithm", ["lru", "second-chance"])
def test_failed_io_preserves_ram_tables_clock_and_policy(monkeypatch, algorithm):
    fs, memory = setup_memory(frames=1, algorithm=algorithm)
    process = memory.create("app", 8192)
    memory.execute("app", "STORE_DATA Página 0 importante")
    before = memory.stats(), memory.clock, memory.hand, process.pages[0].referenced
    write = fs.disk.write

    def fail(*args):
        raise DiskError("falha de E/S")

    monkeypatch.setattr(fs.disk, "write", fail)
    with pytest.raises(DiskError, match="falha de E/S"):
        memory.translate("app", 1)
    assert before == (memory.stats(), memory.clock, memory.hand, process.pages[0].referenced)
    assert_consistent(memory)
    monkeypatch.setattr(fs.disk, "write", write)
    memory.translate("app", 1)
    assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"importante")


def test_swap_read_and_terminate_failures_are_atomic(monkeypatch):
    fs, memory = setup_memory(frames=1)
    memory.create("app", 8192)
    memory.translate("app", 0)
    memory.translate("app", 1)
    before = memory.stats()

    def fail(*args):
        raise DiskError("indisponível")

    with monkeypatch.context() as patch:
        patch.setattr(fs.disk, "read_physical", fail)
        with pytest.raises(DiskError):
            memory.translate("app", 0)
    with monkeypatch.context() as patch:
        patch.setattr(fs.disk, "release", fail)
        with pytest.raises(DiskError):
            memory.terminate("app")
    assert before == memory.stats()
    assert_consistent(memory)


def test_swap_works_with_degraded_raid_and_rejects_excessive_failures():
    fs, memory = setup_memory(frames=1, device="raid")
    memory.create("app", 8192)
    memory.execute("app", "STORE_DATA Página 0 importante")
    memory.translate("app", 1)
    fs.raid_action("fail", 1)
    assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"importante")
    memory.translate("app", 1)
    fs.raid_action("rebuild", 1)
    assert memory.execute("app", "LOAD_DATA Página 0")[1].startswith(b"importante")
    memory.translate("app", 1)
    fs.raid_action("fail", 1)
    fs.raid_action("fail", 2)
    before = memory.stats()
    with pytest.raises(DiskError, match="tolerância"):
        memory.translate("app", 0)
    assert memory.stats() == before
    assert_consistent(memory)


def test_user_isolation_critical_privilege_and_logout():
    fs, memory = setup_memory()
    fs.security.add_user("ana", "users", "test-ana")
    fs.security.add_user("bia", "users", "test-bia")
    memory.create("kernel", 1024, "critical")
    fs.login("ana", "test-ana")
    memory.create("app-ana", 4096)
    memory.translate("app-ana", 0)
    for operation in (lambda: memory.create("driver", 1024, "critical"),
                      memory.compact, lambda: memory.set_algorithm("second-chance"),
                      lambda: render_memory(memory), lambda: memory.terminate("kernel")):
        with pytest.raises(SecurityError):
            operation()
    fs.login("bia", "test-bia")
    for operation in (lambda: memory.translate("app-ana", 0), lambda: memory.terminate("app-ana")):
        with pytest.raises(SecurityError):
            operation()
    fs.logout()
    with pytest.raises(SecurityError):
        memory.create("app", 1)


@pytest.mark.parametrize("options", [dict(ram_size=0), dict(reserved_size=0), dict(kernel_size=0),
                                         dict(page_size=0), dict(reserved_size=8193),
                                         dict(ram_size=20000), dict(algorithm="fifo"), dict(report_every=0)])
def test_invalid_configuration(options):
    fs, _ = setup_memory()
    with pytest.raises(MemoryError):
        MemoryManager(fs, **options)


@pytest.mark.parametrize("instruction", ["oops", "LOAD_DATA Página -1", "STORE_DATA Página 0",
                                         "LOAD_DATA Página 0 extra", "STORE_DATA Página 8 abc"])
def test_invalid_instructions_do_not_modify_state(instruction):
    _, memory = setup_memory()
    memory.create("app", 1)
    with pytest.raises(MemoryError):
        memory.execute("app", instruction)
    assert memory.clock == memory.faults == 0


def test_invalid_references_names_sizes_and_process_types():
    _, memory = setup_memory()
    memory.create("app", 4097)
    for name, page, offset in [("ausente", 0, 0), ("app", -1, 0), ("app", 2, 0),
                               ("app", 0, -1), ("app", 1, 1)]:
        with pytest.raises(MemoryError):
            memory.translate(name, page, offset)
    for name, size, kind in [("app", 1, "normal"), ("", 1, "normal"), ("x", 0, "normal"),
                              ("x", 1, "outro"), ("Núcleo Base do SO", 1, "critical")]:
        with pytest.raises(MemoryError):
            memory.create(name, size, kind)
    memory.create("driver", 1, "critical")
    with pytest.raises(MemoryError, match="sem paginação"):
        memory.translate("driver", 0)
    assert not memory.frames


@pytest.mark.parametrize("algorithm", ["lru", "second-chance"])
def test_repeated_replacements_match_written_page_content(algorithm):
    _, memory = setup_memory(frames=3, blocks=24, algorithm=algorithm)
    for name in ("a", "b"):
        memory.create(name, 5 * 4096)
    expected = {}
    rng = random.Random(42)
    for step in range(150):
        name, page = rng.choice(("a", "b")), rng.randrange(5)
        if rng.randrange(3) == 0:
            value = f"{step:04d}"
            memory.execute(name, f"STORE_DATA Página {page} {value}")
            expected[name, page] = value.encode()
        else:
            data = memory.execute(name, f"LOAD_DATA Página {page}")[1]
            assert data[:4] == expected.get((name, page), bytes(4))
        assert_consistent(memory)


def test_shell_commands_periodic_reports_and_validation(capsys):
    fs, memory = setup_memory(frames=1, report_every=2)
    shell = Shell(fs)
    for line in ('create_process --name App --size 10KB --type normal',
                 'execute_step --process App --instruction "STORE_DATA Página 0 olá"',
                 'translate_address --process App --page 1',
                 'translate_address --process App --page 1',
                 'memory_status --virtual --details', 'memory_status --reserved',
                 'memory_compact --reserved', 'memory_algorithm second-chance', 'memory_reports'):
        assert shell.execute(line)
    output = capsys.readouterr().out
    assert "Erro:" not in output
    assert "PAGE FAULT" in output and "[HIT]" in output and "LRU" in output
    assert "2048 Bytes" in output and "RELATÓRIO PERIÓDICO" in output
    for line in ('create_process --name App --size 1KB --type normal',
                 'translate_address --process App --page abc', 'memory_compact',
                 'execute_step --process App --instruction invalid',
                 'memory_status --reserved --virtual', 'memory_status extra',
                 'create_process --name foo', 'memory_algorithm fifo'):
        shell.execute(line)
        assert "Erro:" in capsys.readouterr().out
    shell.execute("terminate_process --name App")
    assert not memory.frames and not memory.processes


def test_periodic_history_is_bounded_and_reports_both_regions():
    _, memory = setup_memory(report_every=1)
    memory.create("app", 4096)
    for _ in range(25):
        memory.translate("app", 0)
    assert len(memory.periodic_reports) == 20
    assert memory.periodic_reports[-1][0] == 26
    report = render_memory(memory, details=True)
    assert "Consumo reservado" in report and "RAM dinâmica ocupada" in report
    assert "Memória virtual requisitada" in report and "Página Lógica 0" in report


@pytest.mark.parametrize("entrypoint", ["main.py", "gui.py"])
@pytest.mark.parametrize("options", [["--page-size", "0"], ["--ram-size", "4MB"],
                                     ["--memory-report-every", "0"], ["--reserved-size", "5KB"]])
def test_entrypoints_reject_invalid_memory_configuration(entrypoint, options):
    result = subprocess.run([sys.executable, entrypoint, *options],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "error:" in result.stderr and "Traceback" not in result.stderr
