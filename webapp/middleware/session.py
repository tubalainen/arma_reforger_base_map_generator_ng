"""
Session middleware for FastAPI.

Handles session cookie management and injects session into request state.
Supports both HTTPS (via reverse proxy) and HTTP (local network) access.

Key features:
- Creates session on first visit
- Persists session via HttpOnly cookies
- Handles proxy scenarios (Cloudflare, nginx)
- Relaxed security for local network access
"""

import ipaddress
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from services.session_service import get_or_create_session

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "arma_session"
COOKIE_MAX_AGE = 86400  # 24 hours

# Local network CIDRs that don't require secure cookies
LOCAL_CIDRS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
]


def is_local_request(request: Request) -> bool:
    """Check if request is from a local/private network."""
    # Get the real client IP (may be forwarded by proxy)
    # Priority: X-Real-IP > X-Forwarded-For > direct connection
    client_ip = request.headers.get("X-Real-IP")
    if not client_ip:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()

    if not client_ip and request.client:
        client_ip = request.client.host

    if not client_ip:
        logger.warning("Unable to determine client IP")
        return False

    try:
        ip = ipaddress.ip_address(client_ip)
        is_local = any(ip in ipaddress.ip_network(cidr) for cidr in LOCAL_CIDRS)
        logger.debug(f"Client IP {client_ip} is {'local' if is_local else 'remote'}")
        return is_local
    except ValueError as e:
        logger.warning(f"Invalid IP address {client_ip}: {e}")
        return False


class SessionMiddleware(BaseHTTPMiddleware):
    """
    Middleware that manages user sessions via secure cookies.

    - Creates a new session if none exists or if the existing one is expired
    - Attaches the session to request.state for use in route handlers
    - Sets secure cookie attributes based on request origin (local vs remote)
    - Skips session creation for health checks, static files, and favicon
    """

    # Paths that don't require session management
    NO_SESSION_PATHS = {
        "/api/health",
        "/favicon.ico",
    }

    # Paths that should not create sessions but can read existing ones
    READ_ONLY_PATHS = {
        "/status/",  # Public status polling
    }

    async def dispatch(self, request: Request, call_next) -> Response:
        # Check if this path requires a session
        path = request.url.path
        requires_session = path not in self.NO_SESSION_PATHS and not path.startswith("/static/")

        # Check if this is a read-only path (can read session but shouldn't create new one)
        is_read_only = any(path.startswith(prefix) for prefix in self.READ_ONLY_PATHS)

        session = None
        is_new = False

        if requires_session:
            # Get existing session ID from cookie
            session_id = request.cookies.get(SESSION_COOKIE_NAME)

            if session_id:
                logger.debug(f"Request to {path} with session cookie: {session_id[:8]}...")
            else:
                logger.debug(f"Request to {path} without session cookie")

            # For read-only paths, only retrieve existing sessions, don't create new ones
            if is_read_only and not session_id:
                logger.debug(f"Read-only path {path} without session - not creating new session")
                session = None
            else:
                # Get or create session
                session, is_new = get_or_create_session(session_id)

                if is_new:
                    if session_id:
                        logger.warning(
                            f"Created NEW session {session.session_id[:8]}... despite cookie present "
                            f"(cookie value: {session_id[:8]}...) for {path}. "
                            f"This indicates the session expired or was invalid."
                        )
                    else:
                        logger.info(f"Created new session {session.session_id[:8]}... for {path}")
                else:
                    logger.debug(f"Using existing session {session.session_id[:8]}... for {path}")

        # Attach session to request state (None for paths that don't need sessions)
        request.state.session = session

        # Process request
        response: Response = await call_next(request)

        # Set/refresh session cookie ONLY if:
        # 1. Path requires sessions
        # 2. Session exists
        # 3. It's not a read-only path OR it's a new session
        should_set_cookie = (
            requires_session
            and session is not None
            and (not is_read_only or is_new)
        )

        if should_set_cookie:
            # Use secure cookies for non-local requests (assumes HTTPS via reverse proxy)
            is_local = is_local_request(request)

            # For local requests, don't set secure flag to ensure cookie works over HTTP
            # For remote requests (via Cloudflare/nginx), always use secure flag
            cookie_secure = not is_local

            # Set SameSite to Lax to allow cookies on GET requests from external sources
            # This is important for download links shared between devices
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=session.session_id,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                secure=cookie_secure,
                samesite="lax",
                path="/",  # Cookie available for all paths
            )

            logger.debug(
                f"Set session cookie for {session.session_id[:8]}... "
                f"(path={path}, is_local={is_local}, secure={cookie_secure})"
            )

        return response
