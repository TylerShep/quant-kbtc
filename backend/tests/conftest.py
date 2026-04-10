"""Shared fixtures for the test suite."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("KALSHI_API_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "/dev/null")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
