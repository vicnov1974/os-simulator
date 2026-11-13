"""Exercita autenticação, fronteiras de ACL e dados protegidos em disco."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osim import report
from osim.allocation import make_strategy
from osim.disk import Disk, NoSpaceError
from osim.filesystem import FileSystem
from osim.security import IntegrityError, SecurityError
from osim.shell import Shell
from osim.storage import make_storage


@pytest.fixture
def fs():
    return FileSystem(Disk(64, 40), make_strategy("indexed"))


@pytest.fixture
def accounts(fs):
    fs.security.set_password("root", "root-test")
    fs.security.add_group("devs")
    fs.security.add_user("carlos", "devs", "carlos-test")
    return fs


def test_salted_passwords_and_authenticated_mode_switch(accounts):
    fs = accounts
    fs.security.add_user("ana", "devs", "carlos-test")
    first = fs.security.users["carlos"].password
    second = fs.security.users["ana"].password
    assert first.salt != second.salt and first.digest != second.digest
    assert first.verify("carlos-test") and not first.verify("wrong")
    fs.login("carlos", "carlos-test")
    assert fs.security.mode == "MODO_USUARIO"
    with pytest.raises(SecurityError, match="inválidos"):
        fs.login("root", "wrong")
    assert fs.security.name == "carlos"
    fs.logout()
    with pytest.raises(SecurityError, match="login"):
        fs.read("/a")
    fs.login("root", "root-test")
    assert fs.security.mode == "MODO_KERNEL"
    assert any(e.action == "login" and e.target == "root" and e.result == "NEGADO"
               for e in fs.security.events())


def test_acl_uses_group_owner_and_other_and_requires_directory_traversal(accounts):
    fs = accounts
    fs.mkdir("/sistema")
    config = fs.write("/sistema/config.sys", b"private")
    fs.chmod(config.path(), "rwxr-x---")
    fs.mkdir("/oculto")
    hidden = fs.write("/oculto/item", b"hidden")
    fs.chmod("/oculto", "700")
    fs.mkdir("/home")
    fs.chown("/home", "carlos")
    fs.chmod("/home", "700")

    fs.login("carlos", "carlos-test")
    with pytest.raises(SecurityError, match="rwxr-x---"):
        fs.read(config.path())
    with pytest.raises(SecurityError, match="NEGADO"):
        fs.read_node(hidden)
    assert "item" not in fs.tree()
    own = fs.write("/home/meu", b"owned")
    assert (own.owner, own.group) == ("carlos", "devs")
    fs.chmod(own.path(), "700")
    assert fs.check_execute(own.path()) is own
    with pytest.raises(SecurityError, match="NEGADO"):
        fs.check_execute(config.path())
    fs.login("root", "root-test")
    fs.security.add_membership("carlos", "admin")
    fs.login("carlos", "carlos-test")
    assert fs.read(config.path()) == b"private"
    assert fs.check_execute(config.path()) is config
    with pytest.raises(SecurityError, match="NEGADO"):
        fs.write(config.path(), b"forbidden")
    with pytest.raises(SecurityError, match="NEGADO"):
        fs.chmod(config.path(), "777")


def test_mutations_respect_parent_acl_and_recursive_delete_is_preflighted(accounts):
    fs = accounts
    fs.mkdir("/public")
    fs.chmod("/public", "777")
    fs.mkdir("/public/locked")
    secret = fs.write("/public/locked/file", b"still here")
    fs.chmod("/public/locked", "500")
    fs.mkdir("/target")
    fs.chmod("/target", "555")
    fs.login("carlos", "carlos-test")
    fs.write("/public/source", b"public")
    with pytest.raises(SecurityError):
        fs.copy("/public/source", "/target/file")
    with pytest.raises(SecurityError):
        fs.move("/public/source", "/target/file")
    with pytest.raises(SecurityError):
        fs.remove("/public/locked", recursive=True)
    assert fs.read("/public/source") == b"public"
    fs.login("root", "root-test")
    assert fs.read(secret.path()) == b"still here"


def test_kernel_commands_are_denied_and_format_preserves_identity(accounts, capsys):
    fs = accounts
    fs.write("/file", b"data")
    shell = Shell(fs)
    fs.login("carlos", "carlos-test")
    commands = ("groupadd x", "chown carlos /file", "format", "compact", "set_alloc linked",
                "disk_config ssd", "fail_disk 1", "rebuild_disk 1", "storage_status",
                "io_status", "io_flush", "fs_status --details", "antivirus_scan /",
                "audit_log", "corrupt_block /file")
    for command in commands:
        shell.execute(command)
    assert capsys.readouterr().out.count("exige MODO_KERNEL") == len(commands)
    assert fs.read("/file") == b"data"
    fs.login("root", "root-test")
    assert sum(e.result == "NEGADO" for e in fs.security.events()) == len(commands)
    fs.format(64, 16, make_strategy("linked"))
    assert list(fs.root.children) == []
    fs.login("carlos", "carlos-test")
    assert fs.security.name == "carlos"


@pytest.mark.parametrize("kind", ["hdd", "ssd", "raid"])
def test_encrypted_bytes_are_stored_before_disk_io_and_updates_keep_encryption(kind):
    fs = FileSystem(Disk(64, 32, make_storage(kind, 64, 32)), make_strategy("indexed"))
    secret = b"plaintext-private" * 5
    node = fs.write("/secret", secret, key="file-test")
    data = b"".join(fs.disk.read_physical(b) for b in node.blocks)[:node.stored_size]
    assert len(data) == len(secret) + 16 and secret[:16] not in data
    with pytest.raises(SecurityError, match="informe a chave"):
        fs.read("/secret")
    with pytest.raises(SecurityError, match="chave inválida"):
        fs.write("/secret", b"replacement", key="wrong")
    assert fs.read("/secret", "file-test") == secret
    nonce = node.encryption.nonce
    fs.write("/secret", b"!", append=True, key="file-test")
    assert node.encryption.nonce != nonce
    fs.resize("/secret", len(secret), "file-test")
    assert fs.read("/secret", "file-test") == secret
    fs.copy("/secret", "/copy")
    assert fs.read("/copy", "file-test") == secret
    fs.move("/copy", "/renamed")
    fs.decrypt_file("/renamed", "file-test")
    assert fs.read("/renamed") == secret
    assert report.analyze(fs)["internal_total"] == sum(
        len(n.blocks) * fs.disk.block_size - n.stored_size for n in fs.files())


def test_compaction_migration_and_raid_recovery_preserve_ciphertext(fs):
    fs.mkdir("/dados")
    node = fs.create("/dados/privado", content=bytes(range(180)), key="file-test")
    hashes = node.block_hashes
    fs.change_strategy("linked")
    fs.compact()
    fs.configure_storage("ssd")
    fs.configure_storage("raid", raid_level=5, disks=3)
    member = fs.disk.storage.location(node.blocks[0])[1] + 1
    fs.raid_action("fail", member)
    assert fs.read(node.path(), "file-test") == bytes(range(180))
    assert fs.disk.storage.recoveries
    fs.raid_action("rebuild", member)
    assert node.block_hashes == hashes
    assert fs.read(node.path(), "file-test") == bytes(range(180))
    assert all(status.startswith("[OK") for _, status in fs.antivirus_scan("/dados"))


def test_failed_encryption_expansion_keeps_old_file():
    fs = FileSystem(Disk(64, 2), make_strategy("linked"))
    node = fs.write("/full", b"a" * 64)
    hashes = node.block_hashes
    with pytest.raises(NoSpaceError):
        fs.encrypt_file("/full", "file-test")
    assert node.encryption is None and node.block_hashes == hashes
    assert fs.read("/full") == b"a" * 64


def test_antivirus_detects_physical_tamper_padding_inode_and_signatures(fs):
    node = fs.write("/physical", b"good")
    fs.write("/signature", b"test OSIM_TEST_MALWARE marker")
    fs.create("/sealed", content=b"private", key="file-test")
    metadata = fs.mkdir("/metadata")
    fs.disk.flush()
    physical = fs.disk.storage
    block = node.blocks[0]
    physical.data[block] = physical.data[block][:-1] + b"X"
    physical.data[metadata.meta_block] = b"X" + bytes(63)
    results = dict(fs.antivirus_scan())
    assert "Hash do bloco" in results["/physical"]
    assert "Hash do bloco" in results["/metadata"]
    assert "Assinatura maliciosa" in results["/signature"]
    assert results["/sealed"].startswith("[OK - Criptografado e Íntegro]")
    with pytest.raises(IntegrityError):
        fs.read("/physical")
    with pytest.raises(IntegrityError):
        fs.configure_storage("ssd")
    assert fs.disk.storage is physical


@pytest.mark.parametrize("level", [1, 5])
def test_antivirus_checks_redundant_raid_blocks(fs, level):
    fs.configure_storage("raid", raid_level=level, disks=3)
    node = fs.write("/payload", b"healthy")
    fs.disk.flush()
    raid = fs.disk.storage
    row, _, parity = raid.location(node.blocks[0])
    member = raid.members[1 if level == 1 else parity]
    block = member.data[row]
    member.data[row] = bytes([block[0] ^ 1]) + block[1:]
    assert fs.read("/payload") == b"healthy"
    assert any("Espelho ou paridade divergente" in status for _, status in fs.antivirus_scan())


def test_storage_history_does_not_retain_plaintext_after_encrypt_file(fs):
    fs.write("/secret", b"old cleartext")
    node, _ = fs.encrypt_file("/secret", "file-test")
    assert b"old cleartext" not in fs.disk.read_physical(node.blocks[0])
    assert all(not request.data and not request.result for request in fs.disk.storage.history)


def test_key_on_existing_plaintext_encrypts_new_content(fs):
    fs.write("/write", b"plain")
    fs.write("/write", b"protected", key="file-test")
    fs.write("/resize", b"plain")
    fs.resize("/resize", 3, key="file-test")
    for path, expected in (("/write", b"protected"), ("/resize", b"pla")):
        node = fs.resolve(path)
        assert node.encryption is not None
        assert fs.read(path, "file-test") == expected
        assert fs.disk.read_physical(node.blocks[0])[:len(expected)] != expected
