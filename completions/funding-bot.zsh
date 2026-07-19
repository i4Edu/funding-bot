#compdef funding-bot
_funding_bot_completion() {
  local command=""
  local word
  for word in ${words[@]:2}; do
    if [[ "$word" != -* ]]; then
      command="$word"
      break
    fi
  done
  case "$words[CURRENT-1]" in
    --cadence)
      compadd -- monthly weekly
      return 0
      ;;
    --connector)
      compadd -- csr-network foundation-directory globalgiving grants-portal kickstarter-for-good ngo-directory
      return 0
      ;;
    --month)
      compadd -- 1 10 11 12 2 3 4 5 6 7 8 9
      return 0
      ;;
    --segment)
      compadd -- corporate individual institutional unknown
      return 0
      ;;
    --shell)
      compadd -- bash zsh
      return 0
      ;;
  esac
  if [[ -z "$command" ]]; then
    compadd -- --db --help --json --non-interactive --quiet --verbose -h audit-log completion discover doctor enforce-data-retention gdpr-self-check-report list-donors list-opportunities monthly-audit-report register-credential send-daily-summary send-outreach set-data-retention-policy set-organization-profile show-settings test-connector
    return 0
  fi
  case "$command" in
    audit-log)
      compadd -- --action --help --json --limit -h
      return 0
      ;;
    completion)
      compadd -- --help --json --shell -h
      return 0
      ;;
    discover)
      compadd -- --help --json --keywords --trusted-sources -h
      return 0
      ;;
    doctor)
      compadd -- --connector-keywords --help --json -h
      return 0
      ;;
    enforce-data-retention)
      compadd -- --as-of --dry-run --help --json -h
      return 0
      ;;
    gdpr-self-check-report)
      compadd -- --cadence --help --json --output -h
      return 0
      ;;
    list-donors)
      compadd -- --help --json --segment -h
      return 0
      ;;
    list-opportunities)
      compadd -- --help --json --limit --status -h
      return 0
      ;;
    monthly-audit-report)
      compadd -- --help --json --month --output --year -h
      return 0
      ;;
    register-credential)
      compadd -- --alias --env-var --help --json -h
      return 0
      ;;
    send-daily-summary)
      compadd -- --dry-run --help --json --recipient -h
      return 0
      ;;
    send-outreach)
      compadd -- --body --dry-run --email --help --json --locale --name --subject --template-name -h
      return 0
      ;;
    set-data-retention-policy)
      compadd -- --audit-logs-days --communications-days --completed-tasks-days --documents-days --help --json --submission-attempts-days -h
      return 0
      ;;
    set-organization-profile)
      compadd -- --file --help --json -h
      return 0
      ;;
    show-settings)
      compadd -- --help --json -h
      return 0
      ;;
    test-connector)
      compadd -- --connector --help --json --keywords --limit -h
      return 0
      ;;
  esac
}
compdef _funding_bot_completion funding-bot
