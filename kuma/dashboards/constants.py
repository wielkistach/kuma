from django.utils.translation import ugettext_lazy as _

KNOWN_AUTHORS_GROUP = 'Known Authors'

# Rounded to nearby 7-day period for weekly cycles
SPAM_PERIODS = (
    (1, _('Daily')),
    (7, _('Weekly')),
    (28, _('Monthly')),
    (91, _('Quarterly')),
)
SPAM_STAT_CATEGORY_OPTIONS = (
    ('group', ('staff', 'known', 'other')),
    ('published', ('published', 'blocked')),
    ('content', ('spam', 'ham', 'unknown')),
    ('fresh', ('new', 'edit')),
    ('lang', ('en', 'other')),
)
SPAM_STAT_CATEGORIES = set(name for name, opts in SPAM_STAT_CATEGORY_OPTIONS)
SPAM_DASHBOARD_DERIVED_STATS = [
    {
        'name': _('Total Attempted Edits'),
        'id': 'total',
        'derived': {}
    }, {
        'name': _('Published Ham'),
        'id': 'published_ham',
        'derived': {'content': 'ham', 'published': 'published'},
    }, {
        'name': _('Blocked Spam'),
        'id': 'blocked_spam',
        'derived': {'content': 'spam', 'published': 'blocked'},
        'rate_denominiator': 'total',
        'rate_name': _('Blocked Spam Rate'),
    }, {
        'name': _('Published Spam'),
        'id': 'published_spam',
        'derived': {'content': 'spam', 'published': 'published'},
        'rate_denominiator': 'total',
        'rate_name': _('Published Spam Rate'),
    }, {
        'name': _('Blocked Ham'),
        'id': 'blocked_ham',
        'derived': {'content': 'ham', 'published': 'blocked'},
        'rate_denominiator': 'total',
        'rate_name': _('Blocked Ham Rate'),
    }
]
SPAM_RATE_ID_SUFFIX = '_rate'
