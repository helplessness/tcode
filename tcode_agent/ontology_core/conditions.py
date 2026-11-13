"""Shared typed comparison of recorded conditions; no unit or semantic inference."""

METADATA_QUALIFIERS = {'evidence_basis', 'primary_reference'}


def typed_equal(left, right):
    if left is None or right is None:
        return False  # Unknown conditions never establish a match, even against another null.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(typed_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(typed_equal(a, b) for a, b in zip(left, right))
    return left == right



def known(value):
    if isinstance(value, dict):
        return all(known(item) for item in value.values())
    if isinstance(value, list):
        return all(known(item) for item in value)
    return value is not None


def compare_conditions(left, right):
    left = {k: v for k, v in left.items() if k not in METADATA_QUALIFIERS}
    right = {k: v for k, v in right.items() if k not in METADATA_QUALIFIERS}
    if any(known(left[k]) and known(right[k]) and not typed_equal(left[k], right[k])
           for k in left.keys() & right.keys()):
        return 'different_conditions'
    if left and typed_equal(left, right):
        return 'same_conditions'
    return 'unknown_conditions'

