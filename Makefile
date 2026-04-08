SHELL := /bin/bash

COMPOSE := docker compose
COMPOSE_LOCAL := docker compose --profile local-docker
DEFAULT_GOAL := help

.PHONY: help setup build up down restart logs ps update

help:
	@echo "Available targets:"
	@echo "  setup         Create .env from .env.example if it doesn't exist"
	@echo "  build         Build the local development images"
	@echo "  up            Start the local development stack"
	@echo "  down          Stop containers but keep data volumes"
	@echo "  restart       Restart all services"
	@echo "  logs          Follow logs for all services"
	@echo "  ps            Show container status"
	@echo "  update        Pull latest changes from git"

setup:
	@./setup.sh

build: setup
	$(COMPOSE_LOCAL) build

up: build
	$(COMPOSE_LOCAL) up -d

down:
	$(COMPOSE_LOCAL) down --remove-orphans || true

restart: down up

logs:
	$(COMPOSE_LOCAL) logs -f

ps:
	$(COMPOSE_LOCAL) ps

update:
	git pull --rebase --autostash
