"""Etapa 5: relógio, prioridades/RR, E/S, mutex, RAM e métricas reais."""
import random

import pytest

from osim.allocation import make_strategy
from osim.disk import Disk, NoSpaceError
from osim.filesystem import FileSystem, FSError
from osim.memory import MemoryManager, MemoryError
from osim.security import SecurityError
from osim.shell import Shell


def setup_cpu(frames=2, blocks=32):
    fs = FileSystem(Disk(4096, blocks), make_strategy("indexed"))
    fs.memory = MemoryManager(fs, ram_size=(frames + 2) * 4096,
                              reserved_size=8192, kernel_size=4096)
    return fs, fs.memory.cpu


def names(cpu):
    return [identity[0] if identity else None for _, _, identity in cpu.timeline]


def assert_metrics(cpu):
    for process in cpu.processes.values():
        if process.completion is not None and not process.cancelled:
            assert process.completion - process.arrival == process.waiting + process.executed + process.blocked
            assert process.executed == process.burst
    assert cpu.busy == sum(identity is not None for _, _, identity in cpu.timeline)
    assert cpu.clock == len(cpu.timeline)


def test_equal_priority_round_robin_and_exact_metrics():
    _, cpu = setup_cpu()
    a, b = cpu.create("a", 3), cpu.create("b", 3)
    cpu.run()
    assert names(cpu) == ["a", "a", "b", "b", "a", "b"]
    assert (a.waiting, a.completion, b.waiting, b.completion) == (2, 5, 3, 6)
    assert cpu.metrics() == dict(completed=2, average_waiting=2.5, average_turnaround=5.5,
                                 context_switches=3, utilization=100, throughput=2 / 6)
    assert_metrics(cpu)


def test_syscall_uses_accumulated_cpu_and_priority_preempts_on_wakeup():
    _, cpu = setup_cpu()
    a = cpu.create("a", 2, profile="read_disk at 1ms (duration: 2ms)")
    b = cpu.create("b", 5, priority=2)
    cpu.configure(4)
    log = cpu.run()
    assert names(cpu) == ["a", "b", "b", "a", "b", "b", "b"]
    assert (a.blocked, a.waiting, a.completion) == (2, 0, 4)
    assert (b.waiting, b.completion) == (2, 7)
    assert "Preempção" in log and "desbloqueio em t=3ms" in log
    assert_metrics(cpu)


def test_syscall_at_quantum_boundary_and_idle_time():
    _, cpu = setup_cpu()
    process = cpu.create("web", 4, profile="read_disk at 2ms (duration: 3ms)")
    cpu.run(2)
    assert process.state == "BLOQUEADO" and process.io_until == 5
    cpu.run()
    assert names(cpu) == ["web", "web", None, None, None, "web", "web"]
    assert (process.blocked, process.waiting, process.completion) == (3, 0, 7)
    assert cpu.metrics()["utilization"] == 400 / 7
    assert cpu.context_switches == 0
    assert_metrics(cpu)


def test_step_execution_matches_uninterrupted_run_and_multiple_syscalls():
    results = []
    for partial in (False, True):
        _, cpu = setup_cpu()
        cpu.create("a", 7, profile="read_disk at 2ms (duration: 3ms); write_disk at 5ms (duration: 1ms)")
        cpu.create("b", 4)
        if partial:
            for _ in range(20):
                cpu.run(1)
        else:
            cpu.run()
        assert_metrics(cpu)
        results.append((cpu.timeline, cpu.metrics(), cpu.memory.stats(), cpu.render()))
    assert results[0] == results[1]


def test_random_io_and_page_references_are_seeded_and_use_shared_swap():
    results = []
    for _ in range(2):
        fs, cpu = setup_cpu(frames=1)
        fs.write("/dados", b"preservado")
        process = cpu.create("render", 12, profile="random", seed=42)
        log = cpu.run()
        assert process.blocked > 0 and "SYSCALL" in log
        assert cpu.memory.evictions > 0 and cpu.memory.faults + cpu.memory.hits == 12
        assert cpu.memory.clock == 12
        assert any(owner and owner.kind == "SWAP" for owner in fs.disk.owner)
        assert fs.read("/dados") == b"preservado"
        assert "PAGE FAULT" in log and "referência" in cpu.render_ram()
        assert_metrics(cpu)
        results.append((process.syscalls, log, cpu.render_ram()))
    assert results[0] == results[1]


def test_critical_process_uses_existing_reserved_allocation():
    _, cpu = setup_cpu()
    process = cpu.create("driver", 2, size=4096, kind="critical")
    log = cpu.run()
    assert "RAM reservada" in log
    assert process.memory.start == 4096 and not cpu.memory.frames
    assert_metrics(cpu)


def test_ram_summary_recognizes_manual_access_after_cpu_reference():
    fs, cpu = setup_cpu()
    cpu.create("app", 2, size=4096)
    cpu.run(1)
    assert "PAGE FAULT (NEW)" in cpu.render_ram()
    fs.memory.translate("app", 0)
    assert "manual" in cpu.render_ram() and "PAGE FAULT (NEW)" not in cpu.render_ram()


def test_mutex_contention_between_threads_and_automatic_release():
    _, cpu = setup_cpu()
    process = cpu.create("render", 3)
    cpu.create_thread("render", "T2", 2)
    for thread in ("T1", "T2"):
        cpu.thread_action("render", thread, 0, "lock", "video")
    cpu.configure(1)
    log = cpu.run()
    assert "EXECUTANDO -> BLOQUEADO (Mutex video)" in log
    assert "BLOQUEADO -> PRONTO; adquiriu Mutex video" in log
    assert [identity[1] for _, _, identity in cpu.timeline] == ["T1", "T1", "T1", "T2", "T2"]
    assert process.burst == process.executed == 5
    assert process.state == "CONCLUÍDO" and cpu.mutexes["video"].owner is None
    assert_metrics(cpu)


def test_process_waiting_for_mutex_and_io_release_fifo():
    _, cpu = setup_cpu()
    a = cpu.create("a", 3, profile="io at 1ms (duration: 3ms)")
    b = cpu.create("b", 2)
    c = cpu.create("c", 1)
    for name in ("a", "b", "c"):
        cpu.thread_action(name, "T1", 0, "lock", "shared")
    cpu.configure(1)
    log = cpu.run()
    assert b.blocked > 0 and c.blocked > 0
    grants = [line for line in log.splitlines() if "adquiriu Mutex" in line]
    assert "('b', 'T1')" in grants[0] and "('c', 'T1')" in grants[1]
    assert a.state == b.state == c.state == "CONCLUÍDO"
    assert_metrics(cpu)


def test_mutex_explicit_unlock_and_foreign_unlock_rejected():
    _, cpu = setup_cpu()
    cpu.create("a", 3)
    cpu.thread_action("a", "T1", 0, "lock", "m")
    cpu.thread_action("a", "T1", 1, "unlock", "m")
    cpu.run(2)
    assert cpu.mutexes["m"].owner is None
    cpu.create("b", 2)
    cpu.thread_action("b", "T1", 0, "unlock", "m")
    with pytest.raises(MemoryError, match="não é dona"):
        cpu.run()


def test_deadlock_pauses_and_termination_releases_owner_and_waiters():
    fs, cpu = setup_cpu()
    for name, first, second in (("a", "m1", "m2"), ("b", "m2", "m1")):
        cpu.create(name, 3)
        cpu.thread_action(name, "T1", 0, "lock", first)
        cpu.thread_action(name, "T1", 1, "lock", second)
    cpu.configure(1)
    assert "DEADLOCK" in cpu.run()
    assert cpu.clock == 2
    assert all(p.state == "BLOQUEADO" for p in cpu.processes.values())
    fs.memory.terminate("a")
    assert cpu.processes["b"].state == "PRONTO"
    cpu.run()
    assert cpu.processes["b"].state == "CONCLUÍDO"
    assert all(not m.waiters and m.owner is None for m in cpu.mutexes.values())
    assert cpu.metrics()["completed"] == 1
    assert_metrics(cpu)


def test_termination_cleans_cpu_swap_and_format_guard():
    fs, cpu = setup_cpu(frames=1)
    cpu.create("a", 8)
    cpu.run(4)
    with pytest.raises(FSError):
        fs.format(4096, 32, make_strategy("indexed"))
    fs.memory.terminate("a")
    assert cpu.current is None and all(not q for q in cpu.ready.values())
    assert not fs.memory.frames and not fs.memory.processes
    assert not any(owner and owner.kind == "SWAP" for owner in fs.disk.owner)
    before = cpu.clock
    cpu.run()
    assert cpu.clock == before
    fs.format(4096, 32, make_strategy("indexed"))


def test_creation_at_later_clock_and_target_keeps_global_scheduling():
    _, cpu = setup_cpu()
    cpu.create("a", 4)
    cpu.run(1)
    b = cpu.create("b", 1, priority=2)
    cpu.run(process="b")
    assert names(cpu) == ["a", "a", "a", "a", "b"]
    assert (b.arrival, b.waiting, b.completion) == (1, 3, 5)
    assert_metrics(cpu)


def test_completed_metrics_survive_memory_release_and_reused_name():
    fs, cpu = setup_cpu()
    original = cpu.create("app", 1)
    cpu.run()
    stats = cpu.metrics()
    fs.memory.terminate("app")
    assert cpu.metrics() == stats
    new = cpu.create("app", 2)
    cpu.run()
    assert new.memory.pid != original.memory.pid
    assert cpu.metrics()["completed"] == 2 and cpu.context_switches == 1
    assert len(cpu.history) == 2


@pytest.mark.parametrize("device", ["hdd", "ssd", "raid"])
def test_cpu_references_preserve_shared_files_across_storage_devices(device):
    fs, cpu = setup_cpu(frames=1)
    fs.configure_storage(device, **(dict(raid_level=5, disks=3) if device == "raid" else {}))
    fs.write("/shared", b"arquivos e processos")
    cpu.create("app", 10, profile="random", seed=9)
    cpu.run()
    assert cpu.memory.evictions > 0
    assert fs.read("/shared") == b"arquivos e processos"
    fs.memory.terminate("app")
    assert not any(owner and owner.kind == "SWAP" for owner in fs.disk.owner)
    assert fs.read("/shared") == b"arquivos e processos"


@pytest.mark.parametrize("seed", range(5))
def test_mixed_workloads_keep_accounting_consistent(seed):
    _, cpu = setup_cpu(frames=2, blocks=128)
    rng = random.Random(seed)
    for index in range(5):
        name = f"app{index}"
        cpu.create(name, rng.randint(2, 8), rng.randint(1, 3), profile="random", seed=seed + index)
        cpu.create_thread(name, "T2", rng.randint(1, 4))
        for thread in ("T1", "T2"):
            cpu.thread_action(name, thread, 0, "lock", "shared")
    cpu.configure(rng.randint(1, 4))
    assert "DEADLOCK" not in cpu.run()
    assert cpu.metrics()["completed"] == 5
    assert all(p.state == "CONCLUÍDO" for p in cpu.processes.values())
    assert_metrics(cpu)


@pytest.mark.parametrize("options", [dict(burst=0), dict(burst="1.5ms"), dict(burst=-1),
    dict(priority=0), dict(priority="bad"), dict(seed="bad"), dict(profile="bad"),
    dict(profile="io at 4ms (duration: 2ms)"), dict(profile="io at 1ms (duration: 0ms)"),
    dict(profile="io at 1ms (duration: 2ms); io at 1ms (duration: 3ms)")])
def test_invalid_creation_is_atomic(options):
    fs, cpu = setup_cpu()
    with pytest.raises(MemoryError):
        cpu.create("bad", **dict(dict(burst=4), **options))
    assert not cpu.processes and not fs.memory.processes and cpu.clock == 0


def test_validation_empty_metrics_and_no_work():
    _, cpu = setup_cpu()
    assert cpu.metrics()["utilization"] == 0
    assert "Nenhum" in cpu.run()
    for quantum in (0, -1, "x"):
        with pytest.raises(MemoryError):
            cpu.configure(quantum)
    with pytest.raises(MemoryError):
        cpu.configure(2, "FCFS")
    with pytest.raises(MemoryError):
        cpu.create("small", 1, profile="random")
    cpu.create("a", 2)
    for steps in (0, -1, "x"):
        with pytest.raises(MemoryError):
            cpu.run(steps)
    with pytest.raises(MemoryError):
        cpu.thread_action("a", "T1", 2, "lock", "m")
    with pytest.raises(MemoryError):
        cpu.thread_action("a", "absent", 0, "lock", "m")
    with pytest.raises(MemoryError):
        cpu.create_thread("a", "T1", 1)
    cpu.run(1)
    with pytest.raises(MemoryError):
        cpu.create_thread("a", "T2", 1)


def test_cpu_failures_do_not_consume_time_or_random_reference():
    _, cpu = setup_cpu(frames=1, blocks=1)
    process = cpu.create("a", 10, seed=1)
    cpu.run(1)
    clock, rng = cpu.clock, process.rng.getstate()
    with pytest.raises(NoSpaceError):
        cpu.run(1)
    assert cpu.clock == clock and process.executed == 1 and process.rng.getstate() == rng


def test_security_preserves_ownership_and_kernel_only_global_execution():
    fs, cpu = setup_cpu()
    cpu.create("root_app", 2)
    fs.security.add_user("ana", "users", "test-ana")
    fs.login("ana", "test-ana")
    cpu.create("app", 2)
    cpu.create_thread("app", "T2", 1)
    for action in (lambda: cpu.create_thread("root_app", "T2", 1), lambda: cpu.run(),
                   lambda: cpu.step(), lambda: cpu.configure(), lambda: cpu.render(), lambda: cpu.render_ram(), lambda: cpu.metrics(),
                   lambda: cpu.create("driver", 2, kind="critical", size=1)):
        with pytest.raises(SecurityError):
            action()
    assert cpu.clock == 0


def test_shell_new_commands_and_legacy_memory_compatibility(capsys):
    fs, cpu = setup_cpu()
    shell = Shell(fs)
    for line in ('create_process --name legacy --size 1KB --type normal',
                 'create_process --name web --burst 4ms --priority 1 --syscall "read_disk at 2ms (duration: 2ms)"',
                 'create_thread --process web --name T2 --burst 2ms',
                 'thread_action --process web --thread T1 --at 0ms --operation lock --mutex m',
                 'thread_action --process web --thread T2 --at 0ms --operation lock --mutex m',
                 'run_simulation --process web --steps 1', 'process_status', 'ram_status',
                 'schedule --algorithm Priority_RR --quantum 1ms', 'system_metrics'):
        shell.execute(line)
    output = capsys.readouterr().out
    assert "Erro:" not in output and "CONCLUÍDO" in output and "Tempo Médio" in output
    assert fs.memory.process("legacy").size == 1024 and "legacy" not in cpu.processes
    for line in ('run_simulation --steps abc', 'schedule --algorithm FCFS --quantum 2',
                 'create_process --name bad --burst 3 --priority x', 'create_process --name x --size 4KB --syscall random',
                 'ram_status extra', 'thread_action --process web --thread T1 --at -1 --operation lock --mutex m'):
        shell.execute(line)
        assert "Erro:" in capsys.readouterr().out
