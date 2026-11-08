"""Políticas de escalonamento (Strategy Pattern), sem dependência do filesystem."""
from dataclasses import dataclass

from .disk import DiskError


@dataclass
class Schedule:
    algorithm: str
    order: list                 # índices das requisições, preserva duplicatas
    path: list                  # inclui extremos e retorno circular

    @property
    def movements(self):
        return [abs(b - a) for a, b in zip(self.path, self.path[1:])]

    @property
    def total(self):
        return sum(self.movements)


class FCFSScheduler:
    key = "fcfs"

    def plan(self, tracks, head, maximum, direction):
        return Schedule(self.key, list(range(len(tracks))), [head] + list(tracks))


class SSTFScheduler:
    key = "sstf"

    def plan(self, tracks, head, maximum, direction):
        pending, order, path = list(range(len(tracks))), [], [head]
        while pending:
            # Empate: mantém a ordem de chegada.
            i = min(pending, key=lambda i: (abs(tracks[i] - path[-1]), i))
            pending.remove(i)
            order.append(i)
            path.append(tracks[i])
        return Schedule(self.key, order, path)


class SCANScheduler:
    key = "scan"
    circular = False

    def plan(self, tracks, head, maximum, direction):
        up = direction == "up"
        same = [i for i, t in enumerate(tracks) if t == head]
        ahead = [i for i, t in enumerate(tracks) if (t > head if up else t < head)]
        ahead.sort(key=lambda i: tracks[i], reverse=not up)
        behind = [i for i, t in enumerate(tracks) if t < head] if up else [
            i for i, t in enumerate(tracks) if t > head]
        behind.sort(key=lambda i: tracks[i], reverse=(up != self.circular))
        order = same + ahead + behind
        path = [head] + [tracks[i] for i in same + ahead]
        if behind:
            edge = maximum if up else 0
            if path[-1] != edge:
                path.append(edge)
            if self.circular:
                path.append(0 if up else maximum)
            path.extend(tracks[i] for i in behind)
        return Schedule(self.key, order, path)


class CSCANScheduler(SCANScheduler):
    key = "c-scan"
    circular = True


SCHEDULERS = {c.key: c for c in (FCFSScheduler, SSTFScheduler, SCANScheduler, CSCANScheduler)}


def schedule(tracks, head=53, algorithm="sstf", track_count=200, direction="up"):
    tracks = list(tracks)
    if algorithm.lower() not in SCHEDULERS:
        raise DiskError("algoritmo inválido: use fcfs, sstf, scan ou c-scan")
    if direction not in ("up", "down"):
        raise DiskError("direção inválida: use up ou down")
    if track_count <= 0 or not 0 <= head < track_count or any(
            not 0 <= t < track_count for t in tracks):
        raise DiskError(f"cabeça e trilhas devem estar entre 0 e {track_count - 1}")
    return SCHEDULERS[algorithm.lower()]().plan(tracks, head, track_count - 1, direction)


def render_schedule(result):
    steps = result.movements
    return (f"--- ESCALONAMENTO DE DISCO: {result.algorithm.upper()} ---\n"
            f"Ordem de Atendimento : {' -> '.join(map(str, result.path))}\n"
            f"Deslocamento da Cabeça: {' + '.join(f'|{a}-{b}|' for a, b in zip(result.path, result.path[1:])) or '0'}\n"
            f"Total de Movimento  : {' + '.join(map(str, steps)) or '0'} = {result.total} trilhas")
