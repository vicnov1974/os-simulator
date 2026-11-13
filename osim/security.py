"""Identidades, controle de acesso, auditoria e criptografia do simulador."""
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecurityError(Exception):
    """Falha de autenticação, autorização ou proteção dos dados."""


class IntegrityError(SecurityError):
    """Os bytes físicos divergem do registro de integridade."""


_ITERATIONS = 600_000
TAG_BYTES = 16
_RIGHTS = {"r": 4, "w": 2, "x": 1}
_ACTIONS = {"r": "LEITURA", "w": "ESCRITA", "x": "EXECUÇÃO", "wx": "ESCRITA E EXECUÇÃO",
            "rx": "LISTAGEM", "rwx": "REMOÇÃO RECURSIVA"}


def _derive(secret, salt):
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _ITERATIONS)


@dataclass(frozen=True)
class Password:
    salt: bytes
    digest: bytes = field(repr=False)

    @classmethod
    def create(cls, value):
        if not value:
            raise SecurityError("a senha não pode ser vazia")
        salt = secrets.token_bytes(16)
        return cls(salt, _derive(value, salt))

    def verify(self, value):
        return hmac.compare_digest(self.digest, _derive(value, self.salt))


@dataclass
class User:
    name: str
    primary_group: str
    password: Password = field(default=None, repr=False)
    groups: set = field(default_factory=set)


@dataclass(frozen=True)
class AuditEvent:
    timestamp: datetime
    user: str
    action: str
    target: str
    result: str

    def __str__(self):
        fields = (self.timestamp.isoformat(timespec="seconds"), self.user, self.action,
                  self.target, self.result)
        return " | ".join(field.replace("\n", "\\n").replace("\r", "\\r") for field in fields)


def parse_mode(text):
    if re.fullmatch(r"0?[0-7]{3}", text):
        return int(text, 8)
    if re.fullmatch(r"[r-][w-][x-]" * 3, text):
        return sum(_RIGHTS[c] << (6 - 3 * (i // 3)) for i, c in enumerate(text) if c != "-")
    raise SecurityError("permissões inválidas (use 750 ou rwxr-x---)")


def mode_text(mode):
    return "".join(bit if mode & (value << shift) else "-"
                   for shift in (6, 3, 0) for bit, value in _RIGHTS.items())


def describe_acl(node):
    return (f"Objeto      : {node.path()}\n"
            f"Proprietário: {node.owner} (Grupo: {node.group})\n"
            f"Permissões  : {mode_text(node.mode)} ({node.mode:03o})")


class Security:
    def __init__(self):
        self.groups = {"admin", "users"}
        self.users = {"root": User("root", "admin", groups={"admin"})}
        self._current = self.users["root"]  # sessão de configuração de um disco novo
        self._audit = []

    @property
    def current(self):
        return self._current

    @property
    def name(self):
        return self.current.name if self.current else "sem sessão"

    @property
    def is_kernel(self):
        return self.name == "root"

    @property
    def mode(self):
        return "MODO_KERNEL" if self.is_kernel else "MODO_USUARIO"

    def identity(self):
        group = self.current.primary_group if self.current else "-"
        return f"Usuário: '{self.name}' | Grupo: '{group}' | Modo: {self.mode}"

    def record(self, action, target, result):
        self._audit.append(AuditEvent(datetime.now(timezone.utc), self.name, action, target, result))

    def events(self):
        self.require_kernel("consulta dos logs de auditoria")
        return tuple(self._audit)

    def require_session(self):
        if self.current is None:
            self.record("acesso ao sistema", "/", "NEGADO")
            raise SecurityError("autentique-se com login")

    def require_kernel(self, action):
        if not self.is_kernel:
            self.record(action, "sistema", "NEGADO")
            raise SecurityError(f"[NEGADO] {action} exige MODO_KERNEL (root). "
                                "Tentativa registrada nos logs do sistema.")

    @staticmethod
    def _check_name(name):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name):
            raise SecurityError("nome inválido (use letras minúsculas, números, _ ou -)")

    def add_group(self, name):
        self.require_kernel("cadastro de grupo")
        self._check_name(name)
        if name in self.groups:
            raise SecurityError(f"grupo '{name}' já existe")
        self.groups.add(name)
        self.record("cadastro de grupo", name, "OK")

    def add_user(self, name, group, password):
        self.require_kernel("cadastro de usuário")
        self._check_name(name)
        if name in self.users:
            raise SecurityError(f"usuário '{name}' já existe")
        if group not in self.groups:
            raise SecurityError(f"grupo '{group}' inexistente")
        self.users[name] = User(name, group, Password.create(password), {group})
        self.record("cadastro de usuário", name, "OK")

    def add_membership(self, name, group):
        self.require_kernel("alteração de grupo")
        if name not in self.users or group not in self.groups:
            raise SecurityError("usuário ou grupo inexistente")
        self.users[name].groups.add(group)
        self.record("alteração de grupo", f"{name}:{group}", "OK")

    def set_password(self, name, password):
        self.require_session()
        if name != self.name:
            self.require_kernel("alteração da senha de outro usuário")
        if name not in self.users:
            raise SecurityError(f"usuário '{name}' inexistente")
        self.users[name].password = Password.create(password)
        self.record("alteração de senha", name, "OK")

    def login(self, name, password):
        user = self.users.get(name)
        if user is None or user.password is None or not user.password.verify(password):
            self.record("login", name, "NEGADO")
            raise SecurityError("usuário ou senha inválidos")
        self._current = user
        self.record("login", name, "OK")

    def logout(self):
        if self.current:
            self.record("logout", self.name, "OK")
            self._current = None

    def allows(self, node, rights):
        if self.is_kernel:
            return True
        if self.current is None:
            return False
        shift = 0
        if self.name == node.owner:
            shift = 6
        elif node.group in self.current.groups:
            shift = 3
        mask = sum(_RIGHTS[right] for right in rights)
        return (node.mode >> shift) & mask == mask

    def require(self, node, rights):
        if not self.allows(node, rights):
            action = _ACTIONS[rights]
            group = self.current.primary_group if self.current else "-"
            self.record(action, node.path(), "NEGADO")
            raise SecurityError(
                "[SISTEMA DE SEGURANÇA - VERIFICAÇÃO DE ACL]\n"
                f"{describe_acl(node)}\n"
                f"Ação Pedida : {action} por '{self.name}' (Grupo: {group})\n"
                f"[NEGADO]: O usuário '{self.name}' não possui a permissão solicitada.\n"
                "Tentativa não autorizada registrada nos logs do sistema."
            )

    def require_owner(self, node):
        if not self.is_kernel and (self.current is None or self.name != node.owner):
            self.record("chmod", node.path(), "NEGADO")
            raise SecurityError("[NEGADO] chmod exige o proprietário ou root. Tentativa registrada nos logs.")


@dataclass(frozen=True)
class Encryption:
    salt: bytes
    nonce: bytes


def encrypt(data, password, key=None, salt=None):
    if not password and key is None:
        raise SecurityError("a chave de criptografia não pode ser vazia")
    seal = Encryption(secrets.token_bytes(16) if salt is None else salt, secrets.token_bytes(12))
    key = _derive(password, seal.salt) if key is None else key
    return AESGCM(key).encrypt(seal.nonce, data, None), seal


def decrypt(data, seal, password):
    if not password:
        raise SecurityError("arquivo criptografado: informe a chave")
    key = _derive(password, seal.salt)
    try:
        return AESGCM(key).decrypt(seal.nonce, data, None), key
    except InvalidTag:
        raise SecurityError("chave inválida ou conteúdo criptografado adulterado") from None


SIGNATURES = (b"OSIM_TEST_MALWARE", b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE")


def render_scan(findings):
    alerts = sum(status.startswith("[ALERTA]") for _, status in findings)
    lines = ["[MÓDULO DE SEGURANÇA - VARREDURA DE INTEGRIDADE]"]
    lines.extend(f"- {path}: {status}" for path, status in findings)
    lines.append(f"Resultado: {len(findings)} objeto(s) reportado(s), {alerts} alerta(s).")
    return "\n".join(lines)
