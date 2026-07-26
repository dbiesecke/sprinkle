#!/usr/bin/env python3
"""Legacy setuptools entrypoint.

The same metadata is also declared in pyproject.toml for modern PEP 517
builds. Keep this file dependency-free so packaging does not import
sprinkle.py before install requirements exist.
"""

from pathlib import Path

from setuptools import setup


ROOT = Path(__file__).resolve().parent
RUNTIME_REQUIREMENTS = [
    "daemons>=1.3.0",
    "filelock>=3.0.10",
    "progress>=1.4",
]


setup(
    name="sprinkle-py",
    version="1.1.0",
    packages=["libsprinkle"],
    py_modules=["sprinkle"],
    scripts=["sprinkle.py"],
    install_requires=RUNTIME_REQUIREMENTS,
    url="https://gitlab.com/mmontuori/sprinkle",
    license="GPLv3",
    include_package_data=True,
    author="Michael Montuori",
    author_email="michael.montuori@gmail.com",
    description="Sprinkle is a volume clustering utility based on RClone.",
    long_description=(ROOT / "README.md").read_text(),
    long_description_content_type="text/markdown",
    keywords="sprinkle cloud backup restore rclone",
    zip_safe=True,
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    ],
)
