"""Approval keys and tool security digests (01-security, "Approval binding").

An approval is bound to one exact call: `approval_key` hashes the pack, the tool and the
canonical JSON of the validated arguments, so any difference in any argument (whitespace,
case, Unicode form) gives a new key (REQ-SEC-19, REQ-SEC-20). The pack name is part of the key
so a renamed or replaced pack does not inherit grants.

`security_digest` hashes every field of a `ToolMeta` that the policy reads. Grants record it,
and a pack update that changes a tier, a flag, the domains or any parameter rule makes the
grant stop matching (REQ-SEC-21); `pack_version` and descriptions are left out, so a release
that changes none of these keeps grants valid.

Both use version tags (`hal-approval/v1`, `hal-toolmeta/v1`) so a future encoding can coexist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING

from halkit.canon import canonical_json

if TYPE_CHECKING:  # annotation only: importing the policy package here would be circular
    from ..policy.model import ToolMeta

APPROVAL_KEY_TAG = b"hal-approval/v1\n"
TOOLMETA_TAG = b"hal-toolmeta/v1\n"


def approval_key(pack: str, tool: str, args: Mapping[str, object]) -> str:
    """`"sha256:" + sha256(tag + pack + "\\n" + tool + "\\n" + canonical_json(args))` (REQ-SEC-19)."""
    digest = hashlib.sha256(
        APPROVAL_KEY_TAG + pack.encode() + b"\n" + tool.encode() + b"\n" + canonical_json(dict(args))
    )
    return "sha256:" + digest.hexdigest()


def security_digest(meta: ToolMeta) -> str:
    """Digest of every `ToolMeta` field the policy reads (01-security, "Approval binding")."""
    doc = {
        "tier": str(meta.tier),
        "network": meta.network,
        "untrusted_output": meta.untrusted_output,
        "reviewable": meta.reviewable,
        "reversible": meta.reversible,
        "runs_code": meta.runs_code,
        "domains": sorted(meta.domains),
        "params": [
            {
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "enum": list(p.enum) if p.enum is not None else None,
                "max_length": p.max_length,
                "format": p.format,
                "access": p.access,
                "content": p.content,
            }
            for p in sorted(meta.params, key=lambda p: p.name)
        ],
    }
    return "sha256:" + hashlib.sha256(TOOLMETA_TAG + canonical_json(doc)).hexdigest()
