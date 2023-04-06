# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from json import dump
from json import dumps
from uuid import uuid4

from arche.security import ROLE_EDITOR
from arche.security import ROLE_REVIEWER
from arche.utils import resolve_uid
from arche_usertags.interfaces import IUserTags
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
from voteit.multiple_votes import MEETING_NAMESPACE

try:
    from voteit.vote_groups.interfaces import ROLE_PRIMARY
    from voteit.vote_groups.interfaces import ROLE_STANDIN
except ImportError:
    ROLE_PRIMARY = ROLE_STANDIN = None


# Settings
SINGLE_ERROR = False
ONLY_MEETING_NAMES=[]
DIE_ON_CRITICAL = False
DEBUG_ENCODE = False
REPORT_NOT_CLOSED = False
REPORT_TRUNCATED_TAGS_AS_ERROR = False
REPORT_SCHULZE_STV = False
REPORT_EMPTY_POLLS = False
REPORT_DUPLICATE_EMAIL = False
VOTE_GROUPS_NAMESPACE = '_vote_groups'
SFS_DELEGATIONS_NAMESPACE = '__delegations__'

userid_to_pk = {}
user_pk_to_fullname = {}
meeting_name_to_pk = {}
meeting_to_user_pks = {}
reported_meeting_to_user_pks = {}
ai_name_to_pk = {}
ai_uid_to_pk = {}
proposal_uid_to_pk = {}
# key like f"{ai_pk}:{paragraph}"
diff_text_ai_pk_and_paragraph_to_pk = {}
needed_userids = set()
long_tag_to_trunc = {}
pns_pn_check = {}
pk_to_old_pns = {}

userid_force_swap_email = {
}

errors = {}
unique_errors = set()
critical_errors = set()
email_to_userid = {}
ai_prop_ids = {}
meeting_groupids = {}

def get_pk_for_userid(userid, context=None, msg=None, ck_meeting_pk=None):
    needed_userids.add(userid)
    try:
        user_pk = userid_to_pk[userid]
    except KeyError:
        if context:
            if msg is None:
                msg="Missing user '{userid}'"
            add_error(context, msg,userid=userid, critical=True)
            return
        else:
            raise
    if ck_meeting_pk:
        if user_pk not in meeting_to_user_pks[ck_meeting_pk]:
            already_reported = reported_meeting_to_user_pks.setdefault(ck_meeting_pk, set())
            if user_pk in already_reported:
                return user_pk
            # Figure out meeting name
            meeting_name = "(Unknown meeting with export pk %s)" % ck_meeting_pk
            for (k, v) in meeting_name_to_pk.items():
                if v == ck_meeting_pk:
                    meeting_name = k
                    break
            add_error(context, "ck_meeting_pk failed for meeting {meeting_name}, userid {userid} not part of meeting", meeting_name=meeting_name, userid=userid)
            already_reported.add(user_pk)
    return user_pk


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
    'irv': 'irv',
    'repeated_irv':'repeated_irv',
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
    ROLE_REVIEWER: '',
    ROLE_EDITOR: '',
}


model_map = {
    'DiscussionPost': 'discussion_post',
    'Meeting': 'meeting',
    'Proposal': 'proposal',
}


def add_error(obj, msg, critical=False, **kwargs):

    def _get_path(obj):
        try:
            return resource_path(obj)
        except AttributeError:
            return str(obj) + " without traversal"


    if critical:
        critical_errors.add(msg.format(**kwargs))
        msg = "CRIT: " + msg
        if DIE_ON_CRITICAL:

            raise Exception(_get_path(obj) + "   " + msg.format(**kwargs))
    if SINGLE_ERROR and msg in unique_errors:
        return
    errs = errors.setdefault(_get_path(obj), [])
    errs.append(msg.format(**kwargs))
    unique_errors.add(msg)


def debugencode(fn):
    def _inner(*args,**kwargs):
        result = fn(*args, **kwargs)
        if not DEBUG_ENCODE:
            return result
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

hashtag_tag = """<span class="mention" data-index="0" data-denotation-char="#" data-id="{tag}" data-value="{tag}"><span contenteditable="false"><span class="ql-mention-denotation-char">#</span>{tag}</span></span>
"""

def mk_v4_hashtag(tag):
    return hashtag_tag.format(tag=tag)

user_tag = """<span class="mention" data-index="0" data-denotation-char="@" data-id="{userid}" data-value="{name}"><span contenteditable="false"><span class="ql-mention-denotation-char">@</span>{name}</span></span>
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

def add_paras(text):
    if '<p>' in text.lower():
        return text
    text = text.strip()
    text = re.sub(r"(\s*)[\n]{2,}", "</p>\n<p>", text)
    reformatted = ""
    for row in text.splitlines():
        row = row.strip()
        if not row.endswith(">"):
            row += "<br/>"
        reformatted += row + "\n"
    text = "<p>" + reformatted + "</p>"
    text = text.replace("<br/>\n</p>", "</p>")
    text = re.sub(r"(<br/>\n){2,}", "</p>\n<p>", text, flags=re.DOTALL)
    return text


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
    #if user.userid != user.userid.lower():
    #    raise ValueError("Uppercase userid: %s" % user.userid)
    # Even if the email address isn't validated, it shouldn't matter that much since to
    # inherit the account it needs to be validated
    if user.email:
        email = user.email.lower()
        if user.userid in userid_force_swap_email:
            print("Force-swapping email %s -> %s" % (email, userid_force_swap_email[user.userid]))
            email = userid_force_swap_email[user.userid]
        if email in email_to_userid and REPORT_DUPLICATE_EMAIL:
            add_error(user, 'Duplicate email: {email} also used by userid {userid}',  userid=email_to_userid[email], email=email)
        email_to_userid[email] = user.userid
    else:
        email = ""
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

def check_groupid(groupid, meeting_pk, meeting):
    groupids = meeting_groupids.setdefault(meeting_pk, set())
    if groupid in groupids:
        add_error(meeting, "{groupid} not unique for meeting", groupid=groupid, critical=True)
    groupids.add(groupid)


@debugencode
def export_meeting_group_system_user_like(user, pk, meeting_pk, meeting):
    check_groupid(user.userid, meeting_pk, meeting)
    return {
        'pk': pk,
        'model':'meeting.meetinggroup',
        'fields': {
            'created': django_format_datetime(user.created),
            'modified': django_format_datetime(user.modified),
            'title': user.title,
            'meeting': meeting_pk,
            'groupid': user.userid,
        }
    }

@debugencode
def export_meeting_group_sfs_origin(delegation, pk, meeting_pk, meeting):
    check_groupid(delegation.name, meeting_pk, meeting)
    return {
        'pk': pk,
        'model':'meeting.meetinggroup',
        'fields': {
            'created': django_format_datetime(meeting.created),
            'modified': django_format_datetime(meeting.modified),
            'title': delegation.title,
            'body': add_paras(delegation.description),
            'meeting': meeting_pk,
            'groupid': delegation.name,
            'votes': delegation.vote_count,
        }
    }

# @debugencode
# def export_meeting_group_mv_origin(va, pk, meeting_pk, meeting):
#
#     check_groupid(user.userid, meeting_pk, meeting)
#     return {
#         'pk': pk,
#         'model':'meeting.meetinggroup',
#         'fields': {
#             'created': django_format_datetime(va.created),
#             'modified': django_format_datetime(va.modified),
#             'title': va.title,
#             'meeting': meeting_pk,
#             'groupid': va.userid,
#         }
#     }
#     title = ""
#     votes = 1
#     userid_assigned = None


@debugencode
def export_meeting_group_vg_origin(vg, pk, meeting_pk, meeting):
    check_groupid(vg.name, meeting_pk, meeting)
    return {
        'pk': pk,
        'model':'meeting.meetinggroup',
        'fields': {
            'created': django_format_datetime(meeting.created), # No clue what to do otherwise
            'modified': django_format_datetime(meeting.modified),
            'title': vg.title,
            'body': add_paras(vg.description),
            'meeting': meeting_pk,
            'groupid': vg.name,
            'votes': len(list(vg.primaries)) or None,
        }
    }

@debugencode
def export_group_role(pk, meeting_pk, role_id='', title='', roles=()):
    if len(role_id) > 100:
        raise ValueError("role_id more than 100 chars: %s" % role_id)
    if len(title) > 100:
        raise ValueError("role title more than 100 chars: %s" % title)
    return {
        'pk': pk,
        'model':'meeting.grouprole',
        'fields': {
            'title': title,
            'meeting': meeting_pk,
            'role_id': role_id,
            'roles': list(roles) or None,
        }
    }

@debugencode
def export_group_membership(pk, user_pk, meeting_group_pk, role_pk=None, votes=None):
    return {
        'pk': pk,
        'model':'meeting.groupmembership',
        'fields': {
            'user': user_pk,
            'meeting_group': meeting_group_pk,
            'role': role_pk,
            'votes': votes,
        }
    }


@debugencode
def export_meeting(meeting, pk):
    return {
        'pk': pk,
        'model': 'meeting.meeting',
        'fields': {
            'title': meeting.title[:100],
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
    if 'tie_breaker' in result:
        result['tie_breaker'] = [proposal_uid_to_pk[x] for x in result['tie_breaker']]
    #A set with nodes and edges for historic tie breaks - we're not going to care about this level of detail.
    result.pop('actions', None)


def get_proposal_with_check(referencing_obj, uid, request):
    meeting = find_interface(referencing_obj, IMeeting)
    prop = request.resolve_uid(uid, perm=None)
    maybe_other_meeting = find_interface(prop, IMeeting)
    if maybe_other_meeting != meeting:
        add_error(referencing_obj, "Must skip export: Proposal from another meeting: {meeting}", critical=True,
                  meeting=resource_path(maybe_other_meeting))
    return proposal_uid_to_pk[uid]

def reformat_stv_like_result(poll, request, result):
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
    if (not poll.poll_result or not len(poll)) and is_closed:
        if REPORT_EMPTY_POLLS:
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
    elif poll_plugin == 'repeated_irv':
        winners = settings.get('winners', None)
        if winners is None:
            if is_closed:
                settings['winners'] = len(result['winners'])
            else:
                settings['winners'] = 1
        if settings['winners'] == 1:
            if is_closed:
                #We're going to assume incomplete result and just bugger off.
                if len(result['winners']) > 1:
                    print("Eh...")
                    import pdb;pdb.set_trace()
            # Someone has done silly stuff
            poll_plugin = 'irv'
        if settings['winners'] < 2 and poll_plugin=='repeated_irv':
            print("Very broken repeated irv")
            import pdb;pdb.set_trace()
    elif poll_plugin == 'scottish_stv':
        winners = settings.get('winners', None)
        if winners is None:
            # So this setting is invalid, but 1 will be default in voteit. So if there's no data, we can just assume 1.
            add_error(poll, "Missing settings for Scottish STV, setting winners to 1. Will export")
            settings['winners'] = 1
        if winners == 0:
            add_error(poll, "Zero winner STV poll, can't export", critical=True)
            return

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
            result['approved'] = []
            result['denied'] = []
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
            if REPORT_SCHULZE_STV:
                add_error(poll, "schulze_stv")
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
                if REPORT_EMPTY_POLLS:
                    add_error(poll, "No winners in result data, skipping")
                return
            result['winners'] = result['approved'] = [get_proposal_with_check(poll, x, request) for x in result['winners']]
            # Let's not care about the other parts
            result.pop('actions', None)
            result['denied'] = list(set(result['candidates']) - set(result['winners']))
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
        elif poll_plugin in ('scottish_stv', 'repeated_irv', 'irv'):
            reformat_stv_like_result(poll, request, result)
        else:
            print("No such poll plugin %s" % poll_plugin)
            add_error(poll, "No such poll plugin {poll_plugin} - skipping export", critical=True, poll_plugin=poll_plugin)
            #import pdb;pdb.set_trace()
            return
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
        add_error(poll, "Poll contained setting max_stars {max_stars} but has votes with {max_vote}",
                  critical=True, max_stars=poll.poll_settings.get('max_stars'), max_vote=max(x[1] for x in vote_data))
    return sorted([[x[0], max_stars-x[1]] for x in vote_data], key=lambda x: x[0])


@debugencode
def export_vote(vote, pk, poll_pk, user_pk):
    if vote.__name__ not in userid_to_pk:
        add_error(vote, "Duplicate vote or deleted user", critical=True)
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
    elif poll.poll_plugin in ('scottish_stv','irv', 'repeated_irv'):
        vote_data = ",".join([str(proposal_uid_to_pk[x]) for x in orig_vote_data['proposals']])
        # Malformed previous export data:
        # vote_data = dumps({'ranking': [proposal_uid_to_pk[x] for x in orig_vote_data['proposals']]})
    else:
        add_error(poll, "Must handle vote data for method {poll_plugin}", critical=True, poll_plugin=poll.poll_plugin)
        return
        #import pdb;pdb.set_trace()
        #print(vote.get_vote_data())
        #sys.exit("Must handle vote data")
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
def export_proposal(proposal, pk, ai_pk, author_pk=None, meeting_group_pk=None, meeting_pk=None):
    if bool(author_pk) == bool(meeting_group_pk):
        add_error(proposal, "Proposal userid error, either author_pk or meeting_group_pk needed. Author was: {author}",
                  author=proposal.creator[0], critical=True)
        return
    adjust_object_richtext_tags(proposal)
    if '\x00' in proposal.text:
        add_error(proposal, "Proposal contains invalid unicode NULL char", critical=True)
    body = convert_richtext_body(proposal.text)
    if proposal.diff_text_para is None:
        body = add_paras(body)
    prop_ids = ai_prop_ids.setdefault(ai_pk, set())
    if proposal.aid in prop_ids:
        add_error(proposal, "Duplicate aid / prop_id: {prop_id}", prop_id = proposal.aid, critical=True)
    prop_ids.add(proposal.aid)
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
            'mentions': [get_pk_for_userid(x, context=proposal, ck_meeting_pk=meeting_pk, msg="Missing user '{userid}' in mentions") for x in IMentioned(proposal).keys()]
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
def export_discussion_post(discussion_post, pk, ai_pk, author_pk = None, meeting_group_pk = None, meeting_pk = None):
    if bool(author_pk) == bool(meeting_group_pk):
        add_error(discussion_post, "DiscussionPost userid error, either author_pk or meeting_group_pk needed. "
                                   "Author was: {author}", critical=True, author=discussion_post.creator[0])
        #import pdb;pdb.set_trace()
        return
    adjust_object_richtext_tags(discussion_post)
    body = convert_richtext_body(discussion_post.text)
    body = add_paras(body)
    data = {
        'pk': pk,
        'model': 'discussion.discussionpost',
        'fields': {
            'modified': django_format_datetime(discussion_post.modified),
            'created': django_format_datetime(discussion_post.created),
            'body': body,
            'tags': discussion_post.tags,
            'mentions': [get_pk_for_userid(x, context=discussion_post, ck_meeting_pk=meeting_pk, msg="Missing user '{userid}' in mentions") for x in IMentioned(discussion_post).keys()],
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
    if len(diff_text.hashtag) > 40:
        #We can't transform these!
        raise Exception("Tag length %s" % len(diff_text.hashtag))
    return {
        'pk': pk,
        'model': 'proposal.textdocument',
        'fields': {
            'modified': django_format_datetime(diff_text.context.modified),
            'created': django_format_datetime(diff_text.context.created),
            'title': diff_text.title[:99], #Truncate insanely long
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
    pns_pn_check[pk] = set()
    return {
        'pk': pk,
        'model': 'participant_number.pnsystem',
        'fields': {
            'meeting': meeting_pk
        },
    }


@debugencode
def export_pn(pk, number, user_pk, pns_pk, created_ts):
    if number in pns_pn_check[pns_pk]:
        add_error(pk_to_old_pns[pns_pk].context, "Duplicate participant number?", critical=True)
    pns_pn_check[pns_pk].add(number)
    if number < 1:
        add_error(pk_to_old_pns[pns_pk].context, "<1 PN", critical=True)
    if number > 2**15:
        add_error(pk_to_old_pns[pns_pk].context, "Over small-int PN", critical=True)
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
def export_electoral_register(pk, created_ts, meeting_pk, was_er=True):
    return {
        'pk': pk,
        'model': 'poll.electoralregister',
        'fields': {
            'created': django_format_datetime(created_ts, force=True),
            'meeting': meeting_pk,
            'source': was_er and 'v3_er_export' or 'v3_voters_export',
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
    if seconds > 30000:
        print("Speaker seconds %s" % seconds)
        seconds = 30000  # Less than smallint :P
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
            'title': sl_title[:200],
            'state': 'closed',
            'speaker_system': speaker_system_pk,
            'agenda_item': agenda_item_pk,
        },
    }


@debugencode
def export_meeting_roles(pk, entry, meeting_pk):
    # No duplicates
    try:
        user_pk = get_pk_for_userid(entry['userid'])
    except KeyError:
        return
    assigned = set([role_map[x] for x in entry['groups'] if role_map[x]])
    if assigned:  # Participants aren't handled while raw-importing
        assigned.add('participant')
    else:
        # Don't export empty assigned. This will cause admins to be blanked, but no problem they can gain access again
        return
    return {
        'pk': pk,
        'model': 'meeting.meetingroles',
        'fields': {
            'context': meeting_pk,
            'user': user_pk,
            'assigned': sorted(assigned),
        },
    }



@debugencode
def export_reaction_button(pk, meeting, meeting_pk, title="Gilla", icon='mdi-thumb-up', color='primary'):
    """
    The v4 object that holds the information for reactions. We'll mostly create "like"-buttons
    """
    # Should be: ["proposal", "discussion_post"]
    allowed_models = [model_map[x] for x in getattr(meeting, 'like_context_types', [])]
    # This isn't strictly correct since disabled in old system means allowed. But the button is disabled anyway,
    # so it might be a good idea to look at this regardless
    change_roles = [role_map[x] for x in getattr(meeting, 'like_user_roles', [])]
    list_roles = ['participant']
    return {
        'pk': pk,
        'model': 'reactions.reactionbutton',
        'fields': {
            'title': title,
            'icon': icon,
            'color': color,
            'meeting': meeting_pk,
            'change_roles': change_roles,
            'list_roles': list_roles,
            'active': False,
            'allowed_models': allowed_models,
        },
    }


@debugencode
def export_reaction(pk, context, object_id, button_pk, ai_pk, userid, meeting_pk = None):
    """
    :param pk: reactions pk
    :param context: what's reacted on
    :param object_id: The proposal or discussion post pk
    :param button_pk:
    :param ai_pk:
    :param userid:
    :return: maybe return data if there are reactions

    A specific users click on a like-button or similar
    """

    if context.type_name == 'Proposal':
        content_type = ["proposal", "proposal"]
    elif context.type_name == 'DiscussionPost':
        content_type = ["discussion", "discussionpost"]
    else:
        raise ValueError("Wrong context: %s" % context)
    return {
        'pk': pk,
        'model': 'reactions.reaction',
        'fields': {
            "content_type":content_type,
            'object_id': object_id,
            'button': button_pk,
            'user': get_pk_for_userid(userid, ck_meeting_pk=meeting_pk, context=context),
            'agenda_item': ai_pk,
        },
    }


def django_format_datetime(dt, force=False):
    if force and not isinstance(dt, datetime):
        raise ValueError("%s is not a datetime instance" % dt)
    if dt is not None:
        return dt.isoformat()


class ERHandler:

    def __init__(self):
        self.cmp_vw_to_er = {}

    def track_original_er(self, er_data, vw_data):
        key = self.get_cmp_key(vw_data)
        if key not in self.cmp_vw_to_er:
            # Only save first key, they may be identical when using v3 ERs
            self.cmp_vw_to_er[key] = er_data['pk']

    def create_or_reuse(self, poll_data, er_data, vw_data, out_data):
        """
        :param poll_data: dict
        :param er_data: dict
        :param vw_data: list[dict]
        :param out_data: list[dict]
        :return: bool
        """
        key = self.get_cmp_key(vw_data)
        # Reuse data
        reused_pk = self.cmp_vw_to_er.get(key)
        if reused_pk:
            poll_data['fields']['electoral_register'] = reused_pk
            return False
        # Append new ER to data instead, we can't reuse
        self.cmp_vw_to_er[key] = er_data['pk']
        poll_data['fields']['electoral_register'] = er_data['pk']
        out_data.append(er_data)
        out_data.extend(vw_data)
        return True

    def get_cmp_key(self, vw_data):
        return tuple(sorted(
            [(x['fields']['user'], x['fields']['weight']) for x in vw_data]
        ))

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

    # Walk meetings and export contents
    ai_pk = 1
    meeting_group_pk = 1
    group_role_pk = 1
    group_membership_pk = 1
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
    reaction_button_pk = 1
    reaction_pk = 1

    meetings = [x for x in root.values() if x.type_name == 'Meeting']
    print("Exporting %s meetings" % len(meetings))
    for meeting_pk, meeting in enumerate(meetings, start=1):
        # Limit to a few meetings?
        if ONLY_MEETING_NAMES:
            if meeting.__name__ not in ONLY_MEETING_NAMES:
                print("SKIPPING: %s" % meeting.__name__)
                continue
        else:
            print("Exporting: %s" % meeting.__name__)

        if MEETING_NAMESPACE in meeting:
            add_error(meeting, "Multi-votes activated, adjust export", critical=True)
        # In case we need to adjust data
        meeting_out = export_meeting(meeting, meeting_pk)
        data.append(meeting_out)
        meeting_name_to_pk[meeting.__name__] = meeting_pk
        # Meeting groups are meeting local objects within VoteIT4
        # System users are global within VoteIT3, so if we convert sys users -> meeting group
        # we need to create several of them and replace them differently within meetings.
        userid_to_meeting_group_pk = {}
        for userid in meeting.system_userids:
            userid_to_meeting_group_pk[userid] = meeting_group_pk
            needed_userids.add(userid)
            sys_user = users[userid]
            data.append(
                export_meeting_group_system_user_like(sys_user, meeting_group_pk, meeting_pk, meeting)
            )
            # End meeting group loop
            meeting_group_pk += 1

        if meeting.system_userids:
            print(meeting.__name__ + ' has system users that will be groups: ' + ", ".join(meeting.system_userids))

        # keep track of users in meeting
        meeting_to_user_pks[meeting_pk] = set()

        # Export meeting roles
        maybe_new_er_userids = set()  # In case there's no ER, create one with these
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
                meeting_to_user_pks[meeting_pk].add(get_pk_for_userid(entry['userid'], context=meeting))
                # End roles loop
                meeting_roles_pk += 1
            else:
                add_error(meeting, 'Skipping meeting roles assigned to non-existing user: {userid}', userid=entry['userid'])

        # Export participant numbers
        pn_to_userid = {}

        pns = IParticipantNumbers(meeting)
        if len(pns.number_to_userid):
            pn_to_userid.update(pns.number_to_userid)
            pk_to_old_pns[pn_system_pk] = pns
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
                if userid != userid.lower():
                    add_error(meeting, 'Upppercase userid in PN: %s' % userid)

                data.append(
                    export_pn(pn_pk, pn, get_pk_for_userid(userid, context=meeting, msg="Missing user '{userid}' in PNS"), pn_system_pk, created_ts)
                )
                # End pn loop
                pn_pk += 1
            # End pn system
            pn_system_pk += 1

        # ERs created via votes and maybe original ers
        er_handler = ERHandler()

        # Electoral registers - might not exist
        electoral_registers = IElectoralRegister(meeting)
        lastest_er_pk = None
        # Note about ERs here, they may not reflect actual voters used during polls.
        # We'll use vote data later on to simply construct an ER to get voter weight right.
        for register in electoral_registers.registers.values():
            vw_data = []
            for userid in register['userids']:
                vw_data.append(
                    export_voter_weight(voter_weight_pk, electoral_register_pk,
                                        get_pk_for_userid(userid, context=meeting, ck_meeting_pk = meeting_pk, msg="Missing user '{userid}' in ER"))
                )
                # end voter weight loop
                voter_weight_pk += 1
            er_data = export_electoral_register(electoral_register_pk, created_ts=register['time'], meeting_pk=meeting_pk)
            er_handler.track_original_er(er_data, vw_data)
            data.append(er_data)
            data.extend(vw_data)
            lastest_er_pk = electoral_register_pk
            # end er loop
            electoral_register_pk += 1

        # In case we have no ERs
        if lastest_er_pk is None:
            vw_data = []
            for userid in maybe_new_er_userids:
                vw_data.append(
                    export_voter_weight(voter_weight_pk, electoral_register_pk,
                                        get_pk_for_userid(userid, context=meeting, ck_meeting_pk=meeting_pk,
                                                          msg="Missing user '{userid}' in ER"))
                )
                # end voter weight loop
                voter_weight_pk += 1
            er_data = export_electoral_register(electoral_register_pk, created_ts=meeting.start_time or meeting.created,
                                                meeting_pk=meeting_pk)
            er_handler.track_original_er(er_data, vw_data)
            data.append(er_data)
            data.extend(vw_data)
            lastest_er_pk = electoral_register_pk
            # end er loop
            electoral_register_pk += 1

        if MEETING_NAMESPACE in meeting:
            raise
            #print("Multivotes meeting: %s" % meeting.__name__)
            #export_meeting_group_mv_origin(va, meeting_group_pk, meeting_pk, meeting)

        if hasattr(meeting, VOTE_GROUPS_NAMESPACE):
            # Adjust exported meeting
            meeting_out['fields']['er_policy_name'] = 'main_subst_active'
            meeting_out['fields']['installed_dialect'] = 'ordinarie_och_ersattare'
            meeting_out['fields']['group_roles_active'] = True
            meeting_out['fields']['group_votes_active'] = True
            # Create group roles for this specific dialect
            from_vg_roles = ['proposer', 'potential_voter', 'discusser']
            data.append(
                export_group_role(group_role_pk, meeting_pk,
                                  title='Ordinarie', role_id='main', roles=from_vg_roles)
            )
            old_role_to_pk = {ROLE_PRIMARY: group_role_pk}
            group_role_pk += 1
            data.append(
                export_group_role(group_role_pk, meeting_pk,
                                  title='Ersättare', role_id='substitute', roles=from_vg_roles)
            )
            old_role_to_pk[ROLE_STANDIN] = group_role_pk
            group_role_pk += 1

            vg_data = getattr(meeting, VOTE_GROUPS_NAMESPACE)
            for vg in vg_data.values():
                data.append(export_meeting_group_vg_origin(vg, meeting_group_pk, meeting_pk, meeting))
                # Create membership with roles
                for userid, old_role in vg.items():
                    data.append(
                        export_group_membership(
                            group_membership_pk, get_pk_for_userid(userid, context=vg), meeting_group_pk, role_pk=old_role_to_pk.get(old_role)
                        )
                    )
                    group_membership_pk += 1
                # end VG loop
                meeting_group_pk+=1

        if hasattr(meeting, SFS_DELEGATIONS_NAMESPACE):
            import sfs_ga  # This is just a guard to avoid broken objects!
            meeting_out['fields']['er_policy_name'] = 'gv_auto_before_p'
            meeting_out['fields']['installed_dialect'] = 'sfsfum'
            meeting_out['fields']['group_roles_active'] = True
            meeting_out['fields']['group_votes_active'] = True
            # Create group roles for this specific dialect
            from_delegations_roles = ['proposer', 'potential_voter', 'discusser']
            data.append(
                export_group_role(group_role_pk, meeting_pk,
                                  title='Delegationsledare', role_id='leader', roles=from_delegations_roles)
            )
            delegation_leader_pk = group_role_pk
            group_role_pk += 1
            data.append(
                export_group_role(group_role_pk, meeting_pk,
                                  title='Medlem', role_id='member', roles=from_delegations_roles)
            )
            delegation_member_pk = group_role_pk
            group_role_pk += 1

            delegations_data = getattr(meeting, SFS_DELEGATIONS_NAMESPACE)
            for meeting_delegation in delegations_data.values():
                data.append(
                    export_meeting_group_sfs_origin(meeting_delegation, meeting_group_pk, meeting_pk, meeting)
                )
                # Create membership with roles - leaders
                for userid in meeting_delegation.leaders:
                    data.append(
                        export_group_membership(
                            group_membership_pk, get_pk_for_userid(userid, context=meeting_delegation), meeting_group_pk,
                            role_pk=delegation_leader_pk, votes=meeting_delegation.voters.get(userid),
                        )
                    )
                    group_membership_pk += 1
                # Create membership with roles - members
                for userid in meeting_delegation.members:
                    if userid in meeting_delegation.leaders:
                        # Only one role in v4!
                        continue
                    data.append(
                        export_group_membership(
                            group_membership_pk, get_pk_for_userid(userid, context=meeting_delegation), meeting_group_pk,
                            role_pk=delegation_member_pk, votes=meeting_delegation.voters.get(userid),
                        )
                    )
                    group_membership_pk += 1

                # end VG loop
                meeting_group_pk += 1
            #END SFS

        # Prep for reactions (like button)
        reaction_data = []

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
            #FIXME: Export LIKES for props and discussions
            for proposal in items['Proposal']:
                assert len(proposal.creators) == 1
                author_userid = proposal.creators[0]
                if author_userid in userid_to_meeting_group_pk:
                    kw = {'meeting_group_pk': userid_to_meeting_group_pk[author_userid]}
                else:
                    kw = {'author_pk': get_pk_for_userid(author_userid, ck_meeting_pk=meeting_pk, context=proposal)}
                data.append(
                    export_proposal(proposal, proposal_pk, ai_pk, meeting_pk=meeting_pk, **kw)
                )
                proposal_uid_to_pk[proposal.uid] = proposal_pk
                # Likes
                for like_userid in request.registry.getAdapter(proposal, IUserTags, name='like'):
                    local_reaction = export_reaction(reaction_pk, proposal, proposal_pk, reaction_button_pk, ai_pk, like_userid, meeting_pk = meeting_pk)
                    if local_reaction:
                        reaction_pk += 1
                        reaction_data.append(local_reaction)

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
                    kw = {'author_pk': get_pk_for_userid(author_userid, ck_meeting_pk=meeting_pk, context=discussion_post)}
                data.append(
                    export_discussion_post(discussion_post, discussion_post_pk, ai_pk, meeting_pk=meeting_pk, **kw)
                )
                # Likes
                for like_userid in request.registry.getAdapter(discussion_post, IUserTags, name='like'):
                    local_reaction = export_reaction(reaction_pk, discussion_post, discussion_post_pk, reaction_button_pk, ai_pk, like_userid, meeting_pk = meeting_pk)
                    if local_reaction:
                        reaction_pk += 1
                        reaction_data.append(local_reaction)
                # End post loop
                discussion_post_pk += 1

            for poll in items['Poll']:
                poll_out = export_poll(poll, poll_pk, meeting_pk, ai_pk, request, er_pk=lastest_er_pk)
                if not poll_out:
                    continue

                # Try to figure out voter weight and ER from poll
                if poll.get_workflow_state() == 'closed':
                    # We only care about this for closed - supporting ongoing polls is a bit too weird

                    # And export votes - find unique ones
                    non_cloned_votes = set()
                    clone_weight = Counter()

                    for vote in poll.values():
                        if vote.__name__ == vote.creator[0]:
                            non_cloned_votes.add(vote)
                        elif vote.creator[0]:
                            clone_weight[vote.creator[0]] += 1
                        else:
                            add_error(vote, "has no creator", critical=True)
                            continue

                    # Create ERs based on votes if there's nothing else to go on
                    er_out = export_electoral_register(electoral_register_pk, poll.start_time, meeting_pk, was_er=False)
                    vw_out = []
                    er_userids = set([x.__name__ for x in non_cloned_votes])
                    if clone_weight:
                        # We have to settle with the votes we've got
                        for userid in er_userids:
                            vw_out.append(
                                export_voter_weight(
                                    voter_weight_pk, electoral_register_pk,
                                    get_pk_for_userid(userid, context=poll, ck_meeting_pk=meeting_pk, msg="Missing user '{userid}' in poll"),
                                    weight=1 + clone_weight[userid]
                                )
                            )
                            voter_weight_pk += 1
                    else:
                        # We can create an ER from the users who had voting permission when the poll closed +
                        # any others that may have added a vote
                        if poll.voters_mark_closed:
                            er_userids.update(poll.voters_mark_closed)
                        for userid in er_userids:
                            vw_out.append(
                                export_voter_weight(
                                    voter_weight_pk, electoral_register_pk,
                                    get_pk_for_userid(userid, context=poll, ck_meeting_pk=meeting_pk, msg="Missing user '{userid}' in poll")
                                )
                            )
                            voter_weight_pk += 1
                    for vote in non_cloned_votes:
                        out = export_vote(vote, vote_pk, poll_pk, get_pk_for_userid(vote.creator[0], ck_meeting_pk=meeting_pk, context=vote))
                        if out:
                            data.append(out)
                            # End vote loop
                            vote_pk += 1

                    # We'll swap polls ER if needed
                    if er_handler.create_or_reuse(poll_out, er_out, vw_out, data):
                        # Returns None if a new was created
                        electoral_register_pk += 1

                # Finally append the poll data
                data.append(poll_out)
                # End poll loop
                poll_pk += 1

            # END AI block - keep this last!
            ai_pk += 1

        # Finish up reaction exports since we've collected all now.
        if reaction_data:
            data.append(
                export_reaction_button(reaction_button_pk, meeting, meeting_pk)
            )
            data.extend(reaction_data)
            #End reaction/like stuff
            reaction_button_pk += 1

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
                    add_error(meeting, "'female_priority' speaker lists exported as priority.")
                    method_name = 'priority'
                    settings['max_times'] = speaker_list_count
                    assert isinstance(settings['max_times'], int)
                elif v3_settings['speaker_list_plugin'] == 'global_lists':
                    add_error(meeting, "'global_lists' speaker lists aren't handled, exported as 'simple'. (path is meeting)",) # We might not need to care about this
                    method_name = 'simple'
            if not method_name:
                import pdb;pdb.set_trace()
            data.append(
                export_speaker_list_system(speaker_system_pk, meeting_pk, method_name, settings, safe_positions)
            )
            # We're not exporting active lists, queues or anything like that. Everything should be finished.
            for (k, sl) in sls.items():
                if '/' in k:
                    ai_uid, _ = k.split('/')
                else:
                    ai_uid = k
                if ai_uid not in ai_uid_to_pk:
                    # print("UID %s belongs to a deleted agenda item, won't export that speaker list" % ai_uid)
                    continue
                for user_pn, spoken_times in sl.speaker_log.items():
                    if not spoken_times:
                        continue
                    if user_pn not in pn_to_userid:
                        continue  # We can't export historic items that are from an anonymous user
                    # This is certainly wrong, but we don't have any other data to use :(
                    try:
                        created_ts = sl.__parent__.modified
                    except AttributeError:
                        ai = resolve_uid(request, ai_uid, perm=None)
                        created_ts = ai.created
                    for seconds in spoken_times:
                        data.append(
                            export_speaker(speaker_pk, get_pk_for_userid(pn_to_userid[user_pn], ck_meeting_pk=meeting_pk),
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
    # FIXME: Vad gör vi med ballot_data för historiska omröstningar?
    # ALREADY FIXED: Exporten av resultatdata för schulze använder ranking istället för rating, så vi måste vända på siffrorna!

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
        sys.exit("!!! %s critical error types - won't write!" % len(critical_errors))
    filename = 'voteit4_export.json'
    print("Writing %s" % filename)
    with open(filename, "w") as stream:
        dump(data, stream)


if __name__ == '__main__':
    main()
