.PHONY: help install setup test infra migrate seed stand api actions pdp guardrail snapshot smoke scenarios reset

# Группа smoke-проверок: make smoke G=0. Без G прогоняются все группы.
G ?=

help:
	@echo "make install    - зависимости из requirements.txt"
	@echo "make setup      - зависимости, схема и демо-данные (база должна быть поднята)"
	@echo "make infra      - postgres+pgvector (docker compose)"
	@echo "make migrate    - применить db/*.sql"
	@echo "make seed       - демо-данные + база знаний в Postgres"
	@echo "make stand      - стенд готового: issuer + read-MCP + моки + веб-чат (:8100)"
	@echo "make api        - backend + оркестратор (:8000)         [агенты]"
	@echo "make actions    - action-MCP (:8220)                    [контракты]"
	@echo "make pdp        - PDP с политиками Dogwood (:9100)      [права]"
	@echo "make guardrail  - guardrail-сервис (:8300)              [guardrail]"
	@echo "make snapshot   - пересчитать векторы базы знаний (нужна LM Studio)"
	@echo "make smoke      - проверки по шагам, группа: make smoke G=0"
	@echo "make scenarios  - диалоговые сценарии на живой LLM      [сценарии]"
	@echo "make reset      - вернуть демо-данные моков в исходное"

install:
	python -m pip install -r requirements.txt

setup: install migrate seed

test: smoke

infra:
	docker compose -p operator-chat-ai up -d

migrate:
	python -m core.db migrate

seed:
	python -m core.seed

stand:
	uvicorn --factory stand:build_app --host 127.0.0.1 --port 8100 --log-level warning

api:
	uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning

actions:
	uvicorn actions.server:app --host 127.0.0.1 --port 8220 --log-level warning

pdp:
	uvicorn --factory pdp.service:build_app --host 127.0.0.1 --port 9100 --log-level warning

guardrail:
	uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300 --log-level warning

snapshot:
	python -m kb.build_snapshot

smoke:
	PYTHONPATH=. python tests/smoke.py $(G)

scenarios:
	python scripts/run_scenarios.py

reset:
	curl -s -X POST http://127.0.0.1:8100/mock/admin/reset
