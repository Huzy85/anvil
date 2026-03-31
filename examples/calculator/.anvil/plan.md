# Calculator — Example Anvil Plan

**Goal:** Build a simple calculator module with tests.

**Architecture:** Single Python module with pure functions, tested with pytest.

**Tech Stack:** Python 3, pytest

### Task 1: Core arithmetic

Create `calculator.py` with four functions:
- `add(a, b)` — returns a + b
- `subtract(a, b)` — returns a - b
- `multiply(a, b)` — returns a * b
- `divide(a, b)` — returns a / b, raises ValueError if b is 0

### Task 2: Tests

Create `test_calculator.py` with pytest tests:
- test_add: assert add(2, 3) == 5
- test_subtract: assert subtract(5, 3) == 2
- test_multiply: assert multiply(4, 3) == 12
- test_divide: assert divide(10, 2) == 5.0
- test_divide_by_zero: assert raises ValueError
