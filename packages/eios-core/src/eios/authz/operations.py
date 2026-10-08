from enum import Enum


class Operation(str, Enum):
    DISCOVER = "discover"
    VIEW_METADATA = "view_metadata"
    READ = "read"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    EXECUTE = "execute"
    APPROVE = "approve"
    DELEGATE = "delegate"
    MANAGE_PERMISSIONS = "manage_permissions"
    MANAGE_POLICY = "manage_policy"
    EXPORT = "export"
    IDENTITY_USER_INVITE = "identity.user.invite"
    IDENTITY_USER_INVITATION_REISSUE = "identity.user.invitation.reissue"
    IDENTITY_USER_INVITATION_REVOKE = "identity.user.invitation.revoke"
    IDENTITY_USER_DISABLE = "identity.user.disable"
    IDENTITY_USER_RESTORE = "identity.user.restore"
    IDENTITY_USER_FORCE_RESET = "identity.user.force-reset"
    IDENTITY_USER_RECOVERY_CODES_REGENERATE = (
        "identity.user.recovery-codes.regenerate"
    )


__all__ = ["Operation"]
