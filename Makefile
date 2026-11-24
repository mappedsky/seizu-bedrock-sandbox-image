IMAGE ?= seizu-sandbox:dev

.PHONY: sync test lint build check

sync:
	uv sync --frozen

test: sync
	uv run --frozen --no-sync pytest

lint: sync
	uv run --frozen --no-sync pre-commit run --all-files

# Builds for the host's architecture; AgentCore Runtime runs linux/arm64.
build:
	docker buildx build --load --tag $(IMAGE) .

check: build
	scripts/check-image.sh $(IMAGE)
