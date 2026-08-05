"""
One-time setup script — creates all required properties on the Notion review database.

Run once after creating the empty database in Notion and sharing it with the integration:
    python3 setup_notion_review_db.py                          # DailyNews review DB
    python3 setup_notion_review_db.py --section notion_review_epicfury  # EpicFury review DB

Requires:
    - notion_review.api_key set in config.yaml
    - notion_review.review_database_id set in config.yaml
    - The integration must be connected to the database (Share → Add connections)
"""

import argparse
import asyncio
import sys

import aiohttp
import yaml

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Properties to create on the database.
# The default title property is named "Name" by Notion — we rename it to "Title".
# All other properties are added fresh.
_PROPERTIES = {
    # Rename default title property
    "Name": {
        "name": "Title",
        "title": {},
    },
    # Article content fields
    "Post Content": {"rich_text": {}},
    "AI Comment": {"rich_text": {}},
    "Source": {"rich_text": {}},
    "Source URL": {"url": {}},
    "Image URL": {"url": {}},
    "Published": {"date": {}},
    # Scoring
    "Score": {"number": {"format": "number"}},
    "Score Label": {
        "select": {
            "options": [
                {"name": "🔵 Excellent", "color": "blue"},
                {"name": "🟢 Good", "color": "green"},
                {"name": "🟡 OK", "color": "yellow"},
                {"name": "🟠 Weak", "color": "orange"},
                {"name": "🔴 Poor", "color": "red"},
            ]
        }
    },
    # Approval workflow — user sets this to trigger pipeline action
    "Decision": {
        "select": {
            "options": [
                {"name": "Pending", "color": "yellow"},
                {"name": "Approved", "color": "green"},
                {"name": "Rejected", "color": "red"},
                {"name": "Publish Now", "color": "blue"},
                {"name": "Published", "color": "default"},
                {"name": "Discarded", "color": "gray"},
            ]
        }
    },
    # Editor can type custom post text here; if non-empty, overrides Post Content on approval
    "Edit Override": {"rich_text": {}},
    # Internal — do not edit in Notion
    "Article ID": {"rich_text": {}},
}


async def main(section: str) -> None:
    with open("config.yaml") as f:
        data = yaml.safe_load(f)

    nr = data.get(section, {})
    api_key = nr.get("api_key", "").strip()
    db_id = nr.get("review_database_id", "").strip()

    if not api_key:
        print(f"ERROR: {section}.api_key is empty in config.yaml")
        print("  Get your integration token at https://www.notion.so/profile/integrations")
        sys.exit(1)
    if not db_id:
        print(f"ERROR: {section}.review_database_id is empty in config.yaml")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    print(f"Patching database {db_id} ...")
    async with aiohttp.ClientSession() as session:
        async with session.patch(
            f"{NOTION_API_BASE}/databases/{db_id}",
            headers=headers,
            json={"properties": _PROPERTIES},
        ) as resp:
            body = await resp.json(content_type=None)

    if resp.status == 200:
        print("✅ Done. Properties on the database:")
        for name in sorted(body.get("properties", {})):
            ptype = body["properties"][name].get("type", "?")
            print(f"  • {name}  ({ptype})")
    else:
        print(f"❌ Notion API error {resp.status}:")
        print(body)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--section",
        default="notion_review",
        help="config.yaml section to read (default: notion_review)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.section))
