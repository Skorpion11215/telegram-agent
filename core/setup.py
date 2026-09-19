"""
Cython build script for TransferStats Agent.

Использование:
    python setup.py build_ext --inplace

Компилирует _core.pyx → _core.cpython-3XX-linux-gnu.so
"""

from setuptools import setup
from Cython.Build import cythonize

setup(
    ext_modules=cythonize(
        "core/_core.pyx",
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    ),
)
