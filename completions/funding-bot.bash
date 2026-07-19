_funding_bot_completion() {
  local cur prev command
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  command=""
  for word in "${COMP_WORDS[@]:1}"; do
    if [[ "$word" != -* ]]; then
      command="$word"
      break
    fi
  done
  case "$prev" in
    --cadence)
      COMPREPLY=( $(compgen -W "monthly weekly" -- "$cur") )
      return 0
      ;;
    --connector)
      COMPREPLY=( $(compgen -W "csr-network foundation-directory globalgiving grants-portal kickstarter-for-good ngo-directory" -- "$cur") )
      return 0
      ;;
    --month)
      COMPREPLY=( $(compgen -W "1 10 11 12 2 3 4 5 6 7 8 9" -- "$cur") )
      return 0
      ;;
    --segment)
      COMPREPLY=( $(compgen -W "corporate individual institutional unknown" -- "$cur") )
      return 0
      ;;
    --shell)
      COMPREPLY=( $(compgen -W "bash zsh" -- "$cur") )
      return 0
      ;;
  esac
  if [[ -z "$command" ]]; then
    COMPREPLY=( $(compgen -W "--db --help --json --non-interactive --quiet --verbose -h audit-log completion discover doctor enforce-data-retention gdpr-self-check-report list-donors list-opportunities monthly-audit-report register-credential send-daily-summary send-outreach set-data-retention-policy set-organization-profile show-settings test-connector" -- "$cur") )
    return 0
  fi
  case "$command" in
    audit-log)
      COMPREPLY=( $(compgen -W "--action --help --json --limit -h" -- "$cur") )
      return 0
      ;;
    completion)
      COMPREPLY=( $(compgen -W "--help --json --shell -h" -- "$cur") )
      return 0
      ;;
    discover)
      COMPREPLY=( $(compgen -W "--help --json --keywords --trusted-sources -h" -- "$cur") )
      return 0
      ;;
    doctor)
      COMPREPLY=( $(compgen -W "--connector-keywords --help --json -h" -- "$cur") )
      return 0
      ;;
    enforce-data-retention)
      COMPREPLY=( $(compgen -W "--as-of --dry-run --help --json -h" -- "$cur") )
      return 0
      ;;
    gdpr-self-check-report)
      COMPREPLY=( $(compgen -W "--cadence --help --json --output -h" -- "$cur") )
      return 0
      ;;
    list-donors)
      COMPREPLY=( $(compgen -W "--help --json --segment -h" -- "$cur") )
      return 0
      ;;
    list-opportunities)
      COMPREPLY=( $(compgen -W "--help --json --limit --status -h" -- "$cur") )
      return 0
      ;;
    monthly-audit-report)
      COMPREPLY=( $(compgen -W "--help --json --month --output --year -h" -- "$cur") )
      return 0
      ;;
    register-credential)
      COMPREPLY=( $(compgen -W "--alias --env-var --help --json -h" -- "$cur") )
      return 0
      ;;
    send-daily-summary)
      COMPREPLY=( $(compgen -W "--dry-run --help --json --recipient -h" -- "$cur") )
      return 0
      ;;
    send-outreach)
      COMPREPLY=( $(compgen -W "--body --dry-run --email --help --json --locale --name --subject --template-name -h" -- "$cur") )
      return 0
      ;;
    set-data-retention-policy)
      COMPREPLY=( $(compgen -W "--audit-logs-days --communications-days --completed-tasks-days --documents-days --help --json --submission-attempts-days -h" -- "$cur") )
      return 0
      ;;
    set-organization-profile)
      COMPREPLY=( $(compgen -W "--file --help --json -h" -- "$cur") )
      return 0
      ;;
    show-settings)
      COMPREPLY=( $(compgen -W "--help --json -h" -- "$cur") )
      return 0
      ;;
    test-connector)
      COMPREPLY=( $(compgen -W "--connector --help --json --keywords --limit -h" -- "$cur") )
      return 0
      ;;
  esac
  return 0
}
complete -F _funding_bot_completion funding-bot
