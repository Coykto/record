.PHONY: swift install test

swift:
	swift build -c release --package-path swift-capture
	rm -rf src/record/bin/record-capture src/record/bin/record-capture.app
	mkdir -p src/record/bin/record-capture.app/Contents/MacOS
	cp swift-capture/Info.plist src/record/bin/record-capture.app/Contents/Info.plist
	cp swift-capture/.build/release/record-capture src/record/bin/record-capture.app/Contents/MacOS/record-capture
	chmod +x src/record/bin/record-capture.app/Contents/MacOS/record-capture
	# Sign with a stable, self-signed certificate (created on first build by
	# scripts/ensure-signing-cert.sh). macOS TCC keys permission grants on the
	# signing identity. An ad-hoc signature's identity is its cdhash, which
	# changes on every build — so Screen Recording / Accessibility grants would
	# be orphaned on every `make swift`. The self-signed cert gives a stable
	# LOCAL identity (the designated requirement becomes cert-based, not
	# cdhash-based), so TCC grants persist across rebuilds. This is NOT
	# Developer ID / notarization — that's a separate, deferred distribution
	# concern; an untrusted-but-stable identity is all TCC needs.
	./scripts/ensure-signing-cert.sh
	codesign --force --sign "Record Local Signing" --identifier com.record.capture src/record/bin/record-capture.app

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
