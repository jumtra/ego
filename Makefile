.PHONY: test smoke install uninstall

PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin

install:
	@mkdir -p "$(BINDIR)"
	@ln -sf "$(CURDIR)/bin/ego" "$(BINDIR)/ego"
	@rm -f "$(BINDIR)/harness"
	@echo "Installed: $(BINDIR)/ego"

uninstall:
	@rm -f "$(BINDIR)/ego"
	@echo "Uninstalled ego CLI from: $(BINDIR)"

test:
	PYTHONPATH=src python3 -m pytest -q

smoke:
	PYTHONPATH=src python3 -m ego --version
	PYTHONPATH=src python3 -m ego doctor --root .
