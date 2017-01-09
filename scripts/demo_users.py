from __future__ import unicode_literals
import argparse

import transaction
from pyramid.paster import bootstrap
from pyramid.traversal import find_resource
from voteit.core.security import ROLE_VIEWER
from voteit.core.security import ROLE_DISCUSS
from voteit.core.security import ROLE_PROPOSE
from voteit.core.security import ROLE_VOTER


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    parser.add_argument("meeting_path", help="Meeting to add users to")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    request = env['request']
    print "Adding to meeting %r" % args.meeting_path
    #Just to make sure path exists
    meeting = find_resource(root, args.meeting_path)
    userids = add_users(root, request)
    for userid in userids:
        meeting.local_roles.add(userid, [ROLE_VIEWER, ROLE_DISCUSS, ROLE_PROPOSE, ROLE_VOTER])
    print "Results"
    print "="*80
    for userid in userids:
        print ", ".join([userid, "%s@voteit.se" % userid])
    print "-"*80
    print "Commit"
    transaction.commit()


def add_users(root, request, start=1, count=150):
    print "Adding %s users" % count
    users = root['users']
    User = request.content_factories['User']
    added = []
    for i in range(start, start+count):
        name = "demo-%s" % i
        added.append(name)
        users[name] = user = User(
            first_name = 'Demoperson',
            last_name = str(i),
            email = 'demo-%s@voteit.se' % i,
            password = 'demo-%s' % i,
        )
        user.email_validated = True
    return added

if __name__ == '__main__':
    main()
