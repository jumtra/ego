.PHONY: test smoke bench install uninstall

PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin

install:
	@mkdir -p "$(BINDIR)"
	@ln -sf "$(CURDIR)/bin/ego" "$(BINDIR)/ego"
	@rm -f "$(BINDIR)/harness"
	@echo "Installed: $(BINDIR)/ego"

uninstall:
	@echo "Uninstalled ego CLI from: $(BINDIR)"

test:
	PYTHONPATH=src python3 -m pytest -q

smoke:
	PYTHONPATH=src python3 -m ego --version
	PYTHONPATH=src python3 -m ego modules list
	PYTHONPATH=src python3 -m ego optimize status

bench:
	PYTHONPATH=src python3 -m ego bench --trials 10
