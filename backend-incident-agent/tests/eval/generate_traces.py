import json
import os
from pathlib import Path
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from app.agent import root_agent

# Ensure directories exist
Path("artifacts/traces").mkdir(parents=True, exist_ok=True)

# Load dataset
dataset_path = Path("tests/eval/datasets/basic-dataset.json")
with open(dataset_path, "r") as f:
    dataset = json.load(f)

eval_cases = dataset["eval_cases"]
generated_cases = []

for case in eval_cases:
    case_id = case["eval_case_id"]
    print(f"Running case: {case_id}")
    
    prompt_text = case["prompt"]["parts"][0]["text"]
    
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="test_user", app_name="backend_incident_agent")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="backend_incident_agent")
    
    # Run Turn 0
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=prompt_text)]
    )
    
    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
        )
    )
    
    # Check if we suspended (is there an adk_request_input tool call?)
    suspended = False
    for event in events:
        if (
            event.content
            and event.content.parts
            and event.content.parts[0].function_call
            and event.content.parts[0].function_call.name == "adk_request_input"
        ):
            suspended = True
            break
            
    if suspended:
        # Automate SRE approval decision based on case_id
        if "reject" in case_id or "injection" in case_id:
            decision = "reject"
        else:
            decision = "approve"
            
        print(f"  Suspended for review. Automating decision: {decision}")
        
        # Resume Turn 1
        resume_part = types.Part(
            function_response=types.FunctionResponse(
                name="adk_request_input",
                id="approve_reject",
                response={"approve_reject": decision}
            )
        )
        resume_message = types.Content(
            role="user",
            parts=[resume_part]
        )
        
        list(
            runner.run(
                new_message=resume_message,
                user_id="test_user",
                session_id=session.id,
            )
        )
        
    # Retrieve all events from the session
    sess = session_service.get_session_sync(user_id="test_user", app_name="backend_incident_agent", session_id=session.id)
    
    # Format and group events into turns
    def format_event(e):
        d = e.model_dump(mode="json", exclude_unset=True)
        if "content" in d and d["content"] and "parts" in d["content"]:
            for part in d["content"]["parts"]:
                part.pop("thought_signature", None)
        res = {
            "author": d.get("author"),
            "content": d.get("content")
        }
        return res
        
    turns = []
    current_turn_events = []
    turn_index = 0
    
    for e in sess.events:
        if e.author == "user" or (e.content and e.content.role == "user"):
            if current_turn_events:
                turns.append({
                    "turn_index": turn_index,
                    "turn_id": f"turn_{turn_index}",
                    "events": current_turn_events
                })
                turn_index += 1
                current_turn_events = []
        current_turn_events.append(format_event(e))
        
    if current_turn_events:
        turns.append({
            "turn_index": turn_index,
            "turn_id": f"turn_{turn_index}",
            "events": current_turn_events
        })
        
    # Extract final text response
    final_response = None
    for e in reversed(sess.events):
        if e.author != "user" and e.author != "tool" and e.content:
            parts = e.content.parts or []
            texts = [p.text for p in parts if p.text]
            if texts:
                final_response = {
                    "role": e.content.role or "model",
                    "parts": [{"text": "".join(texts)}]
                }
                break
                
    case_trace = {
        "eval_case_id": case_id,
        "prompt": case["prompt"],
        "agent_data": {
            "agents": {
                "root_agent": {
                    "agent_id": "root_agent",
                    "instruction": "Backend Incident SRE Investigation Workflow"
                }
            },
            "turns": turns
        }
    }
    if final_response:
        case_trace["responses"] = [{"response": final_response}]
        
    generated_cases.append(case_trace)

# Write output JSON
output_data = {"eval_cases": generated_cases}
output_path = Path("artifacts/traces/generated_traces.json")
output_path.parent.mkdir(parents=True, exist_ok=True)
with open(output_path, "w") as f:
    json.dump(output_data, f, indent=2)

print(f"All traces generated and saved to {output_path}")
