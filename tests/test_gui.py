"""Testes da interface gráfica (pulados se não houver tkinter ou display)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tk = pytest.importorskip("tkinter")

from osim.allocation import ContiguousAllocation  # noqa: E402
from osim.disk import Disk  # noqa: E402
from osim.filesystem import FileSystem  # noqa: E402
from osim.gui import App  # noqa: E402


@pytest.fixture
def app():
    try:
        a = App(FileSystem(Disk(1024, 32), ContiguousAllocation()))
    except tk.TclError:
        pytest.skip("sem display gráfico")
    a.errors = []
    a.show_error = a.errors.append
    a.update()
    yield a
    a.destroy()


def rows(a):
    return [a.lv.item(i, "text") for i in a.lv.get_children()]


def test_create_navigate_and_list(app):
    assert app.new_folder("docs")
    assert any("docs" in r for r in rows(app))
    assert app.navigate("/docs")
    assert app.new_file("a.txt", "2500")
    assert any("a.txt" in r for r in rows(app))
    assert app.fs.cwd.path() == "/docs"
    app.go_up()
    assert app.fs.cwd.path() == "/"
    app.go_back()
    assert app.fs.cwd.path() == "/docs"
    app.go_forward()
    assert app.fs.cwd.path() == "/"


def test_map_draws_one_cell_per_block_and_diag(app):
    app.new_file("a.txt", "2500")                    # 3 blocos, último parcial
    app.update()
    assert len(app.canvas.find_all()) >= 32
    text = app.diag.get("1.0", "end")
    assert "Fragmentação Interna" in text and "ARQUIVO SELECIONADO" in text
    assert "COMPACTAÇÃO" not in app.banner.cget("text")


def test_external_fragmentation_banner_and_compact(app):
    for name in ("a", "b", "c"):
        app.new_file(name, "2KB")
    app.delete("/b")
    assert "NECESSÁRIA" in app.banner.cget("text")
    assert app.compact()
    assert "não necessária" in app.banner.cget("text")
    assert app.fs.read("/c") == b"\0" * 2048


def test_rename_delete_and_errors(app):
    app.new_folder("x")
    assert not app.new_folder("x")                   # duplicada
    assert app.errors
    assert not app.new_folder("a/b")                 # nome inválido
    assert app.rename("/x", "y")
    assert "y" in app.fs.root.children
    assert app.delete("/y")
    assert not app.fs.root.children


def test_edit_save_and_strategy_switch(app):
    app.new_file("n.txt", "", "ola")
    assert app.save_file("/n.txt", "novo conteudo")
    assert app.fs.read("/n.txt") == b"novo conteudo"
    assert app.set_strategy("indexed")
    assert app.fs.resolve("/n.txt").meta_block is not None
    assert app.fs.read("/n.txt") == b"novo conteudo"


def test_format(app):
    app.new_folder("x")
    assert app.format_disk("512B", "16", "linked")
    assert app.fs.disk.total_blocks == 16 and not app.fs.root.children


def test_storage_switch_and_raid_controls(app):
    app.new_file("dados", "", "conteudo")
    assert app.configure_storage("ssd")
    assert app.fs.read("/dados") == b"conteudo"
    assert app.configure_storage("raid", raid_level=5, disks=3)
    assert app.raid_action("fail", 2)
    assert app.fs.read("/dados") == b"conteudo"
    assert app.raid_action("rebuild", 2)
    assert app.flush_io()
    app.show_storage()
    app.update()
    assert "RAID 5" in app.diag.get("1.0", "end")
