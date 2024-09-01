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

default_db = Client(credentials=google_auth_creds)
prod_db = Client(
    project=GCP_PROJECT_ID, credentials=google_auth_creds, database="taxi-prod"
)


def migrate_collection(
    source_db: Client,
    target_db: Client,
    source_collection: str,
    target_collection: str,
) -> None:
    """Migrates documents from one collection to another."""
    source_ref: CollectionReference = source_db.collection(source_collection)
    target_ref: CollectionReference = target_db.collection(target_collection)

    docs = source_ref.stream()

    for doc in docs:
        data: Dict[str, Any] = doc.to_dict()
        target_ref.document(doc.id).set(data)
        print(
            f"Document {doc.id} migrated from {source_collection} to {target_collection}"
        )


def main() -> None:
    # Migrate taxi-users to users in the taxi-prod database
    migrate_collection(default_db, prod_db, "taxi-users", "users")

    # Migrate taxi-trips to trips in the taxi-prod database
    migrate_collection(default_db, prod_db, "taxi-trips", "trips")

    # Migrate taxi-shifts to shifts in the taxi-prod database
    migrate_collection(default_db, prod_db, "taxi-shifts", "shifts")


if __name__ == "__main__":
    main()
