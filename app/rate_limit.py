from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory per-IP limits. Behind a proxy, run uvicorn with --proxy-headers so
# the client IP is taken from X-Forwarded-For. Use a Redis storage_uri if you
# run more than one backend instance.
limiter = Limiter(key_func=get_remote_address)
