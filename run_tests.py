#!/usr/bin/env python3
"""
run_tests.py
------------
Self-contained test runner for options_scanner.
Uses unittest only — no pytest required.

Usage:
    python run_tests.py                  # run all tests
    python run_tests.py test_black76     # run one module
    python run_tests.py test_black76.TestBlack76Price.test_put_call_parity
                                         # run one test

pytest is still the recommended runner on a full install:
    pytest tests/                        # all tests
    pytest tests/test_black76.py         # one file
    pytest tests/test_black76.py::TestBlack76Price::test_put_call_parity
"""

import sys
import os
import unittest

# Ensure options_scanner is importable from any working directory
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_HERE, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TEST_DIR = os.path.join(_HERE, 'tests')


def _discover(pattern: str | None = None) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    if pattern is None:
        return loader.discover(TEST_DIR, pattern='test_*.py')

    # Allow: test_black76  |  test_black76.ClassName  |  test_black76.ClassName.method
    parts = pattern.split('.')
    module_name = parts[0]
    if not module_name.startswith('test_'):
        module_name = 'test_' + module_name

    # Import the module
    import importlib.util
    mod_path = os.path.join(TEST_DIR, module_name + '.py')
    if not os.path.exists(mod_path):
        print(f"ERROR: Test file not found: {mod_path}")
        sys.exit(1)

    spec   = importlib.util.spec_from_file_location(module_name, mod_path)
    module = importlib.util.module_from_spec(spec)          # type: ignore[arg-type]
    spec.loader.exec_module(module)                          # type: ignore[union-attr]
    sys.modules[module_name] = module

    if len(parts) == 1:
        return loader.loadTestsFromModule(module)
    elif len(parts) == 2:
        cls = getattr(module, parts[1], None)
        if cls is None:
            print(f"ERROR: Class '{parts[1]}' not found in {module_name}")
            sys.exit(1)
        return loader.loadTestsFromTestCase(cls)
    else:
        return loader.loadTestsFromName(
            f"{module_name}.{parts[1]}.{parts[2]}", module=module
        )


def main() -> None:
    pattern = sys.argv[1] if len(sys.argv) > 1 else None
    suite   = _discover(pattern)

    runner = unittest.TextTestRunner(
        verbosity  = 2,
        stream     = sys.stdout,
        failfast   = False,
        tb_locals  = True,
    )
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
