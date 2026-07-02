# mu2e Shifter & Operations Assistant

An [Archi](../../README.md) deployment that indexes mu2e shifter and operations
documentation and answers questions in a chat UI, with citations back to the
source docs.

## What's here

| File | Purpose |
|------|---------|
| `config.yaml` | Full deployment config (LLM, vector store, data sources). |
| `agents/mu2e-ops.md` | Agent persona + RAG tools for the assistant. |
| `sources/mu2e_links.list` | URLs / git repos / SSO pages to ingest. |
| `local_docs/` | Drop PDFs / Word / Markdown shifter manuals here. |
| `secrets.env.example` | Template for credentials — copy to `secrets.env`. |

## One-time setup

1. **Credentials**

   ```bash
   cp configs/mu2e/secrets.env.example configs/mu2e/secrets.env
   # edit configs/mu2e/secrets.env: set PG_PASSWORD and CERN_LITELLM_API_KEY
   ```

   `secrets.env` is gitignored; the `.example` is tracked.

2. **LiteLLM gateway** — preconfigured for the Fermilab gateway in `config.yaml`:
   `base_url: https://litellm.fnal.gov/` with `default_model: google/gemma4-31b`.
   Put your gateway key in `CERN_LITELLM_API_KEY` in `secrets.env`. Add any other
   models the gateway serves to the `models:` list.

   > **Gotcha:** the base_url is `https://litellm.fnal.gov/` with **no `/v1`** —
   > the OpenAI-compatible client appends the path itself. Adding `/v1` breaks it.

   The `cern_litellm` provider is just a generic LiteLLM / OpenAI-compatible
   client (not CERN-specific despite the name), so it works with any LiteLLM proxy.

3. **Add document sources** — edit `sources/mu2e_links.list`:
   - `https://…` for wiki / web pages
   - `git-https://…` for MkDocs/Markdown git repos
   - `sso-https://…` for Fermilab SSO-protected pages (see caveat below)

   …and copy any PDFs/manuals into `local_docs/`.

## Deploy

```bash
archi create --name mu2e-ops \
  --config configs/mu2e/config.yaml \
  --env-file configs/mu2e/secrets.env \
  --services chatbot
```

The `chatbot` service auto-enables its `postgres` and `data-manager` dependencies.
Uploading documents is built into the chat app — there is **no separate
`uploader` service** in this build (run `archi list-services` to see what's
deployable).

> **Docker Desktop on macOS:** do **not** pass `--hostmode`. Host network mode
> only reaches `localhost` on Linux; on Docker Desktop for Mac/Windows the app
> runs in a VM and the host network is unreachable, so the chat UI won't load.
> Omit the flag and the default published ports (7861 / 7871) work. `--hostmode`
> is still valid when deploying on a native Linux host.

Then open the chat UI (default `http://localhost:7861`), browse ingested docs at
`/data`, and upload extra files directly from the chat app's upload panel.

Re-run the same `archi create` command after editing sources to re-index.

## Data sources — status

| Source | Configured | Notes |
|--------|-----------|-------|
| Web / wiki pages | ✅ enabled | Add real mu2e URLs to the `.list`. |
| Git / MkDocs repos | ✅ enabled | Prefix repo URLs with `git-`. |
| Local PDFs / Word / MD | ✅ enabled | Files in `local_docs/` are staged in at deploy. |
| Fermilab SSO pages | ⚠️ scaffolded, **off** | See caveat. |

### Fermilab SSO caveat

The bundled `CERNSSOScraper` (Selenium) is written against **CERN's** SSO login
flow. Fermilab's SSO (Services / FNAL SSO) uses a different login form, so it is
unlikely to work unmodified. To enable SSO ingestion you will need to either:

- write a Fermilab-specific Selenium scraper class (mirror
  `src/data_manager/collectors/scrapers/integrations/sso_scraper.py`) and
  reference it under `links.selenium_scraper.selenium_class`, **or**
- export the protected pages another way (e.g. authenticated wget/DocDB export)
  and drop them into `local_docs/`.

Until then, leave `sources.sso.enabled` and `links.selenium_scraper.enabled` at
`false` and rely on the public wiki, git repos, and local files.

## Customizing the assistant

Edit `agents/mu2e-ops.md` — the YAML frontmatter sets the RAG `tools`, and the
Markdown body is the system prompt. Tune tone, escalation guidance, and which
tools are available there.
