# HomePilot ⚡

**Self-hosted home energy monitoring with AI analysis and an MCP server.**

HomePilot helps people understand household energy use through a dashboard and a conversational AI assistant. It also exposes energy tools through the **Model Context Protocol (MCP)**, so compatible MCP clients can discover and invoke them.

> **Demo data:** This prototype uses generated household readings for demonstration. It is not currently receiving measurements from physical smart plugs, electricity meters, or solar inverters. Solar values shown in the demo are modeled data, not verified grid-import/export measurements.

## Alexa+ track

HomePilot is submitted to the **Alexa+ track**. The project exposes energy-analysis tools through an MCP server using Streamable HTTP, and the server can be demonstrated with MCP Inspector.

The MCP endpoint used in local development is:

```text
http://localhost:8000/mcp/v1
```

The repository should contain the actual MCP server implementation and its configuration. The current demo demonstrates the MCP server through MCP Inspector; do not interpret this as a claim that HomePilot has been connected to an Alexa+ device or a live Alexa+ experience unless that integration has separately been implemented and tested.

## What it does

- **Energy dashboard:** Shows current power, daily energy use, estimated electricity cost based on a configurable tariff, historical trends, and device-level breakdowns.
- **AI energy assistant:** Lets users ask questions about available household readings in natural language.
- **Anomaly analysis:** Checks readings for unusual patterns and can report when there is not enough data to draw a reliable conclusion.
- **Savings opportunities:** Surfaces potential energy-saving opportunities based on available data and configured logic.
- **Solar-aware view:** Represents consumption and modeled solar generation separately. Actual net grid import/export is not asserted unless supported by measured data.
- **MCP tools:** Makes supported, household-scoped read-only operations available to compatible MCP clients.
- **Authentication:** Implements an OAuth authorization-code flow with PKCE, token refresh and revocation, and discovery metadata for protected-resource and authorization-server configuration.
- **Self-hosted AI:** Uses Ollama to run a local language model rather than requiring a hosted LLM API for the AI workflow.

## How it works

```text
Generated demo readings
          |
          v
    Local database
          |
          v
     FastAPI backend
       /         \
      v           v
Energy analytics  AI chat service
(anomalies, costs)   + Ollama
       \           /
        v         v
          MCP tool registry
                  |
                  v
       JSON-RPC over Streamable HTTP
                  |
                  v
       MCP-compatible client
          (e.g. MCP Inspector)
```

1. Seeded demo data provides households, devices, and energy readings.
2. The backend serves dashboard data and computes energy summaries and analysis.
3. The AI chat service routes questions to appropriate tools and uses their returned data to compose an answer.
4. The MCP server exposes supported operations over Streamable HTTP.
5. Protected access is handled through the OAuth flow and household-scoped authorization.

## Built with

- **Python** and **FastAPI** — backend and HTTP endpoints
- **Ollama** — local language-model runtime
- **Qwen / DeepSeek-R1** — models used for local experimentation; behavior depends on the configured model
- **Model Context Protocol (MCP)** — external tool interface
- **JSON-RPC** — MCP message format
- **Streamable HTTP** — MCP transport
- **OAuth with PKCE** — authentication flow
- **Local database** — household, device, and energy-reading storage
- **Web frontend** — energy dashboard and AI chat

The MCP request dispatcher and tool registry are custom implementations rather than an MCP SDK integration.

## Requirements

- Python version supported by the project's dependency files
- Node.js and npm if running the web frontend or MCP Inspector
- [Ollama](https://ollama.com/) with a model supported by your configuration
- Git

## Run locally

The exact entry points and script names depend on the repository layout. The commands below show the intended local workflow; use the dependency, environment, and seed-script names present in this repository.

### 1. Clone the repository

```bash
git clone https://github.com/code-cerebrum0/homepilot.git
cd homepilot
```

Repository: https://github.com/code-cerebrum0/homepilot

### 2. Configure and start Ollama

Install Ollama, start it, and pull the model configured for HomePilot. For example, if using Qwen3 4B:

```bash
ollama pull qwen3:4b
```

Make sure the backend configuration uses the same model name. Tool-calling behavior varies between local models, so test the configured model before the demo.

### 3. Install backend dependencies

If the backend is located in `backend/` and includes a `requirements.txt` file:

```bash
cd backend
python -m venv .venv
```

Activate the virtual environment.

**macOS/Linux:**

```bash
source .venv/bin/activate
```

**Windows PowerShell:**

```powershell
.venv\Scripts\Activate.ps1
```

Then install the dependencies:

```bash
pip install -r requirements.txt
```

If this repository uses a different dependency manager or file, follow the files provided by the project instead.

### 4. Configure environment and demo data

Create the required local environment configuration using the variable names documented in the repository. Configure database settings, the Ollama model, and OAuth settings as required by the application.

Run the project's database initialization and seed scripts to create the simulated household, devices, and readings. Use the actual script names in the repository; do not point a demo at real household data unless you have configured and verified that integration.

### 5. Start the backend

From the backend directory, if the FastAPI application entry point is `app.main:app`:

```bash
uvicorn app.main:app --reload --port 8000
```

If the entry point differs, use the module path defined by the project. The MCP endpoint should then be available at:

```text
http://localhost:8000/mcp/v1
```

### 6. Start the frontend

If the frontend is in a `frontend/` directory and uses npm, open a second terminal and run:

```bash
cd frontend
npm install
npm run dev
```

Use the scripts specified in the frontend's `package.json` if they differ.

## Test the MCP server

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) can be used to inspect the server's advertised tools and call them.

1. Keep the HomePilot backend running.
2. Open a second terminal and launch Inspector:

   ```bash
   npx -y @modelcontextprotocol/inspector
   ```

3. Open the local URL printed by the command.
4. Select **Streamable HTTP** as the transport.
5. Enter `http://localhost:8000/mcp/v1` and connect.
6. Complete authentication if prompted and required by your configuration.
7. List the available tools, select a read-only tool, and invoke it.
8. Inspect the actual request and JSON-RPC response.

For the demo video, show a successful live tool invocation rather than only displaying an example request copied into a text editor.

## Authentication and security

HomePilot implements an OAuth authorization-code flow with PKCE, token refresh and revocation, and discovery metadata. Protected operations are designed to remain scoped to the appropriate household.

- Keep client secrets, signing keys, and local environment files out of source control.
- Use example or development credentials only for a local demo.
- Do not expose a development server publicly without reviewing authentication, CORS, network binding, and deployment settings.
- The MCP tools in this prototype are read-only; verify the tool registry and authorization checks before extending them.

## Data and limitations

- The readings included for the demo are generated or seeded data, not live sensor measurements.
- Estimated electricity cost depends on the configured tariff and the available readings.
- Instantaneous watts (W) should not be presented as measured cumulative energy (kWh) without a valid calculation over time.
- Solar generation is modeled in the demo. Actual grid import, export, or self-consumption requires appropriate measured data and timestamps.
- Hardware integrations with smart plugs, electricity meters, solar inverters, or home-automation platforms are planned possibilities, not verified current capabilities.
- Local LLM tool-calling compatibility can differ by model and may require configuration or compatibility handling.

## Tests

Run the test suite using the project's configured test command. If the backend uses `pytest`, a typical command is:

```bash
pytest
```

Check the repository's test configuration for the correct working directory, dependencies, and any environment variables needed for tests. Tests that require a running local LLM may be skipped when that runtime is unavailable.

## Roadmap

- Integrate real energy-monitoring devices and validate readings end to end.
- Improve long-term history and baseline quality for anomaly detection.
- Validate savings estimates against measured consumption and transparent assumptions.
- Expand compatibility testing across local models and MCP clients.
- Explore a real Alexa+ integration path where supported and verify it with an end-to-end test.

## Contributing

Issues and contributions are welcome. When proposing changes, include reproducible steps, expected and actual behavior, and tests where practical. Avoid including personal household data, credentials, or secrets in issues and pull requests.

## License

This project is distributed under the license in the [`LICENSE`](LICENSE) file. If this repository is public, add the full text of the license you choose and confirm that you have the rights to license all included code and assets.
