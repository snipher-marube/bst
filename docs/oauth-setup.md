# OAuth 2.0 Setup — Google & LinkedIn

AnalyticsMeta uses **django-allauth** for social authentication. Both providers require you to register an OAuth app in their developer consoles and add the redirect URIs for your environment.

---

## Google OAuth 2.0

### 1. Create credentials

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Web application**
4. Name: `AnalyticsMeta` (or anything descriptive)

### 2. Add Authorised redirect URIs

Add **both** of these (even in production, the dev URI helps local testing):

| Environment | Redirect URI |
|---|---|
| Development | `http://localhost:8000/accounts/google/login/callback/` |
| Production | `https://your-app.onrender.com/accounts/google/login/callback/` |
| Custom domain | `https://yourdomain.com/accounts/google/login/callback/` |

### 3. Add Authorised JavaScript origins

| Environment | Origin |
|---|---|
| Development | `http://localhost:8000` |
| Production | `https://your-app.onrender.com` |

### 4. Copy credentials

After saving, copy **Client ID** and **Client Secret** into your `.env`:

```env
GOOGLE_CLIENT_ID=1234567890-xxxxxxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxx
```

### 5. Enable the required APIs

In **APIs & Services → Library**, enable:
- **Google+ API** (or **People API**) — required by allauth for profile data

---

## LinkedIn OAuth 2.0

### 1. Create an app

1. Go to [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps)
2. Click **Create App**
3. Fill in App name, LinkedIn Page, and App Logo
4. Under **Products**, request access to **Sign In with LinkedIn using OpenID Connect**

### 2. Add Authorised redirect URLs

Go to your app → **Auth** tab → **OAuth 2.0 settings → Authorized redirect URLs**:

| Environment | Redirect URL |
|---|---|
| Development | `http://localhost:8000/accounts/linkedin_oauth2/login/callback/` |
| Production | `https://your-app.onrender.com/accounts/linkedin_oauth2/login/callback/` |
| Custom domain | `https://yourdomain.com/accounts/linkedin_oauth2/login/callback/` |

### 3. Copy credentials

Under the **Auth** tab, copy **Client ID** and **Client Secret**:

```env
LINKEDIN_CLIENT_ID=77xxxxxxxxxxxx
LINKEDIN_CLIENT_SECRET=xxxxxxxxxxxxxxxx
```

---

## Django Admin setup (one-time, per environment)

After the app is running, log into `/admin/` and configure the social app records:

1. **Sites → Sites** — make sure `example.com` is updated to your actual domain (e.g. `localhost:8000` for dev or `your-app.onrender.com` for production). Note the **site ID** (usually `1`).

2. **Social Accounts → Social applications → Add**:

   For **Google**:
   - Provider: `Google`
   - Name: `Google`
   - Client ID: *(your Google Client ID)*
   - Secret key: *(your Google Client Secret)*
   - Sites: move your site from Available to Chosen

   For **LinkedIn**:
   - Provider: `LinkedIn`
   - Name: `LinkedIn`
   - Client ID: *(your LinkedIn Client ID)*
   - Secret key: *(your LinkedIn Client Secret)*
   - Sites: move your site from Available to Chosen

> **Note**: `SITE_ID = 1` is set in `base.py`. If your Sites table has a different ID for your domain, update `SITE_ID` accordingly.

---

## Testing OAuth locally

LinkedIn does not allow `localhost` as a redirect URI origin in some configurations. Options:

- Use [ngrok](https://ngrok.com): `ngrok http 8000` → use the `https://xxx.ngrok.io` URL as your redirect URI in LinkedIn
- Or skip LinkedIn in local dev and only configure Google

---

## Troubleshooting

**`SocialAccount matching query does not exist`**
The Social Application record in Django admin is missing or not linked to the correct Site.

**`redirect_uri_mismatch` (Google)**
The redirect URI in your request doesn't exactly match what's registered in Google Cloud Console. Check for trailing slashes and `http` vs `https`.

**`invalid_redirect_uri` (LinkedIn)**
LinkedIn is strict — the URL must match character-for-character including protocol and port. Add the exact URL from your browser's address bar when the error occurs.
