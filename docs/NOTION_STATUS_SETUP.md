# Notion live build status

The repo root `status.json` is the source of truth. On every push to `main`, GitHub Actions runs `scripts/sync_notion_status.py` and replaces your Notion status page with the latest checklist.

## One-time setup

### 1. Create a Notion integration

1. Go to https://www.notion.so/my-integrations
2. Create an integration (e.g. "Verbatim Studio Sync")
3. Copy the **Internal Integration Secret** → this is `NOTION_TOKEN`

### 2. Create a status page

1. In Notion, create a page titled **Verbatim Studio — Build Status**
2. Share the page with your integration (⋯ → Connect to → your integration)
3. Copy the page ID from the URL:
   - `https://www.notion.so/Your-Workspace/Page-Title-` **`abc123def456...`**
   - Use the 32-character ID (with or without hyphens)

### 3. Add GitHub secrets

In **github.com/angryhephilump-tech/ocr-book-pipeline** → Settings → Secrets and variables → Actions:

| Secret | Value |
|--------|--------|
| `NOTION_TOKEN` | Integration secret |
| `NOTION_PAGE_ID` | Page ID from step 2 |

### 4. Enable Notion in Cursor (optional)

To let Cursor post updates directly, enable the **Notion** MCP plugin in Cursor settings and authenticate. The GitHub Action works without Cursor MCP.

## Update status after each work session

Edit `status.json` (especially `last_cursor_note` and boolean flags), commit, and push to `main`.

## Test locally

```powershell
$env:NOTION_TOKEN = "secret_..."
$env:NOTION_PAGE_ID = "your-page-id"
python scripts/sync_notion_status.py
```

## Cursor MCP note

If `CallMcpTool` reports "MCP server does not exist: notion", enable the Notion plugin under Cursor → Settings → MCP. Until then, use GitHub Actions or the local command above.
