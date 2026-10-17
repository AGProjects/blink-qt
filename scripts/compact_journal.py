#!/usr/bin/env python3
"""Compact the message journal of a SylkServer account on the server.

    compact_journal.py ag@sylk.link                          # what the journal holds, and what each rule would remove
    compact_journal.py ag@sylk.link -r receipts --remove     # remove receipts older than 30 days
    compact_journal.py ag@sylk.link -r location-tracks -o 3d --remove
    compact_journal.py ag@sylk.link -r receipts -C alice@example.com --remove   # one contact only

The journal keeps every entry of the account: what a new device downloads to start, and what a
device that was offline downloads to catch up. Compacting removes the entries that stopped being
worth that download, one rule at a time (blink.data_manage.COMPACTION_RULES):

  receipts         delivery and read receipts (message/imdn) older than 30 days
  location-tracks  the positions of location shares (location_update, meeting_update) older than 7 days

The journal is read page by page as a sync reads it and what matches is removed through the bulk
removal endpoint (POST <journal url>/remove). Nothing on any device is changed: the devices that
have the entries keep what they made of them. Without --remove nothing is removed.

The journal URL and the API token are taken from Blink's configuration (~/.blink/config): the
account must have synchronized once.
"""

import os
import signal
import sys

from datetime import datetime, timedelta, timezone
from optparse import OptionParser

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)))
from blink.data_manage import (COMPACTION_RULES, JOURNAL_SINCE_YEARS, JournalRemoveError, JournalRemoveRefused, JournalRemoveUnsupported,   # noqa: E402
                               JournalScanError, compaction_content_types, compaction_ids, compaction_rule, contact_breakdown, journal_summary, parse_age,
                               purge_journal, scan_journal)


def out(line=''):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def journal_accounts(config_directory):
    """{account id: (journal url, token)} of the accounts of Blink's configuration that have a journal."""
    from sipsimple.configuration.backend.file import FileBackend
    path = os.path.join(os.path.expanduser(config_directory), 'config')
    if not os.path.exists(path):
        return {}
    accounts = FileBackend(path).load().get('Accounts') or {}
    result = {}
    for account_id, settings in accounts.items():
        sms = settings.get('sms') if isinstance(settings, dict) else None
        if '@' in account_id and isinstance(sms, dict) and sms.get('history_synchronization_url') and sms.get('history_synchronization_token'):
            result[account_id] = (str(sms['history_synchronization_url']), str(sms['history_synchronization_token']))
    return result


def day(value):
    return value.astimezone().strftime('%Y-%m-%d') if value else '?'


def age_text(age):
    return f'{age.days} days' if age else 'any age'


def main():
    rules = ', '.join(rule.name for rule in COMPACTION_RULES)
    parser = OptionParser(usage='%prog [options] [account]', description='Compacts the message journal of a SylkServer account on the server.')
    parser.add_option('-r', '--rule', action='append', dest='rules', default=[], metavar='RULE', help=f'a rule to apply (repeat for several): {rules}')
    parser.add_option('-o', '--older-than', dest='older_than', default=None, metavar='AGE',
                      help="instead of the rule's own age: 30d, 12w, 6m, 1y, or 0 for all")
    parser.add_option('-C', '--contact', dest='contact', default=None, metavar='URI', help='only the entries of this contact')
    parser.add_option('-n', '--contacts', type='int', dest='contacts', default=20, help='how many contacts the breakdown lists (default 20, 0 for all)')
    parser.add_option('-s', '--since-years', type='int', dest='since_years', default=JOURNAL_SINCE_YEARS,
                      help=f'how far back the journal is read (default {JOURNAL_SINCE_YEARS} years)')
    parser.add_option('-c', '--config-directory', dest='config_directory', default='~/.blink', help="Blink's data directory (default ~/.blink)")
    parser.add_option('-k', '--insecure', action='store_true', dest='insecure', default=False, help='do not verify the TLS certificate of the server')
    parser.add_option('-a', '--all-types', action='store_true', dest='all_types', default=False,
                      help='read every entry of the journal, to list all its types (default: only the types the rules look at)')
    parser.add_option('--remove', action='store_true', dest='remove', default=False, help='remove what the rules match (without it, only count)')
    options, args = parser.parse_args()
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    chosen = []
    for name in options.rules:
        rule = compaction_rule(name)
        if rule is None:
            parser.error(f'no rule {name!r}: {rules}')
        chosen.append(rule)
    try:
        older_than = parse_age(options.older_than) if options.older_than is not None else None
    except ValueError as e:
        parser.error(str(e))
    if options.remove and not chosen:
        parser.error('--remove needs at least one --rule')

    accounts = journal_accounts(options.config_directory)
    if args:
        account_id = args[0]
        if account_id not in accounts:
            out(f'{account_id} has no journal in Blink (it must be a SylkServer account that synchronized once)')
            return 1
    elif len(accounts) == 1:
        account_id = next(iter(accounts))
    else:
        out('Accounts with a journal: ' + (', '.join(sorted(accounts)) if accounts else 'none'))
        return 2
    url, token = accounts[account_id]
    verify = not options.insecure

    out(f'Reading the journal of {account_id} ({url}) from {options.since_years} years ago...')
    since = datetime.now(timezone.utc) - timedelta(days=365 * options.since_years)
    stats = {}
    try:
        entries = scan_journal(url, token, since=since, verify=verify, stats=stats,
                               content_types=None if options.all_types else compaction_content_types(chosen or COMPACTION_RULES),
                               progress=lambda pages, count: sys.stderr.write(f'\r  {pages} pages, {count} entries') or sys.stderr.flush())
    except JournalRemoveRefused as e:
        out(f'\nThe server refused the API token of {account_id}: {e}')
        return 1
    except JournalScanError as e:
        out(f'\nReading the journal failed: {e}')
        return 1
    sys.stderr.write('\n')
    out(f'Request: GET {stats.get("first_url")}')
    out(f'Read {stats.get("pages", 0)} pages, {stats.get("received", 0)} entries received, {len(entries)} kept'
        + ('' if options.all_types else ' (filtered by the server)' if stats.get('server_filtered') else ' (the server does not filter by content type: filtered here)'))

    out()
    out(f'{"Type":<44} {"Entries":>8}   Oldest       Newest')
    for kind, total, old, oldest, newest in journal_summary(entries):
        out(f'{kind:<44} {total:>8}   {day(oldest)}   {day(newest)}')
    out(f'{"":<44} {len(entries):>8}')

    shown = chosen or list(COMPACTION_RULES)
    ages = [(rule, rule.age if older_than is None else older_than) for rule in shown]
    breakdown = contact_breakdown(entries, ages)
    out()
    out(f'By contact ({len(breakdown)} with something to remove)')
    out(f'  {"Contact":<44} ' + ' '.join(f'{rule.name:>16}' for rule, age in ages) + f' {"Total":>8}')
    for contact, per_rule, total in breakdown[:options.contacts or None]:
        out(f'  {contact or "(no contact)":<44} ' + ' '.join(f'{per_rule.get(rule.name, 0):>16}' for rule, age in ages) + f' {total:>8}')
    if options.contacts and len(breakdown) > options.contacts:
        out(f'  ... {len(breakdown) - options.contacts} more (-n 0 lists all)')

    out()
    out('Rules' + (f' for {options.contact}' if options.contact else ''))
    ids = []
    for rule, age in ages:
        matched = compaction_ids(entries, rule, age, contact=options.contact)
        mark = '*' if rule in chosen else ' '
        out(f' {mark} {rule.name:<16} {len(matched):>8}  {rule.title.lower()} older than {age_text(age)}')
        if rule in chosen:
            ids += matched
    if not chosen:
        out()
        out('Choose rules with -r; nothing is removed without --remove.')
        return 0
    ids = list(dict.fromkeys(ids))
    out()
    if not ids:
        out('Nothing to remove.')
        return 0
    if not options.remove:
        out(f'{len(ids)} entries would be removed. Run again with --remove to remove them.')
        return 0
    out(f'Removing {len(ids)} entries from the journal...')
    try:
        removed, requested = purge_journal(url, token, ids, verify=verify,
                                           progress=lambda done, removed, total: sys.stderr.write(f'\r  {done} of {total} done, {removed} removed') or sys.stderr.flush())
    except JournalRemoveUnsupported:
        out('\nThe server cannot remove journal entries in bulk (SylkServer needs updating).')
        return 1
    except JournalRemoveRefused as e:
        out(f'\nThe server refused the API token of {account_id}: {e}')
        return 1
    except JournalRemoveError as e:
        out(f'\nRemoving failed: {e}')
        return 1
    sys.stderr.write('\n')
    out(f'Removed {removed} of {requested} entries' + (f' ({requested - removed} were no longer in the journal)' if removed < requested else '') + '.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
