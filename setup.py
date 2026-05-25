#!/usr/bin/env python3
"""Compatibility shim for setuptools.

Project metadata lives in pyproject.toml. Keeping this file lets legacy
commands such as ``python3 setup.py sdist`` continue to work.
"""

from setuptools import setup


setup()
