"""Request-scoped directory grants. Background indexing uses the unfiltered tables."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import posixpath
import re
import sqlite3


OPERATIONS = ('read', 'upload', 'download', 'update')


def grant_permissions(row):
    """NULL is a legacy grant: keep its original visibility/download behavior."""
    if row['permissions'] is not None:
        return json.loads(row['permissions'])
    if row['effect'] == 'deny':
        return dict.fromkeys(OPERATIONS, 'deny')
    return {'read': 'allow', 'download': 'legacy'}


def normalise(path):
    return posixpath.normpath(str(path).replace('\\', '/')).rstrip('/') or '/'


def within(path, ancestor):
    return path == ancestor or path.startswith(ancestor.rstrip('/') + '/')


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def directory_sql(path):
    # SQLite executes this predicate natively. A Python callback per indexed
    # file would serialize concurrent dashboard queries on the GIL.
    expression = "coalesce(nullif(rtrim(replace(parent,char(92),'/'),'/'),''),'/')"
    prefix = path.rstrip('/') + '/'
    return f"({expression}={sql_literal(path)} OR substr({expression},1,{len(prefix)})={sql_literal(prefix)})"


@dataclass(frozen=True)
class Policy:
    root_key: str
    root: str
    user_id: int
    super_admin: bool
    grants: tuple = ()
    operations: tuple = ()
    legacy_admin: bool = False

    def decision(self, directory, operation):
        directory = normalise(directory)
        if operation not in OPERATIONS or not within(directory, self.root):
            return 'deny'
        if self.super_admin:
            return 'direct' if operation == 'download' else 'allow'
        values = [value for path, action, value in self.operations
                  if action == operation and within(directory, path)]
        if 'deny' in values:
            return 'deny'
        if operation == 'download':
            if not self.visible(directory):
                return 'deny'
            if 'approval' in values or ('legacy' in values and not self.legacy_admin):
                return 'approval'
            return 'direct' if 'direct' in values or ('legacy' in values and self.legacy_admin) else 'deny'
        return 'allow' if 'allow' in values else 'deny'

    def visible(self, directory):
        directory = normalise(directory)
        if not within(directory, self.root):
            return False
        if self.super_admin:
            return True
        if any(effect == 'deny' and within(directory, path) for path, effect in self.grants):
            return False
        return any(effect == 'allow' and within(directory, path) for path, effect in self.grants)

    def complete(self, directory):
        """Rules covering a whole subtree must not expose excluded descendant metadata."""
        directory = normalise(directory)
        return self.visible(directory) and (self.super_admin or not any(
            effect == 'deny' and within(path, directory) for path, effect in self.grants))

    @property
    def cache_key(self):
        return hashlib.sha256(json.dumps((self.root_key, self.user_id, self.super_admin, self.grants, self.operations)).encode()).hexdigest()


current: ContextVar[Policy | None] = ContextVar('directory_policy', default=None)


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS directory_grants (
            id INTEGER PRIMARY KEY, root_key TEXT NOT NULL,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('user','department')),
            subject_key TEXT NOT NULL, target_path TEXT NOT NULL,
            effect TEXT NOT NULL CHECK(effect IN ('allow','deny')),
            created_by INTEGER NOT NULL, created_at REAL NOT NULL,
            UNIQUE(root_key,subject_type,subject_key,target_path)
        );
        CREATE TABLE IF NOT EXISTS directory_grant_audit (
            id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, action TEXT NOT NULL,
            grant_json TEXT NOT NULL, created_at REAL NOT NULL
        );
    ''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(directory_grants)')}
    if 'permissions' not in columns:
        conn.execute('ALTER TABLE directory_grants ADD COLUMN permissions TEXT')


def load_policy(conn, user, root_key, root):
    if user is None:
        return Policy(root_key, normalise(root), 0, False)
    rows = conn.execute('''SELECT target_path,effect,permissions FROM directory_grants
        WHERE root_key=? AND ((subject_type='user' AND subject_key=?)
        OR (subject_type='department' AND subject_key=? AND subject_key<>''))
        ORDER BY target_path,effect''', (root_key, str(user['id']), user.get('department') or '')).fetchall()
    operations = tuple((r['target_path'], action, value) for r in rows
                       for action, value in grant_permissions(r).items())
    grants = tuple((path, value) for path, action, value in operations if action == 'read')
    return Policy(root_key, normalise(root), user['id'], user['role'] == 'super_admin',
                  grants, operations, user['role'] == 'dept_admin')


_TABLES = ('files', 'archive_rules', 'archive_tasks', 'saved_analyses', 'download_requests')
_READ_TABLE = re.compile(r'\b(FROM|JOIN)\s+(' + '|'.join(_TABLES) + r')\b', re.I)


class ScopedConnection(sqlite3.Connection):
    """Route all table reads through connection-local views before ordering/limits.

    This applies to nested selects and FTS joins too. Mutations must first look up
    their target through these same views; schema/index workers have no policy.
    """
    def execute(self, sql, parameters=(), /):
        if current.get() is not None:
            original = sql
            def scoped_read(match):
                # DELETE's target is a base table; its authorization is checked by
                # the caller's preceding scoped SELECT. Nested reads remain scoped.
                if re.search(r'\bDELETE\s*$', original[:match.start()], re.I):
                    return match[0]
                return match[1] + ' visible_' + match[2].lower()
            sql = _READ_TABLE.sub(scoped_read, sql)
        return super().execute(sql, parameters)

    def install_scope(self, policy):
        if policy is None:
            return
        self.create_function('acl_complete', 1, lambda p: int(policy.complete(p)), deterministic=True)
        self.create_function('acl_root', 0, lambda: policy.root_key, deterministic=True)
        self.create_function('acl_actor', 0, lambda: policy.user_id, deterministic=True)
        self.create_function('acl_manager', 0, lambda: int(policy.super_admin), deterministic=True)
        # Bypass execute rewriting while constructing the trusted views.
        allowed = ' OR '.join(directory_sql(path) for path, effect in policy.grants if effect == 'allow') or '0'
        denied = ' OR '.join(directory_sql(path) for path, effect in policy.grants if effect == 'deny') or '0'
        predicate = '1' if policy.super_admin else directory_sql(policy.root)
        if not policy.super_admin:
            predicate += f' AND ({allowed}) AND NOT ({denied})'
        super().execute(f'''CREATE TEMP VIEW visible_files AS SELECT * FROM main.files
            WHERE root_key={sql_literal(policy.root_key)} AND {predicate}''')
        super().executescript('''
            CREATE TEMP VIEW visible_archive_rules AS SELECT * FROM main.archive_rules
                WHERE acl_complete(target_path);
            CREATE TEMP VIEW visible_archive_tasks AS SELECT t.* FROM main.archive_tasks t
                WHERE acl_complete(t.target_path) AND EXISTS (
                    SELECT 1 FROM visible_archive_rules r WHERE r.id=t.rule_id);
            CREATE TEMP VIEW visible_saved_analyses AS SELECT s.* FROM main.saved_analyses s
                WHERE s.file_id IN (SELECT id FROM visible_files);
            CREATE TEMP VIEW visible_download_requests AS SELECT d.* FROM main.download_requests d
                WHERE EXISTS (SELECT 1 FROM visible_files f WHERE f.id=d.file_id AND f.path=d.file_path)
                AND (acl_manager() OR d.user_id=acl_actor() OR
                    EXISTS (SELECT 1 FROM main.system_users u WHERE u.id=acl_actor() AND u.role='dept_admin'));
        ''')
