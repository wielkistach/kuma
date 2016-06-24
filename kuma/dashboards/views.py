from collections import Counter
import datetime
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import Group
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_GET
import waffle

from kuma.core.decorators import login_required
from kuma.core.utils import paginate
from kuma.wiki.models import (Document,
                              DocumentSpamAttempt,
                              Revision,
                              RevisionAkismetSubmission)

from .forms import RevisionDashboardForm
from . import PAGE_SIZE


@require_GET
def revisions(request):
    """Dashboard for reviewing revisions"""

    filter_form = RevisionDashboardForm(request.GET)
    page = request.GET.get('page', 1)

    revisions = (Revision.objects.prefetch_related('creator__bans',
                                                   'document',
                                                   'akismet_submissions')
                                 .order_by('-id')
                                 .defer('content'))

    query_kwargs = False
    exclude_kwargs = False

    # We can validate right away because no field is required
    if filter_form.is_valid():
        query_kwargs = {}
        exclude_kwargs = {}
        query_kwargs_map = {
            'user': 'creator__username__istartswith',
            'locale': 'document__locale',
            'topic': 'slug__icontains',
        }

        # Build up a dict of the filter conditions, if any, then apply
        # them all in one go.
        for fieldname, kwarg in query_kwargs_map.items():
            filter_arg = filter_form.cleaned_data[fieldname]
            if filter_arg:
                query_kwargs[kwarg] = filter_arg

        start_date = filter_form.cleaned_data['start_date']
        if start_date:
            end_date = (filter_form.cleaned_data['end_date'] or
                        datetime.datetime.now())
            query_kwargs['created__range'] = [start_date, end_date]

        preceding_period = filter_form.cleaned_data['preceding_period']
        if preceding_period:
            # these are messy but work with timedelta's seconds format,
            # and keep the form and url arguments human readable
            if preceding_period == 'month':
                seconds = 30 * 24 * 60 * 60
            if preceding_period == 'week':
                seconds = 7 * 24 * 60 * 60
            if preceding_period == 'day':
                seconds = 24 * 60 * 60
            if preceding_period == 'hour':
                seconds = 60 * 60
            # use the form date if present, otherwise, offset from now
            end_date = (filter_form.cleaned_data['end_date'] or
                        timezone.now())
            start_date = end_date - datetime.timedelta(seconds=seconds)
            query_kwargs['created__range'] = [start_date, end_date]

        authors_filter = filter_form.cleaned_data['authors']
        if (not filter_form.cleaned_data['user'] != '' and
           authors_filter not in ['', str(RevisionDashboardForm.ALL_AUTHORS)]):

            # The 'Known Authors' group
            group, created = Group.objects.get_or_create(name="Known Authors")
            # If the filter is 'Known Authors', then query for the
            # 'Known Authors' group
            if authors_filter == str(RevisionDashboardForm.KNOWN_AUTHORS):
                query_kwargs['creator__groups__pk'] = group.pk
            # Else query must be 'Unknown Authors', so exclude the
            # 'Known Authors' group
            else:
                exclude_kwargs['creator__groups__pk'] = group.pk

    if query_kwargs or exclude_kwargs:
        revisions = revisions.filter(**query_kwargs).exclude(**exclude_kwargs)

    revisions = paginate(request, revisions, per_page=PAGE_SIZE)

    context = {
        'revisions': revisions,
        'page': page,
        'show_ips': (
            waffle.switch_is_active('store_revision_ips') and
            request.user.is_superuser
        ),
        'show_spam_submission': (
            request.user.is_authenticated() and
            request.user.has_perm('wiki.add_revisionakismetsubmission')
        ),
    }

    # Serve the response HTML conditionally upon request type
    if request.is_ajax():
        template = 'dashboards/includes/revision_dashboard_body.html'
    else:
        template = 'dashboards/revisions.html'
        context['form'] = filter_form

    return render(request, template, context)


@require_GET
def user_lookup(request):
    """Returns partial username matches"""
    userlist = []

    if request.is_ajax():
        user = request.GET.get('user', '')
        if user:
            matches = get_user_model().objects.filter(username__istartswith=user)
            for match in matches:
                userlist.append({'label': match.username})

    data = json.dumps(userlist)
    return HttpResponse(data, content_type='application/json; charset=utf-8')


@require_GET
def topic_lookup(request):
    """Returns partial topic matches"""
    topiclist = []

    if request.is_ajax():
        topic = request.GET.get('topic', '')
        if topic:
            matches = Document.objects.filter(slug__icontains=topic)
            for match in matches:
                topiclist.append({'label': match.slug})

    data = json.dumps(topiclist)
    return HttpResponse(data,
                        content_type='application/json; charset=utf-8')


spam_periods = [
    (1, _('Daily')),
    (7, _('Weekly')),
    (30, _('Monthly')),
    (90, _('Quarterly')),
]


def spam_stats(periods, summary=90):
    """
    Generate spam statistics for the given periods.

    Keywords Arguments:
    * periods - a sequence of (days, name) tuples
    * summary - the number of days in the summary period
    """
    assert periods, 'Must define at least one period'
    period_names = dict(periods)
    assert summary in period_names, 'Summary period is not in periods'

    # Determine the dates for the given periods
    newest = datetime.date.today() - datetime.timedelta(days=1)
    oldest = newest
    spans = []
    for length, name in periods:
        start = datetime.date.today() - datetime.timedelta(days=1)
        mid = start - datetime.timedelta(days=length)
        end = mid - datetime.timedelta(days=length)
        oldest = min(end, oldest)
        spans.append((length, start, mid, end))

    # Gather published revisions by span
    published = Counter()
    published_revisions = (
        Revision.objects
        .filter(created__range=(oldest, newest))
        .values_list('created', flat=True))
    for created in published_revisions:
        day = created.date()
        for length, start, mid, end in spans:
            if day > start or day <= end:
                continue  # Not in this span
            current = day > mid
            key = (length, current)
            published[key] += 1

    # Gather published spam by span
    published_spam = Counter()
    akismet_spam = (
        RevisionAkismetSubmission.objects
        .filter(revision__created__range=(oldest, newest))
        .filter(type=RevisionAkismetSubmission.SPAM_TYPE)
        .values_list('revision__created', flat=True))
    for created in akismet_spam:
        day = created.date()
        for length, start, mid, end in spans:
            if day > start or day <= end:
                continue  # Not in this span
            current = day > mid
            key = (length, current)
            published_spam[key] += 1

    # Gather blocked ham and spam
    blocked_ham = Counter()
    blocked_spam = Counter()
    blocked_content = (
        DocumentSpamAttempt.objects
        .filter(created__range=(oldest, newest))
        .filter(review__in=(DocumentSpamAttempt.HAM,
                            DocumentSpamAttempt.SPAM))
        .values_list('created', 'review'))
    for created, review in blocked_content:
        day = created.date()
        for length, start, mid, end in spans:
            if day > start or day <= end:
                continue  # Not in this span
            current = day > mid
            key = (length, current)
            if review == DocumentSpamAttempt.HAM:
                blocked_ham[key] += 1
            else:
                blocked_spam[key] += 1

    # Collect data into trends
    data = {'trends': {'over_time': []}}
    summary_period = None
    for length, start, mid, end in spans:
        period_data = {
            'name': period_names[length],
            'days': length,
            'current': {
                'start': (mid + datetime.timedelta(days=1)).isoformat(),
                'end': start.isoformat(),
            },
            'previous': {
                'start': mid.isoformat(),
                'end': (end + datetime.timedelta(days=1)).isoformat(),
            }
        }
        for group in ('current', 'previous'):
            current = group == 'current'
            key = (length, current)
            gdict = period_data[group]
            gdict['published'] = published[key]
            gdict['blocked_ham'] = blocked_ham[key]
            gdict['blocked_spam'] = blocked_spam[key]
            gdict['published_spam'] = published_spam[key]
            gdict['blocked'] = gdict['blocked_ham'] + gdict['blocked_spam']
            gdict['published_ham'] = (
                gdict['published'] - gdict['published_spam'])
            gdict['total'] = gdict['published'] + gdict['blocked']
        if length == summary:
            summary_period = period_data
        data['trends']['over_time'].append(period_data)

    # Get summary from trends
    data['summary'] = {'days': summary_period['days']}
    data['summary'].update(summary_period['current'])

    return data


@require_GET
@login_required
@permission_required((
    'wiki.add_revisionakismetsubmission',
    'wiki.add_documentspamattempt',
    'wiki.add_userban'), raise_exception=True)
def spam(request):
    """Dashboard for spam moderators."""

    data = spam_stats(spam_periods)
    '''
    old_data = {
        'summary': {
            'days': 90,
            'total': 1000,
            'published': 993,
            'blocked': 7,
            'published_ham': 990,
            'published_spam': 3,
            'blocked_ham': 4,
            'blocked_spam': 3,
        },
        'trends': {
            'over_time': [
                {
                    'name': _('Daily'),
                    'days': 1,
                    'current': {
                        'start': datetime.date.today() - datetime.timedelta(days=1),
                        'end': datetime.date.today() - datetime.timedelta(days=1),
                    },
                    'previous': {
                        'start': datetime.date.today() - datetime.timedelta(days=2),
                        'end': datetime.date.today() - datetime.timedelta(days=2),
                    },
                },
                {
                    'name': _('Weekly'),
                    'days': 7,
                    'current': {},
                    'previous': {},
                },
                {
                    'name': _('Monthly'),
                    'days': 30,
                    'current': {},
                    'previous': {},
                },
                {
                    'name': _('Quarterly'),
                    'days': 90,
                    'current': {},
                    'previous': {},
                },
            ],
            'edit_type': [

            ],
        },
    }
    '''

    return render(request, 'dashboards/spam.html', data)
