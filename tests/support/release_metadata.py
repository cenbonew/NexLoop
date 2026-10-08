"""Independent expected release head from the repository lock, not SQL catalog."""
import json
from pathlib import Path

BOOTSTRAP_REVISION = json.loads(
    (Path(__file__).resolve().parents[2] / "versions.lock.json").read_text()
)["packages"]["nexloop-eios-core"]["bootstrap_revision"]
