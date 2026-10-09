"""Run isolated PostgreSQL checks inside the API container with the repo mounted."""

import os

os.environ["TEST_DATABASE_URL"] = (
    os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/recon_test"
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["VALID_API_KEYS"] = "test_key_1234567890abcdef"

import pytest  # noqa: E402

raise SystemExit(
    pytest.main(["tests", "--cov=app", "--cov-report=term-missing", "--cov-report=xml"])
)
