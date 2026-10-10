"""Export JSON Schema for every contract model: `python -m transitos.contracts.export contracts-schema`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import envelope, events, objects, policy


def main(out: str) -> None:
    p = Path(out)
    p.mkdir(parents=True, exist_ok=True)
    models = {"CloudEvent": envelope.CloudEvent, **{f"data.{k}": v for k, v in events.DATA_MODELS.items()},
              "Plan": objects.Plan, "RunState": objects.RunState, "DecisionRecord": objects.DecisionRecord,
              "Vehicle": objects.Vehicle, "Block": objects.Block, "Operator": objects.Operator,
              "WorkOrder": objects.WorkOrder, "AgencyPolicy": policy.AgencyPolicy}
    for name, m in models.items():
        (p / f"{name}.schema.json").write_text(json.dumps(m.model_json_schema(), indent=2))
    print(f"wrote {len(models)} schemas to {p}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "contracts-schema")
