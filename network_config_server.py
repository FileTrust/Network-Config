#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Serve deployed addresses + network endpoint profile for multi-server startup."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def now_iso_local() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_contract_artifact(blockchain_root: Path, contract_name: str) -> dict[str, Any] | None:
    contracts_root = blockchain_root / "artifacts" / "contracts"
    if not contracts_root.is_dir():
        return None
    direct = contracts_root / f"{contract_name}.sol" / f"{contract_name}.json"
    candidates = [direct] if direct.is_file() else []
    if not candidates:
        candidates = list(contracts_root.rglob(f"{contract_name}.json"))
    for candidate in candidates:
        try:
            data = read_json(candidate)
            if isinstance(data, dict) and isinstance(data.get("abi"), list):
                return data
        except Exception:
            continue
    return None


def read_artifact_by_rel(blockchain_root: Path, rel_path: str) -> dict[str, Any] | None:
    path = (blockchain_root / "artifacts" / "contracts" / rel_path).resolve()
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("abi"), list):
        return data
    return None


def rpc_urls(args) -> list[str]:
    """Every chain node a caller may use, best first.

    A Cosmos network is several validators, and the point of naming more than
    one here is that a Data Owner whose first node is down tries the next
    instead of stopping. --rpc-urls takes them comma separated; --chain-ip is
    the older single-machine way and still works.
    """
    given = str(getattr(args, "rpc_urls", "") or "")
    listed = [url.strip().rstrip("/") for url in given.split(",")]
    listed = [url for url in listed if url]
    if listed:
        return listed
    return [f"http://{args.chain_ip}:{args.hardhat_port}"]


def config_server_url(args) -> str:
    """The address this service tells callers to use for itself.

    It used to be built as http://<public_ip>:<port>, which was right while
    the answer was always a machine. Behind Cloudflare the answer is a name
    with its own scheme and no port, so a name given here is published as it
    stands -- gluing http:// and :8989 onto https://config.filetrust.io makes
    an address that goes nowhere.
    """
    given = str(getattr(args, "public_ip", "") or "").strip().rstrip("/")
    if given.startswith(("http://", "https://")):
        return given
    return f"http://{given}:{args.port}"


def discovery_urls(args) -> list[str]:
    """The addresses clients should use for discovery.

    --discovery-urls wins when given, so a deployment behind Cloudflare
    publishes https://disc1.filetrust.io and never its own IP.
    """
    given = getattr(args, "discovery_urls", "") or ""
    named = [url.strip().rstrip("/") for url in str(given).split(",")]
    named = [url for url in named if url]
    if named:
        return named
    return [f"http://{args.discovery_ip}:{port}" for port in (4601, 4602, 4603)]


def ftu_price_url(args) -> str:
    named = str(getattr(args, "ftu_price_url", "") or "").strip().rstrip("/")
    return named or f"http://{args.ftu_price_ip}:{args.ftu_price_port}"


PRODUCTS = ("do", "req", "guardians", "vaultkeeper", "reporter", "discovery")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def minimum_versions(args) -> dict[str, str]:
    """The oldest application version each product needs on this network.

    Written as "do=0.3.0,req=0.3.0" (command line or environment) or as a JSON
    object in config.json. Applications compare it with their own version and
    then refuse new network settings with a clear notice; nothing is installed or
    stopped because of it. A value that does not parse stops the service rather
    than publishing something every application would read wrongly.
    """
    given = getattr(args, "minimum_versions", "") or ""
    if isinstance(given, str):
        pairs = [item.strip() for item in given.split(",") if item.strip()]
        if any(item.count("=") != 1 for item in pairs):
            raise ValueError("minimum_versions takes product=x.y.z entries")
        items = [tuple(part.strip() for part in item.split("=")) for item in pairs]
    elif isinstance(given, dict):
        items = list(given.items())
    else:
        raise ValueError("minimum_versions takes product=x.y.z entries")
    result: dict[str, str] = {}
    for product, version in items:
        if product not in PRODUCTS or product in result:
            raise ValueError("minimum_versions names an unknown or repeated product: " + str(product))
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError("minimum_versions needs x.y.z versions: " + str(product))
        result[product] = version
    return result


def protocol_release(args) -> str:
    """A name for the contract release this network runs, e.g. v2-2026-11; shown by apps."""
    value = str(getattr(args, "protocol_release", "") or "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError("protocol_release takes letters, digits, dot, dash and underscore")
    return value


BUNDLE_NAME = "network_config_bundle.json"


def bundle_path(args) -> Path | None:
    """The one file the deploy step writes, if this install has it.

    Everything this service publishes comes from a deploy: the addresses and
    around twenty-five contract ABIs. On the machine that ran the deploy they
    are a 47 MB Hardhat tree; everywhere else they are this one file, written
    by the deploy as its last step.

    That is what lets the service live away from the chain. It needs no
    validator, no Hardhat, no node_modules -- one file that somebody copied.
    """
    given = str(getattr(args, "bundle_file", "") or "").strip()
    candidate = Path(given) if given else Path(__file__).resolve().parent / BUNDLE_NAME
    return candidate if candidate.is_file() else None


def source_signature(args) -> str:
    """What the current input looks like, so a change can be noticed.

    Size and modified time rather than a hash of 2 MB every thirty seconds.
    A deploy writes a new file; it does not edit one in place to the same
    length at the same second.
    """
    parts = []
    for path in (bundle_path(args), Path(args.deployed_file)):
        if path is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    bundle = bundle_path(args)
    if bundle is not None:
        return build_payload_from_bundle(args, bundle)

    deployed_path = Path(args.deployed_file).resolve()
    blockchain_root = deployed_path.parent
    deployed = read_json(deployed_path)
    artifacts: dict[str, Any] = {}
    for contract_name in ("VaultKeeperRegistry", "GuardianRegistry", "GuardianShareProof"):
        artifact = find_contract_artifact(blockchain_root, contract_name)
        if artifact is not None:
            artifacts[contract_name] = artifact

    # ABI bundle by filename for remote servers (Server1/Server2/Server3) to avoid local
    # dependency on Blockchain/artifacts copy completeness.
    artifact_files: dict[str, Any] = {}
    required_rel = [
        ("AccessControlFT.json", "AccessControlFT.sol/AccessControlFT.json"),
        ("DataVault.json", "DataVault.sol/DataVault.json"),
        ("GuardianRegistry.json", "GuardianRegistry.sol/GuardianRegistry.json"),
        ("VaultKeeperRegistry.json", "VaultKeeperRegistry.sol/VaultKeeperRegistry.json"),
        ("TElGv2.json", "TElGv2.sol/TElGv2.json"),
        ("ZkDkgCoordinatorV2.json", "ZkDkgCoordinatorV2.sol/ZkDkgCoordinatorV2.json"),
        ("GuardianShareProof.json", "GuardianShareProof.sol/GuardianShareProof.json"),
        ("ZkDkgDisputeManager.json", "ZkDkgDisputeManager.sol/ZkDkgDisputeManager.json"),
        ("ReputationManager.json", "ReputationManager.sol/ReputationManager.json"),
        ("StakingManager.json", "StakingManager.sol/StakingManager.json"),
        ("RewardManager.json", "RewardManager.sol/RewardManager.json"),
        ("FileRegistrationSC.json", "FileRegistrationSC.sol/FileRegistrationSC.json"),
        ("VKEscrow.json", "VKEscrow.sol/VKEscrow.json"),
        ("CapacityProofSC.json", "CapacityProofSC.sol/CapacityProofSC.json"),
        ("CapacityCertificationSC.json", "CapacityCertificationSC.sol/CapacityCertificationSC.json"),
        ("CapacityIncrementalBatchSC.json", "CapacityIncrementalBatchSC.sol/CapacityIncrementalBatchSC.json"),
        ("PartitionSnapshotProofSC.json", "PartitionSnapshotProofSC.sol/PartitionSnapshotProofSC.json"),
        ("StorageProofSC.json", "StorageProofSC.sol/StorageProofSC.json"),
        ("StorageReceiptDispute.json", "StorageReceiptDispute.sol/StorageReceiptDispute.json"),
        ("StorageRegistrationTimeout.json", "StorageRegistrationTimeout.sol/StorageRegistrationTimeout.json"),
        ("DeletionReleaseSC.json", "DeletionReleaseSC.sol/DeletionReleaseSC.json"),
        ("ShareVerifier.json", "zkdkg/ShareVerifier.sol/ShareVerifier.json"),
        ("V2PrePartialVerifier.json", "zkdkg/V2PrePartialVerifier.sol/V2PrePartialVerifier.json"),
        ("SharePossessionVerifier.json", "zkdkg/SharePossessionVerifier.sol/SharePossessionVerifier.json"),
        ("GnarkStorageProofVerifierAdapter.json", "GnarkStorageProofVerifierAdapter.sol/GnarkStorageProofVerifierAdapter.json"),
        # The generated source exports `contract Verifier`; publish it under a
        # unique filename so it cannot collide with the partition verifier.
        ("StorageProofGnarkVerifier.json", "StorageProofGnarkVerifier.sol/Verifier.json"),
        ("GnarkPartitionProofVerifierAdapter132.json", "GnarkPartitionProofVerifierAdapter132.sol/GnarkPartitionProofVerifierAdapter132.json"),
        # Discovery reads every keeper and guardian through this one.
        ("NetworkDirectoryReader.json", "NetworkDirectoryReader.sol/NetworkDirectoryReader.json"),
        # Phase 2: the versioned address registry and the verifier router (one ABI for all).
        ("FileTrustAddressRegistry.json", "governance/FileTrustAddressRegistry.sol/FileTrustAddressRegistry.json"),
        ("FileTrustVerifierRouter.json", "FileTrustVerifierRouter.sol/FileTrustVerifierRouter.json"),
    ]
    for filename, rel in required_rel:
        artifact = read_artifact_by_rel(blockchain_root, rel)
        if artifact is not None:
            artifact_files[filename] = artifact
    # Verifier.json can come from multiple folders; prefer 512 profile if present.
    verifier_candidates = [
        "PartitionStorageProofGnarkVerifier512.sol/Verifier.json",
        "PartitionStorageProofGnarkVerifier.sol/Verifier.json",
    ]
    for rel in verifier_candidates:
        artifact = read_artifact_by_rel(blockchain_root, rel)
        if artifact is not None:
            artifact_files["Verifier.json"] = artifact
            break

    required_filenames = [filename for filename, _rel in required_rel] + ["Verifier.json"]
    missing_artifacts = [name for name in required_filenames if name not in artifact_files]

    return assemble(args, str(deployed_path), deployed, artifacts, artifact_files,
                    required_filenames, missing_artifacts)


def build_payload_from_bundle(args, bundle: Path) -> dict[str, Any]:
    """Everything from the file the deploy wrote, with no tree to walk."""
    data = read_json(bundle)
    deployed = data.get("deployed_addresses") or {}
    artifacts = data.get("artifacts") or {}
    artifact_files = data.get("artifact_files") or {}
    # The bundle says what it set out to carry, so a file that was written
    # while a contract was missing still reports that contract as missing
    # here rather than looking complete because nothing asked for it.
    required_filenames = list(data.get("required_artifact_files") or artifact_files.keys())
    missing = [name for name in required_filenames if name not in artifact_files]
    payload = assemble(args, str(bundle), deployed, artifacts, artifact_files,
                       required_filenames, missing)
    payload["network"]["chain_type"] = data.get("chain_type", "")
    payload["explorer_files"] = data.get("explorer_files") or {}
    return payload


def assemble(args, source: str, deployed: dict, artifacts: dict,
             artifact_files: dict, required_filenames: list, missing_artifacts: list
             ) -> dict[str, Any]:
    nodes = rpc_urls(args)
    return {
        "service": "filetrust-network-config",
        "generated_at": now_iso_local(),
        "deployed_file": source,
        "deployed_addresses": deployed,
        "artifacts": artifacts,
        "artifact_files": artifact_files,
        "artifact_bundle_status": {
            "required": len(required_filenames),
            "present": sum(
                1 for filename in required_filenames if filename in artifact_files
            ),
            "missing": missing_artifacts,
        },
        "network": {
            "chain_ip": args.chain_ip,
            "vk_ip": args.vk_ip,
            "discovery_ip": args.discovery_ip,
            "ftu_price_ip": args.ftu_price_ip,
            # Every chain node, best first. rpc_url stays as the first of
            # them: everything written before there was more than one node
            # reads that key, and it must keep meaning what it meant.
            "rpc_urls": nodes,
            "rpc_url": nodes[0],
            "explorer_url": getattr(args, "explorer_url", "") or f"http://{args.chain_ip}:{args.explorer_port}",
            "transaction_dashboard_url": getattr(args, "transaction_dashboard_url", "") or f"http://{args.chain_ip}:{args.transaction_dashboard_port}/reports/transaction_dashboard.html",
            "load_test_dashboard_url": getattr(args, "load_test_dashboard_url", "") or f"http://{args.chain_ip}:{args.load_test_dashboard_port}/",
            "config_server_url": config_server_url(args),
            # The full address of this very endpoint, so a role can write it
            # into its own settings and ask again later without anybody
            # having to type it. One place to change when the service moves.
            "network_config_url": config_server_url(args) + "/v1/network-config",
            # Names, when the services are behind Apache and Cloudflare;
            # addresses otherwise. This is what every Vault Keeper announces
            # to and every Data Owner reads from, so it is the one place that
            # decides whether the network is reached directly or through the
            # front. Published as given: a name here means nothing on the
            # outside ever learns which machine actually answers.
            "discovery_urls": discovery_urls(args),
            "ftu_price_url": ftu_price_url(args),
            # The oldest application each product needs here; see minimum_versions().
            "minimum_application_versions": minimum_versions(args),
            "protocol_release": protocol_release(args),
        },
    }


def hostname_url(value: object) -> bool:
    """A public service URL names a host; it never publishes an origin literal."""
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return False
        host = url.hostname.rstrip(".")
        if not host or any(c.isspace() for c in host):
            return False
        url.port  # Validate port syntax without changing the supplied URL.
        try:
            socket.inet_aton(host)  # Also recognizes integer, octal and hex IPv4.
            return False
        except OSError:
            pass
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        return False
    except ValueError:
        return False



def assert_public_content(value: Any) -> None:
    """Reject private material in response data; errors contain no input values."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if normalized in {"password", "privatekey", "signingkey", "mnemonic",
                              "seedphrase", "apikey", "apitoken", "accesstoken"} and item:
                raise ValueError("Private content is not publishable")
            assert_public_content(str(key))
            assert_public_content(item)
    elif isinstance(value, list):
        for item in value:
            assert_public_content(item)
    elif isinstance(value, str):
        candidates = re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", value)
        candidates += re.findall(r"(?<![\w:])[0-9a-fA-F]*:[0-9a-fA-F:.%]+(?![\w:])", value)
        for candidate in candidates:
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            raise ValueError("IP literals are not publishable")
        if re.search(r"(?:/home/[^/\s]+/|/mnt/[A-Za-z]/)|-----BEGIN [A-Z ]*PRIVATE KEY-----", value):
            raise ValueError("Private infrastructure content is not publishable")
        for candidate in re.findall(r"https?://[^\s\"'<>]+", value):
            parsed = urlsplit(candidate)
            if parsed.username or parsed.password:
                raise ValueError("Credentials in URLs are not publishable")


def public_payload(payload: dict) -> dict:
    """Keep operator addresses private; clients receive named service URLs."""
    internal = payload.get("network", {})
    required = ("rpc_url", "rpc_urls", "discovery_urls", "ftu_price_url", "config_server_url", "network_config_url")
    network = {key: value for key, value in internal.items() if not key.endswith("_ip")}
    for key, value in list(network.items()):
        if not key.endswith(("_url", "_urls")):
            continue
        urls = value if isinstance(value, list) else [value]
        if not urls or not all(hostname_url(url) for url in urls):
            if key in required:
                raise ValueError("Public service URLs must use DNS names")
            del network[key]
    for key in required:
        if key not in network:
            raise ValueError("Public service URLs are incomplete")
    # Publish deployment data, never local filenames or operator wallet metadata.
    public_keys = {"service", "generated_at", "deployed_addresses", "artifacts",
                   "artifact_files", "artifact_bundle_status", "explorer_files"}
    result = {key: value for key, value in payload.items() if key in public_keys}
    deployed = payload.get("deployed_addresses", {})
    deployment_keys = {"generatedAt", "chainId", "contracts", "capabilities",
                       "env", "appConfig", "economics", "addressRegistry"}
    result["deployed_addresses"] = {key: value for key, value in deployed.items()
                                     if key in deployment_keys}
    for section in ("env", "appConfig"):
        values = result["deployed_addresses"].get(section)
        if isinstance(values, dict):
            result["deployed_addresses"][section] = {
                key: value for key, value in values.items()
                if not re.search(r"(?i)treasury|private.?key|password|mnemonic|signing.?key|api.?token", key)
            }
    result["network"] = network
    assert_public_content(result)
    return result


class Handler(BaseHTTPRequestHandler):
    payload: dict[str, Any] = {}
    health_rpc_url: str = ""

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[NetworkConfig] {self.address_string()} - {fmt % args}", flush=True)

    def _rpc_health(self) -> tuple[bool, dict[str, Any]]:
        """Ask every chain node, and say how many answered.

        One validator being down is a number, not an outage: the network
        keeps going and a caller simply uses the next node. Answering "chain
        unavailable" because the first one did not reply would take this
        service down with it, and this service is how anybody finds the
        others.
        """
        nodes = [
            url for url in (self.payload.get("network", {}).get("rpc_urls") or [])
            if str(url).strip()
        ]
        if not nodes:
            nodes = [self.health_rpc_url or
                     str(self.payload.get("network", {}).get("rpc_url", ""))]

        up, block = 0, None
        for url in nodes:
            healthy, details = self._one_rpc_health(url)
            if healthy:
                up += 1
                if block is None:
                    block = details.get("block_number")

        if up:
            return True, {
                "blockchain_rpc": "available",
                "block_number": block,
                "nodes_up": up,
                "nodes_total": len(nodes),
            }
        return False, {
            "blockchain_rpc": "unavailable",
            "nodes_up": 0,
            "nodes_total": len(nodes),
            "error": "Configured blockchain endpoints are unavailable",
        }

    def _one_rpc_health(self, rpc_url: str) -> tuple[bool, dict[str, Any]]:
        try:
            request = urllib.request.Request(
                rpc_url,
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_blockNumber",
                    "params": [],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "FileTrust-role"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                rpc_payload = json.loads(response.read().decode("utf-8"))
                response_status = response.status
            if response_status != 200 or not isinstance(rpc_payload.get("result"), str) or not re.fullmatch(r"0x[0-9a-fA-F]+", rpc_payload["result"]):
                raise RuntimeError("RPC returned no block number")
            return True, {
                "blockchain_rpc": "available",
                "block_number": rpc_payload["result"],
            }
        except Exception as exc:
            return False, {
                "blockchain_rpc": "unavailable",
                "error": "Configured blockchain endpoint is unavailable",
            }

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            healthy, details = self._rpc_health()
            self._send_json(200 if healthy else 503, {
                "ok": healthy,
                "service": "filetrust-network-config",
                **details,
            })
            return
        if self.path in ("/v1/network-config", "/v1/explorer-config", "/v1/bootstrap", "/"):
            healthy, details = self._rpc_health()
            if not healthy:
                self._send_json(503, {
                    "ok": False,
                    "service": "filetrust-network-config",
                    **details,
                })
                return
            try:
                payload = public_payload(self.payload)
            except ValueError:
                self._send_json(503, {"ok": False, "error": "Public configuration is not publishable"})
                return
            if self.path != "/v1/explorer-config":
                payload = {key: value for key, value in payload.items() if key != "explorer_files"}
            self._send_json(200, payload)
            return
        self._send_json(404, {"ok": False, "error": "not_found"})


def settings_from_file(path: Path) -> dict[str, Any]:
    """Your answers, from config.json beside this file.

    install.sh writes them there so the service can be started by systemd
    with no arguments at all. A flag on the command line still wins, which is
    how the launcher scripts have always driven it.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[NetworkConfig] could not read {path.name} ({exc}); using defaults",
              flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    # Section titles, not settings.
    return {k: v for k, v in data.items() if not k.startswith("##")}


def watch_for_new_deploy(args, stop: threading.Event, seconds: int = 30) -> None:
    """Re-read the bundle when a new deploy replaces it.

    The file arrives by a copy from wherever the deploy ran. Nothing tells
    this service that happened, and restarting it by hand after every deploy
    is a step somebody forgets -- after which it serves yesterday's addresses
    and looks perfectly healthy doing it.
    """
    known = source_signature(args)
    while not stop.wait(seconds):
        current = source_signature(args)
        if current == known:
            continue
        try:
            Handler.payload = build_payload(args)
        except Exception as exc:
            print(f"[NetworkConfig] new deploy file is not usable yet ({exc}); keeping the old one",
                  flush=True)
            continue
        known = current
        contracts = Handler.payload.get("deployed_addresses", {}).get("contracts", {})
        print(f"[NetworkConfig] read a new deploy: {len(contracts)} contract(s)", flush=True)


def main() -> int:
    here = Path(__file__).resolve().parent
    default_deployed = here / "deployed_addresses.json"
    stored = settings_from_file(here / "config.json")

    parser = argparse.ArgumentParser(description="Serve FileTrust network bootstrap config JSON.")
    def stored_or(key, fallback):
        value = stored.get(key)
        return fallback if value in (None, "") else value

    parser.add_argument("--host", default=stored_or("host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(stored_or("port", 8989)))
    parser.add_argument("--public-ip", default=stored_or("public_ip", "https://config.example.invalid"))
    parser.add_argument("--deployed-file", default=stored_or("deployed_file", str(default_deployed)))
    parser.add_argument(
        "--bundle-file",
        default=stored_or("bundle_file", ""),
        help=f"The one file a deploy writes. Defaults to {BUNDLE_NAME} beside "
             "this script, which is how a machine with no chain on it serves "
             "the network settings.",
    )
    parser.add_argument(
        "--rpc-urls",
        default=stored_or("rpc_urls", os.environ.get("FILETRUST_RPC_URLS", "")),
        help="Comma-separated chain nodes to publish and to check, best "
             "first, e.g. http://node1:8545,http://node2:8545. One name or "
             "six. Overrides --chain-ip.",
    )
    parser.add_argument("--chain-ip", default=stored_or("chain_ip", "127.0.0.1"))
    parser.add_argument("--vk-ip", default=stored_or("vk_ip", "127.0.0.1"))
    parser.add_argument("--discovery-ip", default=stored_or("discovery_ip", "127.0.0.1"))
    parser.add_argument("--ftu-price-ip", default=stored_or("ftu_price_ip", "127.0.0.1"))
    parser.add_argument("--hardhat-port", type=int, default=int(stored_or("rpc_port", 8545)))
    parser.add_argument("--explorer-port", type=int, default=int(stored_or("explorer_port", 3001)))
    parser.add_argument("--transaction-dashboard-port", type=int,
                        default=int(stored_or("transaction_dashboard_port", 8765)))
    parser.add_argument("--load-test-dashboard-port", type=int,
                        default=int(stored_or("load_test_dashboard_port", 8766)))
    parser.add_argument("--ftu-price-port", type=int, default=int(stored_or("ftu_price_port", 8787)))
    parser.add_argument(
        "--discovery-urls",
        default=stored_or("discovery_urls", os.environ.get("FILETRUST_DISCOVERY_URLS", "")),
        help="Comma-separated discovery URLs to publish, e.g. "
             "https://disc1.filetrust.io,https://disc2.filetrust.io. "
             "Overrides --discovery-ip.",
    )
    parser.add_argument(
        "--ftu-price-url",
        default=stored_or("ftu_price_url", os.environ.get("FILETRUST_FTU_PRICE_URL", "")),
        help="FTU price URL to publish, e.g. https://price.filetrust.io. "
             "Overrides --ftu-price-ip.",
    )
    for name in ("explorer_url", "transaction_dashboard_url", "load_test_dashboard_url"):
        parser.add_argument("--" + name.replace("_", "-"), default=stored_or(name, ""),
                            help="Optional public DNS URL; IP-based links are not published")
    parser.add_argument(
        "--minimum-versions",
        default=stored_or("minimum_versions", os.environ.get("FILETRUST_MINIMUM_VERSIONS", "")),
        help="Oldest application version each product needs on this network, e.g. "
             "do=0.3.0,req=0.3.0. Older apps keep their saved settings and say why.",
    )
    parser.add_argument("--protocol-release", default=stored_or("protocol_release", os.environ.get("FILETRUST_PROTOCOL_RELEASE", "")),
                        help="Name of the contract release this network runs, e.g. v2-2026-11")
    args = parser.parse_args()

    Handler.payload = build_payload(args)
    # The chain used to be on this machine, and health asked 127.0.0.1. On its
    # own server that is always "chain down", so health now asks the nodes it
    # publishes -- the very ones a caller will use.
    Handler.health_rpc_url = ""
    bundle_status = Handler.payload.get("artifact_bundle_status", {})
    missing = bundle_status.get("missing", [])
    present = bundle_status.get("present", 0)
    required = bundle_status.get("required", 0)
    print(
        f"[NetworkConfig] artifact bundle: {present}/{required} files present",
        flush=True,
    )
    if missing:
        print(
            "[NetworkConfig] ERROR incomplete required artifact bundle: "
            + ", ".join(str(name) for name in missing),
            flush=True,
        )
        # Do not publish a bootstrap payload which would let another role start
        # with stale or absent verifier ABIs.  In particular, the generated
        # storage/partition verifiers are part of the required network bundle,
        # even though they are not addresses consumed by every role.
        return 1
    required_runtime_contracts = (
        "AccessControlFT",
        "DataVault",
        "GuardianRegistry",
        "VaultKeeperRegistry",
        "ShareVerifier",
        "V2PrePartialVerifier",
        "SharePossessionVerifier",
        "ZkDkgDisputeManager",
        "ZkDkgCoordinatorV2",
        "GuardianShareProof",
        "TElGv2",
        "ReputationManager",
        "StakingManager",
        "RewardManager",
        "VKEscrow",
        "CapacityProofSC",
        "CapacityCertificationSC",
        "CapacityIncrementalBatchSC",
        "PartitionSnapshotProofSC",
        "StorageProofSC",
        "StorageReceiptDispute",
        "FileRegistrationSC",
        "StorageRegistrationTimeout",
        "DeletionReleaseSC",
    )
    required_runtime_addresses = (
        "StorageProofPartitionVerifier",
        "StorageProofPartitionGeneratedVerifier",
    )
    deployed_contracts = Handler.payload.get("deployed_addresses", {}).get("contracts", {})
    missing_contracts = [
        name
        for name in required_runtime_contracts + required_runtime_addresses
        if not str(deployed_contracts.get(name, "") or "").strip()
    ]
    required_runtime_artifacts = {
        f"{name}.json"
        for name in required_runtime_contracts
        if name not in {"StorageProofSC"}
    }
    required_runtime_artifacts.add("StorageProofSC.json")
    missing_runtime_artifacts = sorted(
        name for name in required_runtime_artifacts
        if name not in Handler.payload.get("artifact_files", {})
    )
    if missing_contracts or missing_runtime_artifacts:
        if missing_contracts:
            print(
                "[NetworkConfig] ERROR missing required deployed contracts: "
                + ", ".join(missing_contracts),
                flush=True,
            )
        if missing_runtime_artifacts:
            print(
                "[NetworkConfig] ERROR missing required runtime artifacts: "
                + ", ".join(missing_runtime_artifacts),
                flush=True,
            )
        return 1
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"[NetworkConfig] listening on http://{args.host}:{args.port} "
        f"(public: {config_server_url(args)})",
        flush=True,
    )
    print(f"[NetworkConfig] chain nodes: {', '.join(rpc_urls(args))}", flush=True)

    stop = threading.Event()
    threading.Thread(target=watch_for_new_deploy, args=(args, stop),
                     daemon=True, name="deploy-watch").start()
    try:
        server.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
