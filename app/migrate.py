from app.config import Settings
from app.db import migrate

if __name__ == "__main__":
    migrate(Settings().database_url)
    print("Database migrations applied.")
