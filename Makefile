.PHONY: test smoke bench

test:
	PYTHONPATH=src python3 -m pytest -q

smoke:
	PYTHONPATH=src python3 -m seh --version
	PYTHONPATH=src python3 -m seh modules list
	PYTHONPATH=src python3 -m seh optimize status

bench:
	PYTHONPATH=src python3 -m seh bench --trials 10
