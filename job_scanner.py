"""
Job posting scanner for Oracle Recruiting Cloud / Oracle Fusion HCM career sites
(this is the ATS American Express's careers.americanexpress.com runs on).

For each site in config.json, this script:
  1. Calls the site's public recruitingCEJobRequisitions REST API
  2. Filters postings whose title matches one of the configured keywords
  3. Compares against seen_jobs.json to find NEW postings only
  4. Emails a summary of new postings via Gmail SMTP
  5. Updates seen_jobs.json so the same posting isn't emailed twice

Add more sites to config.json (each needs: domain, site_number, and the
location/mode params copied from that site's careers URL) to monitor more
than one company/portal.
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"
SEEN_JOBS_PATH = Path(__file__).parent / "seen_jobs.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobScannerBot/1.0)",
    "Accept": "application/json",
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_seen_jobs():
    if SEEN_JOBS_PATH.exists():
        with open(SEEN_JOBS_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen_ids):
    with open(SEEN_JOBS_PATH, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def fetch_jobs(site):
    """Query the Oracle Fusion recruitingCEJobRequisitions API for one site.

    IMPORTANT: the `finder` parameter only accepts a fixed set of variable
    names (see Oracle's own REST API docs for recruitingICEJobRequisitions,
    finder=findReqs). Valid ones we use here: siteNumber, limit, sortBy,
    locationId, keyword. Note that 'locationLevel' and 'mode' — which show
    up in the browser's careers page URL — are NOT valid finder variables;
    they're frontend-only UI state and including them causes Oracle to
    reject the whole finder as invalid (this was the earlier 400 error).

    We also must NOT let requests percent-encode ';' and ',' in the finder
    value (that happens automatically via params=), so the URL is built
    manually here, with those characters kept literal via
    quote(..., safe=";,=").
    """
    base_url = f"https://{site['domain']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    finder_vars = [f"siteNumber={site['site_number']}", "limit=100", "sortBy=POSTING_DATES_DESC"]
    if site.get("location_id"):
        finder_vars.append(f"locationId={site['location_id']}")
    finder = "findReqs;" + ",".join(finder_vars)

    url = (
        f"{base_url}?onlyData=true&expand=requisitionList"
        f"&finder={quote(finder, safe=';,=')}"
    )

    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for item in data.get("items", []):
        for req in item.get("requisitionList", []):
            # NOTE: field names below (Id / Title / PostedDate) come from
            # Oracle's documented schema. If the site's actual response uses
            # slightly different keys, this will silently skip jobs — check
            # the debug dump printed below (search logs for "RAW SAMPLE JOB")
            # on the first run and adjust the .get() keys if needed.
            job_id = req.get("Id") or req.get("RequisitionId") or req.get("JobId")
            title = req.get("Title", "")
            posted = req.get("PostedDate") or req.get("ExternalPostedDate", "")
            if job_id and title:
                jobs.append({"id": str(job_id), "title": title, "posted": posted})

    if not jobs and data.get("items"):
        print("DEBUG: no jobs parsed — RAW SAMPLE JOB from response:")
        print(json.dumps(data["items"][0], indent=2)[:2000])

    return jobs


def matches_keywords(title, keywords):
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords)


def send_email(new_matches):
    gmail_user = "achyutnanda001@gmail.com"
    gmail_app_password = "woiz ghpg slmx qslz"

    lines = []
    for site_name, jobs in new_matches.items():
        lines.append(f"\n{site_name}:")
        for job in jobs:
            lines.append(f"  - {job['title']}")
            lines.append(f"    {job['url']}")

    body = "New matching job postings found:\n" + "\n".join(lines)
    total = sum(len(v) for v in new_matches.values())

    msg = MIMEText(body)
    msg["Subject"] = f"{total} new job posting(s) found"
    msg["From"] = gmail_user
    msg["To"] = gmail_user

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.send_message(msg)

    print(f"Email sent: {total} new posting(s).")


def main():
    config = load_config()
    seen_ids = load_seen_jobs()
    new_matches = {}

    for site in config["sites"]:
        print(f"Checking {site['name']}...")
        try:
            jobs = fetch_jobs(site)
        except requests.RequestException as e:
            print(f"  ERROR fetching {site['name']}: {e}", file=sys.stderr)
            if e.response is not None:
                print(f"  Response body: {e.response.text[:1000]}", file=sys.stderr)
            continue

        print(f"  Fetched {len(jobs)} total postings.")

        site_matches = []
        for job in jobs:
            unique_id = f"{site['domain']}:{job['id']}"
            if not matches_keywords(job["title"], config["keywords"]):
                continue
            if unique_id in seen_ids:
                continue

            seen_ids.add(unique_id)
            job["url"] = site["careers_job_url_template"].format(job_id=job["id"])
            site_matches.append(job)

        if site_matches:
            new_matches[site["name"]] = site_matches
            print(f"  {len(site_matches)} new matching posting(s).")

    if new_matches:
        send_email(new_matches)
    else:
        print("No new matching postings.")

    save_seen_jobs(seen_ids)


if __name__ == "__main__":
    main()