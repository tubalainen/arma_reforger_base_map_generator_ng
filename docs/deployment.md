# Production Deployment

Security configuration, and running the app behind nginx and Cloudflare.

# Security & Multi-User Support

The application includes built-in security features for safe deployment:

## Session Management

- **Automatic sessions**: Each user gets a secure session (256-bit cryptographic ID)
- **Job isolation**: Users can only access their own map generation jobs
- **24-hour expiration**: Sessions automatically expire after 24 hours of inactivity

## Security Features

| Feature | Description |
|---------|-------------|
| Rate Limiting | 60 requests/min general, 10 map generations/hour per IP |
| Input Validation | Polygon size limits, job ID format validation |
| Security Headers | CSP, X-Frame-Options, X-Content-Type-Options |
| SRI Hashes | Subresource integrity for all CDN resources |
| Non-root Container | Application runs as unprivileged user |

## Configuration

Security settings can be configured via environment variables in `.env`:

```bash
# CORS origins (for reverse proxy setup)
CORS_ORIGINS=https://your-domain.com,http://localhost:8080

# Rate limiting
RATE_LIMIT_REQUESTS_PER_MINUTE=60
RATE_LIMIT_GENERATE_PER_HOUR=10

# Trusted proxy IPs
FORWARDED_ALLOW_IPS=127.0.0.1

# Log verbosity — applies to `docker compose logs` AND the in-browser
# Activity Log, which are fed from the same records.
# DEBUG adds per-tile / per-feature tracing (noisy but thorough).
LOG_LEVEL=INFO
```

# Reverse Proxy Setup (nginx + Cloudflare)

For production deployment behind nginx and Cloudflare:

## Architecture

```
Internet → Cloudflare (DDoS/WAF) → nginx (rate limiting) → Docker container
```

## Quick Setup

1. **Configure Docker for localhost-only binding** (already default):
   ```yaml
   # docker-compose.yml
   ports:
     - "127.0.0.1:8080:8080"
   ```

2. **Copy the example nginx config**:
   ```bash
   sudo cp config/nginx/arma-map-generator.conf.example \
        /etc/nginx/sites-available/arma-map-generator.conf

   # Edit the file and update:
   # - server_name with your domain
   # - SSL certificate paths

   sudo ln -s /etc/nginx/sites-available/arma-map-generator.conf \
              /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

3. **Configure Cloudflare** (recommended settings):
   - SSL/TLS: Full (Strict)
   - Always Use HTTPS: On
   - Minimum TLS: 1.2
   - Browser Integrity Check: On

4. **Update your `.env`**:
   ```bash
   CORS_ORIGINS=https://your-domain.com
   ```

## Local Network Access

To allow access from your local network (e.g., 192.168.x.x) alongside the reverse proxy:

```yaml
# docker-compose.yml
ports:
  - "127.0.0.1:8080:8080"      # For nginx
  - "192.168.1.100:8080:8080"  # For LAN (use your server's IP)
```

The application automatically detects local network requests and adjusts cookie security accordingly.

## Cloudflare Page Rules (Optional)

For optimal caching:

| Pattern | Setting |
|---------|---------|
| `*/static/*` | Cache Level: Cache Everything, Edge TTL: 1 day |
| `*/api/*` | Cache Level: Bypass |

---

[← Back to the README](../README.md)
