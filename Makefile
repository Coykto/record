.PHONY: swift install test

swift:
	swift build -c release --package-path swift-capture
	mkdir -p src/record/bin
	cp swift-capture/.build/release/record-capture src/record/bin/record-capture
	chmod +x src/record/bin/record-capture

install: swift
	uv pip install -e .

test: swift
	@echo "=== Python tests ==="
	uv run pytest tests
	@echo "=== Swift tests ==="
	@case "$$(xcode-select -p 2>/dev/null)" in \
		*/Xcode.app/*) swift test --package-path swift-capture ;; \
		*) echo "swift test skipped: XCTest unavailable -- install full Xcode to enable Swift unit tests" ;; \
	esac
