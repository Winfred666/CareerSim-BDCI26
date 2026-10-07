"""Accept standard JSON, plus an optional leading + on integer values only."""
import json,re

def decode(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Match strings as opaque tokens; never edit their content or infer a grade.
        pattern = r'"(?:\\.|[^"\\])*"|(?P<prefix>[:\[,]\s*)\+(?P<number>[0-9]+)(?=\s*[,}\]])'
        cleaned = re.sub(pattern, lambda m: m['prefix']+m['number'] if m['prefix'] is not None else m[0], text)
        if cleaned == text:
            raise
        return json.loads(cleaned)
