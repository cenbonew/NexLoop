"""NX-062 C8 G-6: the built web front end the API serves from its web root holds only publishable files.

The API mounts the web root with StaticFiles(html=True), so every file in it is reachable through the Gateway. The rule is a fixed
table of allowed suffixes - the one scripts/prepare_community_build.py already enforced for the image's build context, now shared
from here - and nothing Vite's own output does not need: no source map (*.map, or a sourceMappingURL comment in a script or
stylesheet), no dotfile, no symbolic link, and an index.html at the top. Vite writes no manifest in this build (build.manifest is
off), so there is no manifest to compare with; the suffix table is the build list.

    python3 scripts/check_web_build.py apps/web/dist      prints one JSON line; exit 0 publishable, 1 problems
"""
import json
import re
import sys
from pathlib import Path

ALLOWED_SUFFIXES = frozenset({'.html', '.js', '.css', '.png'})
SOURCE_MAP = re.compile(rb'[#@]\s*sourceMappingURL\s*=')


def problems(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        return ['web root: a directory required']
    found = []
    files = sorted(p for p in root.rglob('*'))
    if not (root / 'index.html').is_file():
        found.append('index.html missing')
    for path in files:
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            found.append(name + ': symbolic link')
        elif any(part.startswith('.') for part in path.relative_to(root).parts):
            found.append(name + ': dotfile')
        elif path.is_file():
            if path.suffix == '.map':
                found.append(name + ': source map')
            elif path.suffix not in ALLOWED_SUFFIXES:
                found.append(name + ': suffix not in the build list')
            elif path.suffix in ('.js', '.css') and SOURCE_MAP.search(path.read_bytes()):
                found.append(name + ': sourceMappingURL comment')
    return found


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(json.dumps({'publishable': False, 'problems': ['usage: check_web_build.py <web root>']}))
        return 2
    found = problems(argv[0])
    print(json.dumps({'publishable': not found, 'problems': found}))
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
