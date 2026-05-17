DEFAULT_GOAL := help

ifeq ($(OS),Windows_NT)
SHELL := powershell.exe
.SHELLFLAGS := -NoProfile -ExecutionPolicy Bypass -Command
SETUP_COMMAND := .\setup.ps1
COMPOSE_LOCAL := docker compose --profile local-docker
COMPOSE_BUILD := docker compose --profile local-docker --profile build
else
SHELL := /bin/bash
SETUP_COMMAND := bash ./setup.sh
COMPOSE_LOCAL := env -u DOCKER_HOST docker compose --profile local-docker
COMPOSE_BUILD := env -u DOCKER_HOST docker compose --profile local-docker --profile build
endif

.PHONY: help setup build up down restart logs ps update

help:
	@echo "Available targets:"
	@echo "  setup         Create or sync .env and generate API_KEYS if needed"
	@echo "  build         Build the local development images"
	@echo "  up            Start the local development stack"
	@echo "  down          Stop containers but keep data volumes"
	@echo "  restart       Restart all services"
	@echo "  logs          Follow logs for all services"
	@echo "  ps            Show container status"
	@echo "  update        Pull latest changes from git"

setup:
	@$(SETUP_COMMAND)

build: setup
	$(COMPOSE_BUILD) build

up: build
	$(COMPOSE_LOCAL) up -d

down:
	-$(COMPOSE_LOCAL) down --remove-orphans

restart: down up

logs:
	$(COMPOSE_LOCAL) logs -f

ps:
	$(COMPOSE_LOCAL) ps

update:
	git pull --rebase --autostash
