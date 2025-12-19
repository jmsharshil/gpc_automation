# authentication/views.py
import requests
import secrets
import hashlib
import base64
from urllib.parse import urlencode
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import JsonResponse, HttpResponseRedirect
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

# Store state tokens and PKCE verifiers temporarily (use Redis in production)
_state_storage = {}
_pkce_storage = {}

def generate_pkce_pair():
    """Generate PKCE code verifier and challenge"""
    # Generate code verifier (43-128 characters)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
    
    # Generate code challenge (SHA256 hash of verifier, base64url encoded)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('utf-8')).digest()
    ).decode('utf-8').rstrip('=')
    
    return code_verifier, code_challenge

@api_view(['GET'])
@permission_classes([AllowAny])
def microsoft_login(request):
    """
    Generate Microsoft OAuth login URL with PKCE
    """
    # Generate a random state token for CSRF protection
    state = secrets.token_urlsafe(32)
    
    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce_pair()
    
    # Store state and code verifier (use Redis in production)
    _state_storage[state] = True
    _pkce_storage[state] = code_verifier
    
    params = {
        'client_id': settings.MICROSOFT_OAUTH_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': settings.MICROSOFT_OAUTH_REDIRECT_URI,
        'response_mode': 'query',
        'scope': 'openid profile email User.Read',
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',  # SHA256
    }
    
    auth_url = f"{settings.MICROSOFT_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
    
    return Response({
        'auth_url': auth_url,
        'state': state
    })

@api_view(['GET'])
@permission_classes([AllowAny])
def microsoft_callback(request):
    code = request.GET.get("code")
    state = request.GET.get("state")
    error = request.GET.get("error")

    if error:
        return Response({"error": error}, status=400)

    if not code:
        return Response({"error": "Authorization code missing"}, status=400)

    if state not in _state_storage:
        return Response({"error": "Invalid state"}, status=400)

    code_verifier = _pkce_storage.get(state)

    # cleanup
    del _state_storage[state]
    del _pkce_storage[state]

    token_data = {
        "client_id": settings.MICROSOFT_OAUTH_CLIENT_ID,
        "client_secret": settings.MICROSOFT_OAUTH_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.MICROSOFT_OAUTH_REDIRECT_URI,
        "code_verifier": code_verifier,
    }

    token_response = requests.post(
        settings.MICROSOFT_OAUTH_TOKEN_URL, data=token_data
    )
    token_response.raise_for_status()
    token_json = token_response.json()

    access_token = token_json.get("access_token")

    headers = {"Authorization": f"Bearer {access_token}"}
    user_response = requests.get(
        settings.MICROSOFT_GRAPH_USER_URL, headers=headers
    )
    user_response.raise_for_status()
    user_data = user_response.json()

    user = create_or_get_user(user_data)

    refresh = RefreshToken.for_user(user)
    access_jwt = refresh.access_token

    # 🔥 FRONTEND REDIRECT (THIS IS THE KEY)
    frontend_url = "https://gpcblobstorage.z30.web.core.windows.net/auth/microsoft/success"
    redirect_url = (
        f"{frontend_url}"
        f"?access_token={access_jwt}"
        f"&refresh_token={refresh}"
        f"&role={user.role}"
    )

    return HttpResponseRedirect(redirect_url)


def create_or_get_user(user_data):
    """
    Create or retrieve user from Microsoft user data
    """
    microsoft_id = user_data.get('id')
    email = user_data.get('mail') or user_data.get('userPrincipalName')
    display_name = user_data.get('displayName', '')
    given_name = user_data.get('givenName', '')
    surname = user_data.get('surname', '')
    
    # Try to find existing user by Microsoft ID
    user = User.objects.filter(microsoft_id=microsoft_id).first()
    
    if user:
        # Update existing user info
        user.email = email
        user.first_name = given_name
        user.last_name = surname
        user.save()
        return user
    
    # Try to find existing user by email
    user = User.objects.filter(email=email).first()
    
    if user:
        # Link existing user with Microsoft account
        user.microsoft_id = microsoft_id
        user.is_microsoft_user = True
        user.save()
        return user
    
    # Create new user
    username = email.split('@')[0]  # Use email prefix as username
    
    # Ensure unique username
    counter = 1
    original_username = username
    while User.objects.filter(username=username).exists():
        username = f"{original_username}_{counter}"
        counter += 1
    
    # Determine role based on email domain or other criteria
    role = determine_user_role(email, user_data)
    
    user = User.objects.create(
        username=username,
        email=email,
        first_name=given_name,
        last_name=surname,
        microsoft_id=microsoft_id,
        is_microsoft_user=True,
        role=role,
        is_active=True,
    )
    
    return user

def determine_user_role(email, user_data):
    """
    Determine user role based on email or other criteria
    Customize this logic based on your requirements
    """
    # Example: Admins have specific email domains
    admin_domains = ['admin.company.com', 'management.company.com']
    admin_emails = ['admin@company.com', 'manager@company.com']
    
    email_domain = email.split('@')[-1] if '@' in email else ''
    
    if email in admin_emails or email_domain in admin_domains:
        return 'admin'
    
    # Check if user has admin role in Azure AD (requires additional permissions)
    # This would require additional Graph API calls
    
    return 'user'  # Default role

@api_view(['POST'])
@permission_classes([AllowAny])
def microsoft_login_mobile(request):
    """
    Handle Microsoft OAuth for mobile apps (receives access token directly)
    """
    access_token = request.data.get('access_token')
    
    if not access_token:
        return Response({
            'error': 'Access token is required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        # Get user information from Microsoft Graph
        headers = {'Authorization': f'Bearer {access_token}'}
        user_response = requests.get(settings.MICROSOFT_GRAPH_USER_URL, headers=headers)
        user_response.raise_for_status()
        user_data = user_response.json()
        
        # Create or get user
        user = create_or_get_user(user_data)
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        access = refresh.access_token
        
        return Response({
            'access_token': str(access),
            'refresh_token': str(refresh),
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'is_admin': user.is_admin,
            }
        })
        
    except requests.RequestException as e:
        return Response({
            'error': 'Failed to validate access token',
            'details': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
def user_profile(request):
    """
    Get current user profile
    """
    user = request.user
    return Response({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'role': user.role,
        'is_admin': user.is_admin,
        'is_microsoft_user': user.is_microsoft_user,
    })

@api_view(['POST'])
def logout(request):
    """
    Logout user by blacklisting refresh token
    """
    try:
        refresh_token = request.data.get('refresh_token')
        if refresh_token:
            token = RefreshToken(refresh_token)
            token.blacklist()
        
        return Response({'message': 'Successfully logged out'})
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([AllowAny])
def test_callback(request):
    """
    Test endpoint to manually process callback with authorization code
    Use this for Postman testing
    """
    code = request.data.get('code')
    state = request.data.get('state')
    
    if not code:
        return Response({
            'error': 'Authorization code is required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Verify state token (optional for testing)
    if state and state not in _state_storage:
        return Response({
            'error': 'Invalid state parameter'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    if state and state in _state_storage:
        del _state_storage[state]
    
    # Exchange authorization code for access token
    token_data = {
        'client_id': settings.MICROSOFT_OAUTH_CLIENT_ID,
        'client_secret': settings.MICROSOFT_OAUTH_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': settings.MICROSOFT_OAUTH_REDIRECT_URI,
    }
    
    try:
        token_response = requests.post(settings.MICROSOFT_OAUTH_TOKEN_URL, data=token_data)
        token_response.raise_for_status()
        token_json = token_response.json()
        
        access_token = token_json.get('access_token')
        
        if not access_token:
            return Response({
                'error': 'Failed to obtain access token',
                'details': token_json
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get user information from Microsoft Graph
        headers = {'Authorization': f'Bearer {access_token}'}
        user_response = requests.get(settings.MICROSOFT_GRAPH_USER_URL, headers=headers)
        user_response.raise_for_status()
        user_data = user_response.json()
        
        # Create or get user
        user = create_or_get_user(user_data)
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        access = refresh.access_token
        
        return Response({
            'access_token': str(access),
            'refresh_token': str(refresh),
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'is_admin': user.is_admin,
            },
            'microsoft_user_data': user_data  # For debugging
        })
        
    except requests.RequestException as e:
        return Response({
            'error': 'Failed to authenticate with Microsoft',
            'details': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
@permission_classes([AllowAny])
def microsoft_callback_no_state(request):
    """
    Handle Microsoft OAuth callback without strict state checking (for testing)
    """
    code = request.GET.get('code')
    error = request.GET.get('error')
    state = request.GET.get('state')
    
    # Debug logging
    print(f"Callback received - Code: {bool(code)}, Error: {error}")
    print(f"Full URL: {request.build_absolute_uri()}")
    
    if error:
        return Response({
            'error': 'Authentication failed',
            'details': request.GET.get('error_description', 'Unknown error')
        }, status=status.HTTP_400_BAD_REQUEST)
    
    if not code:
        return Response({
            'error': 'Authorization code not provided',
            'debug': 'No code parameter found in callback URL'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Get code verifier for PKCE if state is available
    code_verifier = _pkce_storage.get(state) if state else None
    
    # Clean up storage
    if state and state in _state_storage:
        del _state_storage[state]
    if state and state in _pkce_storage:
        del _pkce_storage[state]
    
    # Exchange authorization code for access token
    token_data = {
        'client_id': settings.MICROSOFT_OAUTH_CLIENT_ID,
        'client_secret': settings.MICROSOFT_OAUTH_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': settings.MICROSOFT_OAUTH_REDIRECT_URI,
    }
    
    # Add PKCE code verifier if available
    if code_verifier:
        token_data['code_verifier'] = code_verifier
        print(f"Using PKCE code verifier: {code_verifier[:20]}...")
    else:
        print("No PKCE code verifier available")
    
    try:
        print(f"Making token exchange request to: {settings.MICROSOFT_OAUTH_TOKEN_URL}")
        print(f"Token data keys: {list(token_data.keys())}")
        
        token_response = requests.post(settings.MICROSOFT_OAUTH_TOKEN_URL, data=token_data)
        print(f"Token response status: {token_response.status_code}")
        print(f"Token response body: {token_response.text}")
        
        token_response.raise_for_status()
        token_json = token_response.json()
        
        access_token = token_json.get('access_token')
        
        if not access_token:
            return Response({
                'error': 'Failed to obtain access token',
                'token_response': token_json
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get user information from Microsoft Graph
        headers = {'Authorization': f'Bearer {access_token}'}
        user_response = requests.get(settings.MICROSOFT_GRAPH_USER_URL, headers=headers)
        user_response.raise_for_status()
        user_data = user_response.json()
        
        print(f"Microsoft user data: {user_data}")
        
        # Create or get user
        user = create_or_get_user(user_data)
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        access_jwt = refresh.access_token
        
        # For web flow, redirect to frontend with tokens
        redirect_url = "http://localhost:3000"  # Change this to your frontend URL
        if user.is_admin:
            redirect_url += "/admin-dashboard"
        else:
            redirect_url += "/user-dashboard"
        
        # Add tokens as query parameters
        redirect_url += f"?access_token={str(access_jwt)}&refresh_token={str(refresh)}&user_role={user.role}"
        
        # For testing, return JSON instead of redirect
        return Response({
            'success': True,
            'access_token': str(access_jwt),
            'refresh_token': str(refresh),
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'is_admin': user.is_admin,
            },
            'redirect_url': redirect_url,
            'microsoft_data': user_data
        })
        
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return Response({
            'error': 'Failed to authenticate with Microsoft',
            'details': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
    
  
  
# To check env loaded or not   
# from django.conf import settings

# @api_view(['GET'])
# @permission_classes([AllowAny])
# def debug_config(request):
#     """
#     Debug view to check configuration (remove in production)
#     """
#     return Response({
#         'client_id_set': bool(settings.MICROSOFT_OAUTH_CLIENT_ID),
#         'client_secret_set': bool(settings.MICROSOFT_OAUTH_CLIENT_SECRET),
#         'tenant_id_set': bool(settings.MICROSOFT_OAUTH_TENANT_ID),
#         'client_id_length': len(settings.MICROSOFT_OAUTH_CLIENT_ID) if settings.MICROSOFT_OAUTH_CLIENT_ID else 0,
#         # Don't show actual secrets in debug
#     })


@api_view(['GET'])
@permission_classes([AllowAny])
def microsoft_callback_json(request):
    """
    Handle Microsoft OAuth callback and return JSON response for testing
    """
    code = request.GET.get('code')
    state = request.GET.get('state')
    error = request.GET.get('error')
    
    if error:
        return Response({
            'error': 'Authentication failed',
            'details': request.GET.get('error_description', 'Unknown error')
        }, status=status.HTTP_400_BAD_REQUEST)
    
    if not code:
        return Response({
            'error': 'Authorization code not provided'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Get code verifier for PKCE
    code_verifier = _pkce_storage.get(state) if state else None
    
    # Clean up storage
    if state and state in _state_storage:
        del _state_storage[state]
    if state and state in _pkce_storage:
        del _pkce_storage[state]
    
    # Exchange authorization code for access token
    token_data = {
        'client_id': settings.MICROSOFT_OAUTH_CLIENT_ID,
        'client_secret': settings.MICROSOFT_OAUTH_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': settings.MICROSOFT_OAUTH_REDIRECT_URI,
    }
    
    if code_verifier:
        token_data['code_verifier'] = code_verifier
    
    try:
        token_response = requests.post(settings.MICROSOFT_OAUTH_TOKEN_URL, data=token_data)
        token_response.raise_for_status()
        token_json = token_response.json()
        
        access_token = token_json.get('access_token')
        
        if not access_token:
            return Response({
                'error': 'Failed to obtain access token'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get user information from Microsoft Graph
        headers = {'Authorization': f'Bearer {access_token}'}
        user_response = requests.get(settings.MICROSOFT_GRAPH_USER_URL, headers=headers)
        user_response.raise_for_status()
        user_data = user_response.json()
        
        # Create or get user
        user = create_or_get_user(user_data)
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        access_jwt = refresh.access_token
        
        # Return JSON response instead of redirect
        return Response({
            'success': True,
            'message': 'Authentication successful',
            'access_token': str(access_jwt),
            'refresh_token': str(refresh),
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'is_admin': user.is_admin,
                'is_microsoft_user': user.is_microsoft_user,
            }
        })
        
    except requests.RequestException as e:
        return Response({
            'error': 'Failed to authenticate with Microsoft',
            'details': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)