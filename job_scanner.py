"""
Job posting scanner for company career sites.

Supports two site "type"s, since different companies' career sites run on
different platforms:

  - "oracle_hcm": Oracle Recruiting Cloud / Oracle Fusion HCM (e.g. Amex).
    Has a public JSON REST API — fast and reliable.
  - "phenom_careerconnect": Phenom People / CareerConnect (e.g. United
    Airlines). No public API; the search-results page is rendered by
    JavaScript, so this uses a headless browser (Playwright) to load the
    page and read job links off the rendered page.

For each site in config.json, this script:
  1. Fetches that site's current job postings (API call or browser render,
     depending on type)
  2. Filters postings whose title matches one of the configured keywords
  3. Compares against seen_jobs.json to find NEW postings only
  4. Emails a summary of new postings via Gmail SMTP
  5. Updates seen_jobs.json so the same posting isn't emailed twice
"""

import json
import os
import re
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


def fetch_jobs_oracle(site):
    """Query the Oracle Fusion recruitingCEJobRequisitions API for one site.

    IMPORTANT: the `finder` parameter only accepts a fixed set of variable
    names (see Oracle's own REST API docs for recruitingICEJobRequisitions,
    finder=findReqs). Valid ones we use here: siteNumber, limit, sortBy,
    locationId, keyword. Note that 'locationLevel' and 'mode' — which show
    up in the browser's careers page URL — are NOT valid finder variables;
    they're frontend-only UI state and including them causes Oracle to
    reject the whole finder as invalid.

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
            # and adjust the .get() keys if needed.
            job_id = req.get("Id") or req.get("RequisitionId") or req.get("JobId")
            title = req.get("Title", "")
            posted = req.get("PostedDate") or req.get("ExternalPostedDate", "")
            if job_id and title:
                jobs.append({"id": str(job_id), "title": title, "posted": posted})

    if not jobs and data.get("items"):
        print("DEBUG: no jobs parsed — RAW SAMPLE JOB from response:")
        print(json.dumps(data["items"][0], indent=2)[:2000])

    return jobs


def fetch_jobs_phenom(site):
    """Render a Phenom People / CareerConnect search-results page with a
    headless browser and pull job links off the rendered DOM.

    Phenom career sites (careers.united.com and similar) build the job
    listing client-side with JavaScript — the raw HTML has no job data in
    it, so plain requests.get() won't work here (unlike the Oracle sites).
    Playwright launches a real (headless) Chromium browser to run that
    JavaScript first, then we read the result.

    We identify job links by matching the confirmed URL shape used by this
    platform: .../job/<jobId>/<slug-title>. If a site's actual markup
    differs, this will silently find 0 jobs — check the debug dump printed
    below (search logs for "DEBUG: dumping"), which lists every link found
    on the page so the pattern can be adjusted.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is required for 'phenom_careerconnect' sites. "
            "Install with: pip install playwright && playwright install chromium"
        )

    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(site["search_url"], wait_until="networkidle", timeout=45000)
        # Give client-side rendering extra time to finish populating results.
        page.wait_for_timeout(3000)

        # Force sort order to "Most recent" so results are always freshest-first.
        # This site's sort dropdown re-fetches results via JS on change
        # (change.delegate="sortfilterSearch()"), so a URL parameter alone
        # won't do it — we select the option in the rendered page itself.
        try:
            page.select_option("#sortselect", label="Most recent")
            page.wait_for_timeout(3000)  # let sortfilterSearch() finish re-rendering
        except Exception as e:
            print(f"  WARNING: could not set sort order to 'Most recent': {e}")

        anchors = page.query_selector_all('a[href*="/job/"]')
        seen_hrefs = set()
        for a in anchors:
            href = a.get_attribute("href")
            title = (a.inner_text() or "").strip()
            if not href or not title or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            if href.startswith("/"):
                href = f"https://{site['domain']}{href}"

            # Job ID is the path segment right after "/job/"
            match = re.search(r"/job/([^/]+)/", href)
            job_id = match.group(1) if match else href

            jobs.append({"id": job_id, "title": title, "url": href})

        if not jobs:
            print("DEBUG: dumping all links found on the rendered page for troubleshooting:")
            all_links = page.query_selector_all("a[href]")
            for a in all_links[:40]:
                print(f"  {a.get_attribute('href')}  |  {(a.inner_text() or '').strip()[:60]}")

        browser.close()

    return jobs


def matches_keywords(title, keywords):
    """Match keywords as whole words/phrases, not raw substrings.

    Plain substring matching would let "software engineer i" match
    "software engineer ii", "software engineer intern", etc. — anything
    where "i" is glued to more letters. Word-boundary regex matching fixes
    this: it only matches when the keyword's last character is followed by
    a non-letter (space, dash, end of string, ...), not another letter.
    """
    title_lower = title.lower()
    for kw in keywords:
        pattern = r"\b" + re.escape(kw.lower()) + r"\b"
        if re.search(pattern, title_lower):
            return True
    return False


def send_email(new_matches):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]

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
        site_type = site.get("type", "oracle_hcm")

        try:
            if site_type == "oracle_hcm":
                jobs = fetch_jobs_oracle(site)
            elif site_type == "phenom_careerconnect":
                jobs = fetch_jobs_phenom(site)
            else:
                print(f"  ERROR: unknown site type '{site_type}'", file=sys.stderr)
                continue
        except requests.RequestException as e:
            print(f"  ERROR fetching {site['name']}: {e}", file=sys.stderr)
            if getattr(e, "response", None) is not None:
                print(f"  Response body: {e.response.text[:1000]}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  ERROR fetching {site['name']}: {e}", file=sys.stderr)
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
            if "url" not in job:
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