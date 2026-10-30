"""Load a KEY=value file into the process environment."""
import os


def load_env_file(path: str) -> None:
    """Set variables from a KEY=value file without overriding existing ones; a missing file is ignored."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())
