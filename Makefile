# Run every check even if an earlier one fails, then fail at the end if any did.
lint:
	@rc=0; \
	for cmd in "black ." "isort ." "flake8 ." "mypy . --check-untyped-defs --follow-imports=silent"; do \
		echo "==> $$cmd"; \
		$$cmd || rc=1; \
	done; \
	exit $$rc

test:
	pytest

# Run lint and test even if lint fails, then fail at the end if either did.
check:
	@rc=0; \
	$(MAKE) --no-print-directory lint || rc=1; \
	$(MAKE) --no-print-directory test || rc=1; \
	exit $$rc
