"""Synthetic Exotel callback fixtures for contract testing.

Source: SYNTHETIC — field names from documented API response fields.
Pending sandbox verification (OQ-1 through OQ-8 in provider contract).
Phone numbers are fictional; CallSid values are synthetic hex.
"""

ANSWERED_OUTBOUND = {
    "CallSid": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "EventType": "answered",
    "Status": "in-progress",
    "From": "+919876543210",
    "To": "08012345678",
    "Direction": "outbound-api",
    "CustomField": "1:corr-001",
}

COMPLETED_OUTBOUND = {
    "CallSid": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "EventType": "terminal",
    "Status": "completed",
    "From": "+919876543210",
    "To": "08012345678",
    "Duration": "120",
    "ConversationDuration": "95",
    "Direction": "outbound-api",
    "CustomField": "1:corr-001",
}

FAILED_OUTBOUND = {
    "CallSid": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d600",
    "EventType": "terminal",
    "Status": "failed",
    "From": "+919876543210",
    "To": "08012345678",
    "Direction": "outbound-api",
}

BUSY_OUTBOUND = {
    "CallSid": "c3d4e5f6a7b8c9d0e1f2a3b4c5d60011",
    "EventType": "terminal",
    "Status": "busy",
    "From": "+919876543210",
    "To": "08012345678",
    "Direction": "outbound-api",
}

NO_ANSWER_OUTBOUND = {
    "CallSid": "d4e5f6a7b8c9d0e1f2a3b4c5d6001122",
    "EventType": "terminal",
    "Status": "no-answer",
    "From": "+919876543210",
    "To": "08012345678",
    "Direction": "outbound-api",
}

ANSWERED_INBOUND = {
    "CallSid": "e5f6a7b8c9d0e1f2a3b4c5d600112233",
    "EventType": "answered",
    "Status": "in-progress",
    "From": "+919111222333",
    "To": "08012345678",
    "Direction": "inbound",
}

COMPLETED_INBOUND = {
    "CallSid": "e5f6a7b8c9d0e1f2a3b4c5d600112233",
    "EventType": "terminal",
    "Status": "completed",
    "From": "+919111222333",
    "To": "08012345678",
    "Duration": "180",
    "ConversationDuration": "150",
    "Direction": "inbound",
}

MISSING_OPTIONAL_FIELDS = {
    "CallSid": "f6a7b8c9d0e1f2a3b4c5d60011223344",
    "EventType": "terminal",
    "Status": "completed",
    "From": "+919876543210",
    "To": "08012345678",
}

NEGATIVE_DURATION = {
    "CallSid": "a7b8c9d0e1f2a3b4c5d6001122334455",
    "EventType": "terminal",
    "Status": "completed",
    "From": "+919876543210",
    "To": "08012345678",
    "Duration": "-5",
}

MISSING_EVENT_TYPE_TERMINAL = {
    "CallSid": "b8c9d0e1f2a3b4c5d600112233445566",
    "Status": "completed",
    "From": "+919876543210",
    "To": "08012345678",
    "Duration": "60",
}

MISSING_EVENT_TYPE_ANSWERED = {
    "CallSid": "c9d0e1f2a3b4c5d60011223344556677",
    "Status": "in-progress",
    "From": "+919876543210",
    "To": "08012345678",
}
