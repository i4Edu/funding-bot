**Title:** Nonprofit Funding Automation Bot  

**Objective:**  
Develop an AI agent for a government‑approved nonprofit organization that continuously searches for donation opportunities, applies intelligently, manages communications, and tracks applications without duplication.

---

### 🔑 Core Capabilities

- **Donation Search**  
  Continuously monitor trusted sources (grant portals, NGO directories, CSR programs, crowdfunding platforms) for new funding opportunities.  
  - Use keyword filters (e.g., “nonprofit grants,” “donation programs,” “CSR funding”).  
  - Store discovered opportunities in bot memory.

- **Smart Application**  
  Automatically apply using stored organizational data.  
  - Fill registration forms, sign‑up/sign‑in, and submit applications.  
  - Prevent duplicate submissions by checking against memory before applying.  
  - Track application status (submitted, pending, approved, rejected).

- **Email Outreach**  
  Compose and send personalized emails to potential donors.  
  - Avoid spamming: throttle frequency, personalize content, and respect opt‑out lists.  
  - Use professional tone and nonprofit branding.  
  - Log all communications in memory.

- **Documentation Preparation**  
  Auto‑generate required documents (cover letters, proposals, compliance forms).  
  - Pull organizational data from memory.  
  - Format in professional templates (PDF/Word).  
  - Attach to applications or emails.

- **Web Interaction**  
  Navigate websites, fill forms, and handle sign‑up/sign‑in securely.  
  - Use headless browser automation (e.g., Puppeteer, Selenium).  
  - Store credentials securely.  
  - Retry failed submissions gracefully.

- **Application Tracking**  
  Maintain a database of all applied opportunities.  
  - Include donor name, portal, date, status, and next action.  
  - Prevent re‑application to the same program.

- **Daily Summary**  
  Send a concise daily report to **lupael@i4e.com.bd**.  
  - Include new opportunities found, applications submitted, donor communications, and pending statuses.  
  - Format summary in clear bullet points.

---

### ⚙️ Safeguards

- Respect donor privacy and anti‑spam regulations.  
- Ensure applications are submitted only once per opportunity.  
- Maintain audit logs for transparency.  
- Securely store organizational data and credentials.

---

### 📌 Example Daily Summary Email

```
Subject: Daily Nonprofit Funding Report – [Date]

Hello Lupael,

Here is today’s funding activity summary:

- New Opportunities Found: 3
   • UNICEF CSR Grant – Pending Application
   • Local NGO Partnership – Applied
   • Tech4Good Donation Program – In Review

- Applications Submitted: 2
   • NGO Partnership Portal – Submitted successfully
   • CSR Grant Portal – Awaiting confirmation

- Donor Communications: 4 personalized emails sent
   • No bounce or spam flags detected

- Pending Applications: 5 (see attached status table)

Best regards,
Nonprofit Funding Bot
```

---

## 🛠 Developer Checklist for Nonprofit Funding Bot

### 1. **Tech Stack Selection**
- Backend: Node.js or Python (FastAPI/Django)  
- Database: PostgreSQL/MySQL for structured tracking  
- Automation: Puppeteer/Selenium for web form filling  
- Email: Gmail API / Outlook API for donor communication  
- Scheduling: Cron jobs or Celery for daily tasks  
- Security: OAuth2 for authentication, encrypted credential storage  

---

### 2. **Bot Memory & Data Handling**
- Store nonprofit’s organizational data (mission, registration info, documents) in structured memory.  
- Maintain donor profiles (name, email, donation history, preferences).  
- Track applications with unique IDs to prevent duplicates.  
- Use audit logs for transparency and compliance.  

---

### 3. **Donation Opportunity Search**
- Scrape or query grant portals, NGO directories, CSR programs.  
- Use APIs where available (e.g., government grant APIs).  
- Apply keyword filters and categorize opportunities.  
- Save results in database with timestamp and source.  

---

### 4. **Application Automation**
- Auto‑fill forms using Puppeteer/Selenium.  
- Handle sign‑up/sign‑in securely with stored credentials.  
- Attach required documents (auto‑generated proposals, compliance forms).  
- Prevent duplicate submissions by checking database before applying.  

---

### 5. **Email Outreach**
- Use Gmail/Outlook API for sending.  
- Personalize emails with donor name and past interactions.  
- Respect anti‑spam rules:  
  - Limit frequency (e.g., max 1 email per donor per week).  
  - Include opt‑out link.  
- Log all communications in database.  

---

### 6. **Documentation Generation**
- Templates for cover letters, proposals, compliance forms.  
- Auto‑populate with nonprofit data (mission, registration number, achievements).  
- Export to PDF/Word for submission.  

---

### 7. **Application Tracking**
- Maintain status table: Submitted, Pending, Approved, Rejected.  
- Include donor name, portal, date, and next action.  
- Update automatically when portal status changes.  

---

### 8. **Daily Summary Report**
- Generate concise report at 9 AM daily.  
- Email to **lupael@i4e.com.bd**.  
- Include:  
  - New opportunities found  
  - Applications submitted  
  - Donor communications sent  
  - Pending statuses  

---

### 9. **Compliance & Safeguards**
- Respect donor privacy and GDPR rules.  
- Encrypt sensitive data.  
- Maintain audit logs for all actions.  
- Ensure bot does not re‑apply to same opportunity.  

---

### 10. **Deployment & Monitoring**
- Host on secure cloud (AWS, Azure, GCP).  
- Use monitoring tools (Prometheus, Grafana).  
- Error handling: retry failed submissions gracefully.  
- Regular updates to donor and grant sources.  

---
