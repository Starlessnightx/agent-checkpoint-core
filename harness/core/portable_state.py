"""Data-only helpers for public component state imports. No storage or plugins."""
import json
import math


def copy_plain(value):
    def check(item, depth=0):
        if depth > 128:
            raise ValueError('component state nesting exceeds bound')
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child, depth+1)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child, depth+1)
            return
        raise ValueError('component state must contain finite JSON data')
    check(value)
    return json.loads(json.dumps(value, allow_nan=False))


def require_fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('component state fields differ')


def natural(value):
    if type(value) is not int or value < 0:
        raise ValueError('component state requires a nonnegative integer')
    return value


def strings(value):
    if type(value) is not list or any(type(v) is not str for v in value):
        raise ValueError('component state requires a list of strings')
    return value
