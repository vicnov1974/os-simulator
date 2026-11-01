#!/usr/bin/env python3
"""Simulador de Sistema Operacional Modular - Etapa 1 (Sistema de Arquivos e Disco)."""
import argparse
import os
import sys

from osim.allocation import STRATEGIES, make_strategy
from osim.disk import Disk
from osim.filesystem import FileSystem
from osim.shell import Shell
from osim.utils import parse_size

BANNER = "Simulador de SO - Etapa 1: Sistema de Arquivos e Disco  (digite 'help')"


def main() -> int:
    ap = argparse.ArgumentParser(description=BANNER)
    ap.add_argument("--block-size", default="4KB", help="tamanho do bloco (padrão: 4KB)")
    ap.add_argument("--blocks", type=int, default=16, help="total de blocos (padrão: 16)")
    ap.add_argument("--alloc", default="indexed", choices=list(STRATEGIES),
                    help="estratégia de alocação inicial (padrão: indexed)")
    ap.add_argument("--script", metavar="ARQUIVO", help="executa comandos de um arquivo")
    ap.add_argument("--no-color", action="store_true", help="desativa cores ANSI")
    args = ap.parse_args()

    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    fs = FileSystem(Disk(parse_size(args.block_size), args.blocks), make_strategy(args.alloc))
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
                print(f"{shell.prompt}{line}")
                if not shell.execute(line):
                    break
                print()
        return 0

    try:
        import readline  # noqa: F401  (histórico/setas no Linux e macOS)
    except ImportError:
        pass
    print(BANNER)
    while True:
        try:
            line = input(shell.prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not shell.execute(line):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
