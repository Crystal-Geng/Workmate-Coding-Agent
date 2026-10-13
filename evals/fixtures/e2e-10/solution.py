from datetime import datetime
def solve(value):
    return datetime.strptime(value, '%Y-%d-%m').date().isoformat()
