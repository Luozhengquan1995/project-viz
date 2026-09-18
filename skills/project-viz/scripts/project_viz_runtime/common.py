"""Portable paths, private state and public text handling."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import uuid


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_path(value):
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


_SECRET_KEY = re.compile(r"^(?:password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key|authorization)$", re.I)
_INJECTED = re.compile(r"<(?:environment_context|recommended_plugins|skills_instructions|permissions_instructions|system_reminder)\b[^>]*>[\s\S]*?</(?:environment_context|recommended_plugins|skills_instructions|permissions_instructions|system_reminder)>", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.match(str(k)) else redact(v)
                for k, v in value.items() if k != "encrypted_content"}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = _INJECTED.sub("", value)
    value = re.sub(r"-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\n]*PRIVATE KEY-----|$)", "[REDACTED PRIVATE KEY]", value)
    value = re.sub(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", "[REDACTED KEY]", value)
    value = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", value)
    value = re.sub(r'''(?i)(["']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''', r'\1"[REDACTED]"', value)
    value = re.sub(r'''(?i)(\bsshpass\s+-p\s*)(?:'[^']*'|"[^"]*"|[^\s;]+)''', r"\1[REDACTED]", value)
    value = re.sub(r"(https?://[^\s/:@]+:)[^\s/@]+(@)", r"\1[REDACTED]\2", value)
    return value


def public_text(value, limit=12000):
    value = redact(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return value if len(value) <= limit else value[:limit] + "\n[Display excerpt; source contains more.]"


def state_base():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ProjectViz"
    if __import__("sys").platform == "darwin":
        return Path.home() / "Library/Application Support/ProjectViz"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "project-viz"


@contextmanager
def state_lock(state_dir, name, timeout=10):
    """A portable, crash-released process lease, separate from the event DB."""
    if not re.fullmatch(r"[a-z-]+", name):
        raise ValueError("Invalid lock name")
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path / ("." + name + "-lock.sqlite"), timeout=timeout, isolation_level=None)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Project Viz " + name + " is already busy") from exc
        yield
    finally:
        connection.close()


def context(project=None, state_dir=None, codex_home=None, create=False):
    project = Path(project or Path.cwd()).expanduser().resolve()
    state = Path(state_dir).expanduser().resolve() if state_dir else state_base() / "projects" / hashlib.sha256(canonical_path(project).encode()).hexdigest()[:24]
    if create:
        if not project.is_dir():
            raise ValueError("Project must be an existing directory")
        with state_lock(state, "setup"):
            return _context(project, state, codex_home, create)
    return _context(project, state, codex_home, create)


def _context(project=None, state_dir=None, codex_home=None, create=False):
    project = Path(project or Path.cwd()).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("Project must be an existing directory")
    identity = canonical_path(project)
    state = Path(state_dir).expanduser().resolve() if state_dir else state_base() / "projects" / hashlib.sha256(identity.encode()).hexdigest()[:24]
    config_file = state / "config.json"
    config = read_json(config_file)
    if config and (config.get("schemaVersion") != 1 or config.get("projectRoot") != identity):
        raise ValueError("State directory belongs to a different project or unsupported schema; select a separate --state-dir")
    if not config:
        if not create:
            raise FileNotFoundError("Project Viz is not initialized for this project. Run init or start first.")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            state.chmod(0o700)
        config = {"schemaVersion": 1, "projectId": str(uuid.uuid4()), "projectRoot": identity,
                  "title": project.name, "goal": "", "createdAt": utcnow(),
                  "codexHome": str(Path(codex_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve())}
        atomic_json(config_file, config)
    elif codex_home and canonical_path(codex_home) != canonical_path(config["codexHome"]):
        raise ValueError("This project already uses a different Codex home; use a new state directory to keep source identities separate")
    token_file = state / "access-token"
    if create and not token_file.exists():
        # Exclusive creation prevents competing initializers replacing a token.
        try:
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    return {"project": project, "state": state, "config": config}


def safe_project_file(project, relative):
    if not isinstance(relative, str) or "\x00" in relative or "\\" in relative:
        raise ValueError("Use a project-relative POSIX path")
    if Path(relative).is_absolute() or re.match(r"^[A-Za-z]:", relative):
        raise ValueError("Absolute paths are not exposed")
    parts = Path(relative).parts
    denied = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".codex", "node_modules", ".venv", "__pycache__"}
    if any(part in denied or part.startswith(".env") for part in parts):
        raise ValueError("Private or generated paths are not exposed")
    path = (Path(project) / relative).resolve()
    if not path.is_relative_to(Path(project).resolve()):
        raise ValueError("Path leaves the selected project")
    resolved_parts = path.relative_to(Path(project).resolve()).parts
    if any(part.lower() in denied or part.lower().startswith(".env") for part in resolved_parts):
        raise ValueError("Private or generated paths are not exposed")
    if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"} or path.name.lower() in {"credentials", "auth.json", "id_rsa", "id_ed25519", "access-token"}:
        raise ValueError("Credential and private database files are not exposed")
    return path
