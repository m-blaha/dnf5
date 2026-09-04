#!/usr/bin/env python3
"""
Fetch unanswered issues, unreviewed PRs, and stale discussions across a curated list of GitHub repositories.
Outputs a clean Markdown report to stdout.
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration & Setup
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
TARGET_REPOS_RAW = os.environ.get("TARGET_REPOS", "").strip()

if not TOKEN:
    print("Error: GH_TOKEN or GITHUB_TOKEN environment variable missing.", file=sys.stderr)
    sys.exit(1)

if not TARGET_REPOS_RAW:
    print("Error: TARGET_REPOS environment variable is required (e.g., 'owner/repo1, owner/repo2').", file=sys.stderr)
    sys.exit(1)

TARGET_REPOS = [r.strip() for r in TARGET_REPOS_RAW.split(",") if r.strip()]

if not TARGET_REPOS:
    print("Error: TARGET_REPOS contains no valid repository entries.", file=sys.stderr)
    sys.exit(1)

# Build search filter (e.g., "repo:owner/repo1 repo:owner/repo2")
SCOPE_FILTER = " ".join([f"repo:{repo}" for repo in TARGET_REPOS])

# Threshold settings
DAYS_STALE = 3
NOW = datetime.now(timezone.utc)
CUTOFF_DATE = NOW - timedelta(days=DAYS_STALE)
CUTOFF_STR = CUTOFF_DATE.strftime("%Y-%m-%d")

TEAM_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}

# ---------------------------------------------------------------------------
# GraphQL Query Construction & Execution
# ---------------------------------------------------------------------------
GRAPHQL_URL = "https://api.github.com/graphql"

GRAPHQL_QUERY = """
query GetStaleItems($issueQuery: String!, $prQuery: String!, $discussionQuery: String!) {
  issues: search(query: $issueQuery, type: ISSUE, first: 50) {
    nodes {
      ... on Issue {
        title
        url
        createdAt
        updatedAt
        repository { name }
        author { login }
        comments(last: 5) {
          nodes {
            createdAt
            authorAssociation
            author { login }
          }
        }
      }
    }
  }
  prs: search(query: $prQuery, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        title
        url
        createdAt
        updatedAt
        isDraft
        repository { name }
        author { login }
        reviews(first: 1) {
          totalCount
        }
      }
    }
  }
  discussions: search(query: $discussionQuery, type: DISCUSSION, first: 50) {
    nodes {
      ... on Discussion {
        title
        url
        createdAt
        isAnswered
        repository { name }
        author { login }
        comments {
          totalCount
        }
      }
    }
  }
}
"""


def make_graphql_request(query, variables):
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Stale-Items-Digest"
        }
    )
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if "errors" in res_data:
                print(f"GraphQL Errors: {res_data['errors']}", file=sys.stderr)
            return res_data.get("data", {})
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}", file=sys.stderr)
        sys.exit(1)

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def calculate_age(iso_date_str):
    created = datetime.fromisoformat(iso_date_str.replace("Z", "+00:00"))
    delta = NOW - created
    days = delta.days
    return f"{days} day{'s' if days != 1 else ''}"


def is_bot(author):
    if not author or "login" not in author:
        return True
    login = author["login"]
    return login.endswith("[bot]") or login in {"dependabot", "renovate", "github-actions"}

# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------


def main():
    variables = {
        "issueQuery": f"{SCOPE_FILTER} is:open is:issue draft:false archived:false created:<{CUTOFF_STR} -label:on-hold -label:wontfix",
        "prQuery": f"{SCOPE_FILTER} is:open is:pr draft:false archived:false review:required created:<{CUTOFF_STR}",
        "discussionQuery": f"{SCOPE_FILTER} is:open archived:false created:<{CUTOFF_STR}"
    }

    raw_data = make_graphql_request(GRAPHQL_QUERY, variables)

    # 1. Process Pull Requests
    stale_prs = []
    for pr in raw_data.get("prs", {}).get("nodes", []):
        if not pr or is_bot(pr.get("author")):
            continue
        if pr.get("reviews", {}).get("totalCount", 0) == 0:
            stale_prs.append({
                "repo": pr["repository"]["name"],
                "title": pr["title"],
                "url": pr["url"],
                "author": pr["author"]["login"] if pr.get("author") else "ghost",
                "age": calculate_age(pr["createdAt"])
            })

    # 2. Process Issues
    stale_issues = []
    for issue in raw_data.get("issues", {}).get("nodes", []):
        if not issue or is_bot(issue.get("author")):
            continue

        comments = issue.get("comments", {}).get("nodes", [])
        has_team_response = any(c.get("authorAssociation") in TEAM_ROLES for c in comments)

        reason = None
        if not comments:
            reason = "Zero comments"
        elif not has_team_response:
            reason = "Awaiting team response"

        if reason:
            stale_issues.append({
                "repo": issue["repository"]["name"],
                "title": issue["title"],
                "url": issue["url"],
                "author": issue["author"]["login"] if issue.get("author") else "ghost",
                "status": reason,
                "age": calculate_age(issue["createdAt"])
            })

    # 3. Process Discussions
    stale_discussions = []
    for disc in raw_data.get("discussions", {}).get("nodes", []):
        if not disc or is_bot(disc.get("author")):
            continue

        is_answered = disc.get("isAnswered", False)
        comment_count = disc.get("comments", {}).get("totalCount", 0)

        if not is_answered or comment_count == 0:
            stale_discussions.append({
                "repo": disc["repository"]["name"],
                "title": disc["title"],
                "url": disc["url"],
                "author": disc["author"]["login"] if disc.get("author") else "ghost",
                "status": "Unanswered" if not is_answered else "No replies",
                "age": calculate_age(disc["createdAt"])
            })

    # ---------------------------------------------------------------------------
    # Output Markdown
    # ---------------------------------------------------------------------------
    formatted_repo_list = ", ".join([f"`{r}`" for r in TARGET_REPOS])
    print(f"# ⚠️ Unanswered Items Digest\n")
    print(f"*Target Repositories:* {formatted_repo_list} | *Threshold:* **Older than {DAYS_STALE} days**\n")

    # PRs Table
    print(f"## 🔀 Pull Requests Needing Review ({len(stale_prs)})\n")
    if stale_prs:
        print("| Repository | Title | Author | Age |")
        print("| :--- | :--- | :--- | :--- |")
        for item in stale_prs:
            print(f"| `{item['repo']}` | [{item['title']}]({item['url']}) | @{item['author']} | {item['age']} |")
    else:
        print("*No unreviewed PRs found. Great job! 🎉*")
    print("\n---\n")

    # Issues Table
    print(f"## ❓ Unanswered Issues ({len(stale_issues)})\n")
    if stale_issues:
        print("| Repository | Title | Author | Status | Age |")
        print("| :--- | :--- | :--- | :--- | :--- |")
        for item in stale_issues:
            print(f"| `{item['repo']}` | [{item['title']}]({item['url']
                                                            }) | @{item['author']} | {item['status']} | {item['age']} |")
    else:
        print("*No unanswered issues found! 🎉*")
    print("\n---\n")

    # Discussions Table
    print(f"## 💬 Unanswered Discussions ({len(stale_discussions)})\n")
    if stale_discussions:
        print("| Repository | Title | Author | Status | Age |")
        print("| :--- | :--- | :--- | :--- | :--- |")
        for item in stale_discussions:
            print(f"| `{item['repo']}` | [{item['title']}]({item['url']
                                                            }) | @{item['author']} | {item['status']} | {item['age']} |")
    else:
        print("*No stale discussions found! 🎉*")


if __name__ == "__main__":
    main()
