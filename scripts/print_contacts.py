from __future__ import unicode_literals
import argparse

from pyramid.paster import bootstrap

from voteit.core.security import find_authorized_userids, MANAGE_SERVER
from voteit.multiple_votes import MEETING_NAMESPACE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    print("---")
    print("Finding managers for %s" % root.title)
    users = root['users']
    print "Email".ljust(40), "Userid".ljust(30), "Name"
    for userid in find_authorized_userids(root, MANAGE_SERVER):
        user = users[userid]
        print user.email.ljust(40), user.userid.ljust(30), user.title

    print("-"*80)
    print("Checking meetings")
    multi=False
    for meeting in [x for x in root.values() if x.type_name == 'Meeting']:
        if MEETING_NAMESPACE in meeting:
            print('Multi-votes activated in %s' % meeting.__name__)
            multi=True
    if not multi:
        print("No multi-votes meetings found")


if __name__ == '__main__':
    main()
