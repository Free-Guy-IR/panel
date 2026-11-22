CLEANUP_TARGETS_DESCRIPTION = """- **target**: `expired` | `limited` | `on_hold` | `disabled` | `inactive`
- **expired_after** / **expired_before** UTC datetime (optional)
- For `expired`: filters by expiration date.
- For `limited` / `on_hold` / `disabled`: filters by last_status_change (when they entered that status).
- For `inactive`: filters by last activity, the later of online_at and edit_at (an edit or a renewal), each read as
  created_at when unset; expired_before is required."""

EXPIRED_USERS_DESCRIPTION = f"Get cleanup-target users in the specified scope.\n\n{CLEANUP_TARGETS_DESCRIPTION}"

DELETE_EXPIRED_USERS_DESCRIPTION = (
    f"Delete cleanup-target users in the specified scope.\n\n{CLEANUP_TARGETS_DESCRIPTION}\n"
    "- **dry_run**: if true, returns users that would be deleted without deleting them."
)
