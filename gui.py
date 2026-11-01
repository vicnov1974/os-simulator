#!/usr/bin/env python3
"""Interface gráfica do simulador (estilo Windows Explorer). Uso: python gui.py"""
import argparse

from osim.allocation import STRATEGIES, make_strategy
from osim.disk import Disk
from osim.filesystem import FileSystem
from osim.gui import run
from osim.utils import parse_size


def main():
    ap = argparse.ArgumentParser(description="Simulador de SO - interface gráfica")
    ap.add_argument("--block-size", default="4KB", help="tamanho do bloco (padrão: 4KB)")
    ap.add_argument("--blocks", type=int, default=64, help="total de blocos (padrão: 64)")
    ap.add_argument("--alloc", default="indexed", choices=list(STRATEGIES),
                    help="estratégia inicial (padrão: indexed)")
    a = ap.parse_args()
    run(FileSystem(Disk(parse_size(a.block_size), a.blocks), make_strategy(a.alloc)))


if __name__ == "__main__":
    main()
