"""Reconcile an authorized Feishu directory snapshot with existing local accounts."""
from collections import defaultdict
from dataclasses import dataclass
import re
from unicodedata import normalize


def normalize_phone(value):
    text = normalize('NFKC', str(value or '')).strip()
    if not re.fullmatch(r'[0-9+()\s-]+', text):
        return ''
    phone = re.sub(r'[()\s-]', '', text)
    if phone.startswith('00'):
        phone = '+' + phone[2:]
    if re.fullmatch(r'1[3-9][0-9]{9}', phone):
        return '+86' + phone
    if re.fullmatch(r'861[3-9][0-9]{9}', phone):
        return '+' + phone
    return phone if re.fullmatch(r'\+[1-9][0-9]{7,14}', phone) else ''


@dataclass
class Member:
    open_id: str
    name: str
    active: bool
    phone: str
    departments: set[str]
    job_key: str
    job_title: str


def member_profile(member, key, department_id=''):
    state = member.get('status') or {}
    active = not any(state.get(flag) for flag in ('is_resigned', 'is_frozen', 'is_exited', 'is_unjoin'))
    departments = {value for value in member.get('department_ids') or [] if isinstance(value, str) and value != '0'}
    if department_id and department_id != '0':
        departments.add(department_id)
    title = str(member.get('job_title') or '').strip()[:80]
    title_id = str(member.get('job_title_id') or '').strip()[:80]
    job_key = ('id:' + title_id) if title_id else ('title:' + title) if title else ''
    return Member(key, member.get('name') or f'飞书成员（{key[-6:]}）', active,
                  normalize_phone(member.get('mobile')), departments, job_key, title)


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS feishu_member_profiles (
        open_id TEXT PRIMARY KEY, phone_tail TEXT NOT NULL, job_key TEXT NOT NULL,
        job_title TEXT NOT NULL, local_user_id INTEGER, match_status TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS feishu_member_departments (
        open_id TEXT NOT NULL, department_id TEXT NOT NULL, PRIMARY KEY (open_id, department_id)
    )""")


def clear_user_link(conn, user_id, *, block=False):
    conn.execute("UPDATE system_users SET feishu_id='', feishu_binding_source=? WHERE id=?",
                 ('blocked' if block else '', user_id))
    conn.execute("UPDATE feishu_member_profiles SET local_user_id=NULL, match_status='resync' WHERE local_user_id=?", (user_id,))
    conn.execute('DELETE FROM login_sessions WHERE user_id=?', (user_id,))


def reconcile(conn, members, departments, *, demo):
    users = {row['id']: dict(row) for row in conn.execute(
        'SELECT id,phone,status,role,feishu_id,feishu_binding_source FROM system_users')}
    local_phones = defaultdict(list)
    remote_phones = defaultdict(list)
    for user in users.values():
        if phone := normalize_phone(user['phone']):
            local_phones[phone].append(user)
    for member in members.values():
        if member.active and member.phone:
            remote_phones[member.phone].append(member.open_id)

    owners = defaultdict(list)
    for user in users.values():
        if user['feishu_id']:
            owners[user['feishu_id']].append(user['id'])

    profiles = []
    revoked = set()
    for member in members.values():
        matches = local_phones.get(member.phone, [])
        user = matches[0] if len(matches) == 1 else None
        linked = owners.get(member.open_id, [])
        if not member.active:
            state = 'inactive'
        elif not member.phone:
            state = 'no_phone'
        elif len(matches) > 1 or len(remote_phones[member.phone]) > 1:
            state = 'conflict'
        elif not user:
            state = 'no_account'
        elif user['status'] != 'active':
            state = 'disabled'
        elif (linked and linked != [user['id']]) or user['feishu_id'] not in (None, '', member.open_id):
            state = 'conflict'
        elif user['feishu_binding_source'] == 'blocked' or (
            user['role'] != 'user' and not (user['feishu_id'] == member.open_id and user['feishu_binding_source'] == 'manual')
        ):
            state = 'review'
        else:
            state = 'matched'
            if not user['feishu_id']:
                conn.execute("UPDATE system_users SET feishu_id=?, feishu_binding_source='phone' WHERE id=?",
                             (member.open_id, user['id']))
            elif not user['feishu_binding_source']:
                conn.execute("UPDATE system_users SET feishu_binding_source='manual' WHERE id=?", (user['id'],))
        if state != 'matched':
            for local_id in linked:
                if users[local_id]['feishu_binding_source'] == 'phone':
                    revoked.add(local_id)
        profiles.append((member.open_id, member.phone[-4:], member.job_key, member.job_title,
                         user['id'] if user else None, state))

    active_members = {m.open_id for m in members.values() if m.active}
    for user in users.values():
        if user['feishu_binding_source'] == 'phone' and user['feishu_id'] not in active_members:
            revoked.add(user['id'])
    for user_id in revoked:
        clear_user_link(conn, user_id)

    conn.execute('DELETE FROM feishu_member_profiles')
    conn.execute('DELETE FROM feishu_member_departments')
    conn.executemany('INSERT INTO feishu_member_profiles VALUES (?,?,?,?,?,?)', profiles)
    conn.executemany('INSERT INTO feishu_member_departments VALUES (?,?)', (
        (member.open_id, key) for member in members.values() for key in sorted(member.departments & departments.keys())
    ))
    if not demo:
        conn.execute("""DELETE FROM login_sessions WHERE auth_source='feishu' AND user_id NOT IN (
            SELECT local_user_id FROM feishu_member_profiles WHERE match_status='matched' AND local_user_id IS NOT NULL
        )""")
    return sum(row[-1] == 'matched' for row in profiles)
