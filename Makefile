# Every target runs inside the compose container, so both partners get identical
# behaviour regardless of host OS. Start the stack first: docker compose up --build
EXEC := docker compose exec -T bazaar

.PHONY: proto test test-integration cov check run shell sim benchmark server

proto:
	$(EXEC) scripts/gen_proto.sh

test:
	$(EXEC) pytest -m "not integration"

test-integration:
	$(EXEC) pytest -m integration

cov:
	$(EXEC) pytest --cov=bazaar_client --cov=bazaar_sim --cov-branch --cov-report=term-missing

# Everything, in one command, as CI runs it: tests, coverage, practice server,
# live simulation, benchmark reproducibility. Writes logs/check-report.json.
check:
	$(EXEC) python scripts/check.py --integration

sim:
	$(EXEC) python -m bazaar_sim.orchestrate $(ARGS)

benchmark:
	$(EXEC) python -m bazaar_sim.benchmark $(ARGS)

server:
	$(EXEC) python -m bazaar_sim.server $(ARGS)

run:
	$(EXEC) python -m bazaar_client.cli $(ARGS)

shell:
	docker compose exec bazaar bash
