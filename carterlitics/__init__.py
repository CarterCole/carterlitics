"""carterlitics: first-party, self-hosted analytics for a handful of sites.

One collector (a Python Lambda, stdlib only) behind each site's CloudFront at `/_c/*`,
one script for the page (`/_c/c.js`), CloudFront's own access logs for everything the
script can't see, and a console at `/_c/admin/`.
"""

__version__ = "0.1.0"
