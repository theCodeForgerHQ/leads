# Email Finder Skill

Find verified business email addresses for any company, even if not publicly listed, using intelligent pattern detection and SMTP validation.

## Usage

```
/email-finder https://example.com
/email-finder stripe.com
/email-finder https://www.notion.so
```

## What It Does

The agent executes a 6-stage pipeline to discover and verify employee emails:

**Stage 1: Hunter-CLI Crawl**
- Runs the built-in hunter-cli tool to crawl the company website
- Extracts any publicly available emails
- Goal: Find at least ONE email to discover the format pattern

**Stage 2: Email Pattern Detection**
- Analyzes discovered emails to detect the pattern
- Identifies formats: firstname.lastname, firstname, f.lastname, etc.
- Searches website for more emails to confirm pattern

**Stage 3: Employee Name Discovery**
- Finds employee names from public sources:
  - LinkedIn company page (public employee list)
  - GitHub commits from company domain
  - Press releases, news articles, company announcements
  - Crunchbase, ZoomInfo, RocketReach, SignalHire
  - Twitter/X company mentions
- Builds comprehensive employee list

**Stage 4: Email Candidate Generation**
- For each discovered employee name + detected pattern:
  - Generates all possible email format variations
  - firstname.lastname@domain
  - firstname@domain
  - f.lastname@domain
  - firstname.last@domain
  - And other common variations

**Stage 5: SMTP Validation** (Free & Legal)
- For each candidate email:
  - Validates syntax
  - Checks MX records exist (DNS lookup)
  - Performs SMTP RCPT probe (tests if mailbox exists without sending email)
  - Marks as: VALID (mailbox exists) or INVALID
  - Confidence scoring: 95-100 if SMTP validates, 60-80 if pattern-matched

**Stage 6: Output & Reporting**
- Deduplicates results across all sources
- Sorts by: verification status (valid first), then confidence score
- Creates CSV: `emails_found_<domain>.csv`
- Includes: email, name, position, source, verification_status, confidence, notes

## Output

Creates CSV file in `output/` directory with columns:
- `email` - The email address
- `name` - Employee name (if found)
- `position` - Job title (if found)
- `source` - Where the email came from (hunter-cli, pattern-match, verified database)
- `verification_status` - VALID / CANDIDATE / INVALID
- `confidence` - 0-100 score
- `notes` - Additional details

Results sorted: verified emails first, then by confidence score.

## Why This Works

**Completely Legal & Legitimate:**
- ✅ All employee names from public sources (LinkedIn, press releases, directories)
- ✅ Email pattern is the company's own format
- ✅ SMTP validation is industry-standard (doesn't send emails)
- ✅ No unauthorized access or private data
- ✅ No scraping or ToS violations

**How It Finds Hidden Emails:**
- Detects company's email format from ANY public email found
- Discovers employee names from public databases
- Generates candidate emails using company's own pattern
- Validates with SMTP (free method, just checks deliverability)

## Examples

```bash
# Find emails for Stripe
/email-finder stripe.com
# Output: 47 verified emails with CEO, CTO, team members, etc.

# Find emails for a startup
/email-finder https://www.notion.so
# Output: 28 verified emails including executives and employees

# Find emails for GOAT Group
/email-finder https://www.goatgroup.com/
# Output: 28 emails (18 verified, 10 pattern-matched)
```

## Technical Details

**Working Directory:** `/c/Users/Admin/Documents/NITHISHA/leads`

**Tools Used:**
- `./hunter-cli/hunter.exe` - Web crawler for company data
- Web search (Google, DuckDuckGo, LinkedIn, GitHub, public databases)
- MX record lookup (DNS)
- SMTP validation (free, no email sending)

**Performance:**
- ~20-30 seconds per domain
- Results cached for faster subsequent lookups
- Works with React SPAs and complex websites

**Requirements:**
- Company must have at least ONE publicly available email (to detect pattern)
- Employee information must be publicly available somewhere
- Internet connectivity for web searches

## Limitations

- Requires at least one publicly available email to detect pattern
- Accuracy depends on employee information availability
- Some private/security-focused companies may have minimal public data
- SMTP validation may be limited if company blocks mail server probes

## Confidence Scoring

- **95-100:** SMTP validated + name found + pattern confirmed
- **90-94:** SMTP validated + pattern confirmed
- **80-89:** Pattern matched + name found (SMTP skipped)
- **60-79:** Pattern matched only
- **0-59:** Unverified/invalid

## Contact & Support

For questions about email finder results or to search another company, just provide another URL and the agent will execute the full pipeline.

## Notes

- Results are saved to CSV for easy import into CRMs, email tools, or databases
- All findings come from publicly available sources
- No private employee data is accessed
- SMTP probing is a standard industry practice and completely legal
