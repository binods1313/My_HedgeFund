"""Proposals shrink toward the current value and never apply themselves."""

from __future__ import annotations

import json
from pathlib import Path

from learning.ledger import insert_proposal, set_proposal_status


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def bounded_value(old: float, target: float, *, shrinkage: float, max_change: float) -> float:
    """Move part of the way to the target, then stop at the cycle cap."""
    moved = old + shrinkage * (target - old)
    low = old * (1.0 - max_change)
    high = old * (1.0 + max_change)
    return min(high, max(low, moved))


def build_proposal(
    conn,
    *,
    kind: str,
    old_value: str,
    target: str,
    older: list[float],
    newer: list[float],
    samples: int,
    minimum: int,
    shrinkage: float,
    max_change: float,
) -> dict:
    """Return the stored proposal. A failed check is stored as rejected."""
    evidence = {
        "samples": samples,
        "older_mean_r": _mean(older),
        "newer_mean_r": _mean(newer),
        "minimum": minimum,
        "max_change": max_change,
        "shrinkage": shrinkage,
    }
    status = "proposed"
    new_value = target
    if samples < minimum:
        status = "rejected"
        new_value = old_value
        evidence["reason"] = "insufficient samples"
    else:
        older_mean = _mean(older)
        newer_mean = _mean(newer)
        if older_mean is None or newer_mean is None or newer_mean <= older_mean:
            status = "rejected"
            new_value = old_value
            evidence["reason"] = "no improvement"
        elif kind == "weight":
            try:
                new_value = f"{bounded_value(float(old_value), float(target), shrinkage=shrinkage, max_change=max_change):.8f}"
            except ValueError:
                status = "rejected"
                new_value = old_value
                evidence["reason"] = "value is not numeric"
    proposal = {
        "kind": kind,
        "old_value": old_value,
        "new_value": new_value,
        "evidence_json": json.dumps(evidence, sort_keys=True),
        "status": status,
        "decided_at": None if status == "proposed" else evidence.get("reason"),
    }
    proposal["id"] = insert_proposal(conn, proposal)
    return proposal


def _version_path(root: Path, version: int) -> Path:
    return root / f"v{version:04d}.json"


def _read_pointer(root: Path) -> dict | None:
    pointer = root / "current.json"
    if not pointer.is_file():
        return None
    return json.loads(pointer.read_text(encoding="utf-8"))


def _write_version(root: Path, version: int, values: dict, proposal_id: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "values": values, "applied_proposal": proposal_id}
    text = json.dumps(payload, sort_keys=True)
    _version_path(root, version).write_text(text, encoding="utf-8")
    (root / "current.json").write_text(text, encoding="utf-8")


def list_versions(root: Path) -> list[int]:
    if not root.is_dir():
        return []
    versions = []
    for path in root.glob("v*.json"):
        try:
            versions.append(int(path.stem[1:]))
        except ValueError:
            continue
    return sorted(versions)


def apply_proposal(conn, proposal_id: int, root: Path, values: dict) -> int:
    """Write a new weights version. Refuses a rejected proposal."""
    row = conn.execute("SELECT status FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None or row["status"] in {"rejected", "applied", "rolled_back"}:
        raise ValueError("proposal cannot be applied")
    versions = list_versions(root)
    if not versions:
        _write_version(root, 1, values, proposal_id)
        version = 1
    else:
        version = versions[-1] + 1
        _write_version(root, version, values, proposal_id)
    set_proposal_status(conn, proposal_id, "applied")
    return version


def rollback_to(conn, proposal_id: int, root: Path, version: int) -> int:
    """Point the current file at an earlier version and mark the proposal rolled back."""
    source = _version_path(root, version)
    if not source.is_file():
        raise ValueError("version does not exist")
    payload = json.loads(source.read_text(encoding="utf-8"))
    versions = list_versions(root)
    restored = (versions[-1] + 1) if versions else 1
    _write_version(root, restored, payload["values"], proposal_id)
    set_proposal_status(conn, proposal_id, "rolled_back")
    return restored


def current_values(root: Path) -> dict:
    payload = _read_pointer(root)
    if not payload:
        return {}
    values = payload.get("values")
    return values if isinstance(values, dict) else {}
