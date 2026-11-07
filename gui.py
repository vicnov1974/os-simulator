#!/usr/bin/env python3
"""Interface gráfica do simulador (estilo Windows Explorer). Uso: python gui.py"""
import argparse

from osim.allocation import STRATEGIES, make_strategy
from osim.disk import Disk, DiskError
from osim.filesystem import FileSystem
from osim.gui import run
from osim.utils import parse_size
from osim.storage import add_storage_arguments, storage_from_args


def main():
    ap = argparse.ArgumentParser(description="Simulador de SO - interface gráfica")
    ap.add_argument("--block-size", default="4KB", help="tamanho do bloco (padrão: 4KB)")
    ap.add_argument("--blocks", type=int, default=64, help="total de blocos (padrão: 64)")
    ap.add_argument("--alloc", default="indexed", choices=list(STRATEGIES),
                    help="estratégia inicial (padrão: indexed)")
    add_storage_arguments(ap)
    a = ap.parse_args()
    try:
        bs = parse_size(a.block_size)
        fs = FileSystem(Disk(bs, a.blocks, storage_from_args(a, bs)), make_strategy(a.alloc))
    except (ValueError, DiskError) as exc:
        ap.error(str(exc))
    run(fs)


if __name__ == "__main__":
    main()
