# Security Policy

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please use GitHub's private vulnerability reporting:

1. Go to https://github.com/GeiserX/atlassian-browser-mcp/security/advisories
2. Click "Report a vulnerability"
3. Fill out the form with details

We will respond within **48 hours** and work with you to understand and address the issue.

### What to Include

- Type of issue (e.g., cookie leakage, session hijacking, credential exposure)
- Full paths of affected source files
- Step-by-step instructions to reproduce
- Proof-of-concept or exploit code (if possible)
- Impact assessment and potential attack scenarios

### Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x.x   | Current release   |

Only the latest version receives security updates. We recommend always running the latest version.

## Security Architecture

### Authentication

- **Browser-cookie SSO** - No API tokens or passwords stored in config; cookies are captured out-of-band by the Chrome extension export, never by driving a browser
- **Local storage state** - Cookies persisted locally in gitignored per-service jars
- **Automatic session refresh** - SSO redirect / 401 detection reloads cookies from the saved jar; no browser is ever opened
- **Configurable SSO markers** - Adapt detection to your identity provider

### Data Protection

- **No credentials in code** - All URLs and settings via environment variables
- **Local-only cookie storage** - `.atlassian-browser-state-*.json` jars and any `atlassian-cookies*.json` export are gitignored
- **Least-privilege extension** - The Chrome extension requests cookie access only for the specific Jira/Confluence origins you enter, at runtime

### For Users

1. **Never commit the cookie jars or exports** - `.atlassian-browser-state-*.json` and `atlassian-cookies*.json` contain live session cookies
2. **Delete the extension export after importing** - Treat `atlassian-cookies.json` like a password
3. **Use environment variables** for all configuration
4. **Keep updated** - Run the latest version of both this wrapper and `mcp-atlassian`

## Contact

For security questions that aren't vulnerabilities, open a regular issue.

---

*Last updated: April 2025*
