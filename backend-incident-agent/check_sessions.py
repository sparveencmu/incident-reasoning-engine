import asyncio
from app.agent_runtime_app import agent_runtime

async def main():
    agent_runtime.set_up()
    print("Listing sessions...")
    resp = await agent_runtime.session_service.list_sessions(app_name="app")
    for s in resp.sessions:
        print(f"Session: {s.id}")
        full = await agent_runtime.session_service.get_session(app_name="app", user_id=s.user_id, session_id=s.id)
        for idx, ev in enumerate(full.events):
            print(f"  Event {idx}: {ev.model_dump()}")
        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
