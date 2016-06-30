from collections import Counter, defaultdict
from itertools import product
import datetime
import json

from django.contrib.auth.models import Group
from django.utils.six.moves.urllib.parse import urlparse
from django.utils.translation import ugettext as _

from kuma.core.urlresolvers import reverse
from kuma.users.models import User
from kuma.wiki.models import (DocumentSpamAttempt,
                              Revision,
                              RevisionAkismetSubmission)
from .constants import (KNOWN_AUTHORS_GROUP,
                        SPAM_DASHBOARD_DERIVED_STATS,
                        SPAM_PERIODS,
                        SPAM_RATE_ID_SUFFIX,
                        SPAM_STAT_CATEGORIES,
                        SPAM_STAT_CATEGORY_OPTIONS)


def spam_special_groups():
    """Gather IDs for special groups."""
    staff = User.objects.filter(is_staff=True)
    staff_ids = set(staff.values_list('id', flat=True))
    try:
        known_group = Group.objects.get(name=KNOWN_AUTHORS_GROUP)
    except Group.DoesNotExist:
        known_ids = set()
    else:
        known_ids = set(known_group.user_set.values_list('id', flat=True))

    return (
        ('staff', staff_ids),
        ('known', known_ids - staff_ids),
    )


def spam_day_stats(day):
    """
    Generate spam statistics for a day.

    Return is a Python dictionary, ready for JSON serialization, like:
    {
        'version': 1,
        'generated': '2016-06-28T11:01:13.477781',
        'day': '2016-05-05',
        'needs_review': False,
        'events': {
            'other_published_ham_edit_en': 13,
            'staff_published_ham_edit_en': 26,
            'other_blocked_ham_edit_other': 1,
            'other_published_ham_new_en': 5,
            'other_published_ham_edit_other': 58,
            'other_published_ham_new_other': 13,
            'staff_published_ham_edit_other': 12
        }
    }
    """
    counts = Counter()
    next_day = day + datetime.timedelta(days=1)
    special_groups = spam_special_groups()
    document_data = dict()

    # Gather published revisions for the day
    revision_ids = (
        Revision.objects
        .filter(created__range=(day, next_day))
        .values_list('id', flat=True))
    for rev_id in revision_ids:
        rev = (
            Revision.objects
            .only('id', 'document_id', 'creator_id')
            .get(id=rev_id))

        # Is the author in a special group?
        group = 'other'
        for name, user_ids in special_groups:
            if rev.creator_id in user_ids:
                group = name
                break

        # Was the revision identified as spam?
        if rev.akismet_submissions.filter(type='spam').exists():
            content = 'spam'
        else:
            content = 'ham'

        # Calculate document data
        # Often a document gets several edits in a day, so calulate once
        if rev.document_id not in document_data:
            first_rev = rev.document.revisions.order_by('created')[0]
            doc_lang = 'other' if rev.document.parent_id else 'en'
            document_data[rev.document_id] = (first_rev, doc_lang)
        first_rev, doc_lang = document_data[rev.document_id]

        # Was it a new page?
        if first_rev == rev:
            fresh = 'new'
        else:
            fresh = 'edit'

        # Update the count for this revision type
        key = '_'.join((group, 'published', content, fresh, doc_lang))
        counts[key] += 1

    # Gather blocked edits for the day
    needs_review = False
    blocked_edits = (
        DocumentSpamAttempt.objects
        .filter(created__range=(day, next_day)))
    for blocked in blocked_edits:
        # Is the author in a special group?
        group = 'other'
        for name, user_ids in special_groups:
            if rev.creator_id in user_ids:
                group = name
                break

        # Is it a false positive?
        if blocked.review == DocumentSpamAttempt.HAM:
            content = 'ham'
        elif blocked.review == DocumentSpamAttempt.SPAM:
            content = 'spam'
        else:
            if blocked.review == DocumentSpamAttempt.NEEDS_REVIEW:
                needs_review = True
            continue

        # Is it a edit? What language?
        if blocked.document_id:
            fresh = 'edit'
            if blocked.document.parent_id:
                lang = 'other'
            else:
                lang = 'en'
        else:
            fresh = 'new'
            data = json.loads(blocked.data or '{}')
            lang = data.get('blog_lang')
            if not lang:
                lang = 'other'  # Unknown, assume other
            elif lang.startswith('en'):
                lang = 'en'
            else:
                lang = 'other'

        # Update the count for this blocked edit type
        key = '_'.join((group, 'blocked', content, fresh, doc_lang))
        counts[key] += 1

    # Return the daily stats
    data = {
        'version': 1,
        'generated': datetime.datetime.now().isoformat(),
        'day': day.isoformat(),
        'needs_review': needs_review,
        'events': dict(counts)
    }
    return data


def spam_derived_stat(events, **categories):
    """
    Calculate the derived statistic from events.

    Examples:
    total = spam_derived_stat(events)
    spam_count = spam_derived_stat(events, content='spam')
    staff_published_edits = spam_derived_stat(
        events, group='staff', published='published', fresh='edit')
    """
    for category in categories:
        assert category in SPAM_STAT_CATEGORIES, (
            'Invalid category %s' % category)
    parts = []
    for category, options in SPAM_STAT_CATEGORY_OPTIONS:
        select = categories.get(category)
        if select:
            assert select in options, 'Invalid: %s=%s' % (category, select)
            parts.append((select,))
        else:
            parts.append(options)

    count = 0
    for key_parts in product(*parts):
        key = '_'.join(key_parts)
        count += events.get(key, 0)

    return count


def spam_dashboard_historical_stats(
        periods=None, end_date=None, derived_stats=None, summary=None):
    """
    Gather spam statistics for a range of dates, with derived stats.

    Keywords Arguments:
    periods - a sequence of (days, name) tuples
    end_date - The ending anchor date for the statistics
    derived_stats - a sequence of derived stats definitions
    summary - the period to use for the summary (last if omitted)
    """
    from .jobs import SpamDayStatsJob

    periods = periods or SPAM_PERIODS
    end_date = end_date or (datetime.date.today() - datetime.timedelta(days=1))
    derived_stats = derived_stats or SPAM_DASHBOARD_DERIVED_STATS
    summary = summary or periods[-1][0]

    assert periods, 'Must define at least one period'
    assert summary in [per[0] for per in periods], "Invalid summary period"

    # Determine the dates for the given periods
    newest = end_date
    oldest = newest
    spans = []
    for length, period_id in periods:
        end = end_date
        mid = end - datetime.timedelta(days=length)
        start = mid - datetime.timedelta(days=length)
        oldest = min(start, oldest)
        spans.append((length, period_id, end, mid, start))

    # Gather columns
    category_options = [opts for category, opts in SPAM_STAT_CATEGORY_OPTIONS]
    raw_columns = []
    for parts in product(*category_options):
        key = '_'.join(parts)
        raw_columns.append(key)
    derived_columns = []
    for stat_def in derived_stats:
        derived_columns.append(stat_def['id'])
        rate_denominiator = stat_def.get('rate_denominiator')
        if rate_denominiator:
            derived_columns.append(stat_def['id'] + SPAM_RATE_ID_SUFFIX)
    columns = derived_columns + raw_columns

    # Iterate over daily stats across the periods
    day = end_date
    events = []
    trends = defaultdict(Counter)
    while day > oldest:
        # Gather daily raw stats
        raw_events = SpamDayStatsJob().get(day)['events']
        # TODO: regenerate if needs_review and stale

        # Create 0 records for missing raw events
        day_events = dict()
        for column_id in raw_columns:
            day_events[column_id] = raw_events.get(column_id, 0)

        # Calculated derived statistics
        for stat_def in derived_stats:
            derived = spam_derived_stat(day_events, **stat_def['derived'])
            day_events[stat_def['id']] = derived

            # Calculate derived rate
            rate_denominiator = stat_def.get('rate_denominiator')
            if rate_denominiator:
                denom = day_events[rate_denominiator]
                if denom:
                    rate = float(derived) / float(denom)
                else:
                    rate = 0.0
                day_events[stat_def['id'] + SPAM_RATE_ID_SUFFIX] = rate

        # Store raw and derived statistics for the day
        events.append((day, day_events))

        # Accumulate trends over periods
        for length, period_id, end, mid, start in spans:
            if end >= day > start:
                current = day > mid
                trend_key = (length, current)
                for column_id in columns:
                    if not column_id.endswith(SPAM_RATE_ID_SUFFIX):
                        stat = day_events[column_id]
                        trends[trend_key][column_id] += stat

        # Continue with one day back in history
        day -= datetime.timedelta(days=1)

    # Prepare output data
    data = {
        'version': 1,
        'generated': datetime.datetime.now().isoformat(),
        'day': end_date.isoformat(),
        'categories': dict(SPAM_STAT_CATEGORY_OPTIONS),
        'change_types': [
            {
                'id': 'changetype_new',
                'fresh': 'new',
                'lang': 'en',
            }, {
                'id': 'changetype_edit',
                'fresh': 'edit',
                'lang': 'en',
            }, {
                'id': 'changetype_newtrans',
                'fresh': 'new',
                'lang': 'other',
            }, {
                'id': 'changetype_edittrans',
                'fresh': 'edit',
                'lang': 'other',
            }
        ],
        'trends': {
            'over_time': []
        },
    }

    # Collect trends over time
    summary_period = None
    for length, period_id, end, mid, start in spans:
        period_data = {
            'id': period_id,
            'days': length,
            'current': {
                'start': (mid + datetime.timedelta(days=1)).isoformat(),
                'end': end.isoformat(),
            },
            'previous': {
                'start': (start + datetime.timedelta(days=1)).isoformat(),
                'end': mid.isoformat(),
            }
        }
        for group in ('current', 'previous'):
            current = group == 'current'
            key = (length, current)

            gdict = period_data[group]
            gdict.update(trends[key])
            for stat_def in derived_stats:
                rate_denominiator = stat_def.get('rate_denominiator')
                if rate_denominiator:
                    derived = gdict[stat_def['id']]
                    denom = gdict[rate_denominiator]
                    if denom:
                        rate = float(derived) / float(denom)
                    else:
                        rate = 0.0
                    gdict[stat_def['id'] + SPAM_RATE_ID_SUFFIX] = rate
        if length == summary:
            summary_period = period_data
        data['trends']['over_time'].append(period_data)

    # Get summary from trends
    data['summary'] = {'days': summary_period['days']}
    data['summary'].update(summary_period['current'])

    # Add raw data
    data['raw'] = {
        'columns': ['date'] + columns,
        'data': []
    }
    for day, metrics in events:
        row = [day.isoformat()]
        for column_id in columns:
            raw = metrics.get(column_id, 0)
            if column_id.endswith(SPAM_RATE_ID_SUFFIX):
                raw = "%0.02f%%" % (100.0 * raw)
            row.append(raw)
        data['raw']['data'].append(row)

    return data


def spam_dashboard_recent_events():
    """Gather data for recent spam events."""
    data = {
        'now': datetime.datetime.now().isoformat(),
        'recent_spam': [],
        'recent_blocked': [],
    }

    # Gather recent published spam
    recent_spam = (RevisionAkismetSubmission.objects
                   .filter(type='spam')
                   .order_by('-id')[:20])
    for rs in recent_spam:
        revision = rs.revision
        document = revision.document

        # How long was it active?
        revision_ids = list(
            document.revisions
            .order_by('-id')
            .values_list('id', flat=True))
        idx = revision_ids.index(revision.id)
        if idx == 0:
            if document.deleted:
                time_active = _('Deleted')
            else:
                time_active = _('Current')
        else:
            next_rev_id = revision_ids[idx - 1]
            next_rev = Revision.objects.only('created').get(id=next_rev_id)
            time_active_raw = next_rev.created - revision.created
            time_active = int(time_active_raw.total_seconds())

        # What type of change was it?
        previous = revision.previous
        if previous:
            if document.parent:
                change_type = 'changetype_edittrans'
            else:
                change_type = 'changetype_edit'
        else:
            if document.parent:
                change_type = 'changetype_newtrans'
            else:
                change_type = 'changetype_new'

        # Gather table data
        data['recent_spam'].append({
            'date': revision.created.date(),
            'time_active': time_active,
            'revision_id': revision.id,
            'revision_path': revision.get_absolute_url(),
            'change_type': change_type,
            'moderator': rs.sender.username,
            'document_path': revision.document.get_absolute_url(),
        })

    # Gather recent blocked edits
    recent_blocked = (
        DocumentSpamAttempt.objects.order_by('-id')[:20])
    for rb in recent_blocked:
        # What kind of change was attempted?
        if rb.document:
            has_link = True
            document_path = rb.document.get_absolute_url()
            if rb.document.parent:
                change_type = 'changetype_edittrans'
            else:
                change_type = 'changetype_edit'
        else:
            has_link = False
            if rb.data:
                data = json.loads(rb.data)
                if data['blog_lang'].startswith('en'):
                    change_type = 'changetype_new'
                else:
                    change_type = 'changetype_newtrans'
                url = data['permalink']
                document_path = urlparse(url).path
            else:
                document_path = '/' + rb.slug
                change_type = 'unknown'

        data['recent_blocked'].append({
            'date': rb.created.date(),
            'admin_url': reverse('admin:wiki_documentspamattempt_change',
                                 args=(rb.id,)),
            'change_type': change_type,
            'moderator': rb.reviewer.username if rb.reviewer else '',
            'review': rb.get_review_display(),
            'has_link': has_link,
            'document_path': document_path,
        })
    return data
