from __future__ import annotations

from dataclasses import dataclass, field


class PermissionDenied(RuntimeError):
    """Raised when a principal is not allowed to perform an operation."""


class ResourceNotFound(RuntimeError):
    """Raised when a tenant-scoped resource does not exist."""


class ConflictError(RuntimeError):
    """Raised when an idempotency key or state transition conflicts."""


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"knowledge:read", "task:read", "trace:read"}),
    "editor": frozenset(
        {
            "knowledge:read",
            "knowledge:write",
            "task:create",
            "task:read",
            "trace:read",
        }
    ),
    "approver": frozenset(
        {
            "knowledge:read",
            "task:approve",
            "task:read",
            "trace:read",
        }
    ),
    "admin": frozenset(
        {
            "knowledge:read",
            "knowledge:write",
            "task:create",
            "task:approve",
            "task:read",
            "trace:read",
        }
    ),
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"viewer"}))
    collection_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if not self.user_id.strip():
            raise ValueError("user_id must not be empty")
        unknown = set(self.roles).difference(ROLE_PERMISSIONS)
        if unknown:
            raise ValueError(f"unknown roles: {', '.join(sorted(unknown))}")

    @property
    def permissions(self) -> frozenset[str]:
        granted: set[str] = set()
        for role in self.roles:
            granted.update(ROLE_PERMISSIONS[role])
        return frozenset(granted)

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PermissionDenied(f"permission required: {permission}")

    def can_access_collection(self, collection_id: str) -> bool:
        return self.collection_ids is None or collection_id in self.collection_ids

    def require_collection(self, collection_id: str) -> None:
        if not self.can_access_collection(collection_id):
            raise PermissionDenied(f"collection is outside the granted scope: {collection_id}")

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> Principal:
        tenant_id = headers.get("x-tenant-id", "").strip()
        user_id = headers.get("x-user-id", "").strip()
        roles = frozenset(
            item.strip().lower()
            for item in headers.get("x-roles", "viewer").split(",")
            if item.strip()
        )
        raw_collections = headers.get("x-collection-ids", "").strip()
        collection_ids = (
            frozenset(item.strip() for item in raw_collections.split(",") if item.strip())
            if raw_collections
            else None
        )
        return cls(tenant_id=tenant_id, user_id=user_id, roles=roles, collection_ids=collection_ids)
