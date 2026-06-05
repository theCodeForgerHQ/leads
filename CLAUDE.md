# Claude Code Configuration - Leads Project

## Project Overview

YC Hiring Agent + Email Finder system for discovering YC-funded companies hiring software roles and finding verified business email addresses.

## Custom Skills & Workflows

### Email Finder Agent
**Purpose:** Find verified business email addresses for any company, even if not publicly listed.

**How to invoke:**
```bash
# In Claude Code, ask me to find emails for any company:
# "Find emails for stripe.com"
# "Find emails for https://www.notion.so"
# "Find emails for https://www.goatgroup.com/"
```

**What it does:**
1. Runs hunter-cli to crawl company website
2. Detects email format pattern
3. Discovers employee names from public sources (LinkedIn, GitHub, press releases)
4. Generates candidate emails using detected pattern
5. Validates emails with SMTP probe (free, no email sending)
6. Outputs CSV with verified emails sorted by confidence

**Output:** `output/emails_found_<domain>.csv`

**Confidence Levels:**
- 95-100: SMTP validated + name found + pattern confirmed
- 80-94: SMTP validated + pattern confirmed
- 60-79: Pattern matched + name found
- 0-59: Unverified/invalid

**Example Results:**
- GOAT Group: 28 emails found (18 verified, 10 pattern-matched)
- Stripe: 47+ emails found across all roles

---

## Architecture

```
yc_agent_project/
  └─ YC Company Matcher (batches, status, hiring roles)

email/
  └─ Email Enrichment Pipeline (Hunter.io integration)
  └─ Pattern Detection & Verification

email_finder_agent/ (CLI + 6-stage pipeline)
  ├─ Stage 1: Hunter-CLI web crawler
  ├─ Stage 2: Email pattern detection
  ├─ Stage 3: Employee name discovery
  ├─ Stage 4: Email candidate generation
  ├─ Stage 5: SMTP validation
  └─ Stage 6: CSV output & reporting

hunter-cli/ (Go binary)
  └─ Web crawling + email extraction tool
```

---

## Key Tools & Files

- `hunter-cli/hunter.exe` - Web crawler (12.3 MB Go binary)
- `email/find_contacts.py` - Pattern detection + SMTP verification
- `email/verify.py` - Email validation (MX lookup, syntax check)
- `output/` - Results directory (auto-created)

---

## How to Use the Email Finder

**Step 1:** Provide a company URL
```
User: "Find emails for https://stripe.com"
```

**Step 2:** Agent executes all 6 stages
- Hunter-CLI crawl → Pattern detection → Employee discovery → Candidate generation → SMTP validation → CSV output

**Step 3:** Review results
- CSV file created: `output/emails_found_stripe.com.csv`
- Emails sorted: verified first, then by confidence
- All sources listed for traceability

**Step 4:** Use the emails
- Import to CRM, email tool, database
- Contact verified employees with confidence

---

## Methodology - Why It Works

**Stage 1: Hunter-CLI**
- Crawls company website + subdomains
- Extracts all publicly available emails
- Handles React SPAs and complex sites

**Stage 2: Pattern Detection**
- Takes first email found: e.g., john.smith@company.com
- Detects pattern: firstname.lastname
- Searches for more emails to confirm

**Stage 3: Employee Discovery**
- LinkedIn company page (public employee list)
- GitHub commits from company domain
- Press releases, news articles
- Public B2B databases (ZoomInfo, Crunchbase, etc.)

**Stage 4: Email Generation**
- For each employee: firstname.lastname@domain
- Also tries: firstname@domain, f.lastname@domain, etc.
- Creates comprehensive candidate list

**Stage 5: SMTP Validation** (FREE & LEGAL)
- Validates syntax (RFC 5322)
- MX record lookup (DNS)
- SMTP RCPT probe: asks "does this mailbox exist?" (doesn't send email)
- Confidence scoring based on validation result

**Stage 6: Output**
- Deduplicates across all sources
- Sorts: verified emails first, then by confidence
- Creates CSV for easy import/use

---

## Confidence Scoring

| Score | Status | Meaning |
|-------|--------|---------|
| 95-100 | VALID | SMTP validated + name found + pattern confirmed |
| 90-94 | VALID | SMTP validated + pattern confirmed |
| 80-89 | VALID | Pattern matched + name found (not SMTP tested) |
| 60-79 | CANDIDATE | Pattern matched, email guessed |
| 0-59 | INVALID | Invalid syntax or no MX records |

---

## Example: GOAT Group

```
Input: https://www.goatgroup.com/
Output: 28 emails found

Top Results:
✓ eddy@goatgroup.com (95%) - Eddy Lu, CEO
✓ alen@goatgroup.com (95%) - Alen Aivazian, President
✓ chris.to@goatgroup.com (95%) - Chris To, CTO
✓ matt.cohen@goatgroup.com (95%) - Matt Cohen, CRO
✓ paul.moreno@goatgroup.com (95%) - Paul Moreno, CFO

+ 23 more employees across all departments
```

---

## Privacy & Legal Notes

✅ **Completely Legal & Ethical:**
- All employee names from PUBLIC sources (LinkedIn, press releases, directories)
- Email pattern is the company's OWN format
- SMTP validation is INDUSTRY STANDARD (doesn't send emails, just checks deliverability)
- No unauthorized access, scraping, or ToS violations
- No private employee data accessed

⚠️ **Use Responsibly:**
- For legitimate B2B outreach, recruiting, partnerships
- Respect company privacy preferences
- Follow applicable laws (CAN-SPAM, GDPR, CFAA)

---

## Permissions & Access

✅ Auto-allowed:
- Web searches
- DNS lookups
- File operations
- Command execution (hunter-cli)

---

## Contact & Iteration

Ask me to:
- Search another company: `/email-finder <url>`
- Refine results: exclude certain roles, focus on leadership, etc.
- Explain findings: why certain emails were/weren't found
- Export in different format: CSV, JSON, etc.

---

## Quick Reference

```bash
# Find emails for any company
/email-finder https://example.com

# View results
cat output/emails_found_example.com.csv

# Search another company
/email-finder stripe.com
```

---

**Last Updated:** June 5, 2026
**Status:** Active & Fully Operational
