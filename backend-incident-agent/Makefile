# Makefile for backend-incident-agent

.PHONY: install playground start test lint generate-traces grade

install:
	agents-cli install

playground:
	agents-cli playground

start:
	uv run python app/fast_api_app.py

test:
	uv run pytest tests/integration/test_agent.py

lint:
	agents-cli lint

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	GOOGLE_CLOUD_PROJECT=dummy-project-id agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml
