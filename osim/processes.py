"""CPU lógica: prioridades preemptivas, RR, syscalls e threads com mutex FIFO.

Uma instrução custa 1 ms. Syscalls usam CPU acumulada do processo; ações de
mutex usam CPU acumulada da thread (antes da próxima instrução).
"""
from collections import deque
from dataclasses import dataclass, field
import random
import re

from .memory import MemoryError, render_translation


def milliseconds(value, allow_zero=False):
    match = re.fullmatch(r"(\d+)(?:ms)?", str(value).strip())
    if not match or int(match[1]) < (0 if allow_zero else 1):
        raise MemoryError("tempo deve ser inteiro em ms " + ("não negativo" if allow_zero else "positivo"))
    return int(match[1])


@dataclass(frozen=True)
class Syscall:
    at: int
    duration: int
    operation: str = "read_disk"


def parse_syscalls(profile, burst, seed=0):
    if not profile or profile == "none":
        return []
    if profile == "random":
        if burst < 2:
            raise MemoryError("perfil random requer burst de pelo menos 2ms")
        rng = random.Random(seed)
        return [Syscall(rng.randrange(1, burst), rng.randint(1, 4))]
    result = []
    for spec in profile.split(";"):
        match = re.fullmatch(r"\s*(read_disk|write_disk|io)\s+at\s+(\d+(?:ms)?)\s*"
                             r"\(duration:\s*(\d+(?:ms)?)\)\s*", spec)
        if not match:
            raise MemoryError('syscall inválida: use "read_disk at 3ms (duration: 4ms)" ou random')
        operation, at, duration = match.groups()
        result.append(Syscall(milliseconds(at), milliseconds(duration), operation))
    if any(s.at >= burst for s in result) or len({s.at for s in result}) != len(result):
        raise MemoryError("syscalls devem ter instantes distintos, positivos e menores que o burst")
    return sorted(result, key=lambda s: s.at)


@dataclass
class Thread:
    name: str
    burst: int
    executed: int = 0
    state: str = "PRONTO"
    waiting_mutex: str = None
    actions: dict = field(default_factory=dict)


@dataclass
class ScheduledProcess:
    memory: object
    burst: int
    priority: int
    arrival: int
    syscalls: list
    seed: int
    state: str = "NOVO"
    executed: int = 0
    waiting: int = 0
    blocked: int = 0
    first_run: int = None
    completion: int = None
    io_until: int = None
    threads: dict = field(default_factory=dict)
    cancelled: bool = False
    rng: object = None


@dataclass
class Mutex:
    owner: tuple = None
    waiters: deque = field(default_factory=deque)


class ProcessSimulator:
    def __init__(self, memory):
        self.memory = memory
        self.processes = {}
        self.history = []  # métricas preservadas após liberar memória/reutilizar nomes
        self.ready = {}  # prioridade -> deque de nomes de processos
        self.mutexes = {}
        self.clock = self.busy = self.context_switches = 0
        self.quantum = 2
        self.current = None  # (nome do processo, nome da thread)
        self.slice_used = 0
        self.previous = None
        self.events = []
        self.timeline = []
        self.accesses = {}  # moldura -> (PID, página, resultado, instante CPU, referência)

    def _event(self, text):
        self.events.append(f"[t={self.clock}ms] {text}")

    def _state(self, process, state):
        if process.state != state:
            self._event(f"PID {process.memory.pid}: {process.state} -> {state}")
            process.state = state

    def create(self, name, burst, priority=1, size=None, kind="normal", profile=None, seed=0):
        burst = milliseconds(burst)
        try:
            priority, seed = int(priority), int(seed)
        except (TypeError, ValueError):
            raise MemoryError("prioridade e seed devem ser inteiros") from None
        if priority < 1:
            raise MemoryError("prioridade deve ser positiva (1 = mais alta)")
        syscalls = parse_syscalls(profile, burst, seed)
        allocation = self.memory.create(name, self.memory.page_size * 4 if size is None else size, kind)
        process = ScheduledProcess(allocation, burst, priority, self.clock, syscalls, seed)
        process.rng = random.Random(seed)
        process.threads["T1"] = Thread("T1", burst)
        self.processes[name] = process
        self.history.append(process)
        self._state(process, "PRONTO")
        self._enqueue(name)
        return process

    def _process(self, name):
        self.memory.process(name)  # autorização e existência são as da Etapa 4
        if name not in self.processes or self.processes[name].cancelled:
            raise MemoryError("processo sem perfil de CPU; crie com --burst")
        return self.processes[name]

    def create_thread(self, name, thread, burst):
        process = self._process(name)
        burst = milliseconds(burst)
        if process.executed or process.state == "CONCLUÍDO":
            raise MemoryError("configure threads antes da execução do processo")
        if not thread or len(thread) > 128 or any(ord(c) < 32 for c in thread) or thread in process.threads:
            raise MemoryError("nome de thread inválido ou duplicado")
        process.threads[thread] = Thread(thread, burst)
        process.burst += burst

    def thread_action(self, name, thread, at, operation, mutex):
        process = self._process(name)
        at = milliseconds(at, allow_zero=True)
        if process.executed or thread not in process.threads:
            raise MemoryError("configure ações de threads existentes antes da execução")
        task = process.threads[thread]
        if at >= task.burst or at in task.actions or operation not in ("lock", "unlock"):
            raise MemoryError("ação inválida: instante único menor que burst; operação lock/unlock")
        if not mutex or len(mutex) > 128 or any(ord(c) < 32 for c in mutex):
            raise MemoryError("nome de mutex inválido")
        task.actions[at] = (operation, mutex)

    def configure(self, quantum=2, algorithm="Priority_RR"):
        self.memory.security.require_kernel("configuração do escalonador")
        if algorithm.lower() != "priority_rr":
            raise MemoryError("algoritmo inválido: use Priority_RR")
        quantum = milliseconds(quantum)
        if quantum != self.quantum and self.current:
            self._yield_current()
        self.quantum = quantum

    def _enqueue(self, name):
        process = self.processes[name]
        queue = self.ready.setdefault(process.priority, deque())
        if process.state == "PRONTO" and name not in queue:
            queue.append(name)

    def _drop(self, name):
        for queue in self.ready.values():
            if name in queue:
                queue.remove(name)

    def _sync(self, name):
        process = self.processes[name]
        if process.cancelled:
            return
        live = [t for t in process.threads.values() if t.state != "CONCLUÍDO"]
        if not live:
            self._state(process, "CONCLUÍDO")
            process.completion = self.clock
        elif process.io_until is not None or all(t.state == "BLOQUEADO" for t in live):
            self._state(process, "BLOQUEADO")
        elif self.current and self.current[0] == name:
            self._state(process, "EXECUTANDO")
        else:
            self._state(process, "PRONTO")
            self._enqueue(name)
        if process.state != "PRONTO":
            self._drop(name)

    def _yield_current(self):
        if self.current:
            name, thread = self.current
            task = self.processes[name].threads[thread]
            if task.state == "EXECUTANDO":
                task.state = "PRONTO"
                # Move a thread para o final do RR interno ao processo.
                tasks = self.processes[name].threads
                tasks[thread] = tasks.pop(thread)
            self.current = None
            self._sync(name)
        self.slice_used = 0

    def _unlock(self, identity, mutex_name):
        mutex = self.mutexes[mutex_name]
        if mutex.owner != identity:
            raise MemoryError(f"Mutex {mutex_name}: unlock por thread que não é dona")
        mutex.owner = None
        self._event(f"Thread {identity} libera Mutex {mutex_name}")
        if mutex.waiters:
            name, thread = mutex.waiters.popleft()
            mutex.owner = (name, thread)
            task = self.processes[name].threads[thread]
            task.state, task.waiting_mutex = "PRONTO", None
            self._event(f"Thread {(name, thread)}: BLOQUEADO -> PRONTO; adquiriu Mutex {mutex_name}")
            self._sync(name)

    def _release_owned(self, identity):
        for name, mutex in self.mutexes.items():
            if mutex.owner == identity:
                self._unlock(identity, name)

    def cancel(self, name):
        if name not in self.processes:
            return
        process = self.processes[name]
        if process.completion is not None:
            return
        process.cancelled = True
        self._drop(name)
        if self.current and self.current[0] == name:
            self.current, self.slice_used = None, 0
        for mutex in self.mutexes.values():
            mutex.waiters = deque(w for w in mutex.waiters if w[0] != name)
        for thread in process.threads:
            self._release_owned((name, thread))
        for task in process.threads.values():
            task.state = "CONCLUÍDO"
        self._state(process, "CONCLUÍDO")
        self._event(f"PID {process.memory.pid}: cancelado; recursos liberados")

    def _wake(self):
        for name, process in self.processes.items():
            if not process.cancelled and process.io_until is not None and process.io_until <= self.clock:
                process.io_until = None
                self._event(f"PID {process.memory.pid}: E/S concluída")
                self._sync(name)

    def _select(self):
        priorities = [p for p, q in self.ready.items() if q]
        if self.current and priorities and min(priorities) < self.processes[self.current[0]].priority:
            self._event("Preempção por prioridade mais alta")
            self._yield_current()
        if not self.current:
            priorities = [p for p, q in self.ready.items() if q]
            if not priorities:
                return
            name = self.ready[min(priorities)].popleft()
            process = self.processes[name]
            task = next(t for t in process.threads.values() if t.state == "PRONTO")
            self.current, self.slice_used = (name, task.name), 0
            task.state = "EXECUTANDO"
            self._state(process, "EXECUTANDO")

    def _action(self):
        name, thread = self.current
        task = self.processes[name].threads[thread]
        action = task.actions.get(task.executed)
        if action is None:
            return True
        operation, mutex_name = action
        mutex = self.mutexes.setdefault(mutex_name, Mutex())
        if operation == "unlock":
            self._unlock(self.current, mutex_name)
        elif mutex.owner is None:
            mutex.owner = self.current
            self._event(f"Thread {self.current} adquire Mutex {mutex_name}")
        elif mutex.owner == self.current:
            raise MemoryError(f"Mutex {mutex_name} não é recursivo")
        else:
            mutex.waiters.append(self.current)
            task.state, task.waiting_mutex = "BLOQUEADO", mutex_name
            self._event(f"Thread {self.current}: EXECUTANDO -> BLOQUEADO (Mutex {mutex_name})")
            del task.actions[task.executed]
            self._yield_current()
            return False
        del task.actions[task.executed]
        return True

    def step(self):
        self.memory.security.require_kernel("execução do escalonador global")
        self._wake()
        while True:
            self._select()
            if self.current is None:
                active = [p for p in self.processes.values() if not p.cancelled and p.state != "CONCLUÍDO"]
                if not active:
                    return False
                if not any(p.io_until is not None for p in active):
                    self._event("DEADLOCK: threads aguardam Mutex; simulação pausada")
                    return False
                self._account(None)
                self._event("CPU OCIOSA")
                self.timeline.append((self.clock, self.clock + 1, None))
                self.clock += 1
                self._wake()
                return True
            if self._action():
                break
        name, thread = self.current
        process = self.processes[name]
        task = process.threads[thread]
        # Falhas no swap não consomem CPU, quantum ou referência aleatória.
        rng_state = process.rng.getstate()
        try:
            if process.memory.kind == "normal":
                page = process.rng.randrange(len(process.memory.pages))
                translation = self.memory.translate(name, page)
                self.accesses[translation.frame] = (process.memory.pid, page,
                                                     "PAGE FAULT (NEW)" if translation.fault else "HIT",
                                                     self.clock + 1, self.memory.clock)
                self._event(render_translation(translation))
            else:
                self._event(f"PID {process.memory.pid}: acesso contíguo na RAM reservada")
        except Exception:
            process.rng.setstate(rng_state)
            raise
        identity = (process.memory.pid, thread)
        if self.previous is not None and self.previous != identity:
            self.context_switches += 1
        self.previous = identity
        if process.first_run is None:
            process.first_run = self.clock
        self.timeline.append((self.clock, self.clock + 1, self.current))
        self._event(f"CPU PID {process.memory.pid} / {thread}: [{self.clock}, {self.clock + 1})ms")
        self._account(name)
        self.clock += 1
        self.busy += 1
        self.slice_used += 1
        process.executed += 1
        task.executed += 1
        if task.executed == task.burst:
            task.state = "CONCLUÍDO"
            self._release_owned(self.current)
            self._yield_current()
        if process.executed < process.burst:
            syscall = next((s for s in process.syscalls if s.at == process.executed), None)
            if syscall:
                process.io_until = self.clock + syscall.duration
                self._event(f"SYSCALL {syscall.operation}: PID {process.memory.pid}; desbloqueio em t={process.io_until}ms")
                if self.current and self.current[0] == name:
                    self._yield_current()
                self._sync(name)
        if self.current and self.slice_used >= self.quantum:
            self._event(f"Quantum {self.quantum}ms expirou")
            self._yield_current()
        self._wake()
        return True

    def _account(self, running):
        for name, process in self.processes.items():
            if process.cancelled:
                continue
            if process.state == "PRONTO" and name != running:
                process.waiting += 1
            elif process.state == "BLOQUEADO":
                process.blocked += 1

    def run(self, steps=None, process=None):
        self.memory.security.require_kernel("execução do escalonador global")
        target = self._process(process) if process else None
        limit = milliseconds(steps) if steps is not None else 100000
        start = len(self.events)
        for _ in range(limit):
            if target and target.state == "CONCLUÍDO":
                break
            if not self.step():
                break
        else:
            if steps is None:
                self._event("Limite de 100000ms atingido; continue com run_simulation")
        return "\n".join(self.events[start:]) or "Nenhum processo executável."

    def render_ram(self):
        self.memory.security.require_kernel("resumo da RAM dinâmica")
        lines = ["[RESUMO DA GERÊNCIA DE MEMÓRIA (RAM DINÂMICA)]",
                 "Moldura | Processo/Pág | Acesso | Último acesso LRU (referência) | CPU (ms)"]
        for frame, (process, index, _) in sorted(self.memory.frames.items()):
            physical = frame + self.memory.reserved_size // self.memory.page_size
            access = self.accesses.get(physical)
            valid = (access and access[:2] == (process.pid, index)
                     and access[4] == process.pages[index].last_access)
            lines.append(f"{physical} | PID {process.pid} / P{index} | {access[2] if valid else 'manual'} | "
                         f"{process.pages[index].last_access} | {access[3] if valid else '-'}")
        return "\n".join(lines)

    def metrics(self):
        self.memory.security.require_kernel("métricas globais do sistema")
        completed = [p for p in self.history if p.completion is not None and not p.cancelled]
        return dict(completed=len(completed),
                    average_waiting=sum(p.waiting for p in completed) / len(completed) if completed else 0,
                    average_turnaround=sum(p.completion - p.arrival for p in completed) / len(completed) if completed else 0,
                    context_switches=self.context_switches,
                    utilization=100 * self.busy / self.clock if self.clock else 0,
                    throughput=len(completed) / self.clock if self.clock else 0)

    def render(self):
        self.memory.security.require_kernel("relatório do escalonador")
        lines = [f"[ESCALONADOR Priority_RR | t={self.clock}ms | Quantum={self.quantum}ms]",
                 "PID | Nome | Prioridade | Estado | Chegada | CPU/Burst | WT | Bloqueio | TAT | Resposta"]
        for process in self.history:
            tat = process.completion - process.arrival if process.completion is not None else "-"
            response = process.first_run - process.arrival if process.first_run is not None else "-"
            lines.append(f"{process.memory.pid} | {process.memory.name} | {process.priority} | "
                         f"{'CANCELADO' if process.cancelled else process.state} | {process.arrival} | "
                         f"{process.executed}/{process.burst} | {process.waiting} | {process.blocked} | {tat} | {response}")
            for task in process.threads.values():
                lines.append(f"  {task.name}: {task.state} CPU={task.executed}/{task.burst} "
                             f"{('Mutex=' + task.waiting_mutex) if task.waiting_mutex else ''}")
        for priority, queue in sorted(self.ready.items()):
            lines.append(f"Fila P{priority}: {list(queue)}")
        stats = self.metrics()
        lines += [f"Tempo Médio de Espera (AWT): {stats['average_waiting']:.2f} ms",
                  f"Tempo Médio de Retorno (ATT): {stats['average_turnaround']:.2f} ms",
                  f"Trocas de Contexto Totais: {self.context_switches}",
                  f"Utilização Global da CPU: {stats['utilization']:.2f}%",
                  f"Vazão: {stats['throughput']:.4f} processos/ms | Concluídos: {stats['completed']}",
                  "Médias: somente concluídos; WT: tempo em PRONTO; TAT inclui E/S.",
                  "Trocas: mudança de processo/thread entre instruções; despacho inicial e ociosidade excluídos."]
        return "\n".join(lines)
