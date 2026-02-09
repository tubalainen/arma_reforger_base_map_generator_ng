# Troubleshooting Guide

## Session Issues Behind Reverse Proxy

### Symptoms
- Progress bar shows "undefined" during map generation
- 404 errors when polling job status
- New session created on every request (visible in logs)
- Logs show `cookie_present=True` but sessions still being recreated

### Root Cause
The reverse proxy (nginx/Cloudflare) is not properly forwarding the `Cookie` header to the FastAPI application, causing sessions to be lost between requests.

### Diagnosis

1. **Check application logs** for warnings like:
   ```
   WARNING: New session created despite cookie present for /api/job/...
   WARNING: No cookies received at all for /api/job/...
   ```

2. **Check if cookies are set in browser**:
   - Open Developer Tools (F12)
   - Go to Application/Storage → Cookies
   - Look for `arma_session` cookie
   - If missing, the application isn't setting cookies properly
   - If present, the reverse proxy isn't forwarding it

3. **Test cookie forwarding**:
   ```bash
   # Test with curl to see if cookies work
   curl -c cookies.txt -b cookies.txt http://your-domain.com/api/health
   curl -b cookies.txt http://your-domain.com/api/health
   # Both should return the same session in the response
   ```

### Solution

#### For Nginx Users

Update your nginx configuration to explicitly forward Cookie headers:

```nginx
location / {
    proxy_pass http://arma_app;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Cookie $http_cookie;  # <-- THIS IS CRITICAL
    proxy_set_header Connection "";
}
```

**Important**: Add `proxy_set_header Cookie $http_cookie;` to **ALL** location blocks that proxy to the application.

See `config/nginx/arma-map-generator.conf.example` for the complete configuration.

#### For Cloudflare Tunnel Users

If using Cloudflare Tunnel (cloudflared), ensure:

1. **Tunnel configuration** (`~/.cloudflared/config.yml`):
   ```yaml
   tunnel: <your-tunnel-id>
   credentials-file: /path/to/credentials.json

   ingress:
     - hostname: your-domain.com
       service: http://localhost:443
       originRequest:
         noTLSVerify: false
         # Cloudflare Tunnel should preserve cookies by default
     - service: http_status:404
   ```

2. **Nginx configuration** must forward cookies (see above)

3. **Cloudflare dashboard settings**:
   - Disable "Browser Integrity Check" if it's interfering
   - Check "Security" → "Settings" for any rules blocking cookies
   - Ensure "Cookie" is not in the list of stripped headers

#### For Apache Users

If using Apache as reverse proxy:

```apache
<VirtualHost *:443>
    ServerName your-domain.com

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:8080/
    ProxyPassReverse / http://127.0.0.1:8080/

    # These directives preserve cookies
    ProxyPassReverseCookiePath / /
    ProxyPassReverseCookieDomain 127.0.0.1 your-domain.com
</VirtualHost>
```

### Verification

After applying the fix:

1. **Restart your reverse proxy**:
   ```bash
   # Nginx
   sudo nginx -t && sudo systemctl reload nginx

   # Apache
   sudo apachectl configtest && sudo systemctl reload apache2

   # Cloudflare Tunnel
   sudo systemctl restart cloudflared
   ```

2. **Clear browser cookies** for your domain

3. **Test the application**:
   - Draw a polygon on the map
   - Click Generate
   - Progress bar should update correctly
   - Check logs for "Existing session" messages (not "New session created")

4. **Check application logs**:
   ```bash
   docker compose logs -f web
   ```

   You should see:
   ```
   INFO: Cookie 'arma_session' found with value: ABC12345...
   DEBUG: Existing session ABC12345... retrieved for /api/job/...
   ```

---

## Other Common Issues

### Issue: "Access denied" errors on job status

**Symptoms**: 403 errors when trying to access job status or download results.

**Cause**: Session mismatch - the job was created by a different session than the one requesting access.

**Solution**:
- Ensure cookies are working (see above)
- Don't share job URLs between different browsers/devices
- Check if browser is blocking third-party cookies

### Issue: Docker container crashes on startup

**Symptoms**: Container exits immediately with GDAL errors.

**Cause**: Missing GDAL dependencies or incorrect Python environment.

**Solution**:
```bash
# Full nuclear restart — removes volumes, rebuilds from scratch, starts fresh, and tails logs
docker compose down -v && docker compose build --no-cache && docker compose up -d && docker compose logs -f
```

### Issue: Elevation data download fails

**Symptoms**: Jobs fail during "Downloading elevation data" step.

**Cause**:
- Missing API keys for country-specific services
- OpenTopography API rate limit
- Network connectivity issues
- Country API returning HTTP 500 due to oversized area request (see below)

**Solution**:
1. Check `.env` file has required API keys
2. Check OpenTopography API key is valid
3. Test connectivity:
   ```bash
   curl "https://portal.opentopography.org/API/globaldem?demtype=COP30&south=58.8&north=58.9&west=16.5&east=16.6&outputFormat=GTiff&API_Key=YOUR_KEY"
   ```

### Issue: Finland (NLS) elevation returns HTTP 500

**Symptoms**: Logs show `HTTP error fetching elevation from Finland: 500` with error `"exceptionCause" is null`.

**Cause**: The Maanmittauslaitos WCS has a **maximum request area of 10 000 × 10 000 metres** for elevation data (and max 5 000 px per axis). Requests for larger areas cause a server-side crash.

**Solution**: The application now automatically chunks oversized requests into tiles that fit within the API limits and merges the results. If you still see this error, ensure you are running the latest version:
```bash
docker compose down -v && docker compose build --no-cache && docker compose up -d && docker compose logs -f
```

### Issue: Denmark (Dataforsyningen) elevation returns "Unsupported FORMAT value"

**Symptoms**: Logs show `WCS 1.0.0 returned XML error: Unsupported FORMAT value`.

**Cause**: The Dataforsyningen DHM WCS only supports `FORMAT=GTiff` — not `image/tiff`. Earlier versions of the application used the wrong format string.

**Solution**: This is now fixed. Rebuild to pick up the corrected format:
```bash
docker compose down -v && docker compose build --no-cache && docker compose up -d && docker compose logs -f
```

### Issue: Sweden (Lantmäteriet) elevation returns errors or falls back to 30m

**Symptoms**: Logs show `Cannot fetch STAC elevation: no authentication credentials` or `OAuth2 auth for Sweden not yet implemented` and Sweden always uses OpenTopography 30m fallback.

**Cause**:
- Missing or incorrect Lantmäteriet credentials
- Using old environment variable names (`LANTMATERIET_CLIENT_ID` / `LANTMATERIET_CLIENT_SECRET`)

**Solution**:
1. Register at [apimanager.lantmateriet.se](https://apimanager.lantmateriet.se/) for free credentials
2. Update your `.env` file with the **new** variable names:
   ```bash
   # OLD (no longer used):
   # LANTMATERIET_CLIENT_ID=
   # LANTMATERIET_CLIENT_SECRET=

   # NEW:
   LANTMATERIET_USERNAME=your_username
   LANTMATERIET_PASSWORD=your_password
   ```
3. Rebuild the container:
   ```bash
   docker compose down && docker compose up --build -d
   ```

**Note**: Without credentials, Sweden gracefully falls back to OpenTopography (30m), Sentinel-2 (10m imagery), and OSM (vector features). All fallbacks work without any credentials.

### Issue: Lantmäteriet STAC elevation returns 403 on asset downloads

**Symptoms**: Logs show `STAC Höjd: searching for elevation items` (search succeeds with items found) but then `HTTP 403 Forbidden` or `account is NOT authorized for Höjddata downloads`.

**Cause**: The STAC catalog search at `api.lantmateriet.se` is open (no auth needed), but the actual data downloads at `dl1.lantmateriet.se` require both:
1. **Valid Basic Auth credentials** (without them you get 401)
2. **An active subscription** to the "Höjddata" product (with valid but unauthorized credentials you get 403)

Getting 403 means your credentials are recognized, but your account hasn't been granted access to elevation data downloads. This is a separate subscription from the general Lantmäteriet API access.

**Solution**:
1. Log in at [apimanager.lantmateriet.se](https://apimanager.lantmateriet.se/)
2. Check your subscriptions — ensure "Höjddata" (elevation data) is included
3. If not subscribed, subscribe to the Höjddata API product (it's free)
4. Test manually:
   ```bash
   # Should return 200 if authorized, 403 if not:
   curl -o /dev/null -w "%{http_code}" -u "your_username:your_password" \
     "https://dl1.lantmateriet.se/hojd/data/grid1m/65_6/55/65825_6750_25.tif"
   ```

**Note**: Even without Höjddata access, the application gracefully falls back to OpenTopography (30m). The orthophoto WMS (historical aerial imagery) uses separate authorization and may work even when Höjddata doesn't.

### Issue: Lantmäteriet orthophoto/STAC returns empty data

**Symptoms**: Logs show `No elevation data found in STAC Höjd search` or `Lantmäteriet orthophoto unavailable, falling back to Sentinel-2`.

**Cause**: The STAC API may not have coverage for the requested area, or the API may be temporarily unavailable.

**Solution**: This is expected behavior — the application automatically falls back to Sentinel-2 imagery and OpenTopography elevation. Check the Lantmäteriet API status at [apimanager.lantmateriet.se](https://apimanager.lantmateriet.se/).

### Issue: Lantmäteriet orthophoto imagery looks outdated

**Symptoms**: Satellite imagery for Swedish maps shows old buildings/roads or outdated development.

**Cause**: Lantmäteriet Historical Orthophotos use the 2005 color layer (`OI.Histortho_color_2005`), the most recent color imagery available through this WMS service.

**Solution**: If you need more current imagery, you can disable Lantmäteriet credentials temporarily (remove from `.env`) and the application will use Sentinel-2 Cloudless 2021 imagery instead (lower resolution but more current).

### Issue: Overpass API returning 504 timeout errors

**Symptoms**: Logs show `Overpass [VK Maps] timeout (504), trying next...` cycling through multiple endpoints, or map features (roads, water, forests, buildings) are missing from the output.

**Cause**: The Overpass API (used for OpenStreetMap data) is a public service that can be overloaded. The main `overpass-api.de` instance is particularly prone to 504 timeouts during peak hours.

**Solution**: The application automatically cycles through 4 public Overpass mirrors (VK Maps → Private.coffee → Kumi → overpass-api.de) with 2 full retry passes. If all fail:
1. Try again later — the public instances may be temporarily overloaded
2. Try a smaller polygon area — large areas generate heavier Overpass queries
3. Check Overpass status: [overpass-api.de/api/status](https://overpass-api.de/api/status)

**Note**: All Overpass mirrors serve identical OpenStreetMap data — the differences are only in server capacity and uptime.

### Issue: Jobs take very long time

**Symptoms**: Generation taking 5+ minutes for small areas.

**Cause**:
- Large polygon area
- High-resolution elevation data
- Slow external API responses
- Container resource constraints

**Solution**:
1. Reduce polygon size
2. Check Docker resource limits in `docker-compose.yml`
3. Monitor container resources:
   ```bash
   docker stats arma-map-generator-1
   ```

---

## Getting Help

If you're still experiencing issues:

1. **Collect diagnostic information**:
   ```bash
   # Application logs (last 100 lines)
   docker compose logs --tail=100 web > app-logs.txt

   # Nginx error logs
   sudo tail -n 100 /var/log/nginx/arma-map-generator.error.log > nginx-error.txt

   # System info
   docker compose ps > docker-status.txt
   docker stats --no-stream >> docker-status.txt
   ```

2. **Enable debug logging**:
   - Edit `webapp/main.py`
   - Change `level=logging.INFO` to `level=logging.DEBUG`
   - Restart container: `docker compose restart web`

3. **Create an issue** on GitHub with:
   - Description of the problem
   - Steps to reproduce
   - Log files (remove any sensitive information)
   - Your reverse proxy configuration (remove domain names/IPs)
   - Browser and OS information

---

## Useful Commands

```bash
# Nuclear restart — tears down everything, rebuilds from scratch, and tails the logs
docker compose down -v && docker compose build --no-cache && docker compose up -d && docker compose logs -f

# View real-time logs
docker compose logs -f web

# Restart application without rebuilding
docker compose restart web

# Rebuild and restart
docker compose up --build -d

# Check nginx configuration
sudo nginx -t

# View nginx access logs
sudo tail -f /var/log/nginx/arma-map-generator.access.log

# Check active sessions in application
curl http://localhost:8080/api/health | jq '.sessions'

# Test session persistence
curl -c /tmp/cookies.txt http://localhost:8080/api/health
curl -b /tmp/cookies.txt http://localhost:8080/api/health
# Should show same session ID in both responses
```
