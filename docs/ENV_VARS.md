# Environment Variable Reference

This table documents the runtime environment variables used by the funding bot, web dashboard, queue stack, and local test tooling.

| Area | Variable | Required? | Default | Example | Notes |
| --- | --- | --- | --- | --- | --- |
| Core | `BOT_DB_PATH` | Yes | `funding_bot.db` | `funding_bot.db` | SQLite database path. Use `/app/data/funding_bot.db` in containers. |
| Web | `FLASK_HOST` | No | `127.0.0.1` | `127.0.0.1` | Host used by `web.app.main()`. |
| Web | `FLASK_PORT` | No | `5000` | `5000` | Port used by `web.app.main()`. |
| Web | `ADMIN_PASSWORD` | Yes | *(none)* | `changeme-admin` | Required for the `admin` dashboard role. |
| Web | `STAFF_PASSWORD` | Yes | *(none)* | `changeme-staff` | Required for the `staff` dashboard role. |
| Web | `AUDITOR_PASSWORD` | Yes | *(none)* | `changeme-auditor` | Required for the `auditor` dashboard role. |
| Web | `FLASK_SECRET_KEY` | Recommended | `SECRET_KEY` or `development-only-change-me` | `dev-only-long-random-string` | Preferred Flask session signing key. |
| Web | `SECRET_KEY` | No | `development-only-change-me` | `legacy-secret-key` | Legacy fallback if `FLASK_SECRET_KEY` is unset. |
| Web | `DASHBOARD_SESSION_TIMEOUT_MINUTES` | No | `30` | `45` | Idle session timeout. |
| Web | `SESSION_COOKIE_SECURE` | No | `true` | `0` | Set to `0` for local HTTP development; keep enabled in HTTPS deployments. |
| Web | `SESSION_COOKIE_SAMESITE` | No | `Lax` | `Lax` | Flask session cookie SameSite value. |
| SMTP | `SMTP_HOST` | Optional | `localhost` | `smtp.example.org` | Mail server hostname. |
| SMTP | `SMTP_PORT` | Optional | `587` | `587` | Mail server port. |
| SMTP | `SMTP_USERNAME` | Optional | *(empty)* | `funding-bot@example.org` | SMTP username. |
| SMTP | `SMTP_PASSWORD` | Optional | *(empty)* | `app-password` | SMTP password or token. |
| SMTP | `SMTP_USE_TLS` | Optional | `1` | `1` | Set to `0` to disable STARTTLS. |
| SMTP | `SMTP_FROM` | Optional | `SMTP_USERNAME` | `funding-bot@example.org` | Envelope From address. |
| Privacy | `DATA_RESIDENCY` | Optional | `US` | `EU` | Allowed values: `US`, `EU`, `ASIA`. |
| Privacy | `DATA_STORAGE_REGION` | Optional | `DATA_RESIDENCY` | `EU` | Must match `DATA_RESIDENCY` when residency enforcement is used. |
| Privacy | `PRIVACY_POLICY_OUTPUT_DIR` | Optional | `generated/privacy_policies` | `generated/privacy_policies` | Output directory for generated privacy policies. |
| Privacy | `FUNDING_BOT_ENCRYPTION_KEY` | Recommended | `funding-bot-dev-key` | `replace-with-random-secret` | Encryption seed for stored sensitive fields. |
| Queue | `ENABLE_TASK_QUEUE` | Optional | `0` | `1` | Enables Celery-backed async execution. |
| Queue | `ENABLE_LEGACY_CRON` | Optional | `1` | `1` | Keeps cron/CLI scheduling active. |
| Queue | `CELERY_BROKER_URL` | Required when queue enabled | `redis://redis:6379/0` | `redis://redis:6379/0` | Broker URL. RabbitMQ is also supported. |
| Queue | `CELERY_RESULT_BACKEND` | Required when queue enabled | `redis://redis:6379/1` | `redis://redis:6379/1` | Result backend URL. |
| Queue | `CELERY_QUEUE_NAME` | Optional | `funding-bot` | `funding-bot` | Queue consumed by workers. |
| Queue | `CELERY_TASK_ALWAYS_EAGER` | Optional | `0` | `1` | Runs queued work inline for tests/debugging. |
| Queue | `CELERY_HEALTH_TIMEOUT_SECONDS` | Optional | `2.0` | `2.0` | Web health-check timeout for broker/worker inspection. |
| Queue | `CELERY_INSPECT_TIMEOUT_SECONDS` | Optional | `1.0` | `2.0` | Queue inspection timeout used by queue config and as a fallback for health checks. |
| Queue | `CELERY_FILESYSTEM_BROKER_DIR` | Optional | `.celery-broker` | `.celery-broker` | Root directory for the filesystem broker transport. |
| Queue | `FUNDING_BOT_TASK_RETRY_LIMIT` | Optional | `3` | `5` | Maximum retries after the initial queue-task failure. |
| Queue | `FUNDING_BOT_TASK_RETRY_BACKOFF_SECONDS` | Optional | `5` | `10` | Base retry backoff in seconds. |
| Queue | `FUNDING_BOT_TASK_RETRY_BACKOFF_MAX_SECONDS` | Optional | `300` | `120` | Maximum retry backoff in seconds. |
| Queue | `DAILY_SUMMARY_RECIPIENT` | Optional | `lupael@i4e.com.bd` | `team@example.org` | Default recipient for scheduled daily summaries. |
| Queue | `DAILY_SUMMARY_DRY_RUN` | Optional | `0` | `1` | If true, scheduled daily summaries are rendered but not sent. |
| Queue | `DAILY_SUMMARY_SCHEDULE_HOUR` | Optional | `9` | `9` | UTC hour for the scheduled daily summary. |
| Queue | `DAILY_SUMMARY_SCHEDULE_MINUTE` | Optional | `0` | `0` | UTC minute for the scheduled daily summary. |
| Queue | `FLOWER_BASIC_AUTH` | Optional | *(empty)* | `admin:secret` | Basic auth credentials for Flower in Docker Compose. |
| Connectors | `FUNDING_BOT_CONNECTORS` | Optional | *(empty)* | `{"connectors":[{"type":"globalgiving","transport":"http"}]}` | JSON override for connector definitions. |
| Connectors | `PORTAL_PAGE_SIZE` | Optional | connector default | `100` | Global default page size for paginated connectors. |
| Connectors | `PORTAL_CACHE_TTL` | Optional | `300` | `300` | Global connector cache TTL in seconds. |
| Connectors | `PORTAL_FALLBACK_MODE` | Optional | `cache-first` | `cache-only` | Allowed values: `cache-first`, `cache-only`, `default-only`, `disabled`. |
| Connectors | `PORTAL_RATE_LIMIT_DEFAULT_CAPACITY` | Optional | `5` | `5` | Global per-connector burst size. |
| Connectors | `PORTAL_RATE_LIMIT_DEFAULT_REFILL_RATE` | Optional | `1` | `1` | Global per-connector refill rate in tokens/second. |
| Connectors | `OAUTH2_REFRESH_SKEW_SECONDS` | Optional | `60` | `120` | Refresh OAuth2 credentials before expiry by this many seconds. |
| Connectors | `GRANTS_GOV_API_BASE_URL` | Optional | `https://api.grants.gov/v1/api/search2` | `https://api.grants.gov/v1/api/search2` | Grants.gov endpoint override. |
| Connectors | `CSR_NETWORK_API_BASE_URL` | Optional | `https://api.candid.org/rfp/v1/opportunity` | `https://api.candid.org/rfp/v1/opportunity` | CSR/Candid endpoint override. |
| Connectors | `GRANTS_GOV_API_CREDENTIALS` | Optional | *(empty)* | `{"api_key":"replace-me"}` | JSON credentials for the Grants.gov connector. |
| Connectors | `CSR_NETWORK_API_CREDENTIALS` | Optional | *(empty)* | `{"subscription_key":"replace-me"}` | JSON credentials for the CSR Network connector. |
| Connectors | `GRANTS_PORTAL_PAGE_SIZE` | Optional | inherits `PORTAL_PAGE_SIZE` | `100` | Grants Portal page size override. |
| Connectors | `GRANTS_PORTAL_CACHE_TTL` | Optional | inherits `PORTAL_CACHE_TTL` | `300` | Grants Portal cache TTL override. |
| Connectors | `GRANTS_PORTAL_RATE_LIMIT_CAPACITY` | Optional | inherits global default | `5` | Grants Portal burst size override. |
| Connectors | `GRANTS_PORTAL_RATE_LIMIT_REFILL_RATE` | Optional | inherits global default | `1` | Grants Portal refill-rate override. |
| Connectors | `CSR_NETWORK_PAGE_SIZE` | Optional | inherits `PORTAL_PAGE_SIZE` | `100` | CSR Network page size override. |
| Connectors | `CSR_NETWORK_CACHE_TTL` | Optional | inherits `PORTAL_CACHE_TTL` | `300` | CSR Network cache TTL override. |
| Connectors | `CSR_NETWORK_RATE_LIMIT_CAPACITY` | Optional | inherits global default | `5` | CSR Network burst size override. |
| Connectors | `CSR_NETWORK_RATE_LIMIT_REFILL_RATE` | Optional | inherits global default | `1` | CSR Network refill-rate override. |
| Connectors | `NGO_DIRECTORY_PAGE_SIZE` | Optional | inherits `PORTAL_PAGE_SIZE` | `100` | NGO Directory page size override. |
| Connectors | `NGO_DIRECTORY_CACHE_TTL` | Optional | inherits `PORTAL_CACHE_TTL` | `300` | NGO Directory cache TTL override. |
| Connectors | `NGO_DIRECTORY_RATE_LIMIT_CAPACITY` | Optional | inherits global default | `5` | NGO Directory burst size override. |
| Connectors | `NGO_DIRECTORY_RATE_LIMIT_REFILL_RATE` | Optional | inherits global default | `1` | NGO Directory refill-rate override. |
| Connectors | `FOUNDATION_DIRECTORY_PAGE_SIZE` | Optional | inherits `PORTAL_PAGE_SIZE` | `100` | Foundation Directory page size override. |
| Connectors | `FOUNDATION_DIRECTORY_CACHE_TTL` | Optional | inherits `PORTAL_CACHE_TTL` | `300` | Foundation Directory cache TTL override. |
| Connectors | `FOUNDATION_DIRECTORY_RATE_LIMIT_CAPACITY` | Optional | inherits global default | `5` | Foundation Directory burst size override. |
| Connectors | `FOUNDATION_DIRECTORY_RATE_LIMIT_REFILL_RATE` | Optional | inherits global default | `1` | Foundation Directory refill-rate override. |
| Connectors | `GLOBALGIVING_PAGE_SIZE` | Optional | inherits `PORTAL_PAGE_SIZE` | `100` | GlobalGiving page size override. |
| Connectors | `GLOBALGIVING_CACHE_TTL` | Optional | inherits `PORTAL_CACHE_TTL` | `300` | GlobalGiving cache TTL override. |
| Connectors | `GLOBALGIVING_RATE_LIMIT_CAPACITY` | Optional | inherits global default | `5` | GlobalGiving burst size override. |
| Connectors | `GLOBALGIVING_RATE_LIMIT_REFILL_RATE` | Optional | inherits global default | `1` | GlobalGiving refill-rate override. |
| Connectors | `KICKSTARTER_FOR_GOOD_PAGE_SIZE` | Optional | inherits `PORTAL_PAGE_SIZE` | `100` | Kickstarter for Good page size override. |
| Connectors | `KICKSTARTER_FOR_GOOD_CACHE_TTL` | Optional | inherits `PORTAL_CACHE_TTL` | `300` | Kickstarter for Good cache TTL override. |
| Connectors | `KICKSTARTER_FOR_GOOD_RATE_LIMIT_CAPACITY` | Optional | inherits global default | `5` | Kickstarter for Good burst size override. |
| Connectors | `KICKSTARTER_FOR_GOOD_RATE_LIMIT_REFILL_RATE` | Optional | inherits global default | `1` | Kickstarter for Good refill-rate override. |
| Collaboration | `TASK_ASSIGNMENT_NOTIFICATION_RATE_LIMIT_SECONDS` | Optional | `3600` | `1800` | Minimum gap between repeated assignment notifications for the same task. |
| Retention | `RETENTION_AUDIT_LOG_DAYS` | Optional | `365` | `365` | Default retention for audit logs. |
| Retention | `RETENTION_COMMUNICATION_DAYS` | Optional | `365` | `365` | Default retention for donor communications. |
| Retention | `RETENTION_DOCUMENT_DAYS` | Optional | `180` | `180` | Default retention for generated documents. |
| Retention | `RETENTION_SUBMISSION_ATTEMPT_DAYS` | Optional | `90` | `90` | Default retention for submission attempts. |
| Retention | `RETENTION_COMPLETED_TASK_DAYS` | Optional | `180` | `180` | Default retention for completed collaboration tasks. |
| GDPR | `GDPR_DONOR_RETENTION_DAYS` | Optional | `365` | `365` | Reporting-only donor retention window used in GDPR self-check reports. |
| GDPR | `GDPR_COMMUNICATION_RETENTION_DAYS` | Optional | `730` | `730` | Reporting-only communication retention window used in GDPR self-check reports. |
| GDPR | `GDPR_APPLICATION_RETENTION_DAYS` | Optional | `1095` | `1095` | Reporting-only application retention window used in GDPR self-check reports. |
| Testing | `ACCESSIBILITY_BASE_URL` | Optional | `http://127.0.0.1:5001` | `http://127.0.0.1:5001` | Base URL used by `npm run test:a11y`. |
| Testing | `ACCESSIBILITY_USERNAME` | Optional | *(empty)* | `staff` | Optional basic-auth username for accessibility scans. |
| Testing | `ACCESSIBILITY_PASSWORD` | Optional | *(empty)* | `changeme-staff` | Optional basic-auth password for accessibility scans. |

## Notes

- Connector credential aliases registered with `register-credential` can point at any additional user-defined environment variable; those names are intentionally deployment-specific and are not fixed by the application.
- For local dashboard work over plain HTTP, set `SESSION_COOKIE_SECURE=0`.
- For container deployments, prefer `/app/data/funding_bot.db` and `/app/data/privacy_policies` so data lands on the mounted volume.
