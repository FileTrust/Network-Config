#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Inspect the selected source files; report locations, never credential values."""
import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".local", "build", "node_modules", "artifacts", "cache", "__pycache__", "reports", ".next", ".network", ".security-backups", ".deployment-backups", "log", "logs"}
PATTERNS = {
    "private-key PEM": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "provider credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[0-9A-Z]{16})"),
    "literal wallet/signing key": re.compile(rb"(?i)(?:private[_-]?key|signing[_-]?key|operational[_-]?key)[\s\"']{0,4}[:=][\s\"']{0,4}(?:0x)?[a-f0-9]{64}"),
    "literal seed phrase": re.compile(rb"(?i)(?:mnemonic|seed_phrase)[\s\"']{0,4}[:=][\s\"']{0,4}(?:[a-z]{2,12} ){11,23}[a-z]{2,12}"),
    "machine user path": re.compile(rb"(?:/home/[A-Za-z0-9._-]+/|/mnt/[A-Za-z]/[A-Za-z0-9._-]+/|[A-Z]:\\Users\\[A-Za-z0-9._-]+)"),
    "credential in URL": re.compile(rb"https?://[^\s/@:]+:[^\s/@]+@"),
}
FIELDS = {"privatekey", "signingkey", "operationalkey", "mnemonic", "seedphrase",
          "password", "apikey", "apitoken", "accesstoken"}

def credential_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[^a-z]", "", key.lower()) in FIELDS and isinstance(item, str) and item.strip():
                yield key
            yield from credential_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from credential_fields(item)

def selected_files():
    if (ROOT / ".git").is_dir():
        result = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
        return sorted(set(name.decode() for name in result.split(b"\0") if name))
    names = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in SKIP]
        for name in files:
            names.append((Path(base) / name).relative_to(ROOT).as_posix())
    return sorted(names)

def main():
    issues = []
    files = selected_files()
    for name in files:
        path = ROOT / name
        if name.startswith("public/index/"):
            issues.append((name, 0, "generated index data selected"))
        if path.is_symlink():
            issues.append((name, 0, "symlink requires explicit publication review"))
            continue
        if not path.is_file():
            continue
        if any(part in SKIP for part in Path(name).parts) or Path(name).name in {
            "config.json", "network_config_bundle.json", "node_key.json", "priv_validator_key.json", "priv_validator_state.json", "keyvault.json", "deploy.env", ".env.local", ".env.web", "deployed_addresses.json"
        } or name.endswith((".pem", ".zip", ".bak", ".log", ".jsonl")):
            issues.append((name, 0, "runtime or sensitive file selected"))
        if path.stat().st_size > 5 * 1024 * 1024:
            issues.append((name, 0, "oversized source file requires review"))
            continue
        data = path.read_bytes()
        for match in re.finditer(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])|(?<![\w:])[0-9a-fA-F]*:[0-9a-fA-F:.%]+(?![\w:])", data):
            try:
                address = ipaddress.ip_address(match.group().decode())
            except ValueError:
                continue
            if not address.is_loopback and not address.is_unspecified:
                issues.append((name, data.count(b"\n", 0, match.start()) + 1, "non-local IP literal"))
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(data):
                issues.append((name, data.count(b"\n", 0, match.start()) + 1, label))
        if path.suffix == ".json":
            try:
                if next(credential_fields(json.loads(data)), None):
                    issues.append((name, 0, "nonempty credential field"))
            except (ValueError, UnicodeDecodeError):
                pass
    for name, line, label in issues:
        print(f"{name}:{line}: {label}")
    if issues:
        return 1
    print(f"Publication checks passed for {len(files)} selected files.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
