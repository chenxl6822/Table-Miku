from __future__ import annotations

from dataclasses import dataclass, field


MAX_IDENTITY_FIELD_LENGTH = 120
MAX_COLLECTION_ID_LENGTH = 120


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
        tenant_id = self.tenant_id.strip()
        user_id = self.user_id.strip()
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        if len(tenant_id) > MAX_IDENTITY_FIELD_LENGTH:
            raise ValueError(f"tenant_id must not exceed {MAX_IDENTITY_FIELD_LENGTH} characters")
        if not user_id:
            raise ValueError("user_id must not be empty")
        if len(user_id) > MAX_IDENTITY_FIELD_LENGTH:
            raise ValueError(f"user_id must not exceed {MAX_IDENTITY_FIELD_LENGTH} characters")
        normalized_roles = frozenset(str(role).strip().lower() for role in self.roles if str(role).strip())
        unknown = set(normalized_roles).difference(ROLE_PERMISSIONS)
        if unknown:
            raise ValueError(f"unknown roles: {', '.join(sorted(unknown))}")
        normalized_collections: frozenset[str] | None = None
        if self.collection_ids is not None:
            cleaned: set[str] = set()
            for raw_collection_id in self.collection_ids:
                collection_id = str(raw_collection_id).strip()
                if not collection_id:
                    raise ValueError("collection_ids must not contain empty values")
                if len(collection_id) > MAX_COLLECTION_ID_LENGTH:
                    raise ValueError(
                        f"collection_id must not exceed {MAX_COLLECTION_ID_LENGTH} characters"
                    )
                cleaned.add(collection_id)
            normalized_collections = frozenset(cleaned)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "roles", normalized_roles)
        object.__setattr__(self, "collection_ids", normalized_collections)

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
        collection_scope = headers.get("x-collection-scope", "").strip().casefold()
        if collection_scope not in {"", "restricted"}:
            raise ValueError("X-Collection-Scope must be 'restricted' when supplied")
        has_collection_header = "x-collection-ids" in headers
        raw_collections = headers.get("x-collection-ids", "").strip()
        collection_ids = None
        if collection_scope == "restricted" or has_collection_header:
            collection_ids = frozenset(
                item.strip() for item in raw_collections.split(",") if item.strip()
            )
        return cls(tenant_id=tenant_id, user_id=user_id, roles=roles, collection_ids=collection_ids)
