#!/usr/bin/env python3
"""Guest account management for example.com.

Rauthy (id.example.com) is the source of truth for who may log in.
Komga, Jellyfin and Seerr each hold their own account, created on first SSO
login. This tool keeps the three in step with Rauthy.

    guests.py status                 what exists where
    guests.py add <email> [name]     create a Rauthy user (the only onboarding step)
    guests.py remove <email>         delete the Rauthy user; then run reconcile
    guests.py harden                 close the Jellyfin passwordless-login hole
    guests.py reconcile              make the apps match Rauthy

Everything that deletes or changes is DRY RUN unless you pass --apply.

Config: ~/.config/guests/config.json  (chmod 600, holds API keys)
"""
from __future__ import annotations
import argparse, base64, json, os, secrets, sqlite3, sys, urllib.error, urllib.parse, urllib.request

CONFIG = os.path.expanduser('~/.config/guests/config.json')
# A run that wants to delete more than this is treated as a bug, not a Saturday.
MAX_DELETES = 3


def die(msg: str, code: int = 1):
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(code)


def load_config() -> dict:
    if not os.path.exists(CONFIG):
        die(f'no config at {CONFIG} - see the template in tools/guests.config.example.json')
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    kf = os.path.expanduser(cfg['rauthy'].get('key_file', ''))
    if kf:
        cfg['rauthy']['key'] = open(kf).read().strip()
    return cfg


def http(url: str, method: str = 'GET', headers: dict | None = None,
         body: dict | None = None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors='replace')[:300]


# ---------------------------------------------------------------- rauthy ----

def rauthy_headers(cfg):
    return {'Authorization': f"API-Key {cfg['rauthy']['key']}"}


def rauthy_users(cfg) -> dict[str, str]:
    """email(lower) -> rauthy user id. Raises rather than returning empty."""
    st, body = http(f"{cfg['rauthy']['url']}/users", headers=rauthy_headers(cfg))
    if st != 200 or not isinstance(body, list):
        die(f'could not read Rauthy users (HTTP {st}): {body}')
    if not body:
        # Fail closed: an empty list would make reconcile delete everything.
        die('Rauthy returned zero users - refusing to continue')
    return {u['email'].strip().lower(): u['id'] for u in body if u.get('email')}


def rauthy_add(cfg, email: str, name: str | None, apply: bool):
    email = email.strip().lower()
    if not apply:
        print(f'  [dry run] would create Rauthy user {email}')
        return
    payload = {'email': email, 'language': 'en', 'roles': ['user'], 'groups': ['user']}
    if name:
        payload['given_name'] = name
    st, body = http(f"{cfg['rauthy']['url']}/users", 'POST', rauthy_headers(cfg), payload)
    if st not in (200, 201) or not isinstance(body, dict) or 'id' not in body:
        die(f'create failed (HTTP {st}): {body}')
    uid = body['id']
    # email_verified is IGNORED on create and must be set by a follow-up update.
    # Without it some clients reject the login with an unverified-email error.
    payload |= {'enabled': True, 'email_verified': True}
    st2, _ = http(f"{cfg['rauthy']['url']}/users/{uid}", 'PUT', rauthy_headers(cfg), payload)
    print(f'  created {email} -> {uid} (email_verified: {"ok" if st2 == 200 else "FAILED"})')


def rauthy_remove(cfg, email: str, apply: bool):
    users = rauthy_users(cfg)
    uid = users.get(email.strip().lower())
    if not uid:
        die(f'{email} is not a Rauthy user')
    if not apply:
        print(f'  [dry run] would delete Rauthy user {email} ({uid})')
        return
    st, body = http(f"{cfg['rauthy']['url']}/users/{uid}", 'DELETE', rauthy_headers(cfg))
    print(f'  deleted {email}: HTTP {st}' + ('' if st in (200, 204) else f' {body}'))


# -------------------------------------------------------------- jellyfin ----

def jf_headers(cfg):
    return {'Authorization': f"MediaBrowser Token={cfg['jellyfin']['api_key']}"}


def jf_users(cfg) -> list[dict]:
    st, body = http(f"{cfg['jellyfin']['url']}/Users", headers=jf_headers(cfg))
    if st != 200 or not isinstance(body, list):
        die(f'could not read Jellyfin users (HTTP {st}): {body}')
    return body


def jf_set_random_password(cfg, uid: str, apply: bool) -> bool:
    if not apply:
        return False
    pw = base64.urlsafe_b64encode(secrets.token_bytes(30)).decode().rstrip('=')
    st, _ = http(f"{cfg['jellyfin']['url']}/Users/{uid}/Password", 'POST', jf_headers(cfg),
                 {'CurrentPw': '', 'NewPw': pw, 'ResetPassword': False})
    return st in (200, 204)


# ----------------------------------------------------------------- komga ----

def komga_headers(cfg):
    k = cfg.get('komga') or {}
    if k.get('api_key'):
        return {'X-API-Key': k['api_key']}
    if k.get('user') and k.get('password'):
        tok = base64.b64encode(f"{k['user']}:{k['password']}".encode()).decode()
        return {'Authorization': f'Basic {tok}'}
    return {}


def komga_users(cfg):
    k = cfg.get('komga') or {}
    if not k.get('url') or not komga_headers(cfg):
        return None  # not configured
    st, body = http(f"{k['url']}/api/v2/users", headers=komga_headers(cfg))
    if st != 200 or not isinstance(body, list):
        print(f'  ! Komga unreadable (HTTP {st}) - skipping Komga')
        return None
    return body


# ----------------------------------------------------------------- seerr ----

def seerr_rows(cfg):
    db = os.path.expanduser(cfg['seerr']['db'])
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    users = con.execute('select id, email, jellyfinUsername, userType from "user"').fetchall()
    links = con.execute('select id, provider, sub, userId from linked_accounts').fetchall()
    con.close()
    return users, links


# -------------------------------------------------------------- commands ----

def cmd_status(cfg, args):
    rusers = rauthy_users(cfg)
    print(f'Rauthy (source of truth): {len(rusers)} users')
    for e in sorted(rusers):
        print(f'   {e}')

    print('\nJellyfin:')
    protect = set(cfg.get('protect', []))
    for u in sorted(jf_users(cfg), key=lambda x: x['Name'].lower()):
        name, pol = u['Name'], u.get('Policy', {})
        tags = []
        if pol.get('IsAdministrator'):
            tags.append('admin')
        if name in protect:
            tags.append('protected')
        if not u.get('HasPassword'):
            tags.append('NO PASSWORD - run `harden`')
        if '@' in name:
            tags.append('in Rauthy' if name.lower() in rusers else 'SSO-named but NOT in Rauthy')
        else:
            tags.append('legacy (never auto-deleted)')
        print(f'   {name:<26} {", ".join(tags)}')

    ku = komga_users(cfg)
    print('\nKomga:')
    if ku is None:
        print('   (not configured - add komga credentials to the config to manage it)')
    else:
        for u in ku:
            e = (u.get('email') or '').lower()
            print(f'   {e:<32} {"in Rauthy" if e in rusers else "NOT in Rauthy"}')

    users, links = seerr_rows(cfg)
    subs = set(rusers.values())
    by_id = {u[0]: u for u in users}
    print('\nSeerr:')
    for lid, prov, sub, uid in links:
        u = by_id.get(uid)
        label = (u[1] if u else '?')
        print(f'   {label:<32} link#{lid} {"live" if sub in subs else "STALE - rauthy user gone"}')
    unlinked = [u for u in users if u[0] not in {l[3] for l in links}]
    for u in unlinked:
        print(f'   {str(u[1]):<32} no SSO link (login via Jellyfin only)')


def cmd_add(cfg, args):
    print(f'add {args.email}:')
    rauthy_add(cfg, args.email, args.name, args.apply)
    print('\nNothing else to do now. Komga, Jellyfin and Seerr create their own')
    print('account on the guest\'s first SSO login. Run `harden` afterwards -')
    print('Jellyfin creates SSO users with NO password, and until one is set')
    print('anyone can log in as them with an empty password.')
    if not args.apply:
        print('\nDRY RUN - re-run with --apply')


def resolve_person(cfg, email: str) -> dict:
    """Find every account belonging to one person, across all four systems.

    Matching by Jellyfin username == email only finds SSO-created accounts.
    Guests migrated from jfa-go still have their ORIGINAL Jellyfin account
    (e.g. "MarcTo"), which shares no string with their email address. The link
    between the two lives in Seerr: linked_accounts.sub -> user.jellyfinUsername.
    Miss that and deleting someone leaves their Jellyfin access fully intact.
    """
    email = email.strip().lower()
    found = {'email': email, 'rauthy': None, 'seerr_user': None, 'seerr_links': [],
             'jellyfin': None, 'komga': None}

    found['rauthy'] = rauthy_users(cfg).get(email)

    users, links = seerr_rows(cfg)
    by_id = {u[0]: u for u in users}
    jf_username = None
    if found['rauthy']:
        for lid, prov, sub, uid in links:
            if sub == found['rauthy']:
                found['seerr_links'].append(lid)
                u = by_id.get(uid)
                if u:
                    found['seerr_user'] = {'id': u[0], 'email': u[1], 'jf': u[2]}
                    jf_username = u[2]
    if found['seerr_user'] is None:
        for u in users:
            if (u[1] or '').strip().lower() == email:
                found['seerr_user'] = {'id': u[0], 'email': u[1], 'jf': u[2]}
                jf_username = u[2]
                break

    for u in jf_users(cfg):
        if u['Name'].strip().lower() == email or (jf_username and u['Name'] == jf_username):
            found['jellyfin'] = {'id': u['Id'], 'name': u['Name'],
                                 'admin': u.get('Policy', {}).get('IsAdministrator')}
            break

    ku = komga_users(cfg)
    if ku is not None:
        for u in ku:
            if (u.get('email') or '').strip().lower() == email:
                found['komga'] = {'id': u.get('id'), 'email': u.get('email')}
                break
    return found


def cmd_remove(cfg, args):
    """Remove one person from every system in a single pass."""
    protect = set(cfg.get('protect', []))
    p = resolve_person(cfg, args.email)
    print(f'remove {p["email"]}\n')

    if not any([p['rauthy'], p['seerr_user'], p['jellyfin'], p['komga']]):
        die(f'no account found anywhere for {p["email"]}')
    if p['jellyfin'] and (p['jellyfin']['name'] in protect or p['jellyfin']['admin']):
        die(f'Jellyfin account {p["jellyfin"]["name"]} is protected or an admin - refusing')

    print('  Rauthy   :', p['rauthy'] or '(none - SSO already blocked)')
    print('  Jellyfin :', (f'{p["jellyfin"]["name"]}  <- deleting this revokes their '
                           f'long-lived token') if p['jellyfin'] else '(none)')
    print('  Komga    :', p['komga']['email'] if p['komga'] else
          ('(none)' if komga_users(cfg) is not None else '(not configured - SKIPPED)'))
    if p['seerr_user']:
        act = 'delete user' if args.purge_seerr else 'remove SSO link, keep request history'
        print(f'  Seerr    : #{p["seerr_user"]["id"]} ({p["seerr_user"]["email"]}) -> {act}')
    else:
        print('  Seerr    : (none)')

    if not args.apply:
        print('\nDRY RUN - nothing changed. Re-run with --apply.')
        return

    if p['rauthy']:
        st, _ = http(f"{cfg['rauthy']['url']}/users/{p['rauthy']}", 'DELETE', rauthy_headers(cfg))
        print(f'\n  rauthy   deleted: HTTP {st}')
    if p['jellyfin']:
        st, _ = http(f"{cfg['jellyfin']['url']}/Users/{p['jellyfin']['id']}", 'DELETE', jf_headers(cfg))
        print(f'  jellyfin deleted: HTTP {st}')
    if p['komga']:
        st, _ = http(f"{cfg['komga']['url']}/api/v2/users/{p['komga']['id']}", 'DELETE', komga_headers(cfg))
        print(f'  komga    deleted: HTTP {st}')
    if p['seerr_user']:
        db = os.path.expanduser(cfg['seerr']['db'])
        con = sqlite3.connect(db)
        for lid in p['seerr_links']:
            con.execute('delete from linked_accounts where id=?', (lid,))
        if args.purge_seerr:
            con.execute('delete from "user" where id=?', (p['seerr_user']['id'],))
        con.commit(); con.close()
        print('  seerr    ' + ('user deleted' if args.purge_seerr else 'SSO link removed'))
    print('\nDone. Run `status` to confirm.')


def cmd_harden(cfg, args):
    """Set a random password on every passwordless Jellyfin user.

    The SSO plugin does not register as an auth provider: it mints tokens
    directly and leaves users on DefaultAuthenticationProvider with no password.
    Jellyfin lets a passwordless user log in with an EMPTY password, so anyone
    knowing the username could bypass SSO entirely. The password is random and
    never shown - SSO does not use it.
    """
    protect = set(cfg.get('protect', []))
    todo = [u for u in jf_users(cfg) if not u.get('HasPassword') and u['Name'] not in protect]
    if not todo:
        print('All Jellyfin users have a password. Nothing to do.')
        return
    print(f'Passwordless Jellyfin users ({len(todo)}):')
    for u in todo:
        ok = jf_set_random_password(cfg, u['Id'], args.apply)
        state = ('password set' if ok else 'FAILED') if args.apply else '[dry run]'
        print(f'   {u["Name"]:<26} {state}')
    if not args.apply:
        print('\nDRY RUN - re-run with --apply')


def cmd_reconcile(cfg, args):
    """Make the apps match Rauthy.

    Deliberately conservative:
      * Jellyfin - only users whose name is an email address (i.e. SSO-created)
        are ever deleted. Legacy jfa-go accounts are left alone entirely.
      * Seerr    - a stale SSO link is removed; the user row is kept unless
        --delete-seerr-users is passed, because they may still have a working
        Jellyfin login and a request history worth keeping.
      * Komga    - accounts are all SSO-created, so absence from Rauthy means go.
    """
    rusers = rauthy_users(cfg)
    protect = set(cfg.get('protect', []))
    actions = []

    for u in jf_users(cfg):
        name = u['Name']
        if name in protect or '@' not in name:
            continue
        if name.lower() not in rusers:
            actions.append(('jellyfin', name, u['Id']))

    ku = komga_users(cfg)
    if ku is not None:
        for u in ku:
            e = (u.get('email') or '').lower()
            if e and e not in rusers and e not in protect:
                actions.append(('komga', e, u.get('id')))

    users, links = seerr_rows(cfg)
    subs = set(rusers.values())
    by_id = {u[0]: u for u in users}
    for lid, prov, sub, uid in links:
        if sub not in subs:
            actions.append(('seerr-link', str(by_id.get(uid, ('', '?'))[1]), lid))

    if not actions:
        print('Everything matches Rauthy. Nothing to do.')
        return
    print(f'Drift found ({len(actions)}):')
    for kind, label, _ in actions:
        print(f'   {kind:<12} {label}')
    if len(actions) > MAX_DELETES and not args.force:
        die(f'\n{len(actions)} changes exceeds the safety limit of {MAX_DELETES}. '
            'That is far more likely to be a bug than a real change. '
            'Re-run with --force if you are sure.')
    if not args.apply:
        print('\nDRY RUN - nothing changed. Re-run with --apply.')
        return

    for kind, label, ident in actions:
        if kind == 'jellyfin':
            st, _ = http(f"{cfg['jellyfin']['url']}/Users/{ident}", 'DELETE', jf_headers(cfg))
            print(f'   deleted jellyfin {label}: HTTP {st}')
        elif kind == 'komga':
            st, _ = http(f"{cfg['komga']['url']}/api/v2/users/{ident}", 'DELETE', komga_headers(cfg))
            print(f'   deleted komga {label}: HTTP {st}')
        elif kind == 'seerr-link':
            db = os.path.expanduser(cfg['seerr']['db'])
            con = sqlite3.connect(db)
            con.execute('delete from linked_accounts where id=?', (ident,))
            if args.delete_seerr_users:
                con.execute('delete from "user" where id=(select userId from linked_accounts where id=?)', (ident,))
            con.commit(); con.close()
            print(f'   removed seerr link for {label}')


def main():
    # --apply is accepted on both sides of the subcommand: `guests.py reconcile
    # --apply` is what anyone actually types, but the bare `--apply reconcile`
    # form keeps working too.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--apply', action='store_true',
                        help='actually make changes (default: dry run)')
    ap = argparse.ArgumentParser(description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status', parents=[common])
    p = sub.add_parser('add', parents=[common]); p.add_argument('email'); p.add_argument('name', nargs='?')
    p = sub.add_parser('remove', parents=[common]); p.add_argument('email')
    p.add_argument('--purge-seerr', action='store_true',
                   help='also delete the Seerr user row (default: keep it, drop the SSO link)')
    sub.add_parser('harden', parents=[common])
    p = sub.add_parser('reconcile', parents=[common])
    p.add_argument('--force', action='store_true', help='allow more than the safety limit')
    p.add_argument('--delete-seerr-users', action='store_true',
                   help='also delete the Seerr user row, not just the SSO link')
    args = ap.parse_args()
    cfg = load_config()
    {'status': cmd_status, 'add': cmd_add, 'remove': cmd_remove,
     'harden': cmd_harden, 'reconcile': cmd_reconcile}[args.cmd](cfg, args)


if __name__ == '__main__':
    main()
