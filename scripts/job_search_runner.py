#!/usr/bin/env python3
"""Read-only public job discovery, scoring, and last-good-result persistence."""
import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "config" / "job_search_profile.json"
DEFAULT_RULES = ROOT / "config" / "job_match_rules.json"
DEFAULT_STATE = ROOT / "state" / "job_search_results.json"
FRESH_RESULTS = "FRESH_RESULTS"
NO_MATCHES = "NO_MATCHES"
SOURCE_TEMPORARILY_UNAVAILABLE = "SOURCE_TEMPORARILY_UNAVAILABLE"


class SourceUnavailable(Exception):
    pass


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fetch_public(url, timeout=12):
    request = urllib.request.Request(url, headers={
        "User-Agent": "ZamalJobSearch/1.0 (+public-read-only)",
        "Accept": "application/json, application/rss+xml, application/xml, text/html",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status in (403, 429):
                raise SourceUnavailable("HTTP %s" % response.status)
            return response.headers.get_content_type(), response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise SourceUnavailable("HTTP %s" % exc.code) from exc
        raise SourceUnavailable("HTTP error") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceUnavailable("network timeout or connection failure") from exc


def _text(value):
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _normalise(item, source):
    if not isinstance(item, dict):
        return None
    title = _text(item.get("title") or item.get("name"))
    url = _text(item.get("url") or item.get("link") or item.get("apply_url"))
    if not title or not url:
        return None
    return {
        "title": title,
        "company": _text(item.get("company") or item.get("company_name") or item.get("companyName")),
        "location": _text(item.get("location") or item.get("jobGeo") or item.get("locations")),
        "description": _text(item.get("description") or item.get("snippet") or item.get("content")),
        "url": url,
        "source": source,
        "published": _text(item.get("published") or item.get("date") or item.get("created_at")),
    }


def parse_jobs(body, source="public"):
    """Parse common public JSON/RSS responses without scraping login-only sites."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            data = data.get("data") or data.get("jobs") or data.get("results") or []
        if isinstance(data, list):
            return [job for item in data if (job := _normalise(item, source))]
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    jobs = []
    for item in root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        values = {child.tag.rsplit("}", 1)[-1]: child.text for child in item}
        job = _normalise(values, source)
        if job:
            jobs.append(job)
    return jobs


def score_job(job, profile, rules):
    title = job["title"].lower()
    text = " ".join(job.get(key, "") for key in ("title", "description", "location")).lower()
    score = 0
    title_hits = sum(1 for term in profile.get("target_roles", []) if term.lower() in title)
    skill_hits = sum(1 for term in profile.get("skills", []) if term.lower() in text)
    location_hits = sum(1 for term in profile.get("locations", []) if term.lower() in text)
    score += min(rules.get("role_points", 40), title_hits * rules.get("role_points", 40))
    score += min(rules.get("skill_points", 30), skill_hits * rules.get("skill_points", 30) // max(1, len(profile.get("skills", []))))
    score += rules.get("location_points", 20) if location_hits else 0
    if any(term.lower() in text for term in profile.get("preferred_keywords", [])):
        score += rules.get("keyword_points", 10)
    return min(100, int(score))


def match_and_sort(jobs, profile, rules):
    matched = []
    for job in jobs:
        scored = dict(job)
        scored["score"] = score_job(scored, profile, rules)
        if scored["score"] >= rules.get("shortlist_threshold", 70):
            scored["match_tier"] = "STRONG_MATCH" if scored["score"] >= rules.get("strong_match_threshold", 85) else "SHORTLIST"
            matched.append(scored)
    return sorted(matched, key=lambda item: (-item["score"], item["title"].lower()))


def _load_previous(path):
    try:
        data = _read_json(path)
        return data if isinstance(data.get("jobs"), list) else None
    except (OSError, ValueError, TypeError):
        return None


def discover(profile, rules, state_path=DEFAULT_STATE, fetcher=fetch_public):
    all_jobs = []
    unavailable = []
    successful_sources = 0
    for source in profile.get("sources", []):
        try:
            content_type, body = fetcher(source["url"], profile.get("timeout_seconds", 12))
            successful_sources += 1
            all_jobs.extend(parse_jobs(body, source.get("name", source["url"])))
        except SourceUnavailable as exc:
            unavailable.append({"source": source.get("name", source["url"]), "reason": str(exc)})
        except Exception:
            unavailable.append({"source": source.get("name", source["url"]), "reason": "invalid public response"})
    # De-duplicate before scoring so fallback and multiple boards stay readable.
    unique = {job["url"]: job for job in all_jobs}
    fresh = match_and_sort(list(unique.values()), profile, rules)
    previous = _load_previous(state_path)
    if fresh:
        result = {"status": FRESH_RESULTS, "result_origin": "FRESH", "jobs": fresh,
                  "source_errors": unavailable, "updated_at": datetime.now(timezone.utc).isoformat()}
    elif successful_sources and not unavailable:
        result = {"status": NO_MATCHES, "result_origin": "FRESH", "jobs": [],
                  "source_errors": [], "updated_at": datetime.now(timezone.utc).isoformat()}
    elif previous:
        result = dict(previous)
        result.update({"status": SOURCE_TEMPORARILY_UNAVAILABLE, "result_origin": "FALLBACK",
                       "source_errors": unavailable, "updated_at": datetime.now(timezone.utc).isoformat()})
    else:
        result = {"status": SOURCE_TEMPORARILY_UNAVAILABLE, "result_origin": "NONE", "jobs": [],
                  "source_errors": unavailable, "updated_at": datetime.now(timezone.utc).isoformat()}
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(state_path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    result = discover(_read_json(args.profile), _read_json(args.rules), args.state)
    print(json.dumps({"status": result["status"], "result_origin": result["result_origin"],
                      "job_count": len(result["jobs"]), "source_errors": result["source_errors"]}, sort_keys=True))
    # Discovery status is data for the next step; notification must still run.
    return 0


if __name__ == "__main__":
    sys.exit(main())