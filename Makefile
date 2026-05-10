.PHONY: swift install test

swift:
	swift build -c release --package-path swift-capture
	mkdir -p src/record/bin
	cp swift-capture/.build/release/record-capture src/record/bin/record-capture
	chmod +x src/record/bin/record-capture

install: swift
	uv pip install -e .

test:
	@echo "tests not implemented yet"
