"""Restore small public regression originals from a checked, local-only bundle.

No model/network/env access. Existing differing files and unsafe paths are refused.
The archive is test data, not a new execution or independent human evaluation.
"""

import argparse
import hashlib
import json
import stat
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tests/fixtures/historical-regression.zip"
MANIFEST = ROOT / "tests/fixtures/historical-regression.json"


def restore(root=ROOT, bundle=BUNDLE, manifest=MANIFEST):
    root = root.resolve()
    contract = json.loads(manifest.read_bytes())
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != contract["archive_sha256"]:
        raise ValueError("Fixture archive fingerprint mismatch")
    pending = []
    with ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(contract["files"]):
            raise ValueError("Fixture archive file set mismatch")
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            if (
                "\\" in name
                or ":" in name
                or path.is_absolute()
                or ".." in path.parts
                or not (name.startswith("reports/") or name == "eval/minimal_correction_v016.json")
                or info.is_dir()
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.file_size > 2_000_000
            ):
                raise ValueError("Unsafe fixture member")
            target = (root / name).resolve()
            if not target.is_relative_to(root) or any(
                parent.is_symlink()
                for parent in [root / name, *(root / name).parents]
                if parent.is_relative_to(root)
            ):
                raise ValueError("Fixture escaped project")
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != contract["files"][name]:
                raise ValueError("Fixture member fingerprint mismatch")
            if target.exists():
                if not target.is_file() or target.read_bytes() != raw:
                    raise ValueError("Existing fixture differs; original was not overwritten")
            else:
                pending.append((target, raw))
        # Validate the whole archive before the first write.
        for target, raw in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)
    return {
        "restored": len(pending),
        "already_identical": len(names) - len(pending),
        "note": "Saved public originals only; no model/network call or new quality evidence.",
    }


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(restore(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
