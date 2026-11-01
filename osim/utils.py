"""Funções utilitárias: conversão e formatação de tamanhos."""
import re

_UNITS = {
    "B": 1,
    "K": 1024, "KB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3,
}


def parse_size(text: str) -> int:
    """Converte '10KB', '4096', '1.5MB' em bytes. Lança ValueError se inválido."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", str(text))
    if not m:
        raise ValueError(f"tamanho inválido: '{text}'")
    number, unit = m.groups()
    unit = unit.upper() or "B"
    if unit not in _UNITS:
        raise ValueError(f"unidade desconhecida: '{unit}' (use B, KB, MB ou GB)")
    return int(float(number) * _UNITS[unit])


def format_size(n: int) -> str:
    """Formata bytes de forma legível: 10240 -> '10 KB'."""
    if n < 1024:
        return f"{n} B"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            value = f"{n / div:.2f}".rstrip("0").rstrip(".")
            return f"{value} {unit}"
    return f"{n} B"
