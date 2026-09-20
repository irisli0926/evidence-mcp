"""Safe, model-facing error codes.

Every *expected* failure is raised as an EvidenceError. Its message is a fixed
sentence written here in the code, never text copied from a lower-level
exception, so it is always safe to show to the MCP client. The details of what
went wrong (paths, exception messages, tracebacks) go to the local log only.
"""

# archive_source failures (the tool call fails; nothing new is stored)
UNSAFE_URL = "UNSAFE_URL"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
UNSUPPORTED_RESPONSE = "UNSUPPORTED_RESPONSE"
STORAGE_ERROR = "STORAGE_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Resource read failure
EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"

# GitHub sync failures (local evidence WAS saved; reported as a warning)
REMOTE_AUTH_FAILED = "REMOTE_AUTH_FAILED"
REMOTE_SYNC_FAILED = "REMOTE_SYNC_FAILED"
REMOTE_RECORD_CONFLICT = "REMOTE_RECORD_CONFLICT"
REMOTE_NOT_CONFIGURED = "REMOTE_NOT_CONFIGURED"


class EvidenceError(Exception):
    """An expected failure with a safe code and a safe, human-readable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
