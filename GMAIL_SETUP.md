# Gmail SMTP Setup for Password Reset

## Quick Setup Instructions

1. **Enable 2-Step Verification** in your Google Account:
   - Go to https://myaccount.google.com/security
   - Turn on 2-Step Verification if not already enabled

2. **Create an App Password**:
   - Go to https://myaccount.google.com/apppasswords
   - Select "Mail" and your device
   - Copy the 16-character app password (no spaces)

3. **Update your .env file** in the project root:
   ```
   GMAIL_USER=your-email@gmail.com
   GMAIL_APP_PASSWORD=your-16-char-app-password
   DEFAULT_FROM_EMAIL=your-email@gmail.com
   ```

4. **Restart the Django server** after updating .env:
   ```
   python manage.py runserver
   ```

## Testing Password Reset

### Firebase Password Reset (Primary)
- Enter your email on the login page
- Click "Forgot password?"
- Check browser console for detailed error messages if it fails

### Django/Gmail Password Reset (Fallback)
- Click "Use server-side reset instead" link
- This uses Gmail SMTP to send password reset emails

## Common Issues

### Firebase Reset Fails
- **User not found**: The email isn't registered in Firebase
- **Unauthorized domain**: Add your domain to Firebase Console → Authentication → Settings → Authorized domains
- **Network error**: Check internet connection and Firebase project status

### Gmail SMTP Fails
- **Authentication failed**: Check app password is correct (16 characters, no spaces)
- **Less secure apps**: Use App Password instead of regular password
- **2-Step Verification**: Must be enabled to create app passwords

## Firebase Console Settings

1. Go to https://console.firebase.google.com/
2. Select your project: `g-trackapp`
3. Go to Authentication → Settings
4. Add authorized domains:
   - `localhost` (for development)
   - Your production domain (when deploying)

## Testing Commands

Test Gmail SMTP from Django shell:
```python
python manage.py shell
from django.core.mail import send_mail
send_mail('Test', 'Test message', 'your-email@gmail.com', ['recipient@example.com'])
```