"""The immutable v1 whitespace contract shared with the review migration."""

from typing import Final

# Unicode White_Space, plus the four information separators already considered
# blank by the Python writer. Do not use the database's locale-specific [:space:].
REVIEW_REASON_WHITESPACE_V1: Final = (
    "\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
REVIEW_REASON_NONBLANK_V1: Final = f"[^{REVIEW_REASON_WHITESPACE_V1}]"
