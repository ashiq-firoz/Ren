"""Built-in skills (playbooks): reusable multi-tool workflows. `agent` refers to a default agent slug."""
from __future__ import annotations


def _i(key: str, label: str, type_: str = "text", required: bool = False, placeholder: str | None = None,
       options: list[str] | None = None) -> dict:
    d = {"key": key, "label": label, "type": type_, "required": required}
    if placeholder:
        d["placeholder"] = placeholder
    if options:
        d["options"] = options
    return d


SKILLS: list[dict] = [
    {
        "name": "Prepare meeting brief", "slug": "prepare-meeting", "category": "productivity", "icon": "calendar-clock",
        "agent": "executive-assistant", "tool_groups": ["calendar", "email", "rag", "search", "documents"],
        "description": "A one-page briefing for an upcoming meeting: context, attendees, history, talking points.",
        "inputs": [_i("meeting", "Meeting (title or description)", required=True, placeholder="Q3 review with Acme"),
                   _i("date", "Date", "date"), _i("attendees", "Attendees", placeholder="names or emails")],
        "instructions": """1. Find the calendar event (calendar_search / calendar_list around the date) and read its details.
2. Search recent email threads with the attendees and on the topic (email_search); extract open items and commitments.
3. Search the knowledge base for related documents (rag_search) — previous minutes, proposals, account plans.
4. For external attendees, look up their role and company briefly (search_web).
5. Write the brief: Purpose & desired outcome · Attendees (role, stance, last interaction) · Background & history ·
   Open items / commitments · Key numbers · Talking points (3–5) · Questions to ask · Risks.
6. Save as output/meeting-brief-<short-name>.docx and summarise the top 3 points in your final answer.""",
    },
    {
        "name": "Research a company", "slug": "research-company", "category": "research", "icon": "building-2",
        "agent": "research-analyst", "tool_groups": ["search", "browser", "rag", "documents"],
        "description": "Company profile: business model, products, customers, financials, leadership, news, risks.",
        "inputs": [_i("company", "Company", required=True), _i("focus", "Focus", "select",
                                                                 options=["Full profile", "Competitive position", "Financials", "Recent news", "Partnership fit"])],
        "instructions": """1. Check the knowledge base for any existing internal notes on the company (rag_search).
2. Search the web: official site, about/investor pages, recent news (search_news), funding/financials, leadership.
3. Fetch and read the most informative pages (search_fetch); note publication dates.
4. Build the profile: Snapshot (HQ, size, founded, funding/revenue) · Business model & products · Customers & markets ·
   Leadership · Recent developments (last 12 months) · Competitive landscape · Risks & red flags · Relevance to us.
5. Cite every non-obvious fact with its URL. Save as output/company-<name>.docx.""",
    },
    {
        "name": "Competitive analysis", "slug": "competitive-analysis", "category": "research", "icon": "swords",
        "agent": "research-analyst", "tool_groups": ["search", "browser", "rag", "sheets", "documents"],
        "description": "Compare competitors on positioning, pricing, features, strengths and weaknesses.",
        "inputs": [_i("product", "Our product / company", required=True), _i("competitors", "Competitors (optional)"),
                   _i("market", "Market / segment")],
        "instructions": """1. Read internal positioning/product docs (rag_search) to understand our offering.
2. If competitors aren't given, identify the top 3–5 from web research.
3. For each: positioning, target customers, pricing (public), key features, recent moves, strengths, weaknesses.
4. Produce a comparison matrix (output/competitive-matrix.xlsx) and a brief (output/competitive-analysis.docx) with
   implications and recommended responses. Cite sources.""",
    },
    {
        "name": "Review a contract", "slug": "review-contract", "category": "legal", "icon": "file-search",
        "agent": "legal-reviewer", "tool_groups": ["rag", "documents", "pdf", "filesystem"],
        "description": "Clause-by-clause review with obligations, risks and proposed redlines against company standards.",
        "inputs": [_i("document", "Contract (workspace path or document name)", required=True),
                   _i("perspective", "We are the", "select", options=["Customer", "Vendor", "Partner"]),
                   _i("focus", "Special focus (optional)", placeholder="liability, data protection…")],
        "instructions": """1. Read the full contract (fs_read / pdf_read / doc_read or rag_get_document).
2. Retrieve the company's contracting standards / playbook from the knowledge base (rag_search_structured).
3. Extract: parties, term & renewal, scope, fees & payment, SLAs, liability caps, indemnities, IP, confidentiality,
   data protection, termination, governing law, assignment, notices.
4. For each risky clause: quote it with section number, explain the risk, rate High/Medium/Low, propose a redline.
5. List obligations and deadlines in a table. Save output/contract-review-<name>.docx. Note: not formal legal advice.""",
    },
    {
        "name": "Reconcile accounts", "slug": "reconcile-accounts", "category": "finance", "icon": "scale",
        "agent": "finance-analyst", "tool_groups": ["analytics", "sheets", "documents"],
        "description": "Match two ledgers/statements, explain every discrepancy and produce a reconciliation workbook.",
        "inputs": [_i("file_a", "First file (e.g. bank statement)", required=True), _i("file_b", "Second file (e.g. ledger)", required=True),
                   _i("key", "Matching key column", placeholder="reference / invoice no."), _i("amount", "Amount column")],
        "instructions": """1. Profile both files (analytics_profile); confirm columns, currencies, date ranges.
2. Run analytics_reconcile on the key and amount columns; also try fuzzy matching on date+amount for unmatched rows.
3. Categorise differences: missing in A, missing in B, amount mismatch, timing difference, duplicates.
4. Produce output/reconciliation.xlsx (Summary, Matched, Unmatched, Differences sheets) and explain the net variance.""",
    },
    {
        "name": "Month-end close status", "slug": "month-end-close", "category": "finance", "icon": "calendar-range",
        "agent": "finance-analyst", "tool_groups": ["rag", "sheets", "analytics", "documents", "pm"],
        "description": "Close checklist with status, blockers and owners, using the company's close procedures.",
        "inputs": [_i("month", "Month", required=True, placeholder="September 2026"), _i("entity", "Entity / business unit")],
        "instructions": """1. Retrieve the month-end close procedure from the knowledge base.
2. Build the checklist (accruals, prepaids, revenue recognition, bank recs, intercompany, fixed assets, payroll, reviews).
3. Check any provided workbooks/trackers for completion status; flag missing reconciliations or variances >5%.
4. Output output/close-status-<month>.xlsx and a short RAG-status summary with blockers and owners.""",
    },
    {
        "name": "Board / exec deck", "slug": "board-deck", "category": "communications", "icon": "presentation",
        "agent": "communications-writer", "tool_groups": ["slides", "analytics", "rag", "documents", "agents"],
        "description": "A 10–12 slide executive deck: highlights, KPIs with charts, risks, asks.",
        "inputs": [_i("period", "Period", required=True, placeholder="Q3 2026"), _i("highlights", "Key messages", "textarea"),
                   _i("metrics", "Metrics file (optional)", placeholder="shared/finance/q3-kpis.xlsx")],
        "instructions": """1. Gather inputs: metrics file (analytics_profile/query), knowledge base updates, provided highlights.
2. If numbers need analysis, delegate to the Data Analyst (agent_delegate with wait=true) for charts.
3. Storyline: Title · Executive summary · KPI dashboard · Wins · Financials · Customers · Product · People · Risks ·
   Priorities next period · Asks/decisions.
4. Build the deck with ppt_create (one idea per slide, takeaway titles, native charts). Save output/<period>-deck.pptx.""",
    },
    {
        "name": "Process an invoice", "slug": "process-invoice", "category": "finance", "icon": "receipt",
        "agent": "finance-analyst", "tool_groups": ["pdf", "vision", "sheets", "rag"],
        "description": "Extract invoice data, validate totals and policy, and prepare an AP entry — never pays.",
        "inputs": [_i("invoice", "Invoice file", required=True), _i("po_file", "PO / vendor list (optional)")],
        "instructions": """1. Extract text/tables (pdf_read, pdf_extract_tables) or use vision_ocr for scans.
2. Capture vendor, invoice no., dates, currency, line items, tax, totals, bank details (mask account numbers).
3. Validate arithmetic, duplicates (same vendor+number), PO match if provided, and AP policy from the knowledge base.
4. Append a row to output/ap-register.xlsx and report anomalies. Never initiate payments.""",
    },
    {
        "name": "Weekly status report", "slug": "weekly-status", "category": "operations", "icon": "clipboard-list",
        "agent": "operations-manager", "tool_groups": ["pm", "chat", "documents", "rag"],
        "description": "RAG-status weekly report from project tools and team channels.",
        "inputs": [_i("project", "Project / team", required=True), _i("week", "Week of", "date")],
        "instructions": """1. Pull completed, in-progress and blocked items from the PM tool (pm_search) for the week.
2. Scan team channels for decisions and risks (chat_find_decisions, chat_find_action_items) if connected.
3. Write: Overall status (R/A/G) · Highlights · Completed · Next week · Risks & mitigations · Decisions needed.
4. Save output/status-<project>-<week>.docx; offer to post to the team channel (requires approval).""",
    },
    {
        "name": "Draft customer email", "slug": "draft-customer-email", "category": "communications", "icon": "mail-plus",
        "agent": "communications-writer", "tool_groups": ["email", "rag", "crm"],
        "description": "A polished customer email saved as a draft for your review.",
        "inputs": [_i("recipient", "Recipient", required=True), _i("purpose", "Purpose", required=True),
                   _i("points", "Key points", "textarea"), _i("tone", "Tone", "select", options=["Warm", "Formal", "Concise", "Apologetic"])],
        "instructions": """1. Check CRM/knowledge base for account context and any relevant policy or pricing documents.
2. Draft: clear subject, purpose in the first sentence, key points, specific next step, sign-off.
3. Respect policies: no unapproved commitments or pricing. Save with email_create_draft (do not send).""",
    },
    {
        "name": "Policy Q&A", "slug": "policy-qa", "category": "knowledge", "icon": "book-open-check",
        "agent": "knowledge-librarian", "tool_groups": ["rag"],
        "description": "Precise answer from company documents with section-level citations.",
        "inputs": [_i("question", "Question", "textarea", required=True)],
        "instructions": """1. Use rag_search_structured to locate the exact leaf sections (STAIR), then rag_search for supporting chunks.
2. Answer with quotes of the governing wording and [n] citations including the breadcrumb.
3. If documents conflict or don't cover it, say so explicitly and suggest who could confirm.""",
    },
    {
        "name": "Summarize documents", "slug": "summarize-documents", "category": "knowledge", "icon": "files",
        "agent": "knowledge-librarian", "tool_groups": ["rag", "documents"],
        "description": "Executive summary of one or more documents for a given audience.",
        "inputs": [_i("documents", "Documents (names or paths)", required=True), _i("audience", "Audience", placeholder="leadership team")],
        "instructions": """1. Load each document's structure (rag_get_document) and read the key sections.
2. Summarise: purpose, key points, decisions/obligations, numbers, open questions — tailored to the audience.
3. Cite sections. Save output/summary.docx.""",
    },
    {
        "name": "Data analysis report", "slug": "data-analysis-report", "category": "data", "icon": "chart-line",
        "agent": "data-analyst", "tool_groups": ["analytics", "sheets", "documents"],
        "description": "Profile a dataset, answer business questions with SQL and charts, write up insights.",
        "inputs": [_i("dataset", "Dataset path", required=True, placeholder="shared/sales/2026.csv"),
                   _i("questions", "Questions to answer", "textarea", required=True)],
        "instructions": """1. analytics_profile the dataset; note data-quality issues.
2. Answer each question with analytics_query (DuckDB SQL); create 2–4 charts with analytics_visualize.
3. Write output/analysis-report.docx: Executive summary · Data & method · Findings per question (chart + insight) ·
   Recommendations · Caveats.""",
    },
    {
        "name": "Sales call prep", "slug": "sales-call-prep", "category": "sales", "icon": "phone-call",
        "agent": "sales-specialist", "tool_groups": ["crm", "search", "rag", "email"],
        "description": "Account context, stakeholders, news and talking points for a sales conversation.",
        "inputs": [_i("account", "Account", required=True), _i("contact", "Contact"), _i("goal", "Meeting goal")],
        "instructions": """1. CRM: account, deals, recent activities and tasks (crm_search_customer, crm_list_deals).
2. Recent email threads with the account; company news from the web.
3. Output: Account snapshot · Stakeholders · Open opportunities · Pain points · Talking points · Objections & answers · Next step.""",
    },
    {
        "name": "Pipeline review", "slug": "pipeline-review", "category": "sales", "icon": "funnel",
        "agent": "sales-specialist", "tool_groups": ["crm", "analytics", "documents"],
        "description": "Pipeline by stage, slippage, risks and recommended actions.",
        "inputs": [_i("period", "Period", placeholder="this quarter"), _i("owner", "Owner / team (optional)")],
        "instructions": """1. crm_pipeline_summary and crm_list_deals for the period.
2. Flag deals with no activity in 14+ days, past close dates, or missing next steps.
3. Report: totals by stage, weighted forecast, top risks, recommended actions per deal. Save output/pipeline-review.xlsx.""",
    },
    {
        "name": "Onboarding plan", "slug": "onboarding-plan", "category": "hr", "icon": "user-plus",
        "agent": "operations-manager", "tool_groups": ["rag", "documents", "calendar"],
        "description": "A 30-60-90 day onboarding plan using company HR policies.",
        "inputs": [_i("role", "Role", required=True), _i("start_date", "Start date", "date"), _i("manager", "Manager")],
        "instructions": """1. Retrieve onboarding, IT and HR policies from the knowledge base.
2. Build week-1 schedule, 30/60/90-day goals, training, accounts/tools access, key people to meet.
3. Save output/onboarding-<role>.docx.""",
    },
    {
        "name": "Job description", "slug": "job-description", "category": "hr", "icon": "badge",
        "agent": "communications-writer", "tool_groups": ["rag", "search", "documents"],
        "description": "Inclusive, structured job description aligned with company levels.",
        "inputs": [_i("role", "Role", required=True), _i("level", "Level"), _i("location", "Location / remote")],
        "instructions": """1. Check the knowledge base for career levels, benefits and values.
2. Write: About us · The role · Responsibilities · Requirements (must/nice) · What we offer · Inclusive language.
3. Save output/jd-<role>.docx.""",
    },
    {
        "name": "Debug an application", "slug": "debug-application", "category": "engineering", "icon": "bug",
        "agent": "software-engineer", "tool_groups": ["filesystem", "shell", "code", "git", "github"],
        "description": "Reproduce, diagnose and fix a bug on a branch, with tests and a pull request.",
        "inputs": [_i("repo", "Repository (path or URL)", required=True), _i("symptom", "Symptom / error", "textarea", required=True)],
        "instructions": """1. Clone or open the repo (git_clone into projects/), read the relevant code and logs.
2. Reproduce the issue with a failing test or script.
3. Fix on a new branch with a minimal change; run tests and lint.
4. Commit, push and open a PR (requires approval) with root cause, fix and testing notes.""",
    },
    {
        "name": "Code review", "slug": "code-review", "category": "engineering", "icon": "git-pull-request",
        "agent": "software-engineer", "tool_groups": ["github", "git", "filesystem"],
        "description": "Thorough review of a pull request: correctness, security, tests, readability.",
        "inputs": [_i("pr", "Pull request (URL or repo#number)", required=True)],
        "instructions": """1. github_get_pr with diff; read changed files in context.
2. Check correctness, edge cases, security, performance, tests and naming.
3. Post a review summary (github_review_pr — requires approval) or return the review as markdown.""",
    },
    {
        "name": "Market sizing", "slug": "market-sizing", "category": "research", "icon": "pie-chart",
        "agent": "research-analyst", "tool_groups": ["search", "sheets", "analytics", "documents"],
        "description": "TAM/SAM/SOM with transparent assumptions and sources.",
        "inputs": [_i("market", "Market", required=True), _i("geography", "Geography")],
        "instructions": """1. Find credible top-down data points (reports, statistics) and a bottom-up approach.
2. Build output/market-sizing.xlsx with assumptions, formulas, sources, and sensitivity (low/base/high).
3. Summarise TAM/SAM/SOM with the key drivers.""",
    },
    {
        "name": "Inbox triage", "slug": "inbox-triage", "category": "productivity", "icon": "inbox",
        "agent": "executive-assistant", "tool_groups": ["email", "calendar"],
        "description": "Categorise recent email, extract action items and draft replies (drafts only).",
        "inputs": [_i("timeframe", "Timeframe", placeholder="last 24 hours")],
        "instructions": """1. email_search for the timeframe; group into Urgent / Needs reply / FYI / Newsletters.
2. Extract action items and deadlines (email_extract_action_items).
3. Create reply drafts for Needs-reply items (email_create_draft). Summarise with links.""",
    },
    {
        "name": "Meeting minutes", "slug": "meeting-minutes", "category": "productivity", "icon": "notebook-pen",
        "agent": "executive-assistant", "tool_groups": ["audio", "documents", "pm"],
        "description": "Minutes with decisions and action items from a recording or transcript.",
        "inputs": [_i("source", "Recording or transcript path", required=True)],
        "instructions": """1. Transcribe audio (audio_transcribe) or read the transcript.
2. Write minutes: attendees, agenda, discussion summary, decisions, action items (owner, due date).
3. Save output/minutes.docx; offer to create tasks in the PM tool.""",
    },
    {
        "name": "RFP response draft", "slug": "rfp-response", "category": "sales", "icon": "file-check",
        "agent": "communications-writer", "tool_groups": ["rag", "documents", "sheets"],
        "description": "Requirements matrix and first-draft answers from company knowledge.",
        "inputs": [_i("rfp", "RFP document", required=True), _i("deadline", "Deadline", "date")],
        "instructions": """1. Extract every requirement/question into output/rfp-matrix.xlsx (id, requirement, owner, status).
2. Draft answers from the knowledge base with citations; flag gaps needing SMEs.
3. Save the draft response output/rfp-response.docx.""",
    },
    {
        "name": "Vendor risk assessment", "slug": "vendor-risk", "category": "legal", "icon": "shield-alert",
        "agent": "legal-reviewer", "tool_groups": ["search", "rag", "documents"],
        "description": "Security, financial, compliance and reputational risk review of a vendor.",
        "inputs": [_i("vendor", "Vendor", required=True), _i("service", "Service provided")],
        "instructions": """1. Research the vendor: security certifications (SOC 2, ISO 27001), breaches, financial health, litigation.
2. Compare with the company's vendor policy in the knowledge base.
3. Output a risk matrix with ratings and required mitigations (output/vendor-risk-<vendor>.docx).""",
    },
    {
        "name": "Content calendar", "slug": "content-calendar", "category": "communications", "icon": "calendar-days",
        "agent": "communications-writer", "tool_groups": ["search", "sheets", "rag"],
        "description": "A month of content ideas with channels, formats and owners.",
        "inputs": [_i("goal", "Goal / theme", required=True), _i("channels", "Channels", placeholder="LinkedIn, blog, newsletter")],
        "instructions": """1. Review brand guidelines and past content in the knowledge base.
2. Propose 12–20 pieces with title, angle, channel, format, CTA, date.
3. Save output/content-calendar.xlsx.""",
    },
]
