#!/usr/bin/env python3
"""scripts/set_image_tag.py -- the CI pipeline's only write to the GitOps state.

Sets ``<service>.image.tag`` in an environment's values file:

    python scripts/set_image_tag.py charts/taskflow/values-dev.yaml task-service 3f2a9c1

A line-based edit (no YAML round trip) so the file's comments and layout
survive. The service's block is the top-level key ``<service>:``; inside it,
an existing ``image: {tag: ...}`` line is replaced, otherwise one is added.
Top-level aliases (``service: *ha``) are expanded into a block that merges
the anchor and adds the tag.

``--promote dev prod`` copies every pinned tag from values-dev.yaml into
values-prod.yaml (promotion of what was tested in dev).
"""

import re
import sys
from pathlib import Path

TAG_LINE = re.compile(r"^  image:\s*\{\s*tag:\s*\"?([^\"}\s]*)\"?\s*\}\s*$")


def set_tag(path: Path, service: str, tag: str) -> bool:
    lines = path.read_text().splitlines()
    out, changed, i = [], False, 0
    found = False
    while i < len(lines):
        line = lines[i]
        alias = re.match(rf"^{re.escape(service)}:\s*\*(\w[\w-]*)\s*$", line)
        if alias:  # "task-service: *ha"  ->  block with merge + image
            out += [f"{service}:", f"  <<: *{alias.group(1)}", f'  image: {{tag: "{tag}"}}']
            found = changed = True
            i += 1
            continue
        if re.match(rf"^{re.escape(service)}:\s*$", line):
            found = True
            out.append(line)
            i += 1
            block_end = i
            while block_end < len(lines) and (lines[block_end].startswith(" ") or not lines[block_end].strip()):
                block_end += 1
            block = lines[i:block_end]
            for j, b in enumerate(block):
                m = TAG_LINE.match(b)
                if m:
                    changed = m.group(1) != tag
                    block[j] = f'  image: {{tag: "{tag}"}}'
                    break
            else:
                # insert right after the service key
                block.insert(0, f'  image: {{tag: "{tag}"}}')
                changed = True
            out += block
            i = block_end
            continue
        out.append(line)
        i += 1
    if not found:
        out += [f"{service}:", f'  image: {{tag: "{tag}"}}']
        changed = True
    path.write_text("\n".join(out) + "\n")
    return changed


def pinned_tags(path: Path) -> dict[str, str]:
    tags, current = {}, None
    for line in path.read_text().splitlines():
        key = re.match(r"^([a-z][\w-]*):", line)
        if key:
            current = key.group(1)
        m = TAG_LINE.match(line)
        if m and current:
            tags[current] = m.group(1)
    return tags


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "--promote":
        base = Path("charts/taskflow")
        src, dst = base / f"values-{argv[2]}.yaml", base / f"values-{argv[3]}.yaml"
        changed = [s for s, t in pinned_tags(src).items() if set_tag(dst, s, t)]
        print(f"promoted {len(changed)} image tag(s) {argv[2]} -> {argv[3]}: {', '.join(changed) or '-'}")
        return 0
    if len(argv) != 4:
        print(__doc__)
        return 2
    path, service, tag = Path(argv[1]), argv[2], argv[3]
    print(f"{service} -> {tag} in {path}: {'changed' if set_tag(path, service, tag) else 'unchanged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
