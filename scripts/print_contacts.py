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
    print("="*80)
    print("Finding managers for %s" % root.title)
    userids = find_authorized_userids(root, MANAGE_SERVER)
    users = [root['users'][x] for x in userids]
    print ("-"*80)
    print "Emails"
    print ("="*80)
    for user in users:
        print user.email
    print ("-"*80)
    print "Userid".ljust(30), "Name"
    print ("="*80)
    for user in users:
        print user.userid.ljust(30), user.title
    print("-"*80)
    print("Checking meetings")
    multi=False
    motion=False
    for obj in root.values():
        if obj.type_name == 'Meeting':
            #[x for x in root.values() if x.type_name == 'Meeting']
            if MEETING_NAMESPACE in obj:
                print('Multi-votes activated in %s' % obj.__name__)
                multi=True
        if obj.type_name == 'MotionProcess':
            print('Motion process in %s' % obj.__name__)
            motion = True
    if not multi:
        print("No multi-votes meetings found")
    if not motion:
        print("No motion processes")
    print("*"*80)
    print("\n\n")

if __name__ == '__main__':
    main()
