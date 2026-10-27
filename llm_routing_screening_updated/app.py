"""LLM Routing dual-reviewer literature screening, Streamlit + Supabase."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import io
import re
from collections import Counter
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from supabase import create_client

st.set_page_config(page_title='LLM Routing | Literature Screening', page_icon='📚', layout='wide')

FIELDS = ['item_type', 'publication_year', 'author', 'title', 'publication_title', 'abstract', 'doi', 'url']
ALIASES = {
    'item_type': ['item type', 'itemtype', 'type', 'document type'],
    'publication_year': ['publication year', 'year', 'published year'],
    'author': ['author', 'authors', 'creator'],
    'title': ['title', 'article title', 'paper title'],
    'publication_title': ['publication title', 'journal', 'source title', 'booktitle', 'conference', 'venue'],
    'abstract': ['abstract', 'abstract note', 'abstractnote', 'summary', '摘要'],
    'doi': ['doi', 'digital object identifier'],
    'url': ['url', 'uri', 'link', '链接', 'website'],
}


def client(key: str):
    return create_client(st.secrets['SUPABASE_URL'], st.secrets[key])


@st.cache_resource
def service():
    # Shared service client is used only for table operations, never user authentication.
    # The key remains on the Streamlit server.
    return client('SUPABASE_SERVICE_ROLE_KEY')


def canonical_header(value):
    return re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', str(value).strip().lower())


def clean(value):
    if pd.isna(value):
        return ''
    return str(value).strip()


def normalize_doi(value):
    v = clean(value)
    v = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', v, flags=re.I)
    v = re.sub(r'^doi:\s*', '', v, flags=re.I)
    return v.strip().lower()


def fingerprint(title, year, doi):
    norm_doi = normalize_doi(doi)
    if norm_doi:
        base = 'doi:' + norm_doi
    else:
        base = 'title:' + re.sub(r'\W+', '', title.lower(), flags=re.UNICODE) + '|year:' + year
    return hashlib.sha256(base.encode('utf-8')).hexdigest()


def valid_http_url(u):
    if not u:
        return False
    parsed = urlparse(u)
    return parsed.scheme in ('https', 'http') and bool(parsed.netloc)


def parse_xlsx(data: bytes):
    frame = pd.read_excel(io.BytesIO(data), dtype=str).fillna('')
    normalized = {canonical_header(c): c for c in frame.columns}
    mapping = {}
    for target, aliases in ALIASES.items():
        found = next((normalized[canonical_header(a)] for a in aliases if canonical_header(a) in normalized), None)
        if found is not None:
            mapping[target] = found
    if 'title' not in mapping:
        raise ValueError('Missing Title column. Found: ' + ', '.join(map(str, frame.columns)))
    records = []
    ignored = 0
    for _, row in frame.iterrows():
        rec = {f: clean(row[mapping[f]]) if f in mapping else '' for f in FIELDS}
        if not rec['title']:
            ignored += 1
            continue
        rec['doi'] = normalize_doi(rec['doi'])
        if rec['url'] and not valid_http_url(rec['url']):
            rec['url'] = ''
        if not rec['url'] and rec['doi']:
            rec['url'] = 'https://doi.org/' + rec['doi']
        rec['fingerprint'] = fingerprint(rec['title'], rec['publication_year'], rec['doi'])
        records.append(rec)
    # Collapse duplicates, preferentially preserving the richest metadata (especially abstract and URL).
    unique = {}
    for r in records:
        key = r['fingerprint']
        if key not in unique:
            unique[key] = r
        else:
            previous = unique[key]
            for field in FIELDS:
                if not previous.get(field) and r.get(field):
                    previous[field] = r[field]
                elif field == 'abstract' and len(r.get(field, '')) > len(previous.get(field, '')):
                    previous[field] = r[field]
    return list(unique.values()), {'sheet_rows': len(frame), 'skipped_blank_titles': ignored, 'duplicates_in_file': len(records) - len(unique), 'columns': mapping, 'missing_abstract': sum(not x.get('abstract') for x in unique.values()), 'missing_url': sum(not x.get('url') for x in unique.values())}


def fetch_all(table, cols='*', page_size=500):
    db = service()
    out = []
    start = 0
    while True:
        rows = db.table(table).select(cols).range(start, start + page_size - 1).execute().data or []
        out.extend(rows)
        if len(rows) < page_size:
            break
        start += page_size
    return out



@st.cache_data(ttl=300, show_spinner=False)
def load_papers():
    # Avoid downloading ~4,800 paper records every time a reviewer clicks Next.
    return sorted(fetch_all('papers'), key=lambda x: (x.get('publication_year') or '', x.get('title') or '', x['id']))


def invalidate_papers_cache():
    load_papers.clear()


def load_reviewer_votes(uid):
    # A private session-local cache: NEVER shared across reviewers.
    key = 'my_votes_cache'
    if st.session_state.get('my_votes_uid') != uid or key not in st.session_state:
        all_votes = []
        db = service()
        offset = 0
        while True:
            batch = (db.table('votes').select('paper_id,decision,note')
                     .eq('reviewer_id', uid).range(offset, offset + 499).execute().data or [])
            all_votes.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
        st.session_state[key] = {v['paper_id']: v for v in all_votes}
        st.session_state['my_votes_uid'] = uid
    return st.session_state[key]

def save_vote_to_db(record):
    """Worker: success only after Supabase confirms the write; exceptions propagate."""
    service().table('votes').upsert(record, on_conflict='paper_id,reviewer_id').execute()
    return True


def vote_queue():
    """One worker per browser session ensures edits to the same paper retain order."""
    if 'vote_executor' not in st.session_state:
        st.session_state['vote_executor'] = ThreadPoolExecutor(max_workers=1)
    return st.session_state['vote_executor']


def sync_queued_votes():
    """Collect completed results, never mislabel unconfirmed writes as saved."""
    queue = st.session_state.setdefault('queued_votes', {})
    votes = st.session_state.get('my_votes_cache', {})
    for pid, item in list(queue.items()):
        f = item['future']
        if not f.done():
            continue
        try:
            f.result()
        except Exception as exc:
            item['error'] = str(exc)
        else:
            votes[pid] = item['vote']
            del queue[pid]
    return queue


def schedule_vote(record):
    queue = st.session_state.setdefault('queued_votes', {})
    pid = record['paper_id']
    # Don't silently overwrite an in-flight queue entry; callers can retry later.
    if pid in queue and not queue[pid]['future'].done():
        return False
    future = vote_queue().submit(save_vote_to_db, record.copy())
    queue[pid] = {'future': future,
                  'vote': {'paper_id': pid, 'decision': record['decision'], 'note': record['note']},
                  'record': record.copy(), 'error': None}
    return True


def auth_user():
    token = st.session_state.get('token')
    if not token:
        return None
    auth = client('SUPABASE_ANON_KEY')
    try:
        user = auth.auth.get_user(token).user
    except Exception:
        try:
            refresh = auth.auth.refresh_session(st.session_state['refresh_token'])
            st.session_state['token'] = refresh.session.access_token
            st.session_state['refresh_token'] = refresh.session.refresh_token
            user = auth.auth.get_user(st.session_state['token']).user
        except Exception:
            st.session_state.pop('token', None)
            st.session_state.pop('refresh_token', None)
            return None
    return user


def login():
    st.title('Literature Screening')
    st.caption('LLM Routing for Software Engineering · Independent screening')
    with st.form('login'):
        email = st.text_input('Email')
        password = st.text_input('Password', type='password')
        submitted = st.form_submit_button('Sign in', type='primary')
    if submitted:
        try:
            result = client('SUPABASE_ANON_KEY').auth.sign_in_with_password({'email': email.strip(), 'password': password})
            st.session_state['token'] = result.session.access_token
            st.session_state['refresh_token'] = result.session.refresh_token
            st.rerun()
        except Exception:
            st.error('Unable to sign in. Check your account credentials or ask the administrator.')
    st.info('Accounts are created by the project administrator. Public registration is disabled.')


def role_for(uid):
    res = service().table('profiles').select('role,email').eq('id', uid).limit(1).execute().data
    return res[0] if res else {'role': 'reviewer', 'email': ''}


def page_header(profile):
    a, b = st.columns([5, 1])
    a.title('LLM Routing Literature Screening')
    a.caption(f"Signed in: {profile['email']}  ·  {profile['role'].title()}")
    queued = st.session_state.get('queued_votes', {})
    if b.button('Sign out', disabled=bool(queued), help='Confirm or retry pending saves before signing out.'):
        for k in ('token', 'refresh_token', 'paper_idx', 'paper_id', 'my_votes_cache', 'my_votes_uid', 'jump_number'):
            st.session_state.pop(k, None)
        st.rerun()


@st.fragment
def reviewer_view(uid):
    papers = load_papers()
    if not papers:
        st.info('No papers have been imported yet. Ask the administrator to upload an Excel file.')
        return
    votes = load_reviewer_votes(uid)
    queue = sync_queued_votes()
    failed = {pid: entry for pid, entry in queue.items() if entry.get('error')}
    pending = len(queue) - len(failed)
    completed = len(votes)
    st.progress(completed / len(papers), text=f'Confirmed saved: {completed} / {len(papers)} ({completed / len(papers):.1%})')
    if pending:
        st.info(f'{pending} decision(s) are still saving to Supabase. You can continue screening; do not close the page until they are confirmed.')
    if failed:
        st.error(f'{len(failed)} decision(s) FAILED to save. They remain available for retry in this browser session.')
        if st.button('Retry failed saves', type='primary'):
            for pid, item in failed.items():
                schedule_vote(item['record'])
            st.rerun(scope='fragment')
    if queue and st.button('Check save status'):
        st.rerun(scope='fragment')
    # Include queued submissions for navigation only; counts above remain confirmed-only.
    effective_votes = {**votes, **{pid: x['vote'] for pid, x in queue.items()}}

    filter_mode = st.radio('Show', ['All', 'Unscreened', 'Screened', 'Uncertain'], horizontal=True)
    visible = [p for p in papers if (filter_mode == 'All' or
        (filter_mode == 'Unscreened' and p['id'] not in effective_votes) or
        (filter_mode == 'Screened' and p['id'] in effective_votes) or
        (filter_mode == 'Uncertain' and effective_votes.get(p['id'], {}).get('decision') == 'uncertain'))]
    if not visible:
        st.success('No papers in this view.')
        return
    idlist = [p['id'] for p in visible]
    positions = {pid: i for i, pid in enumerate(idlist)}
    if st.session_state.get('paper_id') not in positions:
        st.session_state['paper_id'] = idlist[0]
    index = positions[st.session_state['paper_id']]
    paper = visible[index]

    st.caption(f'Paper {index + 1} of {len(visible)} · {paper.get("item_type") or "Publication"} · {paper.get("publication_year") or "Year unknown"}')
    st.subheader(paper['title'])
    st.markdown(f"**Authors:** {paper.get('author') or 'Not supplied'}")
    st.markdown(f"**Venue:** {paper.get('publication_title') or 'Not supplied'}")
    st.markdown(f"**DOI:** {paper.get('doi') or 'Not supplied'}")
    url = paper.get('url') or ''
    if valid_http_url(url):
        st.link_button('Open paper / publisher URL', url)
    st.markdown('#### Abstract')
    st.write(paper.get('abstract') or 'No abstract provided in the imported file.')
    old = effective_votes.get(paper['id'], {})
    # Per-paper widget key prevents unsaved notes from leaking to the next paper.
    note = st.text_area('Reason / note (optional)', value=old.get('note', ''),
                        max_chars=2000, key='note_' + paper['id'])
    saving_this_paper = paper['id'] in queue and not queue[paper['id']]['future'].done()
    st.markdown('**Choose a decision to save and continue:**')
    yes_col, no_col, uncertain_col = st.columns(3)
    with yes_col:
        yes_clicked = st.button('Yes & Next', type='primary', use_container_width=True,
                                disabled=saving_this_paper)
    with no_col:
        no_clicked = st.button('No & Next', use_container_width=True,
                               disabled=saving_this_paper)
    with uncertain_col:
        uncertain_clicked = st.button('Uncertain & Next', use_container_width=True,
                                      disabled=saving_this_paper)
    decision = ('yes' if yes_clicked else 'no' if no_clicked else
                'uncertain' if uncertain_clicked else None)
    if decision is not None:
        record = {'paper_id': paper['id'], 'reviewer_id': uid,
                  'decision': decision, 'note': note.strip()}
        if not schedule_vote(record):
            st.warning('This paper is still saving. Check save status before editing it again.')
            return
        # In Unscreened mode, the current paper vanishes from the filtered
        # results; advance by identity, not by an index in the old list.
        if index + 1 < len(idlist):
            st.session_state['paper_id'] = idlist[index + 1]
        elif index > 0:
            st.session_state['paper_id'] = idlist[index - 1]
        else:
            st.session_state['paper_id'] = None
        st.rerun(scope='fragment')
    # A numeric jump is intentionally used instead of rendering a 4,800-item
    # selectbox with an expensive format_func on EVERY navigation.
    nav_a, nav_b, nav_c = st.columns([1, 2, 1])
    with nav_a:
        if st.button('← Previous', disabled=index == 0, use_container_width=True):
            st.session_state['paper_id'] = idlist[index - 1]
            st.rerun(scope='fragment')
    with nav_b:
        with st.form('jump_form'):
            jump_a, jump_b = st.columns([3, 1])
            target = jump_a.number_input('Paper number', min_value=1, max_value=len(visible),
                                         value=index + 1, step=1, label_visibility='collapsed')
            if jump_b.form_submit_button('Go', use_container_width=True):
                st.session_state['paper_id'] = idlist[int(target) - 1]
                st.rerun(scope='fragment')
    with nav_c:
        if st.button('Next →', disabled=index == len(idlist) - 1, use_container_width=True):
            st.session_state['paper_id'] = idlist[index + 1]
            st.rerun(scope='fragment')

    st.divider()
    st.caption('Each decision button queues a save and advances immediately. Database writes are queued; only confirmed writes count toward progress. Check pending saves before signing out or closing the tab.')


def screening_status(votes):
    if len(votes) < 2:
        return 'Awaiting second review'
    decisions = [v['decision'] for v in votes]
    if decisions == ['yes', 'yes'] or set(decisions) == {'yes'}:
        return 'Agreed include'
    if set(decisions) == {'no'}:
        return 'Agreed exclude'
    if 'uncertain' in decisions:
        return 'Needs full-text review'
    return 'Conflict'


def assemble():
    papers = fetch_all('papers')
    reviewers = sorted([p for p in fetch_all('profiles') if p['role'] == 'reviewer'], key=lambda x: x['email'])
    votes = fetch_all('votes')
    resolutions = {r['paper_id']: r for r in fetch_all('resolutions')}
    by_paper = {}
    for v in votes:
        by_paper.setdefault(v['paper_id'], {})[v['reviewer_id']] = v
    # Fix the expected two reviewers deterministically using their alphabetical email order.
    selected = reviewers[:2]
    rows = []
    for p in papers:
        vr = by_paper.get(p['id'], {})
        pair = [vr[r['id']] for r in selected if r['id'] in vr]
        status = screening_status(pair) if len(selected) == 2 else 'Configure two reviewers'
        resolution = resolutions.get(p['id'])
        final = resolution['decision'] if resolution else ('yes' if status == 'Agreed include' else 'no' if status == 'Agreed exclude' else 'pending')
        row = {**p, 'reviewer_a_email': selected[0]['email'] if len(selected)>0 else '',
               'reviewer_a_vote': vr.get(selected[0]['id'], {}).get('decision', '') if len(selected)>0 else '',
               'reviewer_a_note': vr.get(selected[0]['id'], {}).get('note', '') if len(selected)>0 else '',
               'reviewer_b_email': selected[1]['email'] if len(selected)>1 else '',
               'reviewer_b_vote': vr.get(selected[1]['id'], {}).get('decision', '') if len(selected)>1 else '',
               'reviewer_b_note': vr.get(selected[1]['id'], {}).get('note', '') if len(selected)>1 else '',
               'status': status, 'final_decision': final, 'resolution_note': resolution['note'] if resolution else ''}
        rows.append(row)
    return sorted(rows, key=lambda r: (r.get('title') or '').lower()), selected


def download_excel(rows):
    frame = pd.DataFrame(rows)
    ordered = [k for k in FIELDS if k in frame.columns] + [
        'reviewer_a_email','reviewer_a_vote','reviewer_a_note',
        'reviewer_b_email','reviewer_b_vote','reviewer_b_note',
        'status','final_decision','resolution_note','id','fingerprint']
    frame = frame.reindex(columns=ordered)
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine='xlsxwriter') as writer:
        frame.to_excel(writer, index=False, sheet_name='Screening Results')
        sheet = writer.sheets['Screening Results']
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, max(1,len(frame)), len(frame.columns)-1)
        sheet.set_column(0, len(frame.columns)-1, 22)
        for col in ('title', 'abstract', 'url'):
            if col in frame.columns:
                sheet.set_column(frame.columns.get_loc(col), frame.columns.get_loc(col), 65 if col=='abstract' else 48)
    return bio.getvalue()


def admin_view():
    tabs = st.tabs(['Overview & export', 'Import Excel', 'Conflicts / adjudication'])
    with tabs[0]:
        rows, reviewers = assemble()
        counts = Counter(r['status'] for r in rows)
        a,b,c,d = st.columns(4)
        a.metric('Papers', len(rows))
        b.metric('Agreed include', counts['Agreed include'])
        c.metric('Agreed exclude', counts['Agreed exclude'])
        d.metric('Review / conflict', counts['Needs full-text review'] + counts['Conflict'])
        st.write('Registered reviewers (the first two alphabetically are used for comparisons):',
                 ', '.join(r['email'] for r in reviewers) if reviewers else 'None')
        if len(reviewers) != 2:
            st.warning('For reliable two-reviewer comparisons, create exactly two reviewer accounts (plus your admin account).')
        if rows:
            st.dataframe(pd.DataFrame(rows)[['title','publication_year','reviewer_a_vote','reviewer_b_vote','status','final_decision']],
                         use_container_width=True, hide_index=True)
            st.download_button('Download full results (.xlsx)', data=download_excel(rows),
                               file_name='llm_routing_screening_results.xlsx',
                               mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        st.caption('Only the admin can see both reviewer decisions. Export retains abstracts and URLs.')
    with tabs[1]:
        st.subheader('Import publications')
        upload = st.file_uploader('Choose XLSX containing Title, Abstract, URL and other bibliographic fields', type=['xlsx'])
        if upload:
            try:
                recs, stats = parse_xlsx(upload.getvalue())
                st.write(f"Spreadsheet rows: {stats['sheet_rows']} · valid unique papers: {len(recs)} · blank titles: {stats['skipped_blank_titles']} · duplicates within file: {stats['duplicates_in_file']}")
                st.write('Matched columns:', stats['columns'])
                st.write(f"Missing abstracts: {stats['missing_abstract']} · Missing URLs (after DOI fallback): {stats['missing_url']}")
                st.dataframe(pd.DataFrame(recs).head(10), use_container_width=True)
                st.warning('Existing DOI/title matches will have their bibliographic metadata updated; reviewer votes are preserved. Check the preview before importing.')
                if st.button('Import / update papers', type='primary', disabled=not recs):
                    db = service()
                    for i in range(0, len(recs), 100):
                        db.table('papers').upsert(recs[i:i+100], on_conflict='fingerprint').execute()
                    invalidate_papers_cache()
                    st.success(f'{len(recs)} distinct publications imported or updated.')
            except Exception as exc:
                st.error(f'Import error: {exc}')
    with tabs[2]:
        rows, reviewers = assemble()
        pending = [r for r in rows if r['status'] in ('Conflict','Needs full-text review')]
        st.caption('Only papers with two completed votes that disagree or contain Uncertain are shown.')
        st.write(f'{len(pending)} papers require attention.')
        for r in pending:
            with st.expander(f"{r['title']} — {r['status']} — Final: {r['final_decision']}"):
                st.write('A:',r['reviewer_a_vote'], '—',r['reviewer_a_note'])
                st.write('B:',r['reviewer_b_vote'], '—',r['reviewer_b_note'])
                st.write('Abstract:',r.get('abstract') or '(Missing)')
                if valid_http_url(r.get('url')):
                    st.link_button('Open paper',r['url'])
                with st.form('adjudicate_'+r['id']):
                    opts = ['pending','yes','no']
                    decision = st.selectbox('Final decision',opts,index=opts.index(r['final_decision']))
                    note = st.text_area('Adjudication rationale',value=r.get('resolution_note') or '')
                    if st.form_submit_button('Save adjudication'):
                        # Authorization: admin_view is only invoked after authenticated admin role check.
                        service().table('resolutions').upsert({'paper_id':r['id'],'decision':decision,
                            'note':note.strip(),'resolved_by':st.session_state['uid']},on_conflict='paper_id').execute()
                        st.rerun()


def main():
    try:
        for secret in ('SUPABASE_URL', 'SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY'):
            if not st.secrets.get(secret):
                st.error(f'Missing configuration: {secret}. See README.md.')
                return
    except Exception:
        st.error('Configure Streamlit secrets. See README.md.')
        return
    user = auth_user()
    if not user:
        login()
        return
    uid = str(user.id)
    try:
        profile = role_for(uid)
        st.session_state['uid'] = uid
        page_header(profile)
        if profile['role'] == 'admin':
            admin_view()
        else:
            reviewer_view(uid)
    except Exception as exc:
        st.error(f'Database operation failed: {exc}')


if __name__ == '__main__':
    main()
