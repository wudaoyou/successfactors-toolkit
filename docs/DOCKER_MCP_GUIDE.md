# SuccessFactors Toolkit: Business User Guide

Ask questions about SuccessFactors and save the results on your computer. This guide is for functional consultants and business key users; no programming knowledge is required for everyday use.

## Before you start

Ask your SuccessFactors administrator or IT support to provide these items through your company's approved secure channel:

| What you need | What to confirm |
| --- | --- |
| A company-approved local AI application and Docker Desktop | IT has completed the one-time connection setup below. The supplied setup is for macOS or Linux. |
| The SuccessFactors environment to use | Confirm whether it is a test or production environment and which company ID identifies it. |
| A connection settings file, private key, and certificate | Ask the administrator to prepare the files for your environment, confirm the certificate is registered, and confirm your permitted data access. Do not invent connection values. |
| A local working folder called `sf-toolkit` | This contains the connection files and a `data` folder for your results. |
| Permission to save and convert files | Your AI application needs local file access to create CSV or save copies in other folders. |

> **Keep employee data local.** We recommend local deployment instead of online AI platforms for sensitive HR information. Never paste private keys or connection secrets into chat. A desktop AI application may still send content to a cloud model: ask IT to use a local model and local file processing when company policy requires data to stay inside your controlled environment. Queries still connect to your approved SuccessFactors environment.

## 1. Start your local workspace

Check that Docker Desktop or your IT-managed Docker service is running. Open the AI application configured by IT. The application starts the toolkit through Docker Compose automatically when it connects.

For first-time setup, give IT the **One-time setup** section below. Once configured, you do not need to enter startup commands each time you use the toolkit.

**Ready to continue:** your AI application shows the SuccessFactors connection as available, or responds to the connection check in step 3. The exact settings screen depends on your AI application.

## 2. Confirm the connection files are in place

Ask IT to confirm that the supplied files are in the `credentials` folder inside `sf-toolkit`, using the correct system name. You do not need to open or edit these files.

- `credentials`: connection settings and security files. Keep these private.
- `data`: your query results and exports. This is the folder you use for daily work.

If the files have not been prepared, ask your administrator to complete the one-time setup. You do not need to create a certificate or register an API application yourself.

**Ready to continue:** IT has confirmed the files are in place and that your connection has permission to read the required data.

## 3. Check the connection, then ask a question

Start with:

> Which SuccessFactors environment am I connected to? Tell me the company ID without showing any connection secrets.

Check that this is the environment you intended to use. A configured environment does not yet prove that data access works. Next, use an approved test employee:

> In company demo, check the current job information for employee DEMO001. Tell me whether the request succeeded and how many records were returned. Save the results without displaying employee details in chat.

Replace `demo` and `DEMO001` with your approved company ID and employee identifier. If the identifier is ambiguous, ask the AI to confirm which identifier it needs before querying.

For everyday work, describe **who or what**, **which date**, and **which fields** you need. For example:

> For employee DEMO001 in company demo, find the job title, department, and company effective on 1 September 2026. Confirm which SuccessFactors fields you used. Save the results and tell me where to find the file.

You do not need to write an API query. The AI may need to check the available fields or ask you to clarify their business meaning. Review its interpretation before using the result. Always specify a date or period when you need historical information.

**Ready to continue:** the small query succeeds in the correct environment and the AI reports a saved file. An empty result may mean no matching records; it is not automatically a connection failure.

## 4. Export the result

By default in this setup, original query files are saved under `sf-toolkit/data/mcp` on your computer. Ask for the full local file path when you cannot find a result.

| Format | What to ask for |
| --- | --- |
| JSON | “Keep the original query result as JSON and tell me where it was saved.” |
| XML | “Retrieve the employee's Compound Employee XML and keep the original files.” This requires the correct person identifier. Large results may produce several files. |
| CSV for Excel | “Convert the saved result to CSV. Keep employee IDs as text, including leading zeros. Save it in my sf-toolkit/data folder.” |
| Another format | Name the format and required columns. Availability depends on the AI application's local file tools. |

CSV and other conversions require a file-capable AI application; they are not provided by this connection alone. Ask the AI to convert the saved source file, not reconstruct records from the conversation. When opening CSV in Excel, import employee ID columns as text.

To use a different folder:

> Save a CSV copy in my Reports/SF folder. Do not overwrite an existing file. Tell me the full file path, number of records, and whether all requested records were retrieved.

The AI application needs permission to write to that folder. Original query files remain in `data/mcp`; ask IT if you want to change that default for future queries.

**Check before sharing:** confirm the environment, date range, fields, record count, and whether the query completed. A saved file may contain only part of the requested data. Ask the AI to explain any incomplete result before using it in a report.

### Revealing PII in a saved report

Fields in tiers 1 through the system's `pii_filter_tier` (default 1; all three tiers for a production
system) are tokenized before the AI ever sees them, so a report it writes under `sf-toolkit/data/mcp` contains tokens, not
plaintext. To restore plaintext, run the reveal command from `~/sf-toolkit`
(where `compose.yaml` lives) against the file inside the container, and send
the output to a folder AI tools don't read:

```bash
docker compose run --rm -T mcp \
  python -m successfactors_toolkit.pii_reveal /data/mcp/report.md -o - > ~/Documents/report.md
```

## If something does not work

| What you see | What to do |
| --- | --- |
| SuccessFactors connection unavailable | Check Docker Desktop or your IT-managed Docker service is running, then reopen the AI application. If it still fails, ask IT to check the saved connection setup. |
| Authentication or permission error | Ask your SuccessFactors administrator to check the connection files, certificate validity, and access permissions. Share the error message without secrets or employee data. |
| No matching records | Confirm the environment, employee identifier, effective date, and permitted data scope. |
| CSV or custom-folder save unavailable | Ask IT to enable approved local file tools and access to the destination folder in your AI application. |
| The AI reports a file but you cannot find it | Ask: “Give me the file path on my computer, not the path inside Docker.” Check `sf-toolkit/data/mcp`. |
| Results say `system_invalid` | The environment's settings file is missing `type` or `production`, or has a wrong or misspelled entry, so queries are refused. Ask IT to fix the field named in the message (setup step B). |
| Results say `system_unknown`, `system_required` or `system_unsupported` | The system name is not one of the folders under `credentials/systems`, several systems exist and none was named, or the file's `type` is not supported. Ask the AI to call `list_systems` and name one of the systems it lists. |
| Results say `pii_vault_unavailable` | Ask IT to check the PII vault volume. If no report has been revealed from it yet, IT can remove it with `docker volume rm sf-toolkit-pii-vault` and reopen the AI application; Docker recreates it with the right permissions. Don't remove a vault that has been used — its tokens can't be revealed afterwards. If the detail says the vault is owned by another user, the vault is a host folder mounted into the container, which Docker Desktop reports as root-owned; use the named volume from the setup section instead. |

## Three-minute walkthrough

| Time | Show | Say |
| --- | --- | --- |
| 00:00–00:30 | Local AI application and Docker Desktop | “After IT completes the one-time setup, open these two applications to start.” |
| 00:30–01:00 | Folder names only, with no secrets visible | “Keep the connection files in credentials. Your results go into data.” |
| 01:00–01:30 | Connection check and an approved test employee | “Confirm the environment, then ask a question using the employee, date, and fields you need.” |
| 01:30–02:15 | A saved test result | “The AI saves the result locally. Ask for JSON, original employee XML, or a CSV conversion.” |
| 02:15–03:00 | A CSV copy and its full local path | “Choose a folder and check the record count and completeness before sharing.” |

Use synthetic data for demonstrations. Only show a successful export after checking the actual file; do not present a simulated result as a live query. Live access and file conversion depend on the target environment and AI application.

## One-time setup — for your administrator

The following settings are for the person preparing the workstation. Business users can return to step 1 once this setup has been verified. Prepare the local folders before saving the Compose file. Replace all example connection values with approved settings and test a small query before handing over the workstation.

<details>
<summary>Show connection settings and copyable configuration</summary>

### A. Compose configuration

Start Docker Desktop or your local Docker service with Docker Compose 2.30.0 or newer. Save the following as `~/sf-toolkit/compose.yaml` after creating that folder. Replace `YOUR_UID:YOUR_GID` with the output of `id -u` and `id -g`, for example `501:20`.

```yaml
services:
  mcp:
    image: docker.io/wudaoyou/successfactors-toolkit:v0.5.5
    command: ["python", "-m", "successfactors_toolkit.mcp_server"]
    user: "YOUR_UID:YOUR_GID"
    stdin_open: true
    tty: false
    # Locked down: read-only root filesystem, no capabilities. Writable: /data, /vault, /tmp.
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    environment:
      SYSTEMS_DIR: /credentials/systems
      RESULTS_DIR: /data
      PII_VAULT_DIR: /vault/store
    volumes:
      - type: bind
        source: ./credentials/systems
        target: /credentials/systems
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: ./data
        target: /data
        bind:
          create_host_path: false
      # PII token vault: a named volume, deliberately not a host folder, so
      # host-side AI tools can't read it. Deleting it makes old tokens unrecoverable.
      - type: volume
        source: pii_vault
        target: /vault

volumes:
  pii_vault:
    name: sf-toolkit-pii-vault
```

Settings such as `SF_ALLOWED_HOSTS`, `REQUEST_TIMEOUT` or `RESULTS_RETENTION_DAYS` reach the container only through the service's `environment:` block; there is no env file. Connection settings do not go there: they live in each system's `<name>.json` (step B).

The AI client starts this service with Docker Compose using the AI client connection settings below. Compose automatically obtains the pinned release image when needed. No separate image download, source checkout, Python installation, or local build is required. No network port is exposed.

The image digest pins the exact release used by this configuration. PII
tokenization needs an image built from this release or later. An older
image ignores the PII settings and returns plaintext; check that
`odata_query` results carry `pii_filter_tier`.

### B. Connection files

Create a working directory outside the repository:

```sh
mkdir -p "$HOME/sf-toolkit/credentials/systems/demo" "$HOME/sf-toolkit/data"
chmod 700 "$HOME/sf-toolkit/credentials"
```

Each SuccessFactors environment is a system: one folder under `credentials/systems`. Use this layout. Replace `demo` with a name of your choice (lowercase letters, digits, `_` and `-`) consistently in the folder name and file name.

```text
~/sf-toolkit/
├── compose.yaml
├── credentials/
│   └── systems/
│       └── demo/
│           ├── demo.json
│           ├── private-key.pem
│           └── signing-cert.crt
└── data/
    └── mcp/                      # Created on the first export
```

Place your matching PEM private key and X.509 certificate in this folder using the names shown. Register the certificate with the corresponding SuccessFactors OAuth2 Client Application. The technical user needs access to the target APIs and data. Keep the private key on your machine; do not paste it into chat.

If you need a key pair, see [Connect to SuccessFactors](CONNECT.md#connect-to-successfactors). Use `scripts/generate-keypair.sh`, then register the certificate in SF. Placing files manually does not run the REST upload endpoint's key-pair and expiry validation; verify that the files match and the certificate is valid.

Create `demo.json` with the connection settings. The container mounts
`systems` read-only, so create the file on the host. Set `production` to
`true` for a production environment or `false` for a test environment (without
quotes):

```sh
cat > "$HOME/sf-toolkit/credentials/systems/demo/demo.json" <<'EOF'
{
  "type": "successfactors",
  "production": false,
  "company_id": "demo",
  "host": "your-api-host.sapsf.com",
  "token_url": "https://your-api-host.sapsf.com/oauth/token",
  "client_key": "REPLACE_WITH_OAUTH_CLIENT_API_KEY",
  "user_id": "APIUSER"
}
EOF
```

`company_id` is the SuccessFactors company ID. `host` excludes `https://`; `token_url` includes the scheme and `/oauth/token`. Follow the project's convention that the technical user matches the certificate CN. Optional keys are `odata_version` (`v2` by default, or `v4`), `pii_filter_tier` and `pii_extra_fields`; see [Connect to SuccessFactors](CONNECT.md#2-create-the-system) for the full list. A file without a valid `type` or `production`, or with a misspelled entry, makes the queries refuse the environment with `system_invalid`, naming the field.

A production environment always gets the strictest PII tokenization (tier 3,
names included). A test environment uses its `pii_filter_tier`, default 1
(0 off to 3 most aggressive).

One MCP server can serve several environments this way. For a second
environment `demo2` with its own OAuth client and tier 2, add a folder next to
`demo` with its key, certificate and settings file; no second compose service
is needed:

```sh
mkdir -p "$HOME/sf-toolkit/credentials/systems/demo2"
# Add private-key.pem and signing-cert.crt, then create demo2.json like demo.json,
# with "client_key": "DEMO2_OAUTH_CLIENT_API_KEY" and "pii_filter_tier": 2.
```

The AI then passes `system="demo2"` to reach it; `list_systems` shows both
environments. With one SuccessFactors system, `system` can stay empty.

Set file permissions:

```sh
chmod 600 "$HOME/sf-toolkit/credentials/systems/demo/private-key.pem"
```

This workflow starts MCP directly over stdio. `API_KEY` and `ADMIN_API_KEY` control REST access and are not required here.

### C. AI client connection

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

The UID/GID in `compose.yaml` lets the container read the key and write exports as their owner. Create the credential and data directories first and allow Docker Desktop to share them. The client should discover `list_systems`, `odata_metadata`, `compare_metadata`, `odata_query`, and `ce_query`.


</details>
