# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import argparse

from arche_usertags.interfaces import IUserTags
from pyramid.paster import bootstrap
from pyramid.traversal import traverse
import transaction
from repoze.catalog import query
from six.moves import input

from voteit.core.models.interfaces import IAgendaItem
from voteit.core.models.interfaces import IMeeting


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    parser.add_argument("path", help="from which path to clear likes (meeting or agenda item)")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    request = env['request']
    context = traverse(root, args.path).get('context')

    if IMeeting.providedBy(context) or IAgendaItem.providedBy(context):
        print('Clearing likes on {}'.format(context.title))
        path_query = query.Eq('path', args.path)
        cleared = False

        for type_name in ('Proposal', 'DiscussionPost'):
            count, docids = root.catalog.query(path_query & query.Eq('type_name', type_name))
            response = input('Found {} {} on {}. Do you want to clear likes on these? (y/N) '.format(
                count, type_name, context.title).encode('utf8'))
            if response.lower() in ('y', 'yes', 'j', 'ja'):
                cleared = True
                for obj in request.resolve_docids(docids, perm=None):
                    like = request.registry.getAdapter(obj, IUserTags, name='like')
                    like.storage.clear()
                    like._notify()

        if cleared:
            transaction.commit()
            env['closer']()

    else:
        print('Path does not match a meeting or agenda item')

if __name__ == '__main__':
    main()
