"""Keyless, deterministic test environment: in-memory state, offline Solana seam."""

import os

os.environ.setdefault("PAYLANE_IN_MEMORY_STATE", "1")
os.environ.setdefault("PAYLANE_OFFLINE", "1")
