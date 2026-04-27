COMPOSE_FILE := docker-compose.dev.yml
COMPOSE := docker compose -f $(COMPOSE_FILE)

.PHONY: docker-up docker-down docker-build docker-logs docker-ps docker-shell docker-migrate docker-superuser docker-test

docker-up:
	$(COMPOSE) up --build -d

docker-down:
	$(COMPOSE) down

docker-build:
	$(COMPOSE) build

docker-logs:
	$(COMPOSE) logs -f web

docker-ps:
	$(COMPOSE) ps

docker-shell:
	$(COMPOSE) exec web python manage.py shell

docker-migrate:
	$(COMPOSE) exec web python manage.py migrate

docker-superuser:
	$(COMPOSE) exec web python manage.py createsuperuser

docker-test:
	$(COMPOSE) exec web python manage.py test
