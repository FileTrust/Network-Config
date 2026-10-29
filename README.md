# FileTrust Network Config

A standalone Python service providing FileTrust contract addresses, ABIs and
public service URLs to Data Owners, Requesters, Guardians, VaultKeepers,
Reporters, Discovery and Explorer. It reads the deployment bundle produced by
the FileTrust blockchain deployment and notices replacements automatically.

It requires Python 3.10 or newer, Bash for installation, and a deployment
bundle. There are no pip dependencies, databases or signing keys to install.
Linux with systemd is needed for the optional managed service.

## Install

```sh
git clone https://github.com/FileTrust/Network-Config.git
cd Network-Config
cp /path/to/your/network_config_bundle.json .
./install.sh
```

The installer copies the versioned `config.default.json` example to your local
`config.json` and checks the deployment bundle before starting a service.
Local settings and deployment bundles are ignored by Git. Review the endpoints
in your settings before installation: the example names the current public
FileTrust services, not a new network you may deploy yourself.

```sh
./install.sh --no-service       # configure without installing a service
./install.sh --user             # install a user service
./install.sh --dry-run          # inspect the installer actions
./install.sh --help
python3 network_config_server.py  # run manually after configuration
```

The default bind is `127.0.0.1:8989`. For a public deployment, put it behind
your own TLS reverse proxy and run it as an unprivileged user. Private proxy,
SSH, DNS-provider and infrastructure configurations are not included.

## The current configuration example

`config.default.json` follows the latest verified public FileTrust bootstrap
configuration and includes the current settings format. Its public DNS URLs
are intentional. Legacy `chain_ip`, `vk_ip`, `discovery_ip` and `ftu_price_ip`
keys contain localhost placeholders; they are never included in responses.
`public_ip` is a historical key name: its value must be the **public DNS URL**
of this configuration service.

Important settings:

| Setting | Purpose |
| --- | --- |
| `host`, `port` | Local listening address and port |
| `public_ip` | Public HTTPS configuration-service URL |
| `rpc_urls` | Comma-separated public RPC DNS URLs, in preference order |
| `discovery_urls` | Comma-separated public Discovery DNS URLs |
| `ftu_price_url` | Public FTU price-service DNS URL |
| `explorer_url` | Optional public Explorer DNS URL |
| `bundle_file` | Deployment bundle path; empty uses the file beside the server |
| `minimum_versions` | Minimum application versions; empty imposes no minimum |
| `protocol_release` | Optional release identifier |

For your own deployment, override the public names:

```sh
./install.sh --host 127.0.0.1 \
  --public-url https://config.example.org \
  --rpc https://rpc1.example.org,https://rpc2.example.org \
  --discovery-urls https://discovery.example.org \
  --price-url https://price.example.org
```

Command-line options override `config.json`. Lines with keys beginning `##`
are explanatory headings. Keep the actual local configuration out of Git.

## The deployment bundle

The matching blockchain source is [FileTrust/FileTrust](https://github.com/FileTrust/FileTrust).
Its deployment tooling writes `network_config_bundle.json`, containing the
contract address map, required ABIs and Explorer source/artifact metadata for
that deployment. Supply the complete bundle from your own reviewed deployment;
no live wallet metadata, deployed account inventory or operational bundle is
committed in this repository.

The bundle must match the deployed contracts and verifier versions. Do not
substitute placeholder addresses or an unrelated deployment. The service
refuses startup if required runtime addresses or artifacts are missing. It
reloads a replaced bundle, preserving the last usable one when a replacement
cannot be read. Replace bundle files atomically when updating a deployment.

The legacy `--deployed-file` option supports a local artifact tree, but a
standalone installation should use the deployment bundle beside the server.
No old `Blockchain/` checkout or monorepo directory layout is required.

## API and privacy

- `GET /v1/network-config`: contract addresses, ABIs and public service URLs.
- `GET /v1/explorer-config`: the same plus Explorer source and compiler metadata.
- `GET /health`: RPC availability and the number of responsive endpoints.

RPC health is checked across the configured endpoints. One unavailable node
is not an outage when another responds. Health failures return a generic error
without upstream addresses or connection details.

Published service URLs must use DNS names. Required IP-based URLs fail closed;
invalid optional dashboard links are omitted. Responses omit operator IP fields,
local deployment filenames, deployer/governance account metadata and treasury
configuration fields. Nested artifacts are also checked for IP literals,
credential fields and local machine paths before publication; unsafe content
returns a generic 503 instead of being exposed. Contract addresses, ABIs,
capabilities and valid Explorer sources retain their contents.

Use public contract addresses in deployment data; do not put account keys,
passwords, internal hostnames or private infrastructure in the bundle. Public
DNS names can still resolve to public IPs; these controls do not conceal DNS
or replace an infrastructure review.

## License and security

FileTrust-owned code uses **FSL-1.1-ALv2**, matching FileTrust and Explorer.
This is Fair Source with an Apache-2.0 future grant, rather than immediate open
source. See [LICENSE](LICENSE), [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
and [SECURITY.md](SECURITY.md).
