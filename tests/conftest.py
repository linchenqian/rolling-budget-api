import os

# Settings are constructed while the application module is imported, so test-only
# values must exist before pytest imports any test module.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://budget_test:budget_test@127.0.0.1:5432/budget_test",
)
os.environ.setdefault("API_KEY", "test-master-key-at-least-32-characters")

# The default test process exercises the one-key installation path. Tests for
# advanced role-specific credentials inject those settings explicitly.
os.environ.pop("BUDGET_READ_API_KEY", None)
os.environ.pop("BUDGET_WRITE_API_KEY", None)
os.environ.pop("BUDGET_ADMIN_API_KEY", None)
