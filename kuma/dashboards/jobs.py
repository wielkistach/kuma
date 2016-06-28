from kuma.core.jobs import KumaJob
from .utils import spam_day_stats


class SpamDayStatsJob(KumaJob):
    """Store spam stats for multiple days."""
    lifetime = 60 * 60 * 24 * 7
    fetch_on_miss = True
    version = 1

    def fetch(self, day):
        return spam_day_stats(day)
