import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel


def canonical_json(value: BaseModel | Mapping[str, Any] | list[Any]) -> bytes:
    serializable: Any
    if isinstance(value, BaseModel):
        serializable = value.model_dump(mode="json", exclude_none=False)
    else:
        serializable = value
    return json.dumps(
        serializable,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: BaseModel | Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def checksum_chain(checksums: Iterable[str]) -> str:
    """Hash ordered hexadecimal batch checksums with unambiguous separators."""
    digest = hashlib.sha256()
    for checksum in checksums:
        digest.update(checksum.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def stable_receipt(run_id: object, checksum: str, committed_at: object) -> str:
    payload = f"{run_id}:{checksum}:{committed_at}".encode()
    return hashlib.sha256(payload).hexdigest()
