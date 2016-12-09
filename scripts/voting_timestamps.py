
from __future__ import unicode_literals
import argparse
from math import floor

from pyramid.paster import bootstrap
from pyramid.traversal import find_resource
from voteit.core.models.interfaces import IVote


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    parser.add_argument("path", help="Which path to extract info from")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    request = env['request']
    print "Reading poll times from path %r" % args.path
    #Just to make sure path exists
    find_resource(root, args.path)
    query = "path == '%s'" % args.path
    query += " and type_name == 'Poll'"
    docids = root.catalog.query(query)[1]
    for poll in request.resolve_docids(docids, perm=None):
        print_voting_timestamps(poll, request)


def print_voting_timestamps(poll, request):
    print "\n"
    print poll.title
    print "="*80
    print "Start time:".ljust(20), request.dt_handler.format_dt(poll.start_time)
    print "End time:".ljust(20), request.dt_handler.format_dt(poll.end_time)
    print "Minutes since poll started, within this minute. (Ie 1 min, voted in 0-59 seconds)"
    vote_times = {}
    for vote in poll.values():
        if not IVote.providedBy(vote):
            continue
        ts_min = (vote.created - poll.start_time).total_seconds() // 60
        ts_min = int(floor(ts_min) + 1)
        if ts_min not in vote_times:
            vote_times[ts_min] = 0
        vote_times[ts_min] += 1
    sv_times = sorted(vote_times.items(), key=lambda x: x[0])
    print "-"*80
    print "Minutes".ljust(20), "Voters"
    for (k, v) in sv_times:
        print str(k).ljust(20), v
    print "\n"
    print "Total:".ljust(20), sum(vote_times.values())


if __name__ == '__main__':
    main()
