"""External-operation exposure policy for future API and LLM adapters.

Future dispatchers must publish operations exclusively from
``EXTERNAL_PENDING_ACTION_OPERATIONS``. Internal commit/read operations are
permanently excluded from caller-facing and LLM tool registries.
"""

EXTERNAL_PENDING_ACTION_OPERATIONS = frozenset(
    {
        "create_pending_action",
        "revise_pending_action",
        "mark_awaiting_confirmation",
        "reject_pending_action",
        "cancel_pending_action",
        "get_pending_action",
        "get_active_pending_action",
    }
)

INTERNAL_PENDING_ACTION_OPERATIONS = frozenset(
    {
        "begin_commit",
        "complete_commit",
        "fail_commit",
        "internal_get",
        "internal_get_active",
        "expire_pending_action",
        "bulk_expire_pending_actions",
    }
)

assert EXTERNAL_PENDING_ACTION_OPERATIONS.isdisjoint(INTERNAL_PENDING_ACTION_OPERATIONS)
