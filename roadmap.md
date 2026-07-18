## 🚀 Roadmap: Nonprofit Funding Bot

### **v0.1.0 → MVP (Done)**
- **Opportunity Discovery** from trusted sources  
- **Deduplication** via stable signatures  
- **Application Tracking** with SQLite persistence  
- **Document Generation** (PDF + DOCX)  
- **Outreach Logging** with opt‑out safeguards  
- **Daily Summary** (dry‑run + email composition)  
- **CLI** for cron scheduling  

---

### **v0.2.0 → Multi‑Portal + Engagement (Done)**
- **Portal Connectors** for government, CSR, NGO sites ✅ (`GrantsPortalConnector`, `CSRNetworkConnector`, `NGODirectoryConnector`)
- **Donor Segmentation** (corporate, institutional, individual) ✅
- **Personalized Outreach Templates** with engagement metrics ✅ (segment-aware templates + open/click/bounce analytics)
- **Compliance Expansion** (encrypted storage, GDPR audit logs) ✅ (GDPR export/delete + monthly audit reports)
- **Webhook/Status Polling** for live application updates ✅ (`poll_application_status`)
- **Task Queue** (Celery/RQ) replacing cron — _still open, current scheduling remains cron/CLI-driven_

---

### **v0.3.0 → Automation & Intelligence (Done)**
- **Form Automation** with Puppeteer/Selenium integration ✅ (`submit_application_via_browser` + `BrowserClient` protocol)
- **Credential Vault** (HashiCorp Vault or AWS Secrets Manager) ✅ (`CredentialVault` protocol + `FileVault`/env-var backends)
- **AI‑Assisted Proposal Drafting** using stored nonprofit data ✅ (`draft_proposal` with optional `AIClient`)
- **Outreach Analytics** (open/click rates, donor response tracking) ✅ (`get_outreach_analytics`, `build_outreach_analytics_report`)
- **Admin CLI Extensions** (`list-opportunities`, `audit-log`) ✅, plus `discover`, `send-outreach`, `set-organization-profile`, `register-credential`, and `show-settings`

---

### **v0.4.0 → Dashboard + Collaboration (Done)**
- **Web Dashboard** (Flask) ✅ (`web/app.py` + `dashboard.html`)
- **Role‑Based Access** (admin, staff, auditor) ✅ (HTTP Basic auth with per-role env passwords)
- **Self-Service Settings Panel** ✅ **NEW** — configure the organization profile, donation-search keywords/trusted sources, and credential aliases, plus trigger a live opportunity search and a donor outreach test send, all from `/settings` without leaving the admin panel or touching the CLI/env vars
- **Collaboration Tools** (assign tasks, track progress) — _still open_
- **Audit Reports** auto‑generated monthly compliance summaries ✅ (`build_monthly_audit_report`)

---

### **v0.5.0 → Scaling & Performance**
- **Horizontal Scaling** with container orchestration (Docker + Kubernetes) ✅ (`Dockerfile`, `docker-compose.yml`, `k8s/`)
- **Monitoring** (Prometheus, Grafana dashboards) ✅ (`/metrics` endpoint)
- **Resilience** with retry/backoff policies ✅ (`submit_application_via_browser` retries)
- **Multi‑Language Outreach** (English + Bengali templates) — _still open_

---

### **v1.0.0 → Production‑Grade Release**
- **Full Portal Ecosystem** with connectors + APIs — connectors ship with demo data by default; wiring real portal credentials/APIs per-deployment remains open
- **Mature Donor CRM** integrated into bot memory ✅ (segmented donor records, preferences, opt-outs, communication history)
- **Advanced Compliance** (WCAG accessibility, GDPR, ISO audits) — GDPR export/delete done; WCAG/ISO audits still open
- **Community Documentation** for staff onboarding ✅ (`README.md`, `docs/`)
- **Automated Reporting** (daily, weekly, monthly summaries) ✅ (daily summary + monthly audit report; weekly cadence still open)

---

### **Post‑1.0 Growth**
- **Marketplace Integration** for shared grant templates  
- **AI‑Driven Matching** (match nonprofit profile → best opportunities)  
- **Mobile App** for field staff updates  
- **Community Collaboration** across NGOs  

---


