import os
import json
import logging
import asyncio
import requests
from typing import Any, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("sre-dashboard")

app = FastAPI(title="SRE Incident Manager Dashboard")

# Read configurations from environment variables
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or "ambient-expense-agent-501222"
AGENT_RUNTIME_ID = os.environ.get("AGENT_RUNTIME_ID")
CHAT_WEBHOOK_URL = os.environ.get("CHAT_WEBHOOK_URL", "https://httpbin.org/post")

# Dynamically initialize Vertex AI session service if AGENT_RUNTIME_ID is present
session_service = None
remote_agent = None

if AGENT_RUNTIME_ID:
    try:
        import vertexai
        from vertexai.preview import reasoning_engines
        from google.adk.sessions import VertexAiSessionService
        
        # Parse location and engine ID
        location = "us-east1"
        if "locations/" in AGENT_RUNTIME_ID:
            parts = AGENT_RUNTIME_ID.split("/")
            try:
                idx = parts.index("locations")
                location = parts[idx + 1]
            except Exception:
                pass
                
        engine_id = AGENT_RUNTIME_ID.split("/")[-1] if "/" in AGENT_RUNTIME_ID else AGENT_RUNTIME_ID
        
        vertexai.init(project=PROJECT, location=location)
        session_service = VertexAiSessionService(
            project=PROJECT,
            location=location,
            agent_engine_id=engine_id
        )
        remote_agent = reasoning_engines.ReasoningEngine(AGENT_RUNTIME_ID)
        logger.info(f"Initialized Cloud VertexAiSessionService for runtime ID: {AGENT_RUNTIME_ID}")
    except Exception as e:
        logger.error(f"Failed to initialize Vertex AI Session Service: {e}. Falling back to local Mock Mode.")

from google.cloud import firestore

firestore_db = None
try:
    firestore_db = firestore.Client(project=PROJECT, database="(default)")
    logger.info("Initialized Firestore Client.")
except Exception as e:
    logger.error(f"Failed to initialize Firestore: {e}")


class ActionRequest(BaseModel):
    approved: bool
    interrupt_id: str
    user_id: str


def post_to_google_chat(text: str) -> None:
    """Send incident notification to Google Chat via Webhook."""
    if not CHAT_WEBHOOK_URL:
        logger.info(f"[Google Chat Mock Log]\n{text}")
        return
        
    try:
        payload = {"text": text}
        response = requests.post(
            CHAT_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if response.status_code == 200:
            logger.info("Successfully posted notification to Google Chat space")
        else:
            logger.error(f"Failed to post to Google Chat. Status: {response.status_code}, Response: {response.text}")
    except Exception as e:
        logger.error(f"Google Chat webhook invocation error: {e}")


@app.post("/api/trigger/pubsub")
async def trigger_pubsub(request: Request):
    """Receives Pub/Sub push messages, decodes them, and queries the Reasoning Engine."""
    try:
        body = await request.json()
        logger.info(f"Received Pub/Sub push body: {body}")
        
        # Check if message is wrapped in Pub/Sub envelope
        message = body.get("message", {})
        data_b64 = message.get("data")
        
        if data_b64:
            import base64
            decoded = base64.b64decode(data_b64).decode("utf-8")
            try:
                incident_data = json.loads(decoded)
            except Exception:
                logger.warning(f"Direct JSON parsing failed, attempting cleanup of unescaped quotes: {decoded}")
                try:
                    # Clean common quote issue: "{"input": {"message": "{"service": ..."}"}}"
                    cleaned = decoded.replace('"message": "{', '"message": {').replace('}"}', '}}')
                    incident_data = json.loads(cleaned)
                except Exception:
                    # Final fallback: treat as raw text
                    incident_data = {"error_log": decoded, "service": "unknown", "severity": "HIGH", "alert_name": "Alert"}
        else:
            # If unwrapped
            incident_data = body

        logger.info(f"Decoded incident data: {incident_data}")
        
        # Defensively unwrap if {"input": {"message": ...}}
        if isinstance(incident_data, dict) and "input" in incident_data:
            inner_msg = incident_data["input"].get("message")
            if isinstance(inner_msg, dict):
                incident_data = inner_msg
            elif isinstance(inner_msg, str):
                try:
                    incident_data = json.loads(inner_msg)
                except Exception:
                    incident_data = {"error_log": inner_msg, "service": "unknown", "severity": "HIGH", "alert_name": "Alert"}
        
        # Generate a unique session ID based on the service and a UUID
        import uuid
        service = incident_data.get("service", "unknown")
        # Clean service name for session ID
        clean_service = "".join(c if c.isalnum() or c == "-" else "_" for c in service)
        session_id = f"session-{clean_service}-{uuid.uuid4().hex[:8]}"
        user_id = "default-user"
        
        if not remote_agent:
            # Mock mode: create a mock pending request
            logger.info("Local Mock Mode: adding mock session")
            if firestore_db:
                firestore_db.collection("incidents").document(session_id).set({
                    "session_id": session_id,
                    "user_id": user_id,
                    "interrupt_id": "approve_reject",
                    "incident": incident_data,
                    "redacted_types": ["SSN"] if "000-12" in str(incident_data) else [],
                    "compliance_review": f"### SRE Investigation Report (Mock)\n\nAlert on service `{service}`. Suspended for review.",
                    "timestamp": "2026-07-03T18:00:00Z"
                })
            return {"status": "mock_received", "session_id": session_id}
            
        # Call the remote agent query in executor to avoid blocking event loop
        def run_query():
            req_payload = {
                "name": AGENT_RUNTIME_ID,
                "input": {
                    "user_id": user_id,
                    "message": json.dumps(incident_data)
                },
                "class_method": "async_stream_query"
            }
            stream = remote_agent.execution_api_client.stream_query_reasoning_engine(request=req_payload)
            return list(stream)
            
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, run_query)
        logger.info(f"Reasoning Engine response for session {session_id}: {response}")
        
        return {"status": "success", "session_id": session_id, "response": str(response)}
        
    except Exception as e:
        logger.error(f"Error handling Pub/Sub trigger: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pending")
async def get_pending():
    """List all sessions, fetch history, identify unresolved adk_request_input calls."""
    if not session_service:
        # Return mock data from Firestore
        if firestore_db:
            docs = firestore_db.collection("incidents").stream()
            return [doc.to_dict() for doc in docs]
        return []

    try:
        list_resp = await session_service.list_sessions(app_name="app")
        pending_approvals = []
        
        for s in list_resp.sessions:
            # Fetch the full session to load the complete event history
            full_session = await session_service.get_session(
                app_name="app",
                user_id=s.user_id,
                session_id=s.id
            )
            if not full_session:
                continue
                
            calls = {}
            resolved = set()
            for event in full_session.events:
                # Track request input calls
                for fc in event.get_function_calls():
                    if fc.name == "adk_request_input":
                        calls[fc.id] = fc
                # Track request input responses
                for fr in event.get_function_responses():
                    if fr.name == "adk_request_input":
                        resolved.add(fr.id)
                        
            # Identify unresolved ones
            for interrupt_id, fc in calls.items():
                if interrupt_id not in resolved:
                    # Find compliance review text from the investigator SRE agent
                    compliance_review = "No SRE investigator analysis available."
                    for event in full_session.events:
                        if event.author == "investigator_agent" and event.content and event.content.parts:
                            # Capture the final LLM summary
                            compliance_review = event.content.parts[0].text or compliance_review
                    
                    incident = full_session.state.get("incident") or {}
                    redacted_types = full_session.state.get("redacted_types") or []
                    
                    pending_approvals.append({
                        "session_id": s.id,
                        "user_id": s.user_id,
                        "interrupt_id": interrupt_id,
                        "incident": incident,
                        "redacted_types": redacted_types,
                        "compliance_review": compliance_review,
                        "timestamp": s.last_update_time
                    })
                    
        return pending_approvals
    except Exception as e:
        logger.error(f"Error fetching pending sessions from Vertex AI: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stream/pending")
async def stream_pending(request: Request):
    """Server-Sent Events endpoint for real-time pending approvals."""
    async def event_generator():
        while True:
            try:
                data = await get_pending()
                yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                logger.error(f"SSE Error: {e}")
            await asyncio.sleep(5)
    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/action/{session_id}")
async def take_action(session_id: str, req: ActionRequest):
    """Resume a paused session by sending the approved/rejected decision."""
    decision_text = "APPROVED" if req.approved else "DECLINED"
    
    # 1. Handle Google Chat notifications
    # Retrieve incident details from local mock sessions or build from request details
    service_name = "database-primary"
    alert_name = "Primary DB deadlock"
    
    if not session_service:
        # Find details from mock session
        if firestore_db:
            doc_ref = firestore_db.collection("incidents").document(session_id)
            doc = doc_ref.get()
            if doc.exists:
                s = doc.to_dict()
                service_name = s.get("incident", {}).get("service", service_name)
                alert_name = s.get("incident", {}).get("alert_name", alert_name)
                doc_ref.delete()
        
        chat_msg = (
            f"🟢 **SRE Incident Dashboard Action**\n\n"
            f"*   **Decision**: `{decision_text}`\n"
            f"*   **Service**: `{service_name}`\n"
            f"*   **Alert**: `{alert_name}`\n"
            f"*   **Session**: `{session_id}`\n\n"
            f"Mitigation execution triggered successfully."
        )
        post_to_google_chat(chat_msg)
        return {"status": "success", "mode": "mock"}

    try:
        # Fetch the session state first to find incident details for notifications
        full_session = await session_service.get_session(
            app_name="app",
            user_id=req.user_id,
            session_id=session_id
        )
        if full_session:
            incident = full_session.state.get("incident") or {}
            service_name = incident.get("service", service_name)
            alert_name = incident.get("alert_name", alert_name)

        # Create resume payload matching the requested structure
        resume_payload = {
            "role": "user",
            "parts": [{
                "function_response": {
                    "id": req.interrupt_id,
                    "name": "adk_request_input",
                    "response": {
                        "approved": req.approved,
                        "approve_reject": "approve" if req.approved else "reject"
                    }
                }
            }]
        }
        
        # Build request dict to avoid protobuf package mismatches
        request = {
            "name": AGENT_RUNTIME_ID,
            "input": {
                "user_id": req.user_id,
                "session_id": session_id,
                "message": resume_payload
            },
            "class_method": "async_stream_query"
        }
        
        # Run blocking stream query call in executor and consume the stream to force execution
        def run_stream():
            stream = remote_agent.execution_api_client.stream_query_reasoning_engine(request=request)
            return list(stream)
            
        loop = asyncio.get_running_loop()
        responses = await loop.run_in_executor(None, run_stream)
        
        # Send post-action Google Chat message
        chat_msg = (
            f"🟢 **SRE Incident Dashboard Action**\n\n"
            f"*   **Decision**: `{decision_text}`\n"
            f"*   **Service**: `{service_name}`\n"
            f"*   **Alert**: `{alert_name}`\n"
            f"*   **Session**: `{session_id}`\n\n"
            f"Mitigation execution triggered successfully."
        )
        post_to_google_chat(chat_msg)
        
        return {"status": "success", "response": str(responses)}
    except Exception as e:
        logger.error(f"Error resuming session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the premium manager dashboard HTML page."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Premium SRE incident review and approval manager dashboard.">
    <title>SRE Incident Approval Dashboard</title>
    
    <!-- Google Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    
    <!-- CSS Styles -->
    <style>
        :root {
            --bg-color: #070a13;
            --card-bg: rgba(255, 255, 255, 0.02);
            --card-border: rgba(255, 255, 255, 0.07);
            --card-hover-border: rgba(255, 255, 255, 0.15);
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --primary-glow: rgba(99, 102, 241, 0.15);
            --accent-glow: rgba(168, 85, 247, 0.15);
            --approve-color: #10b981;
            --approve-gradient: linear-gradient(135deg, #10b981, #059669);
            --reject-color: #f43f5e;
            --reject-gradient: linear-gradient(135deg, #f43f5e, #e11d48);
            --view-gradient: linear-gradient(135deg, #6366f1, #4f46e5);
            --font-family: 'Outfit', 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            font-family: var(--font-family);
            min-height: 100vh;
            overflow-x: hidden;
            position: relative;
            padding: 2rem;
        }

        /* Ambient background glow points */
        body::before, body::after {
            content: '';
            position: absolute;
            width: 400px;
            height: 400px;
            border-radius: 50%;
            z-index: -1;
            filter: blur(120px);
            opacity: 0.5;
        }

        body::before {
            background: radial-gradient(circle, var(--primary-glow) 0%, transparent 70%);
            top: -10%;
            left: -10%;
        }

        body::after {
            background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%);
            bottom: -10%;
            right: -10%;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            position: relative;
            z-index: 1;
        }

        /* Header section */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 3rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }

        .logo-group {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-icon {
            width: 2.5rem;
            height: 2.5rem;
            background: linear-gradient(135deg, #6366f1, #a855f7);
            border-radius: 0.5rem;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 1.25rem;
            box-shadow: 0 0 20px rgba(99, 102, 241, 0.4);
        }

        .logo-text h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.025em;
        }

        .logo-text p {
            color: var(--text-secondary);
            font-size: 0.85rem;
        }

        .controls {
            display: flex;
            gap: 1rem;
            align-items: center;
        }

        /* Buttons */
        .btn-refresh {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: var(--text-primary);
            padding: 0.6rem 1.2rem;
            border-radius: 0.5rem;
            font-family: var(--font-family);
            font-weight: 500;
            font-size: 0.9rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            transition: all 0.2s ease;
        }

        .btn-refresh:hover {
            background: rgba(255, 255, 255, 0.1);
            border-color: rgba(255, 255, 255, 0.2);
        }

        /* Auto-refresh toggle switch */
        .toggle-container {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .switch {
            position: relative;
            display: inline-block;
            width: 2.4rem;
            height: 1.3rem;
        }

        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }

        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: rgba(255, 255, 255, 0.1);
            transition: .3s;
            border-radius: 1rem;
        }

        .slider:before {
            position: absolute;
            content: "";
            height: 0.9rem;
            width: 0.9rem;
            left: 0.2rem;
            bottom: 0.2rem;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }

        input:checked + .slider {
            background-color: #6366f1;
        }

        input:checked + .slider:before {
            transform: translateX(1.1rem);
        }

        /* Cards Grid */
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 2rem;
            align-items: start;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 1rem;
            padding: 1.75rem;
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
            backdrop-filter: blur(12px);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }

        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, transparent, rgba(99, 102, 241, 0.3), transparent);
            opacity: 0;
            transition: opacity 0.3s;
        }

        .card:hover {
            transform: translateY(-5px);
            border-color: var(--card-hover-border);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3), 0 0 20px rgba(99, 102, 241, 0.05);
        }

        .card:hover::before {
            opacity: 1;
        }

        /* Header items on card */
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
        }

        .card-title-group h3 {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 0.25rem;
        }

        .card-title-group p {
            font-size: 0.8rem;
            color: var(--text-secondary);
        }

        .badge {
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            padding: 0.25rem 0.6rem;
            border-radius: 1rem;
            letter-spacing: 0.05em;
        }

        .badge-critical {
            background-color: rgba(244, 63, 94, 0.1);
            color: var(--reject-color);
            border: 1px solid rgba(244, 63, 94, 0.2);
        }

        .badge-high {
            background-color: rgba(245, 158, 11, 0.1);
            color: #f59e0b;
            border: 1px solid rgba(245, 158, 11, 0.2);
        }

        /* Card payload section */
        .payload-info {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            background: rgba(255, 255, 255, 0.015);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 0.5rem;
            padding: 1rem;
        }

        .info-row {
            display: flex;
            justify-content: space-between;
            font-size: 0.85rem;
        }

        .info-label {
            color: var(--text-secondary);
        }

        .info-value {
            font-weight: 500;
            max-width: 65%;
            text-overflow: ellipsis;
            white-space: nowrap;
            overflow: hidden;
        }

        .redact-badge {
            background: rgba(168, 85, 247, 0.15);
            color: #c084fc;
            padding: 0.1rem 0.4rem;
            border-radius: 0.25rem;
            font-size: 0.7rem;
            font-weight: 600;
            border: 1px solid rgba(168, 85, 247, 0.3);
            display: inline-block;
            margin-right: 0.25rem;
        }

        /* Card Action Buttons */
        .card-actions {
            display: flex;
            gap: 1rem;
            margin-top: auto;
        }

        .btn {
            flex: 1;
            padding: 0.75rem;
            border-radius: 0.5rem;
            font-family: var(--font-family);
            font-weight: 600;
            font-size: 0.85rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
        }

        .btn-approve {
            background: var(--approve-gradient);
            border: none;
            color: white;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
        }

        .btn-approve:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.4);
        }

        .btn-reject {
            background: var(--reject-gradient);
            border: none;
            color: white;
            box-shadow: 0 4px 12px rgba(244, 63, 148, 0.2);
        }

        .btn-reject:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(244, 63, 148, 0.4);
        }

        .btn-view {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: var(--text-primary);
        }

        .btn-view:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.2);
        }

        /* Loading Spinner */
        .spinner {
            width: 1.1rem;
            height: 1.1rem;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-radius: 50%;
            border-top-color: white;
            animation: spin 0.8s linear infinite;
            display: none;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Empty state styling */
        .empty-state {
            grid-column: 1 / -1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 5rem 2rem;
            background: var(--card-bg);
            border: 1px dashed var(--card-border);
            border-radius: 1rem;
            text-align: center;
            gap: 1.5rem;
            backdrop-filter: blur(12px);
        }

        .empty-icon {
            font-size: 3rem;
            background: rgba(255, 255, 255, 0.03);
            width: 5rem;
            height: 5rem;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            border: 1px solid rgba(255, 255, 255, 0.08);
        }

        .empty-state h3 {
            font-size: 1.5rem;
            font-weight: 600;
        }

        .empty-state p {
            color: var(--text-secondary);
            font-size: 0.95rem;
            max-width: 400px;
        }

        /* Slide-out Drawer Modal */
        .drawer-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(7, 10, 19, 0.8);
            backdrop-filter: blur(8px);
            z-index: 1000;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.3s ease;
        }

        .drawer-overlay.active {
            opacity: 1;
            pointer-events: auto;
        }

        .drawer {
            position: fixed;
            top: 0;
            right: 0;
            bottom: 0;
            width: 100%;
            max-width: 550px;
            background: #0b0f19;
            border-left: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: -10px 0 30px rgba(0, 0, 0, 0.5);
            z-index: 1001;
            transform: translateX(100%);
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            flex-direction: column;
            padding: 2.5rem;
            gap: 2rem;
            overflow-y: auto;
        }

        .drawer.active {
            transform: translateX(0);
        }

        .drawer-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 1rem;
        }

        .drawer-header h2 {
            font-size: 1.5rem;
            font-weight: 700;
        }

        .btn-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
            transition: color 0.2s;
        }

        .btn-close:hover {
            color: var(--text-primary);
        }

        .drawer-section h4 {
            font-size: 0.9rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.75rem;
        }

        .analysis-text {
            background: rgba(255, 255, 255, 0.015);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 0.5rem;
            padding: 1.25rem;
            font-size: 0.95rem;
            line-height: 1.6;
            color: #e2e8f0;
            white-space: pre-wrap;
        }

        .log-box {
            background: #030712;
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 0.5rem;
            padding: 1rem;
            font-family: monospace;
            font-size: 0.85rem;
            color: #f3f4f6;
            word-break: break-all;
            max-height: 200px;
            overflow-y: auto;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-group">
                <div class="logo-icon">🛠️</div>
                <div class="logo-text">
                    <h1>SRE Incident Dashboard</h1>
                    
                </div>
            </div>
            <div class="controls">
                    <div class="header-actions">
                        <div style="display: flex; align-items: center; gap: 0.5rem; background: rgba(16, 185, 129, 0.1); padding: 0.25rem 0.75rem; border-radius: 1rem; border: 1px solid rgba(16, 185, 129, 0.2);">
                            <div style="width: 8px; height: 8px; background: var(--success-color); border-radius: 50%; box-shadow: 0 0 8px var(--success-color); animation: pulse 2s infinite;"></div>
                            <span style="font-size: 0.75rem; color: var(--success-color); font-weight: 500; letter-spacing: 0.5px;">LIVE FEED</span>
                        </div>
                    </div>
                <button class="btn-refresh" onclick="fetchPendingApprovals()">
                    <span id="refreshIcon">🔄</span> Refresh
                </button>
            </div>
        </header>

        <main>
            <div class="dashboard-grid" id="dashboardGrid">
                <!-- Cards will be dynamically rendered here -->
                <div class="empty-state">
                    <div class="empty-icon">⏳</div>
                    <h3>Connecting to services...</h3>
                    <p>Fetching active incident review sessions.</p>
                </div>
            </div>
        </main>
    </div>

    <!-- Side Drawer Modal -->
    <div class="drawer-overlay" id="drawerOverlay" onclick="closeDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <h2 id="drawerTitle">Incident Details</h2>
            <button class="btn-close" onclick="closeDrawer()">&times;</button>
        </div>
        
        <div class="drawer-section">
            <h4>Redacted Event Payload</h4>
            <div class="payload-info">
                <div class="info-row">
                    <span class="info-label">Service</span>
                    <span class="info-value" id="drawerService">-</span>
                </div>
                <div class="info-row">
                    <span class="info-label">Severity</span>
                    <span class="info-value" id="drawerSeverity">-</span>
                </div>
                <div class="info-row">
                    <span class="info-label">Alert Name</span>
                    <span class="info-value" id="drawerAlertName">-</span>
                </div>
                <div class="info-row" id="drawerRedactedRow">
                    <span class="info-label">Redacted Fields</span>
                    <span class="info-value" id="drawerRedacted">-</span>
                </div>
            </div>
        </div>

        <div class="drawer-section">
            <h4>Incident Log Summary</h4>
            <div class="log-box" id="drawerLog">-</div>
        </div>

        <div class="drawer-section">
            <h4>Agent Investigation Report</h4>
            <div class="analysis-text" id="drawerAnalysis">-</div>
        </div>
    </div>

    <!-- JavaScript Actions -->
    <script>
        let eventSource = null;
        let activeApprovals = [];

        function connectSSE() {
            const refreshIcon = document.getElementById("refreshIcon");
            if (refreshIcon) refreshIcon.style.animation = "spin 1s linear infinite";

            if (eventSource) {
                eventSource.close();
            }

            eventSource = new EventSource("/api/stream/pending");
            
            eventSource.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    activeApprovals = data;
                    renderDashboard(activeApprovals);
                } catch (err) {
                    console.error("Error parsing SSE data:", err);
                }
            };
            
            eventSource.onerror = function(err) {
                console.error("SSE connection lost. Browser will auto-reconnect...", err);
                const refreshIcon = document.getElementById("refreshIcon");
                if (refreshIcon) refreshIcon.style.animation = "spin 1s linear infinite";
            };
            
            eventSource.onopen = function() {
                if (refreshIcon) refreshIcon.style.animation = "none";
            };
        }

        function renderDashboard(items) {
            const grid = document.getElementById("dashboardGrid");
            if (!grid) return;

            if (!items || items.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">💡</div>
                        <h3>All Systems Operational</h3>
                        <p>No pending SRE review approvals found at the moment.</p>
                    </div>
                `;
                return;
            }

            grid.innerHTML = items.map((item, idx) => {
                const badgeClass = item.incident.severity === 'CRITICAL' ? 'badge-critical' : 'badge-high';
                const dateStr = new Date(item.timestamp).toLocaleString();
                
                const redactedBadges = (item.redacted_types && item.redacted_types.length > 0)
                    ? item.redacted_types.map(t => `<span class="redact-badge">${t}</span>`).join('')
                    : `<span class="info-label" style="font-size:0.75rem;">None</span>`;

                return `
                    <div class="card" id="card-${item.session_id}">
                        <div class="card-header">
                            <div class="card-title-group">
                                <h3>${item.incident.alert_name || 'Alert'}</h3>
                                <p>Updated: ${dateStr}</p>
                            </div>
                            <span class="badge ${badgeClass}">${item.incident.severity || 'HIGH'}</span>
                        </div>

                        <div class="payload-info">
                            <div class="info-row">
                                <span class="info-label">Service</span>
                                <span class="info-value">${item.incident.service || '-'}</span>
                            </div>
                            <div class="info-row">
                                <span class="info-label">Redacted</span>
                                <div>${redactedBadges}</div>
                            </div>
                            <div class="info-row">
                                <span class="info-label">Session ID</span>
                                <span class="info-value" style="font-size:0.75rem;">${item.session_id}</span>
                            </div>
                        </div>

                        <div class="card-actions">
                            <button class="btn btn-view" onclick="openDrawer(${idx})">View Analysis</button>
                            <button class="btn btn-reject" onclick="actionApproval('${item.session_id}', '${item.interrupt_id}', '${item.user_id}', false, this)">
                                <span class="spinner"></span> Decline
                            </button>
                            <button class="btn btn-approve" onclick="actionApproval('${item.session_id}', '${item.interrupt_id}', '${item.user_id}', true, this)">
                                <span class="spinner"></span> Approve
                            </button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        async function actionApproval(sessionId, interruptId, userId, approved, button) {
            const spinner = button.querySelector(".spinner");
            if (spinner) spinner.style.display = "inline-block";
            button.disabled = true;

            try {
                const response = await fetch(`/api/action/${sessionId}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ approved, interrupt_id: interruptId, user_id: userId })
                });

                if (!response.ok) {
                    const errText = await response.text();
                    throw new Error(errText || "Action failed");
                }

                // Remove card on success
                const card = document.getElementById(`card-${sessionId}`);
                if (card) {
                    card.style.opacity = "0";
                    card.style.transform = "scale(0.9) translateY(10px)";
                    setTimeout(() => {
                        fetchPendingApprovals();
                    }, 300);
                }
            } catch (err) {
                console.error("Action error:", err);
                alert(`Operation failed: ${err.message}`);
                button.disabled = false;
                if (spinner) spinner.style.display = "none";
            }
        }

        function openDrawer(idx) {
            const item = activeApprovals[idx];
            if (!item) return;

            document.getElementById("drawerTitle").innerText = item.incident.alert_name || 'Incident Details';
            document.getElementById("drawerService").innerText = item.incident.service || '-';
            document.getElementById("drawerSeverity").innerText = item.incident.severity || 'HIGH';
            document.getElementById("drawerAlertName").innerText = item.incident.alert_name || '-';
            
            const redactedText = (item.redacted_types && item.redacted_types.length > 0)
                ? item.redacted_types.join(', ')
                : 'None';
            if (redactedText === 'None' || redactedText === '') {
                document.getElementById("drawerRedactedRow").style.display = 'none';
            } else {
                document.getElementById("drawerRedactedRow").style.display = 'flex';
                document.getElementById("drawerRedacted").innerText = redactedText;
            }
            document.getElementById("drawerLog").innerText = item.incident.error_log || '-';
            
            let analysisContent = item.compliance_review || 'No report available.';
            try {
                // Try to parse if it's JSON
                const parsed = JSON.parse(analysisContent);
                if (typeof parsed === 'object' && parsed !== null) {
                    let html = '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem;">';
                    for (const [key, value] of Object.entries(parsed)) {
                        // Skip empty redacted fields
                        if (key.includes('redacted') && (!value || value.toString().toLowerCase() === 'none')) {
                            continue;
                        }
                        
                        let formattedKey = key.replace(/_/g, ' ').replace(/\\b\\w/g, l => l.toUpperCase());
                        if (key === 'scrubbed_sre_log_message') {
                            formattedKey = 'Incident Log Summary';
                        }
                        
                        let formattedValue = value;
                        if (typeof value === 'boolean') {
                            formattedValue = value ? '<span style="color: #10b981; font-weight: bold;">Yes</span>' : '<span style="color: #f43f5e; font-weight: bold;">No</span>';
                        } else if (typeof value === 'number') {
                            formattedValue = `<span style="color: #60a5fa; font-weight: bold;">${value}</span>`;
                        } else if (typeof value === 'string') {
                            // Strip "NOVEL ERROR:" and subsequent instructions
                            let strippedValue = value.replace(/NOVEL ERROR:[\s\S]*?(tool\.?|runbook\.?)/gi, '');
                            // Fallback if the previous regex missed it
                            strippedValue = strippedValue.replace(/NOVEL ERROR:[^\.]*\./gi, '').trim();
                            
                            if (key.includes('mitigation')) {
                                let bulleted = strippedValue.replace(/(\\d+\\.\\s)/g, '<br>&bull; ');
                                if (bulleted.startsWith('<br>')) bulleted = bulleted.substring(4);
                                formattedValue = bulleted.replace(/\\n/g, '<br>');
                            } else {
                                formattedValue = strippedValue.replace(/\\n/g, '<br>');
                            }
                        }
                        
                        let isLong = typeof value === 'string' && value.length > 80;
                        let gridColumnSpan = isLong ? 'grid-column: 1 / -1;' : '';
                        
                        html += `
                            <div style="background: rgba(255, 255, 255, 0.02); padding: 1rem; border-radius: 0.5rem; border: 1px solid rgba(255, 255, 255, 0.05); ${gridColumnSpan}">
                                <div style="color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.5rem;">${formattedKey}</div>
                                <div style="color: #f8fafc; font-size: 0.95rem; line-height: 1.5; word-break: break-word;">${formattedValue}</div>
                            </div>
                        `;
                    }
                    html += '</div>';
                    analysisContent = html;
                } else {
                    analysisContent = `<div style="white-space: pre-wrap;">${analysisContent}</div>`;
                }
            } catch (e) {
                // Not JSON, just render as text
                analysisContent = `<div style="white-space: pre-wrap;">${analysisContent}</div>`;
            }
            document.getElementById("drawerAnalysis").innerHTML = analysisContent;

            document.getElementById("drawerOverlay").classList.add("active");
            document.getElementById("drawer").classList.add("active");
        }

        function closeDrawer() {
            document.getElementById("drawerOverlay").classList.remove("active");
            document.getElementById("drawer").classList.remove("active");
        }

        // Initial Load
        connectSSE();
    </script>
</body>
</html>
"""
    return html_content
