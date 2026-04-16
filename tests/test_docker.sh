#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

IMAGE_NAME="medpilot-test"

echo "=== Building Docker image ==="
docker build -t "$IMAGE_NAME" -f deploy/Dockerfile .

echo ""
echo "=== Running 'medpilot onboard' ==="
docker run --name medpilot-test-run "$IMAGE_NAME" onboard

echo ""
echo "=== Running 'medpilot status' ==="
STATUS_OUTPUT=$(docker commit medpilot-test-run medpilot-test-onboarded > /dev/null && \
    docker run --rm medpilot-test-onboarded status 2>&1) || true

echo "$STATUS_OUTPUT"

echo ""
echo "=== Validating output ==="
PASS=true

check() {
    if echo "$STATUS_OUTPUT" | grep -q "$1"; then
        echo "  PASS: found '$1'"
    else
        echo "  FAIL: missing '$1'"
        PASS=false
    fi
}

check "medpilot Status"
check "Config:"
check "Workspace:"
check "Model:"
check "OpenRouter API:"
check "Anthropic API:"
check "OpenAI API:"

echo ""
if $PASS; then
    echo "=== All checks passed ==="
else
    echo "=== Some checks FAILED ==="
    exit 1
fi

# Cleanup
echo ""
echo "=== Cleanup ==="
docker rm -f medpilot-test-run 2>/dev/null || true
docker rmi -f medpilot-test-onboarded 2>/dev/null || true
docker rmi -f "$IMAGE_NAME" 2>/dev/null || true
echo "Done."
