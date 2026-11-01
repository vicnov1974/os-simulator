"""Shell interativo do simulador (comandos estilo Unix em português)."""
import re
import shlex

from . import report
from .allocation import STRATEGIES, make_strategy
from .disk import Disk, DiskError
from .filesystem import FileSystem, FSError
from .utils import format_size, parse_size

HELP = """\
COMANDOS (caminhos absolutos '/a/b' ou relativos 'a/b', './a', '../a')

 Navegação
  pwd                         mostra o diretório corrente
  cd <dir>                    muda de diretório
  ls [-l] [caminho]           lista o conteúdo
  tree [caminho]              mostra a árvore hierárquica
  stat <caminho>              detalhes e blocos ocupados

 CRUD
  mkdir [-p] <dir>...         cria diretório(s)
  create <arq> [--size 10KB] [--content "texto"]
                              cria arquivo (tamanho e/ou conteúdo)
  cat <arq>                   lê o conteúdo
  write <arq> <texto>         sobrescreve (cria se não existir)
  append <arq> <texto>        anexa ao final
  resize <arq> <tamanho>      aumenta/reduz o arquivo (ex.: resize a.txt 20KB)
  mv <origem> <destino>       move/renomeia
  cp <origem> <destino>       copia arquivo
  rm [-r] <caminho>...        remove arquivo (ou diretório com -r)
  rmdir <dir>...              remove diretório vazio

 Disco
  fs_status [--details]       mapa do disco + fragmentação + compactação
  compact                     compacta o disco (elimina fragmentação externa)
  set_alloc <contiguous|linked|indexed>
                              migra o disco para outra estratégia de alocação
  format [--block-size 4KB] [--blocks 16] [--alloc indexed]
                              recria o disco (APAGA tudo)

 Outros
  help                        esta ajuda
  exit | quit                 sai
"""


def parse_opts(args, valued=(), flags=()):
    """Separa argumentos posicionais de opções (--x valor, --x=valor, -r, -rf)."""
    expanded = []
    for a in args:
        if re.fullmatch(r"-[A-Za-z]{2,}", a) and all(f"-{c}" in flags for c in a[1:]):
            expanded += [f"-{c}" for c in a[1:]]
        else:
            expanded.append(a)
    pos, opts, i = [], {}, 0
    while i < len(expanded):
        a = expanded[i]
        if a.startswith("--") and "=" in a and a.split("=", 1)[0] in valued:
            k, v = a.split("=", 1)
            opts[k] = v
        elif a in valued:
            if i + 1 >= len(expanded):
                raise FSError(f"a opção {a} requer um valor")
            opts[a] = expanded[i + 1]
            i += 1
        elif a in flags:
            opts[a] = True
        elif a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
            raise FSError(f"opção desconhecida: {a}")
        else:
            pos.append(a)
        i += 1
    return pos, opts


def _size(text: str) -> int:
    try:
        return parse_size(text)
    except ValueError as exc:
        raise FSError(str(exc))


def _text(parts) -> bytes:
    return " ".join(parts).replace("\\n", "\n").encode("utf-8")


class Shell:
    def __init__(self, fs: FileSystem, color: bool = False):
        self.fs = fs
        self.color = color
        self.commands = {
            "help": self.c_help, "pwd": self.c_pwd, "cd": self.c_cd, "ls": self.c_ls,
            "tree": self.c_tree, "stat": self.c_stat, "mkdir": self.c_mkdir,
            "create": self.c_create, "touch": self.c_create, "cat": self.c_cat,
            "write": self.c_write, "append": self.c_append, "resize": self.c_resize,
            "mv": self.c_mv, "cp": self.c_cp, "rm": self.c_rm, "rmdir": self.c_rmdir,
            "fs_status": self.c_status, "compact": self.c_compact,
            "set_alloc": self.c_set_alloc, "format": self.c_format,
        }

    @property
    def prompt(self) -> str:
        return f"{self.fs.cwd.path()}> "

    def execute(self, line: str) -> bool:
        """Executa uma linha. Retorna False quando o usuário pede para sair."""
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"Erro: {exc}")
            return True
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("exit", "quit", "sair"):
            return False
        handler = self.commands.get(cmd)
        if handler is None:
            print(f"Comando desconhecido: '{cmd}'. Digite 'help'.")
            return True
        try:
            handler(args)
        except (FSError, DiskError) as exc:
            print(f"Erro: {exc}")
        return True

    # ---- navegação -----------------------------------------------------
    def c_help(self, args):
        print(HELP)

    def c_pwd(self, args):
        print(self.fs.cwd.path())

    def c_cd(self, args):
        self.fs.chdir(args[0] if args else "/")

    def c_ls(self, args):
        pos, opts = parse_opts(args, flags=("-l",))
        node = self.fs.resolve(pos[0] if pos else ".")
        items = self.fs.sorted_children(node) if node.is_dir else [node]
        if "-l" in opts:
            for n in items:
                if n.is_dir:
                    print(f"[DIR] {n.name}/")
                else:
                    print(f"[ARQ] {n.name:<20} {n.size:>8} B  {len(n.blocks)} bloco(s)")
        else:
            print("  ".join(n.name + ("/" if n.is_dir else "") for n in items) or "(vazio)")

    def c_tree(self, args):
        print(self.fs.tree(args[0] if args else "/"))

    def c_stat(self, args):
        if not args:
            raise FSError("uso: stat <caminho>")
        n = self.fs.resolve(args[0])
        print(f"Caminho : {n.path()}")
        print(f"Tipo    : {'diretório' if n.is_dir else 'arquivo'}")
        if n.is_dir:
            print(f"Itens   : {len(n.children)}")
            print(f"Bloco   : {n.meta_block}")
            return
        bs = self.fs.disk.block_size
        print(f"Tamanho : {n.size} bytes ({format_size(n.size)})")
        if n.meta_block is not None:
            print(f"I-node  : bloco {n.meta_block}")
        print(f"Blocos  : {n.blocks or '-'} ({len(n.blocks)} x {format_size(bs)})")
        print(f"Frag. interna: {len(n.blocks) * bs - n.size} bytes")

    # ---- CRUD ------------------------------------------------------------
    def c_mkdir(self, args):
        pos, opts = parse_opts(args, flags=("-p",))
        if not pos:
            raise FSError("uso: mkdir [-p] <dir>...")
        for p in pos:
            self.fs.mkdir(p, parents="-p" in opts)

    def c_create(self, args):
        pos, opts = parse_opts(args, valued=("--size", "--content"))
        if len(pos) != 1:
            raise FSError('uso: create <arq> [--size 10KB] [--content "texto"]')
        size = _size(opts["--size"]) if "--size" in opts else None
        content = _text([opts["--content"]]) if "--content" in opts else b""
        node = self.fs.create(pos[0], size=size, content=content)
        print(f"Arquivo '{node.path()}' criado: {node.size} bytes em "
              f"{len(node.blocks)} bloco(s) {node.blocks}")

    def c_cat(self, args):
        if not args:
            raise FSError("uso: cat <arq>")
        data = self.fs.read(args[0])
        if b"\0" in data:
            print(f"[conteúdo binário/zerado: {len(data)} bytes]")
        else:
            print(data.decode("utf-8", errors="replace"))

    def c_write(self, args):
        if len(args) < 2:
            raise FSError("uso: write <arq> <texto>")
        n = self.fs.write(args[0], _text(args[1:]))
        print(f"'{n.path()}' atualizado: {n.size} bytes, blocos {n.blocks}")

    def c_append(self, args):
        if len(args) < 2:
            raise FSError("uso: append <arq> <texto>")
        n = self.fs.write(args[0], _text(args[1:]), append=True)
        print(f"'{n.path()}' atualizado: {n.size} bytes, blocos {n.blocks}")

    def c_resize(self, args):
        if len(args) != 2:
            raise FSError("uso: resize <arq> <tamanho>")
        n = self.fs.resize(args[0], _size(args[1]))
        print(f"'{n.path()}' agora com {n.size} bytes, blocos {n.blocks}")

    def c_mv(self, args):
        if len(args) != 2:
            raise FSError("uso: mv <origem> <destino>")
        self.fs.move(*args)

    def c_cp(self, args):
        if len(args) != 2:
            raise FSError("uso: cp <origem> <destino>")
        self.fs.copy(*args)

    def c_rm(self, args):
        pos, opts = parse_opts(args, flags=("-r", "-R", "-f"))
        if not pos:
            raise FSError("uso: rm [-r] <caminho>...")
        for p in pos:
            freed = self.fs.remove(p, recursive=("-r" in opts or "-R" in opts))
            print(f"'{p}' removido ({freed} bloco(s) liberado(s))")

    def c_rmdir(self, args):
        if not args:
            raise FSError("uso: rmdir <dir>...")
        for p in args:
            self.fs.remove(p, only_dir=True)

    # ---- disco -----------------------------------------------------------
    def c_status(self, args):
        _, opts = parse_opts(args, flags=("--details", "-d"))
        print(report.render_status(self.fs, details=bool(opts), color=self.color))

    def c_compact(self, args):
        before = report.analyze(self.fs)
        moved = self.fs.compact()
        print(f"Compactação concluída: {moved} bloco(s) realocado(s). "
              f"Regiões livres: {len(before['free_runs'])} -> "
              f"{len(self.fs.disk.free_runs())}.")

    def c_set_alloc(self, args):
        if len(args) != 1:
            raise FSError(f"uso: set_alloc <{'|'.join(STRATEGIES)}>")
        try:
            moved = self.fs.change_strategy(args[0])
        except ValueError as exc:
            raise FSError(str(exc))
        print(f"Estratégia alterada para {self.fs.strategy.title}; "
              f"{moved} bloco(s) realocado(s).")

    def c_format(self, args):
        _, opts = parse_opts(args, valued=("--block-size", "--blocks", "--alloc"))
        d = self.fs.disk
        bs = _size(opts["--block-size"]) if "--block-size" in opts else d.block_size
        try:
            blocks = int(opts["--blocks"]) if "--blocks" in opts else d.total_blocks
            strategy = make_strategy(opts["--alloc"]) if "--alloc" in opts else self.fs.strategy
        except ValueError as exc:
            raise FSError(str(exc))
        self.fs = FileSystem(Disk(bs, blocks), strategy)
        print(f"Disco formatado: {blocks} blocos de {format_size(bs)} "
              f"({format_size(bs * blocks)}), alocação {strategy.title}.")
