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

def display_name_for_path(file_path):
    """Convert a markdown path to a display name without extension."""
    if file_path and file_path.endswith('.md'):
        return file_path[:-3]
    return file_path

def aggregate_daily_activity(date_str, daily_changes, daily_renames, first_appearance, get_current_name):
    """Return one entry per file for a day using priority: Published > Renamed > Changed."""
    priority_by_status = {
        'changed': 1,
        'renamed': 2,
        'published': 3,
    }
    selected = {}

    def is_new_for_date(*paths):
        for path in paths:
            if path and first_appearance.get(path) == date_str:
                return True
        return False

    def upsert(current_path, candidate):
        existing = selected.get(current_path)
        if not existing:
            selected[current_path] = candidate
            return
        if priority_by_status[candidate['status']] > priority_by_status[existing['status']]:
            selected[current_path] = candidate

    for file_path in sorted(daily_changes or []):
        current_path = get_current_name(file_path)
        is_new = is_new_for_date(current_path, file_path)
        upsert(current_path, {
            'status': 'published' if is_new else 'changed',
            'name': display_name_for_path(current_path),
            'path': current_path,
            'new': is_new,
        })

    for old_path, new_path in sorted(daily_renames or []):
        current_path = get_current_name(new_path or old_path)
        is_new = is_new_for_date(current_path, new_path, old_path)
        if is_new:
            upsert(current_path, {
                'status': 'published',
                'name': display_name_for_path(new_path or current_path),
                'path': new_path or current_path,
                'new': True,
            })
        else:
            upsert(current_path, {
                'status': 'renamed',
                'oldName': display_name_for_path(old_path),
                'newName': display_name_for_path(new_path or current_path),
                'oldPath': old_path,
                'newPath': new_path or current_path,
            })

    published = sorted(
        [entry for entry in selected.values() if entry['status'] == 'published'],
        key=lambda entry: entry['name']
    )
    renamed = sorted(
        [entry for entry in selected.values() if entry['status'] == 'renamed'],
        key=lambda entry: entry['newName']
    )
    changed = sorted(
        [entry for entry in selected.values() if entry['status'] == 'changed'],
        key=lambda entry: entry['name']
    )

    return published, renamed, changed

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

            published_entries, renamed_entries, changed_entries = aggregate_daily_activity(
                date_str,
                changes_by_day.get(date_str, []),
                rename_events_by_day.get(date_str, set()),
                first_appearance,
                get_current_name,
            )

            for entry in published_entries:
                f.write(f"- **Published: [[{entry['name']}]]**\n")

            for entry in renamed_entries:
                f.write(f"- Renamed: ~~{entry['oldName']}~~ to [[{entry['newName']}]]\n")

            for entry in changed_entries:
                f.write(f"- Changed: [[{entry['name']}]]\n")
    
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
    # Build rename mapping so we can resolve old git paths to their current names.
    # sent_changes was recorded using the current name at time of send, which may
    # differ from the raw git path stored in changes_by_day for renamed files.
    rename_mapping = {}
    for rename_events in rename_events_by_day.values():
        for old_path, new_path in rename_events:
            rename_mapping[old_path] = new_path

    def get_current_name(file_path):
        current = file_path
        seen = set()
        while current in rename_mapping and current not in seen:
            seen.add(current)
            current = rename_mapping[current]
        return current

    filtered_changes = {}
    filtered_renames = {}
    new_items_count = 0
    
    # Filter changes
    for date_str, files in changes_by_day.items():
        # Get files already sent for this specific date
        sent_data = last_sent_by_date.get(date_str, {})
        sent_changes = sent_data.get('changes', set()) if isinstance(sent_data, dict) else sent_data
        
        # Include files not yet sent for this specific date.
        # Check both the raw git path AND the current name (after rename chain),
        # because sent_changes may store either form depending on when it was recorded.
        new_files = [
            f for f in files
            if f not in sent_changes and get_current_name(f) not in sent_changes
        ]
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
        
        # Format data for Firebase latest
        firebase_data = {}
        
        for date_str in sorted(set(changes_by_day.keys()) | set(rename_events_by_day.keys()), reverse=True):
            published_entries, renamed_entries, changed_entries = aggregate_daily_activity(
                date_str,
                changes_by_day.get(date_str, []),
                rename_events_by_day.get(date_str, set()),
                first_appearance,
                get_current_name,
            )

            daily_changes = [
                {
                    'name': entry['name'],
                    'path': entry['path'],
                    'new': True,
                }
                for entry in published_entries
            ]
            daily_changes.extend(
                {
                    'name': entry['name'],
                    'path': entry['path'],
                    'new': False,
                }
                for entry in changed_entries
            )

            daily_renames = [
                {
                    'oldName': entry['oldName'],
                    'newName': entry['newName'],
                    'oldPath': entry['oldPath'],
                    'newPath': entry['newPath'],
                }
                for entry in renamed_entries
            ]
            
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
                published_entries, renamed_entries, changed_entries = aggregate_daily_activity(
                    date_str,
                    pending_by_day.get(date_str, []) if pending_by_day else [],
                    pending_renames_by_day.get(date_str, []) if pending_renames_by_day else [],
                    first_appearance,
                    get_current_name,
                )

                daily_changes = [
                    {
                        'name': entry['name'],
                        'path': entry['path'],
                        'new': True,
                    }
                    for entry in published_entries
                ]
                daily_changes.extend(
                    {
                        'name': entry['name'],
                        'path': entry['path'],
                        'new': False,
                    }
                    for entry in changed_entries
                )

                daily_renames = [
                    {
                        'oldName': entry['oldName'],
                        'newName': entry['newName'],
                        'oldPath': entry['oldPath'],
                        'newPath': entry['newPath'],
                    }
                    for entry in renamed_entries
                ]
                
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
