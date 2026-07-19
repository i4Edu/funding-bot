# Configuration

Funding Bot can load CLI defaults from YAML or TOML configuration files.

## File locations

Funding Bot loads configuration in this order:

1. `~/.funding-bot/config.yml`
2. `~/.funding-bot/config.yaml`
3. `~/.funding-bot/config.toml`
4. `./.funding-bot/config.yml`
5. `./.funding-bot/config.yaml`
6. `./.funding-bot/config.toml`
7. `./funding-bot.toml`

Project-level files override home-directory files. You can also pass an explicit file with `--config PATH` or `FUNDING_BOT_CONFIG=PATH`.

## Precedence

Values are applied in this order:

1. built-in defaults
2. configuration files
3. environment variables
4. CLI flags

## Supported keys

```yaml
db: funding_bot.db

send_daily_summary:
  recipient: ops@example.org
  dry_run: false

discover:
  keywords:
    - education
    - csr
  trusted_sources:
    - Grants Portal
  dry_run: false

send_outreach:
  email: donor@example.org
  name: Example Donor
  template_name: intro
  subject: Thank you for supporting {organization_name}
  body: "Dear {donor_name},\n\nThank you for supporting {organization_name}."
  locale: en
  dry_run: true

export_data_warehouse:
  datasets:
    - donors
    - tasks
  format: json
  output_dir: generated/exports
  archive: false
  dry_run: false
```

Equivalent TOML:

```toml
db = "funding_bot.db"

[send_daily_summary]
recipient = "ops@example.org"
dry_run = false

[discover]
keywords = ["education", "csr"]
trusted_sources = ["Grants Portal"]
dry_run = false

[send_outreach]
email = "donor@example.org"
name = "Example Donor"
template_name = "intro"
locale = "en"
dry_run = true

[export_data_warehouse]
datasets = ["donors", "tasks"]
format = "json"
output_dir = "generated/exports"
archive = false
dry_run = false
```

## Environment variable overrides

- `BOT_DB_PATH`
- `DAILY_SUMMARY_RECIPIENT`
- `DAILY_SUMMARY_DRY_RUN`
- `FUNDING_BOT_DISCOVER_KEYWORDS`
- `FUNDING_BOT_DISCOVER_TRUSTED_SOURCES`
- `FUNDING_BOT_DISCOVER_DRY_RUN`
- `FUNDING_BOT_OUTREACH_EMAIL`
- `FUNDING_BOT_OUTREACH_NAME`
- `FUNDING_BOT_OUTREACH_TEMPLATE_NAME`
- `FUNDING_BOT_OUTREACH_SUBJECT`
- `FUNDING_BOT_OUTREACH_BODY`
- `FUNDING_BOT_OUTREACH_LOCALE`
- `FUNDING_BOT_OUTREACH_DRY_RUN`
- `FUNDING_BOT_EXPORT_DATASETS`
- `FUNDING_BOT_EXPORT_FORMAT`
- `FUNDING_BOT_EXPORT_OUTPUT_DIR`
- `FUNDING_BOT_EXPORT_ARCHIVE`
- `FUNDING_BOT_EXPORT_DRY_RUN`

List-valued environment variables use comma-separated values.

## Dry-run behavior

- `discover --dry-run` fetches and filters opportunities but does not save them.
- `send-outreach --dry-run` renders the message preview and planned actions without sending or writing donor/outreach records.
- `export-data-warehouse --dry-run` shows planned artifacts and row counts without writing files.

Use `.funding-bot/config.example.yml` as a starting point for project configuration.
