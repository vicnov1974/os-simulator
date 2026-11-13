#!/usr/bin/env python3
"""Simulador de SO - Sistema de Arquivos e Gerenciamento de Armazenamento."""
import argparse
import os
import sys

from osim.allocation import STRATEGIES, make_strategy
from osim.disk import Disk, DiskError
from osim.filesystem import FileSystem
from osim.shell import Shell, has_inline_secret
from osim.utils import parse_size
from osim.storage import add_storage_arguments, storage_from_args

BANNER = "Simulador de SO - Etapas 1 a 3: Arquivos, Armazenamento e Segurança  (digite 'help')"


def main() -> int:
    ap = argparse.ArgumentParser(description=BANNER)
    ap.add_argument("--block-size", default="4KB", help="tamanho do bloco (padrão: 4KB)")
    ap.add_argument("--blocks", type=int, default=16, help="total de blocos (padrão: 16)")
    ap.add_argument("--alloc", default="indexed", choices=list(STRATEGIES),
                    help="estratégia de alocação inicial (padrão: indexed)")
    ap.add_argument("--script", metavar="ARQUIVO", help="executa comandos de um arquivo")
    ap.add_argument("--no-color", action="store_true", help="desativa cores ANSI")
    add_storage_arguments(ap)
    args = ap.parse_args()

    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    try:
        bs = parse_size(args.block_size)
        fs = FileSystem(Disk(bs, args.blocks, storage_from_args(args, bs)), make_strategy(args.alloc))
    except (ValueError, DiskError) as exc:
        ap.error(str(exc))
    shell = Shell(fs, color=color)

    if args.script:                      # modo script: ecoa cada comando
        with open(args.script, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    print(line)
                    continue
                shown = "[comando com credencial omitido]" if has_inline_secret(line) else line
                print(f"{shell.prompt}{shown}")
                if not shell.execute(line):
                    break
                print()
        shell.fs.disk.flush()
        return 0

    try:
        import readline
    except ImportError:
        readline = None
    print(BANNER)
    while True:
        try:
            line = input(shell.prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if readline and has_inline_secret(line) and readline.get_current_history_length():
            last = readline.get_current_history_length()
            if readline.get_history_item(last) == line:
                readline.remove_history_item(last - 1)
        if not shell.execute(line):
            break
    shell.fs.disk.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
