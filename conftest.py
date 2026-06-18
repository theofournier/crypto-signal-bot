"""Pytest configuration.

Its mere presence at the repo root puts that root on sys.path (pytest prepends
the rootdir conftest's directory), so tests can `from data import db` without an
installed package or PYTHONPATH tweak.
"""
