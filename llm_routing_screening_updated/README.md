# LLM Routing — Dual-independent Literature Screening

A hosted Streamlit + Supabase system for an SMS/SLR: XLSX import; Title, Abstract, URL, DOI and metadata display; two independent reviewers; Yes/No/Uncertain voting; resume/edit; admin adjudication; Excel export.

## 1. Create a database

1. Register at https://supabase.com and create a project.
2. In **SQL Editor**, paste the full contents of `schema.sql` and run it once.
3. In **Authentication → Providers → Email**, disable public signup / enable account creation only by admin. Keep email/password authentication enabled.
4. In **Authentication → Users**, use **Add user** to create exactly three accounts: your admin account and two reviewer accounts. Ensure users can log in (confirmed emails or email-confirmed admin-created accounts).
5. In **SQL Editor**, replace `YOUR_ADMIN_EMAIL` with your own account email and execute:

```sql
update public.profiles set role = 'admin' where email = 'YOUR_ADMIN_EMAIL';
```

If users were created before `schema.sql` ran, first backfill:

```sql
insert into public.profiles (id, email, role)
select id, coalesce(email, ''), 'reviewer' from auth.users
on conflict (id) do nothing;
```

For straightforward two-reviewer comparison, make sure **only two accounts** have the `reviewer` role. Admin is separate.

## 2. Configure credentials

In **Supabase → Project Settings → API Keys**, obtain the Project URL, public anon/publishable key, and server-only secret/service_role key.

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`, replace placeholders. NEVER upload that real file to GitHub, email, or screenshots; service role bypasses database row-level security. Streamlit Cloud Secrets is server-side and should only be editable by trusted project maintainers.

## 3. Run locally

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## 4. Deploy a shared HTTPS website

1. Push `app.py`, `schema.sql`, `requirements.txt`, `.gitignore`, `README.md`, and `.streamlit/secrets.toml.example` to a **private GitHub repository**. DO NOT commit your real `secrets.toml`.
2. Visit https://share.streamlit.io and create an app, selecting the repo and `app.py`.
3. In **Advanced settings → Secrets**, paste your three real TOML lines (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`).
4. Deploy and share its resulting `*.streamlit.app` link with the two reviewers. Each signs in with their own email/password.
5. As the admin, open **Import Excel**, upload your file, preview and confirm.

## 5. Excel columns

Accepted headings (case/spacing insensitive):

| Preferred heading | Also accepts |
| --- | --- |
| Item Type | type, document type |
| Publication Year | year |
| Author | authors |
| Title **(required)** | article title, paper title |
| Publication Title | journal, source title, conference, venue |
| Abstract | Abstract Note (Zotero export), summary, 摘要 |
| DOI | digital object identifier |
| Url | URL, Link, URI, 链接 |

DOI strings normalize to bare DOI. Missing URL is filled with `https://doi.org/DOI` when DOI exists. Malformed URL values are cleared. Duplicate DOIs are deduplicated, falling back to normalized title + year if DOI absent. Reimports update metadata and preserve votes. Only the first worksheet is imported; combine files or upload sequentially if necessary.

## 6. Review workflow and outputs

Reviewer A and B see the same list and only their own recorded decisions. They can select **Yes**, **No**, **Uncertain**, optionally add a note, and **Save decision & next**. Each vote is stored in Supabase and remains available after sign out.

- Yes/Yes: Agreed include.
- No/No: Agreed exclude.
- Yes/No: Conflict; full text / adjudication required.
- Either Uncertain: Needs full-text review.
- Missing one/both votes: Awaiting second review.

The administrator sees both votes, adjudicates conflicts and uncertain papers, and exports XLSX with title, abstract, URL, original bibliographic metadata, reviewer decisions, notes, status, and final decision. Direct unanimous Yes/Yes and No/No results are calculated automatically unless explicitly overridden in `resolutions`.

## 7. Important limitations & governance

- This is a deployable source-code package, **not a deployed live website**. Actual activation requires your Supabase/GitHub/Streamlit accounts and credentials.
- Screening decisions are separate from the broader SMS inclusion/exclusion criteria: “related to LLM routing” is a *preliminary relevance vote*. Consider expanding labels or storing full-text exclusion reasons as the study progresses.
- The admin can see and export reviewer identities and responses; reviewers cannot see the other's answer through the app. Supabase row-level security also blocks their direct API reads when using public authentication keys.
- An admin override always takes priority, even after a reviewer changes a vote. Revisit adjudications if you allow vote revisions post-adjudication.
- If you later add more than two reviewer accounts, this release chooses the first two alphabetically by email for pairwise comparison; disable/merge extra reviewer accounts to retain unambiguous comparisons.
- Never share server service-role credentials. Back up your Supabase project / regularly export screening XLSX. Publish an anonymized screening audit trail if required by your research protocol.
- Account registration and deployment are not automated; administrator must follow steps above.

## Recommended official references

- https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
- https://docs.streamlit.io/deploy/concepts/secrets
- https://supabase.com/docs/reference/python/auth-signinwithpassword
- https://supabase.com/docs/reference/python/upsert

## Your uploaded Zotero workbook

The file `LLM Routing.xlsx` uses `Abstract Note`, not `Abstract`. This release maps that column to the in-app **Abstract** field. It also recognizes the source `Url` column and retains the DOI. The workbook contains 4,803 data rows. The original workbook is not included in the ZIP; upload it through the administrator interface to keep your bibliography private.
