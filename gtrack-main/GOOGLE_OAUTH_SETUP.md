# Google OAuth Login Setup (Django Allauth)

## Steps

1. Create an OAuth Client in Google Cloud Console
- Go to `APIs & Services → Credentials → Create Credentials → OAuth client ID`
- Application type: `Web application`
- Authorized JavaScript origins:
  - `http://127.0.0.1:8000`
  - Add `http://127.0.0.1:8001` if you run a second dev server
- Authorized redirect URIs:
  - `http://127.0.0.1:8000/accounts/google/login/callback/`
  - Add `http://127.0.0.1:8001/accounts/google/login/callback/` if using port 8001
- Save the `Client ID` and `Client Secret`.

2. Set credentials in `.env` (project root `gtrack-main/gtrack-main`)
```
GOOGLE_CLIENT_ID=your-google-oauth-client-id
GOOGLE_CLIENT_SECRET=your-google-oauth-client-secret
```

3. Restart the Django server
- Restart both servers if you have two running.

4. Verify the flow
- Visit `/accounts/login/` or `/login/`
- The "Login with Gmail" button appears only when `GOOGLE_CLIENT_ID` is set.
- Clicking it should redirect to Google without the "Missing required parameter: client_id" error.

## Notes
- This project uses Allauth with provider APP credentials from settings:
  - `SOCIALACCOUNT_PROVIDERS['google']['APP']`
- The Sites framework and SocialApp DB entries are not required with this approach.
- Ensure your `ALLOWED_HOSTS` include `127.0.0.1` and `localhost` during development.