# SuccessFactors Toolkit: Docker MCP Guide

Run MCP with Docker, configure credentials, ask questions, and export results.

This guide is for macOS or Linux, local Docker, and an AI client that can launch a stdio MCP server. All values are placeholders. Use a test tenant and synthetic data when recording a demonstration.

> **Data security: prefer local deployment.** We recommend local Docker and a local AI client rather than online AI platforms or third-party hosted MCP services for sensitive employee data and credentials. Keep credentials and exports on your machine. Do not upload private keys or employee payloads to online platforms.
>
> **A local client is not necessarily a local model.** If the client calls a cloud model, prompts, tool responses, previews, and file contents supplied to that model may leave your machine. If HR data must remain within your controlled environment, use a locally hosted model and local file-processing tools, and check the client's outbound data handling. Local Docker alone does not guarantee this. The toolkit still connects to your configured SuccessFactors tenant to query data.

## What the current version supports

| Action | How it works |
| --- | --- |
| Start MCP with Docker | The AI client starts `docker compose run --rm -T mcp` using the Compose file below. |
| Configure credentials | Store connection settings in an environment file and PEM keys and certificates in a designated folder. Make them available through Docker. |
| Ask questions | The AI client calls tools for tenants, metadata, OData, and Compound Employee. |
| Export JSON or XML | OData queries write JSON; Compound Employee writes one XML file per page. |
| Export CSV or other formats | Requires local file access and conversion tools in the AI client. This MCP has no generic format-conversion tool. |
| Save under data | This guide sets `RESULTS_DIR=/data`, placing raw files in the host's `data/mcp/` folder. The unconfigured program default is `results/mcp/`. |
| Request another folder in chat | The AI client saves or converts files in an authorized local folder. MCP query tools have no per-call destination argument. |

## 1. Set up Docker Compose

Start Docker Desktop or your local Docker service with Docker Compose 2.30.0 or newer. Save the following as `~/sf-toolkit/compose.yaml` after creating that folder. Replace `YOUR_UID:YOUR_GID` with the output of `id -u` and `id -g`, for example `501:20`.

```yaml
services:
  mcp:
    image: docker.io/wudaoyou/successfactors-toolkit@sha256:80a51e6917fed4508aad558803a2d7d9dc02d0b303f301ecb356f07baa73647a
    command: ["python", "-m", "successfactors_toolkit.mcp_server"]
    user: "YOUR_UID:YOUR_GID"
    stdin_open: true
    tty: false
    env_file:
      - path: ./credentials/sf.env
        format: raw
    environment:
      TENANT_KEYS_DIR: /credentials/tenants
      RESULTS_DIR: /data
    volumes:
      - type: bind
        source: ./credentials/tenants
        target: /credentials/tenants
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: ./data
        target: /data
        bind:
          create_host_path: false
```

The AI client starts this service with Docker Compose in step 3. Compose automatically obtains the pinned release image when needed. No separate image download, source checkout, Python installation, or local build is required. No network port is exposed.

The image digest pins the exact release used by this configuration.

## 2. Configure credentials

Create a working directory outside the repository:

```sh
mkdir -p "$HOME/sf-toolkit/credentials/tenants/demo" "$HOME/sf-toolkit/data"
chmod 700 "$HOME/sf-toolkit/credentials"
```

Use this layout. Replace `demo` with your actual company ID consistently in folder names, filenames, and configuration.

```text
~/sf-toolkit/
├── compose.yaml
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

Replace `/Users/YOUR_NAME/sf-toolkit` below with your actual absolute path (usually `/home/YOUR_NAME/sf-toolkit` on Linux). JSON does not expand `~`. Add this configuration to your AI client's MCP settings:

```json
{
  "mcpServers": {
    "successfactors": {
      "command": "docker",
      "args": [
        "compose",
        "-f",
        "/Users/YOUR_NAME/sf-toolkit/compose.yaml",
        "run",
        "--rm",
        "-T",
        "mcp"
      ]
    }
  }
}
```

Reload the client's MCP configuration. It starts `docker compose run --rm -T mcp` and communicates over stdin/stdout. Keep `-T` to disable a terminal; do not add `-d`. Do not start this stdio service with `docker compose up`. If Docker cannot be found, use the absolute executable path from `command -v docker`.

The UID/GID in `compose.yaml` lets the container read the key and write exports as their owner. Create the credential and data directories first and allow Docker Desktop to share them. The client should discover `list_tenants`, `odata_metadata`, `compare_metadata`, `odata_query`, and `ce_query`.

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
2. **Change the destination for future raw exports:** create the new host folder, change the output volume's `source: ./data` in `compose.yaml` to its absolute path, and restart MCP. Keep `target: /data` and `RESULTS_DIR=/data`. Raw files then appear in the new folder's `mcp/` subdirectory.

Tool responses contain container paths. Client-side file tools must translate them using the mount mapping; do not assume `/data` exists on the host.

Before reporting a complete export, check OData `stopped_reason`: `exhausted` means pagination finished; `max_pages` means the query hit its page limit. For Compound Employee, check for errors and inspect `truncated`. An existing file does not prove the entire query succeeded.

## 6. Three-minute recording script

| Time | On screen | Narration |
| --- | --- | --- |
| 00:00–00:15 | Title, four-step workflow, and local-deployment recommendation | “Employee data is sensitive. We recommend local deployment instead of uploading it to online AI platforms. Here is how to start Docker MCP, configure credentials, ask questions, and export files.” |
| 00:15–00:40 | Show compose.yaml with the pinned release image and local folder mounts. | “Save the Compose configuration. Your AI agent uses Docker Compose to start MCP locally; Compose handles the image automatically.” |
| 00:40–01:10 | Example folder tree and sf.env containing placeholders only | “Store connection settings under credentials and place your key and certificate in the tenant folder. Keep real credentials on your computer. The data folder holds query results.” |
| 01:10–01:35 | MCP configuration with paths and UID/GID filled in; five tools visible | “Add the Docker MCP configuration. Credentials are mounted read-only, and the data folder accepts output files. Reload the configuration to make the five tools available.” |
| 01:35–02:00 | List tenants, then query a demonstration record | “Confirm the tenant and describe what you want to query, such as an employee's current job information. The AI calls MCP and returns the record count and file location.” |
| 02:00–02:25 | Open local data/mcp and show synthetic output files | “OData results are saved as JSON. Compound Employee results are saved as XML. This configuration places them in the mcp subfolder under your local data directory.” |
| 02:25–02:50 | Enter the CSV conversion prompt and show the actual generated CSV | “With local file-processing tools, your AI client can convert results to CSV or other supported formats. You can also ask it to save a copy in a folder you choose.” |
| 02:50–03:00 | Final file path, record count, and pagination status | “Check the file location, record count, and pagination status. After this one-time setup, you can query and export using natural language.” |

Record the CSV segment only after client-side file processing works. Otherwise, show the example prompt without presenting a simulated result as a successful export.

Opening caption: “A local client may still send content to a cloud model. Use a locally hosted model and local file-processing tools when HR data must remain within your controlled environment.”

## 7. Before recording

- Start the service through Docker Compose; confirm MCP initialization and discovery of all five tools in the target AI client.
- Confirm that `list_tenants` identifies the intended tenant and a small live metadata or query request succeeds.
- Open the generated file under local `data/mcp/` and check its format. Make sure pagination status matches the narration.
- If demonstrating CSV or a custom folder, verify the actual file exists and its fields and record count match the source.
- Show only synthetic data and placeholder settings. Do not reveal real private keys, client keys, or employee details.

Implementation references: `Dockerfile`, `docker-compose.mcp.yml`, `successfactors_toolkit/config.py`, `successfactors_toolkit/mcp_server.py`, and the credentials and tenant-store services.

The Compose segment requires the verified release image to be available. Live SF access, client-side conversion, and the custom-folder workflow must also be checked in the target environment before recording. Local tests do not establish that an image has been published.
