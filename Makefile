SHELL := /bin/bash

COMPOSE := docker compose
DEFAULT_GOAL := help

.PHONY: help setup build up down restart logs ps update

help:
	@echo "Available targets:"
	@echo "  setup         Create .env from .env.example if it doesn't exist"
	@echo "  build         Build the sandbox Docker image"
	@echo "  up            Start stack"
	@echo "  down          Stop containers but keep data volumes"
	@echo "  restart       Restart all services"
	@echo "  logs          Follow logs for all services"
	@echo "  ps            Show container status"
	@echo "  update        Pull latest changes from git"

setup:
	@./setup.sh

build: setup
	$(COMPOSE) build sandbox

up: build
	$(COMPOSE) up -d

down:
	$(COMPOSE) down --remove-orphans || true

restart: down up

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

update:
	git pull --rebase --autostash
