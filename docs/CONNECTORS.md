# Connector Guide

This guide explains how to add a new portal connector, wire in connector-specific
keyword/category mappings, and validate the connector from the CLI.

## Built-in connectors

| CLI slug | Class | Source | Default mode |
| --- | --- | --- | --- |
| `grants-portal` | `GrantsPortalConnector` | Grants Portal | Demo data unless an `http_client` is supplied |
| `csr-network` | `CSRNetworkConnector` | CSR Network | Demo data unless an `http_client` is supplied |
| `ngo-directory` | `NGODirectoryConnector` | NGO Directory | Demo data unless an `http_client` is supplied |

## Available keyword mappings

Each connector can expand an incoming keyword into connector-specific synonyms
and category names. Matching is case-insensitive.

### `grants-portal`

| Canonical keyword | Synonyms | Categories |
| --- | --- | --- |
| `education` | `learning`, `school improvement`, `innovation grant` | `Education` |
| `youth` | `student success`, `young learners` | `Education` |

### `csr-network`

| Canonical keyword | Synonyms | Categories |
| --- | --- | --- |
| `csr` | `corporate social responsibility`, `corporate giving` | `Corporate Partnerships` |
| `digital learning` | `edtech`, `technology training`, `online learning` | `Corporate Partnerships` |

### `ngo-directory`

| Canonical keyword | Synonyms | Categories |
| --- | --- | --- |
| `literacy` | `reading`, `community engagement`, `library support` | `Literacy` |
| `institutional` | `foundation grant`, `capacity building` | `Literacy` |

## How keyword mapping works

When `discover` or `test-connector` receives a keyword:

1. The connector normalizes the keyword.
2. If it matches a canonical keyword, synonym, or mapped category, the connector
   expands the search set.
3. Matching then runs against opportunity title, summary, tags, and category.

Example:

```bash
python -m funding_bot test-connector --connector csr-network --keywords edtech
```

The connector expands `edtech` to include `digital learning` and
`Corporate Partnerships`, then returns matching sample opportunities.

## Add a new connector

1. **Create the connector class** in `funding_bot.py`.
   - Subclass `_BasePortalConnector`
   - Set `connector_slug`, `source_name`, and `base_url`
   - Implement `_demo_data()` with safe sample records
2. **Add keyword/category mappings** on the class using:

   ```python
   keyword_category_mappings = {
       "canonical keyword": {
           "keywords": ("synonym one", "synonym two"),
           "categories": ("Portal Category",),
       }
   }
   ```

3. **Register the connector** in `connector_registry()`.
4. **Include it in discovery** by adding it to `default_connectors()` if it
   should run in the default search flow.
5. **Write tests** in `tests/test_funding_bot.py` for:
   - direct keyword matching
   - synonym/category expansion
   - `test-connector` CLI output if applicable
6. **Document the connector** in this file:
   - add it to the built-in connector table
   - document every canonical keyword, synonym, and category mapping
7. **Validate it locally**:

   ```bash
   python -m funding_bot test-connector --connector <slug> --keywords "<term>"
   python -m unittest tests.test_funding_bot -v
   ```

## Test a connector in isolation

Use the dedicated CLI command:

```bash
python -m funding_bot test-connector --connector grants-portal --keywords learning --limit 2
```

The command returns JSON with:

- connector slug and source name
- validation status
- mode (`demo` or `remote`)
- requested and expanded keywords
- sample result count
- sample results
- the connector's keyword mapping table

If a connector raises an exception, the JSON response includes `status: "error"`
and an `error` field for troubleshooting.
