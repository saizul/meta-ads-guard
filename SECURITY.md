# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
private vulnerability reporting ("Report a vulnerability" under the
repository's **Security** tab). You will get an answer within a week.

Useful reports include the version, the configuration involved (with secrets
removed) and steps to reproduce.

## Handling credentials

meta-ads-guard is built so that secrets never have to be written to disk by
it:

- The access token, app secret, Telegram bot token, webhook URLs and SMTP
  password are read from environment variables named in the config. Literal
  tokens in the config are rejected (`api.access_token`) or warned about
  (`webhook.url`).
- Tokens are sent as request parameters or in the request body, never logged.
  URLs in error messages are passed through a redactor that hides
  `access_token`, `appsecret_proof` and Telegram bot tokens.
- Set `api.app_secret_env` to sign every call with `appsecret_proof`, and turn
  on "Require App Secret" in the app settings so a leaked token alone is not
  enough.

Recommendations:

- Use a **system user** token from Business Manager with only the
  `ads_read` and `ads_management` permissions, scoped to the ad accounts the
  tool should manage. Use `ads_read` alone if you only want alerts.
- Keep `.env`, `guard.yaml` with real ids, the state file and the audit log out
  of version control (the provided `.gitignore` does this).
- Run without `--apply` first and read the plan.
