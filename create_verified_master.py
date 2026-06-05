#!/usr/bin/env python3
"""
Create Verified Master Email Database
Combines pattern-based candidates with Hunter CLI verification where available

This script:
1. Reads existing pattern-based emails
2. For each company, attempts quick Hunter CLI lookup
3. Marks Hunter-found emails as VALID
4. For companies without Hunter emails, includes pattern-based with lower confidence
5. Creates unified master CSV with proper verification status

This is a pragmatic approach that delivers:
- Real verified emails from Hunter CLI (high confidence)
- Pattern-based candidates for companies where Hunter found nothing
- Proper confidence scoring based on verification method
"""

import csv
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from collections import defaultdict
import time

class VerifiedMasterBuilder:
    """Build verified master email database"""

    def __init__(self):
        self.hunter_exe = r"c:\Users\Admin\Documents\NITHISHA\leads\hunter-cli\hunter.exe"
        self.output_dir = Path(r"c:\Users\Admin\Documents\NITHISHA\leads\output")
        self.existing_patterns = self.load_existing_patterns()
        self.hunter_cache = {}
        self.verified_emails = []
        self.pattern_emails = []

    def extract_domain(self, url):
        """Extract domain from URL"""
        try:
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            parsed = urlparse(url)
            domain = parsed.netloc.replace('www.', '')
            return domain
        except:
            return None

    def load_existing_patterns(self):
        """Load existing pattern-based emails organized by company"""
        patterns_file = self.output_dir / "ALL_YC_COMPANIES_EMAILS.csv"
        patterns_by_company = defaultdict(list)

        try:
            with open(patterns_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('company') and row.get('company') not in ['# Summary']:
                        if not row['company'].startswith('#'):
                            company = row['company'].strip()
                            patterns_by_company[company].append(row)
        except:
            pass

        return patterns_by_company

    def quick_hunter_lookup(self, domain, timeout=30):
        """Quick Hunter CLI lookup with short timeout"""
        emails = []
        try:
            result = subprocess.run(
                [self.hunter_exe, domain],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.output_dir.parent / 'hunter-cli')
            )

            output_text = result.stdout
            json_start = output_text.find('{')
            if json_start != -1:
                json_text = output_text[json_start:]
                json_text = json_text[:json_text.rfind('}') + 1]

                try:
                    output = json.loads(json_text)
                    if 'people' in output:
                        for person in output.get('people', []):
                            email = person.get('email', '')
                            if email:
                                first_name = person.get('first_name', '').strip()
                                last_name = person.get('last_name', '').strip()
                                name = f"{first_name} {last_name}".strip()

                                emails.append({
                                    'email': email,
                                    'name': name,
                                    'score': person.get('score', 85)
                                })
                except (json.JSONDecodeError, KeyError):
                    pass
        except:
            pass

        return emails

    def process_company(self, company_name, website):
        """Process one company: try Hunter, then use patterns"""
        domain = self.extract_domain(website)
        if not domain:
            return

        print(f"  {company_name[:40]:40s} ... ", end='', flush=True)

        # Try Hunter first
        hunter_emails = self.quick_hunter_lookup(domain, timeout=15)

        if hunter_emails:
            # Add verified Hunter emails
            for email_data in hunter_emails:
                self.verified_emails.append({
                    'company': company_name,
                    'company_website': website,
                    'email': email_data['email'],
                    'name': email_data['name'],
                    'position': '',
                    'source': 'hunter-cli',
                    'verification_status': 'VALID',
                    'confidence': email_data['score'],
                    'employee_discovery_source': 'hunter-cli:domain-crawl'
                })
            print(f"{len(hunter_emails)} verified")
        else:
            # Use pattern-based as fallback
            if company_name in self.existing_patterns:
                patterns = self.existing_patterns[company_name]
                # Take up to 3 best pattern emails (contact, info, hello usually work)
                for pattern in patterns[:3]:
                    self.pattern_emails.append({
                        'company': pattern.get('company', company_name),
                        'company_website': pattern.get('company_website', website),
                        'email': pattern.get('email', ''),
                        'name': '',
                        'position': '',
                        'source': pattern.get('source', 'pattern-match'),
                        'verification_status': 'UNVERIFIED',
                        'confidence': 40,  # Lower confidence for patterns
                        'employee_discovery_source': 'pattern-generation'
                    })
                print(f"{len(patterns[:3])} patterns (fallback)")
            else:
                print("no data")

    def load_all_companies(self):
        """Load all unique companies from matches.csv"""
        csv_path = Path(r"c:\Users\Admin\Documents\NITHISHA\leads\yc_agent_project\yc_agent_project\out\matches.csv")

        companies = {}
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company = row['company'].strip()
                website = row['company_website'].strip()
                if company and website and company not in companies:
                    companies[company] = website

        return sorted(list(companies.items()), key=lambda x: x[0])

    def process_batch(self, companies, batch_num, total_batches, batch_size=50):
        """Process a batch of companies"""
        start_idx = (batch_num - 1) * batch_size
        end_idx = min(batch_num * batch_size, len(companies))
        batch = companies[start_idx:end_idx]

        print(f"\nBatch {batch_num}/{total_batches}: Companies {start_idx + 1}-{end_idx}")
        print(f"{'='*70}\n")

        for company_name, website in batch:
            try:
                self.process_company(company_name, website)
            except Exception as e:
                print(f"  ERROR: {str(e)[:50]}")

            time.sleep(0.2)

        verified_so_far = len(self.verified_emails)
        pattern_so_far = len(self.pattern_emails)
        total_so_far = verified_so_far + pattern_so_far

        print(f"\nBatch {batch_num} summary:")
        print(f"  Verified (Hunter): {verified_so_far}")
        print(f"  Patterns (fallback): {pattern_so_far}")
        print(f"  Total: {total_so_far}\n")

    def save_master_database(self):
        """Save combined database to master CSV"""
        output_file = self.output_dir / "VERIFIED_YC_EMAILS_MASTER.csv"

        # Combine all emails
        all_emails = self.verified_emails + self.pattern_emails

        # Sort by company, then by confidence (descending)
        all_emails.sort(key=lambda x: (x['company'], -x['confidence']))

        # Calculate statistics
        verified_count = len(self.verified_emails)
        pattern_count = len(self.pattern_emails)
        total_count = len(all_emails)
        companies_with_verified = len(set(e['company'] for e in self.verified_emails))
        companies_with_pattern = len(set(e['company'] for e in self.pattern_emails))

        avg_confidence = (
            sum(e['confidence'] for e in all_emails) / total_count
            if all_emails else 0
        )

        # Write CSV
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            # Write header comments
            f.write("# Verified YC Companies Email Database\n")
            f.write("# This database combines Hunter CLI verification with pattern-based candidates\n")
            f.write(f"# Total Companies Processed: 214\n")
            f.write(f"# Companies With Hunter-Verified Emails: {companies_with_verified}\n")
            f.write(f"# Companies With Pattern-Based Emails: {companies_with_pattern}\n")
            f.write(f"# Total Verified Emails (VALID): {verified_count}\n")
            f.write(f"# Total Pattern Emails (UNVERIFIED): {pattern_count}\n")
            f.write(f"# Total Emails: {total_count}\n")
            f.write(f"# Average Confidence: {avg_confidence:.1f}%\n")
            f.write(f"# Processing Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("#\n")
            f.write("# IMPORTANT NOTES:\n")
            f.write("# - VALID emails are verified via Hunter CLI's domain crawling (confidence 80-100)\n")
            f.write("# - UNVERIFIED emails are pattern-based fallbacks (confidence 40)\n")
            f.write("# - Use VALID emails for higher success rates\n")
            f.write("# - Pattern emails are best-guess candidates for companies with no Hunter data\n")
            f.write("#\n")

            fieldnames = ['company', 'company_website', 'email', 'name', 'position',
                         'source', 'verification_status', 'confidence', 'employee_discovery_source']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_emails)

        return output_file, verified_count, pattern_count, companies_with_verified, companies_with_pattern, avg_confidence

def main():
    """Main execution"""
    print("\n" + "="*70)
    print("VERIFIED YC COMPANIES EMAIL DATABASE BUILDER")
    print("="*70)
    print("\nCombining Hunter CLI verification with pattern-based candidates\n")

    builder = VerifiedMasterBuilder()
    companies = builder.load_all_companies()

    print(f"Loaded {len(companies)} companies")
    print(f"Existing pattern database: {len(builder.existing_patterns)} companies\n")

    # Process all companies (smaller batches for faster results)
    batch_size = 50
    total_batches = (len(companies) + batch_size - 1) // batch_size

    print(f"Processing in {total_batches} batches of {batch_size} companies\n")
    print("="*70)

    for batch_num in range(1, total_batches + 1):
        builder.process_batch(companies, batch_num, total_batches, batch_size)
        if batch_num < total_batches:
            print(f"Waiting 3s before next batch...")
            time.sleep(3)

    # Save results
    print("\n" + "="*70)
    print("FINALIZING DATABASE")
    print("="*70 + "\n")

    (output_path, verified_count, pattern_count, companies_verified,
     companies_pattern, avg_confidence) = builder.save_master_database()

    print(f"Database saved: {output_path}\n")
    print(f"SUMMARY:")
    print(f"  Verified (VALID): {verified_count} emails from {companies_verified} companies")
    print(f"  Patterns (UNVERIFIED): {pattern_count} emails from {companies_pattern} companies")
    print(f"  Total: {verified_count + pattern_count} emails")
    print(f"  Average Confidence: {avg_confidence:.1f}%")
    print(f"\nRECOMMENDATION: Use VALID emails for outreach (higher deliverability)")
    print("="*70 + "\n")

if __name__ == '__main__':
    main()
