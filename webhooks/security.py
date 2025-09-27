"""
Comprehensive webhook security system with rate limiting, IP allowlisting, 
signature verification, and authentication
"""
import hmac
import hashlib
import time
import json
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, current_app, g
from werkzeug.exceptions import RequestEntityTooLarge
import ipaddress

logger = logging.getLogger(__name__)

# In-memory storage for rate limiting (in production, use Redis)
rate_limit_storage = {}
failed_attempts_storage = {}

class WebhookSecurityError(Exception):
    """Custom exception for webhook security violations"""
    def __init__(self, message, status_code=401):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class WebhookSecurity:
    """Comprehensive webhook security manager"""
    
    # Known webhook source IP ranges (can be configured via environment)
    TRUSTED_IP_RANGES = {
        'github': [
            '140.82.112.0/20',
            '185.199.108.0/22',
            '192.30.252.0/22',
            '143.55.64.0/20'
        ],
        'stripe': [
            '54.187.174.169/32',
            '54.187.205.235/32',
            '54.187.216.72/32',
            '54.241.31.99/32',
            '54.241.31.102/32',
            '54.241.34.107/32'
        ]
    }
    
    # Rate limiting settings
    RATE_LIMITS = {
        'default': {'requests': 100, 'window': 3600},  # 100 requests per hour
        'github': {'requests': 1000, 'window': 3600},  # Higher limit for GitHub
        'stripe': {'requests': 500, 'window': 3600},   # Moderate limit for Stripe
    }
    
    # Maximum payload sizes (in bytes)
    MAX_PAYLOAD_SIZES = {
        'default': 1024 * 1024,  # 1MB
        'github': 25 * 1024 * 1024,  # 25MB for GitHub (large repos)
        'stripe': 4096,  # 4KB for Stripe (small events)
    }
    
    @staticmethod
    def validate_request_size(source):
        """Validate request payload size"""
        max_size = WebhookSecurity.MAX_PAYLOAD_SIZES.get(
            source, 
            WebhookSecurity.MAX_PAYLOAD_SIZES['default']
        )
        
        content_length = request.content_length
        if content_length and content_length > max_size:
            raise WebhookSecurityError(
                f"Payload too large: {content_length} bytes > {max_size} bytes allowed",
                413
            )
    
    @staticmethod
    def validate_ip_allowlist(source):
        """Validate request comes from trusted IP ranges"""
        # Skip IP validation in development
        if current_app.config.get('ENV') == 'development':
            return True
            
        # Use trusted remote_addr (sanitized by ProxyFix) instead of spoofable headers
        client_ip = request.remote_addr
        if not client_ip:
            raise WebhookSecurityError("Unable to determine client IP", 400)
        
        # Get trusted IP ranges for this source
        trusted_ranges = WebhookSecurity.TRUSTED_IP_RANGES.get(source, [])
        
        # If no trusted ranges configured, allow all (but log warning)
        if not trusted_ranges:
            logger.warning(f"No IP allowlist configured for webhook source: {source}")
            return True
        
        try:
            client_addr = ipaddress.ip_address(client_ip.split(',')[0].strip())
            
            for ip_range in trusted_ranges:
                if client_addr in ipaddress.ip_network(ip_range):
                    return True
            
            raise WebhookSecurityError(
                f"IP {client_ip} not in allowlist for {source}",
                403
            )
            
        except (ipaddress.AddressValueError, ValueError) as e:
            logger.error(f"Invalid IP address format: {client_ip} - {str(e)}")
            raise WebhookSecurityError("Invalid IP address format", 400)
    
    @staticmethod
    def check_rate_limit(source):
        """Check and update rate limiting for webhook source"""
        # Use trusted remote_addr (sanitized by ProxyFix) instead of spoofable headers
        client_ip = request.remote_addr
        rate_key = f"{source}:{client_ip}"
        
        current_time = time.time()
        
        # Get rate limit settings for this source
        limits = WebhookSecurity.RATE_LIMITS.get(
            source, 
            WebhookSecurity.RATE_LIMITS['default']
        )
        
        max_requests = limits['requests']
        window_size = limits['window']
        
        # Clean up old entries
        if rate_key in rate_limit_storage:
            rate_limit_storage[rate_key] = [
                timestamp for timestamp in rate_limit_storage[rate_key]
                if current_time - timestamp < window_size
            ]
        else:
            rate_limit_storage[rate_key] = []
        
        # Check if rate limit exceeded
        if len(rate_limit_storage[rate_key]) >= max_requests:
            # Log rate limit violation
            logger.warning(
                f"Rate limit exceeded for {source} from {client_ip}: "
                f"{len(rate_limit_storage[rate_key])} requests in {window_size}s"
            )
            raise WebhookSecurityError("Rate limit exceeded", 429)
        
        # Add current request timestamp
        rate_limit_storage[rate_key].append(current_time)
    
    @staticmethod
    def verify_signature(source, payload, headers):
        """Enhanced signature verification with better error handling"""
        try:
            # Get webhook secret from environment
            secret_key = current_app.config.get(f'WEBHOOK_{source.upper()}_SECRET')
            
            if not secret_key:
                # In production, require secrets for known services
                if current_app.config.get('ENV') == 'production' and source in ['github', 'stripe']:
                    raise WebhookSecurityError(f"Webhook secret required for {source} in production")
                
                logger.info(f"No webhook secret configured for {source}, skipping signature verification")
                return True
            
            # GitHub signature verification
            if source == 'github':
                return WebhookSecurity._verify_github_signature(payload, headers, secret_key)
            
            # Stripe signature verification  
            elif source == 'stripe':
                return WebhookSecurity._verify_stripe_signature(payload, headers, secret_key)
            
            # Generic HMAC verification
            else:
                return WebhookSecurity._verify_generic_signature(payload, headers, secret_key)
                
        except WebhookSecurityError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error verifying webhook signature for {source}: {str(e)}")
            raise WebhookSecurityError("Signature verification failed")
    
    @staticmethod
    def _verify_github_signature(payload, headers, secret_key):
        """Verify GitHub webhook signature"""
        signature = headers.get('X-Hub-Signature-256')
        if not signature:
            raise WebhookSecurityError("Missing GitHub signature header")
        
        if not signature.startswith('sha256='):
            raise WebhookSecurityError("Invalid GitHub signature format")
        
        expected_signature = 'sha256=' + hmac.new(
            secret_key.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected_signature):
            raise WebhookSecurityError("Invalid GitHub signature")
        
        return True
    
    @staticmethod
    def _verify_stripe_signature(payload, headers, secret_key):
        """Verify Stripe webhook signature with enhanced security"""
        signature = headers.get('Stripe-Signature')
        if not signature:
            raise WebhookSecurityError("Missing Stripe signature header")
        
        try:
            # Parse Stripe signature format: t=timestamp,v1=signature
            signature_elements = {}
            for element in signature.split(','):
                if '=' in element:
                    key, value = element.split('=', 1)
                    signature_elements[key] = value
            
            timestamp = signature_elements.get('t')
            stripe_signature = signature_elements.get('v1')
            
            if not timestamp or not stripe_signature:
                raise WebhookSecurityError("Invalid Stripe signature format")
            
            # Verify timestamp is valid integer
            try:
                webhook_timestamp = int(timestamp)
            except ValueError:
                raise WebhookSecurityError("Invalid timestamp in Stripe signature")
            
            # Check for replay attacks (stricter 5-minute window)
            current_timestamp = int(datetime.utcnow().timestamp())
            if abs(current_timestamp - webhook_timestamp) > 300:  # 5 minutes
                raise WebhookSecurityError("Webhook timestamp too old (possible replay attack)")
            
            # Create the signed payload
            signed_payload = timestamp + '.' + payload
            
            # Compute expected signature
            expected_signature = hmac.new(
                secret_key.encode('utf-8'),
                signed_payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            if not hmac.compare_digest(stripe_signature, expected_signature):
                raise WebhookSecurityError("Invalid Stripe signature")
            
            return True
            
        except WebhookSecurityError:
            raise
        except Exception as e:
            logger.error(f"Error parsing Stripe signature: {str(e)}")
            raise WebhookSecurityError("Invalid Stripe signature format")
    
    @staticmethod
    def _verify_generic_signature(payload, headers, secret_key):
        """Verify generic webhook signature"""
        signature = headers.get('X-Signature') or headers.get('X-Hub-Signature')
        if not signature:
            raise WebhookSecurityError("Missing webhook signature header")
        
        # Handle different signature formats
        if signature.startswith('sha256='):
            signature = signature[7:]
        elif signature.startswith('sha1='):
            # Reject SHA1 signatures as they're insecure
            raise WebhookSecurityError("SHA1 signatures not supported (use SHA256)")
        
        expected_signature = hmac.new(
            secret_key.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected_signature):
            raise WebhookSecurityError("Invalid webhook signature")
        
        return True
    
    @staticmethod
    def track_failed_attempt(source):
        """Track failed webhook attempts for security monitoring"""
        # Use trusted remote_addr (sanitized by ProxyFix) instead of spoofable headers
        client_ip = request.remote_addr
        attempt_key = f"{source}:{client_ip}"
        current_time = time.time()
        
        if attempt_key not in failed_attempts_storage:
            failed_attempts_storage[attempt_key] = []
        
        failed_attempts_storage[attempt_key].append(current_time)
        
        # Clean up old attempts (older than 1 hour)
        failed_attempts_storage[attempt_key] = [
            timestamp for timestamp in failed_attempts_storage[attempt_key]
            if current_time - timestamp < 3600
        ]
        
        # Log suspicious activity (more than 10 failed attempts in an hour)
        if len(failed_attempts_storage[attempt_key]) > 10:
            logger.warning(
                f"Suspicious webhook activity detected for {source} from {client_ip}: "
                f"{len(failed_attempts_storage[attempt_key])} failed attempts in last hour"
            )
    
    @staticmethod
    def validate_content_type():
        """Validate request content type"""
        content_type = request.content_type
        
        # Allow JSON and form data
        allowed_types = [
            'application/json',
            'application/x-www-form-urlencoded',
            'text/plain'  # Some services send plain text
        ]
        
        if content_type and not any(content_type.startswith(t) for t in allowed_types):
            raise WebhookSecurityError(f"Unsupported content type: {content_type}", 415)
    
    @staticmethod
    def sanitize_headers(headers):
        """Sanitize and filter webhook headers for logging"""
        safe_headers = {}
        
        # Only include webhook-related headers
        for key, value in headers.items():
            if key.lower().startswith(('x-', 'stripe-', 'user-agent', 'content-')):
                # Sanitize header values (remove potential sensitive data)
                if 'signature' in key.lower():
                    # Keep signature format but hide actual value
                    if '=' in value:
                        parts = value.split('=', 1)
                        safe_headers[key] = f"{parts[0]}=***REDACTED***"
                    else:
                        safe_headers[key] = "***REDACTED***"
                else:
                    safe_headers[key] = value[:200]  # Limit header value length
        
        return safe_headers


def require_webhook_security(f):
    """
    Decorator to apply comprehensive webhook security checks
    """
    @wraps(f)
    def decorated_function(source, *args, **kwargs):
        try:
            # Start security validation timer
            start_time = time.time()
            
            # 1. Validate request size
            WebhookSecurity.validate_request_size(source)
            
            # 2. Validate content type
            WebhookSecurity.validate_content_type()
            
            # 3. Check IP allowlist
            WebhookSecurity.validate_ip_allowlist(source)
            
            # 4. Check rate limiting
            WebhookSecurity.check_rate_limit(source)
            
            # 5. Verify webhook signature
            payload = request.get_data(as_text=True)
            headers = dict(request.headers)
            WebhookSecurity.verify_signature(source, payload, headers)
            
            # Store sanitized security context for the request
            g.webhook_security = {
                'source': source,
                'client_ip': request.remote_addr,  # Use trusted remote_addr (sanitized by ProxyFix)
                'validation_time': time.time() - start_time,
                'payload_size': len(payload),
                'headers': WebhookSecurity.sanitize_headers(headers)
            }
            
            logger.info(
                f"Webhook security validation passed for {source} "
                f"(validation time: {g.webhook_security['validation_time']:.3f}s)"
            )
            
            return f(source, *args, **kwargs)
            
        except WebhookSecurityError as e:
            # Track failed attempt
            WebhookSecurity.track_failed_attempt(source)
            
            logger.warning(
                f"Webhook security violation for {source}: {e.message} "
                f"(IP: {request.remote_addr})"
            )
            
            return jsonify({
                'error': 'Security validation failed',
                'message': e.message
            }), e.status_code
            
        except Exception as e:
            logger.error(f"Unexpected security error for {source}: {str(e)}")
            return jsonify({
                'error': 'Security validation failed',
                'message': 'Internal security error'
            }), 500
    
    return decorated_function


def require_admin_auth(f):
    """
    Decorator to require authentication for administrative webhook endpoints
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for admin token in headers
        admin_token = request.headers.get('X-Admin-Token')
        expected_token = current_app.config.get('WEBHOOK_ADMIN_TOKEN')
        
        if not expected_token:
            return jsonify({'error': 'Admin endpoints disabled'}), 503
        
        if not admin_token or not hmac.compare_digest(admin_token, expected_token):
            logger.warning(
                f"Unauthorized admin access attempt from {request.remote_addr}"
            )
            return jsonify({'error': 'Authentication required'}), 401
        
        return f(*args, **kwargs)
    
    return decorated_function