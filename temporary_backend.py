import os
import sys
import time
import uuid
import email
from email import policy
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("temporary_backend")

app = FastAPI(title="Temporary Email Receiver", version="1.1.0")

# Directory to save .eml files
EMAILS_DIR = Path(__file__).resolve().parent / "received_emails"
EMAILS_DIR.mkdir(parents=True, exist_ok=True)


def parse_eml_file(filepath: Path):
    stat = filepath.stat()
    try:
        with open(filepath, "rb") as fp:
            msg = email.message_from_binary_file(fp, policy=policy.default)

        # Extract text and html bodies
        text_body = ""
        html_body = ""

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdispo = str(part.get("Content-Disposition", ""))
                if "attachment" in cdispo:
                    continue
                if ctype == "text/plain" and not text_body:
                    try:
                        text_body = part.get_content()
                    except Exception:
                        text_body = str(part.get_payload(decode=True), errors="replace")
                elif ctype == "text/html" and not html_body:
                    try:
                        html_body = part.get_content()
                    except Exception:
                        html_body = str(part.get_payload(decode=True), errors="replace")
        else:
            ctype = msg.get_content_type()
            if ctype == "text/html":
                try:
                    html_body = msg.get_content()
                except Exception:
                    html_body = str(msg.get_payload(decode=True), errors="replace")
            else:
                try:
                    text_body = msg.get_content()
                except Exception:
                    text_body = str(msg.get_payload(decode=True), errors="replace")

        # Collect headers
        headers_dict = {}
        for k, v in msg.items():
            if k in headers_dict:
                headers_dict[k] += f", {v}"
            else:
                headers_dict[k] = str(v)

        return {
            "filename": filepath.name,
            "size_bytes": stat.st_size,
            "created_at": stat.st_mtime,
            "from": msg.get("from", "(Unknown Sender)"),
            "to": msg.get("to", "(Unknown Recipient)"),
            "subject": msg.get("subject", "(No Subject)"),
            "date": msg.get("date", ""),
            "text_body": text_body,
            "html_body": html_body,
            "has_html": bool(html_body.strip()),
            "headers": headers_dict,
        }
    except Exception as e:
        logger.error(f"Failed to parse {filepath.name}: {e}")
        return {
            "filename": filepath.name,
            "size_bytes": stat.st_size,
            "created_at": stat.st_mtime,
            "from": "(Error parsing)",
            "to": "(Error parsing)",
            "subject": f"Error: {e}",
            "date": "",
            "text_body": f"Failed to parse email file: {e}",
            "html_body": "",
            "has_html": False,
            "headers": {},
        }


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "Temporary Email Receiver",
        "timestamp": time.time(),
    }


@app.get("/api/emails")
async def list_emails():
    """List all received emails sorted newest first."""
    files = sorted(EMAILS_DIR.glob("*.eml"), key=lambda p: p.stat().st_mtime, reverse=True)
    emails_list = []
    for f in files:
        data = parse_eml_file(f)
        # Exclude huge bodies in list view for performance
        data_summary = {
            "filename": data["filename"],
            "size_bytes": data["size_bytes"],
            "created_at": data["created_at"],
            "from": data["from"],
            "to": data["to"],
            "subject": data["subject"],
            "date": data["date"],
            "has_html": data["has_html"],
            "snippet": (data["text_body"] or data["html_body"] or "")[:120].strip(),
        }
        emails_list.append(data_summary)
    return {"status": "ok", "count": len(emails_list), "emails": emails_list}


@app.get("/api/emails/{filename}")
async def get_email_detail(filename: str):
    """Get full details of a specific email."""
    safe_name = Path(filename).name
    filepath = EMAILS_DIR / safe_name
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Email not found")
    return parse_eml_file(filepath)


@app.get("/api/emails/{filename}/raw")
async def get_email_raw(filename: str):
    """Download raw .eml file."""
    safe_name = Path(filename).name
    filepath = EMAILS_DIR / safe_name
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Email not found")
    with open(filepath, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.get("/api/emails/{filename}/html")
async def get_email_html_body(filename: str):
    """Return raw HTML body for sandboxed iframe viewing."""
    safe_name = Path(filename).name
    filepath = EMAILS_DIR / safe_name
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Email not found")
    data = parse_eml_file(filepath)
    content = data.get("html_body") or f"<pre>{data.get('text_body')}</pre>"
    return HTMLResponse(content=content)


@app.get("/api/internal/email/incoming")
async def email_incoming_get(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(content=HTML_DASHBOARD)
    return {
        "status": "ok",
        "message": "Temporary Email Receiver endpoint is active. Send POST with raw MIME body.",
    }


@app.post("/api/internal/email/incoming")
async def receive_email(request: Request):
    try:
        # Read raw email body
        body = await request.body()
        if not body:
            logger.warning("Received request with empty body")
            raise HTTPException(status_code=400, detail="Empty email body received")

        # Read headers forwarded from Worker
        sender = (
            request.headers.get("x-email-from")
            or request.headers.get("from")
            or "Unknown"
        )
        recipient = (
            request.headers.get("x-email-to")
            or request.headers.get("to")
            or "Unknown"
        )
        request_id = (
            request.headers.get("x-request-id")
            or request.headers.get("request-id")
            or None
        )
        raw_email_size = len(body)

        # Print / log required fields
        logger.info("=" * 60)
        logger.info(">>> INCOMING EMAIL RECEIVED <<<")
        logger.info(f"Sender:        {sender}")
        logger.info(f"Recipient:     {recipient}")
        logger.info(f"Request ID:    {request_id if request_id else '(not provided)'}")
        logger.info(f"Raw Size:      {raw_email_size} bytes")

        # Save received raw email as .eml file
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        safe_suffix = uuid.uuid4().hex[:8]
        eml_filename = f"email_{timestamp_str}_{safe_suffix}.eml"
        eml_filepath = EMAILS_DIR / eml_filename

        with open(eml_filepath, "wb") as f:
            f.write(body)

        logger.info(f"Saved EML:     {eml_filepath}")
        logger.info("=" * 60)

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "message": "Raw email successfully received and saved",
                "sender": sender,
                "recipient": recipient,
                "request_id": request_id,
                "size_bytes": raw_email_size,
                "saved_file": eml_filename,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing incoming email: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to process email: {str(e)}")


@app.get("/")
@app.get("/emails")
async def dashboard():
    return HTMLResponse(content=HTML_DASHBOARD)


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Cloudflare Email Worker — Live Receiver</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: #111827;
      --card-border: #1f2937;
      --hover-bg: #1e293b;
      --active-bg: #1e3a5f;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --accent: #3b82f6;
      --accent-glow: rgba(59, 130, 246, 0.2);
      --success: #10b981;
      --orange: #f97316;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }
    header {
      background: var(--card-bg);
      border-bottom: 1px solid var(--card-border);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .logo-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .badge-cf {
      background: linear-gradient(135deg, #f38020, #faad3f);
      color: #fff;
      font-weight: 700;
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 6px;
      letter-spacing: 0.5px;
    }
    h1 {
      font-size: 17px;
      font-weight: 600;
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      background: rgba(16, 185, 129, 0.1);
      color: var(--success);
      padding: 5px 12px;
      border-radius: 20px;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 8px var(--success);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(0.85); }
    }
    .endpoint-pill {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      background: rgba(59, 130, 246, 0.1);
      color: #93c5fd;
      padding: 4px 10px;
      border-radius: 6px;
      border: 1px solid rgba(59, 130, 246, 0.3);
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .endpoint-pill:hover {
      background: rgba(59, 130, 246, 0.2);
    }
    main {
      flex: 1;
      display: grid;
      grid-template-columns: 380px 1fr;
      overflow: hidden;
    }
    /* Left pane: list */
    .inbox-pane {
      background: #0f172a;
      border-right: 1px solid var(--card-border);
      display: flex;
      flex-direction: column;
      height: 100%;
      overflow: hidden;
    }
    .inbox-header {
      padding: 14px 16px;
      border-bottom: 1px solid var(--card-border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .inbox-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.8px;
    }
    .inbox-count {
      background: var(--accent);
      color: white;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 12px;
    }
    .email-list {
      flex: 1;
      overflow-y: auto;
      list-style: none;
    }
    .email-item {
      padding: 14px 16px;
      border-bottom: 1px solid rgba(255,255,255,0.05);
      cursor: pointer;
      transition: background 0.15s ease;
    }
    .email-item:hover {
      background: var(--hover-bg);
    }
    .email-item.active {
      background: var(--active-bg);
      border-left: 3px solid var(--accent);
    }
    .email-item-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 4px;
    }
    .email-from {
      font-weight: 600;
      font-size: 14px;
      color: #fff;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 220px;
    }
    .email-time {
      font-size: 11px;
      color: var(--text-muted);
    }
    .email-subject {
      font-size: 13px;
      color: #e5e7eb;
      font-weight: 500;
      margin-bottom: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .email-snippet {
      font-size: 12px;
      color: var(--text-muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .empty-state {
      padding: 48px 24px;
      text-align: center;
      color: var(--text-muted);
    }
    .empty-icon {
      font-size: 36px;
      margin-bottom: 12px;
    }
    /* Right pane: email viewer */
    .viewer-pane {
      background: var(--card-bg);
      display: flex;
      flex-direction: column;
      height: 100%;
      overflow: hidden;
    }
    .viewer-empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100%;
      color: var(--text-muted);
      gap: 12px;
    }
    .viewer-content {
      display: flex;
      flex-direction: column;
      height: 100%;
      overflow: hidden;
    }
    .viewer-header {
      padding: 20px 28px;
      border-bottom: 1px solid var(--card-border);
      background: #131c2e;
    }
    .viewer-subject {
      font-size: 20px;
      font-weight: 700;
      color: #fff;
      margin-bottom: 12px;
    }
    .viewer-meta-grid {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 8px 16px;
      font-size: 13px;
    }
    .meta-label {
      color: var(--text-muted);
      font-weight: 500;
    }
    .meta-value {
      color: #e2e8f0;
      word-break: break-all;
    }
    .viewer-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid rgba(255,255,255,0.06);
    }
    .tab-btn {
      background: transparent;
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }
    .tab-btn.active {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }
    .action-btn {
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-left: auto;
      transition: background 0.2s;
    }
    .action-btn:hover {
      background: rgba(255,255,255,0.12);
    }
    /* Tab Contents */
    .viewer-body {
      flex: 1;
      overflow: hidden;
      position: relative;
    }
    .tab-panel {
      display: none;
      width: 100%;
      height: 100%;
      overflow-y: auto;
      padding: 24px 28px;
    }
    .tab-panel.active {
      display: block;
    }
    #panel-html iframe {
      width: 100%;
      height: 100%;
      border: none;
      background: white;
      border-radius: 8px;
    }
    #panel-text pre {
      white-space: pre-wrap;
      font-family: inherit;
      font-size: 14px;
      line-height: 1.6;
      color: #e5e7eb;
    }
    #panel-headers table {
      width: 100%;
      border-collapse: collapse;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
    }
    #panel-headers th, #panel-headers td {
      padding: 8px 12px;
      text-align: left;
      border-bottom: 1px solid var(--card-border);
    }
    #panel-headers th {
      color: var(--accent);
      width: 200px;
    }
    #panel-headers td {
      color: #cbd5e1;
      word-break: break-all;
    }
  </style>
</head>
<body>
  <header>
    <div class="logo-group">
      <span class="badge-cf">CLOUDFLARE</span>
      <h1>Email Worker Receiver</h1>
    </div>
    <div style="display:flex; align-items:center; gap:16px;">
      <div class="endpoint-pill" onclick="copyEndpoint()" title="Click to copy incoming webhook URL">
        <span>POST /api/internal/email/incoming</span>
        <span id="copy-text" style="color:var(--text-muted); font-size:11px;">(copy)</span>
      </div>
      <div class="status-badge">
        <div class="status-dot"></div>
        <span>Live Receiver Active</span>
      </div>
    </div>
  </header>

  <main>
    <!-- Left column: Email List -->
    <aside class="inbox-pane">
      <div class="inbox-header">
        <span class="inbox-title">Received Emails</span>
        <span class="inbox-count" id="inbox-count">0</span>
      </div>
      <ul class="email-list" id="email-list">
        <div class="empty-state">
          <div class="empty-icon">📬</div>
          <p>Listening for real emails...</p>
        </div>
      </ul>
    </aside>

    <!-- Right column: Email Viewer -->
    <section class="viewer-pane">
      <div class="viewer-empty" id="viewer-empty">
        <div style="font-size:48px;">📨</div>
        <p>Select an email from the list to view its contents.</p>
      </div>

      <div class="viewer-content" id="viewer-content" style="display:none;">
        <div class="viewer-header">
          <div class="viewer-subject" id="view-subject">Subject</div>
          <div class="viewer-meta-grid">
            <span class="meta-label">From:</span>
            <span class="meta-value" id="view-from"></span>
            <span class="meta-label">To:</span>
            <span class="meta-value" id="view-to"></span>
            <span class="meta-label">Date:</span>
            <span class="meta-value" id="view-date"></span>
            <span class="meta-label">Size:</span>
            <span class="meta-value" id="view-size"></span>
          </div>

          <div class="viewer-actions">
            <button class="tab-btn active" onclick="switchTab('html')">HTML View</button>
            <button class="tab-btn" onclick="switchTab('text')">Plain Text</button>
            <button class="tab-btn" onclick="switchTab('headers')">Headers</button>
            <a class="action-btn" id="btn-download" href="#" target="_blank" download>
              ⬇️ Download .eml
            </a>
          </div>
        </div>

        <div class="viewer-body">
          <div class="tab-panel active" id="panel-html" style="padding:12px;">
            <iframe id="html-iframe" sandbox="allow-same-origin"></iframe>
          </div>
          <div class="tab-panel" id="panel-text">
            <pre id="text-content"></pre>
          </div>
          <div class="tab-panel" id="panel-headers">
            <table>
              <tbody id="headers-tbody"></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  </main>

  <script>
    let currentSelectedFilename = null;
    let knownFiles = new Set();

    function copyEndpoint() {
      const url = window.location.origin + "/api/internal/email/incoming";
      navigator.clipboard.writeText(url).then(() => {
        const text = document.getElementById("copy-text");
        text.innerText = "(copied!)";
        setTimeout(() => text.innerText = "(copy)", 2000);
      });
    }

    async function fetchEmails() {
      try {
        const res = await fetch("/api/emails");
        if (!res.ok) return;
        const data = await res.json();
        renderEmailList(data.emails || []);
      } catch (err) {
        console.error("Failed to poll emails:", err);
      }
    }

    function renderEmailList(emails) {
      const listEl = document.getElementById("email-list");
      const countEl = document.getElementById("inbox-count");
      countEl.innerText = emails.length;

      if (emails.length === 0) {
        listEl.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">📬</div>
            <p>Waiting for incoming emails...</p>
          </div>
        `;
        return;
      }

      listEl.innerHTML = "";
      emails.forEach(item => {
        const li = document.createElement("li");
        li.className = "email-item" + (item.filename === currentSelectedFilename ? " active" : "");
        li.onclick = () => selectEmail(item.filename);

        const timeStr = item.date ? item.date.split(" ").slice(0, 4).join(" ") : new Date(item.created_at * 1000).toLocaleTimeString();

        li.innerHTML = `
          <div class="email-item-header">
            <span class="email-from" title="${escapeHtml(item.from)}">${escapeHtml(item.from)}</span>
            <span class="email-time">${timeStr}</span>
          </div>
          <div class="email-subject">${escapeHtml(item.subject || "(No Subject)")}</div>
          <div class="email-snippet">${escapeHtml(item.snippet || "(Empty body)")}</div>
        `;
        listEl.appendChild(li);
      });

      // Auto-select latest if none selected
      if (!currentSelectedFilename && emails.length > 0) {
        selectEmail(emails[0].filename);
      }
    }

    async function selectEmail(filename) {
      currentSelectedFilename = filename;
      document.querySelectorAll(".email-item").forEach(el => el.classList.remove("active"));
      
      try {
        const res = await fetch("/api/emails/" + encodeURIComponent(filename));
        if (!res.ok) return;
        const emailData = await res.json();
        
        document.getElementById("viewer-empty").style.display = "none";
        document.getElementById("viewer-content").style.display = "flex";

        document.getElementById("view-subject").innerText = emailData.subject || "(No Subject)";
        document.getElementById("view-from").innerText = emailData.from;
        document.getElementById("view-to").innerText = emailData.to;
        document.getElementById("view-date").innerText = emailData.date || new Date(emailData.created_at * 1000).toLocaleString();
        document.getElementById("view-size").innerText = (emailData.size_bytes / 1024).toFixed(1) + " KB";

        document.getElementById("btn-download").href = "/api/emails/" + encodeURIComponent(filename) + "/raw";

        // HTML Panel
        const iframe = document.getElementById("html-iframe");
        if (emailData.has_html) {
          iframe.src = "/api/emails/" + encodeURIComponent(filename) + "/html";
        } else {
          iframe.srcdoc = `<html><body style="font-family:sans-serif; padding:16px; white-space:pre-wrap;">${escapeHtml(emailData.text_body || "(No message body)")}</body></html>`;
        }

        // Text Panel
        document.getElementById("text-content").innerText = emailData.text_body || emailData.html_body || "(No plain text version available)";

        // Headers Panel
        const tbody = document.getElementById("headers-tbody");
        tbody.innerHTML = "";
        for (const [k, v] of Object.entries(emailData.headers || {})) {
          const row = document.createElement("tr");
          row.innerHTML = `<th>${escapeHtml(k)}</th><td>${escapeHtml(v)}</td>`;
          tbody.appendChild(row);
        }

        // Re-highlight active in list
        document.querySelectorAll(".email-item").forEach(el => {
          if (el.innerText.includes(emailData.from)) {
            el.classList.add("active");
          }
        });
      } catch (err) {
        console.error("Failed to load email details:", err);
      }
    }

    function switchTab(tabName) {
      document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));

      if (tabName === 'html') {
        document.querySelector("button[onclick=\\"switchTab('html')\\"]").classList.add("active");
        document.getElementById("panel-html").classList.add("active");
      } else if (tabName === 'text') {
        document.querySelector("button[onclick=\\"switchTab('text')\\"]").classList.add("active");
        document.getElementById("panel-text").classList.add("active");
      } else if (tabName === 'headers') {
        document.querySelector("button[onclick=\\"switchTab('headers')\\"]").classList.add("active");
        document.getElementById("panel-headers").classList.add("active");
      }
    }

    function escapeHtml(str) {
      if (!str) return "";
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    // Initial load and live polling every 3 seconds
    fetchEmails();
    setInterval(fetchEmails, 3000);
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Temporary Email Receiver with HTML dashboard on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
