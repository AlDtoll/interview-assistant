import atexit
import http.server
import json
import os
import io
import re
import secrets
import signal
import socketserver
import subprocess
import sys
import base64
import threading
import time
import traceback
import uuid
import logging
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx

import numpy as np
import pyaudiowpatch as pyaudio
import webrtcvad
import mss
import tkinter as tk
from tkinter import scrolledtext
from PIL import Image
from scipy.signal import resample_poly
from pynput import keyboard as pynput_keyboard

# Корректная кодировка stdout/stderr на Windows с cp1251
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Подавляем шум HuggingFace Hub в stdout
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

# --- Конфигурация ---
SAMPLE_RATE = 16000
VAD_FRAME_MS = 30
VAD_AGGRESSIVENESS = 1
SILENCE_FRAMES_THRESHOLD = 33   # 33 x 30ms = ~1 сек тишины -> сегмент завершён
MIN_SEGMENT_SECONDS = 2.0
MAX_SEGMENT_SECONDS = 15.0      # force-close сегмент после N сек непрерывной речи (без паузы)
RATE_LIMIT_SECONDS = 8
MANUAL_TRIGGER_SECONDS = 15     # сколько секунд аудио брать по Ctrl+Shift+S
SCREEN_INTERVAL_SECONDS = 3
SCREEN_RING_SIZE = 5
HISTORY_SIZE = 5
AUDIO_RING_SECONDS = 30         # размер ring buffer для ручного триггера

WHISPER_MODEL_SIZE = "large-v3-turbo"
WHISPER_PROMPT = (
    "Junior DS, Data Science, Python, tuple, list, dict, set, frozen set, "
    "MLOps, ML System Design, Mock Interview, "
    "ранжирование, точность, полнота, F1-мера, precision, recall, "
    "embedding, RAG, LLM, MCP, трансформер, градиентный бустинг, "
    "feature engineering, feature store, A/B тест, "
    "PyTorch, TensorFlow, кросс-валидация, гиперпараметры, "
    "регрессия, классификация, дата-сайентист, "
    "Android, Kotlin, Jetpack Compose, корутины, Flow, StateFlow, ViewModel, "
    "RecyclerView, Activity, Fragment, lifecycle, Gradle, R8, ProGuard, "
    "минификация, obfuscation, APK, AAB, Android Bundle, DEX, multidex, ANR, "
    "permissions, runtime permissions, scoped storage, WorkManager, Room, DI, "
    "Koin, Hilt, Dagger, Retrofit, OkHttp, Compose Navigation, "
    "многомодульность, стартап-профиль, Baseline Profile"
)
QUICK_ENGINE = "gemini"
DEEP_ENGINE = os.environ.get("DEEP_ENGINE", "gemini-deep")  # cursor | gemini-deep
THIRD_ENGINE = os.environ.get("THIRD_ENGINE", "claude")  # claude | off

# F9 режим: A=attach_next, B=retry, C=context-aware (дефолт), D=screen_only
F9_MODE = os.environ.get("F9_MODE", "C").upper()
if F9_MODE not in {"A", "B", "C", "D"}:
    print(f"[f9] Неизвестный F9_MODE='{F9_MODE}', сброс в 'C'", flush=True)
    F9_MODE = "C"

# Эндпоинт второго слота — задаётся через env (DEEP_ENDPOINT), встроенного дефолта нет
DEEP_ENDPOINT = os.environ.get("DEEP_ENDPOINT", "")
# Токен только из окружения: встроенный дефолт не держим, без переменной VPS-режим
# даёт явную ошибку (см. _vps_headers).
INTERVIEW_DEEP_TOKEN = os.environ.get("INTERVIEW_DEEP_TOKEN", "")

CONTEXT_PATH = Path(__file__).parent / "context.md"
# Оставляем CONTEXT_FILE как алиас для обратной совместимости (используется в VadWatcher/ControlHandler)
CONTEXT_FILE = CONTEXT_PATH

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")

# --- Мультиплексор агентов ---
# Клиент НЕ ищет харнессы, не замеряет их и не предлагает выбор.
# Настройку (поиск, замер, выбор, запись agents.json) делает внешний агент по инструкции.
# Здесь только: чтение agents.json, вызовы указанных агентов, роутинг трёх слотов.
AGENTS_CONFIG_PATH = Path(__file__).parent / "agents.json"
AGENT_MODES = {"local", "vps", "local_fallback_vps"}

# Рабочая папка CLI-субагентов: отдельный рабочий каталог убирает случайное чтение
# файлов утилиты из cwd (latest_screen.jpg, secrets.env, .agent_token и т.п.).
# Это НЕ запрет доступа к файловой системе: агент по-прежнему может прочитать
# ..\interview-assistant\secrets.env. Полноценная изоляция — отдельный пользователь/ACL,
# это вне задачи.
# НЕ берём %LOCALAPPDATA%: python3.11 здесь — Store-пакет, он виртуализирует запись
# в AppData\Local (реальный путь уезжает в Packages\...\LocalCache), и нативные
# CLI-агенты (node/codex/cursor-agent) такую папку как cwd не видят (ERROR_DIRECTORY).
# Профиль пользователя виртуализацией не затронут.
AGENT_WORK_DIR_ENV = os.environ.get("MVP_ASSISTANT_AGENT_WORK")
AGENT_WORK_DIR = (
    Path(AGENT_WORK_DIR_ENV)
    if AGENT_WORK_DIR_ENV
    else Path(os.environ.get("USERPROFILE") or Path.home()) / "mvp_assistant_agent_work"
)


def _ensure_agent_work_dir() -> Path:
    """Создаёт рабочую папку агентов (идемпотентно). Внутрь ничего не копируем."""
    try:
        AGENT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[agents] не удалось создать рабочую папку {AGENT_WORK_DIR}: {e}", flush=True)
    return AGENT_WORK_DIR

# Верхний предел промпта: при переполнении обрезается вопрос/транскрипт, не system prompt
MAX_PROMPT_CHARS = 20000

# Пресеты CLI. Промпт ВСЕГДА передаётся через stdin, никогда через argv
# (в Windows .cmd-шим исполняется через cmd.exe, где & | % в тексте речи = инъекция).
CLI_AGENT_PRESETS = {
    "claude": {
        "type": "cli",
        "command": ["claude", "-p"],
        # Требует активной подписки Claude Code; при отказе CLI — понятная ошибка
        "requires_subscription": True,
    },
    "codex": {
        "type": "cli",
        "command": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"],
    },
    # cursor-agent читает stdin, если промпт не передан позиционно (проверено на VPS)
    "cursor-agent": {
        "type": "cli",
        "command": ["cursor-agent", "-p", "--mode", "ask", "--trust", "--output-format", "text"],
    },
    # ВНИМАНИЕ: stdin-режим opencode на VPS не проверялся (CLI не установлен)
    "opencode": {
        "type": "cli",
        "command": ["opencode", "run"],
    },
}

DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")

_SLOT_TITLES = {"quick": "Quick", "deep": "Deep", "third": "Third"}

# agents.json читается ОДИН раз при старте утилиты (см. AGENT_CONFIG.update ниже).
# Правка файла внешним агентом требует перезапуска утилиты — на лету не подхватывается.
_agents_lock = threading.Lock()
# benchmarked_at остаётся в структуре для совместимости с форматом agents.json,
# но нигде не используется — в отдельное глобальное состояние не выносим.
AGENT_CONFIG: dict = {"selected": [], "benchmarked_at": None, "mode": "vps"}


def _normalize_agent_spec(item, extra: dict | None = None) -> dict | None:
    """
    Приводит элемент selected (строка или объект) к описанию агента.
    extra — секция agents.<name> из agents.json, накладывается поверх пресета.
    Возвращает None для некорректного элемента.
    """
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        spec = dict(CLI_AGENT_PRESETS.get(name) or {})
        spec["name"] = name
    elif isinstance(item, dict):
        name = str(item.get("name") or item.get("type") or "").strip()
        if not name:
            return None
        spec = dict(CLI_AGENT_PRESETS.get(name) or {})
        spec.update({k: v for k, v in item.items() if k != "name"})
        spec["name"] = name
    else:
        return None

    if isinstance(extra, dict):
        spec.update(extra)
        spec["name"] = name
    if "type" not in spec:
        spec["type"] = "deepseek" if name == "deepseek" else "cli"
    # CLI без команды вызвать нельзя (незнакомое имя и нет command в agents.<name>)
    if spec.get("type") == "cli":
        command = spec.get("command")
        if not isinstance(command, list) or not command:
            return None
    return spec


def _compose_prompt(system_prompt: str, question: str) -> str:
    """
    Склеивает system prompt и вопрос. При превышении MAX_PROMPT_CHARS
    обрезается вопрос (транскрипт), system prompt остаётся целым.
    """
    if not system_prompt:
        combined = question or ""
        overhead = 0
    else:
        combined = f"{system_prompt}\n\n{question}" if question else system_prompt
        overhead = len(system_prompt) + 2

    if len(combined) <= MAX_PROMPT_CHARS:
        return combined

    budget = MAX_PROMPT_CHARS - overhead
    if budget <= 0:
        print(
            f"[prompt] system prompt {len(system_prompt)} chars уже длиннее лимита "
            f"{MAX_PROMPT_CHARS} — вопрос обрезан полностью (system prompt не режется)",
            flush=True,
        )
        return system_prompt
    truncated = (question or "")[:budget]
    print(
        f"[prompt] промпт {len(combined)} chars > {MAX_PROMPT_CHARS} — "
        f"вопрос обрезан до {len(truncated)} chars",
        flush=True,
    )
    return f"{system_prompt}\n\n{truncated}…" if system_prompt else truncated + "…"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Убивает CLI вместе с потомками: .cmd-шим и харнессы плодят дочерние процессы."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as e:
        print(f"[agents] не удалось снять дерево процессов pid={proc.pid}: {e}", flush=True)
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_agent_cmd(command: list[str], prompt: str, timeout: int) -> tuple[int, str, str]:
    """
    Запускает CLI и передаёт промпт через stdin (никогда через argv).
    Возвращает (returncode, stdout, stderr).
    """
    kwargs: dict = {}
    if sys.platform == "win32":
        # Отдельная группа процессов — чтобы taskkill /T снял всё дерево
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(_ensure_agent_work_dir()),
        **kwargs,
    )
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        raise
    return proc.returncode, out or "", err or ""


class AgentCallError(RuntimeError):
    """
    Реальная ошибка вызова агента — машинный признак для роутинга слота.
    Текст сообщения — то, что раньше возвращалось строкой «Ошибка ...»
    (нужен для показа в окне), но ответ модели с таким же началом сюда не попадает.
    """


def _ask_cli(spec: dict, prompt: str, timeout: int, image_path: str | None = None) -> str:
    """Вызов CLI-харнесса через stdin. Текст ответа при успехе, иначе AgentCallError."""
    name = spec.get("name") or "cli"
    command = spec.get("command")
    # Проверка не дублирует _normalize_agent_spec: ask_local принимает и готовый dict,
    # который эту нормализацию не проходил, а невалидный command не должен дойти до subprocess.
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise AgentCallError(
            f"Ошибка {name}: в конфиге нет корректного 'command' (непустой список строк)"
        )

    prompt_via = str(spec.get("prompt_via") or "stdin").lower()
    if prompt_via != "stdin":
        raise AgentCallError(
            f"Ошибка {name}: поддерживается только prompt_via='stdin' "
            "(передача промпта в аргументах запрещена)"
        )

    # Картинка для CLI: режим arg — флаг -i перед завершающим "-" (codex),
    # режим file — файл уже лежит в cwd; его имя агент берёт из промпта.
    cmd = list(command)
    image_mode = str(spec.get("image_mode") or "").lower()
    if image_mode == "arg" and image_path and Path(image_path).exists():
        if cmd and cmd[-1] == "-":
            cmd = cmd[:-1] + ["-i", image_path, "-"]
        else:
            cmd = cmd + ["-i", image_path]

    returncode, out, err = _run_agent_cmd(cmd, prompt, timeout)
    out = (out or "").strip()

    if returncode != 0:
        detail = (err or "").strip()[:300] or out[:300] or "нет вывода"
        if spec.get("requires_subscription"):
            raise AgentCallError(
                f"Ошибка {name}: CLI не отвечает (код {returncode}). "
                f"Агент требует активной подписки/входа в CLI: {detail}"
            )
        raise AgentCallError(f"Ошибка {name}: код {returncode}: {detail}")

    if not out:
        if spec.get("requires_subscription"):
            raise AgentCallError(f"Ошибка {name}: пустой ответ CLI — проверьте вход/подписку")
        raise AgentCallError(f"Ошибка {name}: пустой ответ")
    return out


def _ask_deepseek(
    spec: dict, prompt: str, timeout: int, image_path: str | None = None,
) -> str:
    """Прямой HTTP-запрос к api.deepseek.com. Текст ответа при успехе, иначе AgentCallError."""
    name = spec.get("name") or "deepseek"
    key_env = spec.get("api_key_env") or "DEEPSEEK_API_KEY"
    api_key = spec.get("api_key") or os.environ.get(key_env, "")
    if not api_key:
        raise AgentCallError(f"Ошибка {name}: не задан ключ (agents.json → api_key или env {key_env})")

    model = spec.get("model") or DEEPSEEK_DEFAULT_MODEL
    api_url = spec.get("api_url") or DEEPSEEK_API_URL
    # Свежий скриншот уходит в формате OpenAI vision (data:image/jpeg;base64,...)
    if image_path and Path(image_path).exists():
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        content: object = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    else:
        content = prompt
    resp = httpx.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AgentCallError(f"Ошибка {name}: неожиданный формат ответа API")
    answer = (answer or "").strip()
    if not answer:
        raise AgentCallError(f"Ошибка {name}: пустой ответ")
    return answer


def ask_local(
    agent, system_prompt: str, question: str, timeout: int = 60,
    image_path: str | None = None,
) -> str:
    """
    Вызов указанного в agents.json агента. Возвращает текст ответа,
    при реальной ошибке вызова поднимает AgentCallError.
    Промпт не попадает в argv: CLI получает его через stdin, deepseek — в теле POST.
    agent — имя, описание агента (dict) или None.
    """
    spec = dict(agent) if isinstance(agent, dict) else _normalize_agent_spec(agent)
    if spec is None:
        raise AgentCallError(f"Ошибка: неизвестный агент '{agent}'")

    name = spec.get("name") or "агент"
    prompt = _compose_prompt(system_prompt, question)
    agent_type = str(spec.get("type") or "cli").lower()
    try:
        if agent_type == "deepseek":
            return _ask_deepseek(spec, prompt, timeout, image_path=image_path)
        if agent_type == "cli":
            return _ask_cli(spec, prompt, timeout, image_path=image_path)
        raise AgentCallError(f"Ошибка {name}: неизвестный тип агента '{agent_type}'")
    except AgentCallError:
        raise
    except subprocess.TimeoutExpired:
        raise AgentCallError(f"Ошибка {name}: timeout {timeout}s")
    except FileNotFoundError:
        raise AgentCallError(f"Ошибка {name}: CLI не найден")
    except Exception as e:
        raise AgentCallError(f"Ошибка {name}: {type(e).__name__}: {_safe_error(e)}")


def load_agent_config() -> dict:
    """Читает agents.json; при отсутствии/ошибке — пустой конфиг (режим VPS)."""
    empty = {"selected": [], "benchmarked_at": None, "mode": "vps"}
    try:
        data = json.loads(AGENTS_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except Exception as e:
        print(f"[agents] ошибка чтения {AGENTS_CONFIG_PATH.name}: {e}", flush=True)
        return empty

    if not isinstance(data, dict):
        print(f"[agents] {AGENTS_CONFIG_PATH.name}: ожидался объект, слоты не заданы", flush=True)
        return empty

    raw_selected = data.get("selected")
    if raw_selected is None:
        raw_selected = []
    elif not isinstance(raw_selected, list):
        print(
            f"[agents] selected должен быть списком, получено "
            f"{type(raw_selected).__name__} — слоты не заданы",
            flush=True,
        )
        raw_selected = []

    extra_agents = data.get("agents")
    if extra_agents is not None and not isinstance(extra_agents, dict):
        print("[agents] секция agents должна быть объектом — игнорирую", flush=True)
        extra_agents = None
    extra_agents = extra_agents or {}

    selected = []
    for item in raw_selected:
        raw_name = item.get("name") if isinstance(item, dict) else item
        extra = extra_agents.get(raw_name) if isinstance(raw_name, str) else None
        if extra is not None and not isinstance(extra, dict):
            extra = None
        spec = _normalize_agent_spec(item, extra)
        if spec is None:
            print(f"[agents] пропускаю некорректный элемент selected: {item!r}", flush=True)
            continue
        selected.append(spec)
        if len(selected) == 3:
            break

    mode = data.get("mode") or "vps"
    if mode not in AGENT_MODES:
        print(f"[agents] неизвестный mode='{mode}' — сброс в 'vps'", flush=True)
        mode = "vps"
    return {"selected": selected, "benchmarked_at": data.get("benchmarked_at"), "mode": mode}


# Конфиг читается синхронно на старте: поиск/выбор/запись агентов делает внешний агент,
# клиент лишь использует то, что записано в agents.json.
# ВАЖНО: это единственное чтение. Если внешний агент правит agents.json во время работы,
# изменения вступят в силу только после перезапуска утилиты (hot-reload не предусмотрен).
AGENT_CONFIG.update(load_agent_config())
if AGENT_CONFIG["selected"]:
    _names = ", ".join(spec.get("name") or "?" for spec in AGENT_CONFIG["selected"])
    print(f"[agents] слоты из {AGENTS_CONFIG_PATH.name}: {_names} (mode={AGENT_CONFIG['mode']})", flush=True)
else:
    print(f"[agents] {AGENTS_CONFIG_PATH.name}: слоты не заданы — режим VPS", flush=True)

# --- Флаг LLM-фильтра: True = фильтровать ответы кандидата (тестовый режим), False = prod ---
FILTER_QUESTIONS = os.environ.get("TEST_FILTER_QUESTIONS", "0") == "1"

# --- Ключ Gemini для локального is_interviewer_question ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Уникальный ID сессии — для привязки серверных и клиентских логов
SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

# --- Control server ---
AGENT_TOKEN = secrets.token_urlsafe(24)
CONTROL_PORT = 17777
STARTED_AT = time.time()

_token_file = Path(__file__).parent / ".agent_token"
try:
    _token_file.write_text(AGENT_TOKEN)
except Exception:
    pass

# Папка для клиентских логов сессии
LOG_DIR = Path(__file__).parent / "interview_logs"
LOG_DIR.mkdir(exist_ok=True)
CLIENT_LOG = LOG_DIR / f"session_{SESSION_ID}_client.jsonl"


# --- Hot-reload context.md ---
# Хранит текущий текст и mtime для O(1)-проверки изменений без лишних disk read
_context_text: str = ""
_context_mtime: float = 0.0
_context_lock = threading.Lock()

# Семафор для ограничения параллельных запросов к Gemini classify (не более 2 одновременно)
# снижает число timeout'ов при частых VAD-сегментах
_GEMINI_CLASSIFY_SEM = threading.Semaphore(2)


def get_context() -> str:
    """Возвращает актуальный текст context.md. Перечитывает файл только при изменении mtime."""
    global _context_text, _context_mtime
    try:
        m = os.path.getmtime(CONTEXT_PATH)
    except FileNotFoundError:
        return ""
    if m > _context_mtime:
        with _context_lock:
            # double-check под блокировкой (другой поток мог уже обновить)
            if m > _context_mtime:
                try:
                    with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
                        raw = f.read().strip()
                    non_comment_lines = [
                        l for l in raw.splitlines()
                        if l.strip() and not l.strip().startswith("#")
                    ]
                    _context_text = raw if non_comment_lines else ""
                    _context_mtime = m
                    print(f"[context] reloaded ({len(_context_text)} chars)", flush=True)
                except Exception as e:
                    print(f"[context] read error: {e}", flush=True)
    return _context_text


# Выполняем первоначальную загрузку при старте — чтобы SYSTEM_PROMPT сформировался правильно
_INIT_CONTEXT = get_context()


# --- Проектные заметки (RAG-lite по локальным .md/.txt) ---
# Папка notes/ рядом с утилитой: рекурсивно читаем .md и .txt, режем на чанки,
# строим TF-IDF на numpy и подмешиваем до 3 релевантных чанков в вопрос.
# Индекс строится лениво при первом вопросе; пересборка — при изменении mtime файлов.
NOTES_DIR = Path(__file__).parent / "notes"
NOTES_CHUNK_CHARS = 500
NOTES_TOP_K = 3
NOTES_MIN_SCORE = 0.12
NOTES_MAX_CHARS = 1800
_NOTES_TOKEN_RE = re.compile(r"[а-яёa-z0-9_]{3,}")

_notes_lock = threading.Lock()
_notes_index: dict = {"signature": None, "chunks": [], "vocab": {}, "idf": None, "matrix": None}
_notes_memo_lock = threading.Lock()
_notes_memo: dict = {"query": None, "signature": None, "block": "", "labels": []}


def _notes_files() -> list:
    """Все .md/.txt рекурсивно в notes/. Пустой список, если папки нет."""
    if not NOTES_DIR.is_dir():
        return []
    try:
        return [
            p for p in sorted(NOTES_DIR.rglob("*"))
            if p.is_file() and p.suffix.lower() in (".md", ".txt")
        ]
    except OSError as e:
        print(f"[notes] scan error: {e}", flush=True)
        return []


def _notes_signature(files: list) -> tuple:
    """Отпечаток набора файлов: путь + mtime. Новый файл или правка меняют отпечаток."""
    sig = []
    for p in files:
        try:
            sig.append((str(p), p.stat().st_mtime))
        except OSError:
            pass
    return tuple(sig)


def _split_notes_text(text: str, limit: int = NOTES_CHUNK_CHARS) -> list:
    """Режет текст на чанки ~limit символов, границы — заголовки и пустые строки."""
    blocks = re.split(r"\n\s*\n|\n(?=#)", text)
    chunks, cur = [], ""
    for block in blocks:
        block = block.strip()
        while len(block) > limit:
            cut = block.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            head, block = block[:cut].strip(), block[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            if head:
                chunks.append(head)
        if not block:
            continue
        if cur and len(cur) + len(block) + 2 > limit:
            chunks.append(cur)
            cur = block
        elif cur:
            cur = f"{cur}\n\n{block}"
        else:
            cur = block
    if cur:
        chunks.append(cur)
    return chunks


def _notes_title(chunk: str) -> str:
    """Заголовок чанка — первые 40 символов текста в одну строку."""
    title = " ".join(chunk.split())
    return title[:40] + ("…" if len(title) > 40 else "")


def _notes_tokens(text: str) -> list:
    return _NOTES_TOKEN_RE.findall(text.lower())


def _build_notes_index(files: list) -> None:
    """Пересобирает TF-IDF индекс по всем файлам заметок (вызывается под локом)."""
    docs, labels = [], []
    for p in files:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[notes] read error {p.name}: {e}", flush=True)
            continue
        try:
            rel = p.relative_to(NOTES_DIR).as_posix()
        except ValueError:
            rel = p.name
        for chunk in _split_notes_text(raw):
            docs.append(chunk)
            labels.append(rel)

    vocab: dict = {}
    token_counts = []
    for chunk in docs:
        counts: dict = {}
        for token in _notes_tokens(chunk):
            counts[token] = counts.get(token, 0) + 1
        token_counts.append(counts)
        for token in counts:
            if token not in vocab:
                vocab[token] = len(vocab)

    n = len(docs)
    df = np.zeros(len(vocab), dtype=np.float64)
    for counts in token_counts:
        for token in counts:
            df[vocab[token]] += 1.0
    idf = np.log((n + 1.0) / (df + 1.0)) + 1.0

    matrix = np.zeros((n, len(vocab)), dtype=np.float32)
    for i, counts in enumerate(token_counts):
        for token, cnt in counts.items():
            matrix[i, vocab[token]] = cnt * idf[vocab[token]]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    matrix /= norms

    chunks = [
        {"label": labels[i], "title": _notes_title(docs[i]), "text": docs[i], "vector": matrix[i]}
        for i in range(n)
    ]
    _notes_index.update(
        signature=_notes_signature(files), chunks=chunks, vocab=vocab, idf=idf, matrix=matrix,
    )
    print(f"[notes] index built: {len(files)} files, {n} chunks", flush=True)


def _clear_notes_memo() -> None:
    """Сбрасывает мемо поиска: после смены индекса старые labels/block невалидны."""
    with _notes_memo_lock:
        _notes_memo.update(query=None, signature=None, block="", labels=[])


def _ensure_notes_index() -> bool:
    """Пересобирает индекс при изменении набора файлов/mtime. False — заметок нет."""
    files = _notes_files()
    if not files:
        with _notes_lock:
            _notes_index.update(signature=None, chunks=[], vocab={}, idf=None, matrix=None)
        _clear_notes_memo()
        return False
    signature = _notes_signature(files)
    if signature != _notes_index["signature"]:
        with _notes_lock:
            # double-check под блокировкой (другой поток мог уже пересобрать)
            if signature != _notes_index["signature"]:
                _build_notes_index(files)
        _clear_notes_memo()
    return True


def search_notes(query: str) -> dict:
    """
    Ищет до NOTES_TOP_K релевантных чанков заметок по косинусу TF-IDF.
    Возвращает {"block": текст для промпта, "labels": [файлы]}.
    Пустой block, если заметок нет или ничего не прошло порог NOTES_MIN_SCORE.
    """
    empty = {"block": "", "labels": []}
    if not query or not query.strip():
        return empty
    # Подпись текущего набора заметок — часть ключа мемо: удаление/правка файлов
    # инвалидирует кэш ещё до обращения к индексу.
    signature = _notes_signature(_notes_files())
    with _notes_memo_lock:
        if _notes_memo["query"] == query and _notes_memo["signature"] == signature:
            return {"block": _notes_memo["block"], "labels": list(_notes_memo["labels"])}

    if not _ensure_notes_index():
        return empty
    with _notes_lock:
        chunks = list(_notes_index["chunks"])
        vocab = dict(_notes_index["vocab"])
        idf = _notes_index["idf"]
        matrix = _notes_index["matrix"]
    if not chunks or not vocab or idf is None or matrix is None or not len(matrix):
        return empty

    counts: dict = {}
    for token in _notes_tokens(query):
        j = vocab.get(token)
        if j is not None:
            counts[j] = counts.get(j, 0) + 1
    if not counts:
        return empty
    qvec = np.zeros(len(vocab), dtype=np.float32)
    for j, cnt in counts.items():
        qvec[j] = cnt * idf[j]
    qnorm = float(np.linalg.norm(qvec))
    if qnorm == 0.0:
        return empty
    qvec /= qnorm

    scores = matrix @ qvec
    order = np.argsort(scores)[::-1]
    picked, total = [], 0
    for idx in order:
        if len(picked) >= NOTES_TOP_K:
            break
        score = float(scores[idx])
        if score < NOTES_MIN_SCORE:
            break
        chunk = chunks[int(idx)]
        piece = chunk["text"]
        if total + len(piece) > NOTES_MAX_CHARS:
            allowed = NOTES_MAX_CHARS - total
            if allowed <= 0:
                break
            piece = piece[:allowed]
            # Режем по границе слова, если она есть в пределах allowed
            cut = piece.rfind(" ")
            if cut > 0:
                piece = piece[:cut]
        picked.append((chunk, score, piece))
        total += len(piece)
        if total >= NOTES_MAX_CHARS:
            break

    if not picked:
        block, labels = "", []
    else:
        lines = ["Выдержки из проектных заметок (могут быть неполными; при противоречии верь вопросу):"]
        for chunk, _score, piece in picked:
            lines.append(f"[notes/{chunk['label']} › {chunk['title']}] {piece}")
        block = "\n".join(lines)
        labels = list(dict.fromkeys(chunk["label"] for chunk, _s, _t in picked))
    with _notes_memo_lock:
        _notes_memo.update(query=query, signature=signature, block=block, labels=labels)
    return {"block": block, "labels": labels}


_SYSTEM_PROMPT_BASE = (
    "Ты помощник пользователю на техническом собеседовании. "
    "Слышишь голос интервьюера через системный звук и можешь получать скриншот экрана пользователя, "
    "если он приложен к запросу. "
    "Если скриншот не приложен — НЕ выдумывай, что на экране; пиши 'Экран: не передан'. "
    "Формат ответа СТРОГО:\n"
    "\n"
    "**Экран:** одна фраза о том что видишь на экране (что за приложение/задача/код). "
    "Если экран не релевантен вопросу — пиши 'Экран: не влияет на вопрос'. НО всегда упоминай эту строку.\n"
    "\n"
    "**Вопрос:** одна фраза — как ты понял вопрос интервьюера.\n"
    "\n"
    "**Ответ:**\n"
    "- 3-4 буллета максимум, каждый с ключевым словом **жирным**\n"
    "- Максимум 30 секунд речи для ответа\n"
    "- Если вопрос по коду на экране — учитывай код в ответе\n"
    "- Если непонятно — предложи 1 короткий уточняющий вопрос для интервьюера\n"
    "\n"
    "Язык ответа — ТОЛЬКО русский."
)


def _build_system_prompt() -> str:
    """Формирует system prompt с актуальным контекстом из context.md (hot-reload)."""
    ctx = get_context()
    if ctx:
        return (
            f"Вот дополнительный контекст (специфика вакансии/тем):\n"
            f"{ctx}\n\n"
            + _SYSTEM_PROMPT_BASE
        )
    return _SYSTEM_PROMPT_BASE


# SYSTEM_PROMPT — для обратной совместимости (статичная версия при старте)
SYSTEM_PROMPT = _build_system_prompt()


# --- HTTP клиент ---

_http_client = httpx.Client(timeout=30.0)
atexit.register(_http_client.close)


def _safe_error(e: Exception) -> str:
    """Убирает ключи API из текста ошибки перед выводом."""
    msg = str(e)
    parts = msg.split()
    sanitized = []
    for part in parts:
        if part.startswith("sk-"):
            sanitized.append("***")
        else:
            sanitized.append(part)
    return " ".join(sanitized)[:300]


def _build_messages(transcript: str, screen_frames: list[str]) -> list[dict]:
    """Собирает messages-список для OpenAI vision format с историей Q&A."""
    # Используем _build_system_prompt() — подхватывает hot-reload из context.md
    messages = [{"role": "system", "content": _build_system_prompt()}]

    with _qa_lock:
        history_snapshot = list(qa_history)
    for past_q, past_a in history_snapshot:
        messages.append({"role": "user", "content": past_q})
        messages.append({"role": "assistant", "content": past_a})

    current_content = []
    for frame_b64 in screen_frames:
        current_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"},
        })
    current_content.append({
        "type": "text",
        "text": (
            f"Транскрипт (голос интервьюера):\n\"\"\"{transcript or '(тишина)'}\"\"\"\n\n"
            "Ответь по инструкции из system prompt."
        ),
    })

    messages.append({"role": "user", "content": current_content})
    return messages


def _vps_headers() -> dict:
    """Заголовки для VPS /interview-deep. Без токена и адреса в окружении запросы не шлём вовсе."""
    if not DEEP_ENDPOINT:
        raise RuntimeError("DEEP_ENDPOINT не задан в окружении — VPS-режим недоступен")
    if not INTERVIEW_DEEP_TOKEN:
        raise RuntimeError("INTERVIEW_DEEP_TOKEN не задан в окружении — VPS-режим недоступен")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {INTERVIEW_DEEP_TOKEN}",
    }


def ask_gemini(transcript: str, screen_frames: list[str]) -> str:
    """Быстрый ответ — Claude Haiku через VPS /interview-deep?engine=claude-haiku."""
    response = _http_client.post(
        f"{DEEP_ENDPOINT}?engine={QUICK_ENGINE}",
        headers=_vps_headers(),
        json={
            "transcript": transcript or "(тишина)",
            "screens_b64": screen_frames,
            "session_id": SESSION_ID,
        },
        timeout=20.0,
    )
    response.raise_for_status()
    data = response.json()
    return data["answer"]


def ask_deep(transcript: str, screen_frames: list[str], session_id: str) -> tuple[str, int]:
    """
    Глубокий ответ — VPS endpoint /interview-deep.
    Возвращает (answer, latency_ms).
    """
    t0 = time.monotonic()
    response = _http_client.post(
        f"{DEEP_ENDPOINT}?engine={DEEP_ENGINE}",
        json={
            "transcript": transcript or "(тишина)",
            "screens_b64": screen_frames,
            "session_id": session_id,
        },
        headers=_vps_headers(),
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    # Latency считаем на клиенте — server тоже может прислать своё поле
    client_latency = int((time.monotonic() - t0) * 1000)
    latency_ms = data.get("latency_ms", client_latency)
    return data["answer"], latency_ms


def ask_third(transcript: str, screen_frames: list[str], session_id: str) -> tuple[str, int]:
    """
    Третий тир — Claude Sonnet через VPS /interview-deep?engine=THIRD_ENGINE.
    Возвращает (answer, latency_ms). Timeout 30s (Sonnet warm 2.5s + запас).
    """
    # Жёсткий gate в точке реального запроса: сюда попадает и fallback из ask_slot,
    # который не проходит через guard в _third_thread.
    if THIRD_ENGINE == "off":
        raise AgentCallError("третий агент отключён (THIRD_ENGINE=off)")
    t0 = time.monotonic()
    response = _http_client.post(
        f"{DEEP_ENDPOINT}?engine={THIRD_ENGINE}",
        json={
            "transcript": transcript or "(тишина)",
            "screens_b64": screen_frames,
            "session_id": session_id,
        },
        headers=_vps_headers(),
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json()
    client_latency = int((time.monotonic() - t0) * 1000)
    latency_ms = data.get("latency_ms", client_latency)
    return data["answer"], latency_ms


def _agent_mode() -> str:
    with _agents_lock:
        return AGENT_CONFIG.get("mode") or "vps"


def _slot_agents() -> dict:
    """Слоты overlay → описания агентов (1-й, 2-й, 3-й слот)."""
    with _agents_lock:
        selected = list(AGENT_CONFIG.get("selected") or [])
    return {
        "quick": selected[0] if len(selected) > 0 else None,
        "deep": selected[1] if len(selected) > 1 else None,
        "third": selected[2] if len(selected) > 2 else None,
    }


def _slot_label(slot: str, default: str) -> str:
    """Имя агента, который реально обслуживает слот (для подписей в оверлее)."""
    spec = _slot_agents().get(slot)
    if not spec or _agent_mode() == "vps":
        return default
    return spec.get("name") or default


def _build_local_question(
    transcript: str, image_attached: bool, image_mode: str = "", image_filename: str = "",
) -> str:
    """Вопрос для локального агента: честно сообщаем, приложен ли скриншот и как."""
    text = f"Транскрипт (голос интервьюера):\n\"\"\"{transcript or '(тишина)'}\"\"\"\n\n"
    mode = (image_mode or "").lower()
    if image_attached and mode in ("arg", "inline"):
        text += "К запросу приложен свежий скриншот экрана.\n\n"
    elif image_attached and mode == "file":
        name = image_filename or "screen.jpg"
        text += (
            f"Свежий скриншот экрана лежит в файле {name} в текущем каталоге — "
            "прочитай его перед ответом.\n\n"
        )
    else:
        text += (
            "Скриншот НЕ передан — не описывай содержимое экрана, "
            "пиши 'Экран: не передан'.\n\n"
        )
    # Подмешиваем релевантные выдержки из notes/ (поиск по TF-IDF, без анализа моделью)
    notes = search_notes(transcript)
    if notes["block"]:
        text += notes["block"] + "\n\n"
    return text + "Ответь по инструкции из system prompt."


def _ask_vps_slot(slot: str, transcript: str, screens: list[str], session_id: str) -> tuple[str, int]:
    """Существующий путь через VPS /interview-deep для конкретного слота."""
    t0 = time.monotonic()
    try:
        if slot == "quick":
            return ask_gemini(transcript, screens), int((time.monotonic() - t0) * 1000)
        if slot == "deep":
            return ask_deep(transcript, screens, session_id)
        return ask_third(transcript, screens, session_id)
    except AgentCallError:
        raise
    except Exception as e:
        # Текст ошибки прежний: потоки вызова показывают его с префиксом слота
        raise AgentCallError(f"{type(e).__name__}: {_safe_error(e)}")


def ask_slot(slot: str, transcript: str, screens: list[str], session_id: str) -> tuple[str, int]:
    """
    Роутинг слота overlay: vps | local | local_fallback_vps.
    Возвращает (answer, latency_ms). Ошибка вызова — AgentCallError.
    """
    mode = _agent_mode()
    agent = _slot_agents().get(slot)
    if mode == "vps":
        return _ask_vps_slot(slot, transcript, screens, session_id)

    if not agent:
        label = _SLOT_TITLES.get(slot, slot)
        if mode == "local_fallback_vps":
            print(f"[{slot}] слот не настроен -> fallback VPS", flush=True)
            return _ask_vps_slot(slot, transcript, screens, session_id)
        # В режиме local молча на VPS не уходим: слот должен быть настроен явно
        raise RuntimeError(f"{label}: слот не настроен (agents.json → selected)")

    t0 = time.monotonic()
    # Скриншот кладём в рабочую папку агента УНИКАЛЬНЫМ файлом на вызов
    # (screen-<uuid>.jpg): общий screen.jpg ломался при параллельных слотах —
    # write_bytes не атомарен, а удаление «старого» файла одной веткой рвало
    # чтение, которое в этот момент делает другой слот.
    image_path = None
    screen_file = None
    image_mode = str(agent.get("image_mode") or "").lower()
    if agent.get("supports_images") and screens:
        try:
            screen_file = _ensure_agent_work_dir() / f"screen-{uuid.uuid4().hex}.jpg"
            screen_file.write_bytes(base64.b64decode(screens[0]))
            image_path = str(screen_file)
        except Exception as e:
            print(f"[{slot}] не удалось подготовить скриншот: {e}", flush=True)
            screen_file = None
            image_path = None
    try:
        answer = ask_local(
            agent,
            _build_system_prompt(),
            _build_local_question(
                transcript,
                image_attached=bool(image_path), image_mode=image_mode,
                image_filename=Path(image_path).name if image_path else "",
            ),
            image_path=image_path,
        )
    except AgentCallError as e:
        if mode == "local_fallback_vps":
            print(f"[{slot}] {agent.get('name')}: {str(e)[:120]} -> fallback VPS", flush=True)
            return _ask_vps_slot(slot, transcript, screens, session_id)
        raise
    finally:
        # Удаляем только свой временный файл, после завершения вызова
        if screen_file is not None:
            try:
                screen_file.unlink(missing_ok=True)
            except Exception as e:
                print(f"[{slot}] не удалось удалить временный скриншот {screen_file}: {e}", flush=True)
    latency_ms = int((time.monotonic() - t0) * 1000)
    return answer, latency_ms


def _gemini_is_question(transcript: str) -> bool:
    """
    Приватный Gemini-классификатор — вызывается только для сомнительных фраз из is_interviewer_question.
    Timeout 3s, fail-open: при ошибке возвращает True.
    """
    if not GEMINI_API_KEY:
        print("[filter] GEMINI_API_KEY не задан — пропускаем классификацию (fail-open)", flush=True)
        return True
    prompt_text = (
        "Определи роль говорящего по фразе. "
        "Если это ВОПРОС ИНТЕРВЬЮЕРА (спрашивает о навыках/опыте/задаёт задачу) — ответь 'YES'. "
        "Если это ОТВЕТ КАНДИДАТА (объясняет что делает/рассказывает про опыт) или "
        "короткое междометие ('да', 'угу', 'понятно') — ответь 'NO'. "
        f"Только YES или NO без объяснений. Фраза: '{transcript}'"
    )
    try:
        with _GEMINI_CLASSIFY_SEM:
            resp = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt_text}]}]},
                timeout=3.0,
            )
        resp.raise_for_status()
        data = resp.json()
        answer = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
        result = answer.startswith("YES")
        print(f"[filter] gemini classify='{answer}' -> {'PASS' if result else 'SKIP'}: {transcript[:60]}", flush=True)
        return result
    except Exception as e:
        print(f"[filter] gemini classify error (fail-open): {e}", flush=True)
        return True


def is_interviewer_question(transcript: str) -> bool:
    """
    Гибридный классификатор: сначала быстрый regex pre-filter (0ms),
    сомнительные фразы — в Gemini (_gemini_is_question).
    Используется только при FILTER_QUESTIONS=True (тест-режим на YouTube mock-interview).
    Fail-open: при любой ошибке возвращает True (лучше лишний ответ, чем потерять реальный вопрос).
    """
    text = transcript.strip().lower()
    if not text:
        return False

    # HARD YES — явные вопросы интервьюера (regex, 0ms)
    YES_PATTERNS = [
        r"^расскажи", r"^объясни", r"^опиши",
        r"^что такое", r"^что это", r"^как ", r"^почему", r"^зачем",
        r"^можешь ", r"^давай", r"^перейдём", r"^перейдем", r"^поговорим",
        r"^следующ", r"^ещё вопрос", r"^ещё один вопрос",
        r"вопрос:", r"задача:", r"кейс:",
    ]
    for p in YES_PATTERNS:
        if re.search(p, text):
            print(f"[filter] regex YES ({p}): {transcript[:60]}", flush=True)
            return True

    # Заканчивается на «?» — явный вопрос
    if text.rstrip(".!… ").endswith("?"):
        print(f"[filter] regex YES (ends ?): {transcript[:60]}", flush=True)
        return True

    # HARD NO — типичные ответы кандидата / междометия
    NO_PATTERNS = [
        r"^да[.,!?\s]", r"^нет[.,!?\s]", r"^ну\b", r"^угу\b", r"^ага\b",
        r"^понятно", r"^понял", r"^понимаю",
        r"^спасибо", r"^хорошо\b", r"^ок[.,!?\s]?$",
        r"^я думаю", r"^мне кажется", r"^я бы сказал",
        r"^значит", r"^то есть", r"^вот[.,!?\s]",
    ]
    for p in NO_PATTERNS:
        if re.search(p, text):
            print(f"[filter] regex NO ({p}): {transcript[:60]}", flush=True)
            return False

    # Короткая фраза без «?» — скорее ответ кандидата, пропускаем
    if len(text) < 30:
        print(f"[filter] regex NO (short, len={len(text)}): {transcript[:60]}", flush=True)
        return False

    # СОМНИТЕЛЬНОЕ — передаём в Gemini classifier
    print(f"[filter] regex UNCERTAIN → Gemini: {transcript[:60]}", flush=True)
    return _gemini_is_question(transcript)


def _log_exchange(
    transcript: str,
    quick: str | None,
    deep: str | None,
    quick_ms: int | None,
    deep_ms: int | None,
    whisper_latency_ms: int | None = None,
    third: str | None = None,
    third_ms: int | None = None,
) -> None:
    """Пишет одну строку JSONL в клиентский лог сессии.
    Принимает None для незавершённых тиров — пишем сразу по факту готовности любого."""
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "transcript": transcript,
        "quick": quick,
        "deep": deep,
        "third": third,
        "quick_latency_ms": quick_ms,
        "deep_latency_ms": deep_ms,
        "third_latency_ms": third_ms,
        "whisper_latency_ms": whisper_latency_ms,
    }
    try:
        with CLIENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[LOG] write error: {e}")


# Флаг защиты от параллельных quick-запросов — skip если предыдущий ещё не вернулся
_quick_in_flight = False
_quick_in_flight_lock = threading.Lock()


def ask_dual(transcript: str, screens: list[str], session_id: str, whisper_latency_ms: int | None = None) -> None:
    """
    Запускает два параллельных запроса (первый и второй слоты), каждый обновляет свой overlay.
    Блокироваться не нужно — оба потока daemon, результаты через after(0, ...).
    Первый слот пропускается (skip), если предыдущий запрос ещё не завершился — защита от throttling.
    Лог пишется раздельно: каждый тир пишет свою JSONL-запись сразу по завершении (null для другого).
    whisper_latency_ms — время транскрипции Whisper, пишется только в quick-запись.
    """
    global _quick_in_flight

    # LLM-фильтр: пропускаем ответы кандидата в тест-режиме (TEST_FILTER_QUESTIONS=1)
    if FILTER_QUESTIONS and not is_interviewer_question(transcript):
        print(f"[filter] skip non-question: {transcript[:80]}", flush=True)
        overlay.set_quick_status_safe("Фильтр: пропуск (не вопрос)")
        return

    def _quick_thread():
        global _quick_in_flight
        with _quick_in_flight_lock:
            if _quick_in_flight:
                print("[quick] skipped: предыдущий запрос ещё в процессе")
                overlay.set_quick_status_safe("пропуск (занят)")
                return
            _quick_in_flight = True
        try:
            answer, quick_ms = ask_slot("quick", transcript, screens, session_id)
            overlay.append_quick_qa_safe(transcript, answer, quick_ms)
            overlay.set_quick_status_safe(f"Ответ ({quick_ms}ms)")
            # Пишем запись только для quick — deep ещё может быть в процессе
            # whisper_latency_ms пишем в quick-запись (transcript первичен здесь)
            _log_exchange(transcript, answer, None, quick_ms, None, whisper_latency_ms=whisper_latency_ms)
        except Exception as e:
            print(f"[quick] error: {type(e).__name__}: {str(e)[:200]}", flush=True)
            overlay.set_quick_status_safe(f"Ошибка: {_safe_error(e)}")
        finally:
            with _quick_in_flight_lock:
                _quick_in_flight = False

    def _deep_thread():
        try:
            overlay.set_deep_status_safe("Думаю глубоко...")
            answer, latency_ms = ask_slot("deep", transcript, screens, session_id)
            overlay.append_deep_qa_safe(transcript, answer, latency_ms)
            overlay.set_deep_status_safe(f"Ответ ({latency_ms}ms)")
            # Пишем запись только для deep — quick уже записан отдельно
            _log_exchange(transcript, None, answer, None, latency_ms)
        except Exception as e:
            print(f"[deep] error: {type(e).__name__}: {str(e)[:200]}", flush=True)
            overlay.set_deep_status_safe(f"Ошибка: {_safe_error(e)}")

    def _third_thread():
        # Guard строим от того, куда реально уйдёт запрос: THIRD_ENGINE управляет
        # только VPS-путём, локальный агент слота от него не зависит.
        # Это лишь ранний пропуск: настоящий gate стоит в ask_third, поэтому
        # fallback из ask_slot до сети при THIRD_ENGINE=off тоже не доходит.
        third_agent = _slot_agents().get("third")
        mode = _agent_mode()
        will_use_vps = mode == "vps" or (mode == "local_fallback_vps" and third_agent is None)
        if will_use_vps and THIRD_ENGINE == "off":
            print("[third] пропуск: запрос ушёл бы на VPS, а THIRD_ENGINE=off", flush=True)
            return
        try:
            overlay.set_third_status_safe("Думает...")
            answer, latency_ms = ask_slot("third", transcript, screens, session_id)
            overlay.append_third_qa_safe(transcript, answer, latency_ms)
            overlay.set_third_status_safe(f"Ответ ({latency_ms}ms)")
            _log_exchange(transcript, None, None, None, None, third=answer, third_ms=latency_ms)
        except Exception as e:
            print(f"[third] error: {type(e).__name__}: {str(e)[:200]}", flush=True)
            overlay.set_third_status_safe(f"Ошибка: {_safe_error(e)}")

    threading.Thread(target=_quick_thread, daemon=True).start()
    threading.Thread(target=_deep_thread, daemon=True).start()
    threading.Thread(target=_third_thread, daemon=True).start()


# --- Audio: WASAPI loopback ---

class LoopbackRecorder:
    """
    Пишет системный звук через WASAPI loopback (blocking mode).
    Blocking read в отдельном thread — обходит проблему Realtek WASAPI,
    когда callback-mode не вызывается вообще.
    """

    def __init__(self):
        self._pa = pyaudio.PyAudio()
        try:
            info = self._pa.get_default_wasapi_loopback()
        except OSError as e:
            raise RuntimeError("Нет WASAPI loopback — нужен Windows + WASAPI-совместимый драйвер.") from e

        self._device_index = info["index"]
        self._native_rate = int(info["defaultSampleRate"])
        self._channels = min(int(info["maxInputChannels"]), 2) or 2

        print(f"Audio device: {info['name']} | {self._native_rate} Hz | {self._channels} ch")

        self._audio_ring: deque = deque(maxlen=AUDIO_RING_SECONDS * SAMPLE_RATE)
        self._ring_lock = threading.Lock()
        self._callbacks: list = []

        # Открываем поток БЕЗ callback — blocking mode
        self._chunk_frames = 1024
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._native_rate,
            input=True,
            input_device_index=self._device_index,
            frames_per_buffer=self._chunk_frames,
        )

        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self):
        """Blocking read в thread: читает чанки, ресемплирует, кладёт в ring и кормит callbacks."""
        print("[reader] thread started, will call stream.read()", flush=True)
        chunks_read = 0
        first_reported = False
        last_stats_time = time.monotonic()
        last_rms = 0.0

        while self._running:
            try:
                raw = self._stream.read(self._chunk_frames, exception_on_overflow=False)
            except Exception as e:
                print(f"[reader] READ EXCEPTION: {e}", flush=True)
                traceback.print_exc()
                time.sleep(0.1)
                continue

            chunks_read += 1
            samples = np.frombuffer(raw, dtype=np.int16)

            if not first_reported:
                print(f"[reader] FIRST CHUNK: {len(raw)} bytes, {len(samples)} samples, dtype={samples.dtype}", flush=True)
                first_reported = True

            if self._channels == 2:
                samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)

            if self._native_rate != SAMPLE_RATE:
                from math import gcd
                g = gcd(SAMPLE_RATE, self._native_rate)
                up = SAMPLE_RATE // g
                down = self._native_rate // g
                resampled = resample_poly(samples.astype(np.float32), up, down).astype(np.int16)
            else:
                resampled = samples

            # RMS последнего чанка для debug (до ресемпла не сохраняем — берём оригинал)
            try:
                last_rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            except Exception:
                pass

            with self._ring_lock:
                self._audio_ring.extend(resampled.tolist())
                ring_size = len(self._audio_ring)

            # Статистика каждые 5 сек
            now = time.monotonic()
            if now - last_stats_time >= 5.0:
                print(f"[reader] chunks={chunks_read} ring_samples={ring_size} last_rms={last_rms:.2f}", flush=True)
                last_stats_time = now

            for cb in self._callbacks:
                cb(resampled)

    def get_recent_audio(self, seconds: float) -> np.ndarray:
        """Возвращает последние seconds секунд из ring buffer."""
        n = int(seconds * SAMPLE_RATE)
        with self._ring_lock:
            samples = list(self._audio_ring)[-n:] if len(self._audio_ring) >= n else list(self._audio_ring)
        return np.array(samples, dtype=np.int16)

    def register_callback(self, cb):
        self._callbacks.append(cb)

    def close(self):
        self._running = False
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        try:
            self._pa.terminate()
        except Exception:
            pass


# --- VAD watcher ---

class VadWatcher:
    """
    Слушает поток PCM int16 @ 16000 Hz через webrtcvad.
    Накапливает речевые фреймы в сегмент.
    Когда тишина >= SILENCE_FRAMES_THRESHOLD и сегмент достаточно длинный —
    вызывает on_segment(np.ndarray).
    """

    def __init__(self, on_segment):
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._on_segment = on_segment
        self._frame_bytes = int(SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2
        self._buf = b""
        self._segment: list[np.ndarray] = []
        self._silence_count = 0
        self._last_sent = 0.0
        self._rate_lock = threading.Lock()
        # Счётчики для debug-вывода каждые 3 сек
        self._dbg_voiced = 0
        self._dbg_silent = 0
        self._dbg_last_print = time.monotonic()

    def feed(self, audio_int16: np.ndarray):
        """Принимает chunk PCM int16, кормит VAD по фреймам."""
        self._buf += audio_int16.tobytes()

        while len(self._buf) >= self._frame_bytes:
            frame = self._buf[:self._frame_bytes]
            self._buf = self._buf[self._frame_bytes:]

            try:
                is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
            except Exception as e:
                print(f"VAD error: {e}")
                continue

            if is_speech:
                self._dbg_voiced += 1
            else:
                self._dbg_silent += 1

            frame_np = np.frombuffer(frame, dtype=np.int16)

            if is_speech:
                self._segment.append(frame_np)
                self._silence_count = 0
                # Force-flush по максимальной длине — не ждать паузы от интервьюера
                seg_ms = len(self._segment) * VAD_FRAME_MS
                if seg_ms >= MAX_SEGMENT_SECONDS * 1000:
                    self._flush_segment(reason="max duration")
            else:
                if self._segment:
                    self._silence_count += 1
                    if self._silence_count <= 10:
                        self._segment.append(frame_np)

                if self._silence_count >= SILENCE_FRAMES_THRESHOLD and self._segment:
                    self._flush_segment()

            # Debug-вывод каждые 3 секунды + обновление счётчика в overlay
            now = time.monotonic()
            if now - self._dbg_last_print >= 3.0:
                seg_ms = int(len(self._segment) * VAD_FRAME_MS)
                in_seg = len(self._segment) > 0
                print(
                    f"[VAD] voiced={self._dbg_voiced} silent={self._dbg_silent}"
                    f" in_segment={in_seg} segment_ms={seg_ms}"
                )
                if in_seg:
                    overlay.set_header_safe(f"Interview Assistant v0.6.0 — Слушаю [{seg_ms // 1000}s]")
                self._dbg_voiced = 0
                self._dbg_silent = 0
                self._dbg_last_print = now

    def _flush_segment(self, reason: str = "silence"):
        segment_audio = np.concatenate(self._segment)
        self._segment = []
        self._silence_count = 0

        duration = len(segment_audio) / SAMPLE_RATE
        duration_ms = int(duration * 1000)
        if duration < MIN_SEGMENT_SECONDS:
            print(f"[VAD] segment closed ({reason}): {duration_ms}ms, too short (min {int(MIN_SEGMENT_SECONDS * 1000)}ms), skipped")
            return

        now = time.time()
        with self._rate_lock:
            if now - self._last_sent < RATE_LIMIT_SECONDS:
                print(f"[VAD] segment closed ({reason}): {duration_ms}ms, rate-limited, skipped")
                return
            self._last_sent = now

        print(f"[VAD] segment closed ({reason}): {duration_ms}ms, forwarding to Whisper")
        overlay.set_quick_status_safe("Слушаю...")
        self._on_segment(segment_audio)

    def reset_rate_limit(self) -> None:
        """Сбрасывает rate-limit timestamp. Используется ручным триггером."""
        now = time.time()
        with self._rate_lock:
            self._last_sent = now

    @property
    def is_recording(self) -> bool:
        """True если VAD сейчас накапливает речевой сегмент."""
        return len(self._segment) > 0


# --- Screenshot ring ---

def _get_window_rect(hwnd):
    """Возвращает (left, top, width, height) окна в координатах экрана или None."""
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    try:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        return (rect.left, rect.top, width, height)
    except Exception:
        return None


def _pick_capture_hwnd():
    """Возвращает HWND окна для захвата.

    Приоритет — активное окно (GetForegroundWindow), если оно проходит фильтры;
    иначе — верхнее подходящее в z-порядке сверху вниз.
    Фильтры: видимое, не свёрнутое, с заголовком, площадью не меньше 20% основного
    монитора; пропускаются окна нашего процесса, консоль и окна с
    "Interview Assistant" в заголовке.
    Если кандидатов нет — возвращает None (вызывающий делает снимок всего экрана).
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Типы аргументов/результатов важны: на 64-битном Python HWND по умолчанию
    # приводится к int (c_int) и обрезается — окна не находятся.
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetTopWindow.restype = wintypes.HWND
    user32.GetTopWindow.argtypes = [wintypes.HWND]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleWindow.restype = wintypes.HWND

    GW_HWNDNEXT = 2
    own_pid = os.getpid()
    console_hwnd = kernel32.GetConsoleWindow()
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    min_area = 0.2 * float(screen_w) * float(screen_h)

    def _passes(candidate):
        try:
            if not user32.IsWindowVisible(candidate) or user32.IsIconic(candidate):
                return False
            if console_hwnd and candidate == console_hwnd:
                return False
            title_len = user32.GetWindowTextLengthW(candidate)
            if title_len <= 0:
                return False
            title_buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(candidate, title_buf, title_len + 1)
            if "interview assistant" in title_buf.value.casefold():
                return False
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(candidate, ctypes.byref(pid))
            if pid.value == own_pid:
                return False
            rect = _get_window_rect(candidate)
            if rect is None:
                return False
            if float(rect[2]) * float(rect[3]) < min_area:
                return False
            return True
        except Exception:
            return False

    try:
        foreground = user32.GetForegroundWindow()
    except Exception:
        foreground = None
    if foreground and _passes(foreground):
        return foreground

    hwnd = user32.GetTopWindow(None)
    while hwnd:
        if _passes(hwnd):
            return hwnd
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
    return None


def _capture_window_bitmap(hwnd):
    """Снимает содержимое окна через PrintWindow (PW_RENDERFULLCONTENT).

    В отличие от mss.grab по координатам окна, PrintWindow рисует само окно в
    свой DC, поэтому перекрывающие его окна в кадр не попадают.
    Возвращает JPEG-байты или None, если снять не удалось.
    """
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int

    PW_RENDERFULLCONTENT = 0x00000002
    DIB_RGB_COLORS = 0
    BI_RGB = 0

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    hwnd_dc = None
    mem_dc = None
    bmp = None
    try:
        rect = _get_window_rect(hwnd)
        if rect is None:
            return None
        width, height = rect[2], rect[3]
        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        if not mem_dc:
            return None
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        if not bmp:
            return None
        gdi32.SelectObject(mem_dc, bmp)
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # отрицательная высота = top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(mem_dc, bmp, 0, height, bits,
                           ctypes.byref(bmi), DIB_RGB_COLORS) == 0:
            return None
        img = Image.frombuffer("RGB", (width, height), bits, "raw", "BGRX", 0, 1)
        img.thumbnail((1600, 1200))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=75)
        return out.getvalue()
    except Exception:
        return None
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if hwnd_dc:
            user32.ReleaseDC(hwnd, hwnd_dc)


class ScreenRing:
    """Захватывает кадры экрана каждые SCREEN_INTERVAL_SECONDS в deque(maxlen=5)."""

    def __init__(self):
        self._ring: deque[str] = deque(maxlen=SCREEN_RING_SIZE)
        self._lock = threading.Lock()
        # Режим захвата печатается при первом кадре и при его смене (см. _capture)
        self._last_mode = None
        # Синхронный первый захват до старта loop — чтобы ring не был пустым при первом сегменте
        self._capture_once()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            time.sleep(SCREEN_INTERVAL_SECONDS)
            self._capture_once()

    def _capture_once(self):
        b64 = self._capture()
        with self._lock:
            self._ring.append(b64)

    def _capture(self) -> str:
        hwnd = _pick_capture_hwnd()
        # PrintWindow рисует окно в свой DC — перекрывающие окна в кадр не попадают
        jpeg = _capture_window_bitmap(hwnd) if hwnd else None
        if jpeg is not None:
            mode = "printwindow"
        else:
            # Fallback: обычный захват экрана по rect окна или всего экрана
            with mss.MSS() as sct:
                rect = _get_window_rect(hwnd) if hwnd else None
                if rect is not None:
                    shot = sct.grab({
                        "left": rect[0], "top": rect[1],
                        "width": rect[2], "height": rect[3],
                    })
                    mode = "grab"
                else:
                    shot = sct.grab(sct.monitors[1])
                    mode = "fullscreen"
                img = Image.frombytes("RGB", shot.size, shot.rgb)
                img.thumbnail((1600, 1200))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                jpeg = buf.getvalue()
        if mode != self._last_mode:
            self._last_mode = mode
            print(f"[screen] mode={mode}", flush=True)
        return base64.b64encode(jpeg).decode()

    def get_recent(self, count: int = 3) -> list[str]:
        """Возвращает до count последних кадров."""
        with self._lock:
            frames = list(self._ring)
        if not frames:
            return []
        indices = [-1, -3, -5]
        result = []
        for idx in indices:
            if abs(idx) <= len(frames):
                result.append(frames[idx])
            if len(result) >= count:
                break
        return result


# --- Память Q&A ---

qa_history: deque = deque(maxlen=HISTORY_SIZE)
_qa_lock = threading.Lock()

# --- Состояние F8/F9 ---
sticky_mode: bool = False          # F8 toggle: каждый VAD-сегмент прикрепляет скрин
pending_screen: list[str] = []     # после F9 mode A: attach к следующему сегменту
last_transcript: str = ""          # последний транскрипт для retry (mode B)
_state_lock = threading.Lock()


# --- Pipeline: сегмент -> транскрипт -> dual LLM -> overlay ---

def save_audio_wav(audio_int16: np.ndarray, path: Path) -> None:
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(audio_int16.tobytes())


_JUNK_TRANSCRIPT_RE = re.compile(r"[\d\s\.\-–—,;:]+")


def _is_junk_transcript(text: str) -> bool:
    """
    Мусорный транскрипт Whisper на тишине/шуме: пустышка, одни цифры с точками,
    мало букв или подозрительно много точек. Такой текст нельзя отправлять агентам.
    """
    t = (text or "").strip()
    # Валидный короткий вопрос интервьюера («Что такое ANR?», «Зачем Flow?»)
    # не мусор: оканчивается на «?» и содержит минимум два слова с буквами.
    if t.endswith("?"):
        words = [w for w in t.split() if any(ch.isalpha() for ch in w)]
        if len(words) >= 2:
            return False
    if len(t) < 10:
        return True
    if _JUNK_TRANSCRIPT_RE.fullmatch(t):
        return True
    compact = t.replace(" ", "")
    if compact:
        letters = sum(1 for ch in compact if ch.isalpha())
        if letters / len(compact) < 0.4:
            return True
    if t.count(".") > len(t) / 3:
        return True
    return False


def on_segment(audio: np.ndarray):
    """Вызывается из VAD watcher в отдельном потоке."""
    def run():
        if whisper_model is None:
            overlay.set_quick_status_safe("Whisper ещё загружается...")
            return

        overlay.set_quick_status_safe("Транскрибирую...")

        latest_wav = Path(__file__).parent / "latest_audio.wav"
        try:
            save_audio_wav(audio, latest_wav)
        except Exception:
            pass

        audio_float = audio.astype(np.float32) / 32768.0
        t_ws_start = time.time()
        segments, _ = whisper_model.transcribe(
            audio_float,
            language="ru",
            beam_size=5,
            initial_prompt=WHISPER_PROMPT,
            condition_on_previous_text=False,
            temperature=0,
        )
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        whisper_latency_ms = int((time.time() - t_ws_start) * 1000)

        if not transcript:
            overlay.set_quick_status_safe("Слушаю...")
            return

        # Whisper hallucination filter: паттерн "1.1.1..." на тишине/фоне
        if transcript.count("1.") > 20 or transcript.count(" 1 ") > 15:
            print(f"[whisper] skip hallucination transcript ({len(transcript)} chars)", flush=True)
            return

        # Мусор от Whisper на тишине/шуме: точки, цифры, обрывки без букв
        if _is_junk_transcript(transcript):
            print(f"[whisper] skip junk transcript: {transcript[:40]!r}", flush=True)
            overlay.set_quick_status_safe("Слушаю...")
            return

        # Какие заметки подтянулись — показываем в статусе слота.
        # Только для локальных агентов: в режиме vps заметки в запрос не попадают,
        # поэтому индекс не строим и статус не обещаем.
        _notes_hint = ""
        if _agent_mode() != "vps":
            _notes_hit = search_notes(transcript)
            if _notes_hit["labels"]:
                _notes_hint = f" (заметки: {', '.join(_notes_hit['labels'])})"
        overlay.set_quick_status_safe(f"Спрашиваю...{_notes_hint}")
        overlay.set_deep_status_safe(f"Спрашиваю...{_notes_hint}")

        # Сохраняем транскрипт для возможного retry через F9 mode B/C
        global last_transcript
        with _state_lock:
            last_transcript = transcript

        # Логика screens: sticky > pending > последний кадр из кольца (по умолчанию)
        with _state_lock:
            if sticky_mode:
                screens = screen_ring.get_recent(count=1)
            elif pending_screen:
                screens = list(pending_screen)
                pending_screen.clear()
            else:
                # По умолчанию отдаём самый свежий кадр (обновляется каждые SCREEN_INTERVAL_SECONDS);
                # если кольцо пустое — get_recent вернёт [], промпт скажет «не передан»
                screens = screen_ring.get_recent(count=1)

        if screens:
            latest_path = Path(__file__).parent / "latest_screen.jpg"
            try:
                latest_path.write_bytes(base64.b64decode(screens[0]))
            except Exception:
                pass

        overlay.show_safe()

        ask_dual(transcript, screens, SESSION_ID, whisper_latency_ms=whisper_latency_ms)

    threading.Thread(target=run, daemon=True).start()


def manual_trigger():
    """F9: context-aware диспетчер по F9_MODE (A/B/C/D)."""
    def _do_attach_next():
        """Mode A: сохранить свежий скрин как pending — следующий VAD-сегмент подхватит."""
        fresh = screen_ring.get_recent(count=1)
        with _state_lock:
            pending_screen.clear()
            pending_screen.extend(fresh)
        overlay.set_quick_status_safe("[F9: скрин прикреплён к след. вопросу]")
        print(f"[f9-A] attach_next: pending_screen готов ({len(fresh)} кадр(ов))", flush=True)

    def _do_retry():
        """Mode B: взять last_transcript + свежий скрин, отправить прямо сейчас."""
        with _state_lock:
            tr = last_transcript
        if not tr:
            overlay.set_quick_status_safe("[F9: нет транскрипта для retry]")
            print("[f9-B] retry: last_transcript пуст", flush=True)
            return
        fresh = screen_ring.get_recent(count=1)
        overlay.set_quick_status_safe("[F9: retry с картинкой...]")
        overlay.set_deep_status_safe("Спрашиваю...")
        print(f"[f9-B] retry: transcript='{tr[:60]}' screens={len(fresh)}", flush=True)
        if fresh:
            latest_path = Path(__file__).parent / "latest_screen.jpg"
            try:
                latest_path.write_bytes(base64.b64decode(fresh[0]))
            except Exception:
                pass
        vad_watcher.reset_rate_limit()
        overlay.show_safe()
        # whisper_latency_ms=None: retry не делает транскрипцию, используем last_transcript
        ask_dual(tr, fresh, SESSION_ID, whisper_latency_ms=None)

    def run():
        mode = F9_MODE

        # Mode C: context-aware — если VAD сейчас пишет → attach_next, иначе retry
        if mode == "C":
            if vad_watcher.is_recording:
                print("[f9-C] is_recording=True → attach_next", flush=True)
                _do_attach_next()
            else:
                print("[f9-C] is_recording=False → retry", flush=True)
                _do_retry()
            return

        # Mode A: всегда attach_next
        if mode == "A":
            _do_attach_next()
            return

        # Mode B: всегда retry
        if mode == "B":
            _do_retry()
            return

        # Mode D: screen_only — пустой транскрипт, свежий скрин
        if mode == "D":
            fresh = screen_ring.get_recent(count=1)
            overlay.set_quick_status_safe("[F9: screen-only...]")
            overlay.set_deep_status_safe("Спрашиваю...")
            print(f"[f9-D] screen_only: screens={len(fresh)}", flush=True)
            if fresh:
                latest_path = Path(__file__).parent / "latest_screen.jpg"
                try:
                    latest_path.write_bytes(base64.b64decode(fresh[0]))
                except Exception:
                    pass
            vad_watcher.reset_rate_limit()
            overlay.show_safe()
            # whisper_latency_ms=None: screen-only не делает транскрипцию
            ask_dual("", fresh, SESSION_ID, whisper_latency_ms=None)

    threading.Thread(target=run, daemon=True).start()


# --- DualOverlay: одно окно с двумя вертикальными зонами ---

class DualOverlay:
    """
    Одно полупрозрачное окно без рамки с тремя вертикальными зонами:
    слева, по центру и справа — по одному агенту в каждой.
    """

    def __init__(self, width: int = 2000, height: int = 450, position_y: int = 30):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#1e1e1e")

        sw = self.root.winfo_screenwidth()
        width = min(width, sw - 20)  # clamp k shirine ekrana
        x = max(0, (sw - width) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{position_y}")

        # Header (drag-handle) — содержит title и кнопку закрытия
        header_frame = tk.Frame(self.root, bg="#2a2a2a")
        header_frame.pack(fill="x", side="top")

        self._header = tk.Label(
            header_frame,
            text="Interview Assistant v0.6.0 — Загружаю Whisper...",
            font=("Segoe UI", 12, "bold"),
            fg="white", bg="#2a2a2a",
            cursor="fleur",
            anchor="w",
            padx=10,
        )
        self._header.pack(fill="x", side="left", expand=True)
        self._header.bind("<ButtonPress-1>", self._on_drag_start)
        self._header.bind("<B1-Motion>", self._on_drag_motion)

        # Кнопка закрытия — завершает весь процесс
        self._btn_close = tk.Label(
            header_frame,
            text="✕",
            font=("Segoe UI", 14, "bold"),
            fg="white", bg="#2a2a2a",
            cursor="hand2",
            padx=10,
        )
        self._btn_close.pack(side="right")
        self._btn_close.bind("<ButtonPress-1>", lambda e: self._close_app())
        self._btn_close.bind("<Enter>", lambda e: self._btn_close.config(fg="red", bg="#a00000"))
        self._btn_close.bind("<Leave>", lambda e: self._btn_close.config(fg="white", bg="#2a2a2a"))

        self.root.bind("<Escape>", lambda e: self._close_app())

        # Body container: две вертикальные зоны
        body_frame = tk.Frame(self.root, bg="#1e1e1e")
        body_frame.pack(fill="both", expand=True)

        # Левая зона (первый слот)
        left = tk.Frame(body_frame, bg="#1e1e1e")
        left.pack(side="left", fill="both", expand=True, padx=(10, 5), pady=10)

        self._quick_title = tk.Label(
            left, text=f"{_slot_label('quick', 'Gemini Flash-Lite')}",
            font=("Segoe UI", 11, "bold"),
            fg="#88ccff", bg="#1e1e1e", anchor="w",
        )
        self._quick_title.pack(fill="x")

        self._quick_body = scrolledtext.ScrolledText(
            left,
            font=("Segoe UI", 12),
            fg="white", bg="#1e1e1e",
            wrap="word",
            width=1,
            borderwidth=0, highlightthickness=0,
        )
        self._quick_body.pack(fill="both", expand=True)
        # Readonly: запрещаем редактирование, но разрешаем выделение и Ctrl+C
        self._quick_body.bind("<Key>", lambda e: "break")
        self._quick_body.bind("<<Paste>>", lambda e: "break")
        self._quick_body.bind("<<Cut>>", lambda e: "break")
        self._quick_body.bind("<Control-c>", lambda e: None)
        self._quick_body.bind("<Control-C>", lambda e: None)

        # Центральная зона (второй слот)
        right = tk.Frame(body_frame, bg="#1e1e1e")
        right.pack(side="left", fill="both", expand=True, padx=(5, 5), pady=10)

        self._deep_title = tk.Label(
            right, text=f"{_slot_label('deep', 'Gemini Flash')}",
            font=("Segoe UI", 11, "bold"),
            fg="#ffcc88", bg="#1e1e1e", anchor="w",
        )
        self._deep_title.pack(fill="x")

        self._deep_body = scrolledtext.ScrolledText(
            right,
            font=("Segoe UI", 12),
            fg="white", bg="#1e1e1e",
            wrap="word",
            width=1,
            borderwidth=0, highlightthickness=0,
        )
        self._deep_body.pack(fill="both", expand=True)
        # Readonly: запрещаем редактирование, но разрешаем выделение и Ctrl+C
        self._deep_body.bind("<Key>", lambda e: "break")
        self._deep_body.bind("<<Paste>>", lambda e: "break")
        self._deep_body.bind("<<Cut>>", lambda e: "break")
        self._deep_body.bind("<Control-c>", lambda e: None)
        self._deep_body.bind("<Control-C>", lambda e: None)

        # Правая зона (третий слот)
        third = tk.Frame(body_frame, bg="#1e1e1e")
        third.pack(side="left", fill="both", expand=True, padx=(5, 10), pady=10)

        self._third_title = tk.Label(
            third, text=f"{_slot_label('third', 'Claude')}",
            font=("Segoe UI", 11, "bold"),
            fg="#aaffaa", bg="#1e1e1e", anchor="w",
        )
        self._third_title.pack(fill="x")

        self._third_body = scrolledtext.ScrolledText(
            third,
            font=("Segoe UI", 12),
            fg="white", bg="#1e1e1e",
            wrap="word",
            width=1,
            borderwidth=0, highlightthickness=0,
        )
        self._third_body.pack(fill="both", expand=True)
        # Readonly: запрещаем редактирование, но разрешаем выделение и Ctrl+C
        self._third_body.bind("<Key>", lambda e: "break")
        self._third_body.bind("<<Paste>>", lambda e: "break")
        self._third_body.bind("<<Cut>>", lambda e: "break")
        self._third_body.bind("<Control-c>", lambda e: None)
        self._third_body.bind("<Control-C>", lambda e: None)

        # Строка состояния F8/F9 (под панелями ответов, поверх footer)
        self._f_status = tk.Label(
            self.root,
            text="[F9: READY]  [F8: STICKY OFF]",
            font=("Segoe UI", 10),
            fg="#aaaaaa", bg="#2a2a2a",
            anchor="w",
            padx=10,
        )
        self._f_status.pack(fill="x", side="bottom")

        # Приветственная строка — отображается до первого вопроса
        welcome = "VAD слушает. F9 — ручной триггер | F8 — sticky скрин.\n"
        self._quick_body.insert("1.0", welcome)
        self._deep_body.insert("1.0", welcome)
        self._third_body.insert("1.0", welcome)

        self._visible = True
        self._drag_start_x = 0
        self._drag_start_y = 0

    def _on_drag_start(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _on_drag_motion(self, event):
        x = self.root.winfo_pointerx() - self._drag_start_x
        y = self.root.winfo_pointery() - self._drag_start_y
        self.root.geometry(f"+{x}+{y}")

    def set_header(self, text: str) -> None:
        self._header.config(text=text)

    def set_quick_status(self, text: str) -> None:
        self._quick_title.config(
            text=f"{_slot_label('quick', 'Gemini')}  |  {text}"
        )

    def set_deep_status(self, text: str) -> None:
        self._deep_title.config(
            text=f"{_slot_label('deep', 'Gemini Flash')}  |  {text}"
        )

    def _append_quick_qa_inner(self, transcript: str, answer: str, latency_ms=None) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        lat_str = f" ({latency_ms}ms)" if latency_ms else ""
        entry = f"\n─── {ts}{lat_str} ────\n❓ {transcript}\n─\n{answer}\n"
        self._quick_body.insert("end", entry)
        self._quick_body.see("end")

    def _append_deep_qa_inner(self, transcript: str, answer: str, latency_ms=None) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        lat_str = f" ({latency_ms}ms)" if latency_ms else ""
        entry = f"\n─── {ts}{lat_str} ────\n❓ {transcript}\n─\n{answer}\n"
        self._deep_body.insert("end", entry)
        self._deep_body.see("end")

    def show(self) -> None:
        self._visible = True
        self.root.deiconify()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

    def hide(self) -> None:
        self._visible = False
        self.root.withdraw()

    def _close_app(self) -> None:
        """Корректно закрывает overlay и завершает весь Python-процесс."""
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def toggle_visible(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def set_header_safe(self, text: str) -> None:
        self.root.after(0, lambda: self.set_header(text))

    def set_quick_status_safe(self, text: str) -> None:
        self.root.after(0, lambda: self.set_quick_status(text))

    def set_deep_status_safe(self, text: str) -> None:
        self.root.after(0, lambda: self.set_deep_status(text))

    def append_quick_qa_safe(self, transcript: str, answer: str, latency_ms=None) -> None:
        self.root.after(0, lambda: self._append_quick_qa_inner(transcript, answer, latency_ms))

    def append_deep_qa_safe(self, transcript: str, answer: str, latency_ms=None) -> None:
        self.root.after(0, lambda: self._append_deep_qa_inner(transcript, answer, latency_ms))

    def set_third_status(self, text: str) -> None:
        self._third_title.config(
            text=f"{_slot_label('third', 'Claude')}  |  {text}"
        )

    def _append_third_qa_inner(self, transcript: str, answer: str, latency_ms=None) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        lat_str = f" ({latency_ms}ms)" if latency_ms else ""
        entry = f"\n─── {ts}{lat_str} ────\n❓ {transcript}\n─\n{answer}\n"
        self._third_body.insert("end", entry)
        self._third_body.see("end")

    def set_third_status_safe(self, text: str) -> None:
        self.root.after(0, lambda: self.set_third_status(text))

    def append_third_qa_safe(self, transcript: str, answer: str, latency_ms=None) -> None:
        self.root.after(0, lambda: self._append_third_qa_inner(transcript, answer, latency_ms))

    def show_safe(self) -> None:
        self.root.after(0, self.show)

    def set_f_status(self, text: str) -> None:
        self._f_status.config(text=text)

    def set_f_status_safe(self, text: str) -> None:
        self.root.after(0, lambda: self.set_f_status(text))


# Единственный экземпляр окна
overlay = DualOverlay()


# --- HTTP control server ---

class ControlHandler(http.server.BaseHTTPRequestHandler):
    def _check_auth(self):
        if self.headers.get("X-Agent-Token", "") != AGENT_TOKEN:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return False
        return True

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _file(self, path, content_type):
        if not path.exists():
            self._json(404, {"error": "not_found", "path": str(path)})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._check_auth():
            return
        path = self.path.split("?")[0]
        if path == "/status":
            vad_in_seg = len(vad_watcher._segment) > 0 if "vad_watcher" in globals() else False
            self._json(200, {
                "version": "v0.6.0",
                "pid": os.getpid(),
                "session_id": SESSION_ID,
                "uptime_sec": int(time.time() - STARTED_AT),
                "vad_state": "recording" if vad_in_seg else "listening",
            })
        elif path == "/latest_screen":
            self._file(Path(__file__).parent / "latest_screen.jpg", "image/jpeg")
        elif path == "/latest_audio":
            self._file(Path(__file__).parent / "latest_audio.wav", "audio/wav")
        elif path == "/log":
            try:
                n = int(self.path.split("tail=")[-1]) if "tail=" in self.path else 10
            except ValueError:
                n = 10
            log_path = CLIENT_LOG
            if not log_path.exists():
                self._json(404, {"error": "log not found"})
                return
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self._json(200, {"lines": lines[-n:], "total": len(lines)})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if not self._check_auth():
            return
        path = self.path.split("?")[0]
        if path == "/trigger":
            overlay.root.after(0, lambda: _manual_trigger_safe())
            self._json(200, {"status": "triggered"})
        elif path == "/show":
            overlay.show_safe()
            self._json(200, {"status": "shown"})
        elif path == "/hide":
            overlay.root.after(0, overlay.hide)
            self._json(200, {"status": "hidden"})
        elif path == "/clear":
            def _clear():
                overlay._quick_body.delete("1.0", "end")
                overlay._deep_body.delete("1.0", "end")
            overlay.root.after(0, _clear)
            self._json(200, {"status": "cleared"})
        elif path == "/invalidate_context":
            # Принудительный сброс кэша context.md — следующий get_context() перечитает файл
            with _context_lock:
                global _context_mtime
                _context_mtime = 0.0
            # Сразу перечитываем, чтобы новый контекст загрузился немедленно
            new_ctx = get_context()
            self._json(200, {"status": "invalidated", "new_context_chars": len(new_ctx)})
        elif path == "/shutdown":
            overlay.root.after(0, lambda: overlay.set_header("shutting down..."))
            overlay.root.after(500, lambda: sys.exit(0))
            self._json(200, {"status": "shutting_down"})
        else:
            self._json(404, {"error": "not_found"})

    def log_message(self, format, *args):
        print(f"[control] {format % args}")


def _manual_trigger_safe():
    """Вызов manual_trigger из control-server (проверяет наличие модели)."""
    if whisper_model is None:
        overlay.set_quick_status_safe("Whisper ещё загружается...")
        return
    manual_trigger()


def start_control_server():
    server = socketserver.ThreadingTCPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    server.daemon_threads = True
    print(f"[control] HTTP server listening on 0.0.0.0:{CONTROL_PORT}")
    print(f"[control] Agent token: {AGENT_TOKEN}")
    server.serve_forever()


threading.Thread(target=start_control_server, daemon=True).start()


# --- Отложенная загрузка Whisper ---

whisper_model = None


def _setup_cuda_dll_paths():
    import os
    import site
    # Добавляем пути к nvidia DLL из pip-пакетов (cublas, cudnn, cuda_runtime)
    _all_site = list(site.getsitepackages())
    try:
        _all_site.append(site.getusersitepackages())
    except Exception:
        pass
    for _pkg in ['nvidia\\cublas\\bin', 'nvidia\\cuda_runtime\\bin', 'nvidia\\cudnn\\bin', 'nvidia\\cuda_nvrtc\\bin']:
        for _site_dir in _all_site:
            _p = os.path.join(_site_dir, _pkg)
            if os.path.isdir(_p):
                try:
                    os.add_dll_directory(_p)
                    print(f"[dll] added {_p}")
                except Exception as _e:
                    print(f"[dll] WARN: {_p}: {_e}")


def _load_whisper():
    global whisper_model
    from faster_whisper import WhisperModel
    overlay.set_header_safe(f"Interview Assistant v0.6.0 — Загружаю Whisper '{WHISPER_MODEL_SIZE}'...")
    print(f"Loading Whisper '{WHISPER_MODEL_SIZE}' on CUDA float16...")
    whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="float16")
    print("Whisper ready (device=cuda, compute_type=float16).")
    overlay.set_header_safe("Interview Assistant v0.6.0 — Слушаю")
    overlay.set_quick_status_safe("Слушаю...")
    overlay.set_deep_status_safe("Готов...")


# DLL-пути CUDA настраиваем и torch прогреваем в главном потоке:
# иначе потоковая загрузка Whisper оставляет torch в sys.modules наполовину
# инициализированным, и scipy в аудио-потоке падает на getattr(torch, 'Tensor').
_setup_cuda_dll_paths()
import torch  # noqa: F401 — прогреваем torch в главном потоке до старта аудио-потока

threading.Thread(target=_load_whisper, daemon=True).start()
# Настройку агентов (поиск/замер/выбор/запись agents.json) выполняет внешний агент;
# клиент только читает конфиг (сделано синхронно выше) и вызывает указанных агентов.


# --- Запуск ---

_ensure_agent_work_dir()
screen_ring = ScreenRing()

recorder = LoopbackRecorder()
vad_watcher = VadWatcher(on_segment=on_segment)
recorder.register_callback(vad_watcher.feed)


def _toggle_overlay():
    overlay.root.after(0, overlay.toggle_visible)


def _manual_trigger():
    if whisper_model is None:
        overlay.set_quick_status_safe("Whisper ещё загружается...")
        return
    manual_trigger()


def _toggle_sticky():
    global sticky_mode
    with _state_lock:
        sticky_mode = not sticky_mode
        status = "ON" if sticky_mode else "OFF"
    label = f"[F9: F9_MODE={F9_MODE}]  [F8: STICKY {status}]"
    overlay.set_f_status_safe(label)
    print(f"[f8] sticky_mode={sticky_mode}", flush=True)


hotkeys = pynput_keyboard.GlobalHotKeys({
    "<f10>": _toggle_overlay,
    "<f9>": _manual_trigger,
    "<f8>": _toggle_sticky,
})
hotkeys.start()

print(f"Assistant v0.6.0 running. Session: {SESSION_ID}")
print(f"  Системный звук: WASAPI loopback")
print(f"  VAD: aggressiveness={VAD_AGGRESSIVENESS}, пауза {SILENCE_FRAMES_THRESHOLD * VAD_FRAME_MS / 1000:.1f}s -> отправка")
print(f"  Три слота: 1={_slot_label('quick', 'Gemini Flash-Lite')} | "
      f"2={_slot_label('deep', 'Gemini Flash')} | 3={_slot_label('third', 'Claude')} "
      f"(mode={_agent_mode()})")
print(f"  Deep endpoint: {DEEP_ENDPOINT}")
print(f"  Control API: http://0.0.0.0:{CONTROL_PORT}  (token saved to .agent_token)")
print(f"  F10 -- показать/скрыть overlay")
print(f"  F9  -- context-aware триггер (F9_MODE={F9_MODE}): A=attach_next B=retry C=auto D=screen_only")
print(f"  F8  -- toggle sticky-скрин (каждый VAD-сегмент прикрепляет скрин)")
print(f"  Esc в overlay -- скрыть")
print(f"  Ctrl+C -- выход")
print(f"  Лог: {CLIENT_LOG}")

try:
    overlay.root.mainloop()
finally:
    hotkeys.stop()
    recorder.close()
