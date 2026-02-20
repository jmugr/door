from git import Repo
from datetime import datetime
from zoneinfo import ZoneInfo
import frontmatter
from pathlib import Path
from collections import defaultdict
import argparse
import firebase_admin
from firebase_admin import credentials, db
import json
import os

def has_publish_true(repo, item):
    """Check if a markdown file has 'publish: true' in frontmatter."""
    try:
        file_content = item.data_stream.read().decode('utf-8')
        post = frontmatter.loads(file_content)
        return post.metadata.get('publish') == True
    except Exception as e:
        print(f"Warning: Could not parse {item.path}: {e}")
        return False

def generate_changelog(changelog_filename, vault_path, filter_published=True):
    """Generate a changelog of markdown file changes by day."""
    repo = Repo(vault_path)
    changes_by_day = defaultdict(set)
    rename_events_by_day = defaultdict(set)
    rename_mapping = {}
    
    print(f"Scanning git history (filter_published={filter_published})...")
    commit_count = 0
    
    # Get files that exist in the latest commit
    latest_commit = next(repo.iter_commits())
    current_files = set()
    for item in latest_commit.tree.traverse():
        if item.type == 'blob' and item.path.endswith('.md'):
            if filter_published:
                if has_publish_true(repo, item):
                    current_files.add(item.path)
            else:
                current_files.add(item.path)
    
    print(f"Found {len(current_files)} {'published ' if filter_published else ''}files in latest commit")
    
    # Iterate through all commits in reverse chronological order
    for commit in repo.iter_commits():
        commit_count += 1
        if commit_count % 100 == 0:
            print(f"Processed {commit_count} commits...")
        
        commit_date = datetime.fromtimestamp(commit.committed_date, tz=ZoneInfo("America/Chicago")).date()
        
        try:
            if commit.parents:
                diff_index = commit.parents[0].diff(commit)
            else:
                diff_index = commit.tree.diff(None)
            
            for diff_item in diff_index:
                new_path = diff_item.b_path
                old_path = diff_item.a_path
                file_path = new_path or old_path
                if not file_path or not file_path.endswith('.md'):
                    continue
                
                current_path = file_path
                if new_path and new_path in current_files:
                    current_path = new_path
                elif old_path and old_path in rename_mapping:
                    current_path = rename_mapping[old_path]
                elif file_path in rename_mapping:
                    current_path = rename_mapping[file_path]
                    
                if current_path not in current_files:
                    continue
                if file_path == changelog_filename or new_path == changelog_filename or current_path == changelog_filename:
                    continue

                # Handle rename events
                if diff_item.renamed:
                    try:
                        blob = commit.tree[new_path] if new_path else commit.tree[file_path]
                        if filter_published:
                            if has_publish_true(repo, blob):
                                rename_events_by_day[str(commit_date)].add((old_path, new_path))
                                rename_mapping[old_path] = new_path
                        else:
                            rename_events_by_day[str(commit_date)].add((old_path, new_path))
                            rename_mapping[old_path] = new_path
                    except KeyError:
                        pass
                    continue

                # Handle regular changes
                try:
                    blob = commit.tree[file_path]
                    if filter_published:
                        if has_publish_true(repo, blob):
                            changes_by_day[str(commit_date)].add(file_path)
                    else:
                        changes_by_day[str(commit_date)].add(file_path)
                except KeyError:
                    pass
        except Exception as e:
            print(f"Warning: Error processing commit {commit.hexsha[:7]}: {e}")
            continue
    
    print(f"Total commits processed: {commit_count}")
    sorted_changes = {day: sorted(files) for day, files in sorted(changes_by_day.items(), reverse=True)}
    
    def get_current_name(file_path, rename_mapping):
        """Follow the rename chain to find the current name."""
        current = file_path
        seen = set()
        while current in rename_mapping and current not in seen:
            seen.add(current)
            current = rename_mapping[current]
        return current
    
    # Track first appearance of each file
    first_appearance = {}
    for date_str in reversed(list(sorted_changes.keys())):
        for file_path in sorted_changes[date_str]:
            current_name = get_current_name(file_path, rename_mapping)
            if current_name not in first_appearance:
                first_appearance[current_name] = date_str
            if file_path not in first_appearance:
                first_appearance[file_path] = date_str
    
    # Remove changelog from all dates except its first appearance
    for date_str in list(sorted_changes.keys()):
        if changelog_filename in sorted_changes[date_str]:
            if first_appearance.get(changelog_filename) != date_str:
                sorted_changes[date_str].remove(changelog_filename)
                if not sorted_changes[date_str]:
                    del sorted_changes[date_str]
    
    return sorted_changes, first_appearance, rename_events_by_day

def write_changelog(changes_by_day, first_appearance, rename_events_by_day, changelog_filename, vault_path):
    """Write the changelog to a markdown file."""
    output_path = Path(vault_path) / changelog_filename
    
    # Build rename mapping
    rename_mapping = {}
    for rename_events in rename_events_by_day.values():
        for old_path, new_path in rename_events:
            rename_mapping[old_path] = new_path
    
    def get_current_name(file_path):
        """Follow the rename chain to find the current name."""
        current = file_path
        seen = set()
        while current in rename_mapping and current not in seen:
            seen.add(current)
            current = rename_mapping[current]
        return current
    
    all_dates = sorted(set(changes_by_day.keys()) | set(rename_events_by_day.keys()), reverse=True)
    
    # Build set of old file paths that were renamed
    old_renamed_paths = set()
    for rename_events in rename_events_by_day.values():
        for old_path, new_path in rename_events:
            old_display = old_path[:-3] if old_path and old_path.endswith('.md') else old_path
            old_renamed_paths.add(old_display)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("---\n")
        f.write("publish: true\n")
        f.write("tags:\n")
        f.write("  - topic/garage\n")
        f.write("  - type/system\n")
        f.write("---\n")
        chicago_time = datetime.now(ZoneInfo("America/Chicago"))
        f.write(f"*Last updated: {chicago_time.strftime('%Y-%m-%d %H:%M:%S %Z')}*\n")
        
        for date_str in all_dates:
            f.write(f"## {date_str}\n")
            
            # Write rename events
            rename_events = sorted(rename_events_by_day.get(date_str, set()))
            for old_path, new_path in rename_events:
                old_display = old_path[:-3] if old_path and old_path.endswith('.md') else old_path
                new_display = new_path[:-3] if new_path and new_path.endswith('.md') else new_path
                f.write(f"- Renamed: ~~{old_display}~~ to [[{new_display}]]\n")
            
            # Write regular changes
            files = changes_by_day.get(date_str, [])
            for file_path in files:
                display_name = file_path[:-3] if file_path.endswith('.md') else file_path
                current_name = get_current_name(file_path)
                if display_name in old_renamed_paths:
                    if first_appearance.get(current_name) == date_str:
                        f.write(f"- **Published: {display_name}**\n")
                    else:
                        f.write(f"- Changed: {display_name}\n")
                else:
                    if first_appearance.get(current_name) == date_str:
                        f.write(f"- **Published: [[{display_name}]]**\n")
                    else:
                        f.write(f"- Changed: [[{display_name}]]\n")
    
    print(f"Changelog written to {output_path}")

def get_last_sent_by_date():
    """Retrieve files that were sent per date in the last email."""
    try:
        initialize_firebase()
        ref = db.reference('changelog/lastSent')
        last_sent = ref.get()
        if last_sent and 'sentByDate' in last_sent:
            result = {}
            for date_str, date_data in last_sent['sentByDate'].items():
                # Handle new nested structure, with fallback to legacy flat list
                if isinstance(date_data, dict):
                    result[date_str] = {
                        'changes': set(date_data.get('changes', [])),
                        'renames': set(date_data.get('renames', []))
                    }
                else:
                    # Legacy format: flat list of paths
                    result[date_str] = {
                        'changes': set(date_data) if isinstance(date_data, list) else set(),
                        'renames': set()
                    }
            return result
    except Exception as e:
        print(f"Warning: Could not retrieve lastSent: {e}")
    return {}

def filter_new_changes(changes_by_day, rename_events_by_day, last_sent_by_date):
    """Only keep date+file combinations that weren't sent before.
    
    Returns filtered changes, filtered renames, and total count.
    """
    filtered_changes = {}
    filtered_renames = {}
    new_items_count = 0
    
    # Filter changes
    for date_str, files in changes_by_day.items():
        # Get files already sent for this specific date
        sent_data = last_sent_by_date.get(date_str, {})
        sent_changes = sent_data.get('changes', set()) if isinstance(sent_data, dict) else sent_data
        
        # Include files not yet sent for this specific date
        new_files = [f for f in files if f not in sent_changes]
        if new_files:
            filtered_changes[date_str] = new_files
            new_items_count += len(new_files)
    
    # Filter renames
    for date_str, rename_events in rename_events_by_day.items():
        sent_data = last_sent_by_date.get(date_str, {})
        sent_renames = sent_data.get('renames', set()) if isinstance(sent_data, dict) else set()
        
        new_renames = []
        for old_path, new_path in rename_events:
            rename_id = f"{old_path}->{new_path}"
            if rename_id not in sent_renames:
                new_renames.append((old_path, new_path))
        
        if new_renames:
            filtered_renames[date_str] = new_renames
            new_items_count += len(new_renames)
    
    return filtered_changes, filtered_renames, new_items_count

def initialize_firebase():
    """Initialize Firebase Admin SDK."""
    try:
        # Check if already initialized
        firebase_admin.get_app()
    except ValueError:
        # Not initialized yet
        cred = credentials.Certificate('./serviceAccountKey.json')
        firebase_admin.initialize_app(cred, {
            'databaseURL': os.getenv('FIREBASE_DATABASE_URL', 'https://garage-cc88b-default-rtdb.firebaseio.com/')
        })

def write_to_firebase(changes_by_day, first_appearance, rename_events_by_day, pending_by_day=None, pending_renames_by_day=None):
    """Write changelog data to Firebase Realtime Database.
    - Writes full snapshot to changelog/latest
    - If pending_by_day provided, writes only unsent items to changelog/pending
    """
    try:
        initialize_firebase()
        
        # Build rename mapping to resolve historical names to current names
        rename_mapping = {}
        for rename_events in rename_events_by_day.values():
            for old_path, new_path in rename_events:
                rename_mapping[old_path] = new_path
        
        def get_current_name(file_path):
            """Follow the rename chain to find the current name."""
            current = file_path
            seen = set()
            while current in rename_mapping and current not in seen:
                seen.add(current)
                current = rename_mapping[current]
            return current
        
        # Build set of old file paths that were renamed (exclude from changes)
        old_renamed_paths = set()
        for rename_events in rename_events_by_day.values():
            for old_path, new_path in rename_events:
                old_display = old_path[:-3] if old_path and old_path.endswith('.md') else old_path
                old_renamed_paths.add(old_display)
        
        # Format data for Firebase latest
        firebase_data = {}
        
        for date_str in sorted(set(changes_by_day.keys()) | set(rename_events_by_day.keys()), reverse=True):
            # Build changes list
            daily_changes = []
            if date_str in changes_by_day:
                for file_path in changes_by_day[date_str]:
                    display_name = file_path[:-3] if file_path.endswith('.md') else file_path
                    current_name = get_current_name(file_path)
                    # Skip old renamed paths
                    if display_name in old_renamed_paths:
                        continue
                    is_new = first_appearance.get(current_name) == date_str
                    daily_changes.append({
                        'name': display_name,
                        'path': file_path,
                        'new': is_new
                    })
            
            # Build renames list
            daily_renames = []
            if date_str in rename_events_by_day:
                for old_path, new_path in sorted(rename_events_by_day[date_str]):
                    old_display = old_path[:-3] if old_path and old_path.endswith('.md') else old_path
                    new_display = new_path[:-3] if new_path and new_path.endswith('.md') else new_path
                    daily_renames.append({
                        'oldName': old_display,
                        'newName': new_display,
                        'oldPath': old_path,
                        'newPath': new_path
                    })
            
            firebase_data[date_str] = {
                'timestamp': datetime.now(ZoneInfo("America/Chicago")).isoformat(),
                'changes': daily_changes,
                'renames': daily_renames,
                # Keep files for backward compatibility
                'files': daily_changes
            }
        
        # Write latest changelog data
        ref_latest = db.reference('changelog/latest')
        ref_latest.set(firebase_data)
        
        # Optionally write pending (unsent date+file combos)
        if pending_by_day is not None or pending_renames_by_day is not None:
            pending_data = {}
            all_pending_dates = set()
            if pending_by_day:
                all_pending_dates.update(pending_by_day.keys())
            if pending_renames_by_day:
                all_pending_dates.update(pending_renames_by_day.keys())
            
            for date_str in all_pending_dates:
                # Build pending changes
                daily_changes = []
                if pending_by_day and date_str in pending_by_day:
                    for file_path in pending_by_day[date_str]:
                        display_name = file_path[:-3] if file_path.endswith('.md') else file_path
                        current_name = get_current_name(file_path)
                        # Skip old renamed paths
                        if display_name in old_renamed_paths:
                            continue
                        is_new = first_appearance.get(current_name) == date_str
                        daily_changes.append({
                            'name': display_name,
                            'path': file_path,
                            'new': is_new
                        })
                
                # Build pending renames
                daily_renames = []
                if pending_renames_by_day and date_str in pending_renames_by_day:
                    for old_path, new_path in sorted(pending_renames_by_day[date_str]):
                        old_display = old_path[:-3] if old_path and old_path.endswith('.md') else old_path
                        new_display = new_path[:-3] if new_path and new_path.endswith('.md') else new_path
                        daily_renames.append({
                            'oldName': old_display,
                            'newName': new_display,
                            'oldPath': old_path,
                            'newPath': new_path
                        })
                
                # Only add entries with actual changes or renames
                if daily_changes or daily_renames:
                    pending_data[date_str] = {
                        'timestamp': datetime.now(ZoneInfo("America/Chicago")).isoformat(),
                        'changes': daily_changes,
                        'renames': daily_renames,
                        'files': daily_changes
                    }
            ref_pending = db.reference('changelog/pending')
            ref_pending.set(pending_data)
            print(f"✓ Wrote pending changes for {len(pending_data)} dates to Firebase")
        
        print(f"✓ Successfully wrote {len(firebase_data)} days of updates to Firebase (latest)")
        return True
    except Exception as e:
        print(f"✗ Error writing to Firebase: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Generate changelog from git history')
    parser.add_argument('--vault-path', default='vault', help='Path to vault directory')
    parser.add_argument('--local-only', action='store_true', 
                        help='Only generate local markdown file, skip Firebase operations')
    args = parser.parse_args()
    
    try:
        print("Starting changelog generation...")
        changelog_filename = 'Garage changelog.md'
        changes_by_day, first_appearance, rename_events_by_day = generate_changelog(
            changelog_filename, args.vault_path, filter_published=True
        )
        
        print(f"Found changes across {len(changes_by_day)} days")
        write_changelog(changes_by_day, first_appearance, rename_events_by_day, changelog_filename, args.vault_path)
        
        if args.local_only:
            print("\n✓ Local changelog generation complete (Firebase skipped)")
        else:
            # Get previously sent files (grouped by date)
            last_sent_by_date = get_last_sent_by_date()
            print(f"Last sent had changes for {len(last_sent_by_date)} dates")
            
            # Filter to only new changes (date+file combinations not sent before)
            new_changes, new_renames, new_items_count = filter_new_changes(
                changes_by_day, rename_events_by_day, last_sent_by_date
            )
            print(f"Found {new_items_count} new items to send ({len(new_changes)} change dates, {len(new_renames)} rename dates)")
            
            # Write to Firebase (latest + pending)
            write_to_firebase(
                changes_by_day, 
                first_appearance, 
                rename_events_by_day,
                pending_by_day=new_changes,
                pending_renames_by_day=new_renames
            )
            
            print("\n✓ Changelog generation complete!")
    except Exception as e:
        print(f"Error: {e}")
        raise

if __name__ == "__main__":
    main()
