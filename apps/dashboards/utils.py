import hashlib
import json
from django.core.cache import cache
from django.utils import timezone

def generate_cache_key(prefix, *args, **kwargs):
    """Generate consistent cache keys"""
    key_parts = [prefix]
    key_parts.extend(str(arg) for arg in args)
    key_parts.extend(f"{k}:{v}" for k, v in sorted(kwargs.items()))
    key_string = ":".join(key_parts)
    return hashlib.md5(key_string.encode()).hexdigest()

def cache_result(timeout=300):
    """Decorator to cache function results"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            cache_key = generate_cache_key(func.__name__, *args, **kwargs)
            result = cache.get(cache_key)
            if result is None:
                result = func(*args, **kwargs)
                cache.set(cache_key, result, timeout)
            return result
        return wrapper
    return decorator

def validate_workspace_limits(workspace):
    """Check if workspace is within usage limits"""
    usage = workspace.get_usage_stats()
    
    if usage['tables'] >= usage['tables_limit']:
        return False, "Table limit reached"
    
    if usage['records'] >= usage['records_limit']:
        return False, "Record limit reached"
    
    if usage['members'] >= usage['members_limit']:
        return False, "Team member limit reached"
    
    return True, "OK"