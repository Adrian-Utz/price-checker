import requests
import sys
import os
import re

try:
    from version import VERSION_NUMBER as LOCAL_VERSION
except Exception:
    LOCAL_VERSION = None

"""
Check for new releases on GitHub and compare with the local app version.

Behavior:
- Fetches the latest GitHub release (falls back to tags if no releases).
- Compares semantic version numbers (major.minor.patch).
- Checked once per app launch; no result is cached to disk.

Written by AJ Utz
Written on: 8/3/2026
Last Updated: 8/25/2026
"""

OWNER = "Adrian-Utz"
REPO = "price-checker"


def get_repo_url():
    """Return the base GitHub repository URL."""
    return f"https://github.com/{OWNER}/{REPO}"


def get_latest_release_url():
    """Return the latest release URL for the configured repository."""
    return f"{get_repo_url()}/releases/latest"


def open_releases_page():
    """Open the latest release page with a Windows fallback.

    Returns True if opening appears successful, else False.
    """
    url = get_latest_release_url()
    try:
        import webbrowser
        if webbrowser.open(url):
            return True
    except Exception:
        pass

    if os.name == 'nt':
        try:
            os.startfile(url)
            return True
        except Exception:
            pass

    return False


def _parse_version(version_str):
    """
    Normalize a version string to a tuple of ints for comparison.

    Examples: 'v1.2.3' -> (1,2,3); '1.2' -> (1,2,0)
    Non-numeric suffixes are ignored.
    """
    if not version_str:
        return ()
    # remove leading 'v' and strip whitespace
    v = str(version_str).strip()
    v = v.lstrip("vV")
    # keep only numeric and dot and dash for split, then take numeric parts
    m = re.match(r"^(\d+(?:\.\d+)*)*", v)
    if not m:
        return ()
    parts = m.group(0).split('.')
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    # ensure major.minor.patch
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _read_local_version():
    """Read the local app version from the shared version module.

    Returns the raw string or None if not found.
    """
    return str(LOCAL_VERSION).strip() if LOCAL_VERSION else None


def get_latest_version():
    """
    Fetch the latest version tag from GitHub releases or tags.

    Returns the version string (e.g., 'v1.2.3' or '1.2.3') or None on error.
    """
    headers = {"Accept": "application/vnd.github.v3+json"}
    #To bypass github's limit rate, add a environment variable named 'GITHUB_TOKEN' to your machine. 
    #The program will use that, and you hopefully won't see the "Too many requests" message.
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'token {token}'

    # Try releases/latest first
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.ok:
            data = r.json()
            # prefer tag_name, fallback to name
            tag = data.get('tag_name') or data.get('name')
            return tag
        # if no releases, fallback to tags endpoint
        tags_url = f"https://api.github.com/repos/{OWNER}/{REPO}/tags?per_page=1"
        r2 = requests.get(tags_url, headers=headers, timeout=10)
        if r2.ok:
            tags = r2.json()
            if tags:
                return tags[0].get('name')
    except Exception:
        return None
    return None


def is_update_available():
    """
    Return (available: bool, local_version: str, latest_version: str).
    """
    local = _read_local_version()
    latest = get_latest_version()

    if not latest:
        return (False, local, None)
    lv = _parse_version(local)
    rv = _parse_version(latest)
    if not lv or not rv:
        return (False, local, latest)
    return (rv > lv, local, latest)


if __name__ == '__main__':
    """
    Main entrance to the update checker. 
    """
    avail, local, latest = is_update_available()
    if latest is None:
        print('Could not determine latest version from GitHub.')
        sys.exit(2)
    print(f'Local version: {local or "(unknown)"}')
    print(f'Latest version: {latest}')
    if avail:
        print('Update available!')
        sys.exit(0)
    else:
        print('Up to date.')
        sys.exit(1)