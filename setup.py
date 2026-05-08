"""Compatibility shim.

Real configuration lives in pyproject.toml. This file exists only so older
pip versions (< 21.3) can still install the package in editable mode, which
they require a setup.py for. With a modern pip you don't need this file.
"""
from setuptools import setup

setup()
