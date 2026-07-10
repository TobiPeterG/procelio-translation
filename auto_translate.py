#!/usr/bin/env python3
"""
Bootstrap and run automated Procelio translation generation.

The script creates or reuses a temporary virtual environment, installs the
required translation package, and translates a selected language pack from the
English source.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent
FILES_DIR = REPO_ROOT / "files"
TEMPLATES_DIR = REPO_ROOT / "templates"
DEFAULT_SOURCE_DIR = "ENGLISH"
DEFAULT_PACKAGE = "deep-translator==1.11.4"
CHILD_ENVVAR = "PROCELIO_TRANSLATE_CHILD"
PLACEHOLDER_RE = re.compile(r"(<[^>]+>|%[A-Z_]+%|%s|%d|\$\{[^}]+\})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update a translated language pack from ENGLISH."
    )
    parser.add_argument(
        "language_dir",
        help="Target folder inside files/, for example GERMAN or FRENCH.",
    )
    parser.add_argument(
        "--target-code",
        help="Translator target code, for example de, fr, es, it.",
    )
    parser.add_argument(
        "--source-dir",
        default=DEFAULT_SOURCE_DIR,
        help=f"Source folder inside files/ (default: {DEFAULT_SOURCE_DIR}).",
    )
    parser.add_argument(
        "--anglicized-name",
        help="Language name in English ASCII, for example German.",
    )
    parser.add_argument(
        "--native-name",
        help="Language name in the target language, for example Deutsch.",
    )
    parser.add_argument(
        "--authors",
        help="Author attribution to write into language.json.",
    )
    parser.add_argument(
        "--venv-dir",
        default=str(Path(tempfile.gettempdir()) / "procelio-translate-venv"),
        help="Temporary virtual environment directory.",
    )
    parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help=f"Translation package to install in the temp venv (default: {DEFAULT_PACKAGE}).",
    )
    parser.add_argument(
        "--glossary-path",
        help="Optional glossary file path. Defaults to files/<LANG>/glossary.json.",
    )
    parser.add_argument(
        "--state-path",
        help="Optional state file path. Defaults to files/<LANG>/translation_state.txt.",
    )
    parser.add_argument(
        "--include-uncommitted-source-changes",
        action="store_true",
        help="Also translate uncommitted changes in files/ENGLISH/language.json.",
    )
    parser.add_argument(
        "--force-retranslate",
        action="store_true",
        help="Retranslate all non-glossary values.",
    )
    parser.add_argument(
        "--no-reuse-existing",
        action="store_true",
        help="Do not reuse existing values from the current target language.json.",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Fail instead of installing the translation package when it is missing.",
    )
    parser.add_argument(
        "--write-glossary-template-only",
        action="store_true",
        help="Create the glossary file if missing and exit without translating.",
    )
    return parser.parse_args()


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def ensure_bootstrap_environment(args: argparse.Namespace) -> None:
    if os.environ.get(CHILD_ENVVAR) == "1":
        return
    if args.write_glossary_template_only:
        return

    venv_dir = Path(args.venv_dir)
    python_in_venv = venv_python(venv_dir)

    if not python_in_venv.exists():
        print(f"[bootstrap] Creating temporary virtual environment at {venv_dir}")
        run([sys.executable, "-m", "venv", str(venv_dir)])

    package_name = args.package.split("==", 1)[0].replace("-", "_")
    check_cmd = [
        str(python_in_venv),
        "-c",
        (
            "import importlib.util, sys; "
            f"sys.exit(0 if importlib.util.find_spec('{package_name}') else 1)"
        ),
    ]
    package_missing = subprocess.run(check_cmd).returncode != 0

    if package_missing:
        if args.no_install:
            raise SystemExit(
                f"Required package {args.package!r} is missing in {venv_dir}. "
                "Re-run without --no-install."
            )
        print(f"[bootstrap] Installing {args.package} into {venv_dir}")
        run([str(python_in_venv), "-m", "pip", "install", args.package])

    child_env = dict(os.environ)
    child_env[CHILD_ENVVAR] = "1"
    child_cmd = [str(python_in_venv), str(Path(__file__).resolve()), *sys.argv[1:]]
    run(child_cmd, env=child_env)
    raise SystemExit(0)


def default_glossary_path(language_dir: str) -> Path:
    return FILES_DIR / language_dir / "glossary.json"


def default_state_path(language_dir: str) -> Path:
    return FILES_DIR / language_dir / "translation_state.txt"


def default_style_path(language_dir: str) -> Path:
    return FILES_DIR / language_dir / "style.json"


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_state_commit(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def write_state_commit(path: Path, commit_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{commit_hash}\n", encoding="utf-8")


def git_output(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, cwd=REPO_ROOT).strip()


def latest_commit_for_path(path: Path) -> str:
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    return git_output(["git", "log", "-n", "1", "--format=%H", "--", relative_path])


def read_json_from_git(commit_hash: str, path: Path) -> Any:
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    return json.loads(git_output(["git", "show", f"{commit_hash}:{relative_path}"]))


def path_has_uncommitted_changes(path: Path) -> bool:
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    has_unstaged_changes = subprocess.run(
        ["git", "diff", "--quiet", "--", relative_path],
        cwd=REPO_ROOT,
    ).returncode != 0
    has_staged_changes = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", relative_path],
        cwd=REPO_ROOT,
    ).returncode != 0
    return has_unstaged_changes or has_staged_changes


def source_values_by_key(source_data: dict[str, Any]) -> dict[str, str]:
    return {
        entry["name"]: entry.get("value", "")
        for entry in source_data.get("language_elements", [])
        if "name" in entry
    }


def changed_existing_source_keys(
    previous_source_data: dict[str, Any],
    current_source_data: dict[str, Any],
) -> set[str]:
    previous_by_name = source_values_by_key(previous_source_data)
    current_by_name = source_values_by_key(current_source_data)

    return {
        key_name
        for key_name, current_value in current_by_name.items()
        if key_name in previous_by_name and previous_by_name[key_name] != current_value
    }


def added_source_keys(
    previous_source_data: dict[str, Any],
    current_source_data: dict[str, Any],
) -> set[str]:
    previous_by_name = source_values_by_key(previous_source_data)
    current_by_name = source_values_by_key(current_source_data)

    return {
        key_name
        for key_name in current_by_name
        if key_name not in previous_by_name
    }


def changed_source_keys_since_commit(
    source_json: Path,
    previous_commit: str | None,
    current_source_data: dict[str, Any],
) -> set[str]:
    if not previous_commit:
        return set()
    try:
        previous_source_data = read_json_from_git(previous_commit, source_json)
    except subprocess.CalledProcessError:
        print(
            f"[warning] Stored source commit {previous_commit} is not available in git history. "
            "Changed existing English keys cannot be detected for this run."
        )
        return set()
    return changed_existing_source_keys(previous_source_data, current_source_data)


def copy_template_if_missing(path: Path, template_name: str) -> dict[str, Any]:
    if not path.exists():
        template_path = TEMPLATES_DIR / template_name
        if not template_path.exists():
            raise SystemExit(f"Template file does not exist: {template_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template_path, path)
    return read_json(path)


def ensure_glossary(path: Path) -> dict[str, Any]:
    payload = copy_template_if_missing(path, "glossary.json")

    payload.setdefault("meta", {})
    payload.setdefault("by_key", {})
    payload.setdefault("by_source_text", {})
    payload.setdefault("post_replace", [])
    return payload


def protect_text(text: str) -> tuple[str, list[tuple[str, str]]]:
    replacements: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        token = f"__PROCELIO_TOKEN_{len(replacements)}__"
        replacements.append((token, match.group(0)))
        return token

    return PLACEHOLDER_RE.sub(replace, text), replacements


def unprotect_text(text: str, replacements: list[tuple[str, str]]) -> str:
    for token, original in replacements:
        text = text.replace(token, original)
    return text


def apply_post_replace(text: str, glossary: dict[str, Any]) -> str:
    for pair in glossary.get("post_replace", []):
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        before, after = pair
        text = text.replace(before, after)
    return text


def resolve_glossary_value(
    text: str,
    glossary: dict[str, Any],
    *,
    key_name: str,
) -> str | None:
    if key_name in glossary["by_key"]:
        return glossary["by_key"][key_name]
    if text in glossary["by_source_text"]:
        return apply_post_replace(glossary["by_source_text"][text], glossary)
    return None


def build_runtime_translation_map(
    source_data: dict[str, Any],
    target_data: dict[str, Any] | None,
    glossary: dict[str, Any],
    excluded_keys: set[str] | None = None,
) -> dict[str, str]:
    excluded_keys = excluded_keys or set()
    runtime_map = {
        source_text: apply_post_replace(translated_text, glossary)
        for source_text, translated_text in glossary.get("by_source_text", {}).items()
        if source_text
    }
    if not target_data:
        return runtime_map

    target_by_name = {
        entry["name"]: entry
        for entry in target_data.get("language_elements", [])
        if "name" in entry
    }
    for source_entry in source_data.get("language_elements", []):
        key_name = source_entry.get("name")
        source_value = source_entry.get("value", "")
        if not key_name or not source_value:
            continue
        if key_name in excluded_keys:
            continue
        if key_name in glossary.get("by_key", {}):
            continue
        target_entry = target_by_name.get(key_name)
        if not target_entry:
            continue
        target_value = target_entry.get("value", "")
        if not target_value:
            continue
        runtime_map[source_value] = target_value

    return runtime_map


def key_needs_machine_translation(
    source_entry: dict[str, Any],
    glossary: dict[str, Any],
    runtime_translation_map: dict[str, str],
) -> bool:
    source_value = source_entry.get("value", "")
    if resolve_glossary_value(source_value, glossary, key_name=source_entry["name"]) is not None:
        return False
    if not source_value:
        return False
    return source_value not in runtime_translation_map


def import_translator() -> Any:
    if importlib.util.find_spec("deep_translator") is None:
        raise SystemExit(
            "deep-translator is not available in the current environment. "
            "Re-run the script without --no-install so it can bootstrap itself."
        )
    from deep_translator import GoogleTranslator  # type: ignore

    return GoogleTranslator


def translate_value(
    translator: Any,
    text: str,
    glossary: dict[str, Any],
    runtime_translation_map: dict[str, str],
    *,
    key_name: str,
) -> str:
    glossary_value = resolve_glossary_value(text, glossary, key_name=key_name)
    if glossary_value is not None:
        return glossary_value
    if not text:
        return text
    if text in runtime_translation_map:
        return apply_post_replace(runtime_translation_map[text], glossary)

    protected_text, replacements = protect_text(text)

    for attempt in range(5):
        try:
            translated = translator.translate(protected_text)
            break
        except Exception as exc:  # pragma: no cover - network dependent
            if attempt == 4:
                raise SystemExit(f"Translation failed for {key_name!r}: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    translated = unprotect_text(translated, replacements)
    translated = apply_post_replace(translated, glossary)
    runtime_translation_map[text] = translated
    return translated


def build_target_entry(
    source_entry: dict[str, Any],
    translated_value: str,
) -> dict[str, Any]:
    return {"name": source_entry["name"], "value": translated_value}


def translate_language(args: argparse.Namespace) -> None:
    source_dir = FILES_DIR / args.source_dir
    target_dir = FILES_DIR / args.language_dir
    source_json = source_dir / "language.json"
    source_image = source_dir / "image.png"
    target_json = target_dir / "language.json"
    target_image = target_dir / "image.png"

    if not source_json.exists():
        raise SystemExit(f"Source language file does not exist: {source_json}")
    if not source_image.exists():
        raise SystemExit(f"Source image file does not exist: {source_image}")

    glossary_path = (
        Path(args.glossary_path)
        if args.glossary_path
        else default_glossary_path(args.language_dir)
    )
    style_path = default_style_path(args.language_dir)
    state_path = (
        Path(args.state_path)
        if args.state_path
        else default_state_path(args.language_dir)
    )

    glossary = ensure_glossary(glossary_path)
    copy_template_if_missing(style_path, "style.json")
    if args.write_glossary_template_only:
        print(f"Wrote glossary template to {glossary_path}")
        print(f"Ensured style template at {style_path}")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    if not target_image.exists():
        shutil.copy2(source_image, target_image)

    current_source_commit = latest_commit_for_path(source_json)
    worktree_source_data = read_json(source_json)
    committed_source_data = read_json_from_git(current_source_commit, source_json)
    existing_target = read_json(target_json) if target_json.exists() else None
    previous_commit = read_state_commit(state_path)
    has_uncommitted_source_changes = path_has_uncommitted_changes(source_json)
    uncommitted_changed_existing_keys = set()
    uncommitted_added_keys = set()
    source_data = committed_source_data
    if has_uncommitted_source_changes:
        uncommitted_changed_existing_keys = changed_existing_source_keys(
            committed_source_data,
            worktree_source_data,
        )
        uncommitted_added_keys = added_source_keys(
            committed_source_data,
            worktree_source_data,
        )
        if args.include_uncommitted_source_changes:
            source_data = worktree_source_data
            affected_keys = sorted(uncommitted_changed_existing_keys | uncommitted_added_keys)
            print(
                "[warning] files/ENGLISH/language.json has uncommitted changes. "
                "Including them because --include-uncommitted-source-changes was used."
            )
            if affected_keys:
                print("[info] Uncommitted English-source keys included:")
                for key_name in affected_keys:
                    print(f"  - {key_name}")
        else:
            ignored_keys = sorted(uncommitted_changed_existing_keys | uncommitted_added_keys)
            print(
                "[warning] files/ENGLISH/language.json has uncommitted changes. "
                "Ignoring them for this run. Commit them first or use "
                "--include-uncommitted-source-changes."
            )
            if ignored_keys:
                print("[warning] Ignored uncommitted English-source keys:")
                for key_name in ignored_keys:
                    print(f"  - {key_name}")

    changed_source_keys = changed_source_keys_since_commit(
        source_json,
        previous_commit,
        committed_source_data,
    )
    if args.include_uncommitted_source_changes:
        changed_source_keys |= uncommitted_changed_existing_keys

    existing_by_name = (
        {
            entry["name"]: entry
            for entry in existing_target.get("language_elements", [])
            if "name" in entry
        }
        if existing_target
        else {}
    )
    if args.no_reuse_existing:
        existing_by_name = {}
        runtime_translation_map = build_runtime_translation_map(
            source_data,
            None,
            glossary,
            excluded_keys=changed_source_keys,
        )
    else:
        runtime_translation_map = build_runtime_translation_map(
            source_data,
            existing_target,
            glossary,
            excluded_keys=changed_source_keys,
        )

    keys_to_translate = [
        source_entry["name"]
        for source_entry in source_data.get("language_elements", [])
        if (
            args.force_retranslate
            or source_entry["name"] not in existing_by_name
            or source_entry["name"] in changed_source_keys
        )
    ]

    machine_translation_needed = any(
        key_needs_machine_translation(source_entry, glossary, runtime_translation_map)
        for source_entry in source_data.get("language_elements", [])
        if source_entry["name"] in keys_to_translate
    )

    translator = None
    if machine_translation_needed:
        if not args.target_code:
            raise SystemExit(
                "--target-code is required when new or changed source entries need machine translation."
            )
        GoogleTranslator = import_translator()
        translator = GoogleTranslator(source="en", target=args.target_code)

    translated_elements = []
    for source_entry in source_data.get("language_elements", []):
        key_name = source_entry["name"]
        source_value = source_entry.get("value", "")
        glossary_value = resolve_glossary_value(source_value, glossary, key_name=key_name)
        if glossary_value is not None:
            translated_value = glossary_value
        elif (
            not args.force_retranslate
            and key_name in existing_by_name
            and key_name not in changed_source_keys
        ):
            translated_value = existing_by_name[key_name].get("value", "")
        else:
            if translator is None:
                raise SystemExit(
                    f"No translator available for key {key_name!r}. "
                    "Re-run with --target-code."
                )
            translated_value = translate_value(
                translator,
                source_value,
                glossary,
                runtime_translation_map,
                key_name=key_name,
            )
        translated_elements.append(
            build_target_entry(
                source_entry,
                translated_value,
            )
        )

    output = copy.deepcopy(source_data)
    output["language_elements"] = translated_elements
    output["anglicized_name"] = (
        args.anglicized_name
        or (existing_target or {}).get("anglicized_name")
        or args.language_dir.title()
    )
    output["native_name"] = (
        args.native_name
        or (existing_target or {}).get("native_name")
        or args.language_dir.title()
    )
    output["authors"] = (
        args.authors
        or (existing_target or {}).get("authors")
        or "Unknown"
    )
    if existing_target and "version" in existing_target:
        output["version"] = existing_target["version"]

    write_json(target_json, output)
    write_state_commit(state_path, current_source_commit)

    print(f"Translated {args.language_dir} from {args.source_dir}")
    print(f"language.json: {target_json}")
    print(f"image.png:      {target_image}")
    print(f"glossary:       {glossary_path}")
    print(f"state:          {state_path}")
    print(f"source commit:  {current_source_commit}")


def main() -> None:
    args = parse_args()
    ensure_bootstrap_environment(args)
    translate_language(args)


if __name__ == "__main__":
    main()
