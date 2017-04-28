from __future__ import unicode_literals
import argparse

from pyramid.paster import bootstrap
import transaction


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    parser.add_argument("meeting_name", help="Which meeting to change")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    request = env['request']
    meeting = root[args.meeting_name]
    print "Resetting read in %r" % args.meeting_name
    meeting._read_names_counter.clear()
    for obj in meeting.values():
        if obj.type_name != 'AgendaItem':
            continue
        rn = request.get_read_names(obj)
        rn.data.clear()
    transaction.commit()
    env['closer']()

if __name__ == '__main__':
    main()
