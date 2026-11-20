"""Import DAMU feature settings into a stopped Server Builder deployment."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

IMAGE_SUFFIXES = {".gif", ".jpg", ".png", ".webp"}
JSON_FILES = ("dashboard_config.json", "verification_config.json", "automod/offenses.json",
              "server_builder_last_schema.json")


def read_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return data


def guild_objects(data: dict, filename: str) -> None:
    if any(not guild.isascii() or not guild.isdecimal() or not isinstance(item, dict)
           for guild, item in data.items()):
        raise ValueError(f"{filename} must map Discord server IDs to settings objects.")


def migration_files(source: Path) -> dict[Path, bytes]:
    assets = {}
    settings = {}
    for name in JSON_FILES:
        path = source / name
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Resolve the linked input file {name} before importing.")
        raw = read_object(path)
        target = Path(name)
        if name == "automod/offenses.json":
            if "offenses" not in raw and "settings" not in raw:
                raw = {"offenses": raw, "settings": {}}
            guilds = raw.get("settings", {})
            if not isinstance(guilds, dict) or not isinstance(raw.get("offenses", {}), dict):
                raise ValueError("AutoMod offenses and settings must contain objects.")
            guild_objects(guilds, name)
            for record in guilds.values():
                if any(not isinstance(record.get(key, []), list)
                       for key in ("bypass_role_ids", "ignored_channel_ids")):
                    raise ValueError("AutoMod role and channel exceptions must be lists.")
                record["enabled"] = False
        else:
            guild_objects(raw, name)
        if name == "dashboard_config.json":
            for guild, record in raw.items():
                welcome = record.get("welcome", {})
                if not isinstance(welcome, dict) or not isinstance(record.get("auto_roles", {}), dict):
                    raise ValueError("Dashboard welcome and auto roles must contain objects.")
                original = welcome.get("banner_file")
                if original:
                    if not isinstance(original, str):
                        raise ValueError("Dashboard banner_file must be a filename.")
                    basename = Path(original).name
                    asset = source / "welcome_assets" / basename
                    relative = Path("welcome_assets") / basename
                    if (asset.stem.split("-", 1)[0] == guild and asset.suffix.lower() in IMAGE_SUFFIXES
                            and asset.is_file() and not asset.is_symlink() and asset.resolve().is_relative_to(source)):
                        assets[relative] = asset.read_bytes()
                        welcome["banner_file"] = basename
                    else:
                        welcome["banner_file"] = ""
        elif name == "server_builder_last_schema.json":
            target = Path("builder_tools.json")
            raw = {guild: {"last_build": schema} for guild, schema in raw.items()}
        settings[target] = (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode()
    return {**assets, **settings}


def copy_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as draft:
        temporary = Path(draft.name)
        try:
            draft.write(data)
            draft.flush()
            os.fsync(draft.fileno())
            draft.close()
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="DAMU data directory from a known backup")
    parser.add_argument("--target", default=Path("data"), type=Path, help="Server Builder data directory")
    parser.add_argument("--apply", action="store_true", help="Write validated files. Defaults to a dry run.")
    args = parser.parse_args()
    try:
        source, target = args.source.resolve(), args.target.resolve()
        if source == target:
            raise ValueError("Use a separate DAMU backup as the input.")
        files = migration_files(source)
        if not files:
            raise ValueError("No supported settings found. Pass DAMU's data directory, not a patch or archive.")
        pending = []
        conflicts = []
        for relative, data in files.items():
            path = target / relative
            if path.is_symlink() or not path.resolve().is_relative_to(target):
                raise ValueError(f"Destination {relative} is a linked path.")
            if path.exists():
                if path.is_file() and path.read_bytes() == data:
                    print(f"Already imported: {relative}")
                else:
                    conflicts.append(relative)
                    print(f"Existing file kept: {relative}")
            else:
                pending.append((path, data))
                print(f"Ready to import: {relative}")
        if conflicts:
            print("Review the existing files before retrying. No files were written.", file=sys.stderr)
            return 1
        if args.apply:
            for path, data in pending:
                copy_new(path, data)
            print(f"Imported {len(pending)} file(s).")
        else:
            print("Dry run. Pass --apply to write these files.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Import stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
