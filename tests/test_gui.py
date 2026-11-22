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


@pytest.mark.parametrize("geometry", ["640x420", "640x240"])
@pytest.mark.parametrize("binary", [False, True])
def test_editor_save_button_stays_visible_and_saves(app, geometry, binary):
    app.new_file("editar.txt", "10KB" if binary else "", "" if binary else "original")
    app.open_editor(app.fs.resolve("/editar.txt"))
    win = app.session_windows[-1]
    win.geometry(geometry)
    app.update()
    text = next(w for w in win.winfo_children() if isinstance(w, tk.Text))
    toolbar = next(w for w in win.winfo_children() if w.winfo_class() == "TFrame")
    save = next(w for w in toolbar.winfo_children() if w.cget("text") == "Salvar")
    assert toolbar.winfo_height() >= save.winfo_reqheight()
    assert save.winfo_ismapped()
    assert toolbar.winfo_y() + toolbar.winfo_height() <= win.winfo_height()
    assert text.winfo_y() + text.winfo_height() <= toolbar.winfo_y()
    text.delete("1.0", "end")
    text.insert("1.0", "alteração salva")
    save.invoke()
    assert app.fs.read("/editar.txt") == "alteração salva".encode()
    assert not win.winfo_exists() and not app.errors


@pytest.mark.parametrize("encrypted", [False, True])
def test_editor_ctrl_s_saves_text_and_preserves_encryption(app, monkeypatch, encrypted):
    app.new_file("atalho.txt", "", "original")
    if encrypted:
        app.fs.encrypt_file("/atalho.txt", "chave-teste")
        monkeypatch.setattr(app, "_ask_secret", lambda label: "chave-teste")
    app.open_editor(app.fs.resolve("/atalho.txt"))
    win = app.session_windows[-1]
    text = next(w for w in win.winfo_children() if isinstance(w, tk.Text))
    text.delete("1.0", "end")
    text.insert("1.0", "salvo pelo atalho")
    win.focus_force()
    text.focus_force()
    app.update()
    text.event_generate("<Control-s>")
    app.update()
    assert not win.winfo_exists() and not app.errors
    assert app.fs.read("/atalho.txt", "chave-teste" if encrypted else None) == b"salvo pelo atalho"
    assert bool(app.fs.resolve("/atalho.txt").encryption) == encrypted


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


def test_memory_panel_buttons_swap_map_and_session_cleanup(app):
    from osim.memory import MemoryManager

    app.fs.memory = MemoryManager(app.fs, ram_size=12288, reserved_size=8192, kernel_size=4096)
    app.show_memory()
    app.update()
    win = app.session_windows[-1]

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    buttons = {widget.cget("text"): widget for widget in descendants(win)
               if widget.winfo_class() == "TButton"}
    buttons["Criar processo"].invoke()
    buttons["Executar instrução"].invoke()
    memory = app.fs.memory
    memory.execute("App_Navegador", "STORE_DATA Página 0 conteúdo")
    memory.translate("App_Navegador", 1)
    app.refresh()
    assert not app.errors
    assert "RAM dinâmica" in app.diag.get("1.0", "end")
    app.draw_map()  # renderiza os blocos SWAP sem tratá-los como arquivos da árvore
    assert memory.stats()["swap_blocks"] == 4  # blocos de disco de 1KB
    assert memory.execute("App_Navegador", "LOAD_DATA Página 0")[1].startswith("conteúdo".encode())
    buttons["Compactar reservada"].invoke()
    buttons["Aplicar algoritmo"].invoke()
    buttons["Encerrar"].invoke()
    assert not memory.processes and not app.errors
    app.fs.security.add_user("ana", "users", "test-ana")
    assert app.login("ana", "test-ana")
    assert not win.winfo_exists()
    app.show_memory()
    assert app.errors and "MODO_KERNEL" in app.errors[-1]


def test_session_switch_closes_privileged_windows_and_hides_restricted_paths(app):
    app.fs.security.set_password("root", "root-test")
    app.fs.security.add_user("carlos", "users", "carlos-test")
    app.fs.mkdir("/restrito/subpasta", parents=True)
    app.fs.write("/restrito/subpasta/sigilo", b"privado")
    app.fs.chmod("/restrito", "700")
    app.open_editor(app.fs.resolve("/restrito/subpasta/sigilo"))
    app.show_storage()
    old_windows = tuple(app.session_windows)

    assert app.login("carlos", "carlos-test")
    app.update()
    assert all(not win.winfo_exists() for win in old_windows)
    assert not app.tree.exists("/restrito/subpasta")
    assert "MODO_USUARIO" in app.status.get()
    assert not app.navigate("/restrito")
    assert not app.format_disk("512B", "16", "linked")
    app.show_storage()
    assert not app.session_windows and "MODO_KERNEL" in app.errors[-1]
    app.logout()
    assert not rows(app)
    assert app.login("root", "root-test")
    assert app.navigate("/restrito/subpasta")
    assert rows(app) == ["sigilo"]


def test_process_panel_io_threads_metrics_and_session_cleanup(app):
    app.show_processes()
    app.update()
    win = app.session_windows[-1]
    values = win.process_values

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    buttons = {w.cget("text"): w for w in descendants(win) if w.winfo_class() == "TButton"}
    buttons["Criar com perfil I/O"].invoke()
    buttons["Criar thread"].invoke()
    values["Thread"].set("T1")
    buttons["Agendar mutex"].invoke()
    values["Thread"].set("T2")
    buttons["Agendar mutex"].invoke()
    values["Quantum (ms)"].set("1")
    buttons["Avançar passos"].invoke()
    assert app.fs.memory.cpu.clock == 1
    buttons["Executar até 1000ms"].invoke()
    buttons["Atualizar"].invoke()
    process = app.fs.memory.cpu.processes["App_Web"]
    assert process.state == "CONCLUÍDO" and process.executed == 11
    assert not app.errors
    texts = [w.get("1.0", "end") for w in descendants(win) if isinstance(w, tk.Text)]
    assert any("Tempo Médio de Espera" in text for text in texts)
    assert any("BLOQUEADO (Mutex" in text and "SYSCALL" in text for text in texts)
    assert any("Moldura" in text and "LRU" in text for text in texts)
    buttons["Encerrar processo"].invoke()
    assert not app.fs.memory.processes and not app.errors
    app.fs.security.add_user("ana", "users", "test-ana")
    assert app.login("ana", "test-ana")
    assert not win.winfo_exists()
    app.show_processes()
    assert "MODO_KERNEL" in app.errors[-1]


def test_process_panel_invalid_profile_preserves_memory(app):
    app.show_processes()
    app.update()
    win = app.session_windows[-1]
    win.process_values["Burst (ms)"].set("invalido")
    for frame in win.winfo_children():
        for child in frame.winfo_children():
            if child.winfo_class() == "TButton" and child.cget("text") == "Criar com perfil I/O":
                child.invoke()
    assert app.errors and not app.fs.memory.processes
