from google.cloud.firestore import Client
from google.cloud.firestore_v1 import CollectionReference
from google.oauth2 import service_account
import os
from typing import Any, Dict
from dotenv import load_dotenv

load_dotenv()
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")

google_auth_creds = service_account.Credentials.from_service_account_file(
    "credentials.json"
)

# taxi_db = Client(
#     project=GCP_PROJECT_ID, credentials=google_auth_creds, database="taxi-dev"
# )
taxi_db = Client(
    project=GCP_PROJECT_ID, credentials=google_auth_creds, database="taxi-prod"
)


def initialize_export_history_for_users(db: Client, collection_name: str) -> None:
    """Adds the 'export_history' field as an empty list for all users if not already present."""
    collection_ref: CollectionReference = db.collection(collection_name)

    # Stream all documents in the collection
    docs = collection_ref.stream()

    for doc in docs:
        data: Dict[str, Any] = doc.to_dict()

        # If the 'export_history' field is missing, set it to an empty list
        if "export_history" not in data:
            collection_ref.document(doc.id).update({"export_history": []})
            print(
                f"Field 'export_history' initialized as an empty list for document {doc.id}"
            )
        else:
            print(f"Document {doc.id} already has 'export_history' field.")


def main() -> None:

    # Update all users in the taxi-prod database to initialize 'export_history' field as an empty list if missing
    initialize_export_history_for_users(taxi_db, "users")


if __name__ == "__main__":
    main()
