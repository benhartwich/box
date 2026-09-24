# The checked-out working tree instead of a git clone (mounted read-only at /src).
mkdir -p /opt/myboxi-server
tar -C /src --exclude=./.venv --exclude=./.dev --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.ruff_cache -cf - . | tar -C /opt/myboxi-server -xf -
