"""Shell interativo do simulador (comandos estilo Unix em português)."""
import getpass
import re
import shlex
import warnings

from . import report
from .allocation import STRATEGIES, make_strategy
from .disk import DiskError
from .filesystem import FileSystem, FSError
from .utils import format_size, parse_size
from .scheduling import schedule, render_schedule
from .storage import render_io, render_storage
from .security import SecurityError, describe_acl, render_scan
from .memory import MemoryError, render_memory, render_process, render_translation

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
  create <arq> [--size 10KB] [--content "texto"] [--ask-key]
                              cria arquivo (tamanho e/ou conteúdo)
  cat <arq> [--ask-key]       lê o conteúdo
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

 Armazenamento
  write_file <arq> <texto> | write_file <arq> --size 8KB
  read_file <arq>             aliases para escrita/leitura
  disk_config <hdd|ssd|raid> [--level 5] [--disks 3]
               [--algorithm sstf] [--tracks 200] [--head 53] [--direction up]
                              migra arquivos para o dispositivo escolhido
  disk_schedule <trilhas...> [--algorithm sstf] [--head 53]
               [--tracks 200] [--direction up|down]
                              demonstra uma fila independente (não altera arquivos)
  io_status | io_flush        consulta/atende a fila física real
  storage_status              distribuição RAID ou páginas/desgaste SSD (atende E/S)
  fail_disk <n>               falha de disco RAID (numeração começa em 1)
  rebuild_disk <n>            reconstrói disco RAID usando espelho ou XOR

 Segurança
  whoami | login --user <nome> | logout
  groupadd <grupo> | useradd <nome> [--group grupo]
  usermod <nome> --group <grupo>  adiciona um grupo suplementar
  passwd [usuário]               define a senha (use passwd root na configuração)
  chmod <750|rwxr-x---> <caminho> | chown <usuário[:grupo]> <caminho>
  exec_file <arq>             verifica execução simulada (x)
  encrypt_file <arq> | decrypt_file <arq>  solicitam a chave sem exibi-la
  cat | write | write_file | append | resize: --ask-key para arquivo criptografado
  antivirus_scan [caminho] | audit_log
  corrupt_block <arq> [índice]  simula adulteração de bloco (índice a partir de 0)

 Memória (Etapa 4)
  create_process --name <nome> --size 10KB --type <critical|normal>
  terminate_process --name <nome>
  translate_address --process <nome> --page <n> [--offset <bytes>]
  execute_step --process <nome> --instruction "LOAD_DATA Página 2"
  execute_step --process <nome> --instruction "STORE_DATA Página 2 texto"
  memory_status [--reserved|--virtual] [--details]
  memory_compact --reserved    compacta a área contígua da RAM
  memory_algorithm <lru|second-chance>
  memory_reports              histórico periódico de fragmentação

 Processos e CPU (Etapa 5; execução global requer modo kernel)
  create_process --name <nome> --burst 8ms [--priority 1] [--size 16KB]
                 [--type normal|critical] [--seed 0]
                 [--syscall "read_disk at 3ms (duration: 4ms)"|random]
  schedule --algorithm Priority_RR --quantum 2ms
                              configura e executa até concluir ou detectar deadlock
  run_simulation [--process <nome>] [--steps 3]
                              avança o relógio global; --process é alvo de parada
  process_status | system_metrics | ram_status
  create_thread --process <nome> --name T2 --burst 3ms
  thread_action --process <nome> --thread T1 --at 0ms
                --operation lock|unlock --mutex Buffer_Video
                              configura ações antes de executar; mutex global FIFO

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


def has_inline_secret(line):
    return "--password" in line or "--key" in line


def _prompt_secret(label):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(label)
    except getpass.GetPassWarning:
        raise SecurityError("entrada oculta indisponível neste terminal") from None
    except (EOFError, KeyboardInterrupt):
        raise SecurityError("entrada de credencial cancelada") from None


def _secret(opts, flag, label):
    return opts[flag] if flag in opts else _prompt_secret(label)


def _file_key(opts):
    if "--ask-key" in opts and "--key" in opts:
        raise FSError("use apenas uma opção de chave")
    if "--ask-key" in opts:
        return _prompt_secret("Chave: ")
    return opts.get("--key")


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
            "write_file": self.c_write_file, "read_file": self.c_cat,
            "disk_config": self.c_disk_config, "disk_schedule": self.c_disk_schedule,
            "io_status": self.c_io_status, "io_flush": self.c_io_flush,
            "storage_status": self.c_storage_status,
            "fail_disk": self.c_fail_disk, "rebuild_disk": self.c_rebuild_disk,
            "whoami": self.c_whoami, "login": self.c_login, "logout": self.c_logout,
            "groupadd": self.c_groupadd, "useradd": self.c_useradd, "usermod": self.c_usermod,
            "passwd": self.c_passwd, "chmod": self.c_chmod, "chown": self.c_chown,
            "exec_file": self.c_exec_file, "encrypt_file": self.c_encrypt_file,
            "decrypt_file": self.c_decrypt_file, "antivirus_scan": self.c_antivirus_scan,
            "audit_log": self.c_audit_log, "corrupt_block": self.c_corrupt_block,
            "create_process": self.c_create_process, "terminate_process": self.c_terminate_process,
            "translate_address": self.c_translate_address, "execute_step": self.c_execute_step,
            "memory_status": self.c_memory_status, "memory_compact": self.c_memory_compact,
            "memory_algorithm": self.c_memory_algorithm, "memory_reports": self.c_memory_reports,
            "schedule": self.c_schedule, "run_simulation": self.c_run_simulation,
            "process_status": self.c_process_status, "system_metrics": self.c_process_status,
            "ram_status": self.c_ram_status, "create_thread": self.c_create_thread,
            "thread_action": self.c_thread_action,
        }

    @property
    def prompt(self) -> str:
        return f"{self.fs.security.name}:{self.fs.cwd.path()}> "

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
            if cmd not in ("help", "login", "logout", "whoami"):
                self.fs.security.require_session()
            before = self.fs.memory.operations
            handler(args)
            memory = self.fs.memory
            if memory.operations != before and memory.operations % memory.report_every == 0 and self.fs.security.is_kernel:
                print(self._memory_snapshot(*memory.periodic_reports[-1]))
        except (FSError, DiskError, SecurityError, MemoryError) as exc:
            print(f"Erro: {exc}")
        return True

    # ---- memória -------------------------------------------------------
    @staticmethod
    def _memory_options(args, required, optional=(), flags=()):
        pos, opts = parse_opts(args, valued=required + optional, flags=flags)
        if pos or any(key not in opts for key in required):
            raise FSError("informe " + " ".join(required) + " com seus valores")
        return opts

    def c_create_process(self, args):
        opts = self._memory_options(args, ("--name",),
                                    ("--size", "--type", "--burst", "--priority", "--syscall", "--seed"))
        if "--burst" in opts:
            scheduled = self.fs.memory.cpu.create(
                opts["--name"], opts["--burst"], opts.get("--priority", 1),
                _size(opts["--size"]) if "--size" in opts else None,
                opts.get("--type", "normal"), opts.get("--syscall"), opts.get("--seed", 0))
            process = scheduled.memory
            print(f"[CRIAÇÃO DE PROCESSO] PID: {process.pid} | Nome: {process.name} | "
                  f"Prioridade: {scheduled.priority} | Burst: {scheduled.burst}ms")
            for syscall in scheduled.syscalls:
                print(f"Syscall/I/O: {syscall.operation} em CPU={syscall.at}ms "
                      f"(Duração do Bloqueio: {syscall.duration}ms)")
            print(f"Estado Inicial: {scheduled.state}")
        else:
            if "--size" not in opts or "--type" not in opts or any(k in opts for k in ("--priority", "--syscall", "--seed")):
                raise FSError("informe --burst ou --size e --type (comando da Etapa 4)")
            process = self.fs.memory.create(opts["--name"], _size(opts["--size"]), opts["--type"])
        print(render_process(process, self.fs.memory.page_size))

    def c_schedule(self, args):
        opts = self._memory_options(args, ("--algorithm", "--quantum"))
        cpu = self.fs.memory.cpu
        cpu.configure(opts["--quantum"], opts["--algorithm"])
        print(cpu.run())
        print(cpu.render_ram())
        print(cpu.render())

    def c_run_simulation(self, args):
        opts = self._memory_options(args, (), ("--process", "--steps"))
        cpu = self.fs.memory.cpu
        print(cpu.run(opts.get("--steps"), opts.get("--process")))
        print(cpu.render_ram())
        print(cpu.render())

    def c_process_status(self, args):
        self._memory_options(args, ())
        print(self.fs.memory.cpu.render())

    def c_ram_status(self, args):
        self._memory_options(args, ())
        print(self.fs.memory.cpu.render_ram())

    def c_create_thread(self, args):
        opts = self._memory_options(args, ("--process", "--name", "--burst"))
        self.fs.memory.cpu.create_thread(opts["--process"], opts["--name"], opts["--burst"])
        print(f"Thread {opts['--name']} criada em {opts['--process']} (PRONTO).")

    def c_thread_action(self, args):
        opts = self._memory_options(args, ("--process", "--thread", "--at", "--operation", "--mutex"))
        self.fs.memory.cpu.thread_action(opts["--process"], opts["--thread"], opts["--at"],
                                         opts["--operation"], opts["--mutex"])
        print("Ação de mutex agendada.")

    def c_terminate_process(self, args):
        opts = self._memory_options(args, ("--name",))
        process = self.fs.memory.terminate(opts["--name"])
        label = "CRÍTICO" if process.kind == "critical" else "NORMAL"
        print(f"[ENCERRAMENTO DE PROCESSO {label}] '{process.name}' finalizado.")
        if process.kind == "critical":
            print(f"Área de {format_size(process.size)} no intervalo "
                  f"[{format_size(process.start)} a {format_size(process.start + process.size)}] liberada.")
        else:
            print("Molduras e blocos de swap liberados.")

    def c_translate_address(self, args):
        opts = self._memory_options(args, ("--process", "--page"), ("--offset",))
        try:
            page, offset = int(opts["--page"]), int(opts.get("--offset", 0))
        except ValueError:
            raise FSError("página e deslocamento devem ser inteiros") from None
        print(render_translation(self.fs.memory.translate(opts["--process"], page, offset)))

    def c_execute_step(self, args):
        opts = self._memory_options(args, ("--process", "--instruction"))
        result, data = self.fs.memory.execute(opts["--process"], opts["--instruction"])
        print("[CPU - REFERÊNCIA DE MEMÓRIA EM EXECUÇÃO]")
        print(render_translation(result))
        print(f"Dados da página ({len(data)} Bytes): {data[:64]!r}")

    def c_memory_status(self, args):
        opts = self._memory_options(args, (), flags=("--reserved", "--virtual", "--details"))
        if "--reserved" in opts and "--virtual" in opts:
            raise FSError("use apenas --reserved ou --virtual")
        region = "reserved" if "--reserved" in opts else "virtual" if "--virtual" in opts else None
        print(render_memory(self.fs.memory, region, "--details" in opts))

    def c_memory_compact(self, args):
        if args != ["--reserved"]:
            raise FSError("uso: memory_compact --reserved")
        print("[EXECUTANDO COMPACTAÇÃO DA ÁREA RESERVADA DA RAM]")
        for name, old, new, size in self.fs.memory.compact():
            print(f"{name}: [{format_size(old)} a {format_size(old + size)}] -> "
                  f"[{format_size(new)} a {format_size(new + size)}]")
        print(render_memory(self.fs.memory, "reserved"))

    def c_memory_algorithm(self, args):
        if len(args) != 1:
            raise FSError("uso: memory_algorithm <lru|second-chance>")
        self.fs.memory.set_algorithm(args[0])
        print(f"Substituição de páginas: {self.fs.memory.algorithm.upper()}")

    @staticmethod
    def _memory_snapshot(operation, stats):
        return (f"[RELATÓRIO PERIÓDICO DE MEMÓRIA - operação {operation}] "
                f"Fragmentação Interna: {stats['internal']} Bytes "
                f"(RAM: {stats['resident_internal']} Bytes); "
                f"Fragmentação Externa: {stats['external']} Bytes; "
                f"RAM reservada: {stats['reserved_used']} Bytes; dinâmica: {stats['resident_bytes']} Bytes")

    def c_memory_reports(self, args):
        self.fs.security.require_kernel("relatórios periódicos de memória")
        if args:
            raise FSError("uso: memory_reports")
        print("\n".join(self._memory_snapshot(op, stats) for op, stats in self.fs.memory.periodic_reports)
              or "Nenhum relatório periódico de memória ainda.")

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
        print(describe_acl(n))
        print(f"Caminho : {n.path()}")
        print(f"Tipo    : {'diretório' if n.is_dir else 'arquivo'}")
        if n.is_dir:
            if self.fs.security.allows(n, "rx"):
                print(f"Itens   : {len(self.fs.sorted_children(n))}")
            print(f"Bloco   : {n.meta_block}")
            return
        bs = self.fs.disk.block_size
        if n.encryption:
            print(f"Criptografia: AES-GCM ({n.stored_size} bytes no disco)")
        print(f"Tamanho : {n.size} bytes ({format_size(n.size)})")
        if n.meta_block is not None:
            print(f"I-node  : bloco {n.meta_block}")
        print(f"Blocos  : {n.blocks or '-'} ({len(n.blocks)} x {format_size(bs)})")
        print(f"Frag. interna: {len(n.blocks) * bs - n.stored_size} bytes")

    # ---- CRUD ------------------------------------------------------------
    def c_mkdir(self, args):
        pos, opts = parse_opts(args, flags=("-p",))
        if not pos:
            raise FSError("uso: mkdir [-p] <dir>...")
        for p in pos:
            self.fs.mkdir(p, parents="-p" in opts)

    def c_create(self, args):
        pos, opts = parse_opts(args, valued=("--size", "--content", "--key"), flags=("--ask-key",))
        if len(pos) != 1:
            raise FSError('uso: create <arq> [--size 10KB] [--content "texto"]')
        size = _size(opts["--size"]) if "--size" in opts else None
        content = _text([opts["--content"]]) if "--content" in opts else b""
        node = self.fs.create(pos[0], size=size, content=content, key=_file_key(opts))
        print(f"Arquivo '{node.path()}' criado: {node.size} bytes em "
              f"{len(node.blocks)} bloco(s) {node.blocks}")

    def c_cat(self, args):
        pos, opts = parse_opts(args, valued=("--key",), flags=("--ask-key",))
        if len(pos) != 1:
            raise FSError("uso: cat <arq> [--ask-key]")
        data = self.fs.read(pos[0], _file_key(opts))
        if b"\0" in data:
            print(f"[conteúdo binário/zerado: {len(data)} bytes]")
        else:
            print(data.decode("utf-8", errors="replace"))

    def c_write(self, args):
        self._write(args)

    def c_append(self, args):
        self._write(args, append=True)

    def c_resize(self, args):
        pos, opts = parse_opts(args, valued=("--key",), flags=("--ask-key",))
        if len(pos) != 2:
            raise FSError("uso: resize <arq> <tamanho> [--ask-key]")
        n = self.fs.resize(pos[0], _size(pos[1]), _file_key(opts))
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
    def c_write_file(self, args):
        self._write(args, sized=True)
        if self.fs.security.is_kernel:
            print(render_io(self.fs.disk.storage))

    def _write(self, args, append=False, sized=False):
        valued = ("--key", "--size") if sized else ("--key",)
        # Os comandos write/append aceitam texto literal que começa com hífen.
        if not sized and not any(a == "--ask-key" or a == "--key" or a.startswith("--key=") for a in args):
            pos, opts = args, {}
        else:
            pos, opts = parse_opts(args, valued=valued, flags=("--ask-key",))
        if not pos or (len(pos) < 2 and "--size" not in opts):
            raise FSError("informe o caminho e o texto ou use write_file <arq> --size 8KB")
        data = _text(pos[1:])
        if "--size" in opts:
            size = _size(opts["--size"])
            data = data[:size].ljust(size, b"\0")
        node = self.fs.write(pos[0], data, append=append, key=_file_key(opts))
        print(f"'{node.path()}' atualizado: {node.size} bytes, blocos {node.blocks}")

    def c_disk_config(self, args):
        pos, opts = parse_opts(args, valued=("--level", "--disks", "--algorithm",
                                            "--tracks", "--head", "--direction"))
        if len(pos) != 1:
            raise FSError("uso: disk_config <hdd|ssd|raid> [--level 5] [--disks 3] [--algorithm sstf]")
        try:
            self.fs.configure_storage(
                pos[0], raid_level=int(opts.get("--level", 5)), disks=int(opts.get("--disks", 3)),
                algorithm=opts.get("--algorithm", "sstf"), tracks=int(opts.get("--tracks", 200)),
                head=int(opts.get("--head", 53)), direction=opts.get("--direction", "up"))
        except ValueError as exc:
            raise FSError(str(exc))
        print("Dispositivo configurado; arquivos preservados.")
        print(render_storage(self.fs.disk.storage))

    def c_disk_schedule(self, args):
        pos, opts = parse_opts(args, valued=("--algorithm", "--head", "--tracks", "--direction"))
        try:
            tracks = [int(t) for part in pos for t in part.split(",")]
            result = schedule(tracks, int(opts.get("--head", 53)), opts.get("--algorithm", "sstf"),
                              int(opts.get("--tracks", 200)), opts.get("--direction", "up"))
        except ValueError as exc:
            raise FSError(str(exc))
        print(f"[E/S] Requisições nas trilhas: {tracks}")
        print(render_schedule(result))

    def c_io_status(self, args):
        self.fs.security.require_kernel("consulta da fila física")
        if args:
            raise FSError("uso: io_status")
        print(render_io(self.fs.disk.storage))

    def c_io_flush(self, args):
        self.fs.security.require_kernel("atendimento da fila física")
        if args:
            raise FSError("uso: io_flush")
        self.fs.disk.flush()
        print(render_io(self.fs.disk.storage))

    def c_storage_status(self, args):
        self.fs.security.require_kernel("consulta do armazenamento físico")
        if args:
            raise FSError("uso: storage_status")
        self.fs.disk.flush()
        print(render_storage(self.fs.disk.storage))

    def _disk_number(self, args):
        if len(args) != 1:
            raise FSError("informe o número do disco físico (a partir de 1)")
        try:
            return int(args[0])
        except ValueError:
            raise FSError("número de disco inválido")

    def c_fail_disk(self, args):
        number = self._disk_number(args)
        self.fs.raid_action("fail", number)
        print(f"Falha forçada no Disco {number}!")

    def c_rebuild_disk(self, args):
        number = self._disk_number(args)
        count = self.fs.raid_action("rebuild", number)
        print(f"Disco {number}: {count} blocos reconstruídos [OK]")

    def c_status(self, args):
        self.fs.security.require_kernel("relatório do disco")
        _, opts = parse_opts(args, flags=("--details", "-d"))
        print(report.render_status(self.fs, details=bool(opts), color=self.color))

    def c_compact(self, args):
        before = len(self.fs.disk.free_runs())
        moved = self.fs.compact()
        print(f"Compactação concluída: {moved} bloco(s) realocado(s). "
              f"Regiões livres: {before} -> "
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
        self.fs.format(bs, blocks, strategy)
        print(f"Disco formatado: {blocks} blocos de {format_size(bs)} "
              f"({format_size(bs * blocks)}), alocação {strategy.title}.")

    # ---- segurança ------------------------------------------------------
    def c_whoami(self, args):
        print(self.fs.security.identity())

    def c_login(self, args):
        pos, opts = parse_opts(args, valued=("--user", "--password"))
        if len(pos) + ("--user" in opts) != 1:
            raise FSError("uso: login --user <nome>")
        name = opts["--user"] if "--user" in opts else pos[0]
        self.fs.login(name, _secret(opts, "--password", "Senha: "))
        print(f"Autenticado com sucesso. {self.fs.security.identity()}")

    def c_logout(self, args):
        if args:
            raise FSError("uso: logout")
        self.fs.logout()
        print("Sessão encerrada.")

    def c_groupadd(self, args):
        if len(args) != 1:
            raise FSError("uso: groupadd <grupo>")
        self.fs.security.add_group(args[0])
        print(f"Grupo '{args[0]}' cadastrado.")

    def c_useradd(self, args):
        self.fs.security.require_kernel("cadastro de usuário")
        pos, opts = parse_opts(args, valued=("--group", "--password"))
        if len(pos) != 1:
            raise FSError("uso: useradd <nome> [--group grupo]")
        self.fs.security.add_user(pos[0], opts.get("--group", "users"),
                                  _secret(opts, "--password", "Nova senha: "))
        print(f"Usuário '{pos[0]}' cadastrado.")

    def c_usermod(self, args):
        pos, opts = parse_opts(args, valued=("--group",))
        if len(pos) != 1 or "--group" not in opts:
            raise FSError("uso: usermod <nome> --group <grupo>")
        self.fs.security.add_membership(pos[0], opts["--group"])
        print(f"Grupo '{opts['--group']}' adicionado a '{pos[0]}'.")

    def c_passwd(self, args):
        pos, opts = parse_opts(args, valued=("--password",))
        if len(pos) > 1:
            raise FSError("uso: passwd [usuário]")
        name = pos[0] if pos else self.fs.security.name
        if name != self.fs.security.name:
            self.fs.security.require_kernel("alteração da senha de outro usuário")
        self.fs.security.set_password(name, _secret(opts, "--password", "Nova senha: "))
        print("Senha atualizada.")

    def c_chmod(self, args):
        if len(args) != 2:
            raise FSError("uso: chmod <modo> <caminho>")
        print(describe_acl(self.fs.chmod(args[1], args[0])))

    def c_chown(self, args):
        if len(args) != 2:
            raise FSError("uso: chown <usuário[:grupo]> <caminho>")
        owner, _, group = args[0].partition(":")
        print(describe_acl(self.fs.chown(args[1], owner, group or None)))

    def c_exec_file(self, args):
        if len(args) != 1:
            raise FSError("uso: exec_file <arq>")
        node = self.fs.check_execute(args[0])
        print(f"Execução simulada autorizada para '{node.path()}'.")

    def _crypt_args(self, args):
        pos, opts = parse_opts(args, valued=("--key",))
        if len(pos) != 1:
            raise FSError("informe o caminho do arquivo")
        return pos[0], _secret(opts, "--key", "Chave: ")

    def c_encrypt_file(self, args):
        node, preview = self.fs.encrypt_file(*self._crypt_args(args))
        print(f"Arquivo '{node.path()}' criptografado com sucesso.")
        print(f"Conteúdo gravado nos blocos físicos: 0x{preview.hex().upper()}... (AES-GCM)")

    def c_decrypt_file(self, args):
        node = self.fs.decrypt_file(*self._crypt_args(args))
        print(f"Arquivo '{node.path()}' descriptografado.")

    def c_antivirus_scan(self, args):
        if len(args) > 1:
            raise FSError("uso: antivirus_scan [caminho]")
        print(render_scan(self.fs.antivirus_scan(args[0] if args else "/")))

    def c_audit_log(self, args):
        if args:
            raise FSError("uso: audit_log")
        print("\n".join(map(str, self.fs.security.events())) or "(sem eventos)")

    def c_corrupt_block(self, args):
        if not 1 <= len(args) <= 2:
            raise FSError("uso: corrupt_block <arq> [índice]")
        try:
            index = int(args[1]) if len(args) == 2 else 0
        except ValueError:
            raise FSError("índice de bloco inválido") from None
        block = self.fs.simulate_corruption(args[0], index)
        print(f"Alteração simulada no bloco {block}.")
