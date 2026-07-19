# Translation Guide

This repository uses reusable outreach templates with Python `str.format(...)` placeholders. This guide explains how to contribute a new language template so translators and maintainers can review it consistently.

## Current support

The project currently documents and reviews outreach copy for these locales:

| Locale code | Language | Status | Notes |
| --- | --- | --- | --- |
| `en` | English | Default / fallback | Source copy for every template. |
| `bn` | Bengali | Supported for contributions | Keep a natural Bengali tone; only leave proper nouns and URLs in English. |

### Locale code rules

- Use BCP 47 style locale codes when adding future locales.
- Use lowercase language codes for the currently supported outreach locales (`en`, `bn`).
- Keep English as the fallback source locale.
- If you add a new locale later, document it in the table above and note whether it is a general language (`fr`) or region-specific variant (`pt-BR`).

## Translation workflow

1. Start from the English template and identify every placeholder.
2. Keep placeholders unchanged, including braces and names such as `{donor_name}` and `{organization_name}`.
3. Translate only user-facing copy; do not translate variable names, URLs, email addresses, or command examples unless the text itself is meant to be localized.
4. Preserve paragraph breaks and the overall structure so rendered emails stay readable.
5. Keep the call to action and opt-out wording clear. The code appends an opt-out sentence automatically if `{opt_out_url}` is not already present in the body.
6. Add or update tests covering the translated template path and any fallback behavior.
7. Run the test suite and a dry-run preview before opening the PR.

## Supported template placeholders

Outreach templates currently render with data from `FundingBot.send_outreach(...)` and the stored organization profile. Contributors should expect these fields to be available:

| Placeholder | Meaning |
| --- | --- |
| `{donor_name}` | Recipient display name |
| `{organization_name}` | Organization name from the stored profile |
| `{mission}` | Organization mission from the stored profile |
| `{opt_out_url}` | Unsubscribe / opt-out URL |

Additional organization profile keys may also be available, but every new translation should render correctly with at least the placeholders above.

## Template examples

### English source example (`en`)

**Subject**

```text
Support {organization_name}
```

**Body**

```text
Hello {donor_name},

{mission}

Thank you for considering support for {organization_name}.
To opt out of future outreach, visit {opt_out_url}.
```

### Bengali example (`bn`)

**Subject**

```text
{organization_name}-এর উদ্যোগে আপনার সহযোগিতা কামনা করছি
```

**Body**

```text
প্রিয় {donor_name},

{mission}

{organization_name}-এর শিক্ষা কার্যক্রম এগিয়ে নিতে আপনার সহযোগিতা আমাদের জন্য অত্যন্ত মূল্যবান।
ভবিষ্যতে এমন বার্তা না চাইলে এখানে দেখুন: {opt_out_url}
```

### Example review notes

- Good: placeholders are unchanged and readable in context.
- Good: Bengali punctuation and spacing are natural for native readers.
- Avoid: translating `{organization_name}` into literal text.
- Avoid: deleting the opt-out path from the message without confirming the automatic fallback still makes sense.

## Testing requirements

Every translation change should be verified in two ways.

### 1. Automated tests

Run the existing unit suite:

```bash
python -m unittest discover -s tests
```

When translation behavior changes, add or update tests in `tests/test_funding_bot.py` to cover:

- successful rendering of the translated subject and body
- successful rendering of every built-in template for every supported locale
- placeholder substitution for required fields
- fallback to the default English template when a translation is missing
- segment-specific template selection when applicable

### 2. Manual dry-run preview

Preview the copy without sending email:

```bash
python funding_bot.py send-outreach \
  --email donor@example.org \
  --name "Example Donor" \
  --template-name intro \
  --locale bn \
  --dry-run
```

Check that:

- every placeholder resolves cleanly
- line breaks render as expected
- Bengali text is encoded correctly in the terminal and any downstream email client
- the opt-out link appears exactly once

## Contribution checklist

Use this checklist before submitting a PR:

- [ ] Locale code follows the repository convention (`en`, `bn`, `pt-BR`, etc.)
- [ ] English source template exists or was updated first
- [ ] All placeholders are preserved exactly
- [ ] Tone, grammar, punctuation, and line breaks were reviewed by a fluent speaker
- [ ] Proper nouns, URLs, and variable names remain intact
- [ ] Opt-out wording is present or intentionally left to the automatic fallback
- [ ] Automated tests were added or updated when behavior changed
- [ ] `python -m unittest discover -s tests` was run
- [ ] A `send-outreach --dry-run` preview was checked

## Pull request guidance

In your PR description, include:

- the locale code being added or updated
- the templates touched
- whether English fallback behavior changed
- which automated and manual checks you ran
- whether a fluent reviewer approved the wording
