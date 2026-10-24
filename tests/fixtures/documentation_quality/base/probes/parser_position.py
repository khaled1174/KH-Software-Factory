"""Trusted fixed ground-truth probe, not a documentation acceptance gate."""
import json

def probe():
    try:
        json.loads("{\n")
    except json.JSONDecodeError as error:
        return {"lineno": error.lineno, "colno": error.colno,
                "is_value_error": isinstance(error, ValueError)}
    raise AssertionError("The fixed malformed JSON unexpectedly parsed")
