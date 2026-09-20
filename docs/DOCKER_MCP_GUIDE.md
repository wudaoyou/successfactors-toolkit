# SuccessFactors Toolkit: Docker MCP Guide

Run MCP with Docker, configure credentials, ask questions, and export results.

This guide is for macOS or Linux, local Docker, and an AI client that can launch a stdio MCP server. All values are placeholders. Use a test tenant and synthetic data when recording a demonstration.

> **Data security: prefer local deployment.** We recommend local Docker and a local AI client rather than online AI platforms or third-party hosted MCP services for sensitive employee data and credentials. Keep credentials and exports on your machine. Do not upload private keys or employee payloads to online platforms.
>
> **A local client is not necessarily a local model.** If the client calls a cloud model, prompts, tool responses, previews, and file contents supplied to that model may leave your machine. If HR data must remain within your controlled environment, use a locally hosted model and local file-processing tools, and check the client's outbound data handling. Local Docker alone does not guarantee this. The toolkit still connects to your configured SuccessFactors tenant to query data.

## What the current version supports

| Action | How it works |
| --- | --- |
| Start MCP with Docker | Run `python -m successfactors_toolkit.mcp_server` in the project image. The existing `docker compose up` starts the REST API, not MCP. |
| Configure credentials | Store connection settings in an environment file and PEM keys and certificates in a designated folder. Make them available through Docker. |
| Ask questions | The AI client calls tools for tenants, metadata, OData, and Compound Employee. |
| Export JSON or XML | OData queries write JSON; Compound Employee writes one XML file per page. |
| Export CSV or other formats | Requires local file access and conversion tools in the AI client. This MCP has no generic format-conversion tool. |
| Save under data | This guide sets `RESULTS_DIR=/data`, placing raw files in the host's `data/mcp/` folder. The unconfigured program default is `results/mcp/`. |
| Request another folder in chat | The AI client saves or converts files in an authorized local folder. MCP query tools have no per-call destination argument. |

## 1. Download the Docker image

Start Docker Desktop or your local Docker service. Open the
[v0.1.1 release](https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.1.1)
and copy the exact image reference from its `image-reference.txt` asset.
Replace the digest placeholder below with that release's SHA-256 digest.
Install the [GitHub CLI](https://cli.github.com/) and sign in with
`gh auth login`. If registry authentication is requested, use `docker login`
with your Docker Hub account. Verify the signed build provenance before
pulling or mounting credentials:

```sh
IMAGE='docker.io/wudaoyou/successfactors-toolkit@sha256:REPLACE_WITH_RELEASE_DIGEST'
gh attestation verify "oci://$IMAGE" \
  --repo wudaoyou/successfactors-toolkit \
  --signer-workflow wudaoyou/successfactors-toolkit/.github/workflows/release.yml \
  --source-ref refs/tags/v0.1.1 && docker pull "$IMAGE"
```

Continue only if verification succeeds. Use the same verified digest reference
in the AI agent configuration below; JSON does not expand `$IMAGE`. The digest
pins the exact image even if a tag changes. A signature establishes the build's
origin; it does not guarantee that the software has no vulnerabilities.

No source checkout, Python installation, or local image build is needed.
The release targets Intel/AMD (`linux/amd64`) and Apple Silicon (`linux/arm64`).
Images are downloaded from [Docker Hub](https://hub.docker.com/r/wudaoyou/successfactors-toolkit).

Your AI agent launches the downloaded image locally. Downloading an image
from Docker Hub does not host your MCP on Docker Hub or upload your mounted
credentials and exports there. The local-versus-cloud-model guidance above
still applies.

The AI agent will start the MCP container using the configuration below.
You do not need to start the REST API or expose port 8000.

Use `python -m` inside this image: the Dockerfile installs dependencies and
copies source files, but does not install the project's `successfactors-mcp`
console command.

### Developer fallback: build locally

When developing the toolkit, run this from the repository directory:

```sh
docker build -t successfactors-toolkit:local .
```

For this fallback only, replace the digest reference in
the client configuration with `successfactors-toolkit:local`.

## 2. Configure credentials

Create a working directory outside the repository:

```sh
mkdir -p "$HOME/sf-toolkit/credentials/tenants/demo" "$HOME/sf-toolkit/data"
chmod 700 "$HOME/sf-toolkit/credentials"
```

Use this layout. Replace `demo` with your actual company ID consistently in folder names, filenames, and configuration.

```text
~/sf-toolkit/
├── credentials/
│   ├── sf.env
│   └── tenants/
│       └── demo/
│           ├── sf_private_key_demo.pem
│           └── sf_saml_signing_demo.crt
└── data/
    └── mcp/                      # Created on the first export
```

Place your matching PEM private key and X.509 certificate in this folder using the names shown. Register the certificate with the corresponding SuccessFactors OAuth2 Client Application. The technical user needs access to the target APIs and data. Keep the private key on your machine; do not paste it into chat.

If you need a key pair, see [Connect to SuccessFactors](../README.md#connect-to-successfactors). Use `scripts/generate-keypair.sh`, then register the certificate in SF. Placing files manually does not run the REST upload endpoint's key-pair and expiry validation; verify that the files match and the certificate is valid.

Create `~/sf-toolkit/credentials/sf.env` with your connection settings:

```dotenv
SF_HOST=your-api-host.sapsf.com
SF_CLIENT_KEY=REPLACE_WITH_OAUTH_CLIENT_API_KEY
SF_USER_ID=APIUSER
SF_COMPANY_ID=demo
SF_TOKEN_URL=https://your-api-host.sapsf.com/oauth/token
SF_ODATA_VERSION=v2
TENANT_KEYS_DIR=/credentials/tenants
RESULTS_DIR=/data
```

`SF_HOST` excludes `https://`; `SF_TOKEN_URL` includes the scheme and `/oauth/token`. Follow the project's convention that the technical user matches the certificate CN. Enter literal values in the environment file; do not rely on `$VARIABLE` expansion.

Set file permissions:

```sh
chmod 600 "$HOME/sf-toolkit/credentials/sf.env" \
  "$HOME/sf-toolkit/credentials/tenants/demo/sf_private_key_demo.pem"
```

This workflow starts MCP directly over stdio. `API_KEY` and `ADMIN_API_KEY` control REST access and are not required here.

## 3. Connect your AI client to Docker MCP

Find your local user and group IDs:

```sh
id -u
id -g
```

Add this configuration to a client that accepts `mcpServers` JSON. For other clients, enter the same command and arguments in their MCP configuration interface.

Replace `/Users/YOUR_NAME/sf-toolkit` with your actual absolute path; on Linux it is typically `/home/YOUR_NAME/sf-toolkit`. Replace `YOUR_UID:YOUR_GID` with the two IDs above, such as `501:20`. JSON arguments do not execute `$(id -u)` or expand `~`.

```json
{
  "mcpServers": {
    "successfactors": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--user", "YOUR_UID:YOUR_GID",
        "--env-file", "/Users/YOUR_NAME/sf-toolkit/credentials/sf.env",
        "--mount", "type=bind,source=/Users/YOUR_NAME/sf-toolkit/credentials/tenants,target=/credentials/tenants,readonly",
        "--mount", "type=bind,source=/Users/YOUR_NAME/sf-toolkit/data,target=/data",
        "docker.io/wudaoyou/successfactors-toolkit@sha256:REPLACE_WITH_RELEASE_DIGEST",
        "python", "-m", "successfactors_toolkit.mcp_server"
      ]
    }
  }
}
```

The local UID/GID lets the container read your private key and write exports as the file owner without loosening private-key permissions. Create the directories first and allow Docker Desktop to share them.

Reload your client's MCP configuration. The client launches `docker run` and communicates over standard input and output: keep `-i` and do not add `-t` or `-d`. If a desktop client cannot find Docker, replace `command` with the absolute path returned by `command -v docker`.

The connected client should discover five tools: `list_tenants`, `odata_metadata`, `compare_metadata`, `odata_query`, and `ce_query`.

## 4. Ask questions

First, confirm the local configuration:

> List the tenants configured for this SuccessFactors MCP and tell me the default company ID. Do not display credential contents.

A successful tenant listing verifies MCP and local configuration, not SF authentication or query permissions. Follow it with a small live request:

> Check EmpJob metadata in tenant demo. Tell me whether userId and jobCode exist.

Then specify the tenant, object, and scope for your business question:

> Query the current EmpJob records for userId DEMO001 in tenant demo. Select only userId, startDate, jobCode, and company. Save the results and tell me the record count and file location. Do not display employee details in chat.

`demo` and `DEMO001` are placeholders. For historical data, specify the date range; effective-dated entities otherwise usually return the current time slice.

Request a small preview explicitly when you need to inspect values; `preview` is capped at 20 records and 16 KiB serialized UTF-8. Record counts outside 0–20 are rejected before querying. When a valid request exceeds the byte limit, the full results stay in the saved file and the response includes `preview_error`. By default, MCP stores records on disk and returns counts and file paths.

## 5. Export files and choose a folder

### JSON: native OData output

> Keep the EmpJob query results as JSON. Confirm whether pagination finished and tell me the total record count and the file's location on my computer.

OData queries already write JSON. A returned path such as `/data/mcp/odata_query_…json` maps to `~/sf-toolkit/data/mcp/odata_query_…json` on your computer. Filenames are generated automatically.

### XML: native Compound Employee output

> Query Compound Employee for person_id_external DEMO001 in tenant demo. Preserve the original XML. Report the page count, whether the results were truncated, and each file's location on my computer.

Each page is saved separately; the tool does not merge pages into one XML file. `person_id_external` and `userId` are different identifiers. Use the identifier appropriate to your request.

### CSV: conversion by a file-capable AI client

Give your AI client access to the local `~/sf-toolkit/data` folder and tools that can execute conversion code or perform equivalent file processing. Connecting this MCP alone does not let a chat client read or convert local files.

Example request:

> Convert the JSON file from the previous query to CSV with columns userId, startDate, jobCode, and company. Use UTF-8, preserve leading zeros in IDs, and correctly escape commas, quotes, and newlines. Save it to my sf-toolkit/data/empjob.csv without overwriting an existing file. Report the full path, record count, and whether the source query was complete.

Convert the actual saved file rather than reconstructing records from a chat summary. For nested arrays or multiple historical records, define what each CSV row represents. When importing CSV into Excel, treat ID columns as text to preserve leading zeros.

Other formats depend on the client's conversion capabilities and the target format's requirements. MCP does not natively support arbitrary formats. Custom XML converted from OData JSON is not the original SAP Compound Employee XML.

### Choose another folder

There are two options:

1. **Save a copy for this request:** ask the AI to save the CSV to `/Users/YOUR_NAME/Reports/SF/empjob.csv`. The client writes to an authorized host folder; the raw MCP file remains under `data/mcp/`.
2. **Change the destination for future raw exports:** create the new host folder, change the output mount's `source=…/data` to its absolute path, and restart MCP. Keep `target=/data` and `RESULTS_DIR=/data`. Raw files then appear in the new folder's `mcp/` subdirectory.

Tool responses contain container paths. Client-side file tools must translate them using the mount mapping; do not assume `/data` exists on the host.

Before reporting a complete export, check OData `stopped_reason`: `exhausted` means pagination finished; `max_pages` means the query hit its page limit. For Compound Employee, check for errors and inspect `truncated`. An existing file does not prove the entire query succeeded.

## 6. Three-minute recording script

| Time | On screen | Narration |
| --- | --- | --- |
| 00:00–00:15 | Title, four-step workflow, and local-deployment recommendation | “Employee data is sensitive. We recommend local deployment instead of uploading it to online AI platforms. Here is how to start Docker MCP, configure credentials, ask questions, and export files.” |
| 00:15–00:40 | Docker running; pull a verified release tag from Docker Hub. Edit out the download wait. | “Download the published Docker image. You do not need to build it yourself. Your AI agent starts the MCP service locally using this image.” |
| 00:40–01:10 | Example folder tree and sf.env containing placeholders only | “Store connection settings under credentials and place your key and certificate in the tenant folder. Keep real credentials on your computer. The data folder holds query results.” |
| 01:10–01:35 | MCP configuration with paths and UID/GID filled in; five tools visible | “Add the Docker MCP configuration. Credentials are mounted read-only, and the data folder accepts output files. Reload the configuration to make the five tools available.” |
| 01:35–02:00 | List tenants, then query a demonstration record | “Confirm the tenant and describe what you want to query, such as an employee's current job information. The AI calls MCP and returns the record count and file location.” |
| 02:00–02:25 | Open local data/mcp and show synthetic output files | “OData results are saved as JSON. Compound Employee results are saved as XML. This configuration places them in the mcp subfolder under your local data directory.” |
| 02:25–02:50 | Enter the CSV conversion prompt and show the actual generated CSV | “With local file-processing tools, your AI client can convert results to CSV or other supported formats. You can also ask it to save a copy in a folder you choose.” |
| 02:50–03:00 | Final file path, record count, and pagination status | “Check the file location, record count, and pagination status. After this one-time setup, you can query and export using natural language.” |

Record the CSV segment only after client-side file processing works. Otherwise, show the example prompt without presenting a simulated result as a successful export.

Opening caption: “A local client may still send content to a cloud model. Use a locally hosted model and local file-processing tools when HR data must remain within your controlled environment.”

## 7. Before recording

- Verify the published tag is available and pull it successfully; confirm MCP initialization and discovery of all five tools in the target AI client.
- Confirm that `list_tenants` identifies the intended tenant and a small live metadata or query request succeeds.
- Open the generated file under local `data/mcp/` and check its format. Make sure pagination status matches the narration.
- If demonstrating CSV or a custom folder, verify the actual file exists and its fields and record count match the source.
- Show only synthetic data and placeholder settings. Do not reveal real private keys, client keys, or employee details.

Implementation references: `Dockerfile`, `docker-compose.yml`, `successfactors_toolkit/config.py`, `successfactors_toolkit/mcp_server.py`, and the credentials and tenant-store services.

The Docker Hub download segment requires a successfully published and verified tag. Live SF access, client-side conversion, and the custom-folder workflow must also be checked in the target environment before recording. Local tests do not establish that an image has been published.
