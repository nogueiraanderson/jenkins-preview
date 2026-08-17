# jenkins-preview - developer tasks
# Everything runs through uv; no virtualenv to manage.

# List available recipes
default:
    @just --list

# Run the test suite (no network or Jenkins needed)
test:
    uv run --locked --group dev pytest

# Run one test by keyword, e.g. `just test-one g1`
test-one keyword:
    uv run --locked --group dev pytest -k "{{keyword}}" -v

# Format the code (also formats python blocks in markdown)
fmt:
    uvx ruff@0.16.3 format .

# Lint
lint:
    uvx ruff@0.16.3 check .

# Dead-code scan across the package (config in pyproject [tool.vulture])
dead-code:
    uvx vulture@2.14

# What CI runs: format check, lint, tests, build, wheel smoke test
# dist/ is cleared first: with a stale wheel present, the install glob would
# match two files and uv rejects the second as an extra argument.
ci:
    uvx ruff@0.16.3 format --check .
    uvx ruff@0.16.3 check .
    uv run --locked --group dev pytest
    rm -rf dist
    uv build
    uv tool install --force --quiet dist/*.whl
    jenkins-preview --version

# Build the sdist and wheel into dist/
build:
    uv build

# Install the working tree as the `jenkins-preview` tool
install:
    uv tool install --force .

# Run the CLI from the working tree, e.g. `just tool doctor --set pxb-8.1 ...`
tool *args:
    uv run --locked jenkins-preview {{args}}

# Refresh uv.lock to the newest allowed dependency versions
lock-upgrade:
    uv lock --upgrade

# Open the animated demo tour in your browser
demo:
    uv run python -m webbrowser "file://{{justfile_directory()}}/demo/index.html"

# Re-render demo/loop.svg from the captured-scene spec
demo-svg:
    uv run python demo/record.py demo/loop-scenes.json > demo/loop.svg

# Remove build and cache artifacts
clean:
    rm -rf dist .pytest_cache .ruff_cache
