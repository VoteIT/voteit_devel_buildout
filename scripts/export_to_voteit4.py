# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import argparse
import re
import sys
from datetime import datetime
from datetime import timedelta
from json import dump
from json import dumps
from uuid import uuid4

from pyramid.paster import bootstrap
from pyramid.traversal import resource_path
from pyramid.traversal import find_interface
from voteit.core.helpers import AT_PATTERN
from voteit.core.helpers import TAG_PATTERN
from voteit.core.models.interfaces import IDiffText
from voteit.core.models.interfaces import IMeeting
from voteit.core.models.interfaces import IMentioned
from voteit.core.security import ROLE_ADMIN
from voteit.core.security import ROLE_DISCUSS
from voteit.core.security import ROLE_MEETING_CREATOR
from voteit.core.security import ROLE_MODERATOR
from voteit.core.security import ROLE_OWNER
from voteit.core.security import ROLE_PROPOSE
from voteit.core.security import ROLE_VIEWER
from voteit.core.security import ROLE_VOTER
from voteit.debate.interfaces import ISpeakerListSettings
from voteit.debate.models import speaker_lists
from voteit.irl.models.interfaces import IElectoralRegister
from voteit.irl.models.interfaces import IParticipantNumbers

# Settings
SINGLE_ERROR = False
ONLY_MEETING_NAME = None
REPORT_NOT_CLOSED = False
REPORT_TRUNCATED_TAGS_AS_ERROR = False

userid_to_pk = {}
user_pk_to_fullname = {}
meeting_name_to_pk = {}
ai_name_to_pk = {}
ai_uid_to_pk = {}
proposal_uid_to_pk = {}
# key like f"{ai_pk}:{paragraph}"
diff_text_ai_pk_and_paragraph_to_pk = {}
needed_userids = set()
long_tag_to_trunc = {}

errors = {}
unique_errors = set()
critical_errors = set()
emails = set()


def get_pk_for_userid(userid):
    needed_userids.add(userid)
    return userid_to_pk[userid]


def truncate_tag(tag):
    """
    something like: 21-c-val-av-riksforbundets-styrelse-ordinarie-ledamoter-3
    """
    items = tag.split("-")
    num = items[-1]
    text = "-".join(items[:-1])
    new_tag = text[:49-len(num)] + "-" + num
    long_tag_to_trunc[tag] = new_tag
    return new_tag


def adjust_object_richtext_tags(obj):
    """
    Takes a proposal or a discussion post and replaces the tag in the text body and in tags.
    Adjust object in place since we won't save anything anyway.
    """
    tags_to_adjust = set()
    if obj.type_name == 'Proposal':
        if len(obj.aid) > 50:
            if REPORT_TRUNCATED_TAGS_AS_ERROR:
                add_error(obj, "AID tag too long, will be truncated: {tag}", tag=obj.aid)
            obj.aid = truncate_tag(obj.aid)
            tags_to_adjust.add(obj.aid)
    for tag in obj.tags:  # new copy!
        if len(tag) > 50:
            truncate_tag(tag)
            if REPORT_TRUNCATED_TAGS_AS_ERROR:
                add_error(obj, "Tag too long: {tag}", tag=tag)
            tags_to_adjust.add(tag)
    # Silly version of replace i guess
    if tags_to_adjust:
        out = []
        for item in obj.text.split(" "):
            # Hash sign first
            tag = item[1:]
            if tag in tags_to_adjust:
                out.append("#" + long_tag_to_trunc[tag])
            else:
                out.append(item)
        # print("ORIG TEXT:")
        # print(obj.text)
        obj.text = " ".join(out)
        # print("NEW TEXT:")
        # print(obj.text)
        # print("-"*80)
    return obj

# VoteIT3 as key
poll_method_mapping = {
    'schulze': 'schulze',
    'scottish_stv': 'scottish_stv',
    'sorted_schulze': 'repeated_schulze',
    'majority_poll': 'majority',
    'combined_simple': 'combined_simple',
    'dutt_poll': 'dutt',
    'schulze_pr': 'schulze_pr',
    'schulze_stv': 'schulze_stv',
}


role_map = {
    ROLE_VIEWER: "participant",
    ROLE_MODERATOR: 'moderator',
    ROLE_VOTER: 'potential_voter',  # Quirk!
    ROLE_DISCUSS: 'discusser',
    ROLE_PROPOSE: 'proposer',
    ROLE_ADMIN: '',
    ROLE_MEETING_CREATOR: '',
    ROLE_OWNER: '',
}


def add_error(obj, msg, critical=False, **kwargs):
    if critical:
        critical_errors.add(msg)
        msg = "CRIT: " + msg
    if SINGLE_ERROR and msg in unique_errors:
        return
    errs = errors.setdefault(resource_path(obj), [])
    errs.append(msg.format(**kwargs))
    unique_errors.add(msg)


def debugencode(fn):
    def _inner(*args,**kwargs):
        result = fn(*args, **kwargs)
        try:
            if not result:
                return
            dumps(result)
        except Exception as exc:
            print("fn %s cased exc with data:" % fn.__name__)
            print(result)
            raise
        if {'pk', 'fields', 'model'} != set(result):
            raise ValueError("Wrong keys in result: %s" % result.keys())
        return result

    return _inner

hashtag_tag = """
<span class="mention" data-index="0" data-denotation-char="#" data-id="{tag}" data-value="{tag}">
<span contenteditable="false"><span class="ql-mention-denotation-char">#</span>{tag}</span></span> 
"""


def mk_v4_hashtag(tag):
    return hashtag_tag.format(tag=tag)


user_tag = """
<span class="mention" data-index="0" data-denotation-char="@" data-id="{userid}" data-value="{name}">
<span contenteditable="false"><span class="ql-mention-denotation-char">@</span>{name}</span></span>
"""


def mk_v4_usertag(user_pk):
    assert isinstance(user_pk, int)
    name = user_pk_to_fullname[user_pk]
    return user_tag.format(userid=user_pk, name=name)


def text_to_v4_hashtag(text):

    def handle_match(matchobj):
        matched_dict = matchobj.groupdict()
        tag = matched_dict['tag']
        pre = matched_dict['pre']
        return mk_v4_hashtag(tag)

    return re.sub(TAG_PATTERN, handle_match, text)


def text_to_v4_mention(text):

    def handle_match(matchobj):
        # The pattern contains a space, we only find usernames that
        # has a whitespace in front, we save the space so we can put
        # it back after the transformation
        # space, userid = matchobj.group(1, 2)
        userid = matchobj.group(2)
        userid = userid.lower()
        try:
            user_pk = get_pk_for_userid(userid)
        except KeyError:
            # Mentioned user may have been deleted
            return " %s" % userid
        return " %s" % mk_v4_usertag(user_pk)

    return re.sub(AT_PATTERN, handle_match, text)


def convert_richtext_body(text):
    return text_to_v4_hashtag(text_to_v4_mention(text))


@debugencode
def export_root(obj):
    # root -> organisation
    body = obj.body
    if obj.description:
        body = obj.description + "<br/><br/>" + body
    return {
        'pk': 1,#Bogus but doesn't matter until import
        'model':'organisation.organisation',
        'fields': {
            'created': django_format_datetime(obj.created),
            'modified': django_format_datetime(obj.modified),
            'title': obj.title,
            'body': body,
        }
    }


@debugencode
def export_user(user, pk):
    if user.email_validated and user.email:
        email = user.email.lower()
        if email in emails:
            add_error(user, 'Duplicate email: {email}', email=email)
        emails.add(email)
    else:
        email = ""
    if user.userid != user.userid.lower():
        raise ValueError("Uppercase userid: %s" % user.userid)
    return {
        'pk': pk,
        'model':'core.user',
        'fields': {
            'first_name': user.first_name,
            'last_name': user.last_name,
            'date_joined': django_format_datetime(user.created),
            'last_login': django_format_datetime(user.modified),
            'email': email,
            'organisation': 1,  # Will be remapped
            'userid': user.userid,
            'username': str(uuid4()),
        }
    }


@debugencode
def export_meeting_group(user, pk, meeting_pk):
    return {
        'pk': pk,
        'model':'meeting.meetinggroup',
        'fields': {
            'created': django_format_datetime(user.created),
            'modified': django_format_datetime(user.modified),
            'title': user.title,
            'meeting': meeting_pk,
            'members': [],
            'groupid': user.userid,
        }
    }


@debugencode
def export_meeting(meeting, pk):
    return {
        'pk': pk,
        'model': 'meeting.meeting',
        'fields': {
            'title': meeting.title,
            'modified': django_format_datetime(meeting.modified),
            'created': django_format_datetime(meeting.created),
            'body': meeting.body,
            'state': meeting.get_workflow_state(),
            'start_time': django_format_datetime(meeting.start_time),
            'end_time': django_format_datetime(meeting.end_time),
            'er_policy_name': None,  #FIXME: Maybe we need a legacy method?
            'organisation': 1,  # Remapped on import
        },
    }


@debugencode
def export_ai(ai, pk, meeting_pk):
    return {
        'pk': pk,
        'model': 'agenda.agendaitem',
        'fields': {
            'title': ai.title[:100],  # Truncate insanely long ais
            'modified': django_format_datetime(ai.modified),
            'created': django_format_datetime(ai.created),
            'body': ai.body,
            'state': ai.get_workflow_state(),
            #FIXME: These aren't valid for the new AIs, should we keep that data?
            # 'start_time': ai.start_time and django_format_datetime(ai.start_time) or None,
            # 'end_time': ai.end_time and django_format_datetime(ai.end_time) or None,
            'tags': list(ai.tags),
            'meeting': meeting_pk,
            'block_discussion': ai.discussion_block,
            'block_proposals': ai.proposal_block,
            'order': pk, #Should work, since they'll be fetched in order when calling values() on meeting
            #'related_modified': None?
            #'mentions': [],
        },
    }


def reformat_schulze_round(result):
    result['winner'] = proposal_uid_to_pk[result['winner']]
    if len(result) == 1:
        # All other candidates were exhausted so there's nothing else left
        # V4 expects candidates though
        result['candidates'] = [result['winner']]
        return
    result['pairs'] = [[[proposal_uid_to_pk[x] for x in k], v] for k, v in result['pairs'].items()]
    result['candidates'] = [proposal_uid_to_pk[x] for x in result['candidates']]
    result['strong_pairs'] = [[[proposal_uid_to_pk[x] for x in k], v] for k, v in result['strong_pairs'].items()]
    if 'tied_winners' in result:
        result['tied_winners'] = [proposal_uid_to_pk[x] for x in result['tied_winners']]


def get_proposal_with_check(referencing_obj, uid, request):
    meeting = find_interface(referencing_obj, IMeeting)
    prop = request.resolve_uid(uid, perm=None)
    maybe_other_meeting = find_interface(prop, IMeeting)
    if maybe_other_meeting != meeting:
        add_error(referencing_obj, "Must skip export: Proposal from another meeting: {meeting}", critical=True,
                  meeting=resource_path(maybe_other_meeting))
    return proposal_uid_to_pk[uid]

@debugencode
def export_poll(poll, pk, meeting_pk, ai_pk, request, er_pk=None):
    state = poll.get_workflow_state()
    is_closed = state == 'closed'
    is_ongoing = state == 'ongoing'
    if not is_closed and REPORT_NOT_CLOSED:
        add_error(poll, 'Warning: not closed')
    if is_ongoing and er_pk is None:
        add_error(poll, 'Poll open without er, aborting export')
        return
    if not poll.poll_result and is_closed:
        add_error(poll, 'Skipping poll without result data:\n{res}', res=poll.poll_result)
        return
    # Make sure there are no cross-linked proposals!
    proposals = []
    for uid in poll.proposals:
        try:
            proposals.append(get_proposal_with_check(poll, uid, request))
        except KeyError:
            # Are we okay with skipping?
            add_error(poll, "Must skip export: Poll in state {state} contains deleted proposal uid: {uid}", state=state, uid=uid)
            return
    settings = dict(poll.poll_settings)
    poll_plugin = poll.poll_plugin
    result = None
    if is_closed:
        if poll_plugin not in ('dutt_poll', 'majority_poll'):
            try:
                result = dict(poll.poll_result)
            except Exception as exc:
                add_error(poll, "Result data isn't a dict, skipping")
                return

    # Adjust settings and maybe model
    if poll_plugin == 'sorted_schulze':
        winners = settings.get('winners', None)
        if winners == 0:
            settings['winners'] = None
        elif winners == 1:
            # This is basically someone who's done something very weird. And silly us for allowing it.
            poll_plugin = 'schulze'
            if is_closed:
                result = result['rounds'][0]
    elif poll_plugin == 'scottish_stv':
        winners = settings.get('winners', None)
        if winners is None:
            # So this setting is invalid, but 1 will be default in voteit. So if there's no data, we can just assume 1.
            add_error(poll, "Missing settings for Scottish STV, setting winners to 1. Will export")
            settings['winners'] = 1
    elif poll_plugin == 'dutt_poll':
        max_choices = settings.get('max', 0)
        min_choices = settings.get('min', 0)
        if max_choices < min_choices:
            add_error(poll, "Dutt poll with lower max than min. Raised max to {num}", num=min_choices)
            settings['max'] = min_choices

    # Change result format to match V4
    if is_closed:
        if poll_plugin == 'schulze_pr':
            result['candidates'] = [get_proposal_with_check(poll, x, request) for x in result['candidates']]
            result['order'] = [get_proposal_with_check(poll, x, request) for x in result['order']]
            result['rounds'] = [{'winner': get_proposal_with_check(poll, x['winner'], request)} for x in result['rounds']]
        elif poll_plugin == 'schulze':
            if 'winner' not in result:
                add_error(poll, "No winner in result data, skipping")
                return
            reformat_schulze_round(result)
            result['approved'] = [result['winner']]
            result['denied'] = list(set(result['candidates']) - set(result['approved']))
        elif poll_plugin == 'sorted_schulze':
            if 'winners' not in result:
                add_error(poll, "No winners in result data, skipping")
                return
            result['candidates'] = [get_proposal_with_check(poll, x, request) for x in result['candidates']]
            if settings.get('winners', None) is not None:
                result['approved'] = [get_proposal_with_check(poll, x, request) for x in result['winners']]
                result['denied'] = list(set(result['candidates']) - set(result['approved']))
            # Winners aren't part of the new style results
            del result['winners']
            for round in result['rounds']:
                # Modified in place
                reformat_schulze_round(round)

        # class SchulzePollResult(PollResult):
        #     pairs: List[Tuple[Tuple[int, int], int]]
        #     candidates: List[int]
        #     winner: int
        #     strong_pairs: List[Tuple[Tuple[int, int], int]]
        #     tied_winners: Optional[List[int]]
        elif poll_plugin == 'schulze_stv':
            if 'tie_breaker' in result:
                result['tie_breaker'] = [get_proposal_with_check(poll, x, request) for x in result['tie_breaker']]
            if 'tied_winners' in result:
                tied = []
                for row in result['tied_winners']:
                    # Is this the correct format? :)
                    tied.extend([get_proposal_with_check(poll, x, request) for x in row])
                result['tied_winners'] = tied
            result['candidates'] = [get_proposal_with_check(poll, x, request) for x in result['candidates']]
            if 'winners' not in result:
                add_error(poll, "No winners in result data, skipping")
                return
            result['winners'] = result['approved'] = [get_proposal_with_check(poll, x, request) for x in result['winners']]
            # Let's not care about the other parts
            result.pop('actions', None)
            result['denied'] = list(set(result['candidates']) - set(result['winners']))
        elif poll_plugin == 'scottish_stv':
            result['winners'] = result['approved'] = [get_proposal_with_check(poll, x, request) for x in result['winners']]
            result['candidates'] = [get_proposal_with_check(poll, x, request) for x in result['candidates']]
            result['denied'] = list(set(result['candidates']) - set(result['approved']))
            rounds = []
            for round_data in result['rounds']:
                # Expects:
                # class STVResultRoundSchema(BaseModel):
                #     method: str
                #     status: str
                #     selected: List[int]
                #     vote_count: List[Tuple[int, Decimal]]
                round = {}
                round.update(round_data)
                round['selected'] = [get_proposal_with_check(poll, x, request) for x in round_data['selected']]
                round['vote_count'] = []
                for x in round_data['vote_count']:
                    # A dict
                    for k, v in x.items():
                        round['vote_count'].append([get_proposal_with_check(poll, k, request), str(v)])
                rounds.append(round)
            result['rounds'] = rounds
            # Must exist!
            result.setdefault('empty_ballot_count', 0)
            # {u'complete': False, u'quota': 1, u'randomized': False, u'winners': (u'cf555e02-dab9-4520-84fe-6fe3da786105',),
            #  u'candidates': (u'9a532d8c-ea42-496c-9eb2-e61a504381bd', u'e60f4303-d163-406c-9357-8a5bdfa2c7ed',
            #                  u'cf555e02-dab9-4520-84fe-6fe3da786105'), u'runtime': 0.0009920597076416016, u'rounds': (
            # {u'status': u'Elected', u'vote_count': ({u'9a532d8c-ea42-496c-9eb2-e61a504381bd': Decimal('0')},
            #                                         {u'e60f4303-d163-406c-9357-8a5bdfa2c7ed': Decimal('0')},
            #                                         {u'cf555e02-dab9-4520-84fe-6fe3da786105': Decimal('2')}),
            #  u'selected': (u'cf555e02-dab9-4520-84fe-6fe3da786105',), u'method': u'Direct'},), u'empty_ballot_count': 0}
        elif poll_plugin == 'combined_simple':
            reformed_result = {}
            approved = []
            denied = []
            for k, res in result.items():
                reformed_result[get_proposal_with_check(poll, k, request)] = {'yes': res['approve'],
                                                          'no': res['deny'],
                                                          'abstain': res['abstain']}
                # Only clear results!
                # No need to double-check here
                if res['approve'] > res['deny']:
                    approved.append(proposal_uid_to_pk[k])
                elif res['deny'] > res['approve'] :
                    denied.append(proposal_uid_to_pk[k])
            result = {'results': reformed_result, 'approved': approved, 'denied': denied}
        elif poll_plugin == 'dutt_poll':
            # [{'num': 4, 'percent': u'57.1%', 'uid': u'f9635154-68f5-4a17-9940-59a179af6a49'},
            #  {'num': 4, 'percent': u'57.1%', 'uid': u'8bfcf698-92e8-4921-bb68-24f13fdd116e'},
            #  {'num': 3, 'percent': u'42.9%', 'uid': u'15e4ed84-dc5e-4080-af24-1a4752ee6945'},
            #  {'num': 1, 'percent': u'14.3%', 'uid': u'f19460d8-87f8-406e-8d30-5861ad1a6fd3'}]
            if 'num' not in poll.poll_result[0]:
                add_error(poll, "Dutt poll with bad result data, skipping")
                return
            reformatted_results = []
            for item in poll.poll_result:
                reformatted_results.append(
                    {'votes': item['num'], 'proposal': get_proposal_with_check(poll, item['uid'], request)}
                )
            result = {'results': reformatted_results}
        elif poll_plugin == 'majority_poll':
            # And weirdest format so far:
            # ({u'count': 350, u'num': Decimal('0.7070707070707070707070707071'),
            #   u'uid': {'proposal': u'97ba9200-d2d1-49e8-ba81-37cb076b3829'}},
            #  {u'count': 145, u'num': Decimal('0.2929292929292929292929292929'),
            #   u'uid': {'proposal': u'e6ee02b9-aff4-43ca-9b46-d53487dbca61'}})
            reformatted_results = []
            for item in poll.poll_result:
                reformatted_results.append(
                    {'votes': item['count'],
                     'proposal': get_proposal_with_check(poll, item['uid']['proposal'], request)}
                )
            reformatted_results.sort(key=lambda x: x['votes'])
            result = {'results': reformatted_results}
            if len(reformatted_results) > 1:
                if reformatted_results[0]["votes"] != reformatted_results[1]["votes"]:
                    result['approved'] = [reformatted_results[0]['proposal']]
                    result['denied'] = [x['proposal'] for x in reformatted_results[1:]]
            if len(reformatted_results) == 1:  # Don't ask...
                result['approved'] = [reformatted_results[0]['proposal']]

        result['vote_count'] = len(poll)

    data = {
        'pk': pk,
        'model': 'poll.poll',
        'fields': {
            'title': poll.title[:70],
            'modified': django_format_datetime(poll.modified),
            'created': django_format_datetime(poll.created),
            'started': django_format_datetime(poll.start_time),
            'closed': django_format_datetime(poll.end_time),
            'body': poll.description,
            'state': is_closed and 'finished' or state,
            'meeting': meeting_pk,
            'agenda_item': ai_pk,
            'method_name': poll_method_mapping[poll_plugin],
            'proposals': proposals,
            'electoral_register': is_ongoing and er_pk or None,
            #FIXME: This needs to be converted to a format that VoteIT4 expects
            'settings_data': settings,
            'abstains': 0,  # ?
            # ballot_checksum: null ?
        },
    }
    if is_closed:
        data['fields']['result_data'] = result
        data['fields']['ballot_data'] = poll.ballots
    return data


def reverse_schulze_vote(poll, vote_data):
    """ This changes the old schulze vote data to the v4 format.
        VoteIT3 used ballot ranking where a low number was a good thing.
        V4 has 0 for "not ranked" and then points instead

        Example:
            Ranking [[10, 6], [20, 1]] -> [[10, 0], [20, 5]]
    """
    # print('Settings: %s ' % poll.poll_settings.get('max_stars'))
    max_stars = poll.poll_settings.get('max_stars', 5) + 1
    try:
        assert all(x[1] <= max_stars for x in vote_data)
    except AssertionError:
        sys.exit("Poll %s contained settings max_stars %s" % (resource_path(poll), poll.poll_settings.get('max_stars')))
    return sorted([[x[0], max_stars-x[1]] for x in vote_data], key=lambda x: x[0])


@debugencode
def export_vote(vote, pk, poll_pk, user_pk):
    if vote.__name__ not in userid_to_pk:
        add_error(vote, "Duplicate vote, skipping")
        return
    orig_vote_data = vote.get_vote_data()
    poll = vote.__parent__
    if poll.poll_plugin == 'majority_poll':
        # Vote data like: '{"choice": 1}'
        vote_data = dumps({'choice': proposal_uid_to_pk[orig_vote_data['proposal']]})
    elif poll.poll_plugin in ('schulze_pr', 'schulze', 'schulze_stv', 'sorted_schulze'):
        items = []
        for uid, ranking in orig_vote_data.items():
            items.append(
                [proposal_uid_to_pk[uid], int(ranking)]
            )
        reversed = reverse_schulze_vote(poll, items)
        vote_data = dumps(reversed)
    elif poll.poll_plugin == 'combined_simple':
        data = {'yes': [], 'no': [], 'abstain': []}
        for uid, choice in orig_vote_data.items():
            if choice == 'approve':
                data['yes'].append(proposal_uid_to_pk[uid])
            elif choice == 'deny':
                data['no'].append(proposal_uid_to_pk[uid])
            elif choice in ('abstain', ''):
                data['abstain'].append(proposal_uid_to_pk[uid])
            else:
                raise ValueError("Corrupt data within vote_data: %s" % orig_vote_data)
        vote_data = dumps(data)
    elif poll.poll_plugin == 'dutt_poll':
        vote_data = dumps(sorted([proposal_uid_to_pk[x] for x in orig_vote_data['proposals']]))
        # Malformed previous export data:
        # vote_data = dumps({'choices': sorted([proposal_uid_to_pk[x] for x in orig_vote_data['proposals']])})
    elif poll.poll_plugin == 'scottish_stv':
        vote_data = ",".join([str(proposal_uid_to_pk[x]) for x in orig_vote_data['proposals']])
        # Malformed previous export data:
        # vote_data = dumps({'ranking': [proposal_uid_to_pk[x] for x in orig_vote_data['proposals']]})
    else:
        print("Vote data for %s:" % poll.poll_plugin)
        print(vote.get_vote_data())
        sys.exit("Must handle vote data")
    return {
        'pk': pk,
        'model': 'poll.vote',
        'fields': {
            'user': user_pk,
            'poll': poll_pk,
            'created': django_format_datetime(vote.created, force=True),
            'changed': django_format_datetime(vote.modified, force=True),
            'vote_data': vote_data,
        },
    }


@debugencode
def export_proposal(proposal, pk, ai_pk, author_pk=None, meeting_group_pk=None):
    assert bool(author_pk) != bool(meeting_group_pk)
    adjust_object_richtext_tags(proposal)
    body = convert_richtext_body(proposal.text)
    data = {
        'pk': pk,
        'model': 'proposal.proposal',
        'fields': {
            'modified': django_format_datetime(proposal.modified),
            'created': django_format_datetime(proposal.created),
            'body': body,
            'state': proposal.get_workflow_state(),
            'prop_id': proposal.aid,
            'agenda_item': ai_pk,
            'tags': proposal.tags,
            'mentions': [get_pk_for_userid(x) for x in IMentioned(proposal).keys()]
        },
    }
    if author_pk:
        data['fields']['author'] = author_pk
    else:
        data['fields']['meeting_group'] = meeting_group_pk
    return data


@debugencode
def export_diff_proposal(pk, diff_text_para, ai_pk):
    diff_key = "{}:{}".format(ai_pk, diff_text_para)
    return {
        'pk': pk,
        'model': 'proposal.diffproposal',
        'fields': {
            'paragraph': diff_text_ai_pk_and_paragraph_to_pk[diff_key],
            # 'proposal_ptr': pk,  #ptr = 'pointer'
        },
    }


@debugencode
def export_discussion_post(discussion_post, pk, ai_pk, author_pk = None, meeting_group_pk = None):
    assert bool(author_pk) != bool(meeting_group_pk)
    adjust_object_richtext_tags(discussion_post)
    body = convert_richtext_body(discussion_post.text)
    data = {
        'pk': pk,
        'model': 'discussion.discussionpost',
        'fields': {
            'modified': django_format_datetime(discussion_post.modified),
            'created': django_format_datetime(discussion_post.created),
            'body': body,
            'tags': discussion_post.tags,
            'mentions': [get_pk_for_userid(x) for x in IMentioned(discussion_post).keys()],
            'agenda_item': ai_pk,
        },
    }
    if author_pk:
        data['fields']['author'] = author_pk
    else:
        data['fields']['meeting_group'] = meeting_group_pk
    return data


@debugencode
def export_text_document(diff_text, pk, ai_pk):
    return {
        'pk': pk,
        'model': 'proposal.textdocument',
        'fields': {
            'modified': django_format_datetime(diff_text.context.modified),
            'created': django_format_datetime(diff_text.context.created),
            'title': diff_text.title,
            'body': diff_text.text,
            'base_tag': diff_text.hashtag,
            'agenda_item': ai_pk,
        },
    }


@debugencode
def export_text_paragraph(text, pk, ts, paragraph_id, text_document_pk, ai_pk):
    assert isinstance(ts, datetime)
    assert isinstance(paragraph_id, int)
    return {
        'pk': pk,
        'model': 'proposal.textparagraph',
        'fields': {
            'modified': django_format_datetime(ts),
            'created': django_format_datetime(ts),
            'body': text,
            'paragraph_id': paragraph_id,
            'text_document': text_document_pk,
            'agenda_item': ai_pk,
        },
    }


@debugencode
def export_pn_system(pk, meeting_pk):
    return {
        'pk': pk,
        'model': 'participant_number.pnsystem',
        'fields': {
            'meeting': meeting_pk
        },
    }


@debugencode
def export_pn(pk, number, user_pk, pns_pk, created_ts):
    return {
        'pk': pk,
        'model': 'participant_number.participantnumber',
        'fields': {
            'number': number,
            'user': user_pk,
            'pns': pns_pk,
            'created': django_format_datetime(created_ts, force=True),
        },
    }


@debugencode
def export_electoral_register(pk, created_ts, meeting_pk):
    return {
        'pk': pk,
        'model': 'poll.electoralregister',
        'fields': {
            'created': django_format_datetime(created_ts, force=True),
            'meeting': meeting_pk,
        },
    }


@debugencode
def export_voter_weight(pk, register_pk, user_pk, weight=1):
    return {
        'pk': pk,
        'model': 'poll.voterweight',
        'fields': {
            'register': register_pk,
            'user': user_pk,
            'weight': weight,
        },
    }


@debugencode
def export_speaker_list_system(pk, meeting_pk, method_name, settings, safe_positions):
    return {
        'pk': pk,
        'model': 'speaker.speakerlistsystem',
        'fields': {
            'state': 'archived',
            # 'title': '',
            'meeting': meeting_pk,
            'method_name': method_name,
            'settings_data': settings,
            'safe_positions': safe_positions,
            # 'meeting_roles_to_speaker'?
        },
    }


@debugencode
def export_speaker(pk, user_pk, speaker_list_pk, created_ts, seconds):
    assert isinstance(seconds, int)
    return {
        'pk': pk,
        'model': 'speaker.speaker',
        'fields': {
            'user': user_pk,
            'speaker_list': speaker_list_pk,
            'created': django_format_datetime(created_ts, force=True),
            'started': django_format_datetime(created_ts, force=True),  # We don't have this data
            'seconds': seconds,
        },
    }


@debugencode
def export_speaker_list(pk, speaker_system_pk, agenda_item_pk, sl_title):
    return {
        'pk': pk,
        'model': 'speaker.speakerlist',
        'fields': {
            'title': sl_title,
            'state': 'closed',
            'speaker_system': speaker_system_pk,
            'agenda_item': agenda_item_pk,
        },
    }


@debugencode
def export_meeting_roles(pk, entry, meeting_pk):
    # No duplicates
    assigned = set([role_map[x] for x in entry['groups'] if role_map[x]])
    try:
        user_pk = get_pk_for_userid(entry['userid'])
    except KeyError:
        return
    return {
        'pk': pk,
        'model': 'meeting.meetingroles',
        'fields': {
            'context': meeting_pk,
            'user': user_pk,
            'assigned': list(assigned),
        },
    }


def django_format_datetime(dt, force=False):
    if force and not isinstance(dt, datetime):
        raise ValueError("%s is not a datetime instance" % dt)
    if dt is not None:
        return dt.isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_uri", help="Paster ini file to load settings from")
    args = parser.parse_args()
    env = bootstrap(args.config_uri)
    root = env['root']
    request = env['request']
    users = root['users']
    print("Exporting %s" % root.title)

    data = []
    data.append(export_root(root))
    maybe_export_users = {}  # Userid to export

    print("Exporting %s users" % len(users))
    # Export users and map userids
    user_pk = 1
    for user in users.values():
        userid_to_pk[user.userid] = user_pk
        user_pk_to_fullname[user_pk] = user.title
        maybe_export_users[user.userid] = export_user(user, user_pk)
        user_pk += 1

    # FIXME: Should we export admins too?

    # Walk meetings and export contents
    ai_pk = 1
    meeting_group_pk = 1
    poll_pk = 1
    vote_pk = 1
    proposal_pk = 1
    discussion_post_pk = 1
    text_document_pk = 1
    text_paragraph_pk = 1
    pn_system_pk = 1
    pn_pk = 1
    electoral_register_pk = 1
    voter_weight_pk = 1
    speaker_system_pk = 1
    speaker_list_pk = 1
    speaker_pk = 1
    meeting_roles_pk = 1

    meetings = [x for x in root.values() if x.type_name == 'Meeting']
    print("Exporting %s meetings" % len(meetings))
    for meeting_pk, meeting in enumerate(meetings, start=1):
        # Limit to a single meeting?
        if ONLY_MEETING_NAME:
            if meeting.__name__ != ONLY_MEETING_NAME:
               continue
            print("WARNING: Only exporting meeting with name %s" % ONLY_MEETING_NAME)
        else:
            print("Exporting: %s" % meeting.__name__)
        data.append(
            export_meeting(meeting, meeting_pk)
        )
        meeting_name_to_pk[meeting.__name__] = meeting_pk
        # Meeting groups are meeting local objects within VoteIT4
        # System users are global within VoteIT3, so if we convert sys users -> meeting group
        # we need to create several of them and replace the differently within meetings.
        userid_to_meeting_group_pk = {}
        for userid in meeting.system_userids:
            userid_to_meeting_group_pk[userid] = meeting_group_pk
            needed_userids.add(userid)
            sys_user = users[userid]
            data.append(
                export_meeting_group(sys_user, meeting_group_pk, meeting_pk)
            )
            # End meeting group loop
            meeting_group_pk += 1

        # Export meeting roles
        maybe_new_er_userids = set()  # In case there's no er, create one with these
        for entry in meeting.get_security():
            if entry['userid'].lower() != entry['userid']:
                add_error(meeting, 'UserID with uppercase in get_security(): {userid}', userid=entry['userid'])
            if not entry['userid']:
                add_error(meeting, 'Empty userid in get_security()')
                continue
            out = export_meeting_roles(meeting_roles_pk, entry, meeting_pk)
            if out:
                data.append(out)
                if ROLE_VOTER in entry['groups']:
                    maybe_new_er_userids.add(entry['userid'])
                # End roles loop
                meeting_roles_pk += 1
            else:
                add_error(meeting, 'Skipping meeting roles assigned to non-existing user: {userid}', userid=entry['userid'])

        # Export participant numbers
        pn_to_userid = {}

        pns = IParticipantNumbers(meeting)
        if len(pns.number_to_userid):
            pn_to_userid.update(pns.number_to_userid)
            data.append(
                export_pn_system(pn_system_pk, meeting_pk)
            )
            assigned_userids = set()
            for pn, userid in pn_to_userid.items():
                try:
                    created_ts = pns[pn].created
                except (KeyError, AttributeError):
                    # The only fallback we have i guess
                    created_ts = meeting.created
                if userid in assigned_userids:
                    add_error(meeting, "WARNING: {userid} has several participant numbers. Export won't work!", userid=userid)
                    continue
                assigned_userids.add(userid)
                data.append(
                    export_pn(pn_pk, pn, get_pk_for_userid(userid), pn_system_pk, created_ts)
                )
                # End pn loop
                pn_pk += 1
            # End pn system
            pn_system_pk += 1

        # Electoral registers - might not exist
        electoral_registers = IElectoralRegister(meeting)

        exportable_ers = []
        exportable_ers.extend(electoral_registers.registers.values())
        # Maybe always export voters...?
        if maybe_new_er_userids:
            if exportable_ers:
                # We may need to extend with current users if the existing er is wrong?
                last_er = exportable_ers[-1]
                if set(last_er['userids']) != maybe_new_er_userids:
                    # We don't really have good data for this - but the newly created one must have the newest timestamp
                    time_ts = last_er['time'] + timedelta(minutes=1)
                    exportable_ers.append({'userids': list(maybe_new_er_userids), 'time': time_ts})
            else:
                # We have no clue about time but let's use the meeting start time?
                # Make it look like an electoral register
                time_ts = meeting.start_time
                if not time_ts:
                    # This may happen for meetings that haven't started
                    time_ts = meeting.created
                exportable_ers.append({'userids': list(maybe_new_er_userids), 'time': time_ts})

        lastest_er_pk = None
        for register in exportable_ers:
            # Seems like ER needs to be first, so delay appending voterweight
            er_voters_data = []
            voters = []
            for userid in register['userids']:
                er_voters_data.append(
                    export_voter_weight(voter_weight_pk, electoral_register_pk, get_pk_for_userid(userid))
                )
                voters.append(voter_weight_pk)
                # end voter weight loop
                voter_weight_pk += 1
            data.append(
                export_electoral_register(electoral_register_pk, created_ts=register['time'], meeting_pk=meeting_pk)
            )
            data.extend(er_voters_data)
            lastest_er_pk = electoral_register_pk
            # end er loop
            electoral_register_pk += 1

        # Walk all AIs and meeting content
        for ai in meeting.values():
            if ai.type_name != 'AgendaItem':
                continue
            data.append(
                export_ai(ai, ai_pk, meeting_pk)
            )
            ai_name_to_pk[ai.__name__] = ai_pk
            ai_uid_to_pk[ai.uid] = ai_pk

            # First the diff-text stuff if it exists
            diff_text = IDiffText(ai)
            if diff_text.hashtag:
                data.append(
                    export_text_document(diff_text, text_document_pk, ai_pk)
                )
                # Note! paragraph_id starts at 0 while proposal.diff_text_para starts at 0!
                for para_i, paragraph in enumerate(diff_text.get_paragraphs(), start=0):
                    data.append(
                        export_text_paragraph(paragraph, text_paragraph_pk, diff_text.context.modified, para_i + 1,
                                              text_document_pk, ai_pk)
                    )
                    diff_key = "{}:{}".format(ai_pk, para_i)
                    diff_text_ai_pk_and_paragraph_to_pk[diff_key] = text_paragraph_pk
                    # End paragraph loop
                    text_paragraph_pk += 1
                # END text document loop
                text_document_pk += 1

            items = {'Poll': [], 'Proposal': [], 'DiscussionPost': []}
            for obj in ai.values():
                items[obj.type_name].append(obj)

            for proposal in items['Proposal']:
                assert len(proposal.creators) == 1
                author_userid = proposal.creators[0]
                if author_userid in userid_to_meeting_group_pk:
                    kw = {'meeting_group_pk': userid_to_meeting_group_pk[author_userid]}
                else:
                    kw = {'author_pk': get_pk_for_userid(author_userid)}
                data.append(
                    export_proposal(proposal, proposal_pk, ai_pk, **kw)
                )
                proposal_uid_to_pk[proposal.uid] = proposal_pk
                # But there might be more! Is this proposal a difftext one?
                if proposal.diff_text_para is not None:
                    # Diff proposals have a 1-1 relation with their parent proposal. They prefer to use the same pk.
                    data.append(
                        export_diff_proposal(proposal_pk, proposal.diff_text_para, ai_pk)
                    )
                    # End diff prop
                # End proposal loop
                proposal_pk += 1
            for discussion_post in items['DiscussionPost']:
                assert len(discussion_post.creators) == 1
                author_userid = discussion_post.creators[0]
                if author_userid in userid_to_meeting_group_pk:
                    kw = {'meeting_group_pk': userid_to_meeting_group_pk[author_userid]}
                else:
                    kw = {'author_pk': get_pk_for_userid(author_userid)}
                data.append(
                    export_discussion_post(discussion_post, discussion_post_pk, ai_pk, **kw)
                )
                # End post loop
                discussion_post_pk += 1
            for poll in items['Poll']:
                out = export_poll(poll, poll_pk, meeting_pk, ai_pk, request, er_pk=lastest_er_pk)
                if out:
                    data.append(out)

                    # And export votes
                    for vote in poll.values():
                        out = export_vote(vote, vote_pk, poll_pk, get_pk_for_userid(vote.creator[0]))
                        if out:
                            data.append(out)
                            # End vote loop
                            vote_pk += 1

                    # End poll loop
                    poll_pk += 1

            # END ai block - keep this last!
            ai_pk += 1

        # Speaker lists - we can only export one speaker list system since we don't know about relations to
        # categories for voteit3
        sls = speaker_lists(request, meeting)

        if len(sls.data):
            # System has lists
            # Schema and settings lookup doesn't seem to work as it should

            v3_settings = ISpeakerListSettings(meeting)
            method_name = ''
            # Cherry-pick settings...
            settings = {}
            safe_positions = v3_settings.get('safe_positions', 1)
            speaker_list_count = v3_settings.get('speaker_list_count', 0)
            if speaker_list_count == 1:
                method_name = 'simple'
            else:
                if v3_settings.get('speaker_list_plugin', '') == '':
                    method_name = 'priority'
                    settings['max_times'] = speaker_list_count
                    assert isinstance(settings['max_times'], int)
                elif v3_settings['speaker_list_plugin'] == 'female_priority':
                    add_error(meeting, "'female_priority' speaker lists aren't handled. (path is meeting)")
                    continue
                elif v3_settings['speaker_list_plugin'] == 'global_lists':
                    add_error(meeting, "'global_lists' speaker lists aren't handled. (path is meeting)")
                    continue
            if not method_name:
                import pdb;pdb.set_trace()
            data.append(
                export_speaker_list_system(speaker_system_pk, meeting_pk, method_name, settings, safe_positions)
            )
            # We're not exporting active lists, queues or anything like that. Everything should be finished.
            for (k, sl) in sls.items():
                ai_uid, num = k.split('/')
                if ai_uid not in ai_uid_to_pk:
                    # print("UID %s belongs to a deleted agenda item, won't export that speaker list" % ai_uid)
                    continue
                for user_pn, spoken_times in sl.speaker_log.items():
                    if not spoken_times:
                        continue
                    if user_pn not in pn_to_userid:
                        continue  # We can't export historic items that are from an anonymous user
                    # This is certainly wrong, but we don't have any other data to use :(
                    created_ts = sl.__parent__.modified
                    for seconds in spoken_times:
                        data.append(
                            export_speaker(speaker_pk, get_pk_for_userid(pn_to_userid[user_pn]),
                                           speaker_list_pk, created_ts, seconds)
                        )
                        # end speaker loop
                        speaker_pk += 1
                data.append(
                    export_speaker_list(speaker_list_pk, speaker_system_pk, ai_uid_to_pk[ai_uid], sl.title)
                )
                # End speaker list loop
                speaker_list_pk += 1

            # END speaker system block
            speaker_system_pk += 1

    # Append users we care about
    skipped = 0
    for userid, usr_data in maybe_export_users.items():
        if userid in needed_userids:
            data.insert(0, usr_data)
        else:
            skipped += 1
    if skipped:
        print("Maybe we can skip export of %s users" % skipped)


    # FIXME: Röster som är duplicerade?
    # FIXME: presence ?
    # FIXME: Exporten av resultatdata för schulze använder ranking istället för rating, så vi måste vända på siffrorna!
    # FIXME: Vad gör vi med ballot_data för historiska omröstningar?
    # FIXME: Gilla-knappar blir problematiskt med tanke på ContentTypes i django - natural key?

    if not critical_errors and long_tag_to_trunc:
        print("The following tags are too long and need to be adjusted:")
        print("-"*40)
        for longtag,truncated in long_tag_to_trunc.items():
            print(truncated.ljust(53) + "->  " + longtag)

    if errors:
        print("-"*80)
        print("There were errors during import:")
        print("="*80)
        for k, v in errors.items():
            print(k)
            for err in v:
                print(" - " + err)
        if SINGLE_ERROR:
            print("!!! NOTE !!! Errors were omitted, unset SINGLE_ERROR to display all")
    else:
        print("Everything worked as expected!")
    if critical_errors:
        sys.exit("!!! %s critical errors - won't write!" % len(critical_errors))
    filename = 'voteit4_export.json'
    print("Writing %s" % filename)
    with open(filename, "w") as stream:
        dump(data, stream)


if __name__ == '__main__':
    main()
