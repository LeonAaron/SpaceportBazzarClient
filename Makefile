# Every target runs inside the compose container, so both partners get identical
# behaviour regardless of host OS. Start the stack first: docker compose up --build
EXEC := docker compose exec -T bazaar

.PHONY: proto test test-integration cov run shell

proto:
	$(EXEC) scripts/gen_proto.sh

test:
	$(EXEC) pytest -m "not integration"

test-integration:
	$(EXEC) pytest -m integration

cov:
	$(EXEC) pytest -m "not integration" --cov=bazaar_client --cov-report=term-missing

run:
	$(EXEC) python -m bazaar_client.cli $(ARGS)

shell:
	docker compose exec bazaar bash
