from git import Repo
from datetime import datetime
from zoneinfo import ZoneInfo
import frontmatter
from pathlib import Path
from collections import defaultdict
import argparse

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

def main():
    parser = argparse.ArgumentParser(description='Generate changelog from git history')
    parser.add_argument('--vault-path', default='vault', help='Path to vault directory')
    args = parser.parse_args()
    
    try:
        print("Starting changelog generation...")
        changelog_filename = 'Garage changelog.md'
        changes_by_day, first_appearance, rename_events_by_day = generate_changelog(
            changelog_filename, args.vault_path, filter_published=True
        )
        
        print(f"Found changes across {len(changes_by_day)} days")
        write_changelog(changes_by_day, first_appearance, rename_events_by_day, changelog_filename, args.vault_path)
        print("\n✓ Changelog generation complete!")
    except Exception as e:
        print(f"Error: {e}")
        raise

if __name__ == "__main__":
    main()
