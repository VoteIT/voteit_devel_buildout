from __future__ import unicode_literals
import argparse

from pyramid.paster import bootstrap
from repoze.catalog import query

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    print("---")
    print("Title \tUsers \tMeetings \tProposals \tDiscussion posts")
    items = [root.title, str(len(root['users']))]
    for type_name in ['Meeting', 'Proposal', 'DiscussionPost']:
        res = root.catalog.query(query.Eq('type_name', type_name))[0]
        items.append(str(res.total))
    print("\t".join(items))

if __name__ == '__main__':
    main()
