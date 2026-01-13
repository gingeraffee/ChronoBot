# migrate_json.py
import json
from pathlib import Path

def migrate_chromie_state():
    """
    Migrate chromie_state.json to add timezone and reminder_time fields
    """
    
    # Load the current JSON file
    data_file = Path("chromie_state.json")
    
    if not data_file.exists():
        print("❌ chromie_state.json not found!")
        return False
    
    print("📂 Loading chromie_state.json...")
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Counter for changes
    servers_updated = 0
    events_updated = 0
    
    # Step 1: Add timezone to each server
    print("\n🌍 Adding timezone field to servers...")
    for guild_id, server_data in data.get("servers", {}).items():
        if "timezone" not in server_data:
            server_data["timezone"] = "UTC"  # Default timezone
            servers_updated += 1
            print(f"  ✅ Server {guild_id}: Added timezone = 'UTC'")
        else:
            print(f"  ℹ️  Server {guild_id}: timezone already exists")
    
    # Step 2: Add reminder_time to each event
    print("\n⏰ Adding reminder_time field to events...")
    for guild_id, server_data in data.get("servers", {}).items():
        for event in server_data.get("events", []):
            if "reminder_time" not in event:
                event["reminder_time"] = None  # None means use event timestamp
                events_updated += 1
                print(f"  ✅ Event '{event.get('name', 'Unknown')}': Added reminder_time = None")
            else:
                print(f"  ℹ️  Event '{event.get('name', 'Unknown')}': reminder_time already exists")
    
    # Step 3: Save the updated JSON
    print("\n💾 Saving updated chromie_state.json...")
    with open(data_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    # Summary
    print("\n" + "="*50)
    print("✅ MIGRATION COMPLETE!")
    print("="*50)
    print(f"Servers updated: {servers_updated}")
    print(f"Events updated: {events_updated}")
    print("\nYour chromie_state.json has been updated with:")
    print("  • timezone field (defaults to 'UTC')")
    print("  • reminder_time field per event (defaults to None)")
    print("\nYou can now use /timezone and /editevent reminder-time commands!")
    
    return True

if __name__ == "__main__":
    success = migrate_chromie_state()
    if not success:
        print("Migration failed!")
        exit(1)