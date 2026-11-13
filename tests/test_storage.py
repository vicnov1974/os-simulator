"""Integração física, escalonamento, FTL e recuperação dos bytes em RAID."""
import os
import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osim.allocation import make_strategy
from osim.disk import Disk, DiskError, NoSpaceError
from osim.filesystem import FileSystem
from osim.scheduling import schedule
from osim.shell import Shell
from osim.storage import HDD, SSD, RAIDArray, make_storage, xor_blocks


def test_sstf_statement_example():
    result = schedule([98, 183, 37, 122, 14])
    assert result.path == [53, 37, 14, 98, 122, 183]
    assert result.movements == [16, 23, 84, 24, 61]
    assert result.total == 208


@pytest.mark.parametrize("algorithm,direction,path,total", [
    ("fcfs", "up", [53, 98, 183, 37, 122, 14], 469),
    ("scan", "up", [53, 98, 122, 183, 199, 37, 14], 331),
    ("scan", "down", [53, 37, 14, 0, 98, 122, 183], 236),
    ("c-scan", "up", [53, 98, 122, 183, 199, 0, 14, 37], 382),
    ("c-scan", "down", [53, 37, 14, 0, 199, 183, 122, 98], 353),
])
def test_other_schedulers(algorithm, direction, path, total):
    result = schedule([98, 183, 37, 122, 14], algorithm=algorithm, direction=direction)
    assert result.path == path
    assert result.total == total


@pytest.mark.parametrize("algorithm", ["fcfs", "sstf", "scan", "c-scan"])
def test_scheduler_empty_duplicate_and_boundaries(algorithm):
    assert schedule([], algorithm=algorithm).total == 0
    result = schedule([53, 53, 0, 199, 0], algorithm=algorithm)
    assert sorted(result.order) == list(range(5))
    assert schedule([53, 53], algorithm=algorithm).total == 0
    assert schedule([199], head=0, algorithm=algorithm).total == 199


@pytest.mark.parametrize("options", [dict(head=-1), dict(track_count=0),
                                     dict(algorithm="look"), dict(direction="left")])
def test_scheduler_rejects_invalid_configuration(options):
    with pytest.raises(DiskError):
        schedule([10], **options)
    with pytest.raises(DiskError):
        schedule([200])


def test_real_queue_dispatch_and_write_dependencies():
    device = HDD(4, 200)
    for track in [98, 183, 37, 122, 14]:
        device.write(track, bytes([track]))
    assert not device.data and len(device.pending) == 5
    device.flush()
    assert [r.block for r in device.history] == [37, 14, 98, 122, 183]
    assert device.total_movement == 208
    device.write(37, b"old")
    device.write(37, b"new")
    assert device.read(37) == b"new\0"
    assert not device.pending


@pytest.mark.parametrize("kind", ["hdd", "ssd", "raid"])
@pytest.mark.parametrize("strategy", ["contiguous", "linked", "indexed"])
def test_filesystem_roundtrip_generates_physical_io(kind, strategy):
    storage = make_storage(kind, 64, 32)
    fs = FileSystem(Disk(64, 32, storage), make_strategy(strategy))
    fs.mkdir("/dados")
    payload = bytes(range(256))
    fs.write("/dados/a", payload)
    assert fs.read("/dados/a") == payload
    fs.write("/dados/a", b"!", append=True)
    fs.copy("/dados/a", "/dados/b")
    fs.remove("/dados/a")
    fs.compact()
    fs.change_strategy("indexed")
    assert fs.read("/dados/b") == payload + b"!"
    members = storage.members if kind == "raid" else [storage]
    assert any(r.operation == "READ" and "/dados/" in r.label for d in members for r in d.history)
    assert any(r.operation == "WRITE" and "/dados/" in r.label for d in members for r in d.history)


@pytest.mark.parametrize("level,disks", [(0, 3), (1, 3), (4, 4), (5, 4)])
def test_raid_layout_and_parity(level, disks):
    raid = RAIDArray(8, 12, level, disks)
    for block in range(12):
        raid.write(block, bytes([block + 1]) * 8)
    raid.flush()
    for block in range(12):
        assert raid.read(block) == bytes([block + 1]) * 8
    if level in (4, 5):
        for row in range(raid.members[0].blocks):
            assert xor_blocks([d.data[row] for d in raid.members], 8) == bytes(8)
        parities = {raid.location(b)[2] for b in range(12)}
        assert len(parities) == (1 if level == 4 else disks)


def test_raid_rejects_out_of_range_and_oversized_block():
    raid = RAIDArray(8, 5)
    for block in (-1, 5):
        with pytest.raises(DiskError):
            raid.read(block)
        with pytest.raises(DiskError):
            raid.write(block, b"a")
    with pytest.raises(DiskError):
        raid.write(0, b"x" * 9)
    assert all(not d.pending and not d.data for d in raid.members)


@pytest.mark.parametrize("level,disks", [(1, 3), (4, 3), (5, 3), (5, 4)])
def test_every_member_failure_degraded_writes_and_rebuild(level, disks):
    for failed in range(1, disks + 1):
        raid = RAIDArray(16, 13, level, disks)
        original = {b: bytes([b + 1]) * 16 for b in range(13)}
        for b, data in original.items():
            raid.write(b, data)
        raid.fail(failed)
        # Apaga os bytes do disco falho: a recuperação deve usar os sobreviventes.
        raid.members[failed - 1].data.clear()
        for b, data in original.items():
            assert raid.read(b) == data
        for b in range(13):
            original[b] = bytes([30 + b]) * 16
            raid.write(b, original[b])
        assert raid.rebuild(failed) == raid.members[0].blocks
        for b, data in original.items():
            assert raid.read(b) == data
        if level in (4, 5):
            assert raid.recoveries
            for row in range(raid.members[0].blocks):
                assert xor_blocks([d.data.get(row, bytes(16)) for d in raid.members], 16) == bytes(16)


def test_raid_zero_has_no_redundancy_and_mirror_can_lose_two_members():
    raid = RAIDArray(8, 12, 0, 3)
    raid.fail(1)
    with pytest.raises(DiskError):
        raid.read(0)
    with pytest.raises(DiskError):
        raid.rebuild(1)
    mirror = RAIDArray(8, 12, 1, 3)
    mirror.write(0, b"data")
    mirror.fail(1)
    mirror.fail(2)
    assert mirror.read(0) == b"data\0\0\0\0"
    mirror.rebuild(1)
    assert mirror.read(0) == b"data\0\0\0\0"


def test_file_read_reconstructs_from_xor_and_write_failure_is_atomic():
    fs = FileSystem(Disk(64, 24, RAIDArray(64, 24)), make_strategy("indexed"))
    payload = bytes(range(256)) * 2
    node = fs.write("/binary", payload)
    fs.disk.storage.fail(2)
    fs.disk.storage.members[1].data.clear()
    assert fs.read("/binary") == payload
    fs.write("/binary", payload[::-1])
    assert fs.read("/binary") == payload[::-1]
    fs.disk.storage.fail(1)
    before = list(fs.disk.owner), dict(fs.disk.raw), list(node.blocks), node.size
    with pytest.raises(DiskError):
        fs.write("/binary", b"failure")
    assert (fs.disk.owner, fs.disk.raw, node.blocks, node.size) == before
    with pytest.raises(DiskError):
        fs.create("/new", content=b"failure")
    assert "new" not in fs.root.children
    assert (fs.disk.owner, fs.disk.raw, node.blocks, node.size) == before


@pytest.mark.parametrize("kind", ["hdd", "ssd", "raid"])
def test_no_space_and_failed_relayout_preserve_physical_bytes(kind):
    fs = FileSystem(Disk(64, 5, make_storage(kind, 64, 5)), make_strategy("linked"))
    fs.write("/a", b"A" * 128)
    fs.write("/b", b"B" * 128)
    with pytest.raises(NoSpaceError):
        fs.write("/a", b"X" * 1000)
    with pytest.raises(NoSpaceError):
        fs.change_strategy("indexed")
    assert fs.read("/a") == b"A" * 128
    assert fs.read("/b") == b"B" * 128
    if kind == "raid":
        fs.disk.storage.fail(2)
        assert fs.read("/a") == b"A" * 128
        assert fs.read("/b") == b"B" * 128


def test_ssd_out_of_place_wear_leveling_and_gc_preserve_cold_data():
    ssd = SSD(8, 8)
    for block in range(8):
        ssd.write(block, bytes([block]) * 8)
    ssd.flush()
    first_page = ssd.mapping[0]
    ssd.write(0, b"first")
    ssd.flush()
    assert ssd.mapping[0] != first_page
    for count in range(300):
        ssd.write(count % 2, bytes([count % 256]) * 8)
        ssd.flush()
    assert ssd.gc_count > 0
    assert max(ssd.erase_counts) > 0
    assert all(c > 0 for c in ssd.program_counts)
    for block in range(2, 8):
        assert ssd.read(block) == bytes([block]) * 8
    assert ssd.read(0) == bytes([298 % 256]) * 8
    assert ssd.read(1) == bytes([299 % 256]) * 8
    assert ssd.total_movement == 0 and ssd.last_schedule is None


def test_storage_migration_preserves_files_and_invalid_config_is_atomic():
    fs = FileSystem(Disk(64, 32), make_strategy("linked"))
    fs.write("/a", bytes(range(200)))
    for kind in ("ssd", "raid", "hdd"):
        fs.disk.configure_storage(kind)
        assert fs.read("/a") == bytes(range(200))
    old = fs.disk.storage
    with pytest.raises(DiskError):
        fs.disk.configure_storage("raid", disks=2)
    assert fs.disk.storage is old
    assert fs.read("/a") == bytes(range(200))


def test_shell_storage_commands_and_invalid_input(capsys):
    shell = Shell(FileSystem(Disk(4096, 16), make_strategy("linked")))
    for line in ("disk_schedule 98,183,37,122,14 --head 53", "disk_config raid --level 5 --disks 3",
                 "write_file /a --size 8KB", "io_status", "io_flush", "storage_status",
                 "fail_disk 2", "read_file /a", "rebuild_disk 2"):
        assert shell.execute(line)
    output = capsys.readouterr().out
    assert "208 trilhas" in output
    assert "PARIDADE: XOR" in output and "[OK]" in output
    assert "Erro:" not in output
    for line in ("disk_schedule nope", "disk_config raid --level 6", "fail_disk 0",
                 "rebuild_disk nope", "io_flush extra", "disk_schedule 200"):
        shell.execute(line)
    assert capsys.readouterr().out.count("Erro:") == 6


@pytest.mark.parametrize("example", sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.txt")))
def test_documented_example_scripts(example):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "main.py", "--script", str(example)],
                            cwd=root, capture_output=True, encoding="utf-8",
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=30)
    assert result.returncode == 0, result.stderr
    # As demonstrações de fragmentação e ACL incluem uma falha intencional.
    if example.name.startswith("02_"):
        assert "Erro: fragmentação externa" in result.stdout
    elif example.name.startswith("07_"):
        assert result.stdout.count("Erro:") == 1
        assert "[NEGADO]" in result.stdout and "rwxr-x--- (750)" in result.stdout
        assert "Criptografado e Íntegro" in result.stdout
        assert "Hash do bloco" in result.stdout and "Assinatura maliciosa" in result.stdout
        assert "Registro privado" in result.stdout
        assert all(secret not in result.stdout for secret in ("raiz-demo", "carlos-demo", "MinhaChave123"))
    else:
        assert "Erro:" not in result.stdout
    if example.name.startswith("04_"):
        assert "208 trilhas" in result.stdout
    if example.name.startswith("05_"):
        assert "[OK]" in result.stdout and "XOR" in result.stdout
    if example.name.startswith("06_"):
        assert "valor 15" in result.stdout
        assert "Coletas de lixo: 0" not in result.stdout.split("valor 15")[-1]


@pytest.mark.parametrize("device", ["hdd", "ssd", "raid"])
def test_cli_initial_device_configuration(device, tmp_path):
    script = tmp_path / "commands.txt"
    script.write_text('write_file /a "payload"\nread_file /a\nstorage_status\n', encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "main.py", "--device", device,
                             "--scheduler", "c-scan", "--head", "40", "--direction", "down",
                             "--script", str(script)], cwd=root, capture_output=True, encoding="utf-8",
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=30)
    assert result.returncode == 0, result.stderr
    assert "payload" in result.stdout and "Erro:" not in result.stdout
