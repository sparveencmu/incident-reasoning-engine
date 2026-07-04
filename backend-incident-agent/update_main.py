import re

with open("frontend/main.py", "r") as f:
    content = f.read()

# 1. Replace MOCK_SESSIONS = [...] with firestore init
mock_sessions_pattern = re.compile(r'# In-memory mock sessions for local dev and testing\nMOCK_SESSIONS = \[\n(?:.*\n){28}\]\n', re.MULTILINE)

firestore_init = """from google.cloud import firestore

firestore_db = None
try:
    firestore_db = firestore.Client(project=PROJECT, database="(default)")
    logger.info("Initialized Firestore Client.")
except Exception as e:
    logger.error(f"Failed to initialize Firestore: {e}")
"""
content = mock_sessions_pattern.sub(firestore_init, content)

# 2. Replace MOCK_SESSIONS.append
append_pattern = re.compile(r'            MOCK_SESSIONS\.append\(\{([^}]*)\}\)', re.MULTILINE)
append_replace = r'''            if firestore_db:
                firestore_db.collection("incidents").document(session_id).set({\1})'''
content = append_pattern.sub(append_replace, content)

# 3. Replace return MOCK_SESSIONS
return_pattern = re.compile(r'        # Return mock data for local testing\n        return MOCK_SESSIONS')
return_replace = """        # Return mock data from Firestore
        if firestore_db:
            docs = firestore_db.collection("incidents").stream()
            return [doc.to_dict() for doc in docs]
        return []"""
content = return_pattern.sub(return_replace, content)

# 4. Replace MOCK_SESSIONS loop in take_action
loop_pattern = re.compile(r'        # Find details from mock session\n        for s in MOCK_SESSIONS:\n            if s\["session_id"\] == session_id:\n                service_name = s\["incident"\]\.get\("service", service_name\)\n                alert_name = s\["incident"\]\.get\("alert_name", alert_name\)\n                # Remove from pending mock list\n                MOCK_SESSIONS\.remove\(s\)\n                break')

loop_replace = """        # Find details from mock session
        if firestore_db:
            doc_ref = firestore_db.collection("incidents").document(session_id)
            doc = doc_ref.get()
            if doc.exists:
                s = doc.to_dict()
                service_name = s.get("incident", {}).get("service", service_name)
                alert_name = s.get("incident", {}).get("alert_name", alert_name)
                doc_ref.delete()"""
content = loop_pattern.sub(loop_replace, content)

with open("frontend/main.py", "w") as f:
    f.write(content)
print("Updated frontend/main.py")
