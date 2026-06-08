"""gads-agent — autonomous Google Ads optimization for a single Search campaign.

Safety model: DRY-RUN by default. No API writes ever occur without the explicit
--apply flag, and every write is bounded by the caps in config.yaml and logged
for reversal. See README.md.
"""

__version__ = "0.1.0"
