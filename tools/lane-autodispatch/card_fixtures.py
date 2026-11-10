"""Give scheduling fixtures a minimal acceptance command at the API boundary."""
import json


def fixture_json(value):
    def qualify(row):
        if isinstance(row, list):
            return [qualify(item) for item in row]
        if isinstance(row, dict) and 'number' in row and 'body' in row:
            return {**row, 'body': (row['body'] or '') + '\n```sh\npython3 -m unittest\n```'}
        return row
    return json.dumps(qualify(value))
