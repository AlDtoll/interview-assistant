# Interview Assistant

Local interview helper for Windows: it captures system audio, transcribes speech with Whisper, sends the question plus screenshots to up to three LLM agents, and shows their answers in an always-on-top overlay.

> **Приватность:** утилита слушает **системный звук** (то, что звучит в колонках/наушниках), делает **скриншоты активного окна** и отправляет транскрипт и кадры выбранным LLM-провайдерам. Используйте только там, где это законно и допустимо правилами собеседования/встречи.

## Что делает

- **Захват системного звука** через WASAPI loopback (Windows) — слышит голос собеседника из Zoom/Meet/Teams без виртуальных кабелей.
- **VAD** (voice activity detection) на WebRTC VAD: речь → пауза ~400 мс → сегмент уходит в обработку.
- **Whisper** (`faster-whisper`, локально) распознаёт речь в текст. GPU (CUDA) опционален, на CPU тоже работает, но медленнее.
- **Три слота агентов**: конфиг `agents.json` описывает до трёх агентов (HTTP API или локальный CLI) — они вызываются параллельно.
- **Оверлей** — окно поверх остальных приложений, draggable, можно скрыть.
- **Кольцевой буфер скриншотов** — кадры активного окна снимаются в фоне, но по умолчанию к запросу **не** прикладываются: только по F8 (sticky, каждый сегмент) или F9 (один раз/по режиму F9_MODE).
- **Память** — последние 5 пар «вопрос → ответ» в контексте.
- **Контекст вакансии/темы** — необязательный `context.md` рядом с утилитой (игнорируется git-ом).
- **Логи сессии** — `interview_logs/session_<ID>_client.jsonl`.
- **HTTP control API** на `127.0.0.1:17777` (см. ниже).

## Требования

- **Windows 10/11** (WASAPI loopback — Windows API; Linux/macOS не поддерживаются).
- **Python 3.11**.
- **CUDA** — опционально, для быстрого Whisper (`device="cuda"`, `compute_type="float16"`).
- **Прокси** — опционально. Если провайдеры LLM недоступны напрямую, можно поднять локальный HTTP-прокси (например, v2rayN на `127.0.0.1:10808`) и выставить `HTTP_PROXY`/`HTTPS_PROXY` — это уже прописано в `run_assistant.bat`, порт при необходимости меняется.

## Установка

```powershell
git clone https://github.com/AlDtoll/interview-assistant.git
cd interview-assistant
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Первый запуск скачает модель Whisper (`large-v3-turbo`, около 1.5 ГБ); модель меняется константой `WHISPER_MODEL_SIZE`.

## Настройка

1. Скопируйте примеры конфигов:

   ```powershell
   copy agents.example.json agents.json
   copy secrets.example.env secrets.env
   ```

2. Заполните `secrets.env` ключами, которые реально используете (пустые строки можно оставить пустыми):

   ```
   OPENROUTER_KEY=...
   INTERVIEW_DEEP_TOKEN=...
   DEEPSEEK_API_KEY=...
   CURSOR_API_KEY=...
   ```

3. Поправьте `agents.json` под свои установленные харнессы: замените `C:\Users\<you>\...` на реальные пути, либо используйте имена команд из `PATH`. Подробная инструкция по выбору и замеру агентов — в [AGENTS.md](AGENTS.md).

4. (Опционально) положите текст о вакансии/стеке в `context.md` рядом с `assistant.py` — утилита подхватит его в системный промпт. Файл в `.gitignore`.

## Project notes

Создайте папку `notes/` рядом с `assistant.py` и положите туда `.md`/`.txt` файлы (рекурсивно): спеку проекта, вопросы-ответы, шпаргалки. Для каждого вопроса утилита ищет по словам в заметках и подмешивает 0-2 релевантных фрагмента в запрос (порог релевантности — `NOTES_MIN_SCORE`, число фрагментов — `NOTES_TOP_K`); если ничего не прошло порог, блок не добавляется. В статусе слота видно, какие файлы подтянулись.

Заметки подмешиваются только агентам, которых утилита запускает локально (режимы `local`/`local_fallback_vps`). В режиме `vps` вопрос уходит напрямую на VPS без заметок, поэтому индекс не строится и хинт в статусе не показывается.

Поиск лексический (TF-IDF по словам от 3 символов), без эмбеддингов, поэтому формулировки в заметках лучше держать близкими к реальным вопросам. Если папки `notes/` нет или она пуста, режим выключен и поведение прежнее.

Модель Whisper по умолчанию — `large-v3-turbo`; меняется константой `WHISPER_MODEL_SIZE` в начале `assistant.py`.

## Запуск

```powershell
python assistant.py
```

или двойным кликом по `run_assistant.bat` (он читает `secrets.env` и выставляет прокси).

## Горячие клавиши

| Клавиша | Действие |
|---|---|
| **F10** | Показать / скрыть оверлей |
| **F9** | Ручной триггер: взять последние 15 сек аудио и отправить немедленно |
| **F8** | Вкл/выкл «sticky screen»: прикреплять скриншот к каждому VAD-сегменту |
| **Esc** в оверлее | Скрыть оверлей |
| **Ctrl+C** в терминале | Выход |

## Как выбрать агентов

Конфиг `agents.json`:

```json
{
  "mode": "local",
  "selected": [
    {"name": "deepseek", "type": "deepseek", "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY"},
    {"name": "codex", "type": "cli", "command": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"], "prompt_via": "stdin"},
    {"name": "cursor-agent", "type": "cli", "command": ["cursor-agent", "-p", "--trust"], "prompt_via": "stdin"}
  ]
}
```

Поддерживаются:

- **deepseek** — HTTP к `api.deepseek.com` (нужен `DEEPSEEK_API_KEY`). Поле `max_tokens` необязательное: если не задано, быстрый слот (`quick`) ограничен дефолтом в 140 токенов, а `deep`/`third` уходят без ограничения;
- **codex** — локальный CLI (`codex exec ... -`);
- **cursor-agent** — локальный CLI (`cursor-agent -p ...`);
- **opencode** — локальный CLI (`opencode run`);
- **claude** — локальный CLI (`claude -p`, требует подписки).

Режимы `mode`:

- `local` — только локальные агенты (пустой слот = ошибка);
- `vps` — удалённый эндпоинт (нужны `DEEP_ENDPOINT` и `INTERVIEW_DEEP_TOKEN`);
- `local_fallback_vps` — сначала локально, при сбое — на удалённый эндпоинт.

## Схема работы

```
WASAPI loopback (системный звук)
        │
        ▼
   VAD (WebRTC)  ── пауза ≥ 400 мс ──┐
        │                            │
        ▼                            │
   Whisper (faster-whisper, локально)│
        │                            │
        ▼                            ▼
   ring buffer скриншотов ──► три агента (agents.json)
                                     │
                                     ▼
                              оверлей поверх окон
```

## HTTP Control API

Порт **17777**. При старте генерируется токен, печатается в stdout и сохраняется в `.agent_token` рядом с `assistant.py`. Все запросы требуют заголовок `X-Agent-Token`.

```bash
TOKEN=$(cat .agent_token)
BASE="http://127.0.0.1:17777"

curl -H "X-Agent-Token: $TOKEN" $BASE/status
curl -X POST -H "X-Agent-Token: $TOKEN" $BASE/trigger
curl -X POST -H "X-Agent-Token: $TOKEN" $BASE/show
curl -X POST -H "X-Agent-Token: $TOKEN" $BASE/hide
curl -X POST -H "X-Agent-Token: $TOKEN" $BASE/clear
curl -H "X-Agent-Token: $TOKEN" $BASE/latest_screen -o screen.jpg
curl -H "X-Agent-Token: $TOKEN" $BASE/latest_audio -o audio.wav
curl -H "X-Agent-Token: $TOKEN" "$BASE/log?tail=20"
curl -X POST -H "X-Agent-Token: $TOKEN" $BASE/shutdown
```

API слушает `0.0.0.0:17777`. Если порт не нужен снаружи — ограничьте доступ файрволом.

## Ограничения

- Только **Windows**.
- Vision — **1 скриншот** на запрос (последний кадр кольцевого буфера: sticky/pending берут `get_recent(count=1)`).
- История — **5** пар Q&A (`HISTORY_SIZE`).
- Whisper на CPU — **2–4 сек** на сегмент; на GPU заметно быстрее.
- Спекулятивные черновики выключены по умолчанию (экспериментально: на замерах давали 4.7 с против 3.2 с финала); включить — переменная окружения `MVP_SPEC=1`.
- VAD не различает языки и не отделяет речь собеседника от фонового видео/музыки.
- `DEEP_ENDPOINT`-режим требует собственного сервера-прокси; без него используйте локальных агентов.
- Оверлей может попадать в screen share — скрывайте его (F10) при демонстрации экрана.

## Приватность

- Утилита захватывает **всё, что звучит в системе**, и периодически снимает **активное окно**.
- Транскрипт и скриншоты отправляются тем LLM-провайдерам/CLI, которых вы указали в `agents.json`.
- Логи и медиа (`interview_logs/`, `*.wav`, `*.jpg`, `.agent_token`, `secrets.env`, `context.md`) исключены из git через `.gitignore` — проверяйте это, если форкаете репозиторий.
- Не используйте утилиту для записи людей без их согласия и в нарушение правил площадки, на которой проходит собеседование.

## Лицензия

MIT — см. [LICENSE](LICENSE).
