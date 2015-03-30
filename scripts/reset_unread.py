from __future__ import unicode_literals
import argparse

from pyramid.paster import bootstrap
import transaction

from arche.utils import find_all_db_objects
from arche.models.catalog import create_catalog
from arche.interfaces import ICataloger
from arche.utils import find_all_db_objects
from voteit.core.models.interfaces import IUnread


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    parser.add_argument("meeting_name", help="Which meeting to change")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    meeting = root[args.meeting_name]
    print "Resetting unread in %r" % args.meeting_name
    found = 0
    i = 0
    for obj in find_all_db_objects(meeting):
        unread = IUnread(obj, None)
        if unread == None:
            continue
        unread.reset_unread()
        found += 1
        i += 1
        if i == 10:
            print found
            i = 0
            
    transaction.commit()
    env['closer']()

if __name__ == '__main__':
    main()
