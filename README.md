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

## Использование

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
