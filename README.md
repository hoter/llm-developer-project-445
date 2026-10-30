# AI-оператор для сайта мобильного оператора связи

[![hexlet-check](https://github.com/hoter/llm-developer-project-445/actions/workflows/hexlet-check.yml/badge.svg)](https://github.com/hoter/llm-developer-project-445/actions)

Соберите агентный чат для сайта мобильного оператора. Он отвечает абоненту на вопросы о тарифе, списаниях и правилах со ссылками на базу знаний, а смену тарифа, услуг и контактов проводит через карточку подтверждения. По пути освоите оркестрацию нескольких агентов на OpenAI Agents SDK, MCP-серверы с JWT, контракты действий с идемпотентностью, журнал аудита, права через политики Dogwood и guardrail-сервис, при отказе которого действия закрываются.

Учебный проект Хекслета: https://ru.hexlet.io/programs/llm-developer
Как это должно работать: https://files.hexlet.app/a/i3355x

## Стек

- Python 3.12, FastAPI/Starlette, uvicorn
- OpenAI Agents SDK, MCP (`mcp`), JWT RS256 (`pyjwt`)
- PostgreSQL 16 + pgvector (docker compose)

## Установка

Для запуска нужен Docker и Python 3.12.

```bash
git clone https://github.com/hoter/llm-developer-project-445.git
cd llm-developer-project-445

python3.12 -m venv .venv
source .venv/bin/activate
make install

cp .env.example .env   # заполните LLM_BASE_URL, LLM_MODEL, LLM_API_KEY

make infra             # Postgres :5434
make migrate && make seed
```

## Контур

```
веб-чат  ──  бэкенд с оркестратором :8000   (шаг 2)
                │
    action-MCP :8220 · PDP :9100 · guardrail :8300
                │
стенд :8100  (make stand)
  /token, /jwks.json             издатель JWT (RS256), aud = адрес сервера
  /crm/mcp /billing/mcp /kb/mcp  MCP-серверы чтения
  /mock/...                      REST-моки CRM, биллинга, тикетов
  /  /operator.html              веб-чат и страница оператора
                │
PostgreSQL :5434 (pgvector) — состояние всех сервисов
```

| Процесс      | Порт | Команда          | Что внутри                        |
|--------------|------|------------------|-----------------------------------|
| стенд        | 8100 | `make stand`     | издатель JWT, read-MCP, моки, веб |
| бэкенд       | 8000 | `make api`       | оркестратор, агенты               |
| action-MCP   | 8220 | `make actions`   | контракты действий                |
| PDP          | 9100 | `make pdp`       | политики прав (Dogwood)           |
| guardrail    | 8300 | `make guardrail` | проверка входов и выходов         |

Демо-пользователи: `anna` — клиент с правом менять тариф, `boris` — клиент
только на чтение, `olga` — оператор поддержки.

## Как устроен ход

Бэкенд собирает ответ из независимых агентов, а решения принимает код:

1. **Планировщик** (`app/planner.py`) — агент со структурированным выходом
   `Plan`: шаги с агентом, задачей и пометкой «действие».
2. **Гейт** (`planner.gate`) — до исполнения отбрасывает шаги неизвестных
   агентов и снимает `action` у агентов чтения. Отброшенное видно в событии
   `plan` (`dropped`) и в трейсе сессии.
3. **Доменные агенты** (`app/workers.py`) — у каждого только инструменты
   своего домена: `data` (профиль, тариф, списания), `kb` (поиск по базе
   знаний), `support` (карточка абонента, только оператору).
4. **Редактор** (`app/editor.py`) — единственный, чей текст видит абонент;
   правила из базы называет с источником.

Изменения (тариф, услуга, контакты) идут через отдельный шлюз — action-MCP:

1. `prepare_*` проверяет предусловия и возвращает карточку: «с какого
   тарифа на какой, на сколько изменится цена». Ничего не исполняется.
   Ошибки возвращаются модели машиночитаемо: `unknown_tariff` с
   `available`, `bad_phone` с `example`, отказ правила `same_tariff`.
2. Карточка уходит в чат (`card` в SSE), «да/нет» разбирает код, не модель.
3. «да» → `execute_action`: ключ `{action}:{pending_id}` в таблице
   `idempotency` пропускает операцию один раз (плюс идемпотентность самого
   биллинга). Повтор отвечает «уже исполнена».
4. `execute_action` модель не видит никогда: его вызывает только код.
5. Не уверены или нужен живой человек — `create_ticket` без карточки,
   вызов тоже попадает в журнал.

Ход отдаётся во фронтенд потоком SSE:

```
data: {"type": "plan", "agents": ["data", "kb"], "steps": [...], "dropped": [...]}
data: {"type": "card", "pending_id": "p_ab12cd34", "card": {...}}
data: {"type": "reply", "text": "...", "sources": ["charge_rules", ...]}
data: {"type": "done"}
```

## Аудит и разбор инцидентов

Каждый ход получает `trace_id`; ходы одного диалога связывает `session_id`.
Три журнала в Postgres, только дописываются:

| Таблица      | Что хранит                                                  |
|--------------|-------------------------------------------------------------|
| `audit_log`  | исходы контура: `executed` (с `before`/`after`), `duplicate_suppressed`, `rejected_by_user`, `denied`, `server_limit`, `escalated`, `error` |
| `llm_calls`  | запуск агента: имя, модель, токены, задержка                |
| `tool_calls` | вызов инструмента моделью: аргументы (обрезаны), исход, задержка |

Текст реплик целиком не пишется — журнал про действия, не про переписку.

```python
from app import audit

audit.tail(20, who="u_4f2a")        # последние записи абонента
audit.trace("tr_1a2b3c4d5e6f")      # только audit_log одного хода
audit.timeline("tr_1a2b3c4d5e6f")   # хронология: audit + llm_calls + tool_calls
```

## Использование

Бэкенд, action-MCP и стенд — в отдельных терминалах:

```bash
make stand     # стенд :8100
make actions   # action-MCP :8220 (нужен для сессий с правом записи)
source .venv/bin/activate
make api       # бэкенд и оркестратор :8000
```

Затем откройте http://127.0.0.1:8100, выберите пользователя `anna` и
спросите «какой у меня тариф и почему списание 440?» — в ответе будут данные
абонента и правила из базы знаний с источниками.

То же из командной строки:

```bash
SID=$(curl -s -X POST http://127.0.0.1:8000/api/session \
  -H 'content-type: application/json' -d '{"user":"anna"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'content-type: application/json' \
  -d "{\"session_id\":\"$SID\",\"message\":\"какой у меня тариф?\"}"
```

Модель — любой OpenAI-совместимый сервер (LM Studio, облачный провайдер,
llama.cpp). Переменная `LLM_RESPONSE_FORMAT` выбирает, как передавать схему
плана: `auto` — `response_format` с JSON-схемой, `json_object` — для
серверов, которые принимают только `json_object` (схема уходит в инструкцию).

Стенд поднимается в отдельном терминале и проверяется руками:

```bash
make stand

curl http://127.0.0.1:8100/health
curl -X POST http://127.0.0.1:8100/token \
  -H 'content-type: application/json' \
  -d '{"resource": "http://127.0.0.1:8100/crm", "user": "anna"}'
curl 'http://127.0.0.1:8100/mock/crm/subscriber?sub=u_4f2a'
curl http://127.0.0.1:8100/jwks.json
```

Запрос к MCP-серверу без заголовка `Authorization: Bearer …` получает `401`
с `WWW-Authenticate` и метаданными защищённого ресурса.

Smoke-проверки не зависят от модели и прогоняются по группам шагов:

```bash
make smoke G=0     # стенд, токены, read-MCP, векторный поиск по базе знаний
make smoke G=1     # backend: сессия, видимые инструменты
make smoke G=2     # action-MCP: контракты, ошибки, executed → duplicate
make smoke G=3     # аудит: executed с before/after, duplicate_suppressed
make smoke         # все доступные группы
```

`make reset` возвращает демо-данные моков в исходное состояние.

---

<details>
<summary>Автоматические тесты Хекслета</summary>

Тесты запускаются на каждый коммит. За запуск отвечает файл `.github/workflows/hexlet-check.yml` — не удаляйте и не переименовывайте ни его, ни репозиторий.

</details>

## О Хекслете

[Хекслет](https://ru.hexlet.io/) — школа программирования: авторские программы обучения с практикой, поддержкой наставников и реальными проектами, которые остаются в резюме. Этот репозиторий — один из таких проектов.
