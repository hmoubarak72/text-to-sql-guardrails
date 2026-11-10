"""Export pending user feedback for HUMAN review.

    python -m scripts.review_feedback            # writes eval/candidates/feedback_candidates.yaml

Nothing is promoted automatically. A reviewer verifies each item, then manually:
  * adds verified incorrect cases (with corrected gold SQL) to eval/golden.yaml, and/or
  * adds verified correct pairs to config/fewshots.yaml,
and updates app_meta.feedback.review_status accordingly.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.config import Settings
from app.db import make_app_engine
from app.persistence import MetaStore

OUT = Path(__file__).resolve().parents[1] / "eval" / "candidates" / "feedback_candidates.yaml"


def main() -> None:
    store = MetaStore(make_app_engine(Settings()))
    items = store.pending_feedback()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(yaml.safe_dump({"status": "UNVERIFIED - requires human review", "items": [
        {k: (str(v) if v is not None else None) for k, v in it.items()} for it in items]}, sort_keys=False),
        encoding="utf-8")
    print(f"exported {len(items)} pending feedback item(s) to {OUT}")


if __name__ == "__main__":
    main()
