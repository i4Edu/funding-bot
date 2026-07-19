# Collaboration Workflow

The Funding Bot collaboration workflow lets teams assign work, track status, and review progress without leaving the dashboard or API.

## Team setup

1. Set dashboard passwords in your environment or `.env`:
   - `ADMIN_PASSWORD`
   - `STAFF_PASSWORD`
   - `AUDITOR_PASSWORD`
2. Start the web app:
   ```bash
   python -m flask --app web.app run
   ```
3. Sign in with the role that matches each team member's lane:
   - `admin` for triage, assignment, and re-assignment
   - `staff` for delivery work
   - `auditor` for compliance review and read-only oversight

## Task workflow

1. **Create a task** as `admin` with a title, assignee, and optional due date.
2. **Review the queue** from `/dashboard/tasks` or `GET /tasks`.
3. **Start work** by moving a task from `todo` to `in-progress`.
4. **Flag blockers** with `blocked` when external input is required.
5. **Resume work** by moving `blocked` back to `todo` or `in-progress`.
6. **Complete the task** by transitioning `in-progress` to `done`.
7. **Audit changes** in `/audit-log` for `task_created`, `task_assignment_changed`, and `task_status_changed` entries.

### Status transition rules

| Current status | Allowed next status |
| --- | --- |
| `todo` | `in-progress`, `blocked` |
| `in-progress` | `todo`, `blocked`, `done` |
| `blocked` | `todo`, `in-progress` |
| `done` | *(none)* |

## Role-based permissions

| Action | Admin | Staff | Auditor |
| --- | --- | --- | --- |
| View all tasks | Yes | No, only tasks assigned to `staff` | Yes |
| Filter tasks by assignee/status/due date | Yes | Yes, but only within `staff` tasks | Yes |
| Create tasks | Yes | No | No |
| Assign or reassign tasks | Yes | No | No |
| Update task status | Yes | Yes, for tasks assigned to `staff` | Yes, for tasks assigned to `auditor` |
| View audit trail | Yes | No | Yes |

## Task API

### `GET /tasks`
List tasks with optional filters.

Query parameters:
- `assignee` — filter by assigned role (`admin`, `staff`, `auditor`)
- `status` — filter by `todo`, `in-progress`, `blocked`, or `done`
- `due_date_before` — include tasks due on or before an ISO date or datetime
- `due_date_after` — include tasks due on or after an ISO date or datetime
- `sort` — one of `updated_at`, `-updated_at`, `assignee`, `-assignee`, `status`, `-status`, `due_date`, `-due_date`

Example:
```bash
curl -u admin:$ADMIN_PASSWORD \
  "http://localhost:5000/tasks?status=todo&due_date_before=2026-07-31&sort=due_date"
```

### `POST /tasks`
Create a new task. Admin only.

Request body:
```json
{
  "title": "Collect letters of support",
  "assigned_to": "staff",
  "description": "Follow up with partner schools and upload signed PDFs.",
  "status": "todo",
  "due_date": "2026-07-31"
}
```

Example:
```bash
curl -u admin:$ADMIN_PASSWORD \
  -X POST http://localhost:5000/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Collect letters of support","assigned_to":"staff","due_date":"2026-07-31"}'
```

### `GET /tasks/<id>`
Fetch one task. Staff can only fetch tasks in the `staff` lane.

### `POST /tasks/<id>/assign`
Reassign a task. Admin only.

Request body:
```json
{
  "assigned_to": "auditor"
}
```

### `POST /tasks/<id>/status`
Move a task through the collaboration workflow.

Request body:
```json
{
  "status": "in-progress"
}
```

Example:
```bash
curl -u staff:$STAFF_PASSWORD \
  -X POST http://localhost:5000/tasks/12/status \
  -H "Content-Type: application/json" \
  -d '{"status":"in-progress"}'
```

## Common scenarios

### Daily triage
- Admin creates new tasks for incoming opportunities.
- Staff opens `/dashboard/tasks` to see only their work queue.
- Auditor filters `/tasks?assignee=auditor&sort=due_date` to review upcoming compliance work.

### Reassigning blocked work
1. Staff sets a task to `blocked`.
2. Admin reviews the blocker and reassigns it to `auditor` if compliance input is needed.
3. Auditor resolves the issue and moves the task back to `in-progress` or `todo`.

### Sprint review / weekly check-in
- Use `GET /tasks?sort=status` to group similar workflow states.
- Use `GET /tasks?due_date_before=YYYY-MM-DD&sort=due_date` to identify urgent items.
- Cross-check recent task changes in `/audit-log`.

## Troubleshooting

### Staff receives `403 Forbidden` from `/tasks`
Staff users can only view tasks assigned to `staff`. Remove the `assignee` filter or use the staff lane.

### Status change is rejected
Check the state machine above. For example, `todo -> done` is not allowed; move through `in-progress` first.

### Due-date filters return no tasks
Use ISO-8601 values such as `2026-07-31` or `2026-07-31T00:00:00+00:00`.

### Audit review is missing task changes
Open `/audit-log` as `admin` or `auditor` and filter for the task actions:
- `task_created`
- `task_assignment_changed`
- `task_status_changed`
