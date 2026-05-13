# =========================================================
# MONGODB DATABASE HANDLER
# =========================================================
# Provides MongoDB-backed storage for all bot data
# Falls back to JSON if MongoDB is unavailable
# =========================================================

import os
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, List
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

class MongoDBHandler:
    """Handle MongoDB operations with JSON fallback."""
    
    def __init__(self):
        self.client = None
        self.db = None
        self.connected = False
        self.collections = {
            "auth": "moderator_auth",
            "warns": "user_warns",
            "cases": "moderation_cases",
            "protected": "protected_users",
            "abuse": "abuse_tracking",
            "temp_actions": "temporary_actions",
            "appeals": "user_appeals",
        }
        
    def connect(self) -> bool:
        """Connect to MongoDB. Returns True if successful."""
        try:
            mongodb_uri = os.environ.get(
                "MONGODB_URI",
                "mongodb://localhost:27017"
            )
            self.client = MongoClient(
                mongodb_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                retryWrites=True
            )
            # Test the connection
            self.client.admin.command('ping')
            
            db_name = os.environ.get("MONGODB_DB_NAME", "hr_moderation_bot")
            self.db = self.client[db_name]
            self.connected = True
            
            # Create indexes for better performance
            self._create_indexes()
            
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] MongoDB connected successfully", flush=True)
            return True
            
        except (ConnectionFailure, ServerSelectionTimeoutError, Exception) as e:
            self.connected = False
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARNING] MongoDB connection failed: {e}", flush=True)
            return False
    
    def _create_indexes(self):
        """Create necessary indexes for collections."""
        try:
            if self.db:
                for collection_name in self.collections.values():
                    self.db[collection_name].create_index("_id", unique=True)
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARNING] Failed to create indexes: {e}", flush=True)

    def _load_collection_data(self, key: str, default):
        """Load a single serialized dataset from a MongoDB collection."""
        if not self.is_connected():
            return default
        try:
            collection = self.db[self.collections[key]]
            document = collection.find_one({"_id": key})
            if document is None:
                return default
            return document.get("data", default)
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Failed to load {key}: {e}", flush=True)
            return default

    def _save_collection_data(self, key: str, data) -> bool:
        """Save a single serialized dataset into a MongoDB collection."""
        if not self.is_connected():
            return False
        try:
            collection = self.db[self.collections[key]]
            collection.replace_one(
                {"_id": key},
                {"_id": key, "data": data, "updated_at": datetime.now()},
                upsert=True,
            )
            return True
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Failed to save {key}: {e}", flush=True)
            return False
    
    def is_connected(self) -> bool:
        """Check if MongoDB is connected."""
        if not self.connected or not self.db:
            return False
        try:
            self.db.command('ping')
            return True
        except Exception:
            self.connected = False
            return False
    
    def load_auth(self) -> Dict[str, Any]:
        """Load all moderator auth data."""
        return self._load_collection_data("auth", {})
    
    def save_auth(self, data: Dict[str, Any]) -> bool:
        """Save moderator auth data."""
        return self._save_collection_data("auth", data)
    
    def load_warns(self) -> Dict[str, Any]:
        """Load all warn data."""
        return self._load_collection_data("warns", {})
    
    def save_warns(self, data: Dict[str, Any]) -> bool:
        """Save warn data."""
        return self._save_collection_data("warns", data)
    
    def load_cases(self) -> Dict[str, Any]:
        """Load all moderation cases."""
        return self._load_collection_data("cases", {})
    
    def save_cases(self, data: Dict[str, Any]) -> bool:
        """Save moderation cases."""
        return self._save_collection_data("cases", data)
    
    def load_protected(self) -> Dict[str, Any]:
        """Load protected users."""
        return self._load_collection_data("protected", {})
    
    def save_protected(self, data: Dict[str, Any]) -> bool:
        """Save protected users."""
        return self._save_collection_data("protected", data)
    
    def load_abuse(self) -> Dict[str, Any]:
        """Load abuse tracking data."""
        return self._load_collection_data("abuse", {})
    
    def save_abuse(self, data: Dict[str, Any]) -> bool:
        """Save abuse tracking data."""
        return self._save_collection_data("abuse", data)
    
    def load_temp_actions(self) -> List[Any]:
        """Load temporary actions."""
        return self._load_collection_data("temp_actions", [])
    
    def save_temp_actions(self, data: List[Any]) -> bool:
        """Save temporary actions."""
        return self._save_collection_data("temp_actions", data)
    
    def load_appeals(self) -> Dict[str, Any]:
        """Load appeals data."""
        return self._load_collection_data("appeals", {})
    
    def save_appeals(self, data: Dict[str, Any]) -> bool:
        """Save appeals data."""
        return self._save_collection_data("appeals", data)
    
    def disconnect(self):
        """Disconnect from MongoDB."""
        if self.client:
            self.client.close()
            self.connected = False
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] MongoDB disconnected", flush=True)


# Global instance
mongo_db = MongoDBHandler()
