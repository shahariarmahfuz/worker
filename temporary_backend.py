import os
import sys
import time
import uuid
import logging
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("temporary_backend")

app = FastAPI(title="Temporary Email Receiver", version="1.0.0")

# Directory to save .eml files
EMAILS_DIR = Path(__file__).resolve().parent / "received_emails"
EMAILS_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "Temporary Email Receiver",
        "timestamp": time.time(),
    }

@app.get("/api/internal/email/incoming")
async def email_incoming_get():
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Temporary Email Receiver on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
