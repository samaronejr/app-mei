COMPOSE ?= docker compose

.PHONY: up down logs test lint migrate shell

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

test:
	$(COMPOSE) exec -T web uv run pytest

lint:
	$(COMPOSE) exec -T web uv run ruff check . && \
	$(COMPOSE) exec -T web uv run ruff format --check . && \
	$(COMPOSE) exec -T web uv run mypy .

migrate:
	$(COMPOSE) exec -T web uv run python manage.py migrate

shell:
	$(COMPOSE) exec web uv run python manage.py shell
