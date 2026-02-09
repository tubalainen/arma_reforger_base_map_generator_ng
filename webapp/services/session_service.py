"""
Session management for multi-user support.

Uses cryptographically secure session IDs and access tokens.
Sessions are stored in-memory (suitable for single-instance deployments).
Thread-safe for concurrent access.

Architecture:
- Session ID: Identifies a user session (stored in cookie)
- Access Token: Per-job token for downloads (signed with session ID)
"""

import hashlib
import hmac
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Configuration
SESSION_EXPIRY_HOURS = 24
SESSION_ID_BYTES = 32  # 256 bits of entropy

# Secret key for signing access tokens (regenerated on startup)
_TOKEN_SECRET = secrets.token_bytes(32)


@dataclass
class Session:
    """Represents a user session."""

    session_id: str
    created_at: datetime
    last_accessed: datetime
    job_ids: list[str] = field(default_factory=list)

    def is_expired(self) -> bool:
        """Check if the session has expired."""
        return datetime.utcnow() - self.last_accessed > timedelta(hours=SESSION_EXPIRY_HOURS)

    def touch(self) -> None:
        """Update last accessed time."""
        self.last_accessed = datetime.utcnow()

    def add_job(self, job_id: str) -> None:
        """Associate a job with this session."""
        if job_id not in self.job_ids:
            self.job_ids.append(job_id)
            logger.debug(f"Added job {job_id[:8]}... to session {self.session_id[:8]}...")

    def owns_job(self, job_id: str) -> bool:
        """Check if this session owns a specific job."""
        return job_id in self.job_ids

    def remove_job(self, job_id: str) -> None:
        """Remove a job from this session."""
        if job_id in self.job_ids:
            self.job_ids.remove(job_id)


# In-memory session store with thread-safe access
_sessions: dict[str, Session] = {}
_sessions_lock = threading.RLock()


def create_session() -> Session:
    """Create a new session with a cryptographically secure ID."""
    session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
    now = datetime.utcnow()
    session = Session(session_id=session_id, created_at=now, last_accessed=now)

    with _sessions_lock:
        _sessions[session_id] = session

    logger.info(f"Created new session {session_id[:8]}...")
    return session


def get_session(session_id: str) -> Optional[Session]:
    """Get a session by ID, returning None if expired or not found."""
    if not session_id:
        return None

    with _sessions_lock:
        session = _sessions.get(session_id)
        if session and not session.is_expired():
            session.touch()
            return session
        elif session:
            # Clean up expired session and its jobs
            _cleanup_session_jobs(session)
            del _sessions[session_id]
            logger.info(f"Cleaned up expired session {session_id[:8]}...")
    return None


def get_or_create_session(session_id: Optional[str]) -> tuple[Session, bool]:
    """
    Get an existing session or create a new one.

    Returns:
        Tuple of (session, is_new) where is_new indicates if a new session was created.
    """
    if session_id:
        session = get_session(session_id)
        if session:
            return session, False

    return create_session(), True


def generate_access_token(job_id: str, session_id: str) -> str:
    """
    Generate a signed access token for downloading a specific job.

    Token format: {job_id}:{signature}
    Signature: HMAC-SHA256(job_id + session_id, secret)

    This allows downloads via public endpoints without exposing session cookies.
    """
    message = f"{job_id}:{session_id}".encode('utf-8')
    signature = hmac.new(_TOKEN_SECRET, message, hashlib.sha256).hexdigest()
    token = f"{job_id}:{signature}"
    return token


def verify_access_token(token: str, job_id: str) -> bool:
    """
    Verify an access token for a job download.

    Returns True if the token is valid for the given job_id.
    """
    if not token or ':' not in token:
        return False

    try:
        token_job_id, signature = token.split(':', 1)

        # Check if job_id matches
        if token_job_id != job_id:
            return False

        # Find the session that owns this job
        with _sessions_lock:
            for session in _sessions.values():
                if session.owns_job(job_id):
                    # Verify signature
                    expected_message = f"{job_id}:{session.session_id}".encode('utf-8')
                    expected_signature = hmac.new(_TOKEN_SECRET, expected_message, hashlib.sha256).hexdigest()

                    if hmac.compare_digest(signature, expected_signature):
                        return True

        return False
    except Exception as e:
        logger.warning(f"Token verification failed: {e}")
        return False


def _cleanup_session_jobs(session: Session) -> None:
    """
    Clean up jobs associated with an expired session.

    This is called internally when a session expires to remove the job's
    session_id reference, preventing orphaned jobs.
    """
    if not session.job_ids:
        return

    # Import here to avoid circular dependency
    from services.map_generator import cleanup_job_session

    for job_id in session.job_ids:
        try:
            cleanup_job_session(job_id)
        except Exception as e:
            logger.error(f"Failed to cleanup job {job_id[:8]}... for expired session: {e}")


def cleanup_expired_sessions() -> int:
    """
    Remove all expired sessions and their associated job references.

    Returns:
        Number of sessions cleaned up.
    """
    with _sessions_lock:
        expired = [(sid, s) for sid, s in _sessions.items() if s.is_expired()]

        for sid, session in expired:
            _cleanup_session_jobs(session)
            del _sessions[sid]

        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")

        return len(expired)


def get_session_count() -> int:
    """Get the current number of active sessions."""
    with _sessions_lock:
        return len(_sessions)


def get_session_stats() -> dict:
    """Get session statistics for monitoring."""
    with _sessions_lock:
        active_count = 0
        total_jobs = 0

        for session in _sessions.values():
            if not session.is_expired():
                active_count += 1
                total_jobs += len(session.job_ids)

        return {
            "active_sessions": active_count,
            "total_jobs_tracked": total_jobs,
            "session_expiry_hours": SESSION_EXPIRY_HOURS,
        }


def clear_all_sessions() -> int:
    """
    Clear all sessions on application startup.

    This ensures a clean state when the application starts, removing any
    hanging sessions from previous runs. Since sessions are stored in-memory,
    they would be lost on restart anyway, but this also cleans up any
    associated job references.

    Returns:
        Number of sessions cleared.
    """
    with _sessions_lock:
        session_count = len(_sessions)

        if session_count == 0:
            logger.info("No sessions to clear on startup")
            return 0

        # Clean up all session jobs
        for session in _sessions.values():
            _cleanup_session_jobs(session)

        # Clear the sessions dict
        _sessions.clear()

        logger.info(f"Cleared {session_count} sessions on startup")
        return session_count
