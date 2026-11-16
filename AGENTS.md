# AGENTS.md — как настроить харнессы для Interview Assistant

Эта инструкция — для человека или агента, который настраивает `agents.json`: находит установленные LLM-харнессы, замеряет их скорость и записывает конфиг. Сама утилита этого не делает.

## Что делает утилита

- захватывает системный звук и микрофон;
- распознаёт речь через Whisper;
- читает `agents.json` и отправляет вопрос указанным агентам;
- показывает до трёх ответов в отдельных окнах.

Утилита не ищет и не замеряет харнессы — это задача настройщика.

## 1. Найдите, что установлено

```powershell
where.exe claude
where.exe cursor-agent
where.exe codex
where.exe opencode
```

Кандидаты:

| Агент | Команда | Примечание |
|---|---|---|
| deepseek | HTTP к `api.deepseek.com/chat/completions` | нужен `DEEPSEEK_API_KEY` |
| codex | `codex exec --skip-git-repo-check --sandbox read-only -` | подписка ChatGPT |
| cursor-agent | `cursor-agent -p --trust` | подписка Cursor |
| opencode | `opencode run` | проверьте `opencode --help` |
| claude | `claude -p` | требует подписки Claude Code |

Промпт **всегда** передаётся через stdin, никогда аргументом: на Windows `.cmd`-шимы исполняются через `cmd.exe`, где `&`, `|`, `%` в тексте речи могут сломать или подменить команду.

## 2. Замерьте скорость

Отправьте короткий вопрос через stdin и засеките время:

```powershell
"Ответь одним словом: ок" | codex exec --skip-git-repo-check --sandbox read-only -
```

Ориентир: 1–5 секунд — годится для быстрых подсказок, 10+ — только для глубоких ответов.

## 3. Запишите `agents.json`

Скопируйте `agents.example.json` в `agents.json` и замените пути/команды на реальные. Максимум три слота.

```json
{
  "mode": "local",
  "selected": [
    {"name": "deepseek", "type": "deepseek", "model": "deepseek-chat"},
    {"name": "codex", "type": "cli", "command": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"], "prompt_via": "stdin"},
    {"name": "cursor-agent", "type": "cli", "command": ["cursor-agent", "-p", "--trust"], "prompt_via": "stdin"}
  ],
  "benchmarked_at": "2026-01-01T00:00:00"
}
```

Режимы:

- `local` — только локальные агенты, пустой слот = ошибка;
- `vps` — через удалённый сервер (`DEEP_ENDPOINT` + `INTERVIEW_DEEP_TOKEN`);
- `local_fallback_vps` — локально, при сбое — на удалённый сервер.

Поля deepseek-агента:

- `model` — имя модели (`deepseek-chat`, `deepseek-flash`, ...);
- `api_key_env` — имя переменной окружения с ключом (по умолчанию `DEEPSEEK_API_KEY`);
- `max_tokens` — необязательный лимит длины ответа. Если задан явно, применяется к слоту этого агента. Если не задан: быстрый слот (`quick`) получает дефолт **140** токенов, а `deep`/`third` уходят **без ограничения** — иначе глубокий ответ обрезается. Пример: `"max_tokens": 600`. Нечисловое, нулевое и отрицательное значение не отправляется (провайдер отвечает на него 400), в stdout печатается предупреждение.
- `thinking_disabled` — `true` отключает размышления reasoning-модели (тело запроса получает `thinking: {"type": "disabled"}`). По умолчанию отключение включается автоматически только для быстрого слота при `api_url` на `deepseek.com`. Если используется собственный relay к DeepSeek, домен другой — задайте поле явно.

Для CLI-агентов, которым нужны абсолютные пути (например, `.cmd`-шимы из `%APPDATA%`), используйте свои пути. Пример:

```json
{"name": "cursor-agent", "type": "cli", "command": ["C:\\Users\\<you>\\AppData\\Local\\cursor-agent\\cursor-agent.cmd", "-p", "--trust"], "prompt_via": "stdin"}
```

## 4. Системный промпт и контекст

Базовый системный промпт задаётся в утилите (`_SYSTEM_PROMPT_BASE`). Дополнительный контекст (вакансия, стек, тема встречи) кладётся в `context.md` рядом с `assistant.py` — файл подхватывается автоматически и исключён из git.

## Проверка перед запуском

```powershell
python --version
python -c "import torch; print(torch.cuda.is_available())"
dir agents.json, secrets.env
```

## Частые проблемы

- **Пустые окна** — имя агента и команда в `agents.json` должны совпадать с установленными.
- **«Ошибка claude: требует подписки»** — без подписки Claude CLI не работает, замените агента.
- **Whisper путает термины** — дополните `WHISPER_PROMPT` в `assistant.py` своими терминами.
- **GPU не виден** — нужны `nvidia-cublas-cu12` и `nvidia-cudnn-cu12` (либо работайте на CPU).

## Правила правки

Перед изменением `assistant.py` сделайте резервную копию `.bak-<дата>`.
